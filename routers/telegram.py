"""
Telegram bot router.
Manages bot lifecycle, staging groups, poster groups, content inventory,
sound library, and scheduled forwarding batches.
"""

import asyncio
import logging
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from services.telegram import (
    add_inventory_item,
    add_sound,
    assign_page_to_poster,
    clear_bot_token,
    get_all_inventory_summary,
    get_bot_token,
    get_inventory,
    get_last_forwarded_id,
    get_pending_inventory,
    get_poster,
    get_poster_for_page,
    get_schedule,
    get_staging_group,
    list_posters,
    list_sounds,
    load_config,
    mark_forwarded,
    remove_inventory_item,
    remove_poster,
    remove_sound,
    remove_staging_topic,
    set_bot_token,
    set_last_forwarded_id,
    set_last_run,
    set_poster,
    set_poster_topic,
    set_schedule,
    set_staging_group,
    set_staging_topic,
    toggle_sound,
    unassign_page_from_poster,
    update_sound,
)
from services.roster import list_all_pages

# Lazy import to avoid circular import at module level.
# telegram_bot imports services.telegram, and this module also imports
# services.telegram — but telegram_bot does NOT import this router,
# so no real cycle exists.  The lazy import just keeps Pyright happy
# when the venv doesn't have aiogram installed.
import telegram_bot as _tg_bot

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Project root for file-path validation ─────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── Helpers ──────────────────────────────────────────────────────────────

def _page_display_name(page: dict) -> str:
    """Derive a human-readable name from a roster page dict."""
    return page.get("name") or page.get("display_name") or page.get("identifier") or page.get("integration_id", "unknown")


# ── Request Models ────────────────────────────────────────────────────────


class BotTokenRequest(BaseModel):
    token: str


class StagingGroupRequest(BaseModel):
    chat_id: int


class PosterCreateRequest(BaseModel):
    name: str
    chat_id: int


class PosterUpdateRequest(BaseModel):
    name: str | None = None
    chat_id: int | None = None


class AssignPagesRequest(BaseModel):
    page_ids: list[str]


class SendRequest(BaseModel):
    integration_id: str
    file_path: str
    caption: str | None = None


class SoundCreateRequest(BaseModel):
    url: str
    label: str


class SoundUpdateRequest(BaseModel):
    url: str | None = None
    label: str | None = None
    active: bool | None = None


class ScheduleUpdateRequest(BaseModel):
    enabled: bool | None = None
    forward_time: str | None = None
    timezone: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────


def _slugify(name: str) -> str:
    """Generate a URL-safe poster ID from a display name."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "poster"


def _require_bot():
    """Raise 503 if the Telegram bot is not running."""
    if _tg_bot.get_bot() is None:
        raise HTTPException(status_code=503, detail="Telegram bot is not running")


def _find_page_name(integration_id: str) -> str:
    """Look up a roster page's display name by integration_id."""
    for p in list_all_pages():
        if p.get("integration_id") == integration_id:
            return _page_display_name(p)
    return integration_id


# ── Config & Status ───────────────────────────────────────────────────────


@router.get("/status")
async def telegram_status():
    """Return overall Telegram integration status."""
    config = load_config()
    bot = _tg_bot.get_bot()
    staging = config.get("staging_group", {})
    return {
        "bot_configured": bool(config.get("bot_token")),
        "bot_running": bot is not None,
        "bot_username": config.get("bot_username"),
        "staging_group": {
            "chat_id": staging.get("chat_id"),
            "name": staging.get("name"),
            "topic_count": len(staging.get("topics", {})),
            "topics": staging.get("topics", {}),
        }
        if staging.get("chat_id")
        else None,
        "poster_count": len(list_posters()),
        "total_inventory": sum(s["total"] for s in get_all_inventory_summary()),
        "schedule": config.get("schedule", {}),
    }


