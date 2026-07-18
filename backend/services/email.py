"""
services/email.py
─────────────────
Email sending via Gmail SMTP (aiosmtplib for async).

Covers
──────
- General transactional emails (verification, password reset, notifications)
- Alert broadcasts to case subscribers (batched, rate-safe)
- Unsubscribe token generation and verification
"""

import asyncio
import logging
import hmac
import hashlib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Sequence

import aiosmtplib

from backend.config import SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, APP_SECRET_KEY

log = logging.getLogger(__name__)

# Send at most this many emails per SMTP connection to avoid timeouts
BATCH_SIZE = 50


async def send_email(
    to: Sequence[str],
    subject: str,
    body: str,
    html_body: str | None = None,
    reply_to: str | None = None,
) -> bool:
    """
    Send an email to one or more recipients.

    Args:
        to:        List of recipient addresses.
        subject:   Email subject line.
        body:      Plain-text body (always included for accessibility).
        html_body: Optional HTML body. Sent as multipart/alternative if provided.
        reply_to:  Optional Reply-To address.

    Returns True on success, False on failure (logs the error).
    """
    msg = MIMEMultipart("alternative") if html_body else MIMEText(body, "plain", "utf-8")

    msg["Subject"] = subject
    msg["From"] = f"Nipate Missing Persons <{SMTP_USER}>"
    msg["To"] = ", ".join(to) if len(to) <= 5 else SMTP_USER  # BCC for large lists
    if reply_to:
        msg["Reply-To"] = reply_to

    if html_body:
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        await aiosmtplib.send(
            msg,
            hostname=SMTP_HOST,
            port=SMTP_PORT,
            username=SMTP_USER,
            password=SMTP_PASSWORD,
            start_tls=True,
            recipients=list(to),
        )
        log.info("Email sent to %d recipient(s): %s", len(to), subject)
        return True
    except Exception as exc:
        log.error("Email send failed (%s): %s", subject, exc)
        return False


async def broadcast_alert(
    case: dict,
    subscriber_emails: list[str],
) -> int:
    """
    Send an alert email for a verified missing person case to all subscribers.
    Emails are sent in batches to avoid overloading the SMTP server.

    Returns the number of emails successfully sent.
    """
    case_id = case.get("id", "")
    name = case.get("full_name", "Unknown")
    county = case.get("last_seen_county", "")
    status = case.get("status", "Reported")
    url = f"https://nipate.go.ke/case/{case_id}"

    subject = f"ALERT: Missing person — {name} · {county} County"

    sent_count = 0
    for i in range(0, len(subscriber_emails), BATCH_SIZE):
        batch = subscriber_emails[i: i + BATCH_SIZE]
        emails_with_tokens = []

        for email in batch:
            unsub_token = _make_unsubscribe_token(email)
            unsub_url = f"https://nipate.go.ke/api/alerts/unsubscribe?token={unsub_token}"

            html = _render_alert_html(
                case=case,
                name=name,
                county=county,
                status=status,
                case_url=url,
                unsubscribe_url=unsub_url,
            )
            plain = _render_alert_plain(name=name, county=county, status=status, case_url=url, unsub_url=unsub_url)

            # Send individually so unsubscribe tokens are unique per recipient
            success = await send_email(
                to=[email],
                subject=subject,
                body=plain,
                html_body=html,
            )
            if success:
                sent_count += 1

        # Brief pause between batches — respectful of Gmail rate limits
        if i + BATCH_SIZE < len(subscriber_emails):
            await asyncio.sleep(1)

    log.info("Alert broadcast for case %s: %d/%d sent", case_id, sent_count, len(subscriber_emails))
    return sent_count


# ── Unsubscribe token helpers ──────────────────────────────────────────────────

def _make_unsubscribe_token(email: str) -> str:
    sig = hmac.new(APP_SECRET_KEY.encode(), email.lower().encode(), hashlib.sha256).hexdigest()[:24]
    import base64
    encoded_email = base64.urlsafe_b64encode(email.encode()).decode()
    return f"{encoded_email}.{sig}"


