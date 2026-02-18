import asyncio
import time

import httpx

from .base import API_KEYS


async def generate(prompt: str, params: dict, client: httpx.AsyncClient) -> str:
    key = API_KEYS["replicate"]
    model_id = params["model_id"]
    aspect_ratio = params["aspect_ratio"]
    resolution = params["resolution"]
    duration = params["duration"]
    image_data_uri = params.get("image_data_uri")
    entry = params["entry"]

    headers = {"Authorization": f"Token {key}", "Content-Type": "application/json"}

    input_params: dict = {"prompt": prompt}

    # Hailuo 2.3: duration must be exactly 6 or 10 (10 only at 768p)
    if "hailuo" in model_id or ("minimax" in model_id and "hailuo" not in model_id):
        hailuo_dur = 10 if duration >= 8 else 6
        hailuo_res = resolution if resolution in ("768p", "1080p") else "768p"
        if hailuo_res == "1080p":
            hailuo_dur = 6  # 1080p only supports 6s
        input_params["duration"] = hailuo_dur
        input_params["resolution"] = hailuo_res
        if image_data_uri:
            input_params["first_frame_image"] = image_data_uri
    elif "wan" in model_id:
        input_params["aspect_ratio"] = aspect_ratio
    elif "kling" in model_id:
        input_params["aspect_ratio"] = aspect_ratio
        input_params["duration"] = min(duration, 10)
        if image_data_uri:
            input_params["start_image"] = image_data_uri

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
