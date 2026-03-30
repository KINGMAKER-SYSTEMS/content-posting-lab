"""Recreate router — extract frames from TikTok videos, remove burned-in text."""

import asyncio
import base64
import json
import logging
import os
import shutil
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from project_manager import get_project_recreate_dir

log = logging.getLogger("recreate")
log.setLevel(logging.DEBUG)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[recreate] %(levelname)s  %(message)s"))
    log.addHandler(_h)

# ── OpenAI client (lazy singleton, same pattern as caption_extractor) ─
_openai_client = None


def _get_openai():
    global _openai_client
    if _openai_client is None:
        from openai import AsyncOpenAI
        key = (os.getenv("OPENAI_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set")
        _openai_client = AsyncOpenAI(api_key=key)
        log.info("OpenAI client initialized (key len=%d)", len(key))
    return _openai_client

router = APIRouter()

# ── WebSocket client registry ────────────────────────────────────────
_ws_clients: dict[str, list[WebSocket]] = {}


async def _send(job_id: str, event: str, data: dict):
    """Broadcast a JSON event to all WebSocket clients for this job."""
    clients = _ws_clients.get(job_id, [])
    # Truncate base64 fields for logging
    log_data = {k: (f"{v[:40]}..." if isinstance(v, str) and len(v) > 50 else v) for k, v in data.items()}
    log.debug("_send(%s): event=%s, clients=%d, data_keys=%s", job_id[:8], event, len(clients), list(log_data.keys()))
    msg = json.dumps({"event": event, **data})
    dead: list[WebSocket] = []
    for ws in clients:
        try:
            await ws.send_text(msg)
        except Exception as e:
            log.warning("_send failed for client: %s", e)
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
    log.debug("_image_to_data_uri: %s → %d bytes, %d chars b64", path.name, len(data), len(b64))
    return f"data:{mime};base64,{b64}"


async def _get_video_duration(video_path: Path) -> float:
    """Run ffprobe to get video duration in seconds."""
    log.debug("ffprobe: %s", video_path)
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
        err_msg = stderr.decode(errors='replace')[:300]
        log.error("ffprobe failed (rc=%d): %s", proc.returncode, err_msg)
        raise RuntimeError(f"ffprobe failed: {err_msg}")
    duration = float(stdout.decode().strip())
    log.info("ffprobe: duration=%.2fs", duration)
    return duration


# ── Pipeline ─────────────────────────────────────────────────────────


async def _remove_text_with_retry(
    image_b64: str, client: httpx.AsyncClient, label: str, max_retries: int = 3,
) -> str:
    """Call remove_text with retry logic for transient Replicate API failures."""
    from providers.replicate import remove_text

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            url = await remove_text(image_b64, client=client)
            return url
        except Exception as e:
            last_error = e
            log.warning("remove_text %s attempt %d/%d failed: %s", label, attempt, max_retries, e)
            if attempt < max_retries:
                await asyncio.sleep(2 * attempt)  # Exponential backoff: 2s, 4s
    raise RuntimeError(f"Text removal failed for {label} after {max_retries} attempts: {last_error}")


async def _download_cleaned_image(
    client: httpx.AsyncClient, url: str, dest: Path, label: str,
) -> None:
    """Download a cleaned image from Replicate CDN with retry."""
    for attempt in range(1, 4):
        try:
            resp = await client.get(url, timeout=60)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            return
        except Exception as e:
            log.warning("download %s attempt %d failed: %s", label, attempt, e)
            if attempt < 3:
                await asyncio.sleep(2)
    raise RuntimeError(f"Failed to download cleaned {label} from {url[:80]}")


async def _run_pipeline(job_id: str, video_url: str, project: str):
    """Download video, extract first/last frames, remove text from each."""
    from scraper.frame_extractor import download_video, extract_frame

    t0 = time.time()
    log.info("pipeline START job=%s url=%s project=%s", job_id[:8], video_url[:80], project)

    recreate_dir = get_project_recreate_dir(project)
    job_dir = recreate_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Phase 1: Download video
        t1 = time.time()
        await _send(job_id, "downloading", {"text": "Downloading video..."})
        video_path = job_dir / "source_video.mp4"
        try:
            await asyncio.wait_for(download_video(video_url, video_path), timeout=120)
        except asyncio.TimeoutError:
            raise RuntimeError("Video download timed out after 2 minutes. Try a shorter video or check the URL.")
        if not video_path.exists() or video_path.stat().st_size < 1000:
            raise RuntimeError("Video download failed — file is empty or missing. Check if the URL is accessible.")
        log.info("phase1 download: %.1fs, size=%d bytes", time.time() - t1, video_path.stat().st_size)

        # Phase 2: Extract first and last frames
        t2 = time.time()
        await _send(job_id, "extracting_frames", {"text": "Extracting frames..."})
        try:
            duration = await _get_video_duration(video_path)
        except Exception as e:
            raise RuntimeError(f"Could not read video duration — file may be corrupted: {e}")

        first_frame = job_dir / "first_frame_original.jpg"
        last_frame = job_dir / "last_frame_original.jpg"

        await extract_frame(video_path, first_frame, timestamp=0.0)
        if not first_frame.exists():
            raise RuntimeError("Failed to extract first frame from video")

        last_ts = max(0.0, duration - 0.1)
        await extract_frame(video_path, last_frame, timestamp=last_ts)
        if not last_frame.exists():
            raise RuntimeError("Failed to extract last frame from video")

        first_b64 = _image_to_data_uri(first_frame)
        last_b64 = _image_to_data_uri(last_frame)
        log.info("phase2 frames: %.1fs, first=%d chars, last=%d chars", time.time() - t2, len(first_b64), len(last_b64))

        await _send(job_id, "frames_ready", {
            "first_frame": first_b64,
            "last_frame": last_b64,
            "duration": duration,
        })

        # Phase 3: Remove text from each frame (local mask + LaMa inpainting)
        t3 = time.time()
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15)) as client:
            # First frame
            await _send(job_id, "removing_text", {
                "text": "Removing text from first frame (attempt 1)...",
            })
            log.debug("remove_text: first frame (%d chars)", len(first_b64))
            first_clean_url = await _remove_text_with_retry(first_b64, client, "first frame")
            log.info("remove_text: first frame done → %s", first_clean_url[:120] if first_clean_url else "None")

            await _send(job_id, "status", {"text": "Downloading cleaned first frame..."})
            first_clean_path = job_dir / "first_frame_clean.png"
            await _download_cleaned_image(client, first_clean_url, first_clean_path, "first frame")
            first_clean_b64 = _image_to_data_uri(first_clean_path)

            # Last frame
            await _send(job_id, "removing_text", {
                "text": "Removing text from last frame...",
            })
            log.debug("remove_text: last frame (%d chars)", len(last_b64))
            last_clean_url = await _remove_text_with_retry(last_b64, client, "last frame")
            log.info("remove_text: last frame done → %s", last_clean_url[:120] if last_clean_url else "None")

            await _send(job_id, "status", {"text": "Downloading cleaned last frame..."})
            last_clean_path = job_dir / "last_frame_clean.png"
            await _download_cleaned_image(client, last_clean_url, last_clean_path, "last frame")
            last_clean_b64 = _image_to_data_uri(last_clean_path)

            log.info("phase3 text removal: %.1fs, first_clean=%d chars, last_clean=%d chars", time.time() - t3, len(first_clean_b64), len(last_clean_b64))

            await _send(job_id, "text_removed", {
                "first_clean": first_clean_b64,
                "last_clean": last_clean_b64,
            })

        # Phase 4: Complete — send all base64 images so frontend can display them
        await _send(job_id, "complete", {
            "job_id": job_id,
            "first_original": first_b64,
            "last_original": last_b64,
            "first_clean": first_clean_b64,
            "last_clean": last_clean_b64,
        })
        log.info("pipeline COMPLETE job=%s total=%.1fs", job_id[:8], time.time() - t0)

    except Exception as e:
        import traceback
        log.error("pipeline FAILED job=%s after %.1fs: %s", job_id[:8], time.time() - t0, e)
        traceback.print_exc()
        try:
            await _send(job_id, "error", {"error": str(e)})
        except Exception:
            log.error("Failed to send error event to WebSocket")


