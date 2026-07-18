"""
routes/webhooks.py
───────────────────
Inbound webhooks from external providers. Currently just Didit (identity
verification); more can be added here as the system grows (e.g. an SMS
delivery-report webhook from SMSGate).

Endpoints
─────────
POST /api/webhooks/didit — receives session status/decision updates from Didit

Security note
─────────────
These endpoints are NOT protected by our normal JWT/RBAC middleware — the
caller is Didit's servers, not a logged-in user. Authenticity is instead
verified via HMAC signature (see services/didit.verify_webhook). Do not add
@require_role to anything in this file; do the trust check inside the
handler instead.
"""

import logging

from aiohttp import web

from backend.config import supabase_admin
from backend.middleware.auth import public_route
from backend.services.didit import verify_webhook, map_didit_status, is_terminal_status
from backend.services.email import send_email

log = logging.getLogger(__name__)
router = web.RouteTableDef()


@router.post("/api/webhooks/didit")
@public_route
async def didit_webhook(request: web.Request) -> web.Response:
    """
    Receive a session status update from Didit.

    Flow:
      1. Read the raw request body (signature is computed over exact bytes —
         must not parse-then-reserialise before verifying).
      2. Verify the HMAC signature and timestamp freshness.
      3. Look up the profile via `vendor_data` (our user_id, set when the
         session was created in routes/auth.py:start_didit_verification).
      4. Map Didit's status onto our id_verification_status enum and persist.
      5. On a terminal status, email the user with the outcome.

    Always returns 200 for successfully-processed (even if declined)
    verifications so Didit doesn't retry; returns 401 only for signature
    failures, and 200-with-log for anything else unexpected — Didit retries
    on 5xx/404, and retry storms don't help us recover from "profile not
    found" or similar data issues, so we swallow those after logging.
    """
    raw_body = await request.read()

    is_valid, payload = verify_webhook(raw_body, dict(request.headers))
    if not is_valid or payload is None:
        return web.json_response({"error": "Invalid webhook signature."}, status=401)

    webhook_type = payload.get("webhook_type", "")
    status = payload.get("status", "")
    session_id = payload.get("session_id", "")
    user_id = payload.get("vendor_data", "")  # set to our user_id at session creation

    log.info(
        "Didit webhook received: type=%s status=%s session=%s vendor_data=%s",
        webhook_type, status, session_id, user_id,
    )

    if not user_id:
        # Nothing we can map this back to — log and acknowledge so Didit
        # doesn't keep retrying an event we can never resolve.
        log.warning("Didit webhook has no vendor_data — cannot map to a user. session=%s", session_id)
        return web.json_response({"message": "Acknowledged (no vendor_data to map)."})

    our_status = map_didit_status(status)

    try:
        update = {"id_verification_status": our_status}
        if our_status == "approved":
            update["id_verified"] = True
        supabase_admin.table("profiles").update(update).eq("id", user_id).execute()
    except Exception as exc:
        log.error("Failed to update profile %s from Didit webhook: %s", user_id, exc)
        # Acknowledge anyway — a DB write failure on our side shouldn't cause
        # Didit to retry indefinitely; this will show up in our own logs/alerts.
        return web.json_response({"message": "Acknowledged (internal update failed)."})

    # Notify the user once the decision is final (skip "In Review" etc — more
    # webhooks are still coming for those).
    if is_terminal_status(status):
        await _notify_user_of_decision(user_id, our_status)

    return web.json_response({"message": "Processed.", "status": our_status})


async def _notify_user_of_decision(user_id: str, our_status: str) -> None:
    """Email the user once their Didit verification reaches a final outcome."""
    try:
        profile = supabase_admin.table("profiles").select(
            "email, full_name"
        ).eq("id", user_id).maybe_single().execute()
    except Exception as exc:
        log.error("Could not fetch profile %s to send verification-decision email: %s", user_id, exc)
        return

    if not profile.data or not profile.data.get("email"):
        return

    name = profile.data.get("full_name", "there")
    email = profile.data["email"]

    if our_status == "approved":
        subject = "Your Nipate identity has been verified"
        body = (
            f"Hello {name},\n\n"
            f"Your identity has been verified successfully. You can now submit missing "
            f"person reports on Nipate.\n\nLog in at https://nipate.go.ke"
        )
    else:
        subject = "Action required — Nipate identity verification"
        body = (
            f"Hello {name},\n\n"
            f"We were unable to verify your identity document. This can happen if the "
            f"document was unclear, expired, or didn't match the selfie taken.\n\n"
            f"Please try again at https://nipate.go.ke/settings/verify-id\n\n"
            f"If you continue to have trouble, contact support@nipate.go.ke"
        )

    await send_email(to=[email], subject=subject, body=body)
