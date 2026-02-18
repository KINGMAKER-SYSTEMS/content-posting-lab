import asyncio
import base64
import time
from pathlib import Path

import httpx

from .base import API_KEYS


async def generate(prompt: str, params: dict, client: httpx.AsyncClient) -> str:
    key = API_KEYS["openai"]
    aspect_ratio = params["aspect_ratio"]
    duration = params["duration"]
    image_data_uri = params.get("image_data_uri")
    entry = params["entry"]
    dest: Path = params["dest"]

    headers = {"Authorization": f"Bearer {key}"}

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

    if image_data_uri and image_data_uri.startswith("data:"):
        header_part, b64_data = image_data_uri.split(",", 1)
        mime = header_part.split(":")[1].split(";")[0]
        ext = mime.split("/")[-1]
        raw = base64.b64decode(b64_data)
        files = {"input_reference": (f"input.{ext}", raw, mime)}
        form_fields = {k: str(v) for k, v in payload.items()}
        resp = await client.post(
            "https://api.openai.com/v1/videos",
            headers=headers,
            data=form_fields,
            files=files,
            timeout=60,
        )
    else:
        resp = await client.post(
            "https://api.openai.com/v1/videos",
            headers={**headers, "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Sora start failed: {resp.text}")
    video_id = resp.json()["id"]
    entry["provider_request_id"] = video_id
    entry["status"] = "polling"

    deadline = time.time() + 600
    while time.time() < deadline:
        r = await client.get(
            f"https://api.openai.com/v1/videos/{video_id}",
            headers=headers,
            timeout=30,
        )
        data = r.json()
        status = data.get("status", "")
        if status == "completed":
            break
        if status == "failed":
            raise RuntimeError(
                f"Sora generation failed: {data.get('error', 'unknown')}"
            )
        await asyncio.sleep(5)
    else:
        raise RuntimeError("Sora generation timed out")

    entry["status"] = "downloading"
    async with client.stream(
        "GET",
        f"https://api.openai.com/v1/videos/{video_id}/content",
        headers=headers,
        timeout=120,
    ) as dl:
        dl.raise_for_status()
        with open(dest, "wb") as f:
            async for chunk in dl.aiter_bytes(8192):
                f.write(chunk)

    return ""
