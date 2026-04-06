"""Clipper router — download any video link and chop it into 8-15s 9:16 UGC clips."""

import asyncio
import json
import logging
import math
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
import zipfile
from zipfile import ZipFile

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile, File, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from project_manager import PROJECTS_DIR, sanitize_project_name

log = logging.getLogger("clipper")
log.setLevel(logging.DEBUG)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[clipper] %(levelname)s  %(message)s"))
    log.addHandler(_h)

router = APIRouter()

# ── WebSocket client registry ────────────────────────────────────────
_ws_clients: dict[str, list[WebSocket]] = {}


def _make_clip_job_id(project: str = "clip") -> str:
    """Generate a readable clip job ID: clip-{project}-{MMDDHHmm}-{short_uuid}."""
    prefix = project.lower().replace(" ", "-")[:20]
    ts = datetime.now().strftime("%m%d%H%M")
    short = uuid.uuid4().hex[:4]
    return f"clip-{prefix}-{ts}-{short}"


def _get_clipper_dir(project: str) -> Path:
    sanitized = sanitize_project_name(project)
    d = PROJECTS_DIR / sanitized / "clips"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _send(job_id: str, event: str, data: dict):
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


async def _generate_thumbnail(video_path: Path, thumb_path: Path, seek: float = -1) -> bool:
    """Extract a single JPEG frame for use as a poster/thumbnail.

    Uses -ss after -i for accurate frame-level seeking.
    Tries multiple seek points to avoid blank intro frames.
    """
    # Get duration to calculate smart seek points
    duration = 0.0
    if seek < 0:
        try:
            info = await _get_video_info(video_path)
            duration = info.get("duration", 0)
        except Exception:
            pass

    # Try multiple seek points: 10%, 2s, 5s, 0.5s
    if seek >= 0:
        seek_points = [seek]
    elif duration > 0:
        seek_points = [duration * 0.1, 2.0, 5.0, 0.5]
        seek_points = [s for s in seek_points if s < duration]
        if not seek_points:
            seek_points = [0.1]
    else:
        seek_points = [2.0, 0.5, 0.1]

    min_thumb_size = 2000  # bytes — below this likely a blank/black frame

    for sp in seek_points:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-ss", f"{sp:.3f}",
            "-vframes", "1",
            "-q:v", "3",
            "-vf", "scale=270:480:force_original_aspect_ratio=decrease",
            str(thumb_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0 and thumb_path.exists():
            if thumb_path.stat().st_size >= min_thumb_size:
                return True
            log.debug("thumbnail at %.1fs too small (%d bytes), trying next seek",
                      sp, thumb_path.stat().st_size)

    # Accept whatever we got even if small
    if thumb_path.exists():
        return True
    log.warning("thumbnail failed for %s: %s", video_path, stderr.decode(errors="replace")[-200:])
    return False


async def _faststart(mp4_path: Path) -> None:
    """Move moov atom to front of mp4 for instant browser playback/seeking."""
    tmp = mp4_path.with_suffix(".faststart.mp4")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(mp4_path),
        "-c", "copy", "-movflags", "+faststart",
        str(tmp),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode == 0 and tmp.exists():
        tmp.replace(mp4_path)
    else:
        tmp.unlink(missing_ok=True)
        log.warning("faststart failed: %s", stderr.decode(errors="replace")[-200:])


async def _get_video_info(video_path: Path) -> dict:
    """Get duration, dimensions, and rotation via ffprobe.

    Returns the *displayed* width/height (after rotation is applied),
    since ffmpeg auto-rotates when decoding.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration",
        "-show_entries", "stream_side_data=rotation,displaymatrix",
        "-show_entries", "format=duration",
        "-of", "json",
        str(video_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"ffprobe timed out after 15s — file may be corrupt: {video_path.name}")
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {stderr.decode(errors='replace')[:300]}")

    info = json.loads(stdout.decode())
    log.debug("ffprobe raw: %s", json.dumps(info)[:800])
    duration = None
    width = None
    height = None
    rotation = 0

    # Try stream-level duration and dimensions
    streams = info.get("streams", [])
    for s in streams:
        if s.get("width"):
            width = int(s["width"])
            height = int(s.get("height", 0))
            dur_str = s.get("duration")
            if dur_str:
                d = float(dur_str)
                if d > 0:
                    duration = d
            # Check for rotation in side_data_list
            for sd in s.get("side_data_list", []):
                if "rotation" in sd:
                    rotation = int(float(sd["rotation"]))
            break

    # If rotation is 90 or -90 (or 270), swap width/height to get displayed dimensions
    # ffmpeg auto-applies rotation when decoding, so filters see the rotated frame
    if abs(rotation) in (90, 270, -90, -270):
        log.info("Video has rotation=%d — swapping %dx%d → %dx%d", rotation, width, height, height, width)
        width, height = height, width

    # Fallback to format-level duration
    if not duration:
        fmt = info.get("format", {})
        dur_str = fmt.get("duration")
        if dur_str:
            d = float(dur_str)
            if d > 0:
                duration = d

    # Last resort: raw format probe
    if not duration:
        log.info("ffprobe json had no duration, trying raw format probe...")
        proc2 = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout2, _ = await proc2.communicate()
        raw = stdout2.decode().strip()
        if raw:
            d = float(raw)
            if d > 0:
                duration = d

    if not duration or duration <= 0:
        raise RuntimeError("Could not determine video duration — is this a valid video file?")

    if duration < 1.0:
        raise RuntimeError(
            f"File duration is only {duration:.2f}s — this looks like an image, not a video. "
            "Please upload or link an actual video file (MP4, MOV, MKV, WEBM)."
        )

    log.info("video info: duration=%.2f, displayed=%dx%d, rotation=%d", duration, width or 0, height or 0, rotation)
    return {"duration": duration, "width": width or 1920, "height": height or 1080}


async def _clip_segment(
    source: Path,
    output: Path,
    start: float,
    clip_duration: float,
    src_width: int,
    src_height: int,
) -> bool:
    """Cut a segment. Only crop/scale to 9:16 if the source isn't already 9:16."""
    out_w, out_h = 1080, 1920
    target_ratio = out_w / out_h  # 0.5625

    src_ratio = src_width / src_height if src_height else 1.0
    is_already_9_16 = abs(src_ratio - target_ratio) < 0.02  # ~1% tolerance

    # Use fast preset on deployed environments (RAILWAY_ENVIRONMENT set) or large files
    # slow preset causes proxy timeouts on Railway due to long encode times
    import os
    source_size = source.stat().st_size if source.exists() else 0
    is_deployed = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_SERVICE_ID"))
    preset = "fast" if is_deployed or source_size > 500 * 1024 * 1024 else "slow"

    log.info(
        "clip_segment: src=%dx%d ratio=%.4f target=%.4f diff=%.4f is_9_16=%s",
        src_width, src_height, src_ratio, target_ratio,
        abs(src_ratio - target_ratio), is_already_9_16,
    )

    if is_already_9_16 and src_width >= 1080 and src_height >= 1920:
        # Already 9:16 at full res — re-encode to H.264 for browser compatibility
        # (source may be HEVC which Chrome can't play)
        log.info("Source is already 9:16 (%dx%d) at full res, re-encoding to H.264 (preset=%s)", src_width, src_height, preset)
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            "-i", str(source),
            "-t", f"{clip_duration:.3f}",
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", "16",
            "-minrate", "8M",
            "-maxrate", "20M",
            "-bufsize", "20M",
            "-profile:v", "high",
            "-level", "4.2",
            "-pix_fmt", "yuv420p",
            "-an",
            "-movflags", "+faststart",
            str(output),
        ]
    elif is_already_9_16:
        # Already 9:16 but low res — scale up to 1080x1920 with high quality encode
        log.info("Source is 9:16 (%dx%d) but below 1080x1920, scaling up (preset=%s)", src_width, src_height, preset)
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            "-i", str(source),
            "-t", f"{clip_duration:.3f}",
            "-vf", f"scale=1080:1920:flags=lanczos",
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", "16",
            "-minrate", "8M",
            "-maxrate", "20M",
            "-bufsize", "20M",
            "-profile:v", "high",
            "-level", "4.2",
            "-pix_fmt", "yuv420p",
            "-an",
            "-movflags", "+faststart",
            str(output),
        ]
    else:
        # Need to crop to 9:16 and re-encode
        if src_ratio > target_ratio:
            # Source is wider — crop sides
            crop_h = src_height
            crop_w = int(src_height * target_ratio)
            crop_x = (src_width - crop_w) // 2
            crop_y = 0
            vf = f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},scale={out_w}:{out_h}:flags=lanczos"
        else:
            # Source is taller — crop top/bottom
            crop_w = src_width
            crop_h = int(src_width / target_ratio)
            crop_x = 0
            crop_y = (src_height - crop_h) // 2
            vf = f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},scale={out_w}:{out_h}:flags=lanczos"

        log.debug("cropping %dx%d → 9:16 with vf=%s (preset=%s)", src_width, src_height, vf, preset)
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            "-i", str(source),
            "-t", f"{clip_duration:.3f}",
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", "16",
            "-minrate", "8M",
            "-maxrate", "20M",
            "-bufsize", "20M",
            "-profile:v", "high",
            "-level", "4.2",
            "-pix_fmt", "yuv420p",
            "-an",
            "-movflags", "+faststart",
            str(output),
        ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        log.error("ffmpeg clip failed: %s", stderr.decode(errors="replace")[-300:])
        return False

    return output.exists()


