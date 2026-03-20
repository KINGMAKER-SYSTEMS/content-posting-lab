import asyncio
import shutil
import subprocess
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from project_manager import (
    get_project_slideshow_dir,
    get_project_slideshow_images_dir,
    sanitize_project_name,
)
from services.cropper import crop_to_916

router = APIRouter(tags=["slideshow"])

jobs: dict[str, dict] = {}

VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


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
    images_dir = get_project_slideshow_images_dir(project)
    target = images_dir / filename
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
        img_path = images_dir / slide.image
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
            stderr = result.stderr.decode(errors="replace")[-500:]
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
    output_dir = get_project_slideshow_dir(project)
    target = output_dir / filename
    if not target.exists():
        raise HTTPException(status_code=404, detail="Render not found")
    target.unlink()
    return {"deleted": filename}
