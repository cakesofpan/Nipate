"""
routes/auth.py
──────────────
All authentication and identity endpoints.

Endpoints
─────────
POST /api/auth/signup          — create account (registered_user role)
POST /api/auth/login           — email+password login, returns JWT
POST /api/auth/logout          — invalidate session
POST /api/auth/refresh         — exchange refresh token for new access token
GET  /api/auth/me              — return current user profile + role
POST /api/auth/verify-id       — submit ID document for manual review (fallback flow)
POST /api/auth/verify-id/start — start automated Didit identity verification (primary flow)
POST /api/auth/verify-police   — admin: grant police_officer role
PATCH /api/auth/role           — admin: change any user's role
POST /api/auth/request-reset   — send password-reset email
POST /api/auth/reset-password  — complete password reset (token from email)
"""

import logging
import re
from pathlib import Path

from aiohttp import web

from backend.config import supabase, supabase_admin, Role, SUPABASE_URL
from backend.middleware.auth import require_role, public_route
from backend.services.storage import upload_id_document
from backend.services.email import send_email
from backend.services.didit import create_verification_session, DiditError

log = logging.getLogger(__name__)

router = web.RouteTableDef()

# ── Validation helpers ─────────────────────────────────────────────────────────

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[\d\s\-]{7,15}$")


def _validate_signup(data: dict) -> list[str]:
    errors = []
    if not data.get("email") or not EMAIL_RE.match(data["email"]):
        errors.append("A valid email address is required.")
    pw = data.get("password", "")
    if len(pw) < 8:
        errors.append("Password must be at least 8 characters.")
    if not any(c.isdigit() for c in pw):
        errors.append("Password must contain at least one number.")
    if not any(c.isupper() for c in pw):
        errors.append("Password must contain at least one uppercase letter.")
    if not data.get("full_name", "").strip():
        errors.append("Full name is required.")
    if not data.get("phone") or not PHONE_RE.match(data["phone"]):
        errors.append("A valid phone number is required.")
    if not data.get("county"):
        errors.append("County is required.")
    return errors


# ── Signup ─────────────────────────────────────────────────────────────────────

@router.post("/api/auth/signup")
@public_route
async def signup(request: web.Request) -> web.Response:
    """
    Create a new account.
    New users always start as `registered_user`. Police accounts must go
    through the /api/auth/verify-police flow after signup.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    errors = _validate_signup(data)
    if errors:
        return web.json_response({"errors": errors}, status=422)

    try:
        # Create the auth user via Supabase Auth
        auth_resp = supabase.auth.sign_up({
            "email": data["email"].strip().lower(),
            "password": data["password"],
            "options": {
                "data": {
                    # user_metadata is editable by the user — not used for RBAC
                    "full_name": data["full_name"].strip(),
                    "phone": data["phone"].strip(),
                    "county": data["county"],
                    "preferred_language": data.get("language", "en"),
                }
            },
        })

        if not auth_resp.user:
            return web.json_response(
                {"error": "Could not create account. Email may already be registered."},
                status=409,
            )

        user_id = auth_resp.user.id

        # Set role in app_metadata (only service_role key can write this — user cannot forge it)
        supabase_admin.auth.admin.update_user_by_id(
            user_id,
            {"app_metadata": {"role": Role.USER, "id_verified": False}},
        )

        # Create profile row (triggers RLS — user can only see own row)
        supabase_admin.table("profiles").insert({
            "id": user_id,
            "full_name": data["full_name"].strip(),
            "email": data["email"].strip().lower(),
            "phone": data["phone"].strip(),
            "county": data["county"],
            "role": Role.USER,
            "id_verified": False,
            "preferred_language": data.get("language", "en"),
        }).execute()

        log.info("New user signed up: %s (%s)", user_id, data["email"])

        return web.json_response(
            {
                "message": "Account created. Please check your email to confirm your address.",
                "user_id": user_id,
            },
            status=201,
        )

    except Exception as exc:
        log.exception("Signup error: %s", exc)
        return web.json_response(
            {"error": "An unexpected error occurred. Please try again."},
            status=500,
        )


# ── Login ──────────────────────────────────────────────────────────────────────

@router.post("/api/auth/login")
@public_route
async def login(request: web.Request) -> web.Response:
    """
    Authenticate with email + password.
    Returns access_token (JWT), refresh_token, user profile, and role.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return web.json_response(
            {"error": "Email and password are required."},
            status=400,
        )

    try:
        auth_resp = supabase.auth.sign_in_with_password({
            "email": email,
            "password": password,
        })

        if not auth_resp.session or not auth_resp.user:
            return web.json_response(
                {"error": "Invalid email or password."},
                status=401,
            )

        session = auth_resp.session
        user = auth_resp.user

        # Role lives in app_metadata (server-controlled)
        role = (user.app_metadata or {}).get("role", Role.PUBLIC)
        id_verified = (user.app_metadata or {}).get("id_verified", False)

        # Fetch profile for display data
        profile_resp = supabase_admin.table("profiles").select(
            "full_name, county, phone, preferred_language, police_badge_number, police_station"
        ).eq("id", user.id).maybe_single().execute()

        profile = profile_resp.data or {}

        return web.json_response({
            "access_token": session.access_token,
            "refresh_token": session.refresh_token,
            "expires_in": session.expires_in,
            "user": {
                "id": user.id,
                "email": user.email,
                "role": role,
                "id_verified": id_verified,
                "full_name": profile.get("full_name"),
                "county": profile.get("county"),
                "preferred_language": profile.get("preferred_language", "en"),
                "police_badge_number": profile.get("police_badge_number"),
                "police_station": profile.get("police_station"),
            },
        })

    except Exception as exc:
        msg = str(exc).lower()
        if "invalid login" in msg or "email not confirmed" in msg:
            return web.json_response(
                {"error": "Invalid email or password. If you just signed up, please confirm your email first."},
                status=401,
            )
        log.exception("Login error: %s", exc)
        return web.json_response({"error": "Login failed. Please try again."}, status=500)


