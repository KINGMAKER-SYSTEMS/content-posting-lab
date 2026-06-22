"""
Content request intake — the queue posters fill from the Telegram Mini App.

This is the lab side of the "request more content from the agent" loop:
the Mini App writes requests here; the external agent reads the open ones,
acts on them, and marks them fulfilled. Stored as a JSON file on the Railway
volume with atomic writes, mirroring the telegram/roster data layers.
"""

import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from services.json_store import atomic_load, atomic_save, lock_for

BASE_DIR = Path(__file__).parent.parent
_VOLUME_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")
_DATA_DIR = Path(_VOLUME_PATH) if _VOLUME_PATH and Path(_VOLUME_PATH).exists() else BASE_DIR
REQUESTS_PATH = _DATA_DIR / "content_requests.json"

# Lifecycle of a request as the agent works it.
STATUSES = ("open", "in_progress", "fulfilled", "cancelled")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def _empty() -> dict:
    return {"version": 1, "requests": []}


def load_requests() -> dict:
    """Load the request store. Returns empty structure if missing or corrupt."""
    data = atomic_load(REQUESTS_PATH, default=None)
    if not isinstance(data, dict) or "requests" not in data:
        return _empty()
    return data


def save_requests(data: dict) -> None:
    """Atomic write: tmp file then rename."""
    atomic_save(REQUESTS_PATH, data)


@contextmanager
def mutate_requests() -> Iterator[dict]:
    """Load the store, yield it for in-place mutation, then save it — under a lock.

    Closes the load-modify-save (TOCTOU) race: atomic_save only atomizes the
    write, so two concurrent callers (add_request / update_request from async
    routers via the sync threadpool) that each load → mutate → save could lose
    one set of changes. Holding REQUESTS_PATH's reentrant lock across the whole
    transaction serializes them. Mirrors services/telegram.mutate_config.
    """
    with lock_for(REQUESTS_PATH):
        data = load_requests()
        yield data
        save_requests(data)


def add_request(
    poster_id: str,
    text: str,
    *,
    poster_name: str | None = None,
    page_id: str | None = None,
    page_name: str | None = None,
    quantity: int | None = None,
    source: str = "miniapp",
) -> dict:
    """Append a new content request. Returns the stored entry with a generated id."""
    entry = {
        "id": uuid.uuid4().hex[:12],
        "poster_id": poster_id,
        "poster_name": poster_name,
        "page_id": page_id,
        "page_name": page_name,
        "text": (text or "").strip(),
        "quantity": quantity,
        "status": "open",
        "source": source,
        "created_at": _now(),
        "updated_at": _now(),
        "fulfilled_at": None,
        "agent_note": None,
    }
    with mutate_requests() as data:
        data["requests"].append(entry)
    return entry


def list_requests(
    poster_id: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """List requests, newest first, optionally filtered by poster and/or status."""
    items = load_requests()["requests"]
    if poster_id is not None:
        items = [r for r in items if r.get("poster_id") == poster_id]
    if status is not None:
        items = [r for r in items if r.get("status") == status]
    return sorted(items, key=lambda r: r.get("created_at", ""), reverse=True)


def get_request(request_id: str) -> dict | None:
    for r in load_requests()["requests"]:
        if r.get("id") == request_id:
            return r
    return None


def update_request(
    request_id: str,
    *,
    status: str | None = None,
    agent_note: str | None = None,
) -> dict | None:
    """Update a request's status / agent note. Returns the updated entry or None."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"invalid status {status!r}; expected one of {STATUSES}")
    with mutate_requests() as data:
        for r in data["requests"]:
            if r.get("id") == request_id:
                if status is not None:
                    r["status"] = status
                    if status == "fulfilled":
                        r["fulfilled_at"] = _now()
                if agent_note is not None:
                    r["agent_note"] = agent_note
                r["updated_at"] = _now()
                return r
    return None