async def _run_pipeline(
    job_id: str,
    video_url: str,
    project: str,
    clip_min: float,
    clip_max: float,
    strategy: str,
):
    """Download video and chop into clips."""
    from scraper.frame_extractor import download_video

    t0 = time.time()
    log.info("pipeline START job=%s url=%s", job_id[:8], video_url[:80])

    clipper_dir = _get_clipper_dir(project)
    job_dir = clipper_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Phase 1: Download
        await _send(job_id, "status", {"text": "Downloading video..."})
        source_path = job_dir / "source.mp4"
        await download_video(video_url, source_path)
        log.info("downloaded: %d bytes", source_path.stat().st_size if source_path.exists() else 0)

        # Phase 2: Probe video info
        await _send(job_id, "status", {"text": "Analyzing video..."})
        info = await _get_video_info(source_path)
        duration = info["duration"]
        src_w = info["width"]
        src_h = info["height"]
        log.info("video info: %.1fs, %dx%d", duration, src_w, src_h)

        await _send(job_id, "video_info", {
            "duration": duration,
            "width": src_w,
            "height": src_h,
        })

        # Phase 3: Calculate clip segments
        if strategy == "even":
            # Split evenly into clips within the min-max range
            clip_dur = min(clip_max, max(clip_min, duration))
            num_clips = max(1, math.floor(duration / clip_dur))
            # Adjust clip duration to fill evenly
            actual_dur = duration / num_clips
            if actual_dur > clip_max:
                num_clips += 1
                actual_dur = duration / num_clips
            segments = [(i * actual_dur, actual_dur) for i in range(num_clips)]
        else:
            # Sequential: chop from start using clip_max duration
            segments = []
            pos = 0.0
            while pos + clip_min <= duration:
                seg_dur = min(clip_max, duration - pos)
                if seg_dur < clip_min:
                    break
                segments.append((pos, seg_dur))
                pos += seg_dur

        total = len(segments)
        log.info("will create %d clips (strategy=%s)", total, strategy)
        await _send(job_id, "clip_plan", {"total": total, "segments": [
            {"start": s, "duration": d} for s, d in segments
        ]})

        # Phase 4: Cut clips
        clips: list[dict] = []
        for idx, (start, seg_dur) in enumerate(segments):
            clip_name = f"clip_{idx + 1:03d}.mp4"
            clip_path = job_dir / clip_name
            await _send(job_id, "clipping", {
                "index": idx,
                "total": total,
                "start": round(start, 2),
                "duration": round(seg_dur, 2),
            })

            ok = await _clip_segment(source_path, clip_path, start, seg_dur, src_w, src_h)
            clip_info = {
                "index": idx,
                "name": clip_name,
                "start": round(start, 2),
                "duration": round(seg_dur, 2),
                "ok": ok,
            }
            if ok:
                clip_info["url"] = f"/projects/{sanitize_project_name(project)}/clips/{job_id}/{clip_name}"
            clips.append(clip_info)

            await _send(job_id, "clipped", {
                "index": idx,
                "total": total,
                **clip_info,
            })

        # Phase 5: Complete
        await _send(job_id, "complete", {
            "job_id": job_id,
            "clips": clips,
            "total": total,
            "ok_count": sum(1 for c in clips if c["ok"]),
        })
        log.info("pipeline COMPLETE job=%s clips=%d total=%.1fs", job_id[:8], len(clips), time.time() - t0)

    except Exception as e:
        import traceback
        log.error("pipeline FAILED job=%s: %s", job_id[:8], e)
        traceback.print_exc()
        await _send(job_id, "error", {"error": str(e)})