# ── Logout ─────────────────────────────────────────────────────────────────────

@router.post("/api/auth/logout")
@require_role(Role.USER)
async def logout(request: web.Request) -> web.Response:
    """Invalidate the current session server-side."""
    try:
        supabase.auth.sign_out()
        return web.json_response({"message": "Logged out successfully."})
    except Exception as exc:
        log.warning("Logout error (non-critical): %s", exc)
        return web.json_response({"message": "Logged out."})


# ── Token refresh ──────────────────────────────────────────────────────────────

@router.post("/api/auth/refresh")
@public_route
async def refresh_token(request: web.Request) -> web.Response:
    """Exchange a refresh token for a new access token."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    refresh = data.get("refresh_token")
    if not refresh:
        return web.json_response({"error": "refresh_token is required."}, status=400)

    try:
        session_resp = supabase.auth.refresh_session(refresh)
        session = session_resp.session
        return web.json_response({
            "access_token": session.access_token,
            "refresh_token": session.refresh_token,
            "expires_in": session.expires_in,
        })
    except Exception as exc:
        log.warning("Token refresh failed: %s", exc)
        return web.json_response(
            {"error": "Session expired. Please log in again."},
            status=401,
        )


# ── Current user ───────────────────────────────────────────────────────────────

@router.get("/api/auth/me")
@require_role(Role.USER)
async def get_me(request: web.Request) -> web.Response:
    """Return the authenticated user's profile and role."""
    user_id = request["user_id"]
    try:
        profile_resp = supabase_admin.table("profiles").select("*").eq("id", user_id).maybe_single().execute()
        profile = profile_resp.data or {}

        return web.json_response({
            "id": user_id,
            "email": request["user_email"],
            "role": request["role"],
            **profile,
        })
    except Exception as exc:
        log.exception("get_me error: %s", exc)
        return web.json_response({"error": "Could not fetch profile."}, status=500)


# ── ID document upload (identity verification) ─────────────────────────────────

