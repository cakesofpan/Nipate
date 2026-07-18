"""
services/social.py
──────────────────
Social media sharing for verified missing person alerts.

Covers
──────
- Twitter/X v2 API — post an alert tweet
- Facebook — generate a pre-filled share URL (no server-side FB API required;
  uses the public sharer endpoint which respects Open Graph meta tags)
- WhatsApp — generate a wa.me share link
- Generic Open Graph meta tag dict (used by case.html template)

Twitter setup
─────────────
Requires a Twitter Developer App with OAuth 1.0a credentials (read+write).
Set in .env: TWITTER_API_KEY, TWITTER_API_SECRET,
             TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET
"""

import os
import logging
import textwrap

import tweepy  # pip install tweepy

log = logging.getLogger(__name__)

_twitter_client: tweepy.Client | None = None


def _get_twitter_client() -> tweepy.Client | None:
    """Lazy-init Twitter client; returns None if credentials are missing."""
    global _twitter_client
    if _twitter_client:
        return _twitter_client

    api_key        = os.getenv("TWITTER_API_KEY")
    api_secret     = os.getenv("TWITTER_API_SECRET")
    access_token   = os.getenv("TWITTER_ACCESS_TOKEN")
    access_secret  = os.getenv("TWITTER_ACCESS_SECRET")

    if not all([api_key, api_secret, access_token, access_secret]):
        log.warning("Twitter credentials not configured — social posting disabled.")
        return None

    _twitter_client = tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_secret,
    )
    return _twitter_client


# ── Twitter ────────────────────────────────────────────────────────────────────

def post_twitter_alert(case: dict) -> str | None:
    """
    Post a missing person alert tweet.
    Returns the tweet URL on success, None on failure.

    Tweet format (≤ 280 chars):
    ⚠ MISSING: [Name], [Gender], [Age]
    Last seen: [Location], [County] on [Date]
    [Description snippet]
    Case: TF-XXXX-NNNNN
    🔗 [case_url]
    #MissingPerson #Kenya #[County]
    """
    client = _get_twitter_client()
    if not client:
        return None

    name        = case.get("full_name", "Unknown")
    gender      = (case.get("gender") or "").capitalize()
    age         = case.get("age", "?")
    location    = case.get("last_seen_location", "")
    county      = case.get("last_seen_county", "")
    last_date   = case.get("last_seen_date", "")
    case_number = case.get("case_number", "")
    case_id     = case.get("id", "")
    description = case.get("physical_description", "")
    case_url    = f"https://nipate.go.ke/case/{case_id}"

    # Truncate description to keep tweet under 280 chars
    desc_snippet = textwrap.shorten(description, width=80, placeholder="…")

    county_tag = county.replace(" ", "").replace("-", "")

    tweet = (
        f"⚠ MISSING: {name}, {gender}, Age {age}\n"
        f"Last seen: {location}, {county} County on {last_date}\n"
        f"{desc_snippet}\n"
        f"Case: {case_number}\n"
        f"🔗 {case_url}\n"
        f"#MissingPerson #Kenya #{county_tag}"
    )

    # Hard truncate to 280 chars as safety net
    if len(tweet) > 280:
        tweet = tweet[:277] + "…"

    try:
        response = client.create_tweet(text=tweet)
        tweet_id = response.data["id"]
        tweet_url = f"https://twitter.com/NipateKenya/status/{tweet_id}"
        log.info("Tweet posted for case %s: %s", case_number, tweet_url)
        return tweet_url
    except Exception as exc:
        log.error("Twitter post failed for case %s: %s", case_number, exc)
        return None


# ── Facebook ───────────────────────────────────────────────────────────────────

def get_facebook_share_url(case_id: str) -> str:
    """
    Return a Facebook sharer URL for the case page.
    Facebook will read Open Graph meta tags from the page automatically.
    No server-side credentials needed.
    """
    case_url = f"https://nipate.go.ke/case/{case_id}"
    return f"https://www.facebook.com/sharer/sharer.php?u={case_url}"


# ── WhatsApp ───────────────────────────────────────────────────────────────────

def get_whatsapp_share_url(case: dict) -> str:
    """
    Return a WhatsApp share link with pre-filled message text.
    Works on both mobile (opens WhatsApp app) and desktop (opens web.whatsapp.com).
    """
    import urllib.parse

    name       = case.get("full_name", "Unknown")
    county     = case.get("last_seen_county", "")
    case_id    = case.get("id", "")
    case_url   = f"https://nipate.go.ke/case/{case_id}"

    message = (
        f"⚠ MISSING PERSON ALERT\n"
        f"Name: {name}\n"
        f"Last seen in {county} County\n"
        f"If you have information, please visit:\n{case_url}\n"
        f"You can also submit an anonymous tip."
    )
    encoded = urllib.parse.quote(message)
    return f"https://api.whatsapp.com/send?text={encoded}"


# ── Open Graph meta tags ───────────────────────────────────────────────────────

def build_og_tags(case: dict) -> dict[str, str]:
    """
    Return a dict of Open Graph meta tag key → value for a case page.
    Used by the server when rendering case.html to improve link previews.
    """
    name       = case.get("full_name", "Unknown")
    age        = case.get("age", "?")
    gender     = (case.get("gender") or "").capitalize()
    county     = case.get("last_seen_county", "")
    case_id    = case.get("id", "")
    case_url   = f"https://nipate.go.ke/case/{case_id}"
    description = case.get("physical_description", "")

    # Primary photo URL (first case image if available)
    images      = case.get("images", [])
    image_url   = images[0].get("storage_url") if images else "https://nipate.go.ke/assets/og-default.png"

    return {
        "og:type":        "article",
        "og:url":         case_url,
        "og:title":       f"MISSING: {name} — Nipate",
        "og:description": f"{gender}, Age {age}. Last seen in {county} County. {description[:120]}…",
        "og:image":       image_url,
        "twitter:card":   "summary_large_image",
        "twitter:title":  f"MISSING: {name}",
        "twitter:description": f"{gender}, Age {age}. Last seen in {county} County.",
        "twitter:image":  image_url,
    }
