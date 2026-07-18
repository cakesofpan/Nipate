"""
routes/tips.py
──────────────
Tip submission and management.

Endpoints
─────────
POST  /api/tips                    — submit a tip (anonymous OR identified)
GET   /api/tips                    — list tips for a case (police+)
GET   /api/tips/<id>               — single tip detail (police+ or own tip)
PATCH /api/tips/<id>/status        — update tip status (police+)
POST  /api/tips/<id>/attachments   — upload evidence file to a tip (anyone)
GET   /api/tips/track/<tip_number> — public tip status tracker (no auth, number only)
"""

import logging
import re
import uuid

import bleach
from aiohttp import web

from backend.config import supabase_admin, Role
from backend.middleware.auth import require_role, public_route
from backend.services.storage import upload_tip_evidence, get_signed_url, BUCKET_TIP_EVIDENCE
from backend.services.sms import notify_new_tip

log = logging.getLogger(__name__)
router = web.RouteTableDef()

ALLOWED_STATUSES  = ["received", "reviewed", "under_investigation", "resolved", "dismissed"]
ALLOWED_CATEGORIES = ["sighting", "location_info", "suspect_info", "other"]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[\d\s\-]{7,15}$")
MAX_CONTENT_LEN = 5000
ALLOWED_MIME = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
    "video/mp4": "mp4", "video/quicktime": "mov",
    "application/pdf": "pdf",
}


def _clean(text: str | None, max_len: int = MAX_CONTENT_LEN) -> str:
    if not text:
        return ""
    return bleach.clean(str(text).strip(), tags=[], strip=True)[:max_len]


# ── POST /api/tips ─────────────────────────────────────────────────────────────

