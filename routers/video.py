"""Video generation router — migrated from server.py with project-scoped paths."""

import asyncio
import base64
import io
import json
import os
import re
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from project_manager import PROJECTS_DIR, get_project_video_dir
from providers import PROVIDERS
from providers.base import API_KEYS, generate_one

router = APIRouter()

jobs: dict[str, dict] = {}

MAX_PROMPT_HISTORY = 200

# Limit concurrent video generation tasks to prevent resource exhaustion
_gen_semaphore = asyncio.Semaphore(10)


def _make_job_id(provider: str, prompt: str) -> str:
    """Generate a readable job ID: {provider}-{words}-{MMDDHHmm}-{short_uuid}.

    Example: "grok-stars-and-gal-04011430-a1b2"
    """
    # Extract first 3 words from prompt, slugified
    words = re.sub(r"[^a-z0-9 ]", "", prompt.lower()).split()[:3]
    slug = "-".join(words)[:20] if words else "gen"
    ts = datetime.now().strftime("%m%d%H%M")
    short = uuid.uuid4().hex[:4]
    return f"{provider}-{slug}-{ts}-{short}"


def _prompts_path(project: str) -> Path:
    return PROJECTS_DIR / project / "prompts.json"


def _jobs_path(project: str) -> Path:
    return PROJECTS_DIR / project / "jobs.json"


def _save_jobs(project: str) -> None:
    """Persist active jobs for this project to disk."""
    project_jobs = {jid: j for jid, j in jobs.items() if j.get("project") == project}
    if not project_jobs:
        return
    p = _jobs_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(json.dumps(project_jobs, indent=2), encoding="utf-8")
    except OSError:
        pass


_TERMINAL_STATUSES = {"done", "error"}


def _load_jobs(project: str) -> None:
    """Load persisted jobs from disk into the in-memory dict.

    Videos stuck in non-terminal states (generating, downloading, cropping, polling)
    are marked as done-with-crops if crop files exist on disk, or as error otherwise.
    This handles server restarts that kill in-flight async tasks.
    """
    p = _jobs_path(project)
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        for jid, job in data.items():
            if jid not in jobs:
                # Fix stuck videos from previous server session
                for v in job.get("videos", []):
                    if v.get("status") not in _TERMINAL_STATUSES:
                        # Check if crop files landed on disk before the crash
                        if v.get("crops"):
                            v["status"] = "done"
                        elif v.get("file"):
                            v["status"] = "done"
                        else:
                            v["status"] = "error"
                            v["error"] = "Server restarted during processing"
                jobs[jid] = job
    except (json.JSONDecodeError, OSError):
        pass


def _persist_job(job_id: str) -> None:
    """Callback for generate_one — write job state to disk on completion."""
    job = jobs.get(job_id)
    if not job:
        return
    project = job.get("project", "quick-test")
    _save_jobs(project)


def _read_prompts(project: str) -> list[dict]:
    p = _prompts_path(project)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_prompt(project: str, entry: dict) -> None:
    prompts = _read_prompts(project)
    prompts.insert(0, entry)
    prompts = prompts[:MAX_PROMPT_HISTORY]
    p = _prompts_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(prompts, indent=2), encoding="utf-8")


@router.get("/providers")
async def list_providers():
    available = []
    for pid, info in PROVIDERS.items():
        if API_KEYS.get(info["key_id"]):
            available.append(
                {
                    "id": pid,
                    **{k: v for k, v in info.items() if k != "module"},
                }
            )
    return available


