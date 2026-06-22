import asyncio
import base64
import json as _json
import random
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Safe filename pattern — alphanumeric, dash, underscore, dot only
_SAFE_FILENAME_RE = re.compile(r"^[a-zA-Z0-9_\-][a-zA-Z0-9_\-\.]*$")

from project_manager import (
    get_global_sounds_dir,
    get_project_slideshow_audio_dir,
    get_project_slideshow_dir,
    get_project_slideshow_formats_dir,
    get_project_slideshow_images_dir,
    get_project_video_dir,
    sanitize_project_name,
)
from services import sound_cache
from services.captions import scan_project_captions
from services.cropper import crop_to_916
from services.fsutil import safe_rmtree
from services.json_store import atomic_save

router = APIRouter(tags=["slideshow"])

jobs: dict[str, dict] = {}

VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VALID_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg"}
VALID_VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv"}

_UNSAFE_SLUG_RE = re.compile(r"[^a-zA-Z0-9_\-]")


def safe_slug(name: str) -> str:
    """Sanitize a user-supplied name into a filesystem-safe slug (<=64 chars)."""
    return _UNSAFE_SLUG_RE.sub("_", name.strip())[:64]


# ── Image management ────────────────────────────────────────────────────


@router.post("/upload")
async def upload_images(
    project: str = Form(...),
    files: list[UploadFile] = File(...),
):
    images_dir = get_project_slideshow_images_dir(project)
    saved = 0
    results = []

    for f in files:
        if not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext not in VALID_EXTENSIONS:
            continue

        # Save raw then crop
        safe_name = f"{uuid.uuid4().hex[:12]}{ext}"
        raw_path = images_dir / f"raw_{safe_name}"
        cropped_path = images_dir / safe_name

        content = await f.read()
        raw_path.write_bytes(content)

        try:
            crop_to_916(raw_path, cropped_path)
            raw_path.unlink(missing_ok=True)
            saved += 1
            results.append({
                "name": safe_name,
                "original_name": f.filename,
            })
        except Exception as e:
            raw_path.unlink(missing_ok=True)
            cropped_path.unlink(missing_ok=True)
            results.append({
                "name": None,
                "original_name": f.filename,
                "error": str(e),
            })

    return {"saved": saved, "results": results}


@router.get("/images")
async def list_images(project: str):
    images_dir = get_project_slideshow_images_dir(project)
    if not images_dir.exists():
        return {"images": []}

    images = []
    for p in sorted(images_dir.iterdir()):
        if p.suffix.lower() in VALID_EXTENSIONS and not p.name.startswith("raw_"):
            images.append({
                "name": p.name,
                "path": str(p.relative_to(images_dir.parent.parent)),
            })

    return {"images": images}


@router.delete("/images/{filename}")
async def delete_image(filename: str, project: str):
    if not _SAFE_FILENAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    images_dir = get_project_slideshow_images_dir(project)
    target = images_dir / filename
    # Prevent path traversal
    try:
        target.resolve().relative_to(images_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    target.unlink()
    return {"deleted": filename}


@router.delete("/images")
async def delete_all_images(project: str):
    images_dir = get_project_slideshow_images_dir(project)
    if images_dir.exists():
        count = 0
        for p in images_dir.iterdir():
            if p.suffix.lower() in VALID_EXTENSIONS:
                p.unlink()
                count += 1
        return {"deleted": count}
    return {"deleted": 0}


# ── Rendering ────────────────────────────────────────────────────────────


class SlideConfig(BaseModel):
    image: str  # filename in slideshow-images/
    duration: float  # seconds


class RenderRequest(BaseModel):
    project: str
    slides: list[SlideConfig]
    transition: str = "none"  # future: "fade", "crossfade"
    fps: int = 30


def _build_slideshow_ffmpeg(
    slides: list[SlideConfig],
    images_dir: Path,
    output_path: Path,
    fps: int = 30,
) -> list[str]:
    """Build FFmpeg command for a photo slideshow video (no audio)."""
    filter_parts: list[str] = []
    inputs: list[str] = []

    for i, slide in enumerate(slides):
        if not _SAFE_FILENAME_RE.match(slide.image):
            raise ValueError(f"Invalid image filename: {slide.image}")
        img_path = images_dir / slide.image
        # Prevent path traversal
        try:
            img_path.resolve().relative_to(images_dir.resolve())
        except ValueError:
            raise ValueError(f"Invalid image path: {slide.image}")
        if not img_path.exists():
            raise FileNotFoundError(f"Image not found: {slide.image}")
        inputs += ["-loop", "1", "-t", f"{slide.duration:.3f}", "-i", str(img_path)]
        filter_parts.append(
            f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,setsar=1,fps={fps}[s{i}]"
        )

    concat_in = "".join(f"[s{i}]" for i in range(len(slides)))
    filter_parts.append(f"{concat_in}concat=n={len(slides)}:v=1:a=0[out]")

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-movflags", "+faststart",
        str(output_path),
    ]
    return cmd


def _run_render(job_id: str, project: str, slides: list[SlideConfig], fps: int):
    """Synchronous render — runs in thread pool."""
    try:
        jobs[job_id] = {
            "status": "running",
            "progress": 10,
            "message": "Building slideshow...",
        }

        images_dir = get_project_slideshow_images_dir(project)
        output_dir = get_project_slideshow_dir(project)
        output_name = f"slideshow_{job_id[:8]}.mp4"
        output_path = output_dir / output_name

        cmd = _build_slideshow_ffmpeg(slides, images_dir, output_path, fps)

        jobs[job_id] = {
            "status": "running",
            "progress": 30,
            "message": "Rendering with FFmpeg...",
        }

        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")[-2000:]
            raise RuntimeError(f"FFmpeg failed: {stderr}")

        jobs[job_id] = {
            "status": "complete",
            "progress": 100,
            "message": f"Done: {output_name}",
            "output": output_name,
            "path": str(output_path.relative_to(output_dir.parent.parent.parent)),
        }

    except Exception as e:
        jobs[job_id] = {
            "status": "error",
            "progress": 0,
            "message": str(e),
        }


