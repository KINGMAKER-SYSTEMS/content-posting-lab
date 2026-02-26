"""Replicate API provider — Hailuo 2.3, Wan 2.2 T2V, Wan 2.2 I2V."""

import asyncio
import time

import httpx

from .base import API_KEYS

REPLICATE_API = "https://api.replicate.com/v1"


def _build_hailuo_input(prompt: str, params: dict) -> dict:
    """Build Replicate input payload for MiniMax Hailuo 2.3."""
    duration = params.get("duration", 6)
    resolution = params.get("resolution", "768p")
    image = params.get("image_data_uri")

    # Snap duration to valid values
    duration = 10 if duration >= 8 else 6
    # Validate resolution
    if resolution not in ("768p", "1080p"):
        resolution = "768p"
    # 1080p only supports 6s
    if resolution == "1080p":
        duration = 6

    inp: dict = {
        "prompt": prompt,
        "duration": duration,
        "resolution": resolution,
        "prompt_optimizer": params.get("optimize_prompt", True),
    }
    if image:
        inp["first_frame_image"] = image
    return inp


def _build_wan_t2v_input(prompt: str, params: dict) -> dict:
    """Build Replicate input payload for Wan 2.2 14B T2V Fast."""
    inp: dict = {
        "prompt": prompt,
        "aspect_ratio": params.get("aspect_ratio", "16:9"),
        "resolution": params.get("resolution", "480p"),
        "num_frames": params.get("num_frames", 81),
        "frames_per_second": params.get("frames_per_second", 16),
        "sample_shift": params.get("sample_shift", 12),
        "go_fast": params.get("go_fast", True),
        "interpolate_output": params.get("interpolate_output", True),
    }
    # Optional LoRA
    lora = params.get("lora_weights_transformer")
    if lora:
        inp["lora_weights_transformer"] = lora
        inp["lora_scale_transformer"] = params.get("lora_scale_transformer", 1)
    lora2 = params.get("lora_weights_transformer_2")
    if lora2:
        inp["lora_weights_transformer_2"] = lora2
        inp["lora_scale_transformer_2"] = params.get("lora_scale_transformer_2", 1)
    return inp


def _build_wan_i2v_input(prompt: str, params: dict) -> dict:
    """Build Replicate input payload for Wan 2.2 14B I2V."""
    image = params.get("image_data_uri")
    if not image:
        raise ValueError("Wan I2V requires an image — upload one before generating.")

    return {
        "prompt": prompt,
        "image": image,
        "resolution": params.get("resolution", "480p"),
        "num_frames": min(params.get("num_frames", 81), 100),
        "frames_per_second": min(params.get("frames_per_second", 16), 24),
        "sample_steps": params.get("sample_steps", 40),
        "sample_shift": params.get("sample_shift", 5),
        "go_fast": params.get("go_fast", False),
    }


def _build_wan_i2v_fast_input(prompt: str, params: dict) -> dict:
    """Build Replicate input payload for Wan 2.2 14B I2V Fast (PrunaAI)."""
    image = params.get("image_data_uri")
    if not image:
        raise ValueError("Wan I2V Fast requires an image — upload one before generating.")

    inp: dict = {
        "prompt": prompt,
        "image": image,
        "resolution": params.get("resolution", "480p"),
        "num_frames": params.get("num_frames", 81),
        "frames_per_second": params.get("frames_per_second", 16),
        "sample_shift": params.get("sample_shift", 12),
        "go_fast": params.get("go_fast", True),
        "interpolate_output": params.get("interpolate_output", False),
    }
    # Optional last frame
    last_image = params.get("last_image_data_uri")
    if last_image:
        inp["last_image"] = last_image
    # Optional LoRA
    lora = params.get("lora_weights_transformer")
    if lora:
        inp["lora_weights_transformer"] = lora
        inp["lora_scale_transformer"] = params.get("lora_scale_transformer", 1)
    lora2 = params.get("lora_weights_transformer_2")
    if lora2:
        inp["lora_weights_transformer_2"] = lora2
        inp["lora_scale_transformer_2"] = params.get("lora_scale_transformer_2", 1)
    return inp


_INPUT_BUILDERS = {
    "minimax/hailuo-2.3": _build_hailuo_input,
    "wan-video/wan-2.2-t2v-fast": _build_wan_t2v_input,
    "wan-video/wan-2.2-i2v-a14b": _build_wan_i2v_input,
    "wan-video/wan-2.2-i2v-fast": _build_wan_i2v_fast_input,
}


async def generate(prompt: str, params: dict, client: httpx.AsyncClient) -> str:
    """Submit a prediction to Replicate and poll until complete."""
    key = API_KEYS["replicate"]
    model_id = params["model_id"]
    entry = params["entry"]

    headers = {"Authorization": f"Token {key}", "Content-Type": "application/json"}

    builder = _INPUT_BUILDERS.get(model_id)
    if not builder:
        raise RuntimeError(f"No input builder for model: {model_id}")
    input_params = builder(prompt, params)

    resp = await client.post(
        f"{REPLICATE_API}/models/{model_id}/predictions",
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

    poll_url = f"{REPLICATE_API}/predictions/{pred_id}"
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
            raise RuntimeError(
                f"Replicate {status}: {data.get('error', 'unknown')}"
            )
        await asyncio.sleep(5)
    raise RuntimeError("Replicate generation timed out")


async def remove_text(
    image_data_uri: str, client: httpx.AsyncClient | None = None
) -> str:
    """Remove burned-in text from an image using FLUX Kontext text-removal.

    Args:
        image_data_uri: Base64 data URI of the source image.
        client: Optional shared httpx client; one is created if not provided.

    Returns:
        URL of the cleaned image on success.

    Raises:
        ValueError: If image_data_uri is empty/falsy.
        RuntimeError: If API key is missing, the prediction fails, or it times out.
    """
    if not image_data_uri:
        raise ValueError("image_data_uri is required — provide a base64 data URI")

    key = API_KEYS.get("replicate")
    if not key:
        raise RuntimeError("REPLICATE_API_TOKEN not set")

    headers = {"Authorization": f"Token {key}", "Content-Type": "application/json"}
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient()

    try:
        resp = await client.post(
            f"{REPLICATE_API}/models/flux-kontext-apps/text-removal/predictions",
            headers=headers,
            json={"input": {"input_image": image_data_uri}},
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Replicate text-removal start failed: {resp.text}")

        pred_id = resp.json()["id"]
        poll_url = f"{REPLICATE_API}/predictions/{pred_id}"
        deadline = time.time() + 120

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
                raise RuntimeError(
                    f"Replicate text-removal {status}: {data.get('error', 'unknown')}"
                )
            await asyncio.sleep(3)

        raise RuntimeError("Replicate text-removal timed out")
    finally:
        if owns_client:
            await client.aclose()
