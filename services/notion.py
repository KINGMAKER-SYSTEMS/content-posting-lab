"""
Notion CRM integration for campaign sound sync.
Queries the campaigns database for entries with TikTok Sound Links
and syncs them into the Telegram sound library.
"""

import os
from typing import Any

import httpx

from services.telegram import add_sound, list_sounds

NOTION_API_KEY = os.getenv("NOTION_API_KEY", "")
NOTION_CAMPAIGNS_DB = os.getenv("NOTION_CAMPAIGNS_DB", "")
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _extract_title(prop: dict) -> str:
    """Extract plain text from a Notion title property."""
    title_list = prop.get("title", [])
    return title_list[0].get("plain_text", "") if title_list else ""


def _extract_rich_text(prop: dict) -> str:
    """Extract plain text from a Notion rich_text property."""
    rt_list = prop.get("rich_text", [])
    return rt_list[0].get("plain_text", "") if rt_list else ""


def _extract_url(prop: dict) -> str | None:
    """Extract URL from a Notion url property."""
    return prop.get("url")


def _extract_status(prop: dict) -> str:
    """Extract status name from a Notion status property."""
    status = prop.get("status")
    return status.get("name", "") if isinstance(status, dict) else ""


def _build_sound_label(artist: str, song: str) -> str:
    """Build a human-readable sound label from artist and song name."""
    artist = artist.strip()
    song = song.strip()
    if artist and song:
        return f"{artist} - {song}"
    return artist or song or "Unknown Campaign"


def _parse_campaign(page: dict) -> dict[str, Any] | None:
    """Parse a Notion page into a campaign sound record.

    Returns None if the page has no TikTok Sound Link.
    """
    props = page.get("properties", {})

    tiktok_url = _extract_url(props.get("TikTok Sound Link", {}))
    if not tiktok_url:
        return None

    artist = _extract_title(props.get("Artist Name", {}))
    song = _extract_rich_text(props.get("Song Name", {}))
    insta_url = _extract_url(props.get("Insta Sound Link", {}))
    campaign_stage = _extract_status(props.get("Campaign Stage", {}))
    pipeline_status = _extract_status(props.get("Pipeline Status", {}))

    return {
        "notion_page_id": page.get("id", ""),
        "artist": artist,
        "song": song,
        "label": _build_sound_label(artist, song),
        "tiktok_url": tiktok_url,
        "insta_url": insta_url,
        "campaign_stage": campaign_stage,
        "pipeline_status": pipeline_status,
    }


async def fetch_campaigns_with_sounds() -> list[dict[str, Any]]:
    """Query Notion for all campaigns that have a TikTok Sound Link.

    Paginates through all results. Returns parsed campaign records.
    """
    if not NOTION_API_KEY or not NOTION_CAMPAIGNS_DB:
        raise RuntimeError(
            "NOTION_API_KEY and NOTION_CAMPAIGNS_DB must be set in environment"
        )

    campaigns: list[dict[str, Any]] = []
    has_more = True
    start_cursor: str | None = None

    async with httpx.AsyncClient(timeout=30) as client:
        while has_more:
            body: dict[str, Any] = {
                "filter": {
                    "property": "TikTok Sound Link",
                    "url": {"is_not_empty": True},
                },
                "page_size": 100,
            }
            if start_cursor:
                body["start_cursor"] = start_cursor

            resp = await client.post(
                f"{NOTION_API_BASE}/databases/{NOTION_CAMPAIGNS_DB}/query",
                headers=_headers(),
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

            for page in data.get("results", []):
                parsed = _parse_campaign(page)
                if parsed:
                    campaigns.append(parsed)

            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")

    return campaigns


async def sync_sounds_from_notion() -> dict[str, Any]:
    """Sync TikTok sound links from Notion into the Telegram sound library.

    Deduplicates by URL — existing sounds with the same URL are skipped.
    New sounds are added as active.

    Returns {added: int, skipped: int, total_in_notion: int, errors: list[str]}.
    """
    campaigns = await fetch_campaigns_with_sounds()

    # Build set of existing sound URLs for dedup
    existing_sounds = list_sounds(active_only=False)
    existing_urls = {s.get("url", "").rstrip("/").lower() for s in existing_sounds}

    added = 0
    skipped = 0
    errors: list[str] = []

    for campaign in campaigns:
        url = campaign["tiktok_url"].rstrip("/").lower()
        if url in existing_urls:
            skipped += 1
            continue

        try:
            add_sound(url=campaign["tiktok_url"], label=campaign["label"])
            existing_urls.add(url)
            added += 1
        except Exception as exc:
            errors.append(f"{campaign['label']}: {exc}")

    return {
        "added": added,
        "skipped": skipped,
        "total_in_notion": len(campaigns),
        "errors": errors,
    }


def is_configured() -> bool:
    """Check if Notion integration is configured."""
    return bool(NOTION_API_KEY) and bool(NOTION_CAMPAIGNS_DB)