@router.post("/render")
async def start_render(body: RenderRequest):
    if not body.slides:
        raise HTTPException(status_code=400, detail="No slides provided")

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "pending",
        "progress": 0,
        "message": "Queued...",
    }

    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        None, _run_render, job_id, body.project, body.slides, body.fps
    )
    return {"job_id": job_id}


@router.get("/job/{job_id}")
async def get_render_job(job_id: str):
    return jobs.get(job_id, {"status": "not_found"})


@router.get("/renders")
async def list_renders(project: str):
    output_dir = get_project_slideshow_dir(project)
    if not output_dir.exists():
        return {"renders": []}

    renders = []
    for p in sorted(output_dir.iterdir(), reverse=True):
        if p.suffix == ".mp4":
            renders.append({
                "name": p.name,
                "path": str(p.relative_to(output_dir.parent.parent.parent)),
            })

    return {"renders": renders}


@router.delete("/renders/{filename}")
async def delete_render(filename: str, project: str):
    if not _SAFE_FILENAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    output_dir = get_project_slideshow_dir(project)
    target = output_dir / filename
    try:
        target.resolve().relative_to(output_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not target.exists():
        raise HTTPException(status_code=404, detail="Render not found")
    target.unlink()
    return {"deleted": filename}


# ── Audio management ──────────────────────────────────────────────────────


@router.post("/audio/upload")
async def upload_audio(
    project: str = Form(...),
    file: UploadFile = File(...),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")
    ext = Path(file.filename).suffix.lower()
    if ext not in VALID_AUDIO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid audio format. Allowed: {', '.join(VALID_AUDIO_EXTENSIONS)}",
        )

    audio_dir = get_project_slideshow_audio_dir(project)
    safe_name = f"{uuid.uuid4().hex[:12]}{ext}"
    target = audio_dir / safe_name

    content = await file.read()
    target.write_bytes(content)

    return {
        "name": safe_name,
        "original_name": file.filename,
        "path": str(target.relative_to(audio_dir.parent.parent)),
    }


@router.get("/audio")
async def list_audio(project: str):
    audio_dir = get_project_slideshow_audio_dir(project)
    if not audio_dir.exists():
        return {"audio": []}

    files = []
    for p in sorted(audio_dir.iterdir()):
        if p.suffix.lower() in VALID_AUDIO_EXTENSIONS:
            files.append({
                "name": p.name,
                "path": str(p.relative_to(audio_dir.parent.parent)),
            })
    return {"audio": files}


@router.delete("/audio/{filename}")
async def delete_audio(filename: str, project: str):
    if not _SAFE_FILENAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    audio_dir = get_project_slideshow_audio_dir(project)
    target = audio_dir / filename
    try:
        target.resolve().relative_to(audio_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not target.exists():
        raise HTTPException(status_code=404, detail="Audio not found")
    target.unlink()
    return {"deleted": filename}


# ── Render V2: Two-Block Slideshow ────────────────────────────────────────


class BlockOneConfig(BaseModel):
    images: list[str]
    duration: float
    shuffle_speed: float = 0.4
    overlay_png: str | None = None


class BlockTwoConfig(BaseModel):
    source: str
    source_type: str  # "image" or "video"
    duration: float


class RenderV2Request(BaseModel):
    project: str
    block1: BlockOneConfig
    block2: BlockTwoConfig
    audio: str | None = None
    fps: int = 30


def _validate_image(filename: str, images_dir: Path) -> Path:
    """Validate and return resolved path for an image filename."""
    if not _SAFE_FILENAME_RE.match(filename):
        raise ValueError(f"Invalid image filename: {filename}")
    img_path = images_dir / filename
    try:
        img_path.resolve().relative_to(images_dir.resolve())
    except ValueError:
        raise ValueError(f"Invalid image path: {filename}")
    if not img_path.exists():
        raise FileNotFoundError(f"Image not found: {filename}")
    return img_path


def _build_block1_cmd(
    images: list[str],
    images_dir: Path,
    duration: float,
    shuffle_speed: float,
    overlay_path: Path | None,
    output_path: Path,
    fps: int = 30,
    beats: list[float] | None = None,
) -> list[str]:
    """Build FFmpeg command for Block 1: image shuffle + optional text overlay.

    If `beats` is provided, photo switches land on the beat timestamps.
    Otherwise, photos cycle at fixed `shuffle_speed` intervals.
    """
    # Validate all images first
    image_paths = [_validate_image(img, images_dir) for img in images]
    if not image_paths:
        raise ValueError("Block 1 needs at least one image")

    # Shuffle once per call (each batch item gets a different photo order)
    shuffled = list(image_paths)
    random.shuffle(shuffled)

    # Build (image, segment_duration) pairs
    sequence: list[tuple[Path, float]] = []

    if beats:
        # Beat-synced mode: one photo per beat interval
        # If there are more beats than photos, photos cycle via modulo.
        # Safety cap: merge adjacent beats if total > 100 (ffmpeg filter_complex
        # starts getting unwieldy beyond that).
        usable_beats = [t for t in beats if 0.0 < t <= duration]
        if len(usable_beats) > 100:
            # Keep every other beat
            usable_beats = usable_beats[::2]

        prev_t = 0.0
        for i, t in enumerate(usable_beats):
            seg_dur = t - prev_t
            if seg_dur < 0.05:  # skip too-short segments
                continue
            sequence.append((shuffled[i % len(shuffled)], seg_dur))
            prev_t = t
        # Tail segment: from last beat to end of duration
        if prev_t < duration - 0.01:
            tail_img = shuffled[len(sequence) % len(shuffled)]
            sequence.append((tail_img, duration - prev_t))
    else:
        # Fixed-interval mode: cycle photos at shuffle_speed cadence
        remaining = duration
        i = 0
        while remaining > 0.05:
            seg_dur = min(shuffle_speed, remaining)
            sequence.append((shuffled[i % len(shuffled)], seg_dur))
            remaining -= shuffle_speed
            i += 1

    if not sequence:
        raise ValueError("Block 1 produced no segments — check duration and beats")

    # Build ffmpeg inputs and filter
    filter_parts: list[str] = []
    inputs: list[str] = []

    for i, (img_path, seg_dur) in enumerate(sequence):
        inputs += ["-loop", "1", "-t", f"{seg_dur:.3f}", "-i", str(img_path)]
        filter_parts.append(
            f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,setsar=1,fps={fps}[s{i}]"
        )

    input_idx = len(sequence)

    # Concat all image segments
    concat_in = "".join(f"[s{i}]" for i in range(input_idx))
    filter_parts.append(f"{concat_in}concat=n={input_idx}:v=1:a=0[shuffled]")

    # If overlay, add it as the last input and composite
    if overlay_path and overlay_path.exists():
        inputs += ["-i", str(overlay_path)]
        overlay_idx = input_idx
        filter_parts.append(
            f"[{overlay_idx}:v]scale=1080:1920:flags=lanczos[ovr];"
            f"[shuffled][ovr]overlay=0:0[out]"
        )
        map_label = "[out]"
    else:
        map_label = "[shuffled]"

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", map_label,
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-movflags", "+faststart",
        str(output_path),
    ]
    return cmd


def _build_block2_cmd(
    source: str,
    source_type: str,
    source_dir: Path,
    duration: float,
    output_path: Path,
    fps: int = 30,
) -> list[str]:
    """Build FFmpeg command for Block 2: single static image or video clip."""
    if not _SAFE_FILENAME_RE.match(source):
        raise ValueError(f"Invalid source filename: {source}")
    source_path = source_dir / source
    try:
        source_path.resolve().relative_to(source_dir.resolve())
    except ValueError:
        raise ValueError(f"Invalid source path: {source}")
    if not source_path.exists():
        raise FileNotFoundError(f"Source not found: {source}")

    if source_type == "image":
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-t", f"{duration:.3f}",
            "-i", str(source_path),
            "-vf", f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps={fps}",
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-movflags", "+faststart",
            str(output_path),
        ]
    else:
        # Video: trim to duration, scale to 1080x1920
        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1",
            "-t", f"{duration:.3f}",
            "-i", str(source_path),
            "-vf", f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps={fps}",
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-an",
            "-movflags", "+faststart",
            str(output_path),
        ]
    return cmd


