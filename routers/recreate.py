"""Recreate router — extract frames from TikTok videos, remove burned-in text."""

import asyncio
import base64
import json
import shutil
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from project_manager import get_project_recreate_dir

router = APIRouter()

# ── WebSocket client registry ────────────────────────────────────────
_ws_clients: dict[str, list[WebSocket]] = {}


async def _send(job_id: str, event: str, data: dict):
    """Broadcast a JSON event to all WebSocket clients for this job."""
    clients = _ws_clients.get(job_id, [])
    msg = json.dumps({"event": event, **data})
    dead: list[WebSocket] = []
    for ws in clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.remove(ws)


def _image_to_data_uri(path: Path) -> str:
    """Convert an image file to a base64 data URI string."""
    data = path.read_bytes()
    b64 = base64.b64encode(data).decode()
    ext = path.suffix.lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(
        ext, "image/png"
    )
    return f"data:{mime};base64,{b64}"


async def _get_video_duration(video_path: Path) -> float:
    """Run ffprobe to get video duration in seconds."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {stderr.decode(errors='replace')[:300]}")
    return float(stdout.decode().strip())


# ── Pipeline ─────────────────────────────────────────────────────────


async def _run_pipeline(job_id: str, video_url: str, project: str):
    """Download video, extract first/last frames, remove text from each."""
    from scraper.frame_extractor import download_video, extract_frame
    from providers.replicate import remove_text

    recreate_dir = get_project_recreate_dir(project)
    job_dir = recreate_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Phase 1: Download video
        await _send(job_id, "downloading", {"text": "Downloading video..."})
        video_path = job_dir / "source_video.mp4"
        await download_video(video_url, video_path)

        # Phase 2: Extract first and last frames
        await _send(job_id, "extracting_frames", {"text": "Extracting frames..."})
        duration = await _get_video_duration(video_path)

        first_frame = job_dir / "first_frame_original.jpg"
        last_frame = job_dir / "last_frame_original.jpg"

        await extract_frame(video_path, first_frame, timestamp=0.0)
        last_ts = max(0.0, duration - 0.1)
        await extract_frame(video_path, last_frame, timestamp=last_ts)

        first_b64 = _image_to_data_uri(first_frame)
        last_b64 = _image_to_data_uri(last_frame)

        await _send(job_id, "frames_ready", {
            "first_frame_b64": first_b64,
            "last_frame_b64": last_b64,
        })

        # Phase 3: Remove text from each frame
        async with httpx.AsyncClient() as client:
            # First frame
            await _send(job_id, "removing_text", {
                "text": "Removing text from first frame...",
                "frame": "first",
            })
            first_clean_url = await remove_text(first_b64, client=client)
            # Download cleaned image
            resp = await client.get(first_clean_url, timeout=30)
            first_clean_path = job_dir / "first_frame_clean.png"
            first_clean_path.write_bytes(resp.content)
            first_clean_b64 = _image_to_data_uri(first_clean_path)
            await _send(job_id, "text_removed", {
                "frame": "first",
                "clean_b64": first_clean_b64,
            })

            # Last frame
            await _send(job_id, "removing_text", {
                "text": "Removing text from last frame...",
                "frame": "last",
            })
            last_clean_url = await remove_text(last_b64, client=client)
            resp = await client.get(last_clean_url, timeout=30)
            last_clean_path = job_dir / "last_frame_clean.png"
            last_clean_path.write_bytes(resp.content)
            last_clean_b64 = _image_to_data_uri(last_clean_path)
            await _send(job_id, "text_removed", {
                "frame": "last",
                "clean_b64": last_clean_b64,
            })

        # Phase 4: Complete
        await _send(job_id, "complete", {
            "job_id": job_id,
            "first_frame_clean": str(first_clean_path),
            "last_frame_clean": str(last_clean_path),
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        await _send(job_id, "error", {"error": str(e)})


# ── WebSocket endpoint ────────────────────────────────────────────────


@router.websocket("/ws/{job_id}")
async def websocket_recreate(ws: WebSocket, job_id: str):
    """WebSocket endpoint for real-time recreate pipeline progress."""
    await ws.accept()
    _ws_clients.setdefault(job_id, []).append(ws)
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            if msg.get("action") == "start":
                asyncio.create_task(
                    _run_pipeline(
                        job_id,
                        msg["video_url"],
                        msg.get("project", "quick-test"),
                    )
                )
    except WebSocketDisconnect:
        pass
    finally:
        clients = _ws_clients.get(job_id, [])
        if ws in clients:
            clients.remove(ws)


# ── REST endpoints ────────────────────────────────────────────────────


@router.get("/jobs")
async def list_recreate_jobs(project: str = Query(default="quick-test")):
    """List completed recreate jobs for a project."""
    recreate_dir = get_project_recreate_dir(project)
    jobs: list[dict] = []

    if not recreate_dir.exists():
        return {"jobs": jobs}

    for job_dir in sorted(recreate_dir.iterdir()):
        if not job_dir.is_dir():
            continue
        # Only include jobs that have at least the first cleaned frame
        if not (job_dir / "first_frame_clean.png").exists():
            continue
        jobs.append({
            "job_id": job_dir.name,
            "has_first_clean": True,
            "has_last_clean": (job_dir / "last_frame_clean.png").exists(),
            "has_source_video": (job_dir / "source_video.mp4").exists(),
        })

    return {"jobs": jobs}


@router.delete("/jobs/{job_id}")
async def delete_recreate_job(
    job_id: str,
    project: str = Query(default="quick-test"),
):
    """Delete a recreate job and all its files."""
    recreate_dir = get_project_recreate_dir(project)
    job_dir = recreate_dir / job_id

    if not job_dir.exists() or not job_dir.is_dir():
        raise HTTPException(404, "Job not found")

    shutil.rmtree(job_dir)
    return {"deleted": True, "job_id": job_id}