PROVIDER_SCHEMAS: dict[str, dict] = {
    "grok": {
        "duration": {"type": "range", "min": 1, "max": 15, "default": 10, "label": "Duration (seconds)"},
        "aspect_ratio": {
            "type": "select",
            "options": ["16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3"],
            "default": "16:9",
            "label": "Aspect Ratio",
        },
        "resolution": {"type": "select", "options": ["480p", "720p"], "default": "480p", "label": "Resolution"},
    },
    "hailuo": {
        "duration": {"type": "select", "options": [6, 10], "default": 6, "label": "Duration (seconds)", "note": "10s only at 768p"},
        "resolution": {"type": "select", "options": ["768p", "1080p"], "default": "768p", "label": "Resolution", "note": "1080p locks duration to 6s"},
        "crop_mode": {"type": "select", "options": ["none", "dual", "triptych", "both"], "default": "both", "label": "Multi-Crop", "note": "dual=2, triptych=3, both=5 crops from one 16:9"},
        "optimize_prompt": {"type": "toggle", "default": True, "label": "Prompt Optimizer"},
    },
    "wan-t2v": {
        "aspect_ratio": {"type": "select", "options": ["16:9", "9:16"], "default": "16:9", "label": "Aspect Ratio"},
        "resolution": {"type": "select", "options": ["480p", "720p"], "default": "480p", "label": "Resolution"},
        "num_frames": {"type": "range", "min": 81, "max": 121, "default": 81, "step": 4, "label": "Frames", "note": "81 = ~5s, 121 = ~7.5s at 16fps"},
        "_advanced": {
            "sample_shift": {"type": "range", "min": 1, "max": 20, "default": 12, "step": 1, "label": "Sample Shift"},
            "frames_per_second": {"type": "range", "min": 5, "max": 30, "default": 16, "step": 1, "label": "FPS"},
            "go_fast": {"type": "toggle", "default": True, "label": "Go Fast"},
            "interpolate_output": {"type": "toggle", "default": True, "label": "Interpolate to 30fps"},
            "lora_weights_transformer": {"type": "text", "default": "", "label": "LoRA Weights URL", "placeholder": "https://huggingface.co/.../lora.safetensors"},
            "lora_scale_transformer": {"type": "range", "min": 0, "max": 2, "default": 1, "step": 0.1, "label": "LoRA Scale"},
        },
    },
    "wan-i2v": {
        "resolution": {"type": "select", "options": ["480p", "720p"], "default": "480p", "label": "Resolution"},
        "num_frames": {"type": "range", "min": 81, "max": 100, "default": 81, "step": 1, "label": "Frames", "note": "81 = ~5s, 100 = ~6.25s at 16fps"},
        "image_required": True,
        "_advanced": {
            "sample_steps": {"type": "range", "min": 1, "max": 50, "default": 40, "step": 1, "label": "Sample Steps"},
            "sample_shift": {"type": "range", "min": 1, "max": 20, "default": 5, "step": 1, "label": "Sample Shift"},
            "frames_per_second": {"type": "range", "min": 5, "max": 24, "default": 16, "step": 1, "label": "FPS"},
            "go_fast": {"type": "toggle", "default": False, "label": "Go Fast"},
        },
    },
    "wan-i2v-fast": {
        "resolution": {"type": "select", "options": ["480p", "720p"], "default": "480p", "label": "Resolution"},
        "num_frames": {"type": "range", "min": 81, "max": 121, "default": 81, "step": 4, "label": "Frames", "note": "81 = ~5s, 121 = ~7.5s at 16fps"},
        "image_required": True,
        "last_image_supported": True,
        "_advanced": {
            "sample_shift": {"type": "range", "min": 1, "max": 20, "default": 12, "step": 1, "label": "Sample Shift"},
            "frames_per_second": {"type": "range", "min": 5, "max": 30, "default": 16, "step": 1, "label": "FPS"},
            "go_fast": {"type": "toggle", "default": True, "label": "Go Fast"},
            "interpolate_output": {"type": "toggle", "default": False, "label": "Interpolate to 30fps"},
            "lora_weights_transformer": {"type": "text", "default": "", "label": "LoRA Weights URL", "placeholder": "https://huggingface.co/.../lora.safetensors"},
            "lora_scale_transformer": {"type": "range", "min": 0, "max": 2, "default": 1, "step": 0.1, "label": "LoRA Scale"},
        },
    },
}


@router.get("/provider-schemas")
async def get_provider_schemas():
    return PROVIDER_SCHEMAS


@router.get("/prompts")
async def list_prompts(project: str = "quick-test"):
    return _read_prompts(project)


@router.delete("/prompts")
async def clear_prompts(project: str = "quick-test"):
    p = _prompts_path(project)
    if p.exists():
        p.unlink()
    return {"ok": True}


@router.delete("/file")
async def delete_video_file(project: str = "quick-test", path: str = ""):
    """Delete a single video file from a project's videos directory."""
    if not path:
        raise HTTPException(400, "path is required")
    video_dir = get_project_video_dir(project)
    target = (video_dir / path).resolve()
    # Prevent path traversal
    if not str(target).startswith(str(video_dir.resolve())):
        raise HTTPException(400, "Invalid path")
    if not target.exists():
        raise HTTPException(404, "File not found")
    target.unlink()
    return {"deleted": True, "path": path}