# ── Prompt generation ─────────────────────────────────────────────────

_PROMPT_GEN_SYSTEM = (
    "You are a video generation prompt writer. You will receive the first and "
    "last frames of a short TikTok-style video. Analyze both frames to understand:\n"
    "1. The scene, setting, and subjects\n"
    "2. Any motion or transition implied between the first and last frames\n"
    "3. Camera angle and movement\n"
    "4. Lighting, mood, and visual style\n\n"
    "Write a concise 2-4 sentence video generation prompt that would recreate "
    "this video. The prompt should describe the action/motion, not just the "
    "static scene. Write in present tense, be specific about visual details. "
    "Do NOT mention 'first frame' or 'last frame' — write as if describing "
    "the full video. Output ONLY the prompt text, nothing else."
)


class GeneratePromptRequest(BaseModel):
    first_frame: str
    last_frame: str


@router.post("/generate-prompt")
async def generate_prompt(request: Request, req: GeneratePromptRequest):
    """Use GPT-4o vision to write a video generation prompt from clean frames."""
    content_length = request.headers.get("content-length", "?")
    log.info(
        "generate-prompt: content-length=%s, first_frame=%d chars, last_frame=%d chars",
        content_length, len(req.first_frame), len(req.last_frame),
    )

    if not req.first_frame or not req.last_frame:
        log.warning("generate-prompt: empty frames")
        raise HTTPException(400, "Both first_frame and last_frame are required")

    # Validate data URI format
    for label, uri in [("first_frame", req.first_frame), ("last_frame", req.last_frame)]:
        if not uri.startswith("data:image/"):
            log.error("generate-prompt: %s is not a data URI (starts with %r)", label, uri[:60])
            raise HTTPException(400, f"{label} must be a data:image/... URI, got: {uri[:60]}...")

    try:
        client = _get_openai()
    except RuntimeError as e:
        log.error("generate-prompt: OpenAI client init failed: %s", e)
        raise HTTPException(500, str(e))

    try:
        t0 = time.time()
        log.info("generate-prompt: calling OpenAI gpt-4o ...")
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": _PROMPT_GEN_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": req.first_frame}},
                        {"type": "image_url", "image_url": {"url": req.last_frame}},
                        {
                            "type": "text",
                            "text": "Write a video generation prompt for recreating this video.",
                        },
                    ],
                },
            ],
            max_tokens=300,
            temperature=0.7,
        )
        prompt = resp.choices[0].message.content.strip()
        log.info(
            "generate-prompt: OK in %.1fs, tokens=%s, prompt=%r",
            time.time() - t0,
            getattr(resp.usage, "total_tokens", "?"),
            prompt[:80],
        )
        return {"prompt": prompt}
    except Exception as e:
        import traceback
        log.error("generate-prompt: OpenAI call FAILED: %s", e)
        traceback.print_exc()
        detail = str(e)
        if hasattr(e, "status_code"):
            detail = f"OpenAI API error ({e.status_code}): {detail}"
        raise HTTPException(502, detail)