# ── File upload endpoints ─────────────────────────────────────────────

# Max file size: 10 GB
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024 * 1024


@router.post("/upload-stream")
async def upload_video_stream(request: Request):
    """Streaming upload that bypasses multipart parsing — handles files >3GB.

    Query params: project, job_id (optional), filename (optional)
    Body: raw binary video data (Content-Type: application/octet-stream)
    """
    project = request.query_params.get("project", "quick-test")
    job_id = request.query_params.get("job_id") or str(uuid.uuid4())
    filename = request.query_params.get("filename", "video.mp4")

    clipper_dir = _get_clipper_dir(project)
    job_dir = clipper_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    ext = Path(filename).suffix or ".mp4"
    dest = job_dir / f"source{ext}"

    total_bytes = 0
    try:
        with open(dest, "wb") as f:
            async for chunk in request.stream():
                total_bytes += len(chunk)
                if total_bytes > _MAX_UPLOAD_BYTES:
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, f"File exceeds maximum size of {_MAX_UPLOAD_BYTES // (1024**3)}GB")
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        dest.unlink(missing_ok=True)
        log.error("streaming upload failed at %d bytes: %s", total_bytes, e)
        raise HTTPException(500, f"Upload failed after {total_bytes / (1024**3):.2f}GB: {e}")

    log.info("stream upload: %s → %s (%d bytes / %.2f GB)", filename, dest, total_bytes, total_bytes / (1024**3))
    return {"job_id": job_id, "path": str(dest), "size_bytes": total_bytes}


