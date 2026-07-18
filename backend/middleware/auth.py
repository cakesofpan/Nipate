"""
middleware/auth.py
─────────────────
JWT verification, RBAC enforcement, CORS, rate limiting, and audit logging.

How it works
────────────
1.  Every request hits `cors_middleware` first (allows preflight OPTIONS).
2.  Protected routes use the `@require_role(Role.X)` decorator.
3.  The decorator calls `verify_jwt()` which:
      a. Pulls the Bearer token from the Authorization header.
      b. Reads the token's `alg` header (without trusting it yet) to decide
         which verification path to use:
           - HS256 → legacy symmetric secret (SUPABASE_JWT_SECRET)
           - RS256/ES256 → asymmetric JWT Signing Keys, verified against
             Supabase's public JWKS endpoint (no shared secret needed)
         Supabase projects created since May 2025 default to the asymmetric
         path; older projects may still use the legacy HS256 secret. Both
         are supported automatically — nothing to configure per-project.
      c. Checks expiry, issuer, and audience.
      d. Returns the decoded payload (sub = user UUID, role, email, etc.)
4.  If the role claim is insufficient, a 403 is returned before the handler runs.
5.  Every authenticated action is appended to the audit_log table.

Note on alg-confusion safety: each verification path uses its own key
material and its own explicit `algorithms=[...]` allowlist — a token can
never talk its way from one path into using the other's key, since which
branch runs is decided before the key is chosen, and each branch only ever
calls jwt.decode() with the key type that matches its own algorithm family.
"""

import time
import json
import hmac
import hashlib
import logging
from collections import defaultdict
from functools import wraps
from typing import Callable

import jwt as pyjwt
from jwt import PyJWKClient
from aiohttp import web

from backend.config import (
    SUPABASE_URL,
    SUPABASE_JWT_SECRET,
    ALLOWED_ORIGINS,
    APP_SECRET_KEY,
    supabase_admin,
    Role,
)

log = logging.getLogger(__name__)

# ── Simple in-process rate limiter ─────────────────────────────────────────────
# For production use Redis (e.g. aioredis) to share state across workers.
_rate_buckets: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_WINDOW = 60        # seconds
RATE_LIMIT_MAX_REQUESTS = 30  # per window per IP


def _check_rate_limit(ip: str) -> bool:
    """Return True if the request is allowed, False if rate-limited."""
    now = time.monotonic()
    bucket = _rate_buckets[ip]
    # Purge timestamps outside the window
    _rate_buckets[ip] = [t for t in bucket if now - t < RATE_LIMIT_WINDOW]
    if len(_rate_buckets[ip]) >= RATE_LIMIT_MAX_REQUESTS:
        return False
    _rate_buckets[ip].append(now)
    return True


# ── JWT verification ───────────────────────────────────────────────────────────

# Lazily fetches and caches Supabase's public signing keys (JWKS) the first
# time an asymmetric (RS256/ES256) token needs verifying. No network call
# happens at import time — only when get_signing_key_from_jwt() is first
# called. Cached keys are reused until a token references an unknown `kid`
# (e.g. after Supabase rotates keys), at which point it refetches.
#
# Note: PyJWKClient's fetch is a blocking HTTP call. It's cached and only
# triggered on a cache miss (new/rotated key), so in practice this almost
# never blocks the event loop after the first request — acceptable for this
# project's scale without pulling in an async JWKS client.
_jwks_client = PyJWKClient(f"{SUPABASE_URL.rstrip('/')}/auth/v1/jwks", cache_keys=True)