@router.put("/bot-token")
async def update_bot_token(req: BotTokenRequest):
    """Set bot token, stop old bot if running, start new bot."""
    if _tg_bot.get_bot() is not None:
        await _tg_bot.stop_bot()

    set_bot_token(req.token)

    try:
        await _tg_bot.start_bot(req.token)
    except Exception as exc:
        clear_bot_token()
        raise HTTPException(status_code=400, detail=f"Failed to start bot: {exc}")

    config = load_config()
    return {"ok": True, "bot_username": config.get("bot_username")}


@router.delete("/bot-token")
async def delete_bot_token():
    """Stop the bot and clear the stored token."""
    if _tg_bot.get_bot() is not None:
        await _tg_bot.stop_bot()

    clear_bot_token()
    return {"ok": True}


# ── Staging Group ─────────────────────────────────────────────────────────


@router.put("/staging-group")
async def update_staging_group(req: StagingGroupRequest):
    """Set the staging group chat_id and validate it."""
    _require_bot()

    info = await _tg_bot.validate_group(req.chat_id)
    if not info.get("is_admin"):
        raise HTTPException(status_code=400, detail="Bot must be an admin in the group")
    if not info.get("is_forum"):
        raise HTTPException(status_code=400, detail="Group must be a forum (topics enabled)")

    set_staging_group(req.chat_id, name=info.get("title", ""))
    return {"ok": True, "group": info}


@router.get("/staging-group")
async def get_staging_group_info():
    """Return staging group details with topic list and inventory counts."""
    staging = get_staging_group()
    if not staging or not staging.get("chat_id"):
        return {"staging_group": None}

    topics = staging.get("topics", {})
    summary_list = get_all_inventory_summary()
    summary_map = {s["integration_id"]: s for s in summary_list}

    enriched_topics = {}
    for integration_id, topic_info in topics.items():
        s = summary_map.get(integration_id, {})
        enriched_topics[integration_id] = {
            **topic_info,
            "inventory_total": s.get("total", 0),
            "inventory_pending": s.get("pending", 0),
            "inventory_forwarded": s.get("forwarded", 0),
        }

    return {
        "staging_group": {
            "chat_id": staging.get("chat_id"),
            "name": staging.get("name"),
            "topics": enriched_topics,
        }
    }


@router.post("/staging-group/sync-topics")
async def sync_staging_topics():
    """Create forum topics in the staging group for all roster pages."""
    _require_bot()

    staging = get_staging_group()
    if not staging or not staging.get("chat_id"):
        raise HTTPException(status_code=400, detail="Staging group not configured")

    chat_id = staging["chat_id"]
    existing_topics = staging.get("topics", {})
    pages = list_all_pages()

    created = 0
    existing = 0

    for page in pages:
        integration_id = page.get("integration_id", "")
        if not integration_id:
            continue

        if integration_id in existing_topics:
            existing += 1
            continue

        page_name = _page_display_name(page)
        provider = page.get("provider", "")
        topic_name = f"{page_name} ({provider})" if provider else page_name

        for attempt in range(3):
            try:
                topic_id = await _tg_bot.create_forum_topic(chat_id, topic_name)
                set_staging_topic(integration_id, topic_id, topic_name)
                created += 1
                break
            except Exception as exc:
                if attempt < 2 and "retry" in str(exc).lower() or "too many" in str(exc).lower() or "429" in str(exc):
                    await asyncio.sleep(5)  # back off on rate limit
                else:
                    logger.warning("Failed to create topic for %s: %s", integration_id, exc)
                    break

        await asyncio.sleep(2)

    return {"created": created, "existing": existing}


@router.delete("/staging-group/topics/{integration_id}")
async def delete_staging_topic(integration_id: str):
    """Remove a staging topic mapping."""
    remove_staging_topic(integration_id)
    return {"ok": True}


# ── Posters ───────────────────────────────────────────────────────────────


@router.get("/posters")
async def get_posters():
    """List all poster groups."""
    return list_posters()