@router.post("/upload")
async def upload_video(
    file: UploadFile = File(...),
    project: str = Form(default="quick-test"),
    job_id: str = Form(default=""),
):
    """Accept a video file upload, store it in the job dir, return the local path."""
    if not job_id:
        job_id = str(uuid.uuid4())

    clipper_dir = _get_clipper_dir(project)
    job_dir = clipper_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Preserve original extension or default to .mp4
    ext = Path(file.filename or "video.mp4").suffix or ".mp4"
    dest = job_dir / f"source{ext}"

    # Stream to disk in 8MB chunks for better throughput on large files
    total_bytes = 0
    try:
        with open(dest, "wb") as f:
            while chunk := await file.read(8 * 1024 * 1024):  # 8MB chunks
                f.write(chunk)
                total_bytes += len(chunk)
                if total_bytes > _MAX_UPLOAD_BYTES:
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, f"File exceeds maximum size of {_MAX_UPLOAD_BYTES // (1024**3)}GB")
    except HTTPException:
        raise
    except Exception as e:
        dest.unlink(missing_ok=True)
        log.error("upload failed at %d bytes: %s", total_bytes, e)
        raise HTTPException(500, f"Upload failed after {total_bytes / (1024**2):.0f}MB: {e}")

    log.info("upload: %s → %s (%d bytes)", file.filename, dest, total_bytes)

    return {"job_id": job_id, "path": str(dest), "size_bytes": total_bytes}


@router.post("/upload-batch")
async def upload_batch(
    files: list[UploadFile] = File(...),
    project: str = Form(default="quick-test"),
):
    """Accept multiple video files, store each in a staging dir, return paths + info."""
    clipper_dir = _get_clipper_dir(project)
    batch_id = uuid.uuid4().hex[:12]
    staging_dir = clipper_dir / f"_staging_{batch_id}"
    staging_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    for i, file in enumerate(files):
        orig_ext = (Path(file.filename or "video.mp4").suffix or ".mp4").lower()
        # Always output as .mp4 for browser compatibility
        safe_name = f"src_{i:03d}.mp4"
        dest = staging_dir / safe_name

        # Stream to disk in 8MB chunks for better throughput on large files
        if orig_ext == ".mp4":
            total_bytes = 0
            with open(dest, "wb") as f:
                while chunk := await file.read(8 * 1024 * 1024):
                    f.write(chunk)
                    total_bytes += len(chunk)
            log.info("batch upload: %s → %s (%d bytes)", file.filename, dest, total_bytes)
            await _faststart(dest)
        else:
            # Non-MP4 (.mov, .mkv, .webm, etc): transcode to mp4 with faststart
            raw_path = staging_dir / f"_raw_{i:03d}{orig_ext}"
            total_bytes = 0
            with open(raw_path, "wb") as f:
                while chunk := await file.read(8 * 1024 * 1024):
                    f.write(chunk)
                    total_bytes += len(chunk)
            log.info("batch upload: %s (%d bytes) → transcoding to mp4...", file.filename, total_bytes)
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", str(raw_path),
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-movflags", "+faststart",
                str(dest),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                log.error("transcode failed for %s: %s", file.filename,
                          stderr.decode(errors="replace")[-300:])
                # Fallback: just copy the raw file
                import shutil
                shutil.move(str(raw_path), str(dest))
            else:
                raw_path.unlink(missing_ok=True)
                log.info("batch upload: %s → transcoded to %s", file.filename, dest)

        # Probe video info
        try:
            info = await _get_video_info(dest)
        except Exception as e:
            log.error("probe failed for %s: %s", file.filename, e)
            info = {"duration": 0, "width": 0, "height": 0}

        # Generate thumbnail
        thumb_name = f"thumb_{i:03d}.jpg"
        thumb_path = staging_dir / thumb_name
        await _generate_thumbnail(dest, thumb_path)

        sanitized = sanitize_project_name(project)
        results.append({
            "index": i,
            "original_name": file.filename,
            "path": str(dest),
            "url": f"/projects/{sanitized}/clips/_staging_{batch_id}/{safe_name}",
            "thumb_url": f"/projects/{sanitized}/clips/_staging_{batch_id}/{thumb_name}",
            "duration": info["duration"],
            "width": info["width"],
            "height": info["height"],
        })

    return {"batch_id": batch_id, "files": results}


