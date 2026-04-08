"""Centralized structured logging and real-time debug infrastructure.

Provides:
- Ring buffer of recent log entries (queryable by level, module, job_id, time)
- SSE stream for real-time subscribers (agents, debug UIs)
- Custom logging handler that captures all Python logs into the ring buffer
- Structured JSON format for every log entry
"""

import asyncio
import logging
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from typing import Any

# ── Configuration ──────────────────────────────────────────────────────

MAX_ENTRIES = 5000  # ring buffer capacity
MAX_SSE_QUEUE = 200  # per-subscriber backpressure limit

# ── Ring Buffer ────────────────────────────────────────────────────────

_buffer: deque[dict] = deque(maxlen=MAX_ENTRIES)
_subscribers: list[asyncio.Queue] = []
_seq = 0  # monotonic sequence counter


def _next_seq() -> int:
    global _seq
    _seq += 1
    return _seq


def emit(
    level: str,
    module: str,
    message: str,
    *,
    job_id: str | None = None,
    event: str | None = None,
    extra: dict[str, Any] | None = None,
    exc_info: str | None = None,
) -> dict:
    """Write a structured log entry to the ring buffer and notify subscribers."""
    entry = {
        "seq": _next_seq(),
        "ts": datetime.now(timezone.utc).isoformat(),
        "epoch": time.time(),
        "level": level.upper(),
        "module": module,
        "message": message,
    }
    if job_id:
        entry["job_id"] = job_id
    if event:
        entry["event"] = event
    if extra:
        entry["extra"] = extra
    if exc_info:
        entry["exc_info"] = exc_info

    _buffer.append(entry)

    # Fan out to SSE subscribers (non-blocking)
    dead: list[asyncio.Queue] = []
    for q in _subscribers:
        try:
            q.put_nowait(entry)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _subscribers.remove(q)

    return entry


# ── Query API ──────────────────────────────────────────────────────────


def query(
    *,
    level: str | None = None,
    module: str | None = None,
    job_id: str | None = None,
    since: float | None = None,
    limit: int = 200,
    search: str | None = None,
) -> list[dict]:
    """Query the ring buffer with optional filters.

    Args:
        level: Minimum level filter (DEBUG, INFO, WARNING, ERROR)
        module: Module name prefix filter
        job_id: Exact job_id match
        since: Unix epoch — only entries after this time
        limit: Max entries to return (newest first)
        search: Substring search in message
    """
    level_order = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
    min_level = level_order.get((level or "").upper(), 0)

    results = []
    for entry in reversed(_buffer):
        if len(results) >= limit:
            break
        if min_level > 0 and level_order.get(entry["level"], 0) < min_level:
            continue
        if module and not entry["module"].startswith(module):
            continue
        if job_id and entry.get("job_id") != job_id:
            continue
        if since and entry["epoch"] < since:
            continue
        if search and search.lower() not in entry["message"].lower():
            continue
        results.append(entry)

    return results


def query_errors(limit: int = 50) -> list[dict]:
    """Get recent errors with full tracebacks."""
    return query(level="ERROR", limit=limit)


def query_job(job_id: str) -> list[dict]:
    """Get all log entries for a specific job, in chronological order."""
    results = [e for e in _buffer if e.get("job_id") == job_id]
    return results


def get_stats() -> dict:
    """Get buffer statistics."""
    level_counts: dict[str, int] = {}
    module_counts: dict[str, int] = {}
    for entry in _buffer:
        lv = entry["level"]
        level_counts[lv] = level_counts.get(lv, 0) + 1
        mod = entry["module"]
        module_counts[mod] = module_counts.get(mod, 0) + 1

    return {
        "total_entries": len(_buffer),
        "capacity": MAX_ENTRIES,
        "subscribers": len(_subscribers),
        "level_counts": level_counts,
        "module_counts": module_counts,
        "oldest": _buffer[0]["ts"] if _buffer else None,
        "newest": _buffer[-1]["ts"] if _buffer else None,
    }


# ── SSE Subscription ──────────────────────────────────────────────────


def subscribe() -> asyncio.Queue:
    """Create a new SSE subscription queue."""
    q: asyncio.Queue = asyncio.Queue(maxsize=MAX_SSE_QUEUE)
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    """Remove an SSE subscription."""
    if q in _subscribers:
        _subscribers.remove(q)


# ── Python Logging Handler ─────────────────────────────────────────────


class DebugBufferHandler(logging.Handler):
    """Logging handler that routes all Python log records into the ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        exc_text = None
        if record.exc_info and record.exc_info[1]:
            exc_text = "".join(traceback.format_exception(*record.exc_info))

        # Extract job_id from record if attached
        j_id = getattr(record, "job_id", None)

        # Extract event from record if attached
        ev = getattr(record, "event", None)

        # Extract extra dict if attached
        ex = getattr(record, "extra_data", None)

        emit(
            level=record.levelname,
            module=record.name,
            message=record.getMessage(),
            job_id=j_id,
            event=ev,
            extra=ex,
            exc_info=exc_text,
        )


# ── Setup ──────────────────────────────────────────────────────────────

_handler = DebugBufferHandler()
_handler.setLevel(logging.DEBUG)


def setup_logging(level: str = "DEBUG") -> None:
    """Install the debug buffer handler on the root logger.

    Call this once at app startup. All loggers (including third-party)
    will route through the ring buffer.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.DEBUG))

    # Add our handler if not already present
    if _handler not in root.handlers:
        root.addHandler(_handler)

    # Also keep a StreamHandler for terminal output
    has_stream = any(isinstance(h, logging.StreamHandler) and not isinstance(h, DebugBufferHandler) for h in root.handlers)
    if not has_stream:
        sh = logging.StreamHandler()
        sh.setLevel(logging.INFO)
        sh.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s  %(message)s",
            datefmt="%H:%M:%S",
        ))
        root.addHandler(sh)

    # Quiet down noisy third-party loggers
    for name in ("httpx", "httpcore", "uvicorn.access", "watchfiles"):
        logging.getLogger(name).setLevel(logging.WARNING)
