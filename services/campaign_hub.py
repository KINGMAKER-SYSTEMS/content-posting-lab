"""
Campaign Hub integration for sound lifecycle management.
Polls the Campaign Hub API to detect completed campaigns and deactivate
their corresponding sounds in the Telegram sound library.
"""

import os
from typing import Any

import httpx

from services.telegram import list_sounds, toggle_sound

CAMPAIGN_HUB_URL = os.getenv(
    "CAMPAIGN_HUB_URL",
    "https://risingtides-campaign-hub-production.up.railway.app",
).rstrip("/")


def _normalize(text: str) -> str:
    """Normalize text for fuzzy matching: lowercase, strip, collapse whitespace."""
    return " ".join(text.lower().split())


def _build_match_key(artist: str, song: str) -> str:
    """Build a match key from artist + song for comparison."""
    return f"{_normalize(artist)}|{_normalize(song)}"


def _parse_sound_label(label: str) -> tuple[str, str]:
    """Parse a sound label 'Artist - Song' back into (artist, song).

    Falls back to (label, '') if no ' - ' separator found.
    """
    if " - " in label:
        parts = label.split(" - ", 1)
        return parts[0].strip(), parts[1].strip()
    return label.strip(), ""


async def fetch_completed_campaigns() -> list[dict[str, Any]]:
    """Fetch all campaigns from the Campaign Hub that are marked completed."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{CAMPAIGN_HUB_URL}/api/campaigns")
        resp.raise_for_status()
        campaigns = resp.json()

    return [
        c for c in campaigns
        if c.get("completion_status") == "completed"
    ]


async def sync_sound_status() -> dict[str, Any]:
    """Cross-reference Campaign Hub completion status with Telegram sounds.

    For each active sound, check if its artist+song matches a completed
    campaign in the Campaign Hub. If so, deactivate the sound.

    Returns {deactivated: int, already_inactive: int, still_active: int, errors: list}.
    """
    completed = await fetch_completed_campaigns()

    # Build a set of match keys from completed campaigns
    completed_keys: set[str] = set()
    for c in completed:
        artist = c.get("artist", "")
        song = c.get("song", "")
        if artist or song:
            completed_keys.add(_build_match_key(artist, song))
        # Also try matching on the title field (format: "Artist - Song")
        title = c.get("title", "")
        if " - " in title:
            t_artist, t_song = title.split(" - ", 1)
            completed_keys.add(_build_match_key(t_artist, t_song))

    all_sounds = list_sounds(active_only=False)
    deactivated = 0
    already_inactive = 0
    still_active = 0
    errors: list[str] = []

    for sound in all_sounds:
        label = sound.get("label", "")
        sound_artist, sound_song = _parse_sound_label(label)
        sound_key = _build_match_key(sound_artist, sound_song)

        if sound_key in completed_keys:
            if sound.get("active", True):
                try:
                    toggle_sound(sound["id"], False)
                    deactivated += 1
                except Exception as exc:
                    errors.append(f"{label}: {exc}")
            else:
                already_inactive += 1
        else:
            if sound.get("active", True):
                still_active += 1

    return {
        "deactivated": deactivated,
        "already_inactive": already_inactive,
        "still_active": still_active,
        "completed_campaigns": len(completed),
        "errors": errors,
    }


def is_configured() -> bool:
    """Check if Campaign Hub integration is available."""
    return bool(CAMPAIGN_HUB_URL)
