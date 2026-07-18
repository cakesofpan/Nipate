"""
config.py — centralised configuration, loaded once at startup.
All other modules import from here rather than touching os.environ directly.
"""
import os
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

# ── Supabase ──────────────────────────────────────────────────────────────────
SUPABASE_URL: str = os.environ["SUPABASE_URL"]
SUPABASE_ANON_KEY: str = os.environ["SUPABASE_ANON_KEY"]
SUPABASE_SERVICE_ROLE_KEY: str = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

# ── JWT (Supabase signs with this secret for legacy/symmetric projects) ────────
# Optional: only needed if your Supabase project still has a Legacy JWT Secret
# configured (Settings → API → JWT Settings). Projects created since May 2025
# default to asymmetric JWT Signing Keys instead, which don't need this at all —
# see backend/middleware/auth.py for how both are verified automatically.
SUPABASE_JWT_SECRET: str = os.getenv("SUPABASE_JWT_SECRET", "")

# Public client — respects Row-Level Security (used for user-scoped queries)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

# Admin client — bypasses RLS (use only in trusted server-side operations)
supabase_admin: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# ── App ───────────────────────────────────────────────────────────────────────
APP_HOST: str = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT: int = int(os.getenv("APP_PORT", "8080"))
APP_SECRET_KEY: str = os.environ["APP_SECRET_KEY"]
DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"
ALLOWED_ORIGINS: list[str] = os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:3000"
).split(",")

# ── Email ─────────────────────────────────────────────────────────────────────
SMTP_HOST: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER: str = os.environ["SMTP_USER"]
SMTP_PASSWORD: str = os.environ["SMTP_PASSWORD"]

# ── SMS (SMSGate — sms-gate.app / capcom6/android-sms-gateway) ────────────────
# SMSGate turns an Android phone into an SMS gateway and exposes a REST API.
# Three deployment modes are supported, chosen via SMSGATE_MODE:
#   "cloud"   — the maintainers' public relay at api.sms-gate.app (default, easiest)
#   "private" — a self-hosted relay server (e.g. the capcom6/sms-gateway Docker
#               image) running on your own infrastructure, mirrors the cloud API
#   "local"   — talk directly to the Android device's local HTTP server over
#               the same Wi-Fi/LAN (no internet required, but device must be
#               reachable — not usable from a cloud-hosted backend)
# Get credentials by opening the SMSGate app on the sending device and
# enabling Cloud Server (or configuring Private/Local mode) — the app displays
# the username/password to use for Basic Auth.
SMSGATE_MODE: str = os.getenv("SMSGATE_MODE", "cloud")            # cloud | private | local
SMSGATE_BASE_URL: str = os.getenv("SMSGATE_BASE_URL", "")         # required for private/local; ignored for cloud
SMSGATE_USERNAME: str = os.getenv("SMSGATE_USERNAME", "")
SMSGATE_PASSWORD: str = os.getenv("SMSGATE_PASSWORD", "")
SMS_ENABLED: bool = bool(SMSGATE_USERNAME and SMSGATE_PASSWORD)   # SMS silently no-ops if not configured

# ── Maps ──────────────────────────────────────────────────────────────────────
GOOGLE_MAPS_API_KEY: str = os.getenv("GOOGLE_MAPS_API_KEY", "")

# ── Identity verification (Didit) ──────────────────────────────────────────────
# Didit is a hosted KYC provider (document OCR + liveness + face-match) used
# to automatically verify reporters' identity documents, replacing/augmenting
# the manual admin-review flow. See backend/services/didit.py for details.
DIDIT_BASE_URL: str = os.getenv("DIDIT_BASE_URL", "https://verification.didit.me")
DIDIT_API_KEY: str = os.getenv("DIDIT_API_KEY", "")
DIDIT_WORKFLOW_ID: str = os.getenv("DIDIT_WORKFLOW_ID", "")
DIDIT_WEBHOOK_SECRET: str = os.getenv("DIDIT_WEBHOOK_SECRET", "")
DIDIT_ENABLED: bool = bool(DIDIT_API_KEY and DIDIT_WORKFLOW_ID)

# ── Roles (stored in Supabase user_metadata.role) ────────────────────────────
class Role:
    PUBLIC = "public_viewer"         # unauthenticated or guest
    USER = "registered_user"         # verified account holder
    POLICE = "police_officer"        # verified law enforcement
    ADMIN = "admin"                  # platform administrator

    ALL = [PUBLIC, USER, POLICE, ADMIN]

    # Ordered permission tiers — higher index = more privilege
    _TIER = {PUBLIC: 0, USER: 1, POLICE: 2, ADMIN: 3}

    @classmethod
    def at_least(cls, required: str, actual: str) -> bool:
        """Return True if `actual` role has >= privilege than `required`."""
        return cls._TIER.get(actual, -1) >= cls._TIER.get(required, 999)