def _assemble_final_cmd(
    block1_path: Path,
    block2_path: Path,
    audio_path: Path | None,
    output_path: Path,
    total_duration: float,
) -> list[str]:
    """Concat two blocks and mux audio into final output."""
    # Use ffmpeg concat protocol via filter
    cmd = [
        "ffmpeg", "-y",
        "-i", str(block1_path),
        "-i", str(block2_path),
    ]

    if audio_path and audio_path.exists():
        cmd += ["-i", str(audio_path)]
        # Concat video streams, map audio from input 2, trim to total duration
        cmd += [
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[outv]",
            "-map", "[outv]",
            "-map", "2:a",
            "-t", f"{total_duration:.3f}",
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(output_path),
        ]
    else:
        # No audio — just concat
        cmd += [
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[outv]",
            "-map", "[outv]",
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(output_path),
        ]

    return cmd


def _run_render_v2(job_id: str, body: RenderV2Request):
    """Synchronous V2 render — runs in thread pool."""
    tmp_dir = None
    try:
        jobs[job_id] = {"status": "running", "progress": 5, "message": "Preparing..."}

        images_dir = get_project_slideshow_images_dir(body.project)
        output_dir = get_project_slideshow_dir(body.project)
        output_name = f"slideshow_{job_id[:8]}.mp4"
        output_path = output_dir / output_name

        tmp_dir = tempfile.mkdtemp(prefix="slideshow_v2_")
        tmp = Path(tmp_dir)
        block1_path = tmp / "block1.mp4"
        block2_path = tmp / "block2.mp4"

        # Decode overlay PNG if provided
        overlay_path: Path | None = None
        if body.block1.overlay_png:
            overlay_path = tmp / "overlay.png"
            png_data = body.block1.overlay_png
            if png_data.startswith("data:"):
                png_data = png_data.split(",", 1)[1]
            overlay_path.write_bytes(base64.b64decode(png_data))

        # Resolve audio path
        audio_path: Path | None = None
        if body.audio:
            audio_dir = get_project_slideshow_audio_dir(body.project)
            candidate = audio_dir / body.audio
            if candidate.exists():
                audio_path = candidate

        # ── Pass 1: Block 1 (shuffle + overlay) ──
        jobs[job_id] = {"status": "running", "progress": 15, "message": "Rendering Block 1 (shuffle)..."}
        cmd1 = _build_block1_cmd(
            body.block1.images, images_dir, body.block1.duration,
            body.block1.shuffle_speed, overlay_path, block1_path, body.fps,
        )
        result1 = subprocess.run(cmd1, capture_output=True, timeout=600)
        if result1.returncode != 0:
            stderr = result1.stderr.decode(errors="replace")[-2000:]
            raise RuntimeError(f"Block 1 FFmpeg failed: {stderr}")

        # ── Pass 2: Block 2 (static) ──
        jobs[job_id] = {"status": "running", "progress": 50, "message": "Rendering Block 2 (static)..."}

        # Determine source directory for Block 2
        if body.block2.source_type == "video":
            video_dir = get_project_video_dir(body.project)
            b2_source_dir = video_dir
        else:
            b2_source_dir = images_dir

        cmd2 = _build_block2_cmd(
            body.block2.source, body.block2.source_type,
            b2_source_dir, body.block2.duration, block2_path, body.fps,
        )
        result2 = subprocess.run(cmd2, capture_output=True, timeout=600)
        if result2.returncode != 0:
            stderr = result2.stderr.decode(errors="replace")[-2000:]
            raise RuntimeError(f"Block 2 FFmpeg failed: {stderr}")

        # ── Pass 3: Assemble final ──
        jobs[job_id] = {"status": "running", "progress": 80, "message": "Assembling final video..."}
        total_duration = body.block1.duration + body.block2.duration
        cmd3 = _assemble_final_cmd(block1_path, block2_path, audio_path, output_path, total_duration)
        result3 = subprocess.run(cmd3, capture_output=True, timeout=600)
        if result3.returncode != 0:
            stderr = result3.stderr.decode(errors="replace")[-2000:]
            raise RuntimeError(f"Assembly FFmpeg failed: {stderr}")

        jobs[job_id] = {
            "status": "complete",
            "progress": 100,
            "message": f"Done: {output_name}",
            "output": output_name,
            "path": str(output_path.relative_to(output_dir.parent.parent.parent)),
        }

    except Exception as e:
        jobs[job_id] = {"status": "error", "progress": 0, "message": str(e)}
    finally:
        if tmp_dir:
            safe_rmtree(tmp_dir)


