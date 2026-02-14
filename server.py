import asyncio
import base64
import io
import os
import re
import subprocess
import time
import uuid
import zipfile
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

app = FastAPI()

# ---------------------------------------------------------------------------
# API keys (all optional — only configured providers appear in /api/providers)
# ---------------------------------------------------------------------------
API_KEYS = {
    "xai": os.getenv("XAI_API_KEY"),
    "fal": os.getenv("FAL_KEY"),
    "luma": os.getenv("LUMA_API_KEY"),
    "replicate": os.getenv("REPLICATE_API_TOKEN"),
    "openai": os.getenv("OPENAI_API_KEY"),
}

jobs: dict[str, dict] = {}


# ===========================================================================
# Provider implementations
# Each returns a video URL string. Raises on failure.
# ===========================================================================

async def _grok_generate(
    client: httpx.AsyncClient,
    prompt: str,
    aspect_ratio: str,
    resolution: str,
    duration: int,
    image_data_uri: str | None,
    entry: dict,
) -> str:
    """Grok Imagine Video (xAI)."""
    key = API_KEYS["xai"]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload: dict = {
        "model": "grok-imagine-video",
        "prompt": prompt,
        "duration": duration,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
    }
    if image_data_uri:
        payload["image"] = {"url": image_data_uri}

    resp = await client.post(
        "https://api.x.ai/v1/videos/generations",
        headers=headers, json=payload, timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"xAI start failed: {resp.text}")
    request_id = resp.json()["request_id"]
    entry["provider_request_id"] = request_id
    entry["status"] = "polling"

    # Poll
    deadline = time.time() + 600
    while time.time() < deadline:
        r = await client.get(
            f"https://api.x.ai/v1/videos/{request_id}",
            headers={"Authorization": f"Bearer {key}"}, timeout=30,
        )
        data = r.json()
        if "video" in data:
            return data["video"]["url"]
        if "error" in data:
            raise RuntimeError(data["error"])
        status = data.get("status", "")
        if status == "expired":
            raise RuntimeError("xAI request expired")
        await asyncio.sleep(5)
    raise RuntimeError("xAI generation timed out")


async def _fal_generate(
    client: httpx.AsyncClient,
    model_id: str,
    prompt: str,
    aspect_ratio: str,
    resolution: str,
    duration: int,
    image_data_uri: str | None,
    entry: dict,
) -> str:
    """FAL.ai queue-based generation (Wan 2.5, Kling 2.5, Ovi)."""
    key = API_KEYS["fal"]
    headers = {"Authorization": f"Key {key}", "Content-Type": "application/json"}

    payload: dict = {"prompt": prompt}

    # Model-specific params
    if "wan" in model_id:
        payload["duration"] = str(duration) if duration <= 10 else "10"
        payload["resolution"] = resolution
        payload["aspect_ratio"] = aspect_ratio
    elif "kling" in model_id:
        payload["duration"] = str(min(duration, 10))
        payload["aspect_ratio"] = aspect_ratio
    elif "ovi" in model_id:
        payload["aspect_ratio"] = aspect_ratio

    if image_data_uri and "image-to-video" in model_id:
        payload["image_url"] = image_data_uri

    # Submit to queue
    resp = await client.post(
        f"https://queue.fal.run/{model_id}",
        headers=headers, json=payload, timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"FAL submit failed: {resp.text}")
    submit_data = resp.json()
    request_id = submit_data["request_id"]
    entry["provider_request_id"] = request_id
    entry["status"] = "polling"

    # Poll status
    status_url = f"https://queue.fal.run/{model_id}/requests/{request_id}/status"
    result_url = f"https://queue.fal.run/{model_id}/requests/{request_id}"
    deadline = time.time() + 600
    while time.time() < deadline:
        r = await client.get(status_url, headers=headers, timeout=30)
        data = r.json()
        status = data.get("status", "")
        if status == "COMPLETED":
            break
        if status in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"FAL generation {status}: {data}")
        await asyncio.sleep(5)
    else:
        raise RuntimeError("FAL generation timed out")

    # Fetch result
    r = await client.get(result_url, headers=headers, timeout=30)
    result = r.json()
    video = result.get("video") or result.get("output", {})
    if isinstance(video, dict):
        return video.get("url", "")
    raise RuntimeError(f"FAL unexpected result format: {result}")


