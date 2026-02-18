import asyncio
import time

import httpx

from .base import API_KEYS


async def generate(prompt: str, params: dict, client: httpx.AsyncClient) -> str:
    """Grok Imagine Video (xAI). Returns video URL."""
    key = API_KEYS["xai"]
    aspect_ratio = params["aspect_ratio"]
    resolution = params["resolution"]
    duration = params["duration"]
    image_data_uri = params.get("image_data_uri")
    entry = params["entry"]

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
        headers=headers,
        json=payload,
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"xAI start failed: {resp.text}")
    request_id = resp.json()["request_id"]
    entry["provider_request_id"] = request_id
    entry["status"] = "polling"

    deadline = time.time() + 600
    while time.time() < deadline:
        r = await client.get(
            f"https://api.x.ai/v1/videos/{request_id}",
            headers={"Authorization": f"Bearer {key}"},
            timeout=30,
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
