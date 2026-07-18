"""
routes/cases.py
───────────────
All missing person case endpoints.

Endpoints
─────────
GET    /api/cases               — list/search cases (public sees verified only)
GET    /api/cases/stats         — platform-wide counts (public)
GET    /api/cases/<id>          — single case detail
POST   /api/cases               — submit new report (registered_user+)
PUT    /api/cases/<id>          — update own case (reporter) or any (police+)
PATCH  /api/cases/<id>/status   — update status (police+)
PATCH  /api/cases/<id>/verify   — verify & publish case (police+)
DELETE /api/cases/<id>          — soft-delete (admin only)
GET    /api/cases/<id>/notes    — investigation notes (police+)
POST   /api/cases/<id>/notes    — add investigation note (police+)
POST   /api/cases/dedup-check   — check for duplicates before submitting
"""

import logging
import re
from datetime import date, datetime

import bleach
from aiohttp import web

from backend.config import supabase_admin, Role
from backend.middleware.auth import require_role, public_route
from backend.services.dedup import find_duplicates
from backend.services.storage import upload_case_photo
from backend.services.sms import notify_case_status_change, notify_case_submitted

log = logging.getLogger(__name__)
router = web.RouteTableDef()

ALLOWED_STATUSES  = ["reported", "under_investigation", "found_safe", "found_deceased", "closed"]
ALLOWED_RISK      = ["standard", "high", "urgent"]
ALLOWED_CATEGORIES = ["child", "adult", "elderly", "vulnerable_adult", "foreign_national"]
ALLOWED_GENDERS   = ["female", "male", "other", "unknown"]
PAGE_SIZE         = 12


def _sanitise(text: str | None, max_len: int = 2000) -> str:
    """Strip HTML tags and limit length."""
    if not text:
        return ""
    return bleach.clean(str(text).strip(), tags=[], strip=True)[:max_len]


def _validate_case(data: dict) -> list[str]:
    errors = []
    if not data.get("full_name", "").strip():
        errors.append("Full name is required.")
    if not data.get("date_of_birth"):
        errors.append("Date of birth is required.")
    else:
        try:
            dob = date.fromisoformat(data["date_of_birth"])
            if dob > date.today():
                errors.append("Date of birth cannot be in the future.")
        except ValueError:
            errors.append("Date of birth must be in YYYY-MM-DD format.")
    if not data.get("physical_description", "").strip():
        errors.append("Physical description is required.")
    if not data.get("last_seen_date"):
        errors.append("Last seen date is required.")
    if not data.get("last_seen_location", "").strip():
        errors.append("Last seen location is required.")
    if not data.get("last_seen_county", "").strip():
        errors.append("Last seen county is required.")
    if data.get("gender") and data["gender"] not in ALLOWED_GENDERS:
        errors.append(f"Gender must be one of: {', '.join(ALLOWED_GENDERS)}.")
    if data.get("category") and data["category"] not in ALLOWED_CATEGORIES:
        errors.append(f"Category must be one of: {', '.join(ALLOWED_CATEGORIES)}.")
    return errors


# ── GET /api/cases ─────────────────────────────────────────────────────────────

@router.get("/api/cases")
@public_route
async def list_cases(request: web.Request) -> web.Response:
    q           = request.rel_url.query
    page        = max(1, int(q.get("page", 1)))
    limit       = min(int(q.get("limit", PAGE_SIZE)), 50)
    offset      = (page - 1) * limit
    search      = q.get("q", "").strip()
    county      = q.get("county", "").strip()
    category    = q.get("category", "").strip()
    status      = q.get("status", "").strip()
    risk_level  = q.get("risk_level", "").strip()
    is_public   = q.get("is_public", "").strip()

    role = request.get("role", Role.PUBLIC)
    is_police_or_above = Role.at_least(Role.POLICE, role)

    try:
        query = supabase_admin.table("cases").select(
            "id, case_number, full_name, alias, date_of_birth, age, gender, "
            "category, status, risk_level, last_seen_date, last_seen_location, "
            "last_seen_county, last_seen_lat, last_seen_lng, physical_description, "
            "is_verified, is_public, alert_sent, reporter_id, assigned_officer_id, "
            "created_at, updated_at, days_missing:last_seen_date",
            count="exact",
        )

        # Public viewers see all public cases (no verification gate)
        if not is_police_or_above:
            query = query.eq("is_public", True)
        else:
            # Police can optionally filter by is_public
            if is_public:
                query = query.eq("is_public", is_public == "true")

        if search:
            query = query.ilike("full_name", f"%{search}%")
        if county:
            query = query.ilike("last_seen_county", f"%{county}%")
        if category and category in ALLOWED_CATEGORIES:
            query = query.eq("category", category)
        if status and status in ALLOWED_STATUSES:
            query = query.eq("status", status)
        if risk_level and risk_level in ALLOWED_RISK:
            query = query.eq("risk_level", risk_level)

        result = query.order("risk_level", desc=True).order(
            "created_at", desc=True
        ).range(offset, offset + limit - 1).execute()

        cases = result.data or []

        # Compute days_missing client-side (Supabase doesn't support computed columns in select easily)
        today = date.today()
        for c in cases:
            try:
                lsd = date.fromisoformat(c["last_seen_date"])
                c["days_missing"] = (today - lsd).days
            except Exception:
                c["days_missing"] = None

        return web.json_response({
            "cases": cases,
            "total": result.count or 0,
            "page": page,
            "limit": limit,
        })

    except Exception as exc:
        log.exception("list_cases error: %s", exc)
        return web.json_response({"error": "Failed to fetch cases."}, status=500)