@router.post("/posters")
async def create_poster(req: PosterCreateRequest):
    """Create a new poster group. Validates the group is a forum with bot admin."""
    _require_bot()

    info = await _tg_bot.validate_group(req.chat_id)
    if not info.get("is_forum"):
        raise HTTPException(status_code=400, detail="Group must be a forum (topics enabled)")
    if not info.get("is_admin"):
        raise HTTPException(status_code=400, detail="Bot must be an admin in the group")

    poster_id = _slugify(req.name)

    # Prevent slug collision — append suffix if ID already taken
    if get_poster(poster_id) is not None:
        base = poster_id
        counter = 2
        while get_poster(f"{base}-{counter}") is not None:
            counter += 1
        poster_id = f"{base}-{counter}"

    set_poster(poster_id, {"name": req.name, "chat_id": req.chat_id})

    return {"ok": True, "poster_id": poster_id, "name": req.name, "chat_id": req.chat_id}


@router.put("/posters/{poster_id}")
async def update_poster(poster_id: str, req: PosterUpdateRequest):
    """Update an existing poster group."""
    poster = get_poster(poster_id)
    if not poster:
        raise HTTPException(status_code=404, detail="Poster not found")

    if req.chat_id is not None and req.chat_id != poster.get("chat_id"):
        _require_bot()
        info = await _tg_bot.validate_group(req.chat_id)
        if not info.get("is_admin") or not info.get("is_forum"):
            raise HTTPException(status_code=400, detail="Group must be a forum with bot as admin")

    set_poster(poster_id, {
        "name": req.name if req.name is not None else poster.get("name"),
        "chat_id": req.chat_id if req.chat_id is not None else poster.get("chat_id"),
    })

    return {"ok": True, "poster_id": poster_id}


@router.delete("/posters/{poster_id}")
async def delete_poster(poster_id: str):
    """Delete a poster group."""
    if not get_poster(poster_id):
        raise HTTPException(status_code=404, detail="Poster not found")
    remove_poster(poster_id)
    return {"ok": True}


@router.post("/posters/{poster_id}/pages")
async def assign_pages(poster_id: str, req: AssignPagesRequest):
    """Assign pages to a poster and auto-create topics in the poster's group."""
    poster = get_poster(poster_id)
    if not poster:
        raise HTTPException(status_code=404, detail="Poster not found")

    chat_id = poster.get("chat_id")
    existing_topics = poster.get("topics", {})
    created_topics = 0
    bot_available = _tg_bot.get_bot() is not None

    for page_id in req.page_ids:
        assign_page_to_poster(poster_id, page_id)

        # Auto-create topic in poster's group if bot is running
        if bot_available and page_id not in existing_topics:
            page_name = _find_page_name(page_id)
            for attempt in range(3):
                try:
                    topic_id = await _tg_bot.create_forum_topic(chat_id, page_name)
                    set_poster_topic(poster_id, page_id, topic_id, page_name)
                    created_topics += 1
                    break
                except Exception as exc:
                    if attempt < 2 and ("retry" in str(exc).lower() or "too many" in str(exc).lower() or "429" in str(exc)):
                        await asyncio.sleep(5)
                    else:
                        logger.warning("Failed to create topic for %s in poster %s: %s", page_id, poster_id, exc)
                        break

            await asyncio.sleep(2)

    # Re-read poster to confirm assignment was saved
    updated_poster = get_poster(poster_id)
    return {
        "ok": True,
        "assigned": len(req.page_ids),
        "topics_created": created_topics,
        "bot_available": bot_available,
        "poster_page_ids": updated_poster.get("page_ids", []) if updated_poster else [],
        "poster_topics": list(updated_poster.get("topics", {}).keys()) if updated_poster else [],
    }


@router.delete("/posters/{poster_id}/pages/{integration_id}")
async def unassign_page(poster_id: str, integration_id: str):
    """Remove a page assignment from a poster and delete the topic in their group."""
    poster = get_poster(poster_id)
    if not poster:
        raise HTTPException(status_code=404, detail="Poster not found")

    # Delete the topic in the poster's Telegram group if it exists
    topic_deleted = False
    poster_topics = poster.get("topics", {})
    topic_info = poster_topics.get(integration_id)
    if topic_info and poster.get("chat_id") and _tg_bot.get_bot() is not None:
        try:
            topic_deleted = await _tg_bot.delete_forum_topic(
                poster["chat_id"], topic_info["topic_id"]
            )
        except Exception as exc:
            logger.warning("Failed to delete topic for %s in poster %s: %s", integration_id, poster_id, exc)

    unassign_page_from_poster(poster_id, integration_id)
    return {"ok": True, "topic_deleted": topic_deleted}