@router.post("/generate")
async def generate_video(
    prompt: str = Form(...),
    provider: str = Form("wan-t2v"),
    count: int = Form(1),
    duration: int = Form(10),
    aspect_ratio: str = Form("9:16"),
    resolution: str = Form("720p"),
    media: UploadFile | None = File(None),
    last_image: UploadFile | None = File(None),
    project: str = Form("quick-test"),
    # Model-specific optional params (passed through to provider)
    num_frames: int | None = Form(None),
    frames_per_second: int | None = Form(None),
    sample_shift: float | None = Form(None),
    sample_steps: int | None = Form(None),
    go_fast: bool | None = Form(None),
    interpolate_output: bool | None = Form(None),
    optimize_prompt: bool | None = Form(None),
    lora_weights_transformer: str | None = Form(None),
    lora_scale_transformer: float | None = Form(None),
    lora_weights_transformer_2: str | None = Form(None),
    lora_scale_transformer_2: float | None = Form(None),
    crop_mode: str | None = Form(None),
):
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")
    key_id = PROVIDERS[provider]["key_id"]
    if not API_KEYS.get(key_id):
        raise HTTPException(
            status_code=400, detail=f"API key not configured for {provider}"
        )

    count = max(1, min(count, 20))
    duration = max(1, min(duration, 15))

    image_data_uri = None
    if media and media.size and media.size > 0:
        raw = await media.read()
        b64 = base64.b64encode(raw).decode()
        ct = media.content_type or "image/jpeg"
        image_data_uri = f"data:{ct};base64,{b64}"

    last_image_data_uri = None
    if last_image and last_image.size and last_image.size > 0:
        raw = await last_image.read()
        b64 = base64.b64encode(raw).decode()
        ct = last_image.content_type or "image/jpeg"
        last_image_data_uri = f"data:{ct};base64,{b64}"

    extra: dict = {}
    if last_image_data_uri:
        extra["last_image_data_uri"] = last_image_data_uri
    for key, val in [
        ("num_frames", num_frames),
        ("frames_per_second", frames_per_second),
        ("sample_shift", sample_shift),
        ("sample_steps", sample_steps),
        ("go_fast", go_fast),
        ("interpolate_output", interpolate_output),
        ("optimize_prompt", optimize_prompt),
        ("lora_weights_transformer", lora_weights_transformer),
        ("lora_scale_transformer", lora_scale_transformer),
        ("lora_weights_transformer_2", lora_weights_transformer_2),
        ("lora_scale_transformer_2", lora_scale_transformer_2),
        ("crop_mode", crop_mode),
    ]:
        if val is not None:
            extra[key] = val

    output_dir = get_project_video_dir(project)
    output_dir.mkdir(parents=True, exist_ok=True)

    job_id = _make_job_id(provider, prompt)
    jobs[job_id] = {
        "id": job_id,
        "prompt": prompt,
        "provider": provider,
        "count": count,
        "project": project,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "videos": [{"index": i, "status": "queued"} for i in range(count)],
    }

    _save_prompt(
        project,
        {
            "prompt": prompt,
            "provider": provider,
            "count": count,
            "duration": duration,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "has_media": image_data_uri is not None,
            "job_id": job_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )

    url_prefix = f"/projects/{project}/videos"

    async def _throttled_generate(index: int) -> None:
        async with _gen_semaphore:
            await generate_one(
                job_id, index, provider, prompt,
                aspect_ratio, resolution, duration, image_data_uri,
                jobs, output_dir, url_prefix,
                on_complete=_persist_job,
                **extra,
            )

    for i in range(count):
        asyncio.create_task(_throttled_generate(i))

    return {"job_id": job_id, "count": count}


@router.get("/jobs")
async def list_jobs(project: str = "quick-test"):
    _load_jobs(project)
    return [j for j in jobs.values() if j.get("project") == project]


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    if job_id not in jobs:
        # Try loading from all projects
        for proj_dir in PROJECTS_DIR.iterdir():
            if proj_dir.is_dir():
                _load_jobs(proj_dir.name)
        if job_id not in jobs:
            raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id]


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str, project: str = "quick-test"):
    """Delete a job and all its video files from disk."""
    if job_id not in jobs:
        _load_jobs(project)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    proj = job.get("project", project)
    video_dir = get_project_video_dir(proj)

    # Delete all video files on disk
    deleted_files = 0
    for v in job.get("videos", []):
        # Delete main file
        if v.get("file"):
            target = (video_dir / v["file"]).resolve()
            if str(target).startswith(str(video_dir.resolve())) and target.exists():
                target.unlink()
                deleted_files += 1
        # Delete crop files
        for crop in v.get("crops", []):
            if crop.get("file"):
                target = (video_dir / crop["file"]).resolve()
                if str(target).startswith(str(video_dir.resolve())) and target.exists():
                    target.unlink()
                    deleted_files += 1

    # Remove from in-memory state
    del jobs[job_id]
    _save_jobs(proj)

    return {"deleted": True, "job_id": job_id, "files_removed": deleted_files}