@router.post("/api/auth/verify-id")
@require_role(Role.USER, log_action="upload_id_document")
async def submit_id_verification(request: web.Request) -> web.Response:
    """
    Upload a national ID, passport, or driving licence for identity verification.
    The document is stored in a PRIVATE Supabase Storage bucket.
    An admin reviews it and calls /api/auth/verify-police or patches the profile.
    """
    user_id = request["user_id"]

    reader = await request.multipart()
    field = await reader.next()

    if not field or field.name != "id_document":
        return web.json_response(
            {"error": "Field 'id_document' (file) is required."},
            status=400,
        )

    content_type = field.headers.get("Content-Type", "")
    allowed_types = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
    if content_type not in allowed_types:
        return web.json_response(
            {"error": "Only JPEG, PNG, WebP, or PDF files are accepted for ID verification."},
            status=415,
        )

    file_bytes = await field.read(decode=True)
    max_size = 10 * 1024 * 1024  # 10 MB
    if len(file_bytes) > max_size:
        return web.json_response({"error": "File must be under 10 MB."}, status=413)

    # Determine file extension
    ext_map = {
        "image/jpeg": "jpg", "image/png": "png",
        "image/webp": "webp", "application/pdf": "pdf"
    }
    ext = ext_map[content_type]

    try:
        storage_path = await upload_id_document(
            user_id=user_id,
            file_bytes=file_bytes,
            extension=ext,
            content_type=content_type,
        )

        # Record submission in profile (status = pending)
        supabase_admin.table("profiles").update({
            "id_verification_status": "pending",
            "id_document_path": storage_path,
        }).eq("id", user_id).execute()

        # Notify admins
        await send_email(
            to=["admin@nipate.go.ke"],
            subject="New ID verification submission",
            body=f"User {user_id} ({request['user_email']}) has submitted an ID document for verification.\n\nDocument path: {storage_path}\n\nReview at https://nipate.go.ke/admin/id-verification",
        )

        return web.json_response({
            "message": "ID document received. Verification typically takes 1–2 business days.",
            "status": "pending",
        })

    except Exception as exc:
        log.exception("ID upload error: %s", exc)
        return web.json_response({"error": "Upload failed. Please try again."}, status=500)


# ── ID verification via Didit (primary, automated flow) ────────────────────────

@router.post("/api/auth/verify-id/start")
@require_role(Role.USER, log_action="start_didit_verification")
async def start_didit_verification(request: web.Request) -> web.Response:
    """
    Start a hosted Didit identity verification session for the current user.

    This is the primary ID verification path: the frontend calls this, then
    redirects the user's browser to the returned `verification_url`, where
    Didit guides them through ID capture + liveness + face match. When they
    finish, Didit calls POST /api/webhooks/didit, which updates the user's
    profile automatically (see routes/webhooks.py).

    The manual upload flow (POST /api/auth/verify-id) remains available as a
    fallback for cases where the hosted flow isn't suitable.
    """
    user_id = request["user_id"]

    try:
        data = await request.json()
    except Exception:
        data = {}

    # Where Didit sends the user's *browser* after they finish — separate
    # from the webhook, which is configured once, server-side, in Didit's console.
    callback_url = data.get("callback_url") or "https://nipate.go.ke/settings/verify-id/complete"

    try:
        session = await create_verification_session(user_id=user_id, callback_url=callback_url)
    except DiditError as exc:
        log.error("Didit session start failed for user %s: %s", user_id, exc)
        return web.json_response(
            {"error": "Identity verification is temporarily unavailable. Please try again shortly."},
            status=503,
        )

    try:
        supabase_admin.table("profiles").update({
            "id_verification_status": "pending",
            "id_verification_session_id": session["session_id"],
        }).eq("id", user_id).execute()
    except Exception as exc:
        # Session was created successfully on Didit's side even if this write
        # fails — log it, but still hand the user their verification_url.
        log.error("Failed to record Didit session on profile %s: %s", user_id, exc)

    return web.json_response({
        "verification_url": session["verification_url"],
        "session_id": session["session_id"],
    })


# ── Police account verification (admin only) ───────────────────────────────────