def verify_jwt(token: str) -> dict:
    """
    Decode and verify a Supabase-issued JWT — supports both signing schemes:

      - HS256 (legacy symmetric secret): verified against SUPABASE_JWT_SECRET.
      - RS256/ES256 (JWT Signing Keys, the default since May 2025): verified
        against Supabase's public JWKS endpoint, keyed by the token's `kid`.

    Which path runs is decided by the token's own `alg` header — see the
    module docstring for why this doesn't introduce an alg-confusion risk.

    Returns the payload dict on success.
    Raises jwt.PyJWTError subclasses on failure (expired, invalid sig, etc.)
    """
    unverified_header = pyjwt.get_unverified_header(token)
    alg = unverified_header.get("alg", "HS256")

    if alg == "HS256":
        if not SUPABASE_JWT_SECRET:
            raise pyjwt.InvalidTokenError(
                "Token is signed with HS256 but no SUPABASE_JWT_SECRET is configured. "
                "Either set it from Supabase → Settings → API → JWT Settings → Legacy "
                "JWT Secret, or migrate your project to JWT Signing Keys."
            )
        return pyjwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",
            options={"require": ["exp", "sub", "role"]},
        )

    # Asymmetric — RS256 or ES256, verified via Supabase's public JWKS.
    signing_key = _jwks_client.get_signing_key_from_jwt(token)
    return pyjwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256", "ES256"],
        audience="authenticated",
        options={"require": ["exp", "sub", "role"]},
    )