@router.post("/render-v2")
async def start_render_v2(body: RenderV2Request):
    if not body.block1.images:
        raise HTTPException(status_code=400, detail="Block 1 needs at least one image")
    if not body.block2.source:
        raise HTTPException(status_code=400, detail="Block 2 needs a source")
    if body.block2.source_type not in ("image", "video"):
        raise HTTPException(status_code=400, detail="Block 2 source_type must be 'image' or 'video'")

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending", "progress": 0, "message": "Queued..."}

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_render_v2, job_id, body)
    return {"job_id": job_id}


# ── Project videos listing (for Block 2 video picker) ────────────────────


@router.get("/project-videos")
async def list_project_videos(project: str):
    """List MP4 files in project videos directory for Block 2 video picker."""
    video_dir = get_project_video_dir(project)
    if not video_dir.exists():
        return {"videos": []}

    videos = []
    for p in sorted(video_dir.rglob("*.mp4")):
        # Skip slideshow renders themselves
        if "slideshow" in str(p.relative_to(video_dir)):
            continue
        videos.append({
            "name": p.name,
            "path": str(p.relative_to(video_dir)),
            "full_path": str(p.relative_to(video_dir.parent.parent)),
        })
    return {"videos": videos}


# ── Captions proxy (shared with burn router) ─────────────────────────────


@router.get("/captions")
async def list_captions(project: str):
    """List caption sources available for this project (from scraped TikTok data)."""
    return {"sources": scan_project_captions(project)}


# ── Campaign Sound Cache (beat-synced slideshows) ────────────────────────


class PrepareSoundRequest(BaseModel):
    telegram_sound_id: str
    label: str


@router.post("/sounds/prepare")
async def prepare_sound_endpoint(body: PrepareSoundRequest):
    """Match a campaign sound to a Campaign Hub video and cache its audio + beats.

    - Matches telegram sound label → Campaign Hub campaign title
    - Pulls the highest-viewed matched_videos URL
    - Downloads with yt-dlp, extracts audio with ffmpeg
    - Runs librosa beat detection
    - Caches MP3 + beats JSON under projects/sounds/{tiktok_sound_id}.*
    - Subsequent calls for the same TikTok sound_id hit the cache instantly
    """
    try:
        return await sound_cache.prepare_sound(body.telegram_sound_id, body.label)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to prepare sound: {e}")


@router.get("/sounds/{sound_id}/audio")
async def serve_sound_audio(sound_id: str):
    """Stream the cached MP3 for browser audio preview playback."""
    # TikTok sound IDs are numeric — hard validation prevents path traversal
    if not sound_id.isdigit():
        raise HTTPException(status_code=400, detail="Invalid sound_id")
    path = get_global_sounds_dir() / f"{sound_id}.mp3"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Sound not cached")
    return FileResponse(path, media_type="audio/mpeg")


# ── Meme Mode: Batch Render ──────────────────────────────────────────────


class MemeRenderRequest(BaseModel):
    project: str
    images: list[str]
    batch_size: int
    duration: float
    shuffle_speed: float = 0.4  # ignored when beats provided
    audio: str | None = None  # legacy: per-project uploaded audio
    sound_id: str | None = None  # NEW: TikTok sound_id from /sounds/prepare
    beats: list[float] | None = None  # NEW: beat timestamps for on-beat cuts
    fps: int = 30


