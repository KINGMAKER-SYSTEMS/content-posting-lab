"""
Telegram bot configuration data access layer.
Manages the telegram_config.json file that stores bot credentials,
staging group settings, poster assignments, content inventory, sounds,
and forwarding schedule.
"""

import json
import time
import uuid
from pathlib import Path

import os

BASE_DIR = Path(__file__).parent.parent

# Store config on the persistent volume.
# Railway mounts the volume at RAILWAY_VOLUME_MOUNT_PATH (e.g. /app/projects).
# Locally, falls back to the app directory.
_VOLUME_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")
_DATA_DIR = Path(_VOLUME_PATH) if _VOLUME_PATH and Path(_VOLUME_PATH).exists() else BASE_DIR
CONFIG_PATH = _DATA_DIR / "telegram_config.json"

# Default posters — seeded on first boot so they don't need to be recreated
_DEFAULT_POSTERS = {
    "seffra": {"poster_id": "seffra", "name": "Seffra", "chat_id": -1003869464172},
    "gigi": {"poster_id": "gigi", "name": "Gigi", "chat_id": -1003814954137},
    "johnny-balik": {"poster_id": "johnny-balik", "name": "Johnny Balik", "chat_id": -1003754164520},
    "sam-hudgen": {"poster_id": "sam-hudgen", "name": "Sam Hudgen", "chat_id": -1003691005229},
    "jake-balik": {"poster_id": "jake-balik", "name": "Jake Balik", "chat_id": -1003867018292},
    "eric-cromartie": {"poster_id": "eric-cromartie", "name": "Eric Cromartie", "chat_id": -1003796560010},
    "john-smathers": {"poster_id": "john-smathers", "name": "John Smathers", "chat_id": -1003302681249},
}


def _empty_config() -> dict:
    return {
        "version": 1,
        "bot_token": None,
        "bot_username": None,
        "staging_group": {
            "chat_id": -1003748889949,
            "name": "Rising Tides Pages",
            "topics": {},
        },
        "posters": {},
        "inventory": {},
        "sounds": [],
        "schedule": {
            "enabled": False,
            "forward_time": "09:00",
            "timezone": "America/New_York",
            "last_run": None,
        },
    }