async def _luma_generate(
    client: httpx.AsyncClient,
    prompt: str,
    aspect_ratio: str,
    resolution: str,
    duration: int,
    image_data_uri: str | None,
    entry: dict,
) -> str:
    """Luma Dream Machine (Ray 2)."""
    key = API_KEYS["luma"]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload: dict = {
        "prompt": prompt,
        "model": "ray-2",
        "resolution": resolution,
        "duration": f"{min(duration, 10)}s",
        "aspect_ratio": aspect_ratio,
    }
    if image_data_uri:
        payload["key_frames"] = {
            "frame0": {"type": "image", "url": image_data_uri}
        }

    resp = await client.post(
        "https://api.lumalabs.ai/dream-machine/v1/generations",
        headers=headers, json=payload, timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Luma start failed: {resp.text}")
    gen_id = resp.json()["id"]
    entry["provider_request_id"] = gen_id
    entry["status"] = "polling"

    # Poll
    deadline = time.time() + 600
    while time.time() < deadline:
        r = await client.get(
            f"https://api.lumalabs.ai/dream-machine/v1/generations/{gen_id}",
            headers={"Authorization": f"Bearer {key}"}, timeout=30,
        )
        data = r.json()
        state = data.get("state", "")
        if state == "completed":
            return data["assets"]["video"]
        if state == "failed":
            raise RuntimeError(f"Luma generation failed: {data.get('failure_reason', 'unknown')}")
        await asyncio.sleep(5)
    raise RuntimeError("Luma generation timed out")


async def _replicate_generate(
    client: httpx.AsyncClient,
    model_id: str,
    prompt: str,
    aspect_ratio: str,
    resolution: str,
    duration: int,
    image_data_uri: str | None,
    entry: dict,
) -> str:
    """Replicate prediction-based generation."""
    key = API_KEYS["replicate"]
    headers = {"Authorization": f"Token {key}", "Content-Type": "application/json"}

    input_params: dict = {"prompt": prompt}

    # Model-specific input mapping (each model has a unique schema)
    if "hailuo" in model_id or ("minimax" in model_id and "hailuo" not in model_id):
        # Hailuo 2.3: duration must be exactly 6 or 10 (10 only at 768p)
        hailuo_dur = 10 if duration >= 8 else 6
        hailuo_res = resolution if resolution in ("768p", "1080p") else "768p"
        if hailuo_res == "1080p":
            hailuo_dur = 6  # 1080p only supports 6s
        input_params["duration"] = hailuo_dur
        input_params["resolution"] = hailuo_res
        if image_data_uri:
            input_params["first_frame_image"] = image_data_uri
    elif "wan" in model_id:
        # Wan 720p: aspect_ratio only, no duration control
        input_params["aspect_ratio"] = aspect_ratio
    elif "kling" in model_id:
        # Kling v2.1: aspect_ratio + duration (default 5s)
        input_params["aspect_ratio"] = aspect_ratio
        input_params["duration"] = min(duration, 10)
        if image_data_uri:
            input_params["start_image"] = image_data_uri

    # Create prediction via official models endpoint (no version hash needed)
    resp = await client.post(
        f"https://api.replicate.com/v1/models/{model_id}/predictions",
        headers=headers,
        json={"input": input_params},
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Replicate start failed: {resp.text}")
    prediction = resp.json()
    pred_id = prediction["id"]
    entry["provider_request_id"] = pred_id
    entry["status"] = "polling"

    # Poll
    poll_url = f"https://api.replicate.com/v1/predictions/{pred_id}"
    deadline = time.time() + 600
    while time.time() < deadline:
        r = await client.get(poll_url, headers=headers, timeout=30)
        data = r.json()
        status = data.get("status", "")
        if status == "succeeded":
            output = data.get("output")
            if isinstance(output, str):
                return output
            if isinstance(output, list) and output:
                return output[0]
            raise RuntimeError(f"Replicate unexpected output: {output}")
        if status in ("failed", "canceled"):
            raise RuntimeError(f"Replicate {status}: {data.get('error', 'unknown')}")
        await asyncio.sleep(5)
    raise RuntimeError("Replicate generation timed out")


async def _sora_generate(
    client: httpx.AsyncClient,
    prompt: str,
    aspect_ratio: str,
    duration: int,
    image_data_uri: str | None,
    entry: dict,
    dest: Path,
) -> None:
    """OpenAI Sora 2 — downloads MP4 directly (no URL returned)."""
    key = API_KEYS["openai"]
    headers = {"Authorization": f"Bearer {key}"}

    # Map aspect ratio to pixel size
    size_map = {
        "9:16": "720x1280",
        "16:9": "1280x720",
    }
    size = size_map.get(aspect_ratio, "720x1280")

    # Sora only allows 4, 8, or 12 seconds
    allowed = [4, 8, 12]
    seconds = min(allowed, key=lambda x: abs(x - duration))

    payload: dict = {
        "model": "sora-2",
        "prompt": prompt,
        "size": size,
        "seconds": seconds,
    }

    # Image-to-video: send as multipart/form-data with file upload
    if image_data_uri and image_data_uri.startswith("data:"):
        header_part, b64_data = image_data_uri.split(",", 1)
        mime = header_part.split(":")[1].split(";")[0]
        ext = mime.split("/")[-1]
        raw = base64.b64decode(b64_data)
        files = {"input_reference": (f"input.{ext}", raw, mime)}
        form_fields = {k: str(v) for k, v in payload.items()}
        resp = await client.post(
            "https://api.openai.com/v1/videos",
            headers=headers, data=form_fields, files=files, timeout=60,
        )
    else:
        # Text-to-video: send as JSON
        resp = await client.post(
            "https://api.openai.com/v1/videos",
            headers={**headers, "Content-Type": "application/json"},
            json=payload, timeout=60,
        )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Sora start failed: {resp.text}")
    video_id = resp.json()["id"]
    entry["provider_request_id"] = video_id
    entry["status"] = "polling"

    # Poll status
    deadline = time.time() + 600
    while time.time() < deadline:
        r = await client.get(
            f"https://api.openai.com/v1/videos/{video_id}",
            headers=headers, timeout=30,
        )
        data = r.json()
        status = data.get("status", "")
        if status == "completed":
            break
        if status == "failed":
            raise RuntimeError(f"Sora generation failed: {data.get('error', 'unknown')}")
        await asyncio.sleep(5)
    else:
        raise RuntimeError("Sora generation timed out")

    # Download MP4 content directly from API
    entry["status"] = "downloading"
    async with client.stream(
        "GET",
        f"https://api.openai.com/v1/videos/{video_id}/content",
        headers=headers, timeout=120,
    ) as dl:
        dl.raise_for_status()
        with open(dest, "wb") as f:
            async for chunk in dl.aiter_bytes(8192):
                f.write(chunk)


# ===========================================================================
# Provider registry
# ===========================================================================
PROVIDERS = {
    "grok": {
        "name": "Grok Imagine",
        "key_id": "xai",
        "pricing": "~$5/10s video",
        "models": ["grok-imagine-video"],
    },
    "rep-minimax": {
        "name": "MiniMax Hailuo 2.3",
        "key_id": "replicate",
        "pricing": "~$0.28/video",
        "models": ["minimax/hailuo-2.3"],
    },
    "rep-wan": {
        "name": "Wan 2.1 720p",
        "key_id": "replicate",
        "pricing": "~$0.06/sec",
        "models": ["wavespeedai/wan-2.1-t2v-720p"],
    },
    "rep-kling": {
        "name": "Kling v2.1",
        "key_id": "replicate",
        "pricing": "~$0.07/sec",
        "models": ["kwaivgi/kling-v2.1-master"],
    },
    "fal-wan": {
        "name": "Wan 2.5 (FAL)",
        "key_id": "fal",
        "pricing": "$0.05/sec",
        "models": ["fal-ai/wan-25-preview/text-to-video"],
    },
    "fal-kling": {
        "name": "Kling 2.5 (FAL)",
        "key_id": "fal",
        "pricing": "$0.07/sec",
        "models": ["fal-ai/kling-video/v2.5-turbo/pro"],
    },
    "fal-ovi": {
        "name": "Ovi (FAL)",
        "key_id": "fal",
        "pricing": "$0.20/video",
        "models": ["fal-ai/ovi"],
    },
    "luma": {
        "name": "Luma Ray 2",
        "key_id": "luma",
        "pricing": "~$1-2/video",
        "models": ["ray-2"],
    },
    "sora": {
        "name": "Sora 2",
        "key_id": "openai",
        "pricing": "~$0.10/sec (720p)",
        "models": ["sora-2"],
    },
}


async def _download_video(client: httpx.AsyncClient, url: str, dest: Path):
    async with client.stream("GET", url, timeout=120) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            async for chunk in resp.aiter_bytes(8192):
                f.write(chunk)


# Providers that always output 16:9 regardless of aspect_ratio setting
_FORCE_LANDSCAPE = {"rep-minimax"}


async def _crop_to_vertical(src: Path) -> None:
    """Center-crop a 16:9 video to 9:16 in-place using ffmpeg."""
    tmp = src.with_suffix(".tmp.mp4")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(src),
        "-vf", "crop=ih*9/16:ih",
        "-c:a", "copy", str(tmp),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg crop failed: {stderr.decode()[-200:]}")
    tmp.replace(src)


def _slugify(text: str, max_len: int = 40) -> str:
    """Turn a prompt into a filesystem-safe folder name."""
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s[:max_len] or "untitled"


async def _generate_one(
    job_id: str,
    index: int,
    provider: str,
    prompt: str,
    aspect_ratio: str,
    resolution: str,
    duration: int,
    image_data_uri: str | None,
):
    entry = jobs[job_id]["videos"][index]
    try:
        # Organize: output/<provider>/<prompt_slug>/
        slug = _slugify(prompt)
        sub_dir = OUTPUT_DIR / provider / slug
        sub_dir.mkdir(parents=True, exist_ok=True)
        rel_dir = f"{provider}/{slug}"

        async with httpx.AsyncClient() as client:
            entry["status"] = "generating"

            if provider == "grok":
                video_url = await _grok_generate(
                    client, prompt, aspect_ratio, resolution, duration,
                    image_data_uri, entry,
                )
            elif provider.startswith("fal-"):
                model_map = {
                    "fal-wan": "fal-ai/wan-25-preview/text-to-video",
                    "fal-kling": "fal-ai/kling-video/v2.5-turbo/pro",
                    "fal-ovi": "fal-ai/ovi",
                }
                video_url = await _fal_generate(
                    client, model_map[provider], prompt, aspect_ratio,
                    resolution, duration, image_data_uri, entry,
                )
            elif provider == "luma":
                video_url = await _luma_generate(
                    client, prompt, aspect_ratio, resolution, duration,
                    image_data_uri, entry,
                )
            elif provider.startswith("rep-"):
                rep_model_map = {
                    "rep-minimax": "minimax/hailuo-2.3",
                    "rep-wan": "wavespeedai/wan-2.1-t2v-720p",
                    "rep-kling": "kwaivgi/kling-v2.1-master",
                }
                video_url = await _replicate_generate(
                    client, rep_model_map[provider], prompt, aspect_ratio,
                    resolution, duration, image_data_uri, entry,
                )
            elif provider == "sora":
                filename = f"{job_id}_{index}.mp4"
                dest = sub_dir / filename
                await _sora_generate(
                    client, prompt, aspect_ratio, duration,
                    image_data_uri, entry, dest,
                )
                entry["status"] = "done"
                entry["file"] = f"{rel_dir}/{filename}"
                entry["url"] = f"/output/{rel_dir}/{filename}"
                return
            else:
                raise RuntimeError(f"Unknown provider: {provider}")

            filename = f"{job_id}_{index}.mp4"
            dest = sub_dir / filename
            entry["status"] = "downloading"
            await _download_video(client, video_url, dest)

            # Auto-crop to 9:16 for providers that only output 16:9
            if aspect_ratio == "9:16" and provider in _FORCE_LANDSCAPE:
                entry["status"] = "cropping"
                await _crop_to_vertical(dest)

            entry["status"] = "done"
            entry["file"] = f"{rel_dir}/{filename}"
            entry["url"] = f"/output/{rel_dir}/{filename}"
    except Exception as e:
        entry["status"] = "error"
        entry["error"] = str(e)


# ===========================================================================
# Routes
# ===========================================================================
@app.get("/api/providers")
async def list_providers():
    """Return only providers whose API key is configured."""
    available = []
    for pid, info in PROVIDERS.items():
        if API_KEYS.get(info["key_id"]):
            available.append({"id": pid, **info})
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
    # Validate provider
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")
    key_id = PROVIDERS[provider]["key_id"]
    if not API_KEYS.get(key_id):
        raise HTTPException(status_code=400, detail=f"API key not configured for {provider}")

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
            _generate_one(
                job_id, i, provider, prompt, aspect_ratio,
                resolution, duration, image_data_uri,
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
    done_videos = [v for v in job["videos"] if v.get("status") == "done" and v.get("file")]
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
