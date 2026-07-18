"""
routes/admin.py
───────────────
Admin-only platform management endpoints.

Endpoints
─────────
GET  /api/admin/users                  — list all users with roles
GET  /api/admin/users/<id>             — single user detail
GET  /api/admin/id-verification        — pending ID verification submissions
POST /api/admin/id-verification/<uid>/approve  — approve ID + optionally grant role
POST /api/admin/id-verification/<uid>/reject   — reject ID submission
GET  /api/admin/audit-log              — paginated audit trail
GET  /api/admin/stats                  — full platform statistics
GET  /api/admin/duplicate-flags        — cases flagged as possible duplicates
POST /api/admin/cases/merge            — merge two duplicate cases
"""

import logging
from datetime import datetime, timezone

from aiohttp import web

from backend.config import supabase_admin, Role
from backend.middleware.auth import require_role
from backend.services.email import send_email
from backend.services.storage import get_signed_url, BUCKET_ID_DOCS

log = logging.getLogger(__name__)
router = web.RouteTableDef()


# ── GET /api/admin/users ──────────────────────────────────────────────────────

@router.get("/api/admin/users")
@require_role(Role.ADMIN, log_action="list_users")
async def list_users(request: web.Request) -> web.Response:
    q      = request.rel_url.query
    page   = max(1, int(q.get("page", 1)))
    limit  = min(int(q.get("limit", 50)), 200)
    offset = (page - 1) * limit
    role   = q.get("role", "").strip()
    search = q.get("q", "").strip()

    try:
        query = supabase_admin.table("profiles").select(
            "id, full_name, email, phone, county, role, id_verified, "
            "id_verification_status, police_badge_number, police_station, "
            "preferred_language, created_at",
            count="exact",
        )

        if role and role in Role.ALL:
            query = query.eq("role", role)
        if search:
            query = query.or_(f"full_name.ilike.%{search}%,email.ilike.%{search}%")

        result = query.order("created_at", desc=True).range(
            offset, offset + limit - 1
        ).execute()

        return web.json_response({
            "users": result.data or [],
            "total": result.count or 0,
            "page":  page,
            "limit": limit,
        })

    except Exception as exc:
        log.exception("list_users error: %s", exc)
        return web.json_response({"error": "Failed to fetch users."}, status=500)


# ── GET /api/admin/users/<id> ─────────────────────────────────────────────────

@router.get("/api/admin/users/{user_id}")
@require_role(Role.ADMIN, log_action="view_user")
async def get_user(request: web.Request) -> web.Response:
    user_id = request.match_info["user_id"]
    try:
        profile = supabase_admin.table("profiles").select("*").eq(
            "id", user_id
        ).maybe_single().execute()

        if not profile.data:
            return web.json_response({"error": "User not found."}, status=404)

        # Count their submitted cases and tips
        cases_r = supabase_admin.table("cases").select(
            "id", count="exact"
        ).eq("reporter_id", user_id).execute()

        tips_r = supabase_admin.table("tips").select(
            "id", count="exact"
        ).eq("tipster_id", user_id).execute()

        return web.json_response({
            **profile.data,
            "cases_submitted": cases_r.count or 0,
            "tips_submitted":  tips_r.count or 0,
        })

    except Exception as exc:
        log.exception("get_user error: %s", exc)
        return web.json_response({"error": "Failed to fetch user."}, status=500)


# ── GET /api/admin/id-verification ───────────────────────────────────────────

@router.get("/api/admin/id-verification")
@require_role(Role.ADMIN, log_action="list_id_verifications")
async def list_id_verifications(request: web.Request) -> web.Response:
    """Return all users with pending ID verification submissions."""
    try:
        result = supabase_admin.table("profiles").select(
            "id, full_name, email, phone, role, id_verification_status, "
            "id_document_path, created_at",
        ).eq("id_verification_status", "pending").order(
            "created_at", desc=False   # oldest first — FIFO queue
        ).execute()

        submissions = result.data or []

        # Generate 1-hour signed URLs for each document so admin can view them
        for s in submissions:
            if s.get("id_document_path"):
                s["document_url"] = get_signed_url(
                    BUCKET_ID_DOCS, s["id_document_path"], expires_in=3600
                )

        return web.json_response({
            "pending": submissions,
            "count":   len(submissions),
        })

    except Exception as exc:
        log.exception("list_id_verifications error: %s", exc)
        return web.json_response({"error": "Failed to fetch verification queue."}, status=500)


# ── POST /api/admin/id-verification/<uid>/approve ────────────────────────────

