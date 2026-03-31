"""
Telegram bot worker for Content Posting Lab.

Runs alongside FastAPI as asyncio tasks in the same event loop.
Handles media inventory from staging group topics and daily batch forwarding.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile

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

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def start_bot(token: str) -> None:
    """Start the Telegram bot and background scheduler."""
    global _bot, _dp, _poll_task, _schedule_task

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

    print(f"  telegram bot started as @{me.username}", flush=True)


async def stop_bot() -> None:
    """Gracefully stop the bot, scheduler, and clean up resources."""
    global _bot, _dp, _poll_task, _schedule_task

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


def get_bot() -> Bot | None:
    """Return the active Bot instance or None."""
    return _bot


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
# Daily batch forwarding
# ---------------------------------------------------------------------------


async def run_daily_batch() -> dict:
    """Execute the daily batch: forward pending inventory to posters, send summary."""
    if _bot is None:
        raise RuntimeError("Bot is not running")

    staging = get_staging_group()
    if not staging:
        return {"posters_notified": 0, "videos_forwarded": 0, "sounds_sent": 0}

    staging_chat_id = int(staging["chat_id"])
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
            pending = get_pending_inventory(page_id)
            if not pending:
                continue

            # Find the poster's topic for this page
            poster_topics = poster_data.get("topics", {})
            target_topic_id = None
            topic_entry = poster_topics.get(page_id)
            if topic_entry:
                target_topic_id = topic_entry.get("topic_id")

            # Find page name from roster
            page_info = None
            for p in list_all_pages():
                if p.get("integration_id") == page_id:
                    page_info = p
                    break
            page_name = page_info.get("name", page_id) if page_info else page_id

            forwarded_count = 0
            for item in pending:
                try:
                    if target_topic_id is not None:
                        fwd_msg_id = await forward_message(
                            from_chat_id=staging_chat_id,
                            message_id=item["message_id"],
                            to_chat_id=poster_chat_id,
                            to_topic_id=int(target_topic_id),
                        )
                    else:
                        fwd_msg_id = 0
                    mark_forwarded(page_id, item.get("id", ""), poster_id or "", fwd_msg_id)
                    forwarded_count += 1
                except Exception as e:
                    print(f"  forward error page={page_id} item={item.get('message_id')}: {e}", flush=True)

            if forwarded_count > 0:
                summary_lines.append(f"\U0001f4f1 {page_name}: {forwarded_count} new video(s)")
                poster_total += forwarded_count
                page_count += 1

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

    set_last_run(datetime.now(tz=ZoneInfo("UTC")).isoformat())

    return {
        "posters_notified": posters_notified,
        "videos_forwarded": total_forwarded,
        "sounds_sent": len(sounds) if sounds else 0,
    }