@router.post("/api/tips")
@public_route
async def submit_tip(request: web.Request) -> web.Response:
    """
    Submit a tip — can be anonymous or identified.

    Anonymous tip:   { is_anonymous: true, case_id, content, category }
    Identified tip:  { is_anonymous: false, case_id, content, category,
                       tipster_email?, tipster_phone? }
                     OR sent with Authorization header (logged-in user)

    If is_anonymous is false and no contact info is provided and no session
    exists, the tip is still accepted but stored without contact details.
    Identified tips allow the tipster to track status via /api/tips/track/<tip_number>.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    case_id = data.get("case_id", "").strip()
    content = _clean(data.get("content"))
    category = data.get("category", "sighting")
    is_anonymous = bool(data.get("is_anonymous", True))

    # Validate required fields
    errors = []
    if not case_id:
        errors.append("case_id is required.")
    if not content or len(content) < 10:
        errors.append("Tip content must be at least 10 characters.")
    if category not in ALLOWED_CATEGORIES:
        errors.append(f"Category must be one of: {', '.join(ALLOWED_CATEGORIES)}.")
    if errors:
        return web.json_response({"errors": errors}, status=422)

    # Verify case exists and is public
    try:
        case_resp = supabase_admin.table("cases").select(
            "id, is_public, is_verified, full_name, case_number, reporter_phone"
        ).eq("id", case_id).maybe_single().execute()
    except Exception as exc:
        log.error("Case lookup failed: %s", exc)
        return web.json_response({"error": "Could not verify case. Please try again."}, status=500)

    if not case_resp.data:
        return web.json_response({"error": "Case not found."}, status=404)

    # Determine tipster info
    tipster_id    = None
    tipster_email = None
    tipster_phone = None

    if not is_anonymous:
        # Try to get from logged-in session first
        if request.get("user_id"):
            tipster_id = request["user_id"]
        else:
            # Use provided contact details
            email = (data.get("tipster_email") or "").strip().lower()
            phone = (data.get("tipster_phone") or "").strip()
            if email and EMAIL_RE.match(email):
                tipster_email = email
            if phone and PHONE_RE.match(phone):
                tipster_phone = phone
            # If neither provided, treat as anonymous silently
            if not tipster_id and not tipster_email and not tipster_phone:
                is_anonymous = True

    try:
        payload = {
            "case_id":      case_id,
            "category":     category,
            "content":      content,
            "is_anonymous": is_anonymous,
            "tipster_id":   tipster_id,
            "tipster_email": tipster_email,
            "tipster_phone": tipster_phone,
            "status":       "received",
        }

        result = supabase_admin.table("tips").insert(payload).execute()
        tip    = result.data[0]

        log.info(
            "Tip submitted: %s for case %s (anonymous=%s)",
            tip["id"], case_id, is_anonymous,
        )

        # Notify the case reporter (family) by SMS — fire-and-forget, never
        # blocks or fails the tip submission if SMS delivery has an issue
        sms_result = await notify_new_tip(case_resp.data, tip.get("tip_number"))
        if sms_result["failed"]:
            log.warning("SMS notification failed for case %s: %s", case_id, sms_result["failed"])

        response_body = {
            "message": "Your tip has been received. Thank you for helping.",
            "tip_id":  tip["id"],
        }

        # Only give the tip number back to non-anonymous tipsters so they can track
        if not is_anonymous:
            response_body["tip_number"] = tip.get("tip_number")
            response_body["tracking_note"] = (
                "Save your tip number to track the status of your tip."
            )

        return web.json_response(response_body, status=201)

    except Exception as exc:
        log.exception("submit_tip error: %s", exc)
        return web.json_response({"error": "Failed to submit tip. Please try again."}, status=500)


# ── GET /api/tips ──────────────────────────────────────────────────────────────

@router.get("/api/tips")
@require_role(Role.POLICE)
async def list_tips(request: web.Request) -> web.Response:
    """List all tips, optionally filtered by case or status. Police+ only."""
    q         = request.rel_url.query
    case_id   = q.get("case_id", "").strip()
    status    = q.get("status", "").strip()
    page      = max(1, int(q.get("page", 1)))
    limit     = min(int(q.get("limit", 20)), 100)
    offset    = (page - 1) * limit

    try:
        query = supabase_admin.table("tips").select(
            "id, tip_number, case_id, category, content, is_anonymous, "
            "tipster_email, tipster_phone, status, assigned_to, created_at, "
            "case:case_id(case_number, full_name)",
            count="exact",
        )
        if case_id:
            query = query.eq("case_id", case_id)
        if status and status in ALLOWED_STATUSES:
            query = query.eq("status", status)

        result = query.order("created_at", desc=True).range(
            offset, offset + limit - 1
        ).execute()

        tips = result.data or []

        # Redact tipster identity for non-admin police officers
        if not Role.at_least(Role.ADMIN, request["role"]):
            for tip in tips:
                if tip.get("is_anonymous"):
                    tip["tipster_email"] = None
                    tip["tipster_phone"] = None
                    tip["tipster_id"]    = None

        return web.json_response({
            "tips":  tips,
            "total": result.count or 0,
            "page":  page,
            "limit": limit,
        })

    except Exception as exc:
        log.exception("list_tips error: %s", exc)
        return web.json_response({"error": "Failed to fetch tips."}, status=500)


# ── GET /api/tips/<id> ────────────────────────────────────────────────────────

@router.get("/api/tips/{tip_id}")
@public_route
async def get_tip(request: web.Request) -> web.Response:
    tip_id  = request.match_info["tip_id"]
    user_id = request.get("user_id")
    role    = request.get("role", Role.PUBLIC)

    try:
        result = supabase_admin.table("tips").select(
            "*, tip_attachments(id, storage_path, file_type, created_at)"
        ).eq("id", tip_id).maybe_single().execute()

        tip = result.data
        if not tip:
            return web.json_response({"error": "Tip not found."}, status=404)

        is_police = Role.at_least(Role.POLICE, role)
        is_own    = user_id and tip.get("tipster_id") == user_id

        if not is_police and not is_own:
            return web.json_response({"error": "Not found."}, status=404)

        # Generate signed URLs for attachments (they're in private storage)
        if is_police:
            for att in tip.get("tip_attachments") or []:
                att["signed_url"] = get_signed_url(
                    BUCKET_TIP_EVIDENCE, att["storage_path"], expires_in=3600
                )

        # Scrub identity for non-admin police
        if is_police and not Role.at_least(Role.ADMIN, role) and tip.get("is_anonymous"):
            tip["tipster_email"] = None
            tip["tipster_phone"] = None

        return web.json_response(tip)

    except Exception as exc:
        log.exception("get_tip error: %s", exc)
        return web.json_response({"error": "Failed to fetch tip."}, status=500)


# ── PATCH /api/tips/<id>/status ───────────────────────────────────────────────

@router.patch("/api/tips/{tip_id}/status")
@require_role(Role.POLICE, log_action="update_tip_status")
async def update_tip_status(request: web.Request) -> web.Response:
    tip_id = request.match_info["tip_id"]
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    new_status  = data.get("status")
    assigned_to = data.get("assigned_to")

    if new_status not in ALLOWED_STATUSES:
        return web.json_response(
            {"error": f"Status must be one of: {', '.join(ALLOWED_STATUSES)}."},
            status=422,
        )

    update: dict = {"status": new_status}
    if new_status == "reviewed":
        from datetime import datetime
        update["reviewed_at"] = datetime.utcnow().isoformat()
    if assigned_to:
        update["assigned_to"] = assigned_to

    try:
        supabase_admin.table("tips").update(update).eq("id", tip_id).execute()
        return web.json_response({"message": f"Tip status updated to '{new_status}'."})
    except Exception as exc:
        log.exception("update_tip_status error: %s", exc)
        return web.json_response({"error": "Update failed."}, status=500)


# ── POST /api/tips/<id>/attachments ───────────────────────────────────────────

@router.post("/api/tips/{tip_id}/attachments")
@public_route
async def upload_attachment(request: web.Request) -> web.Response:
    """
    Upload a photo, video, or document as evidence for a tip.
    Anyone (including anonymous tipsters) can upload — no auth required.
    Max file size 50 MB.
    """
    tip_id = request.match_info["tip_id"]

    # Verify tip exists
    try:
        tip_resp = supabase_admin.table("tips").select("id").eq(
            "id", tip_id
        ).maybe_single().execute()
        if not tip_resp.data:
            return web.json_response({"error": "Tip not found."}, status=404)
    except Exception:
        return web.json_response({"error": "Could not verify tip."}, status=500)

    try:
        reader = await request.multipart()
        field  = await reader.next()
    except Exception:
        return web.json_response({"error": "Expected multipart/form-data."}, status=400)

    if not field or field.name != "evidence":
        return web.json_response(
            {"error": "Field 'evidence' (file) is required."},
            status=400,
        )

    content_type = field.headers.get("Content-Type", "")
    if content_type not in ALLOWED_MIME:
        return web.json_response(
            {"error": f"File type not allowed. Accepted: {', '.join(ALLOWED_MIME)}."},
            status=415,
        )

    file_bytes = await field.read(decode=True)
    max_size   = 50 * 1024 * 1024  # 50 MB
    if len(file_bytes) > max_size:
        return web.json_response({"error": "File must be under 50 MB."}, status=413)

    ext = ALLOWED_MIME[content_type]

    try:
        path = await upload_tip_evidence(
            tip_id=tip_id,
            file_bytes=file_bytes,
            extension=ext,
            content_type=content_type,
        )
        supabase_admin.table("tip_attachments").insert({
            "tip_id":       tip_id,
            "storage_path": path,
            "file_type":    content_type,
        }).execute()

        return web.json_response({
            "message": "Evidence uploaded successfully.",
            "path":    path,
        }, status=201)

    except Exception as exc:
        log.exception("upload_attachment error: %s", exc)
        return web.json_response({"error": "Upload failed. Please try again."}, status=500)


# ── GET /api/tips/track/<tip_number> ──────────────────────────────────────────

@router.get("/api/tips/track/{tip_number}")
@public_route
async def track_tip(request: web.Request) -> web.Response:
    """
    Allow non-anonymous tipsters to check the status of their tip
    using just the tip number (e.g. NP-TIP-20240518-003).
    Returns only the status — no sensitive investigation details.
    """
    tip_number = request.match_info["tip_number"].upper().strip()

    try:
        result = supabase_admin.table("tips").select(
            "tip_number, status, category, created_at, reviewed_at, case:case_id(case_number, full_name)"
        ).eq("tip_number", tip_number).eq(
            "is_anonymous", False  # anonymous tips cannot be tracked
        ).maybe_single().execute()

        tip = result.data
        if not tip:
            return web.json_response(
                {"error": "Tip not found. Anonymous tips cannot be tracked."},
                status=404,
            )

        # Map status to user-friendly description
        status_labels = {
            "received":           "Received — awaiting review",
            "reviewed":           "Reviewed — assigned to an officer",
            "under_investigation":"Under investigation — field team active",
            "resolved":           "Resolved",
            "dismissed":          "Reviewed — no further action required",
        }

        return web.json_response({
            "tip_number": tip["tip_number"],
            "status":     tip["status"],
            "status_label": status_labels.get(tip["status"], tip["status"]),
            "category":   tip["category"],
            "submitted_at": tip["created_at"],
            "reviewed_at":  tip.get("reviewed_at"),
            "case":       tip.get("case"),
        })

    except Exception as exc:
        log.exception("track_tip error: %s", exc)
        return web.json_response({"error": "Failed to fetch tip status."}, status=500)
