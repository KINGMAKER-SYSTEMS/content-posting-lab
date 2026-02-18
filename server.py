import asyncio
import base64
import io
import uuid
import zipfile

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from providers import PROVIDERS
from providers.base import API_KEYS, OUTPUT_DIR, generate_one

app = FastAPI()

jobs: dict[str, dict] = {}


@app.get("/api/providers")
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


@app.post("/api/generate")
async def generate(
    prompt: str = Form(...),
    provider: str = Form("fal-wan"),
    count: int = Form(1),
    duration: int = Form(10),
    aspect_ratio: str = Form("9:16"),
    resolution: str = Form("720p"),
    media: UploadFile | None = File(None),
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

    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "id": job_id,
        "prompt": prompt,
        "provider": provider,
        "count": count,
        "videos": [{"index": i, "status": "queued"} for i in range(count)],
    }

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
            )
        )

    return {"job_id": job_id, "count": count}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id]


@app.get("/api/jobs")
async def list_jobs():
    return list(jobs.values())


@app.get("/api/jobs/{job_id}/download-all")
async def download_all(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    done_videos = [
        v for v in job["videos"] if v.get("status") == "done" and v.get("file")
    ]
    if not done_videos:
        raise HTTPException(status_code=400, detail="No completed videos to download")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for v in done_videos:
            filepath = OUTPUT_DIR / v["file"]
            if filepath.exists():
                zf.write(filepath, v["file"])
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=videolab_{job_id}.zip"},
    )


app.mount("/output", StaticFiles(directory="output"), name="output")
app.mount("/", StaticFiles(directory="static", html=True), name="static")
