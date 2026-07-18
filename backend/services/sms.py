"""
services/sms.py
────────────────
SMS notifications via SMSGate (sms-gate.app / capcom6/android-sms-gateway) —
an open-source gateway that turns an Android phone into an SMS sender,
exposed over a simple REST API with HTTP Basic Auth.

Why SMSGate:
- No per-SMS cost beyond your own SIM's tariff — messages go out through a
  real Android device and SIM card you control
- Simple REST API — no heavy SDK required, keeping with the project's
  "no heavy frameworks" constraint (we call the HTTP API directly via aiohttp)
- Three deployment modes to fit different infrastructure needs (see below)

Deployment modes (set via SMSGATE_MODE)
────────────────────────────────────────
cloud    Default. Uses the maintainers' public relay at api.sms-gate.app.
         Endpoint: https://api.sms-gate.app/3rdparty/v1/messages
         Fastest to set up — just install the SMSGate app, enable Cloud
         Server, and copy the username/password it displays.

private  A self-hosted relay server (e.g. the capcom6/sms-gateway Docker
         image) running on your own infrastructure, mirroring the cloud API.
         Endpoint: {SMSGATE_BASE_URL}/api/3rdparty/v1/messages

local    Talk directly to the Android device's local HTTP server over the
         same Wi-Fi/LAN — no internet required, but only reachable from a
         backend running on the same network as the device.
         Endpoint: {SMSGATE_BASE_URL}/message

Setup
─────
1. Install "SMS Gateway for Android" (SMSGate) on the sending device.
2. Choose a mode in the app (Cloud Server is simplest) and note the
   username/password it generates for Basic Auth.
3. Set SMSGATE_MODE, SMSGATE_USERNAME, SMSGATE_PASSWORD (and SMSGATE_BASE_URL
   for private/local modes) in .env.

If SMSGATE_USERNAME/SMSGATE_PASSWORD are not configured, all functions in
this module silently no-op and log a warning — this lets the rest of the
system run in dev without SMS.
"""

import logging
import re

import aiohttp

from backend.config import (
    SMSGATE_MODE,
    SMSGATE_BASE_URL,
    SMSGATE_USERNAME,
    SMSGATE_PASSWORD,
    SMS_ENABLED,
)

log = logging.getLogger(__name__)

SMSGATE_CLOUD_URL = "https://api.sms-gate.app/3rdparty/v1/messages"

# SMSGate has no hard character cap of its own (it segments/concatenates SMS
# on the device side), but we keep templates short and cap defensively so a
# bug can't accidentally trigger a very long, multi-part, costly message.
MAX_SMS_LENGTH = 320


def _api_url() -> str:
    """Resolve the correct SMSGate endpoint for the configured mode."""
    if SMSGATE_MODE == "private":
        base = SMSGATE_BASE_URL.rstrip("/")
        return f"{base}/api/3rdparty/v1/messages"
    if SMSGATE_MODE == "local":
        base = SMSGATE_BASE_URL.rstrip("/")
        return f"{base}/message"
    return SMSGATE_CLOUD_URL  # "cloud" (default)


def _normalise_kenyan_number(phone: str) -> str | None:
    """
    Normalise a Kenyan phone number to E.164 format (+254XXXXXXXXX).
    Accepts: 0712345678, 712345678, +254712345678, 254712345678.
    Returns None if the number doesn't look like a valid Kenyan mobile number.
    """
    digits = re.sub(r"\D", "", phone or "")

    if digits.startswith("254") and len(digits) == 12:
        return f"+{digits}"
    if digits.startswith("0") and len(digits) == 10:
        return f"+254{digits[1:]}"
    if len(digits) == 9 and digits[0] in "17":
        return f"+254{digits}"

    return None


