"""
routes/alerts.py
────────────────
Alert subscription and broadcast endpoints.

Endpoints
─────────
POST   /api/alerts/subscribe      — subscribe email to county alerts (public)
GET    /api/alerts/unsubscribe    — one-click unsubscribe via signed token (public)
POST   /api/alerts/send           — trigger alert broadcast for a verified case (police+)
GET    /api/alerts/subscribers    — list subscribers (admin only)
"""

import logging
import re

from aiohttp import web

from backend.config import supabase_admin, Role
from backend.middleware.auth import require_role, public_route
from backend.services.email import broadcast_alert, verify_unsubscribe_token

log = logging.getLogger(__name__)
router = web.RouteTableDef()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
VALID_COUNTIES = {
    "all", "nairobi", "mombasa", "kisumu", "nakuru", "eldoret",
    "thika", "machakos", "kakamega", "kilifi", "meru", "nyeri",
    "garissa", "malindi", "kitale", "lodwar", "isiolo", "wajir",
}


# ── Subscribe ──────────────────────────────────────────────────────────────────

@router.post("/api/alerts/subscribe")
@public_route
async def subscribe(request: web.Request) -> web.Response:
    """Subscribe an email address to missing person alerts for selected counties."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    email = (data.get("email") or "").strip().lower()
    counties = data.get("counties", ["all"])

    if not email or not EMAIL_RE.match(email):
        return web.json_response({"error": "A valid email address is required."}, status=422)

    # Sanitise counties list
    counties = [c.lower() for c in counties if c.lower() in VALID_COUNTIES]
    if not counties:
        counties = ["all"]

    try:
        # Upsert — if already subscribed, update their county preferences
        supabase_admin.table("alert_subscribers").upsert(
            {
                "email": email,
                "counties": counties,
                "is_active": True,
            },
            on_conflict="email",
        ).execute()

        log.info("Alert subscription: %s → counties=%s", email, counties)
        return web.json_response({
            "message": "You are now subscribed. You will receive alerts for your selected counties.",
            "email": email,
            "counties": counties,
        }, status=201)

    except Exception as exc:
        log.exception("Subscribe error: %s", exc)
        return web.json_response({"error": "Subscription failed. Please try again."}, status=500)


# ── Unsubscribe (one-click, no login required) ─────────────────────────────────

@router.get("/api/alerts/unsubscribe")
@public_route
async def unsubscribe(request: web.Request) -> web.Response:
    """
    One-click unsubscribe via HMAC-signed token (included in every alert email).
    Returns a plain HTML confirmation page so it works directly from email clients.
    """
    token = request.rel_url.query.get("token", "")
    email = verify_unsubscribe_token(token)

    if not email:
        return web.Response(
            text="<html><body><h2>Invalid or expired unsubscribe link.</h2>"
                 "<p>Please contact support@nipate.go.ke</p></body></html>",
            content_type="text/html",
            status=400,
        )

    try:
        supabase_admin.table("alert_subscribers").update(
            {"is_active": False}
        ).eq("email", email).execute()

        log.info("Unsubscribed: %s", email)
        return web.Response(
            text=f"""
            <html><head><title>Unsubscribed — Nipate</title>
            <style>body{{font-family:sans-serif;max-width:500px;margin:60px auto;color:#1a2235}}
            h2{{color:#0B1E3D}} p{{color:#6b7a92}} a{{color:#2980B9}}</style></head>
            <body>
              <h2>✓ Unsubscribed successfully</h2>
              <p>The email address <strong>{email}</strong> has been removed from Nipate alerts.</p>
              <p>You can re-subscribe at any time at <a href="https://nipate.go.ke">nipate.go.ke</a>.</p>
            </body></html>
            """,
            content_type="text/html",
        )
    except Exception as exc:
        log.exception("Unsubscribe error: %s", exc)
        return web.Response(
            text="<html><body><h2>Something went wrong. Please try again later.</h2></body></html>",
            content_type="text/html",
            status=500,
        )


# ── Send alert (police / admin only) ──────────────────────────────────────────

@router.post("/api/alerts/send")
@require_role(Role.POLICE, log_action="send_case_alert")
async def send_alert(request: web.Request) -> web.Response:
    """
    Trigger an alert broadcast for a verified case.
    Called by police officers after verifying a report.
    Sends emails to all active subscribers in the case's county (+ 'all' subscribers).
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    case_id = data.get("case_id")
    if not case_id:
        return web.json_response({"error": "case_id is required."}, status=400)

    try:
        # Fetch the case
        case_resp = supabase_admin.table("cases").select("*").eq("id", case_id).maybe_single().execute()
        case = case_resp.data

        if not case:
            return web.json_response({"error": "Case not found."}, status=404)
        if not case.get("is_verified"):
            return web.json_response(
                {"error": "Only verified cases can trigger alerts."},
                status=422,
            )
        if case.get("alert_sent"):
            return web.json_response(
                {"error": "An alert has already been sent for this case. Use a case update alert instead."},
                status=409,
            )

        # Fetch subscribers for this county + 'all'
        county = case.get("last_seen_county", "").lower()
        subs_resp = supabase_admin.table("alert_subscribers").select("email").eq(
            "is_active", True
        ).execute()

        all_emails = [
            row["email"] for row in (subs_resp.data or [])
            if "all" in row.get("counties", []) or county in row.get("counties", [])
        ]

        if not all_emails:
            return web.json_response({
                "message": "No active subscribers found for this county. Alert recorded.",
                "sent": 0,
            })

        # Broadcast
        sent = await broadcast_alert(case=case, subscriber_emails=all_emails)

        # Mark alert as sent
        supabase_admin.table("cases").update({
            "alert_sent": True,
            "alert_sent_at": "now()",
        }).eq("id", case_id).execute()

        log.info(
            "Alert sent for case %s (%s) by officer %s → %d emails",
            case_id, case.get("case_number"), request["user_id"], sent,
        )

        return web.json_response({
            "message": f"Alert sent to {sent} subscriber(s).",
            "case_id": case_id,
            "case_number": case.get("case_number"),
            "sent": sent,
        })

    except Exception as exc:
        log.exception("Send alert error: %s", exc)
        return web.json_response({"error": "Alert failed to send. Please try again."}, status=500)


# ── List subscribers (admin only) ─────────────────────────────────────────────

@router.get("/api/alerts/subscribers")
@require_role(Role.ADMIN, log_action="list_subscribers")
async def list_subscribers(request: web.Request) -> web.Response:
    """Return paginated list of alert subscribers. Admin only."""
    page = int(request.rel_url.query.get("page", 1))
    per_page = min(int(request.rel_url.query.get("per_page", 50)), 200)
    offset = (page - 1) * per_page

    try:
        resp = supabase_admin.table("alert_subscribers").select(
            "id, email, counties, is_active, created_at",
            count="exact",
        ).order("created_at", desc=True).range(offset, offset + per_page - 1).execute()

        return web.json_response({
            "subscribers": resp.data or [],
            "total": resp.count or 0,
            "page": page,
            "per_page": per_page,
        })
    except Exception as exc:
        log.exception("List subscribers error: %s", exc)
        return web.json_response({"error": "Failed to fetch subscribers."}, status=500)