@router.post("/api/admin/id-verification/{user_id}/approve")
@require_role(Role.ADMIN, log_action="approve_id_verification")
async def approve_id(request: web.Request) -> web.Response:
    target_id = request.match_info["user_id"]
    try:
        data = await request.json()
    except Exception:
        data = {}

    # Optional: promote to police officer at the same time
    new_role          = data.get("role", Role.USER)
    badge_number      = data.get("badge_number", "").strip()
    police_station    = data.get("police_station", "").strip()
    county            = data.get("county", "").strip()

    if new_role not in Role.ALL:
        return web.json_response({"error": f"Invalid role: {new_role}."}, status=422)

    try:
        profile_resp = supabase_admin.table("profiles").select(
            "email, full_name"
        ).eq("id", target_id).maybe_single().execute()

        profile = profile_resp.data
        if not profile:
            return web.json_response({"error": "User not found."}, status=404)

        # Update app_metadata (authoritative role source)
        app_meta = {"role": new_role, "id_verified": True}
        if new_role == Role.POLICE:
            app_meta["police_verified"] = True
        supabase_admin.auth.admin.update_user_by_id(
            target_id, {"app_metadata": app_meta}
        )

        # Update profile
        profile_update = {
            "id_verified":           True,
            "id_verification_status":"approved",
            "role":                  new_role,
        }
        if new_role == Role.POLICE and badge_number:
            profile_update["police_badge_number"] = badge_number
            profile_update["police_station"]      = police_station
            profile_update["county"]              = county

        supabase_admin.table("profiles").update(profile_update).eq(
            "id", target_id
        ).execute()

        # Notify user
        portal_note = ""
        if new_role == Role.POLICE:
            portal_note = f"\nBadge: {badge_number}\nStation: {police_station}, {county}\nYou can now access the Police Portal at https://nipate.go.ke/police"

        await send_email(
            to=[profile["email"]],
            subject="Your Nipate identity has been verified",
            body=(
                f"Hello {profile['full_name']},\n\n"
                f"Your identity has been verified on the Nipate platform.\n"
                f"Your account role: {new_role.replace('_', ' ').title()}\n"
                f"{portal_note}\n\n"
                f"You can now log in at https://nipate.go.ke"
            ),
        )

        log.info("Admin %s approved ID for user %s (role=%s)", request["user_id"], target_id, new_role)
        return web.json_response({
            "message": f"Identity approved. User role set to '{new_role}'.",
            "user_id": target_id,
        })

    except Exception as exc:
        log.exception("approve_id error: %s", exc)
        return web.json_response({"error": "Approval failed. Please try again."}, status=500)


# ── POST /api/admin/id-verification/<uid>/reject ─────────────────────────────

@router.post("/api/admin/id-verification/{user_id}/reject")
@require_role(Role.ADMIN, log_action="reject_id_verification")
async def reject_id(request: web.Request) -> web.Response:
    target_id = request.match_info["user_id"]
    try:
        data = await request.json()
    except Exception:
        data = {}

    reason = data.get("reason", "The document provided could not be verified.")

    try:
        profile_resp = supabase_admin.table("profiles").select(
            "email, full_name"
        ).eq("id", target_id).maybe_single().execute()
        profile = profile_resp.data
        if not profile:
            return web.json_response({"error": "User not found."}, status=404)

        supabase_admin.table("profiles").update({
            "id_verification_status": "rejected",
            "id_document_path":       None,
        }).eq("id", target_id).execute()

        await send_email(
            to=[profile["email"]],
            subject="Action required — Nipate identity verification",
            body=(
                f"Hello {profile['full_name']},\n\n"
                f"Unfortunately your identity verification submission could not be approved.\n\n"
                f"Reason: {reason}\n\n"
                f"Please log in and resubmit a valid national ID, passport, or driving licence at "
                f"https://nipate.go.ke/settings/verify-id\n\n"
                f"If you have questions, contact support@nipate.go.ke"
            ),
        )

        log.info("Admin %s rejected ID for user %s", request["user_id"], target_id)
        return web.json_response({"message": "Verification rejected. User has been notified."})

    except Exception as exc:
        log.exception("reject_id error: %s", exc)
        return web.json_response({"error": "Rejection failed."}, status=500)


# ── GET /api/admin/audit-log ──────────────────────────────────────────────────

@router.get("/api/admin/audit-log")
@require_role(Role.ADMIN, log_action="view_audit_log")
async def audit_log(request: web.Request) -> web.Response:
    q        = request.rel_url.query
    page     = max(1, int(q.get("page", 1)))
    limit    = min(int(q.get("limit", 50)), 200)
    offset   = (page - 1) * limit
    user_id  = q.get("user_id", "").strip()
    action   = q.get("action", "").strip()

    try:
        query = supabase_admin.table("audit_log").select(
            "id, user_id, user_email, role, action, http_method, "
            "path, status_code, ip_address, created_at",
            count="exact",
        )
        if user_id:
            query = query.eq("user_id", user_id)
        if action:
            query = query.eq("action", action)

        result = query.order("created_at", desc=True).range(
            offset, offset + limit - 1
        ).execute()

        return web.json_response({
            "logs":  result.data or [],
            "total": result.count or 0,
            "page":  page,
            "limit": limit,
        })

    except Exception as exc:
        log.exception("audit_log error: %s", exc)
        return web.json_response({"error": "Failed to fetch audit log."}, status=500)


