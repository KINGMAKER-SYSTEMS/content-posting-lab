"""Video generation router — migrated from server.py with project-scoped paths."""

import asyncio
import base64
import io
import json
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from project_manager import PROJECTS_DIR, get_project_video_dir
from providers import PROVIDERS
from providers.base import API_KEYS, generate_one

router = APIRouter()

jobs: dict[str, dict] = {}

MAX_PROMPT_HISTORY = 200


def _prompts_path(project: str) -> Path:
    return PROJECTS_DIR / project / "prompts.json"


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
    ]:
        if val is not None:
            extra[key] = val

    output_dir = get_project_video_dir(project)
    output_dir.mkdir(parents=True, exist_ok=True)

    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "id": job_id,
        "prompt": prompt,
        "provider": provider,
        "count": count,
        "project": project,
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
    for i in range(count):
        asyncio.create_task(
            generate_one(
                job_id, i, provider, prompt,
                aspect_ratio, resolution, duration, image_data_uri,
                jobs, output_dir, url_prefix,
                **extra,
            )
        )

    return {"job_id": job_id, "count": count}


@router.get("/jobs")
async def list_jobs():
    return list(jobs.values())


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id]


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

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for v in done_videos:
            filepath = base_dir / v["file"]
            if filepath.exists():
                zf.write(filepath, v["file"])
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=videolab_{job_id}.zip"},
    )