@router.post("/posters/{poster_id}/sync-topics")
async def sync_poster_topics(poster_id: str):
    """Ensure topics exist in a poster's group for all assigned pages."""
    _require_bot()

    poster = get_poster(poster_id)
    if not poster:
        raise HTTPException(status_code=404, detail="Poster not found")

    chat_id = poster.get("chat_id")
    page_ids = poster.get("page_ids", [])
    existing_topics = poster.get("topics", {})

    created = 0
    existing_count = 0

    for page_id in page_ids:
        if page_id in existing_topics:
            existing_count += 1
            continue

        page_name = _find_page_name(page_id)
        for attempt in range(3):
            try:
                topic_id = await _tg_bot.create_forum_topic(chat_id, page_name)
                set_poster_topic(poster_id, page_id, topic_id, page_name)
                created += 1
                break
            except Exception as exc:
                if attempt < 2 and ("retry" in str(exc).lower() or "too many" in str(exc).lower() or "429" in str(exc)):
                    await asyncio.sleep(5)
                else:
                    logger.warning("Failed to create topic for %s in poster %s: %s", page_id, poster_id, exc)
                    break

        await asyncio.sleep(2)

    return {"created": created, "existing": existing_count}


# ── Content & Inventory ───────────────────────────────────────────────────


@router.post("/send")
async def send_content(req: SendRequest):
    """Send a media file to the staging topic for a page."""
    _require_bot()

    file_path = Path(req.file_path).resolve()
    if not file_path.is_relative_to(PROJECT_ROOT):
        raise HTTPException(status_code=400, detail="File path outside project root")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    staging = get_staging_group()
    if not staging or not staging.get("chat_id"):
        raise HTTPException(status_code=400, detail="Staging group not configured")

    topics = staging.get("topics", {})
    topic_info = topics.get(req.integration_id)
    if not topic_info:
        raise HTTPException(status_code=400, detail=f"No staging topic for integration {req.integration_id}")

    chat_id = staging["chat_id"]
    topic_id = topic_info.get("topic_id")

    result = await _tg_bot.send_media_to_topic(
        chat_id=chat_id,
        topic_id=topic_id,
        file_path=str(file_path),
        caption=req.caption,
    )

    item = add_inventory_item(req.integration_id, {
        "message_id": result.get("message_id"),
        "file_id": result.get("file_id"),
        "file_name": file_path.name,
        "media_type": "video" if file_path.suffix.lower() in (".mp4", ".mov", ".avi", ".mkv") else "document",
        "caption": req.caption,
        "source": "api",
    })

    return item


@router.post("/forward/{integration_id}")
async def forward_all_new(integration_id: str):
    """Forward all new messages in a staging topic to the poster's group.

    Uses message ID range scanning — no inventory tracking needed.
    Picks up everything uploaded since the last forward, regardless of
    whether the bot was running when it was uploaded.
    """
    _require_bot()

    poster = get_poster_for_page(integration_id)
    if not poster:
        raise HTTPException(status_code=400, detail=f"No poster assigned for page {integration_id}")

    poster_chat_id = poster.get("chat_id")
    poster_topic_id = poster.get("topics", {}).get(integration_id, {}).get("topic_id")
    if not poster_topic_id:
        raise HTTPException(status_code=400, detail="Poster has no folder for this page — run Set Up Folders first")

    staging = get_staging_group()
    if not staging or not staging.get("chat_id"):
        raise HTTPException(status_code=400, detail="Staging group not configured")

    staging_chat_id = staging["chat_id"]
    staging_topic = staging.get("topics", {}).get(integration_id)
    if not staging_topic:
        raise HTTPException(status_code=400, detail="No staging folder for this page")

    staging_topic_id = staging_topic["topic_id"]
    after_id = get_last_forwarded_id(integration_id)

    result = await _tg_bot.forward_new_messages(
        from_chat_id=staging_chat_id,
        from_topic_id=staging_topic_id,
        to_chat_id=poster_chat_id,
        to_topic_id=poster_topic_id,
        after_message_id=after_id,
    )

    # Update the high-water mark
    if result["last_message_id"] > after_id:
        set_last_forwarded_id(integration_id, result["last_message_id"])

    return {
        "forwarded": result["forwarded"],
        "skipped": result["errors"],
        "poster_id": poster.get("poster_id", ""),
    }


