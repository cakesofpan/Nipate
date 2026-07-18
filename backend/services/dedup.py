"""
services/dedup.py
─────────────────
Fuzzy duplicate detection for missing person cases.

Logic
─────
Before inserting a new case, compute a similarity score against existing
open cases in the same county using:
  - Token-sorted ratio on full name        (weight 0.6)
  - Exact match on date of birth           (weight 0.3)
  - County match                           (weight 0.1)

A combined score ≥ THRESHOLD flags the pair for manual review.
Cases are NOT auto-rejected — officers decide whether to merge.
"""

import logging
from dataclasses import dataclass

from rapidfuzz import fuzz
from backend.config import supabase_admin

log = logging.getLogger(__name__)

# Similarity threshold (0–100). Tune based on real data.
THRESHOLD = 78


@dataclass
class DuplicateMatch:
    case_id: str
    case_number: str
    full_name: str
    date_of_birth: str
    last_seen_county: str
    score: float          # 0–100


async def find_duplicates(
    full_name: str,
    date_of_birth: str,       # ISO format YYYY-MM-DD
    last_seen_county: str,
    exclude_id: str | None = None,
) -> list[DuplicateMatch]:
    """
    Return a list of existing cases that may be duplicates of the given details.
    Only searches open cases (status != 'closed', 'found_safe', 'found_deceased').
    """
    county = last_seen_county.strip().lower()

    try:
        # Pull candidates: same county, open statuses only
        resp = supabase_admin.table("cases").select(
            "id, case_number, full_name, date_of_birth, last_seen_county, status"
        ).in_(
            "status", ["reported", "under_investigation"]
        ).ilike(
            "last_seen_county", f"%{county}%"
        ).execute()

        candidates = resp.data or []
    except Exception as exc:
        log.error("Dedup query failed: %s", exc)
        return []

    matches: list[DuplicateMatch] = []

    for c in candidates:
        if exclude_id and c["id"] == exclude_id:
            continue

        # Name similarity (token sort handles reordered names, e.g. "John Doe" vs "Doe John")
        name_score = fuzz.token_sort_ratio(
            full_name.lower().strip(),
            (c["full_name"] or "").lower().strip(),
        )

        # DOB exact match gives big weight
        dob_score = 100 if c.get("date_of_birth") == date_of_birth else 0

        # County already filtered — give small bonus for exact match
        county_score = 100 if (c.get("last_seen_county") or "").lower() == county else 70

        # Weighted composite
        combined = (name_score * 0.6) + (dob_score * 0.3) + (county_score * 0.1)

        if combined >= THRESHOLD:
            matches.append(DuplicateMatch(
                case_id=c["id"],
                case_number=c.get("case_number", ""),
                full_name=c.get("full_name", ""),
                date_of_birth=c.get("date_of_birth", ""),
                last_seen_county=c.get("last_seen_county", ""),
                score=round(combined, 1),
            ))

    # Sort by score descending
    matches.sort(key=lambda m: m.score, reverse=True)
    log.info(
        "Dedup check for '%s' (%s): %d potential match(es)",
        full_name, date_of_birth, len(matches),
    )
    return matches