# ── WebSocket endpoint ────────────────────────────────────────────────


@router.websocket("/ws/{job_id}")
async def websocket_recreate(ws: WebSocket, job_id: str):
    """WebSocket endpoint for real-time recreate pipeline progress."""
    await ws.accept()
    log.info("WS connected: %s", job_id[:8])
    _ws_clients.setdefault(job_id, []).append(ws)
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            log.info("WS received: action=%s, url=%s", msg.get("action"), msg.get("video_url", "")[:60])
            if msg.get("action") == "start":
                asyncio.create_task(
                    _run_pipeline(
                        job_id,
                        msg["video_url"],
                        msg.get("project", "quick-test"),
                    )
                )
    except WebSocketDisconnect:
        log.info("WS disconnected: %s", job_id[:8])
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
        first_clean = job_dir / "first_frame_clean.png"
        if not first_clean.exists():
            continue

        entry: dict = {"job_id": job_dir.name}

        # Return base64 data URIs for all available frames
        for key, filename in [
            ("first_original", "first_frame_original.jpg"),
            ("last_original", "last_frame_original.jpg"),
            ("first_clean", "first_frame_clean.png"),
            ("last_clean", "last_frame_clean.png"),
        ]:
            fpath = job_dir / filename
            entry[key] = _image_to_data_uri(fpath) if fpath.exists() else None

        jobs.append(entry)

    log.info("list jobs: project=%s, count=%d", project, len(jobs))
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
    log.info("deleted job %s", job_id[:8])
    return {"deleted": True, "job_id": job_id}