@router.post("/download-url")
async def download_url(body: dict):
    """Download a video from URL into staging, return it as a staged file for trimming."""
    from scraper.frame_extractor import download_video

    video_url = body.get("video_url", "").strip()
    project = body.get("project", "quick-test")
    if not video_url:
        raise HTTPException(400, "video_url is required")

    clipper_dir = _get_clipper_dir(project)
    batch_id = uuid.uuid4().hex[:12]
    staging_dir = clipper_dir / f"_staging_{batch_id}"
    staging_dir.mkdir(parents=True, exist_ok=True)

    dest = staging_dir / "src_000.mp4"
    try:
        await download_video(video_url, dest)
    except Exception as e:
        raise HTTPException(500, f"Download failed: {e}")

    try:
        info = await _get_video_info(dest)
    except Exception:
        info = {"duration": 0, "width": 0, "height": 0}

    # Generate thumbnail
    thumb_path = staging_dir / "thumb_000.jpg"
    await _generate_thumbnail(dest, thumb_path)

    sanitized = sanitize_project_name(project)
    return {
        "batch_id": batch_id,
        "files": [{
            "index": 0,
            "original_name": video_url.split("/")[-1].split("?")[0] or "video.mp4",
            "path": str(dest),
            "url": f"/projects/{sanitized}/clips/_staging_{batch_id}/src_000.mp4",
            "thumb_url": f"/projects/{sanitized}/clips/_staging_{batch_id}/thumb_000.jpg",
            "duration": info["duration"],
            "width": info["width"],
            "height": info["height"],
        }],
    }


@router.post("/trim-batch")
async def trim_batch(body: dict):
    """Trim multiple staged videos with custom start/end times.

    Body: {
        "project": "...",
        "batch_id": "...",
        "trims": [{"path": "...", "start": 0.0, "end": 10.0, "original_name": "..."}]
    }
    """
    project = body.get("project", "quick-test")
    batch_id = body.get("batch_id", "")
    trims = body.get("trims", [])

    if not trims:
        raise HTTPException(400, "No trims provided")

    clipper_dir = _get_clipper_dir(project)
    job_id = _make_clip_job_id(project)
    job_dir = clipper_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    sanitized = sanitize_project_name(project)
    results: list[dict] = []

    for i, trim in enumerate(trims):
        source = Path(trim["path"])
        if not source.exists():
            results.append({"index": i, "ok": False, "error": "Source not found"})
            continue

        start = float(trim.get("start", 0))
        end = float(trim.get("end", 0))
        clip_duration = end - start
        if clip_duration <= 0.1:
            results.append({"index": i, "ok": False, "error": "Trim range too short"})
            continue

        # Probe source for dimensions
        try:
            info = await _get_video_info(source)
        except Exception:
            info = {"width": 1080, "height": 1920}

        clip_name = f"clip_{i + 1:03d}.mp4"
        clip_path = job_dir / clip_name
        ok = await _clip_segment(
            source, clip_path, start, clip_duration,
            info["width"], info["height"],
        )
        results.append({
            "index": i,
            "name": clip_name,
            "ok": ok,
            "url": f"/projects/{sanitized}/clips/{job_id}/{clip_name}" if ok else None,
            "duration": round(clip_duration, 2),
        })
        log.info("trim %d: %.1f-%.1f → %s (%s)", i, start, end, clip_name, "ok" if ok else "FAIL")

    # Clean up staging dir
    staging_dir = clipper_dir / f"_staging_{batch_id}"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
        log.info("cleaned up staging dir: %s", staging_dir)

    return {
        "job_id": job_id,
        "clips": results,
        "ok_count": sum(1 for r in results if r.get("ok")),
        "total": len(results),
    }


