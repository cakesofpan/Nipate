"""
services/didit.py
──────────────────
Identity verification via Didit (https://didit.me) — a hosted KYC provider
covering 4,000+ document types across 220+ countries, including Kenyan
national IDs and passports.

Replaces the purely-manual "upload a photo, wait for an admin to look at it"
flow with an automated one: the applicant is sent to Didit's hosted capture
UI (ID scan + liveness + face match), and Didit calls our webhook with a
structured decision once they finish. The old manual upload/admin-review
endpoints are kept in routes/auth.py and routes/admin.py as a fallback for
cases where Didit is down or a manual override is needed.

IMPORTANT — Kenya-specific caveat
──────────────────────────────────
Didit's document OCR + liveness + face-match features apply broadly across
supported document types/countries and cover Kenyan national IDs and
passports for capture and authenticity checks. Real-time cross-checking
against Kenya's IPRS government registry specifically is a separate,
country-specific integration that is NOT included here — treat an
"Approved" decision as "the document looks genuine and matches the
selfie," not as "confirmed against the Kenyan national database."

Setup
─────
1. Create a free account at https://business.didit.me (500 verifications/month free).
2. Create a KYC workflow (ID_VERIFICATION + LIVENESS + FACE_MATCH) and copy its workflow_id.
3. Copy your API key from the Business Console.
4. Register a webhook destination pointing at
   https://your-domain/api/webhooks/didit and copy the webhook secret it
   generates (shown once).
5. Set DIDIT_API_KEY, DIDIT_WORKFLOW_ID, DIDIT_WEBHOOK_SECRET in .env.

If DIDIT_API_KEY/DIDIT_WORKFLOW_ID are not configured, session creation
raises a clear error rather than silently failing, since ID verification is
a hard requirement for report submission — unlike SMS/email, there's no
sensible way to "silently no-op" this feature.
"""

import hashlib
import hmac
import json
import logging
import time

import aiohttp

from backend.config import (
    DIDIT_BASE_URL,
    DIDIT_API_KEY,
    DIDIT_WORKFLOW_ID,
    DIDIT_WEBHOOK_SECRET,
    DIDIT_ENABLED,
)

log = logging.getLogger(__name__)

# Reject webhook deliveries whose timestamp is older than this — defends
# against replay attacks (a captured/re-sent webhook being replayed later).
WEBHOOK_MAX_AGE_SECONDS = 300

# Didit statuses that represent a completed, human-facing decision.
# ("In Review" / "Not Finished" / "Resubmitted" mean "still pending".)
_TERMINAL_STATUSES = {"Approved", "Declined", "Abandoned", "Expired"}


class DiditError(Exception):
    """Raised when a Didit API call fails or the provider isn't configured."""


def _headers() -> dict:
    return {"x-api-key": DIDIT_API_KEY, "Content-Type": "application/json"}


# ── Session creation ────────────────────────────────────────────────────────────

