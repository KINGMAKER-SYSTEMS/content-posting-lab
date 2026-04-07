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
    clear_all_sounds,
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
    set_poster_sounds_topic,
    unassign_page_from_poster,
    update_sound,
)
from services.roster import list_all_pages
from services.notion import sync_sounds_from_notion, is_configured as notion_configured
from services.campaign_hub import sync_sound_status as hub_sync_sound_status, is_configured as hub_configured

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
    page_names: dict[str, str] | None = None  # {integration_id: display_name} — optional hint for topic naming


class SendRequest(BaseModel):
    integration_id: str
    file_path: str
    caption: str | None = None


class SendBatchRequest(BaseModel):
    integration_id: str
    batch_id: str
    project: str


class AssignBatchRequest(BaseModel):
    batch_id: str
    project: str
    integration_ids: list[str]


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
    inventory_summary = get_all_inventory_summary()

    # Enrich topics with inventory counts so frontend can show what's fed
    enriched_topics = {}
    summary_by_id = {s["integration_id"]: s for s in inventory_summary}
    for iid, topic_info in staging.get("topics", {}).items():
        inv = summary_by_id.get(iid, {})
        enriched_topics[iid] = {
            **topic_info,
            "inventory_total": inv.get("total", 0),
            "inventory_pending": inv.get("pending", 0),
            "inventory_forwarded": inv.get("forwarded", 0),
        }

    return {
        "bot_configured": bool(get_bot_token()),
        "bot_running": bot is not None,
        "bot_username": config.get("bot_username"),
        "staging_group": {
            "chat_id": staging.get("chat_id"),
            "name": staging.get("name"),
            "topic_count": len(staging.get("topics", {})),
            "topics": enriched_topics,
        }
        if staging.get("chat_id")
        else None,
        "poster_count": len(list_posters()),
        "total_inventory": sum(s["total"] for s in inventory_summary),
        "schedule": config.get("schedule", {}),
        "notion_configured": notion_configured(),
        "campaign_hub_configured": hub_configured(),
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
    """Create forum topics in the staging group for roster pages that don't have one yet.

    APPEND-ONLY: Never overwrites existing topic mappings. If a page already has
    a topic_id in the config, it is skipped — even if the topic_id is stale.
    To remap a topic, delete it first via DELETE /staging-group/topics/{integration_id}.
    """
    _require_bot()

    staging = get_staging_group()
    if not staging or not staging.get("chat_id"):
        raise HTTPException(status_code=400, detail="Staging group not configured")

    chat_id = staging["chat_id"]
    # Re-read config each iteration to see topics created by earlier iterations
    pages = list_all_pages()

    created = 0
    existing = 0
    skipped_names: list[str] = []

    # Track which page names already have topics (to skip duplicate names)
    seen_names: set[str] = set()

    for page in pages:
        integration_id = page.get("integration_id", "")
        if not integration_id:
            continue

        # Re-read fresh config to catch topics just created in this loop
        fresh_staging = get_staging_group()
        fresh_topics = fresh_staging.get("topics", {})

        if integration_id in fresh_topics:
            existing += 1
            # Track the name so we skip duplicates
            seen_names.add(_page_display_name(page).lower().strip())
            continue

        page_name = _page_display_name(page)

        # Skip if another integration with the same name already has a topic
        if page_name.lower().strip() in seen_names:
            existing += 1
            continue
        seen_names.add(page_name.lower().strip())

        provider = page.get("provider", "")
        topic_name = f"{page_name} ({provider})" if provider else page_name

        for attempt in range(3):
            try:
                topic_id = await _tg_bot.create_forum_topic(chat_id, topic_name)
                set_staging_topic(integration_id, topic_id, topic_name)
                created += 1
                break
            except Exception as exc:
                err_str = str(exc).lower()
                if attempt < 2 and ("retry" in err_str or "too many" in err_str or "429" in err_str):
                    await asyncio.sleep(5)
                else:
                    logger.warning("Failed to create topic for %s: %s", integration_id, exc)
                    skipped_names.append(page_name)
                    break

        await asyncio.sleep(1.5)

    return {"created": created, "existing": existing, "failed": skipped_names}


# Background scan job state
_scan_job: dict | None = None


async def _run_scan_job(chat_id: int, seen_topic_ids: dict[int, str], topics: dict) -> None:
    """Background task that scans all topics and updates _scan_job state."""
    global _scan_job
    results = []
    total_found = 0
    total_topics = len(seen_topic_ids)

    for idx, (topic_id, integration_id) in enumerate(seen_topic_ids.items()):
        topic_name = topics[integration_id].get("topic_name", str(topic_id))
        _scan_job = {
            "status": "running",
            "progress": f"{idx}/{total_topics}",
            "current_topic": topic_name,
            "scanned_topics": idx,
            "total_topics": total_topics,
            "total_found": total_found,
            "results": results,
        }
        try:
            r = await _tg_bot.scan_topic_inventory(
                chat_id=int(chat_id),
                topic_id=topic_id,
                integration_id=integration_id,
            )
            total_found += r["found"]
            results.append({
                "integration_id": integration_id,
                "topic_name": topic_name,
                **r,
            })
        except Exception as exc:
            logger.warning("Scan failed for topic %s (%s): %s", topic_name, integration_id, exc)
            results.append({
                "integration_id": integration_id,
                "topic_name": topic_name,
                "error": str(exc)[:200],
            })

        await asyncio.sleep(1)

    _scan_job = {
        "status": "done",
        "scanned_topics": len(results),
        "total_topics": total_topics,
        "total_found": total_found,
        "results": results,
    }


@router.post("/staging-group/scan-inventory")
async def scan_all_inventory():
    """Start a background scan of ALL staging topics for existing media.

    Returns immediately. Poll GET /staging-group/scan-inventory for progress.
    """
    global _scan_job
    _require_bot()

    # If already running, don't start another
    if _scan_job and _scan_job.get("status") == "running":
        return {"status": "already_running", **_scan_job}

    staging = get_staging_group()
    if not staging or not staging.get("chat_id"):
        raise HTTPException(status_code=400, detail="Staging group not configured")

    chat_id = staging["chat_id"]
    topics = staging.get("topics", {})

    # Deduplicate by topic_id
    seen_topic_ids: dict[int, str] = {}
    for int_id, info in topics.items():
        tid = info.get("topic_id") if isinstance(info, dict) else None
        if tid and tid not in seen_topic_ids:
            seen_topic_ids[tid] = int_id

    _scan_job = {"status": "starting", "total_topics": len(seen_topic_ids), "total_found": 0}
    asyncio.create_task(_run_scan_job(chat_id, seen_topic_ids, topics))

    return {"status": "started", "total_topics": len(seen_topic_ids)}


@router.get("/staging-group/scan-inventory")
async def get_scan_status():
    """Poll the background scan job progress."""
    if _scan_job is None:
        return {"status": "idle"}
    return _scan_job


@router.post("/staging-group/scan-inventory/{integration_id}")
async def scan_single_inventory(integration_id: str):
    """Scan a single staging topic for existing media and backfill inventory."""
    _require_bot()

    staging = get_staging_group()
    if not staging or not staging.get("chat_id"):
        raise HTTPException(status_code=400, detail="Staging group not configured")

    chat_id = staging["chat_id"]
    topics = staging.get("topics", {})
    topic_info = topics.get(integration_id)
    if not topic_info:
        raise HTTPException(status_code=404, detail=f"No topic for integration {integration_id}")

    topic_id = topic_info.get("topic_id")
    topic_name = topic_info.get("topic_name", str(topic_id))

    r = await _tg_bot.scan_topic_inventory(
        chat_id=int(chat_id),
        topic_id=topic_id,
        integration_id=integration_id,
    )

    return {
        "integration_id": integration_id,
        "topic_name": topic_name,
        **r,
    }


@router.delete("/staging-group/topics/{integration_id}")
async def delete_staging_topic(integration_id: str):
    """Remove a staging topic mapping."""
    remove_staging_topic(integration_id)
    return {"ok": True}


@router.put("/staging-group/topics")
async def bulk_restore_staging_topics(body: dict):
    """Bulk restore topic mappings without creating new Telegram topics.

    Body: {"topics": {"integration_id": {"topic_id": 1234, "topic_name": "..."}}}
    Used to restore mappings after config loss. Does NOT call Telegram API.
    """
    topics = body.get("topics", {})
    restored = 0
    for iid, info in topics.items():
        tid = info.get("topic_id")
        tname = info.get("topic_name", "")
        if tid:
            set_staging_topic(iid, int(tid), tname, force=True)
            restored += 1
    return {"ok": True, "restored": restored}


# ── Posters ───────────────────────────────────────────────────────────────


@router.get("/posters")
async def get_posters():
    """List all poster groups."""
    return list_posters()


@router.post("/posters")
async def create_poster(req: PosterCreateRequest):
    """Create a new poster group. Validates the group is a forum with bot admin."""
    _require_bot()

    try:
        info = await _tg_bot.validate_group(req.chat_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Can't reach group {req.chat_id}: {exc}")
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


@router.post("/posters/reset-defaults")
async def reset_default_posters():
    """Re-seed the default posters (Seffra, Gigi, etc.) without wiping custom ones."""
    from services.telegram import _DEFAULT_POSTERS, _now
    config = load_config()
    added = 0
    for pid, pdata in _DEFAULT_POSTERS.items():
        if pid not in config.get("posters", {}):
            now = _now()
            config.setdefault("posters", {})[pid] = {
                **pdata,
                "page_ids": [],
                "topics": {},
                "added_at": now,
                "updated_at": now,
            }
            added += 1
    if added > 0:
        from services.telegram import save_config
        save_config(config)
    return {"ok": True, "added": added, "total": len(config.get("posters", {}))}


@router.post("/posters/{poster_id}/pages")
async def assign_pages(poster_id: str, req: AssignPagesRequest):
    """Assign pages to a poster and auto-create topics in the poster's group.

    Page assignment is saved immediately. Topic creation runs in the background
    to avoid Railway's ~30s request timeout when assigning many pages.
    """
    poster = get_poster(poster_id)
    if not poster:
        raise HTTPException(status_code=404, detail="Poster not found")

    chat_id = poster.get("chat_id")
    existing_topics = poster.get("topics", {})
    bot_available = _tg_bot.get_bot() is not None

    # Save all page assignments immediately (fast, no network calls)
    for page_id in req.page_ids:
        assign_page_to_poster(poster_id, page_id)

    # Topics are NOT auto-created here to avoid duplicates and race conditions.
    # Use "Set Up Folders" (POST /posters/{id}/sync-topics) after assigning pages.

    updated_poster = get_poster(poster_id)
    return {
        "ok": True,
        "assigned": len(req.page_ids),
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
    """Ensure topics exist in a poster's group for all assigned pages.

    APPEND-ONLY: Never overwrites existing topic mappings.
    """
    _require_bot()

    poster = get_poster(poster_id)
    if not poster:
        raise HTTPException(status_code=404, detail="Poster not found")

    chat_id = poster.get("chat_id")
    page_ids = poster.get("page_ids", [])

    created = 0
    existing_count = 0
    errors: list[str] = []

    logger.info("sync-topics for %s: chat_id=%s, %d page_ids", poster_id, chat_id, len(page_ids))

    for page_id in page_ids:
        # Re-read fresh each iteration to see topics just created
        fresh_poster = get_poster(poster_id) or {}
        fresh_topics = fresh_poster.get("topics", {})

        if page_id in fresh_topics:
            existing_count += 1
            continue

        page_name = _find_page_name(page_id)
        logger.info("  creating topic for %s (%s) in %s", page_id[:12], page_name, poster_id)

        success = False
        for attempt in range(3):
            try:
                topic_id = await _tg_bot.create_forum_topic(chat_id, page_name)
                set_poster_topic(poster_id, page_id, topic_id, page_name)
                created += 1
                success = True
                logger.info("  → created topic_id=%s for %s", topic_id, page_name)
                break
            except Exception as exc:
                err_str = str(exc).lower()
                logger.warning("  → attempt %d failed: %s", attempt + 1, exc)
                if attempt < 2 and ("retry" in err_str or "too many" in err_str or "429" in err_str):
                    await asyncio.sleep(5)
                else:
                    errors.append(f"{page_name}: {exc}")
                    break

        await asyncio.sleep(1.5)

    # Ensure a "Sounds" topic exists for sound link forwarding
    sounds_topic_created = False
    fresh_poster = get_poster(poster_id) or {}
    if not fresh_poster.get("sounds_topic_id"):
        for attempt in range(3):
            try:
                sounds_tid = await _tg_bot.create_forum_topic(chat_id, "Campaign Sounds")
                set_poster_sounds_topic(poster_id, sounds_tid)
                sounds_topic_created = True
                logger.info("  → created Sounds topic_id=%s for %s", sounds_tid, poster_id)
                break
            except Exception as exc:
                err_str = str(exc).lower()
                if attempt < 2 and ("retry" in err_str or "too many" in err_str or "429" in err_str):
                    await asyncio.sleep(5)
                else:
                    errors.append(f"Sounds topic: {exc}")
                    break

    return {
        "created": created,
        "existing": existing_count,
        "sounds_topic_created": sounds_topic_created,
        "errors": errors,
        "total_pages": len(page_ids),
    }


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


@router.post("/send-batch")
async def send_batch(req: SendBatchRequest):
    """Send all burned MP4s from a batch to a staging topic.

    Used by the Burn tab to send completed burn batches to Telegram.
    """
    _require_bot()

    from project_manager import get_project_burn_dir, sanitize_project_name

    try:
        burn_dir = get_project_burn_dir(req.project)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    batch_dir = burn_dir / req.batch_id
    if not batch_dir.exists() or not batch_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Batch '{req.batch_id}' not found")

    mp4s = sorted(batch_dir.glob("burned_*.mp4"))
    if not mp4s:
        raise HTTPException(status_code=404, detail="No burned videos in batch")

    staging = get_staging_group()
    if not staging or not staging.get("chat_id"):
        raise HTTPException(status_code=400, detail="Staging group not configured")

    topics = staging.get("topics", {})
    topic_info = topics.get(req.integration_id)
    if not topic_info:
        raise HTTPException(status_code=400, detail=f"No staging topic for integration {req.integration_id}")

    chat_id = staging["chat_id"]
    topic_id = topic_info.get("topic_id")

    sent = 0
    errors: list[str] = []
    for mp4 in mp4s:
        try:
            result = await _tg_bot.send_media_to_topic(
                chat_id=chat_id,
                topic_id=topic_id,
                file_path=str(mp4),
                caption=None,
            )
            add_inventory_item(req.integration_id, {
                "message_id": result.get("message_id"),
                "file_id": result.get("file_id"),
                "file_name": mp4.name,
                "media_type": "video",
                "caption": None,
                "source": "api",
            })
            sent += 1
            # Rate limit between sends
            await asyncio.sleep(0.3)
        except Exception as exc:
            logger.warning("Failed to send %s: %s", mp4.name, exc)
            errors.append(f"{mp4.name}: {exc}")

    return {
        "sent": sent,
        "total": len(mp4s),
        "errors": errors,
        "batch_id": req.batch_id,
    }


# Double-send guard for assign-batch
_active_assign_batches: set[str] = set()


@router.post("/assign-batch")
async def assign_batch(req: AssignBatchRequest):
    """Round-robin split burned videos across pages and send to staging topics.

    Takes a batch of burned videos and distributes them evenly across the
    specified pages, sending each page's share to its staging topic.
    """
    _require_bot()

    # Double-send guard
    batch_key = f"{req.project}:{req.batch_id}"
    if batch_key in _active_assign_batches:
        raise HTTPException(status_code=409, detail="This batch is already being assigned")
    _active_assign_batches.add(batch_key)

    try:
        from project_manager import get_project_burn_dir

        try:
            burn_dir = get_project_burn_dir(req.project)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        batch_dir = burn_dir / req.batch_id
        if not batch_dir.exists() or not batch_dir.is_dir():
            raise HTTPException(status_code=404, detail=f"Batch '{req.batch_id}' not found")

        mp4s = sorted(batch_dir.glob("burned_*.mp4"))
        if not mp4s:
            raise HTTPException(status_code=404, detail="No burned videos in batch")

        if not req.integration_ids:
            raise HTTPException(status_code=400, detail="No pages selected")

        # Validate all pages have staging topics
        staging = get_staging_group()
        if not staging or not staging.get("chat_id"):
            raise HTTPException(status_code=400, detail="Staging group not configured")

        chat_id = staging["chat_id"]
        topics = staging.get("topics", {})

        valid_pages: list[dict] = []
        for iid in req.integration_ids:
            topic_info = topics.get(iid)
            if not topic_info or not topic_info.get("topic_id"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Page {iid} has no staging topic. Run 'Set Up Folders' first.",
                )
            page_name = _find_page_name(iid)
            valid_pages.append({
                "integration_id": iid,
                "topic_id": topic_info["topic_id"],
                "page_name": page_name,
            })

        # Round-robin assignment
        assignments: dict[str, list[Path]] = {p["integration_id"]: [] for p in valid_pages}
        for idx, mp4 in enumerate(mp4s):
            page = valid_pages[idx % len(valid_pages)]
            assignments[page["integration_id"]].append(mp4)

        # Send each page's files to its staging topic
        results: list[dict] = []
        for page in valid_pages:
            iid = page["integration_id"]
            page_files = assignments[iid]
            sent = 0
            errors: list[str] = []

            for mp4 in page_files:
                try:
                    result = await _tg_bot.send_media_to_topic(
                        chat_id=chat_id,
                        topic_id=page["topic_id"],
                        file_path=str(mp4),
                        caption=None,
                    )
                    add_inventory_item(iid, {
                        "message_id": result.get("message_id"),
                        "file_id": result.get("file_id"),
                        "file_name": mp4.name,
                        "media_type": "video",
                        "caption": None,
                        "source": "assign-batch",
                    })
                    sent += 1
                    await asyncio.sleep(0.3)
                except Exception as exc:
                    logger.warning("assign-batch send fail %s→%s: %s", mp4.name, iid, exc)
                    errors.append(f"{mp4.name}: {exc}")

            results.append({
                "integration_id": iid,
                "page_name": page["page_name"],
                "files": [f.name for f in page_files],
                "sent": sent,
                "errors": errors,
            })

        return {
            "total": len(mp4s),
            "pages_used": len(valid_pages),
            "assignments": results,
            "batch_id": req.batch_id,
        }

    finally:
        _active_assign_batches.discard(batch_key)


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


@router.delete("/sounds/all")
async def wipe_all_sounds():
    """Remove ALL sounds from the library. Used to reset before a clean re-sync."""
    count = clear_all_sounds()
    return {"ok": True, "removed": count}


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


@router.post("/sounds/sync-notion")
async def sync_sounds_from_notion_endpoint():
    """Legacy endpoint — redirects to unified sync."""
    return await sync_sounds_unified()


@router.post("/sounds/sync-hub")
async def sync_sounds_from_hub_endpoint():
    """Legacy endpoint — redirects to unified sync."""
    return await sync_sounds_unified()


@router.post("/sounds/sync")
async def sync_sounds_unified():
    """Unified sound sync: Campaign Hub (active campaigns) + Notion (sound links).

    Campaign Hub is source of truth for what's active.
    Notion CRM provides the TikTok Sound Links.
    AI-assisted fuzzy matching bridges naming differences.
    """
    if not hub_configured():
        raise HTTPException(
            status_code=400,
            detail="Campaign Hub URL not configured.",
        )

    # Fetch Notion data if available (optional — sync still works without it)
    notion_data = None
    if notion_configured():
        try:
            from services.notion import fetch_campaigns_with_sounds
            notion_data = await fetch_campaigns_with_sounds()
        except Exception as exc:
            logger.warning("Notion fetch failed (continuing with Hub only): %s", exc)

    try:
        result = await hub_sync_sound_status(notion_campaigns=notion_data)
    except Exception as exc:
        logger.error("Sound sync failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Sound sync failed: {exc}")

    return result


async def _ensure_sounds_topic(poster_id: str, poster: dict) -> int | None:
    """Create the Campaign Sounds topic in a poster's group if it doesn't exist.

    Returns the topic_id (existing or newly created), or None on failure.
    """
    sounds_topic_id = poster.get("sounds_topic_id")
    if sounds_topic_id:
        return int(sounds_topic_id)

    chat_id = poster.get("chat_id")
    if not chat_id:
        return None

    try:
        tid = await _tg_bot.create_forum_topic(int(chat_id), "Campaign Sounds")
        set_poster_sounds_topic(poster_id, tid)
        logger.info("Auto-created Campaign Sounds topic_id=%s for %s", tid, poster_id)
        return tid
    except Exception as exc:
        logger.warning("Failed to create Campaign Sounds topic for %s: %s", poster_id, exc)
        return None


@router.post("/sounds/forward/{poster_id}")
async def forward_sounds_to_poster(poster_id: str):
    """Send all active sound links to a poster's Sounds topic.

    Auto-creates the Campaign Sounds topic if it doesn't exist yet.
    """
    _require_bot()

    poster = get_poster(poster_id)
    if not poster:
        raise HTTPException(status_code=404, detail="Poster not found")

    poster_chat_id = poster.get("chat_id")
    if not poster_chat_id:
        raise HTTPException(status_code=400, detail="Poster has no chat_id")

    sounds_topic_id = await _ensure_sounds_topic(poster_id, poster)
    if not sounds_topic_id:
        raise HTTPException(status_code=502, detail="Failed to create Campaign Sounds topic")

    sounds = list_sounds(active_only=True)
    if not sounds:
        return {"ok": True, "sent": 0, "message": "No active sounds to send"}

    from datetime import datetime

    today = datetime.now().strftime("%B %d, %Y")
    msg_parts = [f"\U0001f3b5 Active Sounds \u2014 {today}", ""]
    for sound in sounds:
        label = sound.get("label", sound.get("name", "Sound"))
        url = sound.get("url", "")
        msg_parts.append(f"\u2022 {label}")
        msg_parts.append(f"  {url}")
        msg_parts.append("")

    try:
        await _tg_bot.send_text_to_topic(
            chat_id=int(poster_chat_id),
            topic_id=sounds_topic_id,
            text="\n".join(msg_parts),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to send sounds: {exc}")

    return {"ok": True, "sent": len(sounds), "poster_id": poster_id}


@router.post("/sounds/forward-all")
async def forward_sounds_to_all_posters():
    """Send all active sound links to every poster's Sounds topic.

    Auto-creates Campaign Sounds topics for posters that don't have one yet.
    """
    _require_bot()

    sounds = list_sounds(active_only=True)
    if not sounds:
        return {"ok": True, "sent_to": 0, "sound_count": 0, "errors": []}

    from datetime import datetime

    today = datetime.now().strftime("%B %d, %Y")
    msg_parts = [f"\U0001f3b5 Active Sounds \u2014 {today}", ""]
    for sound in sounds:
        label = sound.get("label", sound.get("name", "Sound"))
        url = sound.get("url", "")
        msg_parts.append(f"\u2022 {label}")
        msg_parts.append(f"  {url}")
        msg_parts.append("")

    text = "\n".join(msg_parts)
    posters = list_posters()
    sent_to = 0
    topics_created = 0
    errors: list[str] = []

    for poster in posters:
        poster_id = poster.get("poster_id", "")
        poster_chat_id = poster.get("chat_id")
        if not poster_chat_id:
            continue

        # Auto-create Campaign Sounds topic if needed
        sounds_topic_id = await _ensure_sounds_topic(poster_id, poster)
        if not sounds_topic_id:
            errors.append(f"{poster.get('name', poster_id)}: failed to create topic")
            continue

        if not poster.get("sounds_topic_id"):
            topics_created += 1

        try:
            await _tg_bot.send_text_to_topic(
                chat_id=int(poster_chat_id),
                topic_id=sounds_topic_id,
                text=text,
            )
            sent_to += 1
        except Exception as exc:
            errors.append(f"{poster.get('name', poster_id)}: {exc}")

        # Small delay between posters to avoid rate limits
        await asyncio.sleep(0.5)

    return {
        "ok": True,
        "sent_to": sent_to,
        "topics_created": topics_created,
        "sound_count": len(sounds),
        "total_posters": len(posters),
        "errors": errors,
    }


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
