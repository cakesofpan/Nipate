"""
services/storage.py
────────────────────
Supabase Storage wrappers for all file upload operations.

Buckets
───────
id-documents   — PRIVATE. ID verification docs. Only admins can read.
case-photos    — PUBLIC. Images attached to verified cases.
tip-evidence   — PRIVATE. Files submitted with tips. Police/admin only.
"""

import io
import uuid
import logging
from datetime import datetime

from backend.config import supabase_admin

log = logging.getLogger(__name__)

BUCKET_ID_DOCS = "id-documents"
BUCKET_CASE_PHOTOS = "case-photos"
BUCKET_TIP_EVIDENCE = "tip-evidence"


async def upload_id_document(
    user_id: str,
    file_bytes: bytes,
    extension: str,
    content_type: str,
) -> str:
    """
    Upload an ID verification document to the private `id-documents` bucket.

    Returns the storage path (used to generate signed URLs for admin review).
    Path format: {user_id}/{timestamp}_{uuid}.{ext}
    """
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    file_uuid = str(uuid.uuid4())[:8]
    path = f"{user_id}/{timestamp}_{file_uuid}.{extension}"

    supabase_admin.storage.from_(BUCKET_ID_DOCS).upload(
        path=path,
        file=io.BytesIO(file_bytes),
        file_options={"content-type": content_type, "upsert": "false"},
    )

    log.info("ID document uploaded: %s", path)
    return path


async def upload_case_photo(
    case_id: str,
    file_bytes: bytes,
    extension: str,
    content_type: str,
) -> str:
    """
    Upload a case photo to the public `case-photos` bucket.
    Returns the full public URL.
    """
    file_uuid = str(uuid.uuid4())[:12]
    path = f"{case_id}/{file_uuid}.{extension}"

    supabase_admin.storage.from_(BUCKET_CASE_PHOTOS).upload(
        path=path,
        file=io.BytesIO(file_bytes),
        file_options={"content-type": content_type, "upsert": "false"},
    )

    # Build the public URL
    result = supabase_admin.storage.from_(BUCKET_CASE_PHOTOS).get_public_url(path)
    return result


async def upload_tip_evidence(
    tip_id: str,
    file_bytes: bytes,
    extension: str,
    content_type: str,
) -> str:
    """
    Upload tip evidence to the private `tip-evidence` bucket.
    Returns storage path (signed URLs generated on request by police/admin).
    """
    file_uuid = str(uuid.uuid4())[:12]
    path = f"{tip_id}/{file_uuid}.{extension}"

    supabase_admin.storage.from_(BUCKET_TIP_EVIDENCE).upload(
        path=path,
        file=io.BytesIO(file_bytes),
        file_options={"content-type": content_type, "upsert": "false"},
    )

    log.info("Tip evidence uploaded: %s", path)
    return path


def get_signed_url(bucket: str, path: str, expires_in: int = 3600) -> str:
    """Generate a time-limited signed URL for a private bucket object."""
    result = supabase_admin.storage.from_(bucket).create_signed_url(path, expires_in)
    return result.get("signedURL", "")