# ── GET /api/cases/stats ───────────────────────────────────────────────────────

@router.get("/api/cases/stats")
@public_route
async def case_stats(request: web.Request) -> web.Response:
    try:
        counts = {}
        for status in ["reported", "under_investigation", "found_safe", "found_deceased", "closed"]:
            r = supabase_admin.table("cases").select("id", count="exact").eq(
                "status", status
            ).eq("is_public", True).execute()
            counts[status] = r.count or 0

        tips_r = supabase_admin.table("tips").select("id", count="exact").execute()
        subs_r = supabase_admin.table("alert_subscribers").select("id", count="exact").eq(
            "is_active", True
        ).execute()

        active = counts["reported"] + counts["under_investigation"]

        return web.json_response({
            "active":          active,
            "reported":        counts["reported"],
            "investigating":   counts["under_investigation"],
            "found_safe":      counts["found_safe"],
            "found_deceased":  counts["found_deceased"],
            "closed":          counts["closed"],
            "tips":            tips_r.count or 0,
            "subscribers":     subs_r.count or 0,
        })
    except Exception as exc:
        log.exception("case_stats error: %s", exc)
        return web.json_response({"error": "Failed to fetch stats."}, status=500)


# ── GET /api/cases/<id> ────────────────────────────────────────────────────────

@router.get("/api/cases/{case_id}")
@public_route
async def get_case(request: web.Request) -> web.Response:
    case_id = request.match_info["case_id"]
    role    = request.get("role", Role.PUBLIC)
    is_police = Role.at_least(Role.POLICE, role)
    user_id   = request.get("user_id")

    try:
        result = supabase_admin.table("cases").select(
            "*, case_images(id, storage_url, is_primary)"
        ).eq("id", case_id).maybe_single().execute()

        case = result.data
        if not case:
            return web.json_response({"error": "Case not found."}, status=404)

        # Access control
        is_reporter = user_id and case.get("reporter_id") == user_id
        if not case.get("is_public") and not is_police and not is_reporter:
            return web.json_response({"error": "Case not found."}, status=404)

        # Compute days missing
        try:
            lsd = date.fromisoformat(case["last_seen_date"])
            case["days_missing"] = (date.today() - lsd).days
        except Exception:
            case["days_missing"] = None

        # Strip police-only fields for public
        if not is_police:
            for field in ["reporter_id", "assigned_officer_id", "dedup_hash", "possible_duplicate_of"]:
                case.pop(field, None)

        return web.json_response(case)

    except Exception as exc:
        log.exception("get_case error: %s", exc)
        return web.json_response({"error": "Failed to fetch case."}, status=500)


# ── POST /api/cases ────────────────────────────────────────────────────────────