def _mux_audio(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    duration: float,
) -> None:
    """Mux an audio track onto a video, trimming to duration."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{duration:.3f}",
        "-movflags", "+faststart",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=300)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[-2000:]
        raise RuntimeError(f"Audio mux failed: {stderr}")


def _run_meme_batch(job_id: str, body: MemeRenderRequest):
    """Render a batch of meme slideshows (Block 1 only, no caption overlay)."""
    tmp_dir = None
    try:
        jobs[job_id] = {
            "status": "running",
            "progress": 0,
            "message": "Starting batch...",
            "batch_size": body.batch_size,
            "completed": 0,
            "items": [
                {"index": i, "status": "pending"}
                for i in range(body.batch_size)
            ],
        }

        images_dir = get_project_slideshow_images_dir(body.project)
        output_dir = get_project_slideshow_dir(body.project)
        short_id = job_id[:8]

        # Resolve audio path once — prefer campaign sound cache, fall back to
        # per-project upload. Beat sync only applies when sound_id is set.
        audio_path: Path | None = None
        if body.sound_id and body.sound_id.isdigit():
            candidate = get_global_sounds_dir() / f"{body.sound_id}.mp3"
            if candidate.exists():
                audio_path = candidate
        if audio_path is None and body.audio:
            audio_dir = get_project_slideshow_audio_dir(body.project)
            candidate = audio_dir / body.audio
            if candidate.exists():
                audio_path = candidate

        for i in range(body.batch_size):
            tmp_dir = tempfile.mkdtemp(prefix=f"meme_{short_id}_{i}_")
            tmp = Path(tmp_dir)

            jobs[job_id]["items"][i]["status"] = "running"
            jobs[job_id]["message"] = f"Rendering {i + 1}/{body.batch_size}..."
            jobs[job_id]["progress"] = int((i / body.batch_size) * 100)

            output_name = f"meme_{short_id}_{i:03d}.mp4"
            block1_path = tmp / "block1.mp4"
            final_path = output_dir / output_name

            try:
                # Build block1 — shuffles images per call; uses beats if provided
                cmd = _build_block1_cmd(
                    body.images, images_dir, body.duration,
                    body.shuffle_speed, None, block1_path, body.fps,
                    beats=body.beats,
                )
                result = subprocess.run(cmd, capture_output=True, timeout=600)
                if result.returncode != 0:
                    stderr = result.stderr.decode(errors="replace")[-2000:]
                    raise RuntimeError(f"FFmpeg failed: {stderr}")

                # Mux audio if provided
                if audio_path:
                    _mux_audio(block1_path, audio_path, final_path, body.duration)
                else:
                    shutil.move(str(block1_path), str(final_path))

                jobs[job_id]["items"][i] = {
                    "index": i,
                    "status": "complete",
                    "output": output_name,
                }
            except Exception as e:
                jobs[job_id]["items"][i] = {
                    "index": i,
                    "status": "error",
                    "error": str(e),
                }
            finally:
                safe_rmtree(tmp_dir)
                tmp_dir = None

            jobs[job_id]["completed"] = sum(
                1 for it in jobs[job_id]["items"] if it["status"] in ("complete", "error")
            )

        jobs[job_id]["status"] = "complete"
        jobs[job_id]["progress"] = 100
        ok = sum(1 for it in jobs[job_id]["items"] if it["status"] == "complete")
        jobs[job_id]["message"] = f"Done: {ok}/{body.batch_size} rendered"

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["progress"] = 0
        jobs[job_id]["message"] = str(e)


@router.post("/render-meme")
async def start_meme_render(body: MemeRenderRequest):
    if not body.images:
        raise HTTPException(status_code=400, detail="No images selected")
    if body.batch_size < 1 or body.batch_size > 50:
        raise HTTPException(status_code=400, detail="Batch size must be 1-50")

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending", "progress": 0, "message": "Queued..."}

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_meme_batch, job_id, body)
    return {"job_id": job_id}


# ── Format Save/Load ────────────────────────────────────────────────────


class SaveFormatRequest(BaseModel):
    project: str
    name: str
    mode: str  # "meme" or "fan-page"
    config: dict


@router.post("/formats")
async def save_format(body: SaveFormatRequest):
    fmt_dir = get_project_slideshow_formats_dir(body.project)
    safe_name = safe_slug(body.name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid format name")

    fmt_path = fmt_dir / f"{safe_name}.json"
    now = datetime.now(timezone.utc).isoformat()

    # Preserve created_at if updating existing format
    created_at = now
    if fmt_path.exists():
        try:
            existing = _json.loads(fmt_path.read_text(encoding="utf-8"))
            created_at = existing.get("created_at", now)
        except Exception:
            pass

    data = {
        "name": body.name.strip(),
        "mode": body.mode,
        "created_at": created_at,
        "updated_at": now,
        "config": body.config,
    }
    atomic_save(fmt_path, data)
    return {"saved": safe_name, "format": data}


@router.get("/formats")
async def list_formats(project: str):
    fmt_dir = get_project_slideshow_formats_dir(project)
    formats = []
    for p in sorted(fmt_dir.iterdir()):
        if p.suffix == ".json":
            try:
                data = _json.loads(p.read_text(encoding="utf-8"))
                formats.append(data)
            except Exception:
                continue
    return {"formats": formats}


@router.get("/formats/{name}")
async def get_format(name: str, project: str):
    fmt_dir = get_project_slideshow_formats_dir(project)
    safe_name = safe_slug(name)
    fmt_path = fmt_dir / f"{safe_name}.json"
    if not fmt_path.exists():
        raise HTTPException(status_code=404, detail="Format not found")
    data = _json.loads(fmt_path.read_text(encoding="utf-8"))
    return {"format": data}


@router.delete("/formats/{name}")
async def delete_format(name: str, project: str):
    fmt_dir = get_project_slideshow_formats_dir(project)
    safe_name = safe_slug(name)
    fmt_path = fmt_dir / f"{safe_name}.json"
    if not fmt_path.exists():
        raise HTTPException(status_code=404, detail="Format not found")
    fmt_path.unlink()
    return {"deleted": safe_name}


# ════════════════════════════════════════════════════════════════════════
# Agent-facing: B-roll Overlay Montage
#
# A single-pass render that composites a fast-cut photo montage on top of a
# b-roll base layer at controllable opacity. Designed to be driven entirely
# by agents — every spec is dialable via the request body, with sane
# defaults. The b-roll can come from anywhere an agent can reach: a project
# video filename, an absolute path on the volume, or a URL (fetched with
# yt-dlp, falling back to a direct download). Composes with the clipper —
# point it at a clip the clipper produced, or a YouTube/TikTok URL directly.
#
# GET  /api/slideshow/broll-montage/spec   → self-describing parameter schema
# POST /api/slideshow/broll-montage        → start render, returns {job_id}
# GET  /api/slideshow/job/{job_id}         → poll status (shared with v2)
# ════════════════════════════════════════════════════════════════════════

WIDTH, HEIGHT = 1080, 1920

# How the photo montage is blended over the b-roll base.
#   normal  → flat alpha (colorchannelmixer aa); photos sit at `opacity`
#   screen  → lighten/light-leak; only brighter photo pixels punch through
#   overlay → contrast-y blend (ffmpeg blend=all_mode=overlay)
#   multiply, lighten, darken, addition → passthrough to ffmpeg blend modes
_BLEND_MODES = {
    "normal", "screen", "overlay", "multiply",
    "lighten", "darken", "addition",
}


class BrollMontageRequest(BaseModel):
    project: str
    # Photo set: explicit filenames in the project's slideshow-images dir.
    # If omitted, ALL images in that dir are used.
    images: list[str] | None = None

    # B-roll base layer — one of these (filename takes precedence, then
    # path, then url). At least one is required.
    broll_filename: str | None = None   # in project's videos/ dir
    broll_path: str | None = None       # absolute path on the volume
    broll_url: str | None = None        # fetched via yt-dlp / direct download

    # Output spec
    duration: float = 8.0               # seconds
    # Cut speed — supply EITHER (photo_fps wins if both given):
    photo_fps: float | None = None      # photo cuts per second (e.g. 5.5)
    shuffle_speed: float | None = None  # seconds per photo (e.g. 0.18)

    # Blend
    opacity: float = 0.40               # photo montage opacity over b-roll
    blend_mode: str = "normal"

    # Layout / motion
    output_fps: int = 30                # encode framerate of the final video
    seed: int | None = None             # photo shuffle seed; None = random each call
    shuffle: bool = True                # shuffle photo order (False = as-given)
    broll_loop: bool = True             # loop b-roll if shorter than duration
    broll_start: float = 0.0            # seek into b-roll before using it

    # Audio (optional) — a filename in the project's slideshow-audio dir
    audio: str | None = None

    # Encode quality
    crf: int = 18


def _resolve_broll(req: BrollMontageRequest, tmp: Path) -> Path:
    """Resolve the b-roll source to a local file path.

    Precedence: broll_filename (project videos dir) → broll_path (absolute)
    → broll_url (yt-dlp, then direct download fallback). Raises ValueError
    with an agent-readable message on any failure.
    """
    # 1) Project video filename
    if req.broll_filename:
        if not _SAFE_FILENAME_RE.match(req.broll_filename):
            raise ValueError(f"Invalid broll_filename: {req.broll_filename}")
        video_dir = get_project_video_dir(req.project)
        candidate = video_dir / req.broll_filename
        try:
            candidate.resolve().relative_to(video_dir.resolve())
        except ValueError:
            raise ValueError(f"broll_filename escapes project dir: {req.broll_filename}")
        if not candidate.exists():
            raise ValueError(f"broll_filename not found in project videos: {req.broll_filename}")
        return candidate

    # 2) Absolute path on the volume
    if req.broll_path:
        p = Path(req.broll_path)
        if not p.is_absolute():
            raise ValueError("broll_path must be absolute")
        if not p.exists():
            raise ValueError(f"broll_path does not exist: {req.broll_path}")
        if p.suffix.lower() not in VALID_VIDEO_EXTENSIONS:
            raise ValueError(f"broll_path is not a recognized video: {p.suffix}")
        return p

    # 3) URL — try yt-dlp first (handles YouTube/TikTok/etc), then direct curl
    if req.broll_url:
        url = req.broll_url
        if not re.match(r"^https?://", url):
            raise ValueError("broll_url must be http(s)")
        out_stub = tmp / "broll_src"

        # yt-dlp path (mirrors services/sound_cache.py flags + cookies)
        ytdlp_cmd = [
            "yt-dlp",
            "--no-warnings",
            "--no-playlist",
            "--no-check-certificates",
            "-f", "bv*+ba/b",
            "-o", f"{out_stub}.%(ext)s",
        ]
        try:
            from scraper.frame_extractor import get_cookies_path
            cookies = get_cookies_path()
            if cookies:
                ytdlp_cmd += ["--cookies", str(cookies)]
        except Exception:
            pass
        ytdlp_cmd.append(url)

        r = subprocess.run(ytdlp_cmd, capture_output=True, timeout=180)
        if r.returncode == 0:
            files = [p for p in tmp.iterdir() if p.stem == "broll_src"]
            if files:
                return files[0]

        # Direct download fallback (plain CDN mp4 links)
        dl = out_stub.with_suffix(".mp4")
        r2 = subprocess.run(
            ["curl", "-sL", "--max-time", "120",
             "-A", "Mozilla/5.0", "-o", str(dl), url],
            capture_output=True, timeout=130,
        )
        if r2.returncode == 0 and dl.exists() and dl.stat().st_size > 10000:
            return dl

        err = r.stderr.decode(errors="replace")[-400:]
        raise ValueError(f"Could not fetch broll_url via yt-dlp or direct download. {err}")

    raise ValueError("Provide one of: broll_filename, broll_path, broll_url")


def _build_broll_montage_cmd(
    photos: list[Path],
    broll: Path,
    output_path: Path,
    *,
    duration: float,
    seg_dur: float,
    opacity: float,
    blend_mode: str,
    output_fps: int,
    seed: int | None,
    shuffle: bool,
    broll_loop: bool,
    broll_start: float,
    audio_path: Path | None,
    crf: int,
) -> tuple[list[str], int]:
    """Build the single-pass ffmpeg command. Returns (cmd, num_segments).

    Ported from the verified prototype: b-roll base (input 0), photo montage
    concat'd at the cut cadence, blended over the base, optional audio mux.
    """
    order = list(photos)
    if shuffle:
        random.Random(seed).shuffle(order)

    n = max(1, round(duration / seg_dur))
    seq = [order[i % len(order)] for i in range(n)]

    inputs: list[str] = []
    parts: list[str] = []

    # input 0 = b-roll base
    base_in: list[str] = []
    if broll_loop:
        base_in += ["-stream_loop", "-1"]
    if broll_start > 0:
        base_in += ["-ss", f"{broll_start}"]
    base_in += ["-t", f"{duration}", "-i", str(broll)]
    inputs += base_in
    parts.append(
        f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},setsar=1,fps={output_fps},"
        f"trim=duration={duration},setpts=PTS-STARTPTS[base]"
    )

    # photo inputs → montage
    for i, p in enumerate(seq):
        idx = i + 1
        inputs += ["-loop", "1", "-t", f"{seg_dur:.4f}", "-i", str(p)]
        parts.append(
            f"[{idx}:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},setsar=1,fps={output_fps}[p{i}]"
        )
    concat_in = "".join(f"[p{i}]" for i in range(len(seq)))
    parts.append(f"{concat_in}concat=n={len(seq)}:v=1:a=0[montage]")
    parts.append(f"[montage]trim=duration={duration},setpts=PTS-STARTPTS[m]")

    # blend montage over base
    if blend_mode == "normal":
        parts.append(f"[m]format=yuva420p,colorchannelmixer=aa={opacity}[ma]")
        parts.append(f"[base][ma]overlay=0:0:format=auto[out]")
    else:
        # ffmpeg blend modes; opacity applied via blend's all_opacity
        parts.append(
            f"[base][m]blend=all_mode={blend_mode}:all_opacity={opacity}[out]"
        )

    filter_complex = ";".join(parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
    ]
    # optional audio: add as another input and map it
    audio_map: list[str] = []
    if audio_path is not None:
        cmd += ["-i", str(audio_path)]
        audio_idx = len(seq) + 1  # base(0) + photos(1..n) → audio is next
        audio_map = ["-map", f"{audio_idx}:a", "-c:a", "aac", "-b:a", "192k", "-shortest"]

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[out]",
        *audio_map,
        "-t", f"{duration}",
        "-c:v", "libx264", "-crf", str(crf), "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-r", str(output_fps),
        "-movflags", "+faststart",
        str(output_path),
    ]
    return cmd, len(seq)


def _run_broll_montage(job_id: str, req: BrollMontageRequest):
    """Synchronous render — runs in thread pool."""
    tmp_dir = None
    try:
        jobs[job_id] = {"status": "running", "progress": 5, "message": "Resolving inputs..."}
        tmp_dir = tempfile.mkdtemp(prefix="broll_montage_")
        tmp = Path(tmp_dir)

        images_dir = get_project_slideshow_images_dir(req.project)

        # Resolve photo set
        if req.images:
            photos = [_validate_image(img, images_dir) for img in req.images]
        else:
            photos = sorted(images_dir.glob("*.jpg")) + sorted(images_dir.glob("*.jpeg")) \
                + sorted(images_dir.glob("*.png")) + sorted(images_dir.glob("*.webp"))
        if not photos:
            raise RuntimeError("No photos found. Upload to slideshow-images or pass `images`.")

        # Cut cadence: photo_fps wins, else shuffle_speed, else default 5.5fps
        if req.photo_fps and req.photo_fps > 0:
            seg_dur = 1.0 / req.photo_fps
        elif req.shuffle_speed and req.shuffle_speed > 0:
            seg_dur = req.shuffle_speed
        else:
            seg_dur = 1.0 / 5.5
        seg_dur = max(0.02, min(seg_dur, req.duration))  # clamp sane

        if req.blend_mode not in _BLEND_MODES:
            raise RuntimeError(f"blend_mode must be one of {sorted(_BLEND_MODES)}")

        # Resolve b-roll
        jobs[job_id] = {"status": "running", "progress": 20, "message": "Fetching b-roll..."}
        broll = _resolve_broll(req, tmp)

        # Resolve audio (optional)
        audio_path: Path | None = None
        if req.audio:
            audio_dir = get_project_slideshow_audio_dir(req.project)
            cand = audio_dir / req.audio
            if cand.exists():
                audio_path = cand

        output_dir = get_project_slideshow_dir(req.project)
        output_name = f"broll_montage_{job_id[:8]}.mp4"
        output_path = output_dir / output_name

        jobs[job_id] = {"status": "running", "progress": 40, "message": "Rendering montage..."}
        cmd, nseg = _build_broll_montage_cmd(
            photos, broll, output_path,
            duration=req.duration, seg_dur=seg_dur,
            opacity=req.opacity, blend_mode=req.blend_mode,
            output_fps=req.output_fps, seed=req.seed, shuffle=req.shuffle,
            broll_loop=req.broll_loop, broll_start=req.broll_start,
            audio_path=audio_path, crf=req.crf,
        )
        result = subprocess.run(cmd, capture_output=True, timeout=600)
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")[-2000:]
            raise RuntimeError(f"FFmpeg failed: {stderr}")

        jobs[job_id] = {
            "status": "complete",
            "progress": 100,
            "message": f"Done: {output_name}",
            "output": output_name,
            "path": str(output_path.relative_to(output_dir.parent.parent.parent)),
            "spec": {
                "photos": len(photos),
                "segments": nseg,
                "seg_dur_ms": round(seg_dur * 1000),
                "effective_fps": round(1.0 / seg_dur, 2),
                "opacity": req.opacity,
                "blend_mode": req.blend_mode,
                "duration": req.duration,
            },
        }
    except (ValueError, RuntimeError) as e:
        jobs[job_id] = {"status": "error", "progress": 0, "message": str(e)}
    except Exception as e:  # noqa: BLE001 — surface anything to the agent
        jobs[job_id] = {"status": "error", "progress": 0, "message": f"Unexpected: {e}"}
    finally:
        if tmp_dir:
            safe_rmtree(tmp_dir)


@router.get("/broll-montage/spec")
async def broll_montage_spec():
    """Self-describing parameter schema so agents can discover every knob."""
    return {
        "endpoint": "POST /api/slideshow/broll-montage",
        "description": (
            "Composite a fast-cut photo montage over a b-roll base layer at "
            "controllable opacity. 9:16 vertical. Returns {job_id}; poll "
            "GET /api/slideshow/job/{job_id} until status is 'complete' or 'error'."
        ),
        "parameters": {
            "project": {"type": "string", "required": True, "desc": "Project name (scopes photos/output)."},
            "images": {"type": "list[str]", "required": False, "default": "all images in slideshow-images",
                       "desc": "Filenames in the project's slideshow-images dir. Omit to use all."},
            "broll_filename": {"type": "string", "required": False, "desc": "B-roll in the project's videos/ dir."},
            "broll_path": {"type": "string", "required": False, "desc": "Absolute path to a b-roll video on the volume."},
            "broll_url": {"type": "string", "required": False,
                          "desc": "URL fetched via yt-dlp (YouTube/TikTok/etc), falling back to direct download. Compose with the clipper."},
            "_broll_note": "Provide exactly one b-roll source. Precedence: filename > path > url.",
            "duration": {"type": "float", "default": 8.0, "desc": "Output length in seconds."},
            "photo_fps": {"type": "float", "default": None, "desc": "Photo cuts per second (e.g. 5.5). Wins over shuffle_speed."},
            "shuffle_speed": {"type": "float", "default": None, "desc": "Seconds per photo (e.g. 0.18). Used if photo_fps absent."},
            "_cadence_note": "If neither photo_fps nor shuffle_speed given, defaults to 5.5 fps.",
            "opacity": {"type": "float", "default": 0.40, "desc": "Photo montage opacity over the b-roll (0..1)."},
            "blend_mode": {"type": "string", "default": "normal", "enum": sorted(_BLEND_MODES),
                           "desc": "normal=flat alpha; screen=light-leak; others=ffmpeg blend modes."},
            "output_fps": {"type": "int", "default": 30, "desc": "Encode framerate of the final video."},
            "seed": {"type": "int", "default": None, "desc": "Photo shuffle seed. None = random each call."},
            "shuffle": {"type": "bool", "default": True, "desc": "Shuffle photo order. False = use images order as given."},
            "broll_loop": {"type": "bool", "default": True, "desc": "Loop b-roll if shorter than duration."},
            "broll_start": {"type": "float", "default": 0.0, "desc": "Seek into b-roll (seconds) before using."},
            "audio": {"type": "string", "default": None, "desc": "Filename in the project's slideshow-audio dir to mux."},
            "crf": {"type": "int", "default": 18, "desc": "x264 quality (lower=better, 18 visually lossless)."},
        },
        "blend_modes": sorted(_BLEND_MODES),
        "defaults_example": {
            "project": "my-project",
            "broll_url": "https://www.youtube.com/watch?v=...",
            "photo_fps": 5.5,
            "opacity": 0.40,
            "duration": 8.0,
        },
    }


@router.post("/broll-montage")
async def start_broll_montage(req: BrollMontageRequest):
    # Validate b-roll source presence early (clear 400 instead of async error)
    if not (req.broll_filename or req.broll_path or req.broll_url):
        raise HTTPException(
            status_code=400,
            detail="Provide one b-roll source: broll_filename, broll_path, or broll_url.",
        )
    if req.opacity < 0 or req.opacity > 1:
        raise HTTPException(status_code=400, detail="opacity must be between 0 and 1.")
    if req.duration <= 0 or req.duration > 120:
        raise HTTPException(status_code=400, detail="duration must be between 0 and 120 seconds.")

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending", "progress": 0, "message": "Queued..."}
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_broll_montage, job_id, req)
    return {"job_id": job_id}