@router.post("/api/auth/verify-police")
@require_role(Role.ADMIN, log_action="verify_police_account")
async def verify_police_account(request: web.Request) -> web.Response:
    """
    Admin: verify a law enforcement account.
    Grants `police_officer` role and records badge number + station.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    target_user_id = data.get("user_id")
    badge_number = data.get("badge_number", "").strip()
    police_station = data.get("police_station", "").strip()
    county = data.get("county", "").strip()

    if not all([target_user_id, badge_number, police_station, county]):
        return web.json_response(
            {"error": "user_id, badge_number, police_station, and county are all required."},
            status=400,
        )

    try:
        # Update Supabase Auth app_metadata (server-side, user cannot alter)
        supabase_admin.auth.admin.update_user_by_id(
            target_user_id,
            {
                "app_metadata": {
                    "role": Role.POLICE,
                    "id_verified": True,
                    "police_verified": True,
                }
            },
        )

        # Update profiles table
        supabase_admin.table("profiles").update({
            "role": Role.POLICE,
            "id_verified": True,
            "id_verification_status": "approved",
            "police_badge_number": badge_number,
            "police_station": police_station,
            "county": county,
        }).eq("id", target_user_id).execute()

        # Notify the officer
        user_info = supabase_admin.auth.admin.get_user_by_id(target_user_id)
        if user_info.user:
            await send_email(
                to=[user_info.user.email],
                subject="Your Nipate law enforcement account has been verified",
                body=(
                    f"Your account has been verified as a law enforcement officer.\n\n"
                    f"Badge: {badge_number}\n"
                    f"Station: {police_station}, {county}\n\n"
                    f"You can now log in to the Police Portal at https://nipate.go.ke/police"
                ),
            )

        log.info(
            "Admin %s verified police account %s (badge %s, %s)",
            request["user_id"], target_user_id, badge_number, police_station,
        )

        return web.json_response({
            "message": "Law enforcement account verified successfully.",
            "user_id": target_user_id,
            "role": Role.POLICE,
        })

    except Exception as exc:
        log.exception("Police verification error: %s", exc)
        return web.json_response({"error": "Verification failed. Please try again."}, status=500)


# ── Role update (admin only) ───────────────────────────────────────────────────

@router.patch("/api/auth/role")
@require_role(Role.ADMIN, log_action="update_user_role")
async def update_role(request: web.Request) -> web.Response:
    """Admin: set any user's role. Validates against allowed role values."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    target_user_id = data.get("user_id")
    new_role = data.get("role")

    if not target_user_id or new_role not in Role.ALL:
        return web.json_response(
            {"error": f"user_id and a valid role ({', '.join(Role.ALL)}) are required."},
            status=400,
        )

    # Prevent demotion of other admins (safety guard)
    existing = supabase_admin.auth.admin.get_user_by_id(target_user_id)
    existing_role = (existing.user.app_metadata or {}).get("role") if existing.user else None
    if existing_role == Role.ADMIN and new_role != Role.ADMIN:
        return web.json_response(
            {"error": "Cannot demote another admin via this endpoint. Use the Supabase dashboard."},
            status=403,
        )

    try:
        supabase_admin.auth.admin.update_user_by_id(
            target_user_id,
            {"app_metadata": {"role": new_role}},
        )
        supabase_admin.table("profiles").update({"role": new_role}).eq("id", target_user_id).execute()

        return web.json_response({
            "message": f"Role updated to {new_role}.",
            "user_id": target_user_id,
            "role": new_role,
        })
    except Exception as exc:
        log.exception("Role update error: %s", exc)
        return web.json_response({"error": "Role update failed."}, status=500)


# ── Password reset ─────────────────────────────────────────────────────────────

@router.post("/api/auth/request-reset")
@public_route
async def request_password_reset(request: web.Request) -> web.Response:
    """Trigger a Supabase password-reset email."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    email = (data.get("email") or "").strip().lower()
    if not email or not EMAIL_RE.match(email):
        return web.json_response({"error": "A valid email is required."}, status=400)

    try:
        supabase.auth.reset_password_email(
            email,
            options={"redirect_to": "https://nipate.go.ke/reset.html"},
        )
    except Exception:
        pass  # Don't reveal whether the email exists

    # Always return 200 to prevent email enumeration
    return web.json_response({
        "message": "If an account with that email exists, a reset link has been sent."
    })


@router.post("/api/auth/reset-password")
@public_route
async def reset_password(request: web.Request) -> web.Response:
    """Complete password reset using the token from the reset email."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body."}, status=400)

    new_password = data.get("password", "")
    access_token = data.get("access_token")  # from Supabase redirect URL fragment

    if not access_token:
        return web.json_response({"error": "Reset token is required."}, status=400)
    if len(new_password) < 8:
        return web.json_response({"error": "Password must be at least 8 characters."}, status=400)

    try:
        # Set the session from the reset token, then update password
        supabase.auth.set_session(access_token, "")
        supabase.auth.update_user({"password": new_password})
        return web.json_response({"message": "Password updated successfully. Please log in."})
    except Exception as exc:
        log.warning("Password reset error: %s", exc)
        return web.json_response(
            {"error": "Reset link is invalid or has expired. Please request a new one."},
            status=400,
        )
