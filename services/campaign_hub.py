"""
Campaign Hub integration for sound lifecycle management.
Polls the Campaign Hub API to detect completed campaigns and deactivate
their corresponding sounds in the Telegram sound library.

Two-pass matching:
1. Deterministic: normalized artist+song exact match (free, instant)
2. AI-assisted: sends unmatched sounds + hub campaigns to LLM for fuzzy
   matching (handles typos, abbreviations, name variations)
"""

import json
import os
import re
from typing import Any

import httpx

from services.telegram import list_sounds, toggle_sound

CAMPAIGN_HUB_URL = os.getenv(
    "CAMPAIGN_HUB_URL",
    "https://risingtides-campaign-hub-production.up.railway.app",
).rstrip("/")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


# ---------------------------------------------------------------------------
# Normalization helpers (pass 1: deterministic)
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """Normalize for matching: lowercase, strip punctuation, collapse spaces."""
    text = text.lower().strip()
    text = text.replace("\u00f8", "o").replace("\u00e9", "e").replace("\u00e1", "a")
    text = re.sub(r"[^a-z0-9 ]", "", text)
    return " ".join(text.split())


def _nospace(text: str) -> str:
    return text.replace(" ", "")


def _first_collab(artist: str) -> str:
    """Extract first artist from collab string: 'A & B' -> 'A', 'A X B' -> 'A'."""
    parts = re.split(r"\s+(?:x|&|and|feat|ft|with)\s+", artist, maxsplit=1)
    return parts[0].strip()


def _deterministic_keys(artist: str, song: str) -> set[str]:
    """Generate normalized match keys for deterministic pass."""
    keys = set()
    a = _slugify(artist)
    s = _slugify(song)

    if a and s:
        keys.add(f"{a}|{s}")
        keys.add(f"{_nospace(a)}|{s}")
        keys.add(f"{_nospace(a)}|{_nospace(s)}")
        # First word of song (iloveit vs iloveit r3)
        s1 = s.split()[0] if s.split() else s
        if len(s1) > 3:
            keys.add(f"{a}|{s1}")
            keys.add(f"{_nospace(a)}|{s1}")
    if a:
        keys.add(f"artist:{a}")
        keys.add(f"artist:{_nospace(a)}")

    # First collab artist
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
# AI matching (pass 2: handles typos, abbreviations, etc.)
# ---------------------------------------------------------------------------


async def _ai_match_sounds(
    unmatched_sounds: list[dict[str, str]],
    hub_campaigns: list[dict[str, str]],
) -> dict[str, str | None]:
    """Use GPT-4.1-mini to fuzzy-match sounds to campaign hub entries.

    Args:
        unmatched_sounds: [{"id": sound_id, "label": "Artist - Song"}, ...]
        hub_campaigns: [{"slug": slug, "title": "Artist - Song", "status": "completed"}, ...]

    Returns: {sound_id: hub_slug_or_None} for each unmatched sound.
    """
    if not OPENAI_API_KEY or not unmatched_sounds:
        return {}

    # Build compact representations
    sound_list = "\n".join(f"- [{s['id']}] {s['label']}" for s in unmatched_sounds)
    hub_list = "\n".join(
        f"- [{c['slug']}] {c['title']} ({c['status']})"
        for c in hub_campaigns
    )

    prompt = f"""Match each sound to its corresponding campaign. These are music campaigns where the same artist+song may be spelled differently (typos, abbreviations, name variations like "Cam" vs "Cameron", "Mon Rovia" vs "Monrovia", etc).

SOUNDS (unmatched):
{sound_list}

CAMPAIGNS:
{hub_list}

Return a JSON object mapping each sound ID to the campaign slug it matches, or null if no match.
Example: {{"abc123": "artist_song_slug", "def456": null}}

Only return confident matches. If uncertain, use null."""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
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
        print(f"  AI matching failed (non-fatal): {exc}", flush=True)
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


async def sync_sound_status() -> dict[str, Any]:
    """Cross-reference Campaign Hub completion status with Telegram sounds.

    Pass 1 (deterministic): exact normalized artist+song matching.
    Pass 2 (AI): sends remaining unmatched sounds to GPT-4.1-mini for
    fuzzy matching to handle typos and name variations.

    Returns {deactivated, already_inactive, still_active, matched, unmatched, errors}.
    """
    all_hub = await fetch_all_campaigns()

    # Build deterministic lookup: key -> completion_status
    hub_status_by_key: dict[str, str] = {}
    hub_slug_to_status: dict[str, str] = {}
    for c in all_hub:
        artist = c.get("artist", "")
        song = c.get("song", "")
        status = c.get("completion_status", "none")
        slug = c.get("slug", "")
        hub_slug_to_status[slug] = status

        for key in _deterministic_keys(artist, song):
            hub_status_by_key[key] = status
        # Also from title
        title = c.get("title", "")
        if " - " in title:
            t_artist, t_song = title.split(" - ", 1)
            for key in _deterministic_keys(t_artist, t_song):
                hub_status_by_key[key] = status

    all_sounds = list_sounds(active_only=False)
    deactivated = 0
    already_inactive = 0
    still_active = 0
    matched = 0
    errors: list[str] = []

    # Pass 1: deterministic matching
    unmatched_for_ai: list[dict[str, str]] = []
    sound_map: dict[str, dict] = {}  # sound_id -> sound dict

    for sound in all_sounds:
        label = sound.get("label", "")
        sound_artist, sound_song = _parse_sound_label(label)
        sound_keys = _deterministic_keys(sound_artist, sound_song)

        hub_match: str | None = None
        for key in sound_keys:
            if key in hub_status_by_key:
                hub_match = hub_status_by_key[key]
                break

        if hub_match is not None:
            matched += 1
            if hub_match == "completed":
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
        else:
            # Unmatched — queue for AI pass
            sound_map[sound["id"]] = sound
            unmatched_for_ai.append({"id": sound["id"], "label": label})

    # Pass 2: AI matching for remaining
    ai_unmatched_labels: list[str] = []
    if unmatched_for_ai:
        hub_for_ai = [
            {
                "slug": c.get("slug", ""),
                "title": c.get("title", f"{c.get('artist', '')} - {c.get('song', '')}"),
                "status": c.get("completion_status", "none"),
            }
            for c in all_hub
        ]

        ai_matches = await _ai_match_sounds(unmatched_for_ai, hub_for_ai)

        for item in unmatched_for_ai:
            sound = sound_map[item["id"]]
            ai_slug = ai_matches.get(item["id"])

            if ai_slug and ai_slug in hub_slug_to_status:
                matched += 1
                hub_status = hub_slug_to_status[ai_slug]
                if hub_status == "completed":
                    if sound.get("active", True):
                        try:
                            toggle_sound(sound["id"], False)
                            deactivated += 1
                        except Exception as exc:
                            errors.append(f"{item['label']}: {exc}")
                    else:
                        already_inactive += 1
                else:
                    if sound.get("active", True):
                        still_active += 1
            else:
                if sound.get("active", True):
                    ai_unmatched_labels.append(item["label"])
                    still_active += 1

    return {
        "deactivated": deactivated,
        "already_inactive": already_inactive,
        "still_active": still_active,
        "matched": matched,
        "unmatched": ai_unmatched_labels,
        "completed_campaigns": sum(1 for c in all_hub if c.get("completion_status") == "completed"),
        "errors": errors,
    }


def is_configured() -> bool:
    """Check if Campaign Hub integration is available."""
    return bool(CAMPAIGN_HUB_URL)