async def create_verification_session(user_id: str, callback_url: str) -> dict:
    """
    Create a hosted Didit verification session for a user.

    Args:
        user_id:      Our internal Supabase user UUID. Sent as `vendor_data` so
                       the webhook can map the result back to a profile without
                       needing a lookup table.
        callback_url: Where Didit redirects the user's browser after they finish
                       the hosted flow (NOT the webhook — that's configured once,
                       server-side, in the Didit Business Console).

    Returns:
        {"session_id": str, "verification_url": str, "session_token": str}

    Raises:
        DiditError if Didit isn't configured or the API call fails.
    """
    if not DIDIT_ENABLED:
        raise DiditError(
            "Didit is not configured (DIDIT_API_KEY / DIDIT_WORKFLOW_ID missing). "
            "Set these in .env to enable identity verification."
        )

    payload = {
        "workflow_id": DIDIT_WORKFLOW_ID,
        "vendor_data": user_id,
        "callback": callback_url,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{DIDIT_BASE_URL}/v3/session/",
                json=payload,
                headers=_headers(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                body = await resp.json()

                if resp.status not in (200, 201):
                    log.error("Didit session creation failed: HTTP %s — %s", resp.status, body)
                    raise DiditError(f"Didit returned HTTP {resp.status}: {body}")

                session_id = body.get("session_id")
                # Field name has varied across Didit doc revisions — accept either.
                verification_url = body.get("verification_url") or body.get("session_url") or body.get("url")

                if not session_id or not verification_url:
                    log.error("Didit session response missing expected fields: %s", body)
                    raise DiditError("Didit session response was missing session_id/verification_url.")

                log.info("Didit session created for user %s: %s", user_id, session_id)
                return {
                    "session_id": session_id,
                    "verification_url": verification_url,
                    "session_token": body.get("session_token"),
                }

    except aiohttp.ClientError as exc:
        log.error("Didit session creation network error: %s", exc)
        raise DiditError(f"Could not reach Didit: {exc}") from exc


# ── Session retrieval (polling fallback) ────────────────────────────────────────

async def get_session_decision(session_id: str) -> dict | None:
    """
    Fetch the full verification result for a session. Used as a fallback when
    a webhook delivery is missed, or for an admin to inspect a decision.
    Returns None on any failure rather than raising, since this is typically
    used opportunistically (e.g. "check current status" from an admin UI).
    """
    if not DIDIT_ENABLED:
        return None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{DIDIT_BASE_URL}/v3/session/{session_id}/decision/",
                headers=_headers(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    log.warning("Didit decision fetch failed for %s: HTTP %s", session_id, resp.status)
                    return None
                return await resp.json()
    except aiohttp.ClientError as exc:
        log.warning("Didit decision fetch network error for %s: %s", session_id, exc)
        return None


# ── Webhook signature verification ──────────────────────────────────────────────

def verify_webhook(raw_body: bytes, headers: dict) -> tuple[bool, dict | None]:
    """
    Verify a Didit webhook delivery and return (is_valid, parsed_payload).

    Tries three signature methods in order of reliability, matching Didit's
    own documented fallback chain:
      1. X-Signature-V2  — HMAC-SHA256 over the canonically re-serialised JSON
                            (sort_keys, compact separators) — immune to
                            whitespace/re-encoding differences.
      2. X-Signature     — HMAC-SHA256 over the exact raw request bytes.
      3. X-Signature-Simple — HMAC-SHA256 over "{timestamp}:{session_id}:
                            {status}:{webhook_type}" — coarser, used only if
                            the other two are unavailable or fail.

    Also enforces a freshness check on X-Timestamp to reject replayed
    deliveries older than WEBHOOK_MAX_AGE_SECONDS.

    Returns (False, None) on any failure — caller should respond 401.
    """
    if not DIDIT_WEBHOOK_SECRET:
        log.error("DIDIT_WEBHOOK_SECRET not configured — rejecting webhook.")
        return False, None

    timestamp = headers.get("X-Timestamp") or headers.get("x-timestamp")
    if not timestamp:
        log.warning("Didit webhook missing X-Timestamp header.")
        return False, None

    try:
        age = abs(int(time.time()) - int(timestamp))
    except ValueError:
        log.warning("Didit webhook X-Timestamp is not a valid integer: %s", timestamp)
        return False, None

    if age > WEBHOOK_MAX_AGE_SECONDS:
        log.warning("Didit webhook rejected — timestamp too old (%ss).", age)
        return False, None

    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        log.warning("Didit webhook body is not valid JSON.")
        return False, None

    secret = DIDIT_WEBHOOK_SECRET.encode()

    # ── Method 1: X-Signature-V2 (canonical JSON, preferred) ────────────────────
    sig_v2 = headers.get("X-Signature-V2") or headers.get("x-signature-v2")
    if sig_v2:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        expected = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, sig_v2):
            return True, payload

    # ── Method 2: X-Signature (raw body) ─────────────────────────────────────────
    sig_raw = headers.get("X-Signature") or headers.get("x-signature")
    if sig_raw:
        expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, sig_raw):
            return True, payload

    # ── Method 3: X-Signature-Simple (coarse fallback) ───────────────────────────
    sig_simple = headers.get("X-Signature-Simple") or headers.get("x-signature-simple")
    if sig_simple:
        session_id = payload.get("session_id", "")
        status = payload.get("status", "")
        webhook_type = payload.get("webhook_type", "")
        signature_data = f"{timestamp}:{session_id}:{status}:{webhook_type}"
        expected = hmac.new(secret, signature_data.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, sig_simple):
            return True, payload

    log.warning("Didit webhook signature verification failed (all methods).")
    return False, None


# ── Status mapping ───────────────────────────────────────────────────────────────

def map_didit_status(didit_status: str) -> str:
    """
    Map a Didit session status onto our profiles.id_verification_status enum
    (not_submitted | pending | approved | rejected).
    """
    if didit_status == "Approved":
        return "approved"
    if didit_status in ("Declined", "Abandoned", "Expired"):
        return "rejected"
    # In Review, Not Finished, Resubmitted, or anything unrecognised — still pending
    return "pending"


def is_terminal_status(didit_status: str) -> bool:
    """True if this status represents a finished decision (no further webhook expected)."""
    return didit_status in _TERMINAL_STATUSES
