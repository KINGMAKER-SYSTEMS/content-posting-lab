"""
Campaign Hub + Notion integrated sound sync.

Campaign Hub is the source of truth for WHAT campaigns are active.
Notion CRM is the source of truth for TikTok Sound Links.

Flow:
1. Fetch active campaigns from Campaign Hub
2. For each, find the TikTok Sound Link from Notion (AI-assisted fuzzy matching)
3. Add active sounds to Telegram library
4. Deactivate sounds whose Campaign Hub entry is now 'completed'
"""

import json
import logging
import os
import re
from typing import Any

import httpx

log = logging.getLogger("services.campaign_hub")

from services.telegram import list_sounds, toggle_sound, add_sound, remove_sound

CAMPAIGN_HUB_URL = os.getenv(
    "CAMPAIGN_HUB_URL",
    "https://risingtides-campaign-hub-production.up.railway.app",
).rstrip("/")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


# ---------------------------------------------------------------------------
# Normalization for deterministic pass
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = text.replace("\u00f8", "o").replace("\u00e9", "e").replace("\u00e1", "a")
    text = re.sub(r"[^a-z0-9 ]", "", text)
    return " ".join(text.split())


def _nospace(text: str) -> str:
    return text.replace(" ", "")


def _first_collab(artist: str) -> str:
    parts = re.split(r"\s+(?:x|&|and|feat|ft|with)\s+", artist, maxsplit=1)
    return parts[0].strip()


def _match_keys(artist: str, song: str) -> set[str]:
    """Generate normalized keys for deterministic matching."""
    keys = set()
    a = _slugify(artist)
    s = _slugify(song)

    if a and s:
        keys.add(f"{a}|{s}")
        keys.add(f"{_nospace(a)}|{s}")
        keys.add(f"{_nospace(a)}|{_nospace(s)}")
        s1 = s.split()[0] if s.split() else s
        if len(s1) > 3:
            keys.add(f"{a}|{s1}")
            keys.add(f"{_nospace(a)}|{s1}")
    if a:
        keys.add(f"artist:{a}")
        keys.add(f"artist:{_nospace(a)}")
    fa = _first_collab(a) if a else ""
    if fa and fa != a:
        keys.add(f"artist:{fa}")
        keys.add(f"artist:{_nospace(fa)}")
        if s:
            keys.add(f"{fa}|{s}")

    return keys


def _parse_sound_label(label: str) -> tuple[str, str]:
    if " - " in label:
        parts = label.split(" - ", 1)
        return parts[0].strip(), parts[1].strip()
    return label.strip(), ""


# ---------------------------------------------------------------------------
# AI matching
# ---------------------------------------------------------------------------


