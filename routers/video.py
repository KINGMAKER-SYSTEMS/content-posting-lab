"""Video generation router — migrated from server.py with project-scoped paths."""

import asyncio
import base64
import io
import uuid
import zipfile

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from project_manager import get_project_video_dir
from providers import PROVIDERS
from providers.base import API_KEYS, generate_one

router = APIRouter()

jobs: dict[str, dict] = {}


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


@router.post("/generate")
async def generate_video(
    prompt: str = Form(...),
    provider: str = Form("fal-wan"),
    count: int = Form(1),
    duration: int = Form(10),
    aspect_ratio: str = Form("9:16"),
    resolution: str = Form("720p"),
    media: UploadFile | None = File(None),
    project: str = Form("quick-test"),
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

    url_prefix = f"/projects/{project}/videos"
    for i in range(count):
        asyncio.create_task(
            generate_one(
                job_id,
                i,
                provider,
                prompt,
                aspect_ratio,
                resolution,
                duration,
                image_data_uri,
                jobs,
                output_dir,
                url_prefix,
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