@router.post("/api/cases")
@require_role(Role.USER, log_action="submit_case")
async def create_case(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    errors = _validate_case(data)
    if errors:
        return web.json_response({"errors": errors}, status=422)

    user_id = request["user_id"]

    # Dedup check
    dupes = await find_duplicates(
        full_name=data["full_name"],
        date_of_birth=data["date_of_birth"],
        last_seen_county=data["last_seen_county"],
    )
    dupe_ids = [d.case_id for d in dupes]

    try:
        payload = {
            "full_name":            _sanitise(data["full_name"], 120),
            "alias":                _sanitise(data.get("alias"), 80),
            "date_of_birth":        data["date_of_birth"],
            "gender":               data.get("gender", "unknown"),
            "nationality":          _sanitise(data.get("nationality", "Kenyan"), 60),
            "national_id_number":   _sanitise(data.get("national_id_number"), 30),
            "physical_description": _sanitise(data.get("physical_description"), 2000),
            "height_cm":            data.get("height_cm"),
            "weight_kg":            data.get("weight_kg"),
            "complexion":           _sanitise(data.get("complexion"), 60),
            "hair_description":     _sanitise(data.get("hair_description"), 120),
            "distinguishing_marks": _sanitise(data.get("distinguishing_marks"), 500),
            "clothing_description": _sanitise(data.get("clothing_description"), 500),
            "last_seen_date":       data["last_seen_date"],
            "last_seen_time":       data.get("last_seen_time"),
            "last_seen_location":   _sanitise(data["last_seen_location"], 200),
            "last_seen_county":     _sanitise(data["last_seen_county"], 60),
            "last_seen_lat":        data.get("last_seen_lat"),
            "last_seen_lng":        data.get("last_seen_lng"),
            "circumstances":        _sanitise(data.get("circumstances"), 2000),
            "category":             data.get("category", "adult"),
            "status":               "reported",
            "risk_level":           "urgent" if data.get("category") in ("child", "elderly") else "standard",
            "reporter_id":          user_id,
            "reporter_name":        _sanitise(data.get("reporter_name"), 120),
            "reporter_phone":       _sanitise(data.get("reporter_phone"), 20),
            "reporter_relationship":_sanitise(data.get("reporter_relationship"), 60),
            "is_verified":          True,   # cases go public immediately
            "is_public":            True,   # tips/notes remain restricted to family & police
            "possible_duplicate_of": dupe_ids[0] if dupe_ids else None,
        }

        result = supabase_admin.table("cases").insert(payload).execute()
        case   = result.data[0]
        case_id = case["id"]

        log.info("New case submitted: %s by user %s (dupes=%s)", case_id, user_id, dupe_ids)

        # Confirm submission by SMS — fire-and-forget, never blocks the response
        sms_result = await notify_case_submitted(case)
        if sms_result["failed"]:
            log.warning("Confirmation SMS failed for case %s: %s", case_id, sms_result["failed"])

        return web.json_response({
            "message":    "Report submitted and published successfully. Investigators can now see it.",
            "case_id":    case_id,
            "case_number": case.get("case_number"),
            "possible_duplicates": [{"case_id": d.case_id, "case_number": d.case_number,
                                     "full_name": d.full_name, "score": d.score} for d in dupes],
        }, status=201)

    except Exception as exc:
        log.exception("create_case error: %s", exc)
        return web.json_response({"error": "Failed to submit report. Please try again."}, status=500)


# ── PUT /api/cases/<id> ────────────────────────────────────────────────────────

@router.put("/api/cases/{case_id}")
@require_role(Role.USER, log_action="update_case")
async def update_case(request: web.Request) -> web.Response:
    case_id = request.match_info["case_id"]
    user_id = request["user_id"]
    role    = request["role"]

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    # Verify ownership (unless police+)
    if not Role.at_least(Role.POLICE, role):
        existing = supabase_admin.table("cases").select("reporter_id").eq(
            "id", case_id
        ).maybe_single().execute()
        if not existing.data or existing.data["reporter_id"] != user_id:
            return web.json_response({"error": "You can only edit your own reports."}, status=403)

    # Reporters cannot change status/verification fields
    allowed_fields = {
        "alias", "physical_description", "height_cm", "weight_kg",
        "complexion", "hair_description", "distinguishing_marks",
        "clothing_description", "circumstances", "last_seen_time",
        "last_seen_location", "last_seen_lat", "last_seen_lng",
        "reporter_phone", "reporter_name", "reporter_relationship",
    }
    if Role.at_least(Role.POLICE, role):
        allowed_fields |= {"risk_level", "category", "assigned_officer_id", "assigned_county"}

    payload = {k: _sanitise(v) if isinstance(v, str) else v
               for k, v in data.items() if k in allowed_fields}

    if not payload:
        return web.json_response({"error": "No valid fields to update."}, status=400)

    try:
        supabase_admin.table("cases").update(payload).eq("id", case_id).execute()
        return web.json_response({"message": "Case updated successfully."})
    except Exception as exc:
        log.exception("update_case error: %s", exc)
        return web.json_response({"error": "Update failed."}, status=500)


# ── PATCH /api/cases/<id>/status ──────────────────────────────────────────────

@router.patch("/api/cases/{case_id}/status")
@require_role(Role.POLICE, log_action="update_case_status")
async def update_status(request: web.Request) -> web.Response:
    case_id = request.match_info["case_id"]
    try:
        data   = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    new_status = data.get("status")
    if new_status not in ALLOWED_STATUSES:
        return web.json_response(
            {"error": f"Status must be one of: {', '.join(ALLOWED_STATUSES)}."},
            status=422,
        )

    update: dict = {"status": new_status}
    if new_status in ("found_safe", "found_deceased"):
        update["found_at"]     = datetime.utcnow().isoformat()
        update["found_county"] = _sanitise(data.get("found_county"), 60)

    try:
        result = supabase_admin.table("cases").update(update).eq("id", case_id).execute()
        log.info("Case %s status → %s by officer %s", case_id, new_status, request["user_id"])

        # Notify the reporter by SMS — fire-and-forget, never blocks or fails the request
        updated_case = (result.data or [None])[0]
        if updated_case:
            sms_result = await notify_case_status_change(updated_case, new_status)
            if sms_result["failed"]:
                log.warning("SMS notification failed for case %s: %s", case_id, sms_result["failed"])

        return web.json_response({"message": f"Status updated to '{new_status}'."})
    except Exception as exc:
        log.exception("update_status error: %s", exc)
        return web.json_response({"error": "Status update failed."}, status=500)


# ── PATCH /api/cases/<id>/verify ──────────────────────────────────────────────

@router.patch("/api/cases/{case_id}/verify")
@require_role(Role.POLICE, log_action="verify_case")
async def verify_case(request: web.Request) -> web.Response:
    """Verify a case and publish it publicly. Optionally set risk level."""
    case_id = request.match_info["case_id"]
    try:
        data = await request.json()
    except Exception:
        data = {}

    risk_level = data.get("risk_level", "standard")
    if risk_level not in ALLOWED_RISK:
        risk_level = "standard"

    try:
        supabase_admin.table("cases").update({
            "is_verified":       True,
            "is_public":         True,
            "status":            "under_investigation",
            "risk_level":        risk_level,
            "assigned_officer_id": request["user_id"],
        }).eq("id", case_id).execute()

        log.info("Case %s verified by officer %s", case_id, request["user_id"])
        return web.json_response({
            "message": "Case verified and published. You can now send an alert.",
            "case_id": case_id,
        })
    except Exception as exc:
        log.exception("verify_case error: %s", exc)
        return web.json_response({"error": "Verification failed."}, status=500)


# ── GET /api/cases/<id>/notes ─────────────────────────────────────────────────

@router.get("/api/cases/{case_id}/notes")
@require_role(Role.POLICE)
async def get_notes(request: web.Request) -> web.Response:
    case_id = request.match_info["case_id"]
    try:
        result = supabase_admin.table("investigation_notes").select(
            "id, content, is_sensitive, created_at, updated_at, "
            "author:author_id(full_name, role)"
        ).eq("case_id", case_id).order("created_at", desc=True).execute()

        return web.json_response({"notes": result.data or []})
    except Exception as exc:
        log.exception("get_notes error: %s", exc)
        return web.json_response({"error": "Failed to fetch notes."}, status=500)


# ── POST /api/cases/<id>/notes ────────────────────────────────────────────────

@router.post("/api/cases/{case_id}/notes")
@require_role(Role.POLICE, log_action="add_investigation_note")
async def add_note(request: web.Request) -> web.Response:
    case_id = request.match_info["case_id"]
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    content = _sanitise(data.get("content"), 5000)
    if not content:
        return web.json_response({"error": "Note content is required."}, status=400)

    try:
        result = supabase_admin.table("investigation_notes").insert({
            "case_id":      case_id,
            "author_id":    request["user_id"],
            "content":      content,
            "is_sensitive": bool(data.get("is_sensitive", False)),
        }).execute()

        return web.json_response({
            "message": "Note added.",
            "note_id": result.data[0]["id"],
        }, status=201)
    except Exception as exc:
        log.exception("add_note error: %s", exc)
        return web.json_response({"error": "Failed to add note."}, status=500)


# ── POST /api/cases/dedup-check ───────────────────────────────────────────────

@router.post("/api/cases/dedup-check")
@require_role(Role.USER)
async def dedup_check(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    name   = data.get("full_name", "")
    dob    = data.get("date_of_birth", "")
    county = data.get("last_seen_county", "")

    if not all([name, dob, county]):
        return web.json_response(
            {"error": "full_name, date_of_birth, and last_seen_county are required."},
            status=400,
        )

    dupes = await find_duplicates(name, dob, county)
    return web.json_response({
        "possible_duplicates": [
            {"case_id": d.case_id, "case_number": d.case_number,
             "full_name": d.full_name, "score": d.score}
            for d in dupes
        ],
        "has_duplicates": len(dupes) > 0,
    })


# ── DELETE /api/cases/<id> ────────────────────────────────────────────────────

@router.delete("/api/cases/{case_id}")
@require_role(Role.ADMIN, log_action="delete_case")
async def delete_case(request: web.Request) -> web.Response:
    case_id = request.match_info["case_id"]
    try:
        # Soft delete: set status to closed and unpublish
        supabase_admin.table("cases").update({
            "is_public":  False,
            "status":     "closed",
        }).eq("id", case_id).execute()

        log.warning("Case %s soft-deleted by admin %s", case_id, request["user_id"])
        return web.json_response({"message": "Case removed from public database."})
    except Exception as exc:
        log.exception("delete_case error: %s", exc)
        return web.json_response({"error": "Delete failed."}, status=500)