async def _ai_match_campaigns(
    hub_campaigns: list[dict[str, str]],
    notion_entries: list[dict[str, str]],
) -> dict[str, str | None]:
    """Use GPT-4.1-mini to match Hub campaigns to Notion entries by artist+song.

    Args:
        hub_campaigns: [{"slug": ..., "artist": ..., "song": ...}, ...]
        notion_entries: [{"id": notion_page_id, "artist": ..., "song": ..., "url": ...}, ...]

    Returns: {hub_slug: notion_id_or_None}
    """
    if not OPENAI_API_KEY or not hub_campaigns or not notion_entries:
        return {}

    hub_list = "\n".join(
        f"- [{c['slug']}] {c['artist']} - {c['song']}"
        for c in hub_campaigns
    )
    notion_list = "\n".join(
        f"- [{n['id']}] {n['artist']} - {n['song']}"
        for n in notion_entries
    )

    prompt = f"""Match each campaign to its Notion CRM entry. These are music campaigns where the same artist+song may be spelled differently between systems (typos, abbreviations like "Cam" vs "Cameron", "Mon Rovia" vs "Monrovia", "r2" suffix means round 2 of same campaign, etc).

CAMPAIGNS (need sound links):
{hub_list}

NOTION CRM ENTRIES (have sound links):
{notion_list}

Return a JSON object mapping each campaign slug to the Notion entry ID it matches, or null if no match exists.
Example: {{"artist_song_slug": "notion-page-id", "other_slug": null}}

Only return confident matches. If uncertain, use null. A campaign with "R2"/"r2" in the name is round 2 of the same song — match it to the original Notion entry."""

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4.1-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0,
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return json.loads(content)
    except Exception as exc:
        log.warning("AI campaign matching failed (non-fatal): %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_all_campaigns() -> list[dict[str, Any]]:
    """Fetch all campaigns from the Campaign Hub."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{CAMPAIGN_HUB_URL}/api/campaigns")
        resp.raise_for_status()
        return resp.json()


async def fetch_campaign_detail(slug: str) -> dict[str, Any]:
    """Fetch full campaign detail (including matched_videos and sound_id).

    Note the singular /api/campaign/{slug} path — the plural
    /api/campaigns/{slug} falls through to the Campaign Hub SPA.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{CAMPAIGN_HUB_URL}/api/campaign/{slug}")
        resp.raise_for_status()
        return resp.json()


async def find_campaign_by_label(label: str) -> dict[str, Any] | None:
    """Match a telegram sound label to a Campaign Hub campaign by title.

    The telegram sounds list uses labels like "Artist - Song" which exactly
    match Campaign Hub titles (verified: 23/23 sounds match). Case-insensitive
    comparison. Returns None if no match.
    """
    needle = (label or "").strip().lower()
    if not needle:
        return None
    campaigns = await fetch_all_campaigns()
    for c in campaigns:
        if c.get("title", "").strip().lower() == needle:
            return c
    return None


async def sync_sound_status(notion_campaigns: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Full sound sync: Campaign Hub as source of truth, Notion for sound links.

    1. Fetch all Campaign Hub campaigns
    2. For active ones, find TikTok Sound Link from Notion (deterministic + AI)
    3. Add new active sounds to library
    4. Deactivate sounds for completed campaigns

    Args:
        notion_campaigns: Pre-fetched Notion campaigns (to avoid double-fetch).
            If None, will import and call fetch_campaigns_with_sounds().

    Returns detailed sync result.
    """
    all_hub = await fetch_all_campaigns()
    active_hub = [c for c in all_hub if c.get("completion_status") != "completed"]
    completed_hub = [c for c in all_hub if c.get("completion_status") == "completed"]

    # Get Notion data for sound links
    if notion_campaigns is None:
        from services.notion import fetch_campaigns_with_sounds
        notion_campaigns = await fetch_campaigns_with_sounds()

    # Build Notion lookup by deterministic keys: key -> {url, artist, song, id}
    notion_by_key: dict[str, dict] = {}
    notion_entries_for_ai: list[dict[str, str]] = []
    for n in notion_campaigns:
        entry = {
            "id": n.get("notion_page_id", ""),
            "artist": n.get("artist", ""),
            "song": n.get("song", ""),
            "url": n.get("tiktok_url", ""),
        }
        notion_entries_for_ai.append(entry)
        for key in _match_keys(entry["artist"], entry["song"]):
            notion_by_key[key] = entry

    # Pass 1: Deterministic matching for active campaigns
    matched_active: list[dict] = []  # [{hub_campaign, notion_url, notion_label}]
    unmatched_for_ai: list[dict[str, str]] = []

    for c in active_hub:
        artist = c.get("artist", "")
        song = c.get("song", "")
        hub_keys = _match_keys(artist, song)

        notion_entry = None
        for key in hub_keys:
            if key in notion_by_key:
                notion_entry = notion_by_key[key]
                break

        if notion_entry and notion_entry["url"]:
            matched_active.append({
                "artist": artist,
                "song": song,
                "url": notion_entry["url"],
                "label": f"{artist} - {song}" if artist and song else artist or song,
                "slug": c.get("slug", ""),
            })
        else:
            unmatched_for_ai.append({
                "slug": c.get("slug", ""),
                "artist": artist,
                "song": song,
            })

    # Pass 2: AI matching for unmatched active campaigns
    ai_matched = 0
    if unmatched_for_ai and notion_entries_for_ai:
        ai_results = await _ai_match_campaigns(unmatched_for_ai, notion_entries_for_ai)

        notion_by_id = {n["id"]: n for n in notion_entries_for_ai}
        still_unmatched: list[dict] = []

        for item in unmatched_for_ai:
            notion_id = ai_results.get(item["slug"])
            if notion_id and notion_id in notion_by_id:
                n = notion_by_id[notion_id]
                if n["url"]:
                    matched_active.append({
                        "artist": item["artist"],
                        "song": item["song"],
                        "url": n["url"],
                        "label": f"{item['artist']} - {item['song']}" if item["artist"] and item["song"] else item["artist"] or item["song"],
                        "slug": item["slug"],
                    })
                    ai_matched += 1
                    continue
            still_unmatched.append(item)

        unmatched_for_ai = still_unmatched

    # Now sync the sounds library
    existing_sounds = list_sounds(active_only=False)
    existing_urls = {s.get("url", "").rstrip("/").lower(): s for s in existing_sounds}

    sounds_added = 0
    sounds_deactivated = 0
    sounds_reactivated = 0
    errors: list[str] = []

    # Add/reactivate active campaign sounds
    active_urls: set[str] = set()
    for m in matched_active:
        url_key = m["url"].rstrip("/").lower()
        active_urls.add(url_key)

        existing = existing_urls.get(url_key)
        if existing:
            # Sound exists — make sure it's active
            if not existing.get("active", True):
                try:
                    toggle_sound(existing["id"], True)
                    sounds_reactivated += 1
                except Exception as exc:
                    errors.append(f"reactivate {m['label']}: {exc}")
        else:
            # New sound — add as active
            try:
                add_sound(url=m["url"], label=m["label"])
                sounds_added += 1
            except Exception as exc:
                errors.append(f"add {m['label']}: {exc}")

    # Deactivate sounds for completed campaigns
    # Build completed URL set from deterministic + AI matching
    completed_urls: set[str] = set()
    for c in completed_hub:
        hub_keys = _match_keys(c.get("artist", ""), c.get("song", ""))
        for key in hub_keys:
            if key in notion_by_key:
                url = notion_by_key[key].get("url", "")
                if url:
                    completed_urls.add(url.rstrip("/").lower())
                break

    for sound in existing_sounds:
        url_key = sound.get("url", "").rstrip("/").lower()
        if url_key not in active_urls and sound.get("active", True):
            # Not in active list — deactivate
            try:
                toggle_sound(sound["id"], False)
                sounds_deactivated += 1
            except Exception as exc:
                errors.append(f"deactivate {sound.get('label', '')}: {exc}")

    return {
        "active_campaigns": len(active_hub),
        "completed_campaigns": len(completed_hub),
        "sounds_added": sounds_added,
        "sounds_deactivated": sounds_deactivated,
        "sounds_reactivated": sounds_reactivated,
        "matched_deterministic": len(matched_active) - ai_matched,
        "matched_ai": ai_matched,
        "unmatched": [f"{u['artist']} - {u['song']}" for u in unmatched_for_ai],
        "errors": errors,
    }


def is_configured() -> bool:
    """Check if Campaign Hub integration is available."""
    return bool(CAMPAIGN_HUB_URL)