@router.get("/jobs/{job_id}/download-all")
async def download_all(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    done_videos = [
        v for v in job["videos"] if v.get("status") == "done" and v.get("file")
    ]
    if not done_videos:
        raise HTTPException(status_code=400, detail="No completed videos to download")

    project = job.get("project", "quick-test")
    base_dir = get_project_video_dir(project)

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    os.close(tmp_fd)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
            for v in done_videos:
                filepath = base_dir / v["file"]
                if filepath.exists():
                    zf.write(filepath, v["file"])
    except Exception:
        os.unlink(tmp_path)
        raise

    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=f"videolab_{job_id}.zip",
    )


@router.post("/bulk-download")
async def bulk_download(body: dict):
    """Download all completed videos from multiple jobs as a single ZIP.

    Body: {"job_ids": ["id1", "id2", ...], "project": "..."}
    """
    job_ids = body.get("job_ids", [])
    project = body.get("project", "quick-test")
    if not job_ids:
        raise HTTPException(status_code=400, detail="No job IDs provided")

    _load_jobs(project)
    base_dir = get_project_video_dir(project)

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    os.close(tmp_fd)
    file_count = 0
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
            for jid in job_ids:
                job = jobs.get(jid)
                if not job:
                    continue
                for v in job.get("videos", []):
                    if v.get("status") != "done":
                        continue
                    if v.get("file"):
                        fp = base_dir / v["file"]
                        if fp.exists():
                            zf.write(fp, f"{jid}/{v['file']}")
                            file_count += 1
                    for crop in v.get("crops", []):
                        if crop.get("file"):
                            fp = base_dir / crop["file"]
                            if fp.exists():
                                zf.write(fp, f"{jid}/{crop['file']}")
                                file_count += 1
    except Exception:
        os.unlink(tmp_path)
        raise

    if file_count == 0:
        os.unlink(tmp_path)
        raise HTTPException(status_code=400, detail="No completed videos found in the selected jobs")

    today = datetime.now().strftime("%Y-%m-%d")
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=f"videolab_{today}_{file_count}videos.zip",
    )


@router.post("/bulk-delete")
async def bulk_delete(body: dict):
    """Delete multiple jobs and their video files.

    Body: {"job_ids": ["id1", "id2", ...], "project": "..."}
    """
    job_ids = body.get("job_ids", [])
    project = body.get("project", "quick-test")
    if not job_ids:
        raise HTTPException(status_code=400, detail="No job IDs provided")

    _load_jobs(project)
    video_dir = get_project_video_dir(project)
    deleted_jobs = 0
    deleted_files = 0

    for jid in job_ids:
        job = jobs.get(jid)
        if not job:
            continue
        proj = job.get("project", project)
        vdir = get_project_video_dir(proj)
        for v in job.get("videos", []):
            if v.get("file"):
                target = (vdir / v["file"]).resolve()
                if str(target).startswith(str(vdir.resolve())) and target.exists():
                    target.unlink()
                    deleted_files += 1
            for crop in v.get("crops", []):
                if crop.get("file"):
                    target = (vdir / crop["file"]).resolve()
                    if str(target).startswith(str(vdir.resolve())) and target.exists():
                        target.unlink()
                        deleted_files += 1
        del jobs[jid]
        deleted_jobs += 1

    _save_jobs(project)
    return {"deleted_jobs": deleted_jobs, "deleted_files": deleted_files}