# ── GET /api/admin/stats ──────────────────────────────────────────────────────

@router.get("/api/admin/stats")
@require_role(Role.ADMIN)
async def admin_stats(request: web.Request) -> web.Response:
    try:
        def count(table, **filters):
            q = supabase_admin.table(table).select("id", count="exact")
            for k, v in filters.items():
                q = q.eq(k, v)
            return (q.execute().count or 0)

        return web.json_response({
            "users": {
                "total":           count("profiles"),
                "registered_user": count("profiles", role="registered_user"),
                "police_officer":  count("profiles", role="police_officer"),
                "admin":           count("profiles", role="admin"),
                "id_verified":     count("profiles", id_verified=True),
                "pending_id":      count("profiles", id_verification_status="pending"),
            },
            "cases": {
                "total":                count("cases"),
                "reported":             count("cases", status="reported"),
                "under_investigation":  count("cases", status="under_investigation"),
                "found_safe":           count("cases", status="found_safe"),
                "found_deceased":       count("cases", status="found_deceased"),
                "closed":               count("cases", status="closed"),
                "unverified":           count("cases", is_verified=False),
                "alerts_sent":          count("cases", alert_sent=True),
            },
            "tips": {
                "total":                count("tips"),
                "received":             count("tips", status="received"),
                "under_investigation":  count("tips", status="under_investigation"),
                "resolved":             count("tips", status="resolved"),
                "anonymous":            count("tips", is_anonymous=True),
            },
            "alerts": {
                "subscribers": count("alert_subscribers", is_active=True),
            },
        })

    except Exception as exc:
        log.exception("admin_stats error: %s", exc)
        return web.json_response({"error": "Failed to fetch stats."}, status=500)


# ── GET /api/admin/duplicate-flags ───────────────────────────────────────────

@router.get("/api/admin/duplicate-flags")
@require_role(Role.POLICE, log_action="view_duplicate_flags")
async def duplicate_flags(request: web.Request) -> web.Response:
    """Return cases flagged as possible duplicates pending review."""
    try:
        result = supabase_admin.table("cases").select(
            "id, case_number, full_name, date_of_birth, last_seen_county, "
            "status, created_at, possible_duplicate_of, "
            "duplicate_of:possible_duplicate_of(case_number, full_name, status)"
        ).not_.is_("possible_duplicate_of", "null").eq(
            "is_verified", False
        ).order("created_at", desc=True).execute()

        return web.json_response({"flagged": result.data or []})

    except Exception as exc:
        log.exception("duplicate_flags error: %s", exc)
        return web.json_response({"error": "Failed to fetch duplicate flags."}, status=500)


# ── POST /api/admin/cases/merge ───────────────────────────────────────────────

@router.post("/api/admin/cases/merge")
@require_role(Role.ADMIN, log_action="merge_cases")
async def merge_cases(request: web.Request) -> web.Response:
    """
    Merge a duplicate case into a primary case.
    Moves tips and images from the duplicate to the primary, then closes the duplicate.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    primary_id   = data.get("primary_case_id")
    duplicate_id = data.get("duplicate_case_id")

    if not primary_id or not duplicate_id or primary_id == duplicate_id:
        return web.json_response(
            {"error": "primary_case_id and duplicate_case_id (different) are required."},
            status=400,
        )

    try:
        # Re-assign tips
        supabase_admin.table("tips").update(
            {"case_id": primary_id}
        ).eq("case_id", duplicate_id).execute()

        # Re-assign images
        supabase_admin.table("case_images").update(
            {"case_id": primary_id}
        ).eq("case_id", duplicate_id).execute()

        # Close the duplicate
        supabase_admin.table("cases").update({
            "status":    "closed",
            "is_public": False,
        }).eq("id", duplicate_id).execute()

        log.info(
            "Admin %s merged case %s → %s",
            request["user_id"], duplicate_id, primary_id,
        )
        return web.json_response({
            "message": "Cases merged successfully.",
            "primary_case_id":   primary_id,
            "duplicate_case_id": duplicate_id,
        })

    except Exception as exc:
        log.exception("merge_cases error: %s", exc)
        return web.json_response({"error": "Merge failed."}, status=500)
