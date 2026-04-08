"""
Telegram bot worker for Content Posting Lab.

Runs alongside FastAPI as asyncio tasks in the same event loop.
Handles media inventory from staging group topics and daily batch forwarding.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile

# Pyrogram MTProto client — used for forum topic listing and message search
# (capabilities the Bot API doesn't support)
try:
    from pyrogram import Client as PyroClient
    from pyrogram import enums as pyro_enums
    PYROGRAM_AVAILABLE = True
except ImportError:
    PYROGRAM_AVAILABLE = False

logger = logging.getLogger(__name__)

from services.telegram import (
    load_config,
    save_config,
    get_staging_group,
    add_inventory_item,
    get_inventory,
    get_pending_inventory,
    mark_forwarded,
    list_posters,
    get_poster,
    list_sounds,
    get_schedule,
    set_last_run,
)
from services.roster import list_all_pages

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_bot: Bot | None = None
_dp: Dispatcher | None = None
_poll_task: asyncio.Task | None = None
_schedule_task: asyncio.Task | None = None
_notion_sync_task: asyncio.Task | None = None
_pyro: "PyroClient | None" = None  # Pyrogram MTProto client

# How often to poll Notion for new sounds (seconds)
NOTION_SYNC_INTERVAL = int(os.getenv("NOTION_SYNC_INTERVAL", "900"))  # 15 min default

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def _start_pyrogram(token: str) -> None:
    """Start Pyrogram MTProto client for forum topic & search APIs.

    Uses a user session string (TELEGRAM_SESSION_STRING) because
    get_forum_topics and search_messages with message_thread_id are
    user-only methods — bots get BOT_METHOD_INVALID.

    Generate a session string once locally with:
        python generate_session.py
    Then set TELEGRAM_SESSION_STRING env var on Railway.
    """
    global _pyro
    if not PYROGRAM_AVAILABLE:
        logger.info("pyrogram not installed — discover/scan will use fallback")
        return

    api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    session_string = os.getenv("TELEGRAM_SESSION_STRING", "").strip()

    if not api_id or not api_hash:
        logger.info("TELEGRAM_API_ID / TELEGRAM_API_HASH not set — pyrogram disabled")
        return

    if not session_string:
        logger.info("TELEGRAM_SESSION_STRING not set — pyrogram disabled (run generate_session.py)")
        print("  pyrogram disabled: set TELEGRAM_SESSION_STRING env var", flush=True)
        return

    try:
        _pyro = PyroClient(
            name="cpl_user",
            api_id=int(api_id),
            api_hash=api_hash,
            session_string=session_string,
            no_updates=True,  # we only use it for API calls, not receiving updates
        )
        await _pyro.start()
        me = await _pyro.get_me()
        name = me.first_name or me.username or "user"
        logger.info("pyrogram started as %s (user MTProto)", name)
        print(f"  pyrogram MTProto client started as {name}", flush=True)
    except Exception as exc:
        logger.warning("pyrogram failed to start: %s", exc)
        print(f"  pyrogram failed: {exc}", flush=True)
        _pyro = None


async def _stop_pyrogram() -> None:
    """Stop the Pyrogram client."""
    global _pyro
    if _pyro is not None:
        try:
            await _pyro.stop()
        except Exception:
            pass
        _pyro = None


async def start_bot(token: str) -> None:
    """Start the Telegram bot and background scheduler."""
    global _bot, _dp, _poll_task, _schedule_task, _notion_sync_task

    await stop_bot()

    _bot = Bot(token=token)

    me = await _bot.get_me()
    config = load_config()
    config["bot_username"] = me.username
    save_config(config)

    _dp = Dispatcher()
    _dp.include_router(_build_router())

    _poll_task = asyncio.create_task(_dp.start_polling(_bot))
    _schedule_task = asyncio.create_task(_run_scheduler())
    _notion_sync_task = asyncio.create_task(_run_notion_sync())

    # Start Pyrogram MTProto client alongside aiogram
    await _start_pyrogram(token)

    print(f"  telegram bot started as @{me.username}", flush=True)


async def stop_bot() -> None:
    """Gracefully stop the bot, scheduler, and clean up resources."""
    global _bot, _dp, _poll_task, _schedule_task, _notion_sync_task

    if _notion_sync_task is not None:
        _notion_sync_task.cancel()
        try:
            await _notion_sync_task
        except asyncio.CancelledError:
            pass
        _notion_sync_task = None

    if _schedule_task is not None:
        _schedule_task.cancel()
        try:
            await _schedule_task
        except asyncio.CancelledError:
            pass
        _schedule_task = None

    if _dp is not None:
        await _dp.stop_polling()
        _dp = None

    if _bot is not None:
        await _bot.session.close()
        _bot = None

    if _poll_task is not None:
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass
        _poll_task = None

    await _stop_pyrogram()


def get_bot() -> Bot | None:
    """Return the active Bot instance or None."""
    return _bot


def get_pyro():
    """Return the active Pyrogram client or None."""
    return _pyro


# ---------------------------------------------------------------------------
# Router builder
# ---------------------------------------------------------------------------


def _build_router() -> Router:
    """Create the aiogram Router with all message handlers."""
    router = Router()

    @router.message(Command("start"))
    async def _cmd_start(message: Message) -> None:
        await message.answer("Content Posting Lab Bot is running.")

    @router.message(Command("status"))
    async def _cmd_status(message: Message) -> None:
        staging = get_staging_group()
        posters = list_posters()

        lines = ["Content Posting Lab Bot"]
        if staging:
            lines.append(f"Staging group: {staging.get('chat_id', 'not set')}")
            topic_count = len(staging.get("topics", {}))
            lines.append(f"Topics mapped: {topic_count}")
        else:
            lines.append("Staging group: not configured")

        lines.append(f"Posters: {len(posters)}")
        await message.answer("\n".join(lines))

    @router.message(
        F.content_type.in_({"video", "photo", "document", "animation"})
    )
    async def _handle_media(message: Message) -> None:
        staging = get_staging_group()
        if not staging:
            return

        staging_chat_id = staging.get("chat_id")
        if staging_chat_id is None:
            return

        # Only process messages from the staging group
        if message.chat.id != int(staging_chat_id):
            return

        thread_id = message.message_thread_id
        if thread_id is None:
            return  # General topic, skip

        # Reverse lookup: find integration_id for this topic_id
        topics = staging.get("topics", {})
        integration_id: str | None = None
        for int_id, topic_info in topics.items():
            tid = topic_info.get("topic_id") if isinstance(topic_info, dict) else topic_info
            if tid is not None and int(tid) == thread_id:
                integration_id = int_id
                break

        if integration_id is None:
            return  # Unmapped topic, skip

        # Extract media info
        media_type: str
        file_id: str
        file_name: str | None = None

        if message.video:
            media_type = "video"
            file_id = message.video.file_id
            file_name = message.video.file_name
        elif message.photo:
            media_type = "photo"
            file_id = message.photo[-1].file_id  # Largest size
            file_name = f"photo_{message.message_id}.jpg"
        elif message.animation:
            media_type = "animation"
            file_id = message.animation.file_id
            file_name = message.animation.file_name or f"animation_{message.message_id}.gif"
        elif message.document:
            media_type = "document"
            file_id = message.document.file_id
            file_name = message.document.file_name
        else:
            return

        if file_name is None:
            file_name = f"{media_type}_{message.message_id}"

        # Dedup: skip if this message_id is already inventoried (e.g., bot restart replay)
        existing = get_inventory(integration_id)
        if any(item.get("message_id") == message.message_id for item in existing):
            return

        add_inventory_item(integration_id, {
            "file_id": file_id,
            "file_name": file_name,
            "media_type": media_type,
            "caption": message.caption,
            "message_id": message.message_id,
            "chat_id": message.chat.id,
            "source": "manual",
        })

    return router


# ---------------------------------------------------------------------------
# Utility functions (called by router endpoints)
# ---------------------------------------------------------------------------


async def create_forum_topic(chat_id: int, name: str) -> int:
    """Create a forum topic in a supergroup and return the thread ID."""
    if _bot is None:
        raise RuntimeError("Bot is not running")
    result = await _bot.create_forum_topic(chat_id=chat_id, name=name)
    return result.message_thread_id


async def delete_forum_topic(chat_id: int, topic_id: int) -> bool:
    """Delete a forum topic from a supergroup. Returns True on success."""
    if _bot is None:
        raise RuntimeError("Bot is not running")
    try:
        await _bot.delete_forum_topic(chat_id=chat_id, message_thread_id=topic_id)
        return True
    except Exception:
        return False


async def send_media_to_topic(
    chat_id: int,
    topic_id: int,
    file_path: str,
    caption: str | None = None,
) -> dict:
    """Send a local media file to a forum topic. Returns message_id and file_id."""
    if _bot is None:
        raise RuntimeError("Bot is not running")

    ext = os.path.splitext(file_path)[1].lower()
    input_file = FSInputFile(file_path)

    if ext in (".mp4", ".mov", ".avi", ".mkv"):
        msg = await _bot.send_video(
            chat_id=chat_id,
            message_thread_id=topic_id,
            video=input_file,
            caption=caption,
        )
        returned_file_id = msg.video.file_id if msg.video else None
    elif ext in (".jpg", ".jpeg", ".png", ".webp"):
        msg = await _bot.send_photo(
            chat_id=chat_id,
            message_thread_id=topic_id,
            photo=input_file,
            caption=caption,
        )
        returned_file_id = msg.photo[-1].file_id if msg.photo else None
    else:
        msg = await _bot.send_document(
            chat_id=chat_id,
            message_thread_id=topic_id,
            document=input_file,
            caption=caption,
        )
        returned_file_id = msg.document.file_id if msg.document else None

    return {
        "message_id": msg.message_id,
        "file_id": returned_file_id,
    }


async def forward_message(
    from_chat_id: int,
    message_id: int,
    to_chat_id: int,
    to_topic_id: int,
) -> int:
    """Forward a message to a forum topic. Returns the new message ID."""
    if _bot is None:
        raise RuntimeError("Bot is not running")

    forwarded = await _bot.forward_message(
        chat_id=to_chat_id,
        from_chat_id=from_chat_id,
        message_id=message_id,
        message_thread_id=to_topic_id,
    )
    return forwarded.message_id


async def forward_new_messages(
    from_chat_id: int,
    from_topic_id: int,
    to_chat_id: int,
    to_topic_id: int,
    after_message_id: int = 0,
) -> dict:
    """Forward all messages in a staging topic that are newer than after_message_id.

    Strategy: send a temporary marker message to the staging topic to discover
    the current max message_id, then try to forward every ID in the range
    (after_message_id+1 .. marker-1). Non-existent IDs are silently skipped.

    Returns {forwarded: int, last_message_id: int, errors: int}.
    """
    if _bot is None:
        raise RuntimeError("Bot is not running")

    # Send a temporary marker to find the current message_id ceiling
    marker = await _bot.send_message(
        chat_id=from_chat_id,
        message_thread_id=from_topic_id,
        text="⏳",  # temporary marker
    )
    marker_id = marker.message_id

    # Delete the marker immediately
    try:
        await _bot.delete_message(chat_id=from_chat_id, message_id=marker_id)
    except Exception:
        pass  # not critical if delete fails

    if after_message_id >= marker_id - 1:
        return {"forwarded": 0, "last_message_id": after_message_id, "errors": 0}

    forwarded = 0
    errors = 0
    last_success_id = after_message_id

    # Try each message_id in the range
    for msg_id in range(after_message_id + 1, marker_id):
        try:
            await _bot.forward_message(
                chat_id=to_chat_id,
                from_chat_id=from_chat_id,
                message_id=msg_id,
                message_thread_id=to_topic_id,
            )
            forwarded += 1
            last_success_id = msg_id
        except Exception:
            # Message doesn't exist, was deleted, or is a service message — skip
            errors += 1

        # Small delay to avoid rate limits
        if forwarded % 10 == 0 and forwarded > 0:
            await asyncio.sleep(1)

    return {
        "forwarded": forwarded,
        "last_message_id": marker_id - 1,
        "errors": errors,
    }


async def discover_topics(
    chat_id: int,
    progress_callback=None,
) -> list[dict]:
    """Discover all forum topics in a group.

    Uses Pyrogram's get_forum_topics() MTProto API which returns real topic
    data directly — no brute-force probing needed.

    Falls back to aiogram brute-force if Pyrogram is not available.
    """
    if _pyro is not None:
        return await _discover_topics_pyrogram(chat_id, progress_callback)
    return await _discover_topics_fallback(chat_id, progress_callback)


async def _discover_topics_pyrogram(
    chat_id: int,
    progress_callback=None,
) -> list[dict]:
    """Discover topics using Pyrogram MTProto get_forum_topics()."""
    topics: list[dict] = []

    if progress_callback:
        progress_callback(0, 0, 0)

    try:
        async for topic in _pyro.get_forum_topics(chat_id=chat_id):
            topics.append({
                "topic_id": topic.message_thread_id,
                "topic_name": topic.name,
            })
            if progress_callback:
                progress_callback(len(topics), len(topics), len(topics))
    except Exception as exc:
        logger.error("pyrogram discover_topics failed: %s", exc)
        raise

    logger.info("discover_topics (pyrogram): found %d topics", len(topics))
    return topics


async def _discover_topics_fallback(
    chat_id: int,
    progress_callback=None,
) -> list[dict]:
    """Fallback: discover topics by brute-force probing message_thread_ids."""
    if _bot is None:
        raise RuntimeError("Bot is not running")

    marker = await _bot.send_message(chat_id=chat_id, text="🔍 Discovering topics...")
    ceiling = marker.message_id
    try:
        await _bot.delete_message(chat_id=chat_id, message_id=marker.message_id)
    except Exception:
        pass

    topics: list[dict] = []
    probed = 0

    async def _probe_one(candidate_id: int) -> dict | None:
        try:
            test_msg = await _bot.send_message(
                chat_id=chat_id, message_thread_id=candidate_id, text=".",
            )
            topic_name = None
            if test_msg.reply_to_message and test_msg.reply_to_message.forum_topic_created:
                topic_name = test_msg.reply_to_message.forum_topic_created.name
            try:
                await _bot.delete_message(chat_id=chat_id, message_id=test_msg.message_id)
            except Exception:
                pass
            return {"topic_id": candidate_id, "topic_name": topic_name}
        except Exception as exc:
            err = str(exc).lower()
            if "too many requests" in err or "429" in err:
                await asyncio.sleep(10)
            return None

    BATCH_SIZE = 15
    all_ids = list(range(1, ceiling + 1))
    for batch_start in range(0, len(all_ids), BATCH_SIZE):
        batch = all_ids[batch_start : batch_start + BATCH_SIZE]
        results = await asyncio.gather(*[_probe_one(cid) for cid in batch])
        for r in results:
            if r is not None:
                topics.append(r)
        probed += len(batch)
        if progress_callback:
            progress_callback(probed, ceiling, len(topics))
        await asyncio.sleep(0.3)

    return topics


async def scan_topic_inventory(
    chat_id: int,
    topic_id: int,
    integration_id: str,
    next_topic_id: int | None = None,
) -> dict:
    """Scan a staging topic for existing media.

    Uses Pyrogram search_messages with message_thread_id to accurately
    find media in a specific topic. Falls back to brute-force if unavailable.

    Returns {found: int, skipped_existing: int, total_scanned: int}.
    """
    if _pyro is not None:
        return await _scan_topic_pyrogram(chat_id, topic_id, integration_id)
    return await _scan_topic_fallback(chat_id, topic_id, integration_id, next_topic_id)


async def _scan_topic_pyrogram(
    chat_id: int,
    topic_id: int,
    integration_id: str,
) -> dict:
    """Scan a topic using Pyrogram's search_messages with message_thread_id.

    Uses no filter to get ALL messages in the topic, then checks each one
    for media (video, document, photo, animation). This catches videos sent
    as documents, forwarded content, etc.
    """
    existing = get_inventory(integration_id)
    existing_msg_ids = {item.get("message_id") for item in existing}

    found = 0
    skipped = 0
    scanned = 0

    try:
        # No filter — get ALL messages in this topic, check for media
        async for msg in _pyro.search_messages(
            chat_id=chat_id,
            message_thread_id=topic_id,
        ):
            scanned += 1
            if msg.id in existing_msg_ids:
                skipped += 1
                continue

            # Check all media types
            media_type = None
            file_id = None
            file_name = None

            if msg.video:
                media_type = "video"
                file_id = msg.video.file_id
                file_name = msg.video.file_name
            elif msg.document:
                media_type = "document"
                file_id = msg.document.file_id
                file_name = msg.document.file_name
                # Treat video documents as video
                if msg.document.mime_type and msg.document.mime_type.startswith("video/"):
                    media_type = "video"
            elif msg.animation:
                media_type = "animation"
                file_id = msg.animation.file_id
                file_name = msg.animation.file_name
            elif msg.photo:
                media_type = "photo"
                file_id = msg.photo.file_id if hasattr(msg.photo, "file_id") else None
                file_name = f"photo_{msg.id}.jpg"

            if media_type and file_id:
                add_inventory_item(integration_id, {
                    "file_id": file_id,
                    "file_name": file_name or f"{media_type}_{msg.id}",
                    "media_type": media_type,
                    "caption": msg.caption,
                    "message_id": msg.id,
                    "chat_id": chat_id,
                    "source": "scan",
                })
                found += 1

    except Exception as exc:
        logger.error("pyrogram scan_topic failed for topic %s: %s", topic_id, exc)
        return {"found": found, "skipped_existing": skipped, "total_scanned": scanned, "error": str(exc)[:200]}

    return {"found": found, "skipped_existing": skipped, "total_scanned": scanned}


async def _scan_topic_fallback(
    chat_id: int,
    topic_id: int,
    integration_id: str,
    next_topic_id: int | None = None,
) -> dict:
    """Fallback scan using aiogram forward approach (less reliable)."""
    if _bot is None:
        raise RuntimeError("Bot is not running")

    try:
        marker = await _bot.send_message(
            chat_id=chat_id, message_thread_id=topic_id, text="📊",
        )
    except Exception as exc:
        return {"found": 0, "skipped_existing": 0, "total_scanned": 0, "error": str(exc)[:200]}

    marker_id = marker.message_id
    try:
        await _bot.delete_message(chat_id=chat_id, message_id=marker_id)
    except Exception:
        pass

    end_id = next_topic_id if next_topic_id else marker_id
    existing = get_inventory(integration_id)
    existing_msg_ids = {item.get("message_id") for item in existing}

    found = 0
    skipped = 0
    scanned = 0

    for msg_id in range(topic_id + 1, end_id):
        scanned += 1
        if msg_id in existing_msg_ids:
            skipped += 1
            continue
        try:
            fwd = await _bot.forward_message(
                chat_id=chat_id, from_chat_id=chat_id, message_id=msg_id,
            )
            media_type = file_id = file_name = None
            if fwd.video:
                media_type, file_id, file_name = "video", fwd.video.file_id, fwd.video.file_name
            elif fwd.document:
                media_type, file_id, file_name = "document", fwd.document.file_id, fwd.document.file_name
            try:
                await _bot.delete_message(chat_id=chat_id, message_id=fwd.message_id)
            except Exception:
                pass
            if media_type and file_id:
                add_inventory_item(integration_id, {
                    "file_id": file_id, "file_name": file_name or f"{media_type}_{msg_id}",
                    "media_type": media_type, "caption": fwd.caption,
                    "message_id": msg_id, "chat_id": chat_id, "source": "scan",
                })
                found += 1
        except Exception:
            pass
        if scanned % 15 == 0:
            await asyncio.sleep(1)

    return {"found": found, "skipped_existing": skipped, "total_scanned": scanned}


async def send_text_to_topic(
    chat_id: int,
    topic_id: int | None,
    text: str,
) -> int:
    """Send a text message to a forum topic. topic_id=None sends to General."""
    if _bot is None:
        raise RuntimeError("Bot is not running")

    msg = await _bot.send_message(
        chat_id=chat_id,
        message_thread_id=topic_id,
        text=text,
    )
    return msg.message_id


async def validate_group(chat_id: int) -> dict:
    """Validate a group chat: check admin status and forum mode."""
    if _bot is None:
        raise RuntimeError("Bot is not running")

    chat = await _bot.get_chat(chat_id)
    me = await _bot.get_chat_member(chat_id, (await _bot.get_me()).id)

    return {
        "is_admin": me.status in ("administrator", "creator"),
        "is_forum": getattr(chat, "is_forum", False),
        "title": chat.title,
    }


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


async def _run_scheduler() -> None:
    """Background task that runs daily batch forwarding on schedule."""
    while True:
        try:
            schedule = get_schedule()
            if not schedule.get("enabled"):
                await asyncio.sleep(60)
                continue

            # Calculate seconds until next run
            tz = ZoneInfo(schedule.get("timezone", "America/New_York"))
            now = datetime.now(tz)
            target_h, target_m = map(int, schedule.get("forward_time", "09:00").split(":"))
            target = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)

            wait_seconds = (target - now).total_seconds()
            await asyncio.sleep(wait_seconds)

            await run_daily_batch()

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"  telegram scheduler error: {e}", flush=True)
            await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Notion sound sync (polling)
# ---------------------------------------------------------------------------


async def _run_notion_sync() -> None:
    """Background task: unified Campaign Hub + Notion sound sync every 15 min."""
    from services.campaign_hub import sync_sound_status, is_configured as hub_ok
    from services.notion import fetch_campaigns_with_sounds, is_configured as notion_ok

    # Wait a bit on startup before first sync
    await asyncio.sleep(30)

    while True:
        try:
            if hub_ok():
                # Fetch Notion data for sound links (optional)
                notion_data = None
                if notion_ok():
                    try:
                        notion_data = await fetch_campaigns_with_sounds()
                    except Exception as e:
                        print(f"  notion fetch failed (continuing): {e}", flush=True)

                result = await sync_sound_status(notion_campaigns=notion_data)
                added = result.get("sounds_added", 0)
                deactivated = result.get("sounds_deactivated", 0)
                unmatched = len(result.get("unmatched", []))
                if added > 0 or deactivated > 0:
                    print(
                        f"  sound sync: +{added} added, -{deactivated} deactivated, "
                        f"{unmatched} unmatched, "
                        f"{result.get('matched_ai', 0)} AI-matched",
                        flush=True,
                    )

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"  sound sync error: {e}", flush=True)

        await asyncio.sleep(NOTION_SYNC_INTERVAL)


# ---------------------------------------------------------------------------
# Daily batch forwarding
# ---------------------------------------------------------------------------


async def run_daily_batch() -> dict:
    """Execute the daily batch: forward new content to posters, send summary + sounds.

    Uses range-based message forwarding — doesn't depend on inventory tracking.
    Forwards everything uploaded to staging topics since the last forward.
    """
    if _bot is None:
        raise RuntimeError("Bot is not running")

    from services.telegram import get_last_forwarded_id, set_last_forwarded_id

    staging = get_staging_group()
    if not staging or not staging.get("chat_id"):
        return {"posters_notified": 0, "videos_forwarded": 0, "sounds_sent": 0}

    staging_chat_id = int(staging["chat_id"])
    staging_topics = staging.get("topics", {})
    posters = list_posters()
    sounds = list_sounds()

    total_forwarded = 0
    posters_notified = 0
    today = datetime.now().strftime("%B %d, %Y")

    for poster in posters:
        poster_id = poster.get("id") or poster.get("poster_id")
        page_ids = poster.get("page_ids", [])
        poster_data = (get_poster(poster_id) if poster_id else None) or poster

        poster_chat_id = poster_data.get("chat_id")
        if not poster_chat_id:
            continue

        poster_chat_id = int(poster_chat_id)
        summary_lines: list[str] = []
        page_count = 0
        poster_total = 0

        for page_id in page_ids:
            # Get staging topic for this page
            staging_topic = staging_topics.get(page_id)
            if not staging_topic:
                continue
            staging_topic_id = staging_topic.get("topic_id")
            if not staging_topic_id:
                continue

            # Get poster's topic for this page
            poster_topics = poster_data.get("topics", {})
            poster_topic_entry = poster_topics.get(page_id)
            if not poster_topic_entry:
                continue
            poster_topic_id = poster_topic_entry.get("topic_id")
            if not poster_topic_id:
                continue

            # Find page name
            page_name = page_id
            for p in list_all_pages():
                if p.get("integration_id") == page_id:
                    page_name = p.get("name", page_id)
                    break

            # Forward new messages since last forward
            after_id = get_last_forwarded_id(page_id)
            try:
                result = await forward_new_messages(
                    from_chat_id=staging_chat_id,
                    from_topic_id=int(staging_topic_id),
                    to_chat_id=poster_chat_id,
                    to_topic_id=int(poster_topic_id),
                    after_message_id=after_id,
                )

                if result["last_message_id"] > after_id:
                    set_last_forwarded_id(page_id, result["last_message_id"])

                if result["forwarded"] > 0:
                    summary_lines.append(f"\U0001f4f1 {page_name}: {result['forwarded']} new video(s)")
                    poster_total += result["forwarded"]
                    page_count += 1

            except Exception as e:
                print(f"  batch forward error page={page_id}: {e}", flush=True)

        total_forwarded += poster_total

        if poster_total == 0 and not sounds:
            continue

        # Build summary message
        msg_parts: list[str] = [f"\U0001f4cb Daily Content Drop \u2014 {today}"]

        if summary_lines:
            msg_parts.append("")
            msg_parts.extend(summary_lines)
            msg_parts.append("")
            msg_parts.append(f"Total: {poster_total} videos across {page_count} pages")

        if sounds:
            msg_parts.append("")
            msg_parts.append("\U0001f3b5 Today's Sounds:")
            for sound in sounds:
                label = sound.get("label", sound.get("name", "Sound"))
                url = sound.get("url", "")
                msg_parts.append(f"\u2022 {label}: {url}")

        try:
            await send_text_to_topic(
                chat_id=poster_chat_id,
                topic_id=None,
                text="\n".join(msg_parts),
            )
            posters_notified += 1
        except Exception as e:
            print(f"  summary send error poster={poster_id}: {e}", flush=True)

        # Send sound links to the poster's dedicated Sounds topic
        sounds_topic_id = poster_data.get("sounds_topic_id")
        if sounds and sounds_topic_id:
            sounds_msg_parts: list[str] = [
                f"\U0001f3b5 Active Sounds \u2014 {today}",
                "",
            ]
            for sound in sounds:
                label = sound.get("label", sound.get("name", "Sound"))
                url = sound.get("url", "")
                sounds_msg_parts.append(f"\u2022 {label}")
                sounds_msg_parts.append(f"  {url}")
                sounds_msg_parts.append("")

            try:
                await send_text_to_topic(
                    chat_id=poster_chat_id,
                    topic_id=int(sounds_topic_id),
                    text="\n".join(sounds_msg_parts),
                )
            except Exception as e:
                print(f"  sounds topic send error poster={poster_id}: {e}", flush=True)

    set_last_run(datetime.now(tz=ZoneInfo("UTC")).isoformat())

    return {
        "posters_notified": posters_notified,
        "videos_forwarded": total_forwarded,
        "sounds_sent": len(sounds) if sounds else 0,
    }