async def send_sms(to: list[str], message: str) -> dict:
    """
    Send an SMS to one or more Kenyan phone numbers via SMSGate.

    Args:
        to:      List of phone numbers in any common Kenyan format.
        message: SMS body. Truncated to MAX_SMS_LENGTH.

    Returns a dict: {"sent": int, "failed": list[str]}

    Note on "sent": SMSGate's POST /messages response confirms the message
    was accepted onto the gateway's send queue (state "Pending"), not that
    it has been delivered to the handset. Final delivery status is async —
    poll GET /messages/{id} or register a webhook if you need delivery
    confirmation. For this system's purposes (best-effort case-update
    notifications), queue acceptance is treated as success.

    Never raises — failures are logged and returned, not propagated, so a
    failed SMS never breaks the API request that triggered it.
    """
    if not SMS_ENABLED:
        log.warning(
            "SMS not configured (SMSGATE_USERNAME/SMSGATE_PASSWORD missing) — "
            "skipping send to %d recipient(s).", len(to),
        )
        return {"sent": 0, "failed": list(to)}

    # Normalise and drop invalid numbers up front
    valid_numbers: list[str] = []
    invalid_numbers: list[str] = []
    for raw in to:
        normalised = _normalise_kenyan_number(raw)
        if normalised:
            valid_numbers.append(normalised)
        else:
            invalid_numbers.append(raw)

    if invalid_numbers:
        log.warning("Skipping %d invalid phone number(s): %s", len(invalid_numbers), invalid_numbers)

    if not valid_numbers:
        return {"sent": 0, "failed": invalid_numbers}

    body = message[:MAX_SMS_LENGTH]

    payload = {
        "textMessage": {"text": body},
        "phoneNumbers": valid_numbers,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _api_url(),
                json=payload,
                auth=aiohttp.BasicAuth(SMSGATE_USERNAME, SMSGATE_PASSWORD),
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                response_body = await resp.text()

                if 200 <= resp.status < 300:
                    log.info(
                        "SMS queued via SMSGate (%s mode): %d recipient(s), status=%s",
                        SMSGATE_MODE, len(valid_numbers), resp.status,
                    )
                    return {"sent": len(valid_numbers), "failed": invalid_numbers}

                log.error(
                    "SMSGate send failed: HTTP %s — %s", resp.status, response_body[:300],
                )
                return {"sent": 0, "failed": valid_numbers + invalid_numbers}

    except Exception as exc:
        log.error("SMS send failed: %s", exc)
        return {"sent": 0, "failed": valid_numbers + invalid_numbers}


# ── Case-update message templates ───────────────────────────────────────────────

STATUS_LABELS = {
    "reported":            "Reported",
    "under_investigation": "Under investigation",
    "found_safe":          "Found safe",
    "found_deceased":      "Found deceased",
    "closed":              "Closed",
}


async def notify_case_status_change(case: dict, new_status: str) -> dict:
    """
    Send an SMS to the case reporter when a police officer updates case status.
    Uses cases.reporter_phone (collected at submission specifically for this purpose).
    """
    phone = case.get("reporter_phone")
    if not phone:
        log.info("No reporter phone on case %s — skipping SMS.", case.get("id"))
        return {"sent": 0, "failed": []}

    name        = case.get("full_name", "the case")
    case_number = case.get("case_number", "")
    label       = STATUS_LABELS.get(new_status, new_status)

    message = (
        f"Nipate update: The status of {name}'s case ({case_number}) "
        f"has changed to \"{label}\". View details: nipate.go.ke/case.html?id={case.get('id','')}"
    )
    return await send_sms([phone], message)


async def notify_case_submitted(case: dict) -> dict:
    """
    Send an SMS to the reporter immediately after a case is successfully
    submitted and published, confirming the case number for their records.

    This is the first touchpoint — many reporters may have low connectivity
    or may not stay on the confirmation page, so a case number they can
    reference later (e.g. when calling police) is valuable even before any
    status update occurs.
    """
    phone = case.get("reporter_phone")
    if not phone:
        log.info("No reporter phone on new case %s — skipping confirmation SMS.", case.get("id"))
        return {"sent": 0, "failed": []}

    name        = case.get("full_name", "Your report")
    case_number = case.get("case_number", "")

    message = (
        f"Nipate: Your report for {name} has been received and published. "
        f"Case number: {case_number}. Save this number for reference. "
        f"View: nipate.go.ke/case.html?id={case.get('id','')}"
    )
    return await send_sms([phone], message)


async def notify_new_tip(case: dict, tip_number: str | None = None) -> dict:
    """
    Notify the case reporter (family) that a new tip has been received.
    Does NOT reveal tip content — tips remain private to police/family via the portal.
    """
    phone = case.get("reporter_phone")
    if not phone:
        return {"sent": 0, "failed": []}

    name        = case.get("full_name", "your case")
    case_number = case.get("case_number", "")

    message = (
        f"Nipate: A new tip has been received for {name}'s case ({case_number}). "
        f"Log in to view details: nipate.go.ke/case.html?id={case.get('id','')}"
    )
    return await send_sms([phone], message)


async def notify_alert_broadcast(case: dict, recipient_phones: list[str]) -> dict:
    """
    Optional: SMS an urgent alert to opted-in subscribers in addition to email.
    Used sparingly (SMS costs money) — typically only for risk_level='urgent' cases.
    """
    name        = case.get("full_name", "Unknown")
    county      = case.get("last_seen_county", "")
    case_number = case.get("case_number", "")

    message = (
        f"NIPATE ALERT: {name} missing in {county} County. "
        f"Case {case_number}. If you have info, visit nipate.go.ke/tip.html"
    )
    return await send_sms(recipient_phones, message)