def verify_unsubscribe_token(token: str) -> str | None:
    """Return the email if the token is valid, else None."""
    import base64
    try:
        encoded_email, sig = token.rsplit(".", 1)
        email = base64.urlsafe_b64decode(encoded_email.encode()).decode()
        expected = hmac.new(APP_SECRET_KEY.encode(), email.lower().encode(), hashlib.sha256).hexdigest()[:24]
        if hmac.compare_digest(expected, sig):
            return email
    except Exception:
        pass
    return None


# ── Email templates ────────────────────────────────────────────────────────────

def _render_alert_html(
    case: dict, name: str, county: str, status: str, case_url: str, unsubscribe_url: str
) -> str:
    age = case.get("age", "Unknown")
    gender = case.get("gender", "Unknown")
    last_seen_date = case.get("last_seen_date", "")
    description = case.get("physical_description", "")

    return f"""
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f6f9;font-family:'Helvetica Neue',Arial,sans-serif">
  <div style="max-width:560px;margin:32px auto;background:#fff;border-radius:12px;overflow:hidden;border:1px solid #e2e8f0">

    <!-- Header -->
    <div style="background:#0B1E3D;padding:20px 24px;display:flex;align-items:center;gap:12px">
      <div style="width:10px;height:10px;background:#E74C3C;border-radius:50%"></div>
      <span style="color:#fff;font-size:18px;font-weight:600;letter-spacing:.3px">Nipate</span>
    </div>

    <!-- Alert badge -->
    <div style="background:#FEF2F2;padding:12px 24px;border-bottom:1px solid #FECACA">
      <span style="color:#C0392B;font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.8px">
        ⚠ Missing person alert
      </span>
    </div>

    <!-- Body -->
    <div style="padding:28px 24px">
      <h1 style="margin:0 0 4px;font-size:22px;color:#1a2235">{name}</h1>
      <p style="margin:0 0 20px;color:#6b7a92;font-size:14px">{gender} · Age {age} · Last seen in {county} County</p>

      <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:24px">
        <tr>
          <td style="padding:8px 0;color:#6b7a92;border-bottom:1px solid #f0f0f0">Status</td>
          <td style="padding:8px 0;font-weight:500;border-bottom:1px solid #f0f0f0">{status}</td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#6b7a92;border-bottom:1px solid #f0f0f0">Last seen</td>
          <td style="padding:8px 0;font-weight:500;border-bottom:1px solid #f0f0f0">{last_seen_date}</td>
        </tr>
        <tr>
          <td style="padding:8px 0;color:#6b7a92">Description</td>
          <td style="padding:8px 0">{description}</td>
        </tr>
      </table>

      <p style="margin:0 0 20px;font-size:14px;color:#1a2235">
        If you have seen this person or have any information, please submit a tip through the Nipate platform or contact your nearest police station.
      </p>

      <a href="{case_url}" style="display:inline-block;background:#0B1E3D;color:#fff;text-decoration:none;padding:12px 28px;border-radius:8px;font-size:14px;font-weight:500">
        View full case →
      </a>
    </div>

    <!-- Footer -->
    <div style="background:#f4f6f9;padding:16px 24px;border-top:1px solid #e2e8f0">
      <p style="margin:0;font-size:12px;color:#6b7a92">
        You received this alert because you subscribed to Nipate alerts for {county} County.<br>
        <a href="{unsubscribe_url}" style="color:#2980B9">Unsubscribe from alerts</a>
      </p>
    </div>
  </div>
</body>
</html>
"""


def _render_alert_plain(name: str, county: str, status: str, case_url: str, unsub_url: str) -> str:
    return f"""NIPATE MISSING PERSON ALERT
{'─' * 40}

MISSING: {name}
County: {county}
Status: {status}

If you have seen this person or have information, please visit:
{case_url}

Or submit an anonymous tip at: https://nipate.go.ke/tip

─────────────────────────────────────
You received this alert because you subscribed to Nipate alerts.
To unsubscribe: {unsub_url}
"""