def _seed_config() -> dict:
    """Create a fresh config with default staging group and posters."""
    config = _empty_config()
    now = _now()
    for pid, pdata in _DEFAULT_POSTERS.items():
        config["posters"][pid] = {
            **pdata,
            "page_ids": [],
            "topics": {},
            "added_at": now,
            "updated_at": now,
        }
    save_config(config)
    return config


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def _gen_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Config CRUD
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load config from disk. Auto-seeds with defaults on first boot."""
    if not CONFIG_PATH.exists():
        return _seed_config()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "version" not in data:
            return _empty_config()
        return data
    except (json.JSONDecodeError, OSError):
        return _empty_config()


def save_config(data: dict) -> None:
    """Atomic write: write to tmp file then rename."""
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.rename(CONFIG_PATH)


def get_bot_token() -> str | None:
    """Return the bot token. Env var TELEGRAM_BOT_TOKEN takes priority over config file."""
    env_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if env_token:
        return env_token
    token = load_config().get("bot_token")
    return token.strip() if isinstance(token, str) else token


def set_bot_token(token: str) -> None:
    """Store a bot token."""
    config = load_config()
    config["bot_token"] = token
    save_config(config)


def clear_bot_token() -> None:
    """Remove the bot token and username."""
    config = load_config()
    config["bot_token"] = None
    config["bot_username"] = None
    save_config(config)


# ---------------------------------------------------------------------------
# Staging Group
# ---------------------------------------------------------------------------

def get_staging_group() -> dict:
    """Return the staging group settings."""
    config = load_config()
    return config.get("staging_group", _empty_config()["staging_group"])


def set_staging_group(chat_id: int, name: str) -> dict:
    """Set the staging group chat ID and name. Clears topics if group changes."""
    config = load_config()
    old_chat_id = config["staging_group"].get("chat_id")
    config["staging_group"]["chat_id"] = chat_id
    config["staging_group"]["name"] = name
    # Clear stale topic mappings when switching to a different group
    if old_chat_id is not None and old_chat_id != chat_id:
        config["staging_group"]["topics"] = {}
    save_config(config)
    return config["staging_group"]


def set_staging_topic(integration_id: str, topic_id: int, topic_name: str, force: bool = False) -> dict:
    """Map an integration to a staging group topic. Returns updated staging group.

    APPEND-ONLY by default: refuses to overwrite an existing mapping unless force=True.
    """
    config = load_config()
    existing = config["staging_group"]["topics"].get(integration_id)
    if existing and not force:
        return config["staging_group"]
    config["staging_group"]["topics"][integration_id] = {
        "topic_id": topic_id,
        "topic_name": topic_name,
    }
    save_config(config)
    return config["staging_group"]


def remove_staging_topic(integration_id: str) -> bool:
    """Remove a topic mapping. Returns True if it existed."""
    config = load_config()
    topics = config["staging_group"].get("topics", {})
    if integration_id not in topics:
        return False
    del topics[integration_id]
    save_config(config)
    return True


def get_last_forwarded_id(integration_id: str) -> int:
    """Get the last forwarded message_id for a staging topic. Returns 0 if never forwarded."""
    config = load_config()
    topic = config["staging_group"].get("topics", {}).get(integration_id, {})
    return topic.get("last_forwarded_id", 0)


def set_last_forwarded_id(integration_id: str, message_id: int) -> None:
    """Update the last forwarded message_id for a staging topic."""
    config = load_config()
    topic = config["staging_group"].get("topics", {}).get(integration_id)
    if topic is not None:
        topic["last_forwarded_id"] = message_id
        save_config(config)


# ---------------------------------------------------------------------------
# Posters
# ---------------------------------------------------------------------------

def list_posters() -> list[dict]:
    """Return all posters as a list."""
    config = load_config()
    return list(config.get("posters", {}).values())


def get_poster(poster_id: str) -> dict | None:
    """Get a single poster or None."""
    config = load_config()
    return config.get("posters", {}).get(poster_id)


def set_poster(poster_id: str, data: dict) -> dict:
    """Create or update a poster. Auto-sets added_at/updated_at. Returns saved entry."""
    config = load_config()
    existing = config.get("posters", {}).get(poster_id, {})
    now = _now()

    entry = {
        "poster_id": poster_id,
        "name": data.get("name", existing.get("name", "")),
        "chat_id": data.get("chat_id", existing.get("chat_id")),
        "page_ids": data.get("page_ids", existing.get("page_ids", [])),
        "topics": data.get("topics", existing.get("topics", {})),
        "sounds_topic_id": data.get("sounds_topic_id", existing.get("sounds_topic_id")),
        "added_at": existing.get("added_at", now),
        "updated_at": now,
    }

    config.setdefault("posters", {})[poster_id] = entry
    save_config(config)
    return entry


def remove_poster(poster_id: str) -> bool:
    """Remove a poster. Returns True if it existed."""
    config = load_config()
    posters = config.get("posters", {})
    if poster_id not in posters:
        return False
    del posters[poster_id]
    save_config(config)
    return True


def assign_page_to_poster(poster_id: str, integration_id: str) -> dict:
    """Add a page to a poster's page_ids list. Returns updated poster."""
    config = load_config()
    poster = config.get("posters", {}).get(poster_id)
    if poster is None:
        raise ValueError(f"Poster {poster_id} not found")
    if integration_id not in poster.get("page_ids", []):
        poster.setdefault("page_ids", []).append(integration_id)
    poster["updated_at"] = _now()
    save_config(config)
    return poster


def unassign_page_from_poster(poster_id: str, integration_id: str) -> dict:
    """Remove a page from a poster's page_ids and topics. Returns updated poster."""
    config = load_config()
    poster = config.get("posters", {}).get(poster_id)
    if poster is None:
        raise ValueError(f"Poster {poster_id} not found")
    page_ids = poster.get("page_ids", [])
    if integration_id in page_ids:
        page_ids.remove(integration_id)
    topics = poster.get("topics", {})
    if integration_id in topics:
        del topics[integration_id]
    poster["updated_at"] = _now()
    save_config(config)
    return poster


def set_poster_topic(poster_id: str, integration_id: str, topic_id: int, topic_name: str, force: bool = False) -> dict:
    """Map an integration to a topic within a poster. Returns updated poster.

    APPEND-ONLY by default: refuses to overwrite an existing mapping unless force=True.
    """
    config = load_config()
    poster = config.get("posters", {}).get(poster_id)
    if poster is None:
        raise ValueError(f"Poster {poster_id} not found")
    existing = poster.get("topics", {}).get(integration_id)
    if existing and not force:
        return poster
    poster.setdefault("topics", {})[integration_id] = {
        "topic_id": topic_id,
        "topic_name": topic_name,
    }
    poster["updated_at"] = _now()
    save_config(config)
    return poster


def get_poster_for_page(integration_id: str) -> dict | None:
    """Reverse lookup: find which poster owns this page. Returns poster or None."""
    config = load_config()
    for poster in config.get("posters", {}).values():
        if integration_id in poster.get("page_ids", []):
            return poster
    return None