@router.get("/inventory/{integration_id}")
async def get_page_inventory(integration_id: str):
    """Return inventory items for a specific page."""
    return get_inventory(integration_id)


@router.get("/inventory")
async def get_inventory_summary():
    """Return inventory summary for all pages, enriched with page names."""
    summaries = get_all_inventory_summary()
    pages = list_all_pages()

    page_name_map = {
        p.get("integration_id", ""): _page_display_name(p)
        for p in pages
    }

    return [
        {**s, "page_name": page_name_map.get(s.get("integration_id", ""), s.get("integration_id", ""))}
        for s in summaries
    ]


@router.get("/log")
async def get_activity_log(limit: int = Query(default=50, ge=1, le=500)):
    """Return recent inventory items across all pages, sorted by added_at desc."""
    pages = list_all_pages()
    all_items: list[dict] = []

    for page in pages:
        iid = page.get("integration_id", "")
        if not iid:
            continue
        items = get_inventory(iid)
        page_name = _page_display_name(page)
        for item in items:
            all_items.append({**item, "integration_id": iid, "page_name": page_name})

    all_items.sort(key=lambda x: x.get("added_at", ""), reverse=True)
    return all_items[:limit]


# ── Sounds ────────────────────────────────────────────────────────────────


@router.get("/sounds")
async def get_sounds(active_only: bool = Query(default=True)):
    """List sounds, optionally filtering to active-only."""
    return list_sounds(active_only=active_only)


@router.post("/sounds")
async def create_sound(req: SoundCreateRequest):
    """Add a new sound to the library."""
    return add_sound(url=req.url, label=req.label)


@router.delete("/sounds/{sound_id}")
async def delete_sound(sound_id: str):
    """Remove a sound from the library."""
    if not remove_sound(sound_id):
        raise HTTPException(status_code=404, detail="Sound not found")
    return {"ok": True}


@router.put("/sounds/{sound_id}")
async def update_sound_endpoint(sound_id: str, req: SoundUpdateRequest):
    """Update a sound's active status, url, or label."""
    if req.active is not None:
        if not toggle_sound(sound_id, req.active):
            raise HTTPException(status_code=404, detail="Sound not found")

    updates: dict = {}
    if req.url is not None:
        updates["url"] = req.url
    if req.label is not None:
        updates["label"] = req.label

    if updates:
        if not update_sound(sound_id, **updates):
            raise HTTPException(status_code=404, detail="Sound not found")

    return {"ok": True, "sound_id": sound_id}


# ── Schedule & Batch ──────────────────────────────────────────────────────


@router.get("/schedule")
async def get_schedule_config():
    """Return the current forwarding schedule configuration."""
    return get_schedule()


@router.put("/schedule")
async def update_schedule(req: ScheduleUpdateRequest):
    """Update the forwarding schedule configuration."""
    updates: dict = {}
    if req.enabled is not None:
        updates["enabled"] = req.enabled
    if req.forward_time is not None:
        updates["forward_time"] = req.forward_time
    if req.timezone is not None:
        updates["timezone"] = req.timezone

    set_schedule(**updates)
    return {"ok": True, "schedule": get_schedule()}


@router.post("/batch/run")
async def trigger_batch_run():
    """Manually trigger a daily batch forward run."""
    _require_bot()
    result = await _tg_bot.run_daily_batch()
    return result