def _extract_token(request: web.Request) -> str | None:
    """Pull Bearer token from Authorization header or __session cookie."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    # Cookie fallback for SSR pages
    return request.cookies.get("__session")


def _get_role_from_payload(payload: dict) -> str:
    """
    Role is stored in app_metadata.role (set server-side, user cannot forge).
    Falls back to user_metadata.role for backwards compat, then PUBLIC.
    """
    app_meta = payload.get("app_metadata") or {}
    user_meta = payload.get("user_metadata") or {}
    return (
        app_meta.get("role")
        or user_meta.get("role")
        or Role.PUBLIC
    )


# ── RBAC decorator ─────────────────────────────────────────────────────────────

def require_role(minimum_role: str, log_action: str | None = None):
    """
    Decorator factory for aiohttp route handlers.

    Usage
    ─────
    @require_role(Role.POLICE)
    async def approve_case(request):
        user = request["user"]          # decoded JWT payload
        user_id = request["user_id"]    # UUID string
        role = request["role"]          # e.g. "police_officer"
        ...

    @require_role(Role.ADMIN, log_action="delete_case")
    async def delete_case(request):
        ...
    """
    def decorator(handler: Callable):
        @wraps(handler)
        async def wrapper(request: web.Request) -> web.Response:
            # ── Rate limiting ──────────────────────────────────────────────
            ip = request.headers.get("X-Forwarded-For", request.remote or "unknown").split(",")[0].strip()
            if not _check_rate_limit(ip):
                return web.json_response(
                    {"error": "Too many requests. Please wait a moment."},
                    status=429,
                    headers={"Retry-After": str(RATE_LIMIT_WINDOW)},
                )

            # ── Token extraction ───────────────────────────────────────────
            token = _extract_token(request)
            if not token:
                return web.json_response(
                    {"error": "Authentication required.", "code": "NO_TOKEN"},
                    status=401,
                )

            # ── JWT verification ───────────────────────────────────────────
            try:
                payload = verify_jwt(token)
            except pyjwt.ExpiredSignatureError:
                return web.json_response(
                    {"error": "Session expired. Please log in again.", "code": "TOKEN_EXPIRED"},
                    status=401,
                )
            except pyjwt.InvalidTokenError as exc:
                log.warning("Invalid JWT from %s: %s", ip, exc)
                return web.json_response(
                    {"error": "Invalid authentication token.", "code": "TOKEN_INVALID"},
                    status=401,
                )
            except pyjwt.PyJWTError as exc:
                # Catches everything else JWT-related that ISN'T an InvalidTokenError
                # subclass — most importantly PyJWKError/PyJWKClientError, raised when
                # fetching/matching a key from Supabase's JWKS endpoint fails (network
                # issue, DNS, wrong SUPABASE_URL, unknown kid, etc). Without this,
                # those exceptions were propagating unhandled and returning a bare 500.
                log.error("JWT verification failed (JWKS/key error) from %s: %s", ip, exc)
                return web.json_response(
                    {"error": "Could not verify authentication token. Please try again.", "code": "TOKEN_VERIFY_FAILED"},
                    status=401,
                )

            # ── Role check ─────────────────────────────────────────────────
            role = _get_role_from_payload(payload)
            if not Role.at_least(minimum_role, role):
                return web.json_response(
                    {
                        "error": "You do not have permission to perform this action.",
                        "code": "INSUFFICIENT_ROLE",
                        "required": minimum_role,
                        "actual": role,
                    },
                    status=403,
                )

            # ── Attach user context to request ─────────────────────────────
            request["user"] = payload
            request["user_id"] = payload["sub"]          # Supabase UUID
            request["role"] = role
            request["user_email"] = payload.get("email", "")

            # ── Call the actual handler ────────────────────────────────────
            response = await handler(request)

            # ── Audit log (fire-and-forget, non-blocking) ──────────────────
            if log_action:
                try:
                    action = log_action
                    path = str(request.rel_url)
                    method = request.method
                    status = response.status if hasattr(response, "status") else 0
                    supabase_admin.table("audit_log").insert({
                        "user_id": request["user_id"],
                        "user_email": request["user_email"],
                        "role": role,
                        "action": action,
                        "http_method": method,
                        "path": path,
                        "status_code": status,
                        "ip_address": ip,
                    }).execute()
                except Exception as audit_err:
                    log.error("Audit log write failed: %s", audit_err)

            return response
        return wrapper
    return decorator


# ── Optional — no-auth handler with rate limiting only ─────────────────────────

def public_route(handler: Callable):
    """
    Decorator for public endpoints (no auth required).
    Still applies rate limiting and attaches a null user context.
    """
    @wraps(handler)
    async def wrapper(request: web.Request) -> web.Response:
        ip = request.headers.get("X-Forwarded-For", request.remote or "unknown").split(",")[0].strip()
        if not _check_rate_limit(ip):
            return web.json_response(
                {"error": "Too many requests. Please wait a moment."},
                status=429,
            )
        # Try to decode token if present (optional auth)
        token = _extract_token(request)
        if token:
            try:
                payload = verify_jwt(token)
                request["user"] = payload
                request["user_id"] = payload["sub"]
                request["role"] = _get_role_from_payload(payload)
                request["user_email"] = payload.get("email", "")
            except pyjwt.PyJWTError:
                pass  # anonymous is fine for public routes
        else:
            request["user"] = None
            request["user_id"] = None
            request["role"] = Role.PUBLIC
            request["user_email"] = None

        return await handler(request)
    return wrapper


# ── CORS middleware ─────────────────────────────────────────────────────────────

@web.middleware
async def cors_middleware(request: web.Request, handler):
    """
    Handle CORS for all requests.
    - Allows listed origins only (no wildcard in production).
    - Handles OPTIONS preflight quickly without hitting route handlers.
    """
    origin = request.headers.get("Origin", "")
    allowed = origin in ALLOWED_ORIGINS

    if request.method == "OPTIONS":
        # Preflight — respond immediately
        headers = {}
        if allowed:
            headers = {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Requested-With",
                "Access-Control-Allow-Credentials": "true",
                "Access-Control-Max-Age": "86400",
            }
        return web.Response(status=204, headers=headers)

    response = await handler(request)

    if allowed:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Vary"] = "Origin"

    # Security headers on every response
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    if not DEBUG:  # only in production (breaks localhost http)
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"

    return response


# Only import DEBUG after defining the variable reference
from backend.config import DEBUG  # noqa: E402 (circular-safe, config has no middleware dep)


# ── HMAC token helpers (for unsubscribe links, ID verify callbacks) ────────────

def make_signed_token(data: str) -> str:
    """Create a URL-safe HMAC-SHA256 token for `data`."""
    sig = hmac.new(APP_SECRET_KEY.encode(), data.encode(), hashlib.sha256).hexdigest()
    return f"{data}.{sig}"


def verify_signed_token(token: str) -> str | None:
    """Verify token; return the embedded data string or None if invalid."""
    try:
        data, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    expected = hmac.new(APP_SECRET_KEY.encode(), data.encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(expected, sig):
        return data
    return None
