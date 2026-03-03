"""Recreate router — extract frames from TikTok videos, remove burned-in text."""

import asyncio
import base64
import json
import os
import shutil
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from project_manager import get_project_recreate_dir

# ── OpenAI client (lazy singleton, same pattern as caption_extractor) ─
_openai_client = None


def _get_openai():
    global _openai_client
    if _openai_client is None:
        from openai import AsyncOpenAI
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set")
        _openai_client = AsyncOpenAI(api_key=key)
    return _openai_client

router = APIRouter()

# ── WebSocket client registry ────────────────────────────────────────
_ws_clients: dict[str, list[WebSocket]] = {}


async def _send(job_id: str, event: str, data: dict):
    """Broadcast a JSON event to all WebSocket clients for this job."""
    clients = _ws_clients.get(job_id, [])
    # Truncate base64 fields for logging
    log_data = {k: (f"{v[:40]}..." if isinstance(v, str) and len(v) > 50 else v) for k, v in data.items()}
    print(f"[recreate] _send({job_id[:8]}): event={event}, clients={len(clients)}, data_keys={list(log_data.keys())}")
    msg = json.dumps({"event": event, **data})
    dead: list[WebSocket] = []
    for ws in clients:
        try:
            await ws.send_text(msg)
        except Exception as e:
            print(f"[recreate] _send failed for client: {e}")
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
            "first_frame": first_b64,
            "last_frame": last_b64,
            "duration": duration,
        })

        # Phase 3: Remove text from each frame (local mask + LaMa inpainting)
        async with httpx.AsyncClient() as client:
            # First frame
            await _send(job_id, "removing_text", {
                "text": "Generating text mask & inpainting first frame (LaMa)...",
            })
            first_clean_url = await remove_text(first_b64, client=client)
            await _send(job_id, "status", {
                "text": "Downloading cleaned first frame...",
            })
            resp = await client.get(first_clean_url, timeout=30)
            first_clean_path = job_dir / "first_frame_clean.png"
            first_clean_path.write_bytes(resp.content)
            first_clean_b64 = _image_to_data_uri(first_clean_path)

            # Last frame
            await _send(job_id, "removing_text", {
                "text": "Generating text mask & inpainting last frame (LaMa)...",
            })
            last_clean_url = await remove_text(last_b64, client=client)
            await _send(job_id, "status", {
                "text": "Downloading cleaned last frame...",
            })
            resp = await client.get(last_clean_url, timeout=30)
            last_clean_path = job_dir / "last_frame_clean.png"
            last_clean_path.write_bytes(resp.content)
            last_clean_b64 = _image_to_data_uri(last_clean_path)

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

    except Exception as e:
        import traceback
        traceback.print_exc()
        await _send(job_id, "error", {"error": str(e)})


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
async def generate_prompt(req: GeneratePromptRequest):
    """Use GPT-4.1 vision to write a video generation prompt from clean frames."""
    if not req.first_frame or not req.last_frame:
        raise HTTPException(400, "Both first_frame and last_frame are required")

    client = _get_openai()
    resp = await client.chat.completions.create(
        model="gpt-4.1",
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
    return {"prompt": prompt}


# ── WebSocket endpoint ────────────────────────────────────────────────


@router.websocket("/ws/{job_id}")
async def websocket_recreate(ws: WebSocket, job_id: str):
    """WebSocket endpoint for real-time recreate pipeline progress."""
    await ws.accept()
    print(f"[recreate] WS connected: {job_id[:8]}")
    _ws_clients.setdefault(job_id, []).append(ws)
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            print(f"[recreate] WS received: action={msg.get('action')}, url={msg.get('video_url', '')[:60]}")
            if msg.get("action") == "start":
                asyncio.create_task(
                    _run_pipeline(
                        job_id,
                        msg["video_url"],
                        msg.get("project", "quick-test"),
                    )
                )
    except WebSocketDisconnect:
        print(f"[recreate] WS disconnected: {job_id[:8]}")
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
