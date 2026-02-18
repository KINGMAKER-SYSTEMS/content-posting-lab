import asyncio
import time

import httpx

from .base import API_KEYS


async def generate(prompt: str, params: dict, client: httpx.AsyncClient) -> str:
    """FAL.ai queue-based generation (Wan 2.5, Kling 2.5, Ovi). Returns video URL."""
    key = API_KEYS["fal"]
    model_id = params["model_id"]
    aspect_ratio = params["aspect_ratio"]
    resolution = params["resolution"]
    duration = params["duration"]
    image_data_uri = params.get("image_data_uri")
    entry = params["entry"]

    headers = {"Authorization": f"Key {key}", "Content-Type": "application/json"}

    payload: dict = {"prompt": prompt}

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

    resp = await client.post(
        f"https://queue.fal.run/{model_id}",
        headers=headers,
        json=payload,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"FAL submit failed: {resp.text}")
    submit_data = resp.json()
    request_id = submit_data["request_id"]
    entry["provider_request_id"] = request_id
    entry["status"] = "polling"

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

    r = await client.get(result_url, headers=headers, timeout=30)
    result = r.json()
    video = result.get("video") or result.get("output", {})
    if isinstance(video, dict):
        return video.get("url", "")
    raise RuntimeError(f"FAL unexpected result format: {result}")
