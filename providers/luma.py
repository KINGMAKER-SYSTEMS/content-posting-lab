import asyncio
import time

import httpx

from .base import API_KEYS


async def generate(prompt: str, params: dict, client: httpx.AsyncClient) -> str:
    key = API_KEYS["luma"]
    aspect_ratio = params["aspect_ratio"]
    resolution = params["resolution"]
    duration = params["duration"]
    image_data_uri = params.get("image_data_uri")
    entry = params["entry"]

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload: dict = {
        "prompt": prompt,
        "model": "ray-2",
        "resolution": resolution,
        "duration": f"{min(duration, 10)}s",
        "aspect_ratio": aspect_ratio,
    }
    if image_data_uri:
        payload["key_frames"] = {"frame0": {"type": "image", "url": image_data_uri}}

    resp = await client.post(
        "https://api.lumalabs.ai/dream-machine/v1/generations",
        headers=headers,
        json=payload,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Luma start failed: {resp.text}")
    gen_id = resp.json()["id"]
    entry["provider_request_id"] = gen_id
    entry["status"] = "polling"

    deadline = time.time() + 600
    while time.time() < deadline:
        r = await client.get(
            f"https://api.lumalabs.ai/dream-machine/v1/generations/{gen_id}",
            headers={"Authorization": f"Bearer {key}"},
            timeout=30,
        )
        data = r.json()
        state = data.get("state", "")
        if state == "completed":
            return data["assets"]["video"]
        if state == "failed":
            raise RuntimeError(
                f"Luma generation failed: {data.get('failure_reason', 'unknown')}"
            )
        await asyncio.sleep(5)
    raise RuntimeError("Luma generation timed out")