def set_poster_sounds_topic(poster_id: str, topic_id: int) -> dict:
    """Set the sounds topic ID for a poster. Returns updated poster."""
    config = load_config()
    poster = config.get("posters", {}).get(poster_id)
    if poster is None:
        raise ValueError(f"Poster {poster_id} not found")
    poster["sounds_topic_id"] = topic_id
    poster["updated_at"] = _now()
    save_config(config)
    return poster


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def add_inventory_item(integration_id: str, item: dict) -> dict:
    """Append an item to a page's inventory list. Returns the item with generated ID."""
    config = load_config()
    inventory = config.setdefault("inventory", {})
    page_items = inventory.setdefault(integration_id, [])

    entry = {
        "id": _gen_id(),
        "added_at": _now(),
        "forwarded": {},
        **item,
    }
    page_items.append(entry)
    save_config(config)
    return entry


def get_inventory(integration_id: str) -> list:
    """Get all inventory items for a page."""
    config = load_config()
    return config.get("inventory", {}).get(integration_id, [])


def get_all_inventory_summary() -> list[dict]:
    """Return summary stats for each page's inventory."""
    config = load_config()
    inventory = config.get("inventory", {})
    summaries = []
    for integration_id, items in inventory.items():
        total = len(items)
        pending = sum(1 for i in items if not i.get("forwarded"))
        forwarded = total - pending
        summaries.append({
            "integration_id": integration_id,
            "total": total,
            "pending": pending,
            "forwarded": forwarded,
        })
    return summaries


def get_pending_inventory(integration_id: str) -> list:
    """Get items where forwarded dict is empty."""
    config = load_config()
    items = config.get("inventory", {}).get(integration_id, [])
    return [i for i in items if not i.get("forwarded")]


def mark_forwarded(integration_id: str, item_id: str, poster_id: str, message_id: int) -> dict | None:
    """Mark an inventory item as forwarded. Returns updated item or None if not found."""
    config = load_config()
    items = config.get("inventory", {}).get(integration_id, [])
    for item in items:
        if item.get("id") == item_id:
            item["forwarded"] = {
                "poster_id": poster_id,
                "message_id": message_id,
                "forwarded_at": _now(),
            }
            save_config(config)
            return item
    return None


def remove_inventory_item(integration_id: str, item_id: str) -> bool:
    """Remove an inventory item. Returns True if it existed."""
    config = load_config()
    items = config.get("inventory", {}).get(integration_id, [])
    for idx, item in enumerate(items):
        if item.get("id") == item_id:
            items.pop(idx)
            save_config(config)
            return True
    return False


# ---------------------------------------------------------------------------
# Sounds
# ---------------------------------------------------------------------------

def list_sounds(active_only: bool = True) -> list[dict]:
    """List sounds, optionally filtering to active only."""
    config = load_config()
    sounds = config.get("sounds", [])
    if active_only:
        return [s for s in sounds if s.get("active", True)]
    return sounds


def add_sound(url: str, label: str) -> dict:
    """Add a new sound entry. Returns the created sound."""
    config = load_config()
    entry = {
        "id": _gen_id(),
        "url": url,
        "label": label,
        "active": True,
        "added_at": _now(),
    }
    config.setdefault("sounds", []).append(entry)
    save_config(config)
    return entry


def remove_sound(sound_id: str) -> bool:
    """Remove a sound. Returns True if it existed."""
    config = load_config()
    sounds = config.get("sounds", [])
    for idx, sound in enumerate(sounds):
        if sound.get("id") == sound_id:
            sounds.pop(idx)
            save_config(config)
            return True
    return False


def toggle_sound(sound_id: str, active: bool) -> dict | None:
    """Set a sound's active flag. Returns updated sound or None if not found."""
    config = load_config()
    for sound in config.get("sounds", []):
        if sound.get("id") == sound_id:
            sound["active"] = active
            save_config(config)
            return sound
    return None


def update_sound(sound_id: str, url: str | None = None, label: str | None = None) -> dict | None:
    """Update a sound's url and/or label. Returns updated sound or None if not found."""
    config = load_config()
    for sound in config.get("sounds", []):
        if sound.get("id") == sound_id:
            if url is not None:
                sound["url"] = url
            if label is not None:
                sound["label"] = label
            save_config(config)
            return sound
    return None


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

def get_schedule() -> dict:
    """Return the schedule settings."""
    config = load_config()
    return config.get("schedule", _empty_config()["schedule"])


def set_schedule(enabled: bool | None = None, forward_time: str | None = None, timezone: str | None = None) -> dict:
    """Update schedule fields (only provided ones). Returns updated schedule."""
    config = load_config()
    schedule = config.setdefault("schedule", _empty_config()["schedule"])
    if enabled is not None:
        schedule["enabled"] = enabled
    if forward_time is not None:
        schedule["forward_time"] = forward_time
    if timezone is not None:
        schedule["timezone"] = timezone
    save_config(config)
    return schedule


def set_last_run(timestamp: str) -> None:
    """Record when the schedule last ran."""
    config = load_config()
    config.setdefault("schedule", _empty_config()["schedule"])["last_run"] = timestamp
    save_config(config)
