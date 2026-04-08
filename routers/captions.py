"""Caption scraping router — migrated from caption_server.py."""

import asyncio
import base64
import csv
import json
import logging
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from project_manager import get_project_caption_dir

log = logging.getLogger("captions")

router = APIRouter()

# ── WebSocket client registry ────────────────────────────────────────
_ws_clients: dict[str, list[WebSocket]] = {}


async def _broadcast(job_id: str, event: str, data: dict):
    clients = _ws_clients.get(job_id, [])
    log.debug("broadcast job=%s event=%s clients=%d", job_id[:8], event, len(clients))
    msg = json.dumps({"event": event, **data})
    dead: list[WebSocket] = []
    for ws in clients:
        try:
            await ws.send_text(msg)
        except Exception as e:
            log.warning("broadcast send failed: %s", e)
            dead.append(ws)
    for ws in dead:
        clients.remove(ws)


def _video_id(url: str) -> str:
    m = re.search(r"/video/(\d+)", url)
    return m.group(1) if m else "unknown"


# ── Pipeline ─────────────────────────────────────────────────────────


async def _run_pipeline(
    job_id: str,
    profile_url: str,
    max_videos: int,
    sort: str,
    project: str | None = None,
):
    from scraper.frame_extractor import list_profile_videos, get_thumbnail
    from scraper.caption_extractor import extract_caption
    from scraper.sentiment_analyzer import analyze_mood

    # Normalize URL
    pu = profile_url.strip()
    if pu.startswith("@"):
        profile_url = f"https://www.tiktok.com/{pu}"
    elif not pu.startswith("http"):
        profile_url = f"https://www.tiktok.com/@{pu}"
    m = re.search(r"@([\w.]+)", profile_url)
    username = m.group(1) if m else "unknown"

    # Project-scoped output directory (always project-scoped for Railway persistence)
    job_dir = get_project_caption_dir(project or "quick-test") / username
    frames_dir = job_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Phase 1: List video URLs via yt-dlp (no browser)
        await _broadcast(
            job_id, "status", {"text": f"Listing videos for @{username}..."}
        )
        video_urls = await list_profile_videos(profile_url, max_videos, sort)
        total = len(video_urls)

        await _broadcast(job_id, "urls_collected", {"count": total, "urls": video_urls})

        if total == 0:
            await _broadcast(
                job_id,
                "all_complete",
                {"results": [], "csv": None, "username": username},
            )
            return

        results: list[dict | None] = [None] * total

        # Phase 2: Download thumbnails (concurrent, batches of 5)
        DOWNLOAD_BATCH = 5

        async def _download_one(i: int, url: str):
            vid = _video_id(url)
            row: dict = {
                "index": i,
                "video_url": url,
                "video_id": vid,
                "frame_path": None,
                "caption": None,
                "mood": None,
                "error": None,
            }
            try:
                await _broadcast(
                    job_id, "downloading", {"index": i, "total": total, "video_id": vid}
                )
                thumb_path = frames_dir / f"{vid}.jpg"
                # Timeout each thumbnail download to prevent stalling the batch
                await asyncio.wait_for(get_thumbnail(url, thumb_path), timeout=45)
                row["frame_path"] = str(thumb_path)
                b64 = base64.b64encode(thumb_path.read_bytes()).decode()
                await _broadcast(
                    job_id,
                    "frame_ready",
                    {
                        "index": i,
                        "total": total,
                        "video_id": vid,
                        "b64": b64,
                        "video_url": url,
                    },
                )
            except asyncio.TimeoutError:
                log.warning("thumbnail timed out for %s", vid)
                row["error"] = "Thumbnail download timed out"
                await _broadcast(
                    job_id,
                    "frame_error",
                    {
                        "index": i,
                        "total": total,
                        "video_id": vid,
                        "error": "Thumbnail download timed out",
                    },
                )
            except Exception as e:
                log.error("thumbnail failed for %s: %s", vid, e)
                row["error"] = str(e)
                await _broadcast(
                    job_id,
                    "frame_error",
                    {
                        "index": i,
                        "total": total,
                        "video_id": vid,
                        "error": str(e),
                    },
                )
            results[i] = row

        for batch_start in range(0, total, DOWNLOAD_BATCH):
            batch = list(enumerate(video_urls))[
                batch_start : batch_start + DOWNLOAD_BATCH
            ]
            await asyncio.gather(*[_download_one(i, url) for i, url in batch])

        # Phase 3: GPT-4.1 vision caption extraction (concurrent, small batches to avoid rate limits)
        OCR_BATCH = 3
        await _broadcast(job_id, "ocr_starting", {"total": total})

        async def _ocr_one(row: dict):
            i = row["index"]
            if row["error"] or not row["frame_path"]:
                await _broadcast(
                    job_id,
                    "ocr_done",
                    {
                        "index": i,
                        "total": total,
                        "video_id": row["video_id"],
                        "caption": "",
                        "error": row["error"],
                    },
                )
                return
            await _broadcast(
                job_id,
                "ocr_started",
                {
                    "index": i,
                    "total": total,
                    "video_id": row["video_id"],
                },
            )
            try:
                frame_bytes = Path(row["frame_path"]).read_bytes()
                caption = await asyncio.wait_for(extract_caption(frame_bytes), timeout=30)
                row["caption"] = caption
                # Analyze mood/sentiment if caption was extracted
                if caption.strip():
                    try:
                        mood = await asyncio.wait_for(analyze_mood(caption), timeout=10)
                        row["mood"] = mood
                    except (asyncio.TimeoutError, Exception) as me:
                        log.warning("mood analysis failed for %s: %s", row['video_id'], me)
                        row["mood"] = "chill"
                else:
                    row["mood"] = None
            except asyncio.TimeoutError:
                log.warning("OCR timed out for %s", row['video_id'])
                row["error"] = "Caption extraction timed out"
                caption = ""
            except Exception as e:
                log.error("OCR failed for %s: %s", row['video_id'], e)
                row["error"] = str(e)
                caption = ""
            await _broadcast(
                job_id,
                "ocr_done",
                {
                    "index": i,
                    "total": total,
                    "video_id": row["video_id"],
                    "caption": caption,
                    "mood": row.get("mood"),
                    "error": row.get("error"),
                },
            )

        for batch_start in range(0, total, OCR_BATCH):
            batch = [r for r in results[batch_start : batch_start + OCR_BATCH] if r]
            await asyncio.gather(*[_ocr_one(row) for row in batch])

        # Write CSV
        csv_path = job_dir / "captions.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=["video_id", "video_url", "caption", "mood", "error"]
            )
            writer.writeheader()
            for row in results:
                if row:
                    writer.writerow(
                        {
                            "video_id": row["video_id"],
                            "video_url": row["video_url"],
                            "caption": row.get("caption", ""),
                            "mood": row.get("mood", ""),
                            "error": row.get("error", ""),
                        }
                    )

        await _broadcast(
            job_id,
            "all_complete",
            {
                "results": [
                    {
                        "index": r["index"],
                        "video_id": r["video_id"],
                        "video_url": r["video_url"],
                        "caption": r.get("caption", ""),
                        "mood": r.get("mood"),
                        "error": r.get("error"),
                    }
                    for r in results
                    if r
                ],
                "csv": str(csv_path),
                "username": username,
            },
        )

    except Exception as e:
        log.error("pipeline failed for job=%s: %s", job_id[:8], e, exc_info=True)
        await _broadcast(job_id, "error", {"error": str(e)})


