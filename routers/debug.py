"""Debug API router — real-time log access for agents and debugging.

Endpoints:
  GET  /api/debug/logs          — query recent logs (filtered)
  GET  /api/debug/stream        — SSE stream of live log events
  GET  /api/debug/jobs/{job_id} — full trace for a specific job
  GET  /api/debug/errors        — recent errors with tracebacks
  GET  /api/debug/health        — system state snapshot
  POST /api/debug/clear         — clear the log buffer
"""

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

import debug_logger

log = logging.getLogger("debug")

router = APIRouter()


@router.get("/logs")
async def get_logs(
    level: str | None = Query(None, description="Min level: DEBUG, INFO, WARNING, ERROR"),
    module: str | None = Query(None, description="Module name prefix filter"),
    job_id: str | None = Query(None, description="Exact job_id filter"),
    since: float | None = Query(None, description="Unix epoch — entries after this time"),
    limit: int = Query(200, ge=1, le=5000, description="Max entries (newest first)"),
    search: str | None = Query(None, description="Substring search in message"),
):
    """Query recent logs with optional filters. Returns newest first."""
    entries = debug_logger.query(
        level=level,
        module=module,
        job_id=job_id,
        since=since,
        limit=limit,
        search=search,
    )
    return {"count": len(entries), "entries": entries}


@router.get("/stream")
async def stream_logs(
    request: Request,
    level: str | None = Query(None, description="Min level filter for stream"),
    module: str | None = Query(None, description="Module prefix filter for stream"),
    job_id: str | None = Query(None, description="Job ID filter for stream"),
):
    """SSE stream of live log events. Agents subscribe here for real-time monitoring.

    Sends `data: {json}` lines. Each event is a structured log entry.
    Connection stays open until client disconnects.

    Supports optional filters so agents can subscribe to only relevant events.
    """
    level_order = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
    min_level = level_order.get((level or "").upper(), 0)

    async def event_generator():
        q = debug_logger.subscribe()
        try:
            # Send initial keepalive
            yield f"data: {json.dumps({'event': 'connected', 'ts': time.time()})}\n\n"

            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break

                try:
                    entry = await asyncio.wait_for(q.get(), timeout=30.0)

                    # Apply filters
                    if min_level > 0 and level_order.get(entry["level"], 0) < min_level:
                        continue
                    if module and not entry["module"].startswith(module):
                        continue
                    if job_id and entry.get("job_id") != job_id:
                        continue

                    yield f"data: {json.dumps(entry)}\n\n"

                except asyncio.TimeoutError:
                    # Send keepalive ping every 30s
                    yield f"data: {json.dumps({'event': 'ping', 'ts': time.time()})}\n\n"

        finally:
            debug_logger.unsubscribe(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/jobs/{job_id}")
async def get_job_logs(job_id: str):
    """Get all log entries for a specific job in chronological order.

    Useful for tracing the full lifecycle of a video generation,
    caption scrape, clip batch, or burn operation.
    """
    entries = debug_logger.query_job(job_id)
    return {
        "job_id": job_id,
        "count": len(entries),
        "entries": entries,
    }


@router.get("/errors")
async def get_errors(
    limit: int = Query(50, ge=1, le=500, description="Max errors to return"),
):
    """Get recent errors with full tracebacks. No truncation."""
    entries = debug_logger.query_errors(limit=limit)
    return {"count": len(entries), "entries": entries}


@router.get("/health")
async def debug_health():
    """System state snapshot — buffer stats, active subscribers, module breakdown."""
    stats = debug_logger.get_stats()

    # Also report active jobs from video/clipper/burn routers
    active_jobs: dict = {}
    try:
        from routers.video import jobs as video_jobs
        active_video = {
            jid: {
                "provider": j.get("provider"),
                "project": j.get("project"),
                "status": [v.get("status") for v in j.get("videos", [])],
            }
            for jid, j in video_jobs.items()
            if any(v.get("status") not in ("done", "error") for v in j.get("videos", []))
        }
        if active_video:
            active_jobs["video"] = active_video
    except ImportError:
        pass

    try:
        from routers.clipper import _batch_jobs
        active_clip = {
            jid: {"status": j.get("status"), "clip": j.get("clip"), "total": j.get("total")}
            for jid, j in _batch_jobs.items()
            if j.get("status") == "running"
        }
        if active_clip:
            active_jobs["clipper"] = active_clip
    except ImportError:
        pass

    try:
        from routers.burn import _burn_jobs
        active_burn = {
            bid: {
                "total": len(items),
                "done": sum(1 for v in items.values() if v.get("status") in ("done", "error")),
            }
            for bid, items in _burn_jobs.items()
            if any(v.get("status") in ("queued", "burning") for v in items.values())
        }
        if active_burn:
            active_jobs["burn"] = active_burn
    except ImportError:
        pass

    return {
        "buffer": stats,
        "active_jobs": active_jobs,
    }


@router.post("/clear")
async def clear_logs():
    """Clear the log buffer."""
    debug_logger._buffer.clear()
    log.info("Log buffer cleared via API")
    return {"ok": True}