# ── In-memory job state for process-batch (polling pattern) ─────────────

_batch_jobs: dict[str, dict] = {}


@router.post("/process-batch")
async def process_batch(body: dict):
    """Start a clip batch job in the background. Returns job_id immediately.
    Poll GET /process-batch/{job_id} for progress — no SSE, no timeouts.
    """
    project = body.get("project", "quick-test")
    batch_id = body.get("batch_id", "")
    clip_length = float(body.get("clip_length", 7))
    sources = body.get("sources", [])

    if not sources:
        raise HTTPException(400, "No sources provided")
    if clip_length < 1:
        raise HTTPException(400, "clip_length must be >= 1 second")

    job_id = _make_clip_job_id(project)

    # Pre-calculate total clips
    total_clips = 0
    for src in sources:
        trim_start = float(src.get("trim_start", 0))
        trim_end = float(src.get("trim_end", 0))
        trim_duration = trim_end - trim_start
        if not Path(src["path"]).exists() or trim_duration < 0.5:
            total_clips += 1
        else:
            total_clips += max(1, math.floor(trim_duration / clip_length))

    _batch_jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "clip": 0,
        "total": total_clips,
        "source": "",
        "clips": [],
        "ok_count": 0,
        "clip_length": clip_length,
        "error": None,
    }

    asyncio.create_task(_run_batch_job(job_id, project, batch_id, clip_length, sources))

    return {"job_id": job_id, "total_clips": total_clips}


@router.get("/process-batch/{job_id}")
async def poll_batch(job_id: str):
    """Poll a clip batch job for progress. Returns current state."""
    job = _batch_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


async def _run_batch_job(
    job_id: str,
    project: str,
    batch_id: str,
    clip_length: float,
    sources: list[dict],
):
    """Background task: process all clips, update _batch_jobs in-memory state."""
    job = _batch_jobs[job_id]
    clipper_dir = _get_clipper_dir(project)
    job_dir = clipper_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    sanitized = sanitize_project_name(project)

    try:
        source_plans: list[dict] = []
        for src_idx, src in enumerate(sources):
            source = Path(src["path"])
            trim_start = float(src.get("trim_start", 0))
            trim_end = float(src.get("trim_end", 0))
            trim_duration = trim_end - trim_start
            if not source.exists() or trim_duration < 0.5:
                source_plans.append({"skip": True, "src_idx": src_idx, "src": src})
                continue
            num = max(1, math.floor(trim_duration / clip_length))
            source_plans.append({
                "skip": False, "src_idx": src_idx, "src": src,
                "source": source, "trim_start": trim_start, "trim_end": trim_end,
                "trim_duration": trim_duration, "num_clips": num,
            })

        results: list[dict] = []
        clip_counter = 0

        for plan in source_plans:
            src = plan["src"]
            src_idx = plan["src_idx"]
            source_name = src.get("original_name", f"source_{src_idx}")

            if plan.get("skip"):
                clip_counter += 1
                error = "Source not found" if not Path(src["path"]).exists() else "Trim too short"
                results.append({"index": clip_counter - 1, "ok": False, "error": error})
                job["clip"] = clip_counter
                job["clips"] = list(results)
                continue

            source = plan["source"]
            trim_start = plan["trim_start"]
            num_clips = plan["num_clips"]
            actual_clip_len = min(clip_length, plan["trim_duration"])

            try:
                info = await _get_video_info(source)
            except Exception:
                info = {"width": 1080, "height": 1920}

            for c in range(num_clips):
                clip_counter += 1
                clip_start = trim_start + c * actual_clip_len
                clip_name = f"clip_{clip_counter:03d}.mp4"
                clip_path = job_dir / clip_name

                job["clip"] = clip_counter
                job["source"] = source_name

                ok = await _clip_segment(
                    source, clip_path, clip_start, actual_clip_len,
                    info["width"], info["height"],
                )

                thumb_name = f"thumb_{clip_counter:03d}.jpg"
                thumb_path = job_dir / thumb_name
                if ok:
                    await _generate_thumbnail(clip_path, thumb_path, seek=actual_clip_len * 0.3)

                result = {
                    "index": clip_counter - 1,
                    "name": clip_name,
                    "source_name": source_name,
                    "ok": ok,
                    "url": f"/projects/{sanitized}/clips/{job_id}/{clip_name}" if ok else None,
                    "thumb_url": f"/projects/{sanitized}/clips/{job_id}/{thumb_name}" if ok else None,
                    "start": round(clip_start, 2),
                    "duration": round(actual_clip_len, 2),
                }
                results.append(result)
                job["clips"] = list(results)
                job["ok_count"] = sum(1 for r in results if r.get("ok"))

                log.info("  clip %d/%d: %.1f+%.1fs → %s (%s)",
                         clip_counter, job["total"], clip_start, actual_clip_len, clip_name, "ok" if ok else "FAIL")

        # Clean up staging dir
        staging_dir = clipper_dir / f"_staging_{batch_id}"
        if staging_dir.exists():
            shutil.rmtree(staging_dir)

        job["status"] = "complete"
        job["clips"] = results
        job["ok_count"] = sum(1 for r in results if r.get("ok"))
        log.info("batch COMPLETE: %s — %d/%d clips ok", job_id, job["ok_count"], job["total"])

    except Exception as e:
        log.error("batch FAILED: %s — %s", job_id, e)
        import traceback
        traceback.print_exc()
        job["status"] = "error"
        job["error"] = str(e)