# ── WebSocket endpoint ────────────────────────────────────────────────


@router.websocket("/ws/{job_id}")
async def websocket_scrape(ws: WebSocket, job_id: str):
    """WebSocket endpoint for real-time caption scraping progress."""
    await ws.accept()
    log.info("WS connected: %s", job_id[:8])
    _ws_clients.setdefault(job_id, []).append(ws)
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            if msg.get("action") == "start":
                log.info("start scrape: profile=%s max=%s job=%s", msg.get('profile_url', '')[:60], msg.get('max_videos'), job_id[:8])
                asyncio.create_task(
                    _run_pipeline(
                        job_id,
                        msg["profile_url"],
                        min(max(1, msg.get("max_videos", 20)), 50),
                        msg.get("sort", "latest"),
                        msg.get("project"),
                    )
                )
    except WebSocketDisconnect:
        log.info("WS disconnected: %s", job_id[:8])
    finally:
        clients = _ws_clients.get(job_id, [])
        if ws in clients:
            clients.remove(ws)


# ── Export endpoint ───────────────────────────────────────────────────


@router.get("/export/{username}")
async def export_captions(
    username: str,
    project: str | None = Query(default=None),
):
    """Download captions CSV for a username, optionally scoped to a project."""
    csv_path = get_project_caption_dir(project or "quick-test") / username / "captions.csv"
    if not csv_path.exists():
        raise HTTPException(404, "CSV not found")
    return FileResponse(csv_path, filename=f"{username}_captions.csv")


@router.get("/history")
async def caption_history(project: str = Query(default="quick-test")):
    """List all previously scraped caption batches for a project.

    Returns each username folder with its caption count, file modified time,
    and sample captions for preview.
    """
    import csv as csv_mod

    caption_dir = get_project_caption_dir(project)
    if not caption_dir.exists():
        return {"batches": []}

    batches: list[dict] = []
    for user_dir in sorted(caption_dir.iterdir()):
        if not user_dir.is_dir():
            continue
        csv_path = user_dir / "captions.csv"
        if not csv_path.exists():
            continue

        # Read captions from CSV
        captions: list[dict] = []
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv_mod.DictReader(f)
                for row in reader:
                    captions.append({
                        "text": row.get("caption", ""),
                        "mood": row.get("mood", None),
                        "video_id": row.get("video_id", ""),
                    })
        except Exception as e:
            log.warning("Failed to read caption CSV %s: %s", csv_path, e)

        stat = csv_path.stat()
        batches.append({
            "username": user_dir.name,
            "caption_count": len(captions),
            "modified_at": stat.st_mtime,
            "captions": captions,
            "sample": [c["text"] for c in captions[:3]],
        })

    # Sort by most recent first
    batches.sort(key=lambda b: b["modified_at"], reverse=True)
    return {"batches": batches}


@router.post("/rename-batch")
async def rename_caption_batch(body: dict):
    """Rename a caption batch (username folder).

    Body: {"project": "...", "old_name": "...", "new_name": "..."}
    """
    project = body.get("project", "quick-test")
    old_name = body.get("old_name", "")
    new_name = body.get("new_name", "").strip()

    if not old_name or not new_name:
        raise HTTPException(400, "Both old_name and new_name are required")

    # Sanitize new_name
    new_name = new_name.replace("/", "").replace("\\", "").replace("..", "").strip()
    if not new_name:
        raise HTTPException(400, "Invalid new name")

    caption_dir = get_project_caption_dir(project)
    old_path = caption_dir / old_name
    new_path = caption_dir / new_name

    if not old_path.exists():
        raise HTTPException(404, f"Batch '{old_name}' not found")
    if new_path.exists():
        raise HTTPException(409, f"Batch '{new_name}' already exists")

    import shutil
    shutil.move(str(old_path), str(new_path))
    return {"renamed": True, "old_name": old_name, "new_name": new_name}