# ── Pipeline for local files ─────────────────────────────────────────

async def _run_pipeline_local(
    job_id: str,
    source_path: Path,
    project: str,
    clip_min: float,
    clip_max: float,
    strategy: str,
):
    """Clip pipeline for an already-uploaded local file (skips download)."""
    t0 = time.time()
    log.info("pipeline-local START job=%s path=%s", job_id[:8], source_path)

    try:
        # Phase 1: Probe video info
        await _send(job_id, "status", {"text": "Analyzing video..."})
        info = await _get_video_info(source_path)
        duration = info["duration"]
        src_w = info["width"]
        src_h = info["height"]
        log.info("video info: %.1fs, %dx%d", duration, src_w, src_h)

        await _send(job_id, "video_info", {
            "duration": duration,
            "width": src_w,
            "height": src_h,
        })

        # Phase 2: Calculate clip segments
        if strategy == "even":
            clip_dur = min(clip_max, max(clip_min, duration))
            num_clips = max(1, math.floor(duration / clip_dur))
            actual_dur = duration / num_clips
            if actual_dur > clip_max:
                num_clips += 1
                actual_dur = duration / num_clips
            segments = [(i * actual_dur, actual_dur) for i in range(num_clips)]
        else:
            segments = []
            pos = 0.0
            while pos + clip_min <= duration:
                seg_dur = min(clip_max, duration - pos)
                if seg_dur < clip_min:
                    break
                segments.append((pos, seg_dur))
                pos += seg_dur

        total = len(segments)
        log.info("will create %d clips (strategy=%s)", total, strategy)

        clipper_dir = _get_clipper_dir(project)
        job_dir = clipper_dir / job_id

        await _send(job_id, "clip_plan", {"total": total, "segments": [
            {"start": s, "duration": d} for s, d in segments
        ]})

        # Phase 3: Cut clips
        clips: list[dict] = []
        for idx, (start, seg_dur) in enumerate(segments):
            clip_name = f"clip_{idx + 1:03d}.mp4"
            clip_path = job_dir / clip_name
            await _send(job_id, "clipping", {
                "index": idx,
                "total": total,
                "start": round(start, 2),
                "duration": round(seg_dur, 2),
            })

            ok = await _clip_segment(source_path, clip_path, start, seg_dur, src_w, src_h)
            clip_info = {
                "index": idx,
                "name": clip_name,
                "start": round(start, 2),
                "duration": round(seg_dur, 2),
                "ok": ok,
            }
            if ok:
                clip_info["url"] = f"/projects/{sanitize_project_name(project)}/clips/{job_id}/{clip_name}"
            clips.append(clip_info)

            await _send(job_id, "clipped", {
                "index": idx,
                "total": total,
                **clip_info,
            })

        # Phase 4: Complete
        await _send(job_id, "complete", {
            "job_id": job_id,
            "clips": clips,
            "total": total,
            "ok_count": sum(1 for c in clips if c["ok"]),
        })
        log.info("pipeline-local COMPLETE job=%s clips=%d total=%.1fs", job_id[:8], len(clips), time.time() - t0)

    except Exception as e:
        import traceback
        log.error("pipeline-local FAILED job=%s: %s", job_id[:8], e)
        traceback.print_exc()
        await _send(job_id, "error", {"error": str(e)})


# ── WebSocket endpoint ────────────────────────────────────────────────

@router.websocket("/ws/{job_id}")
async def websocket_clipper(ws: WebSocket, job_id: str):
    await ws.accept()
    log.info("WS connected: %s", job_id[:8])
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
                        msg.get("clip_min", 8.0),
                        msg.get("clip_max", 15.0),
                        msg.get("strategy", "sequential"),
                    )
                )
            elif msg.get("action") == "start_local":
                source = Path(msg["source_path"])
                if not source.exists():
                    await _send(job_id, "error", {"error": "Uploaded file not found on server"})
                else:
                    asyncio.create_task(
                        _run_pipeline_local(
                            job_id,
                            source,
                            msg.get("project", "quick-test"),
                            msg.get("clip_min", 8.0),
                            msg.get("clip_max", 15.0),
                            msg.get("strategy", "sequential"),
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
async def list_clipper_jobs(project: str = Query(default="quick-test")):
    clipper_dir = _get_clipper_dir(project)
    jobs: list[dict] = []

    for job_dir in sorted(clipper_dir.iterdir(), reverse=True):
        if not job_dir.is_dir() or job_dir.name == "__pycache__":
            continue
        clips = sorted(job_dir.glob("clip_*.mp4"))
        if not clips:
            continue
        sanitized = sanitize_project_name(project)
        meta_path = job_dir / "job_meta.json"
        meta_label = None
        if meta_path.exists():
            try:
                meta_label = json.loads(meta_path.read_text(encoding="utf-8")).get("label")
            except Exception:
                pass
        jobs.append({
            "job_id": job_dir.name,
            "label": meta_label,
            "clip_count": len(clips),
            "clips": [
                {
                    "name": c.name,
                    "url": f"/projects/{sanitized}/clips/{job_dir.name}/{c.name}",
                    "thumb_url": f"/projects/{sanitized}/clips/{job_dir.name}/{c.stem}_thumb.jpg"
                        if (job_dir / f"{c.stem}_thumb.jpg").exists()
                        else f"/projects/{sanitized}/clips/{job_dir.name}/thumb_{c.stem.split('_')[-1]}.jpg"
                        if (job_dir / f"thumb_{c.stem.split('_')[-1]}.jpg").exists()
                        else None,
                }
                for c in clips
            ],
        })

    return {"jobs": jobs}


@router.patch("/jobs/{job_id}/rename")
async def rename_clipper_job(job_id: str, body: dict, project: str = Query(default="quick-test")):
    """Rename a clipper job by writing/updating a meta sidecar."""
    new_label = (body.get("label") or "").strip()
    if not new_label:
        return JSONResponse({"error": "Label is required"}, status_code=400)
    clipper_dir = _get_clipper_dir(project)
    job_dir = clipper_dir / job_id
    if not job_dir.exists():
        raise HTTPException(404, "Job not found")
    meta_path = job_dir / "job_meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    meta["label"] = new_label
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return {"ok": True, "label": new_label}


@router.delete("/jobs/{job_id}")
async def delete_clipper_job(job_id: str, project: str = Query(default="quick-test")):
    clipper_dir = _get_clipper_dir(project)
    job_dir = clipper_dir / job_id

    if not job_dir.exists() or not job_dir.is_dir():
        raise HTTPException(404, "Job not found")

    shutil.rmtree(job_dir)
    log.info("deleted job %s", job_id[:8])
    return {"deleted": True, "job_id": job_id}


@router.get("/jobs/{job_id}/download-all")
async def download_all_clips(job_id: str, project: str = Query(default="quick-test")):
    clipper_dir = _get_clipper_dir(project)
    job_dir = clipper_dir / job_id

    if not job_dir.exists():
        raise HTTPException(404, "Job not found")

    clips = sorted(job_dir.glob("clip_*.mp4"))
    if not clips:
        raise HTTPException(404, "No clips found")

    zip_path = job_dir / "clips.zip"
    # Use ZIP_STORED (no compression) — MP4s are already compressed,
    # deflate just wastes CPU and can time out on large clips
    with ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for clip in clips:
            zf.write(clip, clip.name)

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"{job_id}.zip",
    )
