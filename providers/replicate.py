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
    negative_prompt = params.get("negative_prompt")
    if negative_prompt:
        inp["negative_prompt"] = negative_prompt
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

    inp: dict = {
        "prompt": prompt,
        "image": image,
        "resolution": params.get("resolution", "480p"),
        "num_frames": min(params.get("num_frames", 81), 100),
        "frames_per_second": min(params.get("frames_per_second", 16), 24),
        "sample_steps": params.get("sample_steps", 40),
        "sample_shift": params.get("sample_shift", 5),
        "go_fast": params.get("go_fast", False),
    }
    negative_prompt = params.get("negative_prompt")
    if negative_prompt:
        inp["negative_prompt"] = negative_prompt
    return inp


def _build_pvideo_input(prompt: str, params: dict) -> dict:
    """Build Replicate input payload for PrunaAI P-Video.

    Two variants share this builder, dispatched via params["variant"]:
      - "landscape": locks aspect_ratio="16:9" so the multi-crop pipeline
        (FORCE_LANDSCAPE) can slice into 9:16 clips, mirroring Hailuo's UX.
      - "vertical": locks aspect_ratio="9:16" for native vertical output,
        single clip per generation.
    """
    variant = params.get("variant", "landscape")
    aspect_ratio = "9:16" if variant == "vertical" else "16:9"

    duration = max(1, min(int(params.get("duration", 6)), 10))
    resolution = params.get("resolution", "720p")
    if resolution not in ("720p", "1080p"):
        resolution = "720p"

    return {
        "prompt": prompt,
        "duration": duration,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "fps": 24,
        "draft": False,
        "prompt_upsampling": params.get("optimize_prompt", True),
        "disable_safety_filter": True,
        "save_audio": False,
        "no_op": False,
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
    negative_prompt = params.get("negative_prompt")
    if negative_prompt:
        inp["negative_prompt"] = negative_prompt
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
    "prunaai/p-video": _build_pvideo_input,
}


# ---------------------------------------------------------------------------
# Transient-failure handling for video generation.
#
# Production evidence (2026-09-23/24): paid-for Replicate predictions were
# abandoned because one poll GET hit a ReadError/ConnectTimeout, and whole
# replenishment jobs failed on a 429 throttle or a 5xx at submission.  Each
# such job blocked its page's refill for hours downstream.  Only failures that
# cannot create a second paid prediction are retried here:
#
# * submission: HTTP 429 (honouring ``retry_after``), 500 and 503 -- Replicate
#   returned no prediction id in every observed case (2026-09-24) -- or a
#   connection that was never established.  502/504 are gateway results whose
#   upstream may already have created the prediction, so they are NOT retried.  A read/write failure after the
#   request body may have reached Replicate is ambiguous and is NOT retried,
#   so a lost response can never become a duplicate paid prediction.
# * polling: the prediction already exists, so a transport error, 429, 5xx or
#   unparseable body is retried on the SAME prediction until the deadline.
# * "Prediction interrupted; please retry (code: PA)" is resubmitted once.
#
# Insufficient credit (402), validation errors, provider-side failures and
# timeouts are never retried.  A prediction that exceeds the deadline is
# cancelled so output that will be discarded stops accruing cost.  The model,
# input and prompt are identical on every attempt; nothing here changes the
# provider a recipe pinned.
# ---------------------------------------------------------------------------
START_ATTEMPTS = 4
START_RETRY_STATUSES = frozenset({429, 500, 503})
START_BACKOFF_SECONDS = (2.0, 5.0, 10.0)
RETRY_AFTER_CAP_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 5.0
POLL_TRANSIENT_LIMIT = 12
PREDICTION_DEADLINE_SECONDS = 600.0
INTERRUPTED_RESUBMITS = 1
_INTERRUPTED_MARKER = "(code: PA)"


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _now() -> float:
    return time.monotonic()


def _retry_after_seconds(resp: httpx.Response, attempt: int) -> float:
    fallback = START_BACKOFF_SECONDS[min(attempt, len(START_BACKOFF_SECONDS) - 1)]
    if resp.status_code != 429:
        return fallback
    try:
        hinted = float(resp.json().get("retry_after"))
    except Exception:
        try:
            hinted = float(resp.headers.get("retry-after", ""))
        except ValueError:
            return fallback
    if hinted != hinted or hinted <= 0:  # NaN or non-positive
        return fallback
    return min(hinted, RETRY_AFTER_CAP_SECONDS)


async def _start_prediction(
    client: httpx.AsyncClient, headers: dict, model_id: str, input_params: dict,
) -> str:
    for attempt in range(START_ATTEMPTS):
        final = attempt == START_ATTEMPTS - 1
        try:
            resp = await client.post(
                f"{REPLICATE_API}/models/{model_id}/predictions",
                headers=headers,
                json={"input": input_params},
                timeout=30,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # No connection means no request reached Replicate: safe to retry.
            if final:
                raise
            await _sleep(START_BACKOFF_SECONDS[min(attempt, len(START_BACKOFF_SECONDS) - 1)])
            continue
        if resp.status_code in (200, 201):
            return resp.json()["id"]
        if resp.status_code in START_RETRY_STATUSES and not final:
            await _sleep(_retry_after_seconds(resp, attempt))
            continue
        raise RuntimeError(f"Replicate start failed: {resp.text}")
    raise RuntimeError("Replicate start failed: retries exhausted")  # pragma: no cover


async def _cancel_prediction(client: httpx.AsyncClient, headers: dict, pred_id: str) -> None:
    """Best effort: stop an abandoned prediction from producing billable output."""
    try:
        await client.post(
            f"{REPLICATE_API}/predictions/{pred_id}/cancel", headers=headers, timeout=15,
        )
    except Exception:
        pass


async def _await_prediction(
    client: httpx.AsyncClient, headers: dict, pred_id: str,
) -> tuple[str, object]:
    """Poll one existing prediction. Returns ("succeeded", output) or (status, error)."""
    poll_url = f"{REPLICATE_API}/predictions/{pred_id}"
    deadline = _now() + PREDICTION_DEADLINE_SECONDS
    transient = 0
    while _now() < deadline:
        data = None
        try:
            r = await client.get(poll_url, headers=headers, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                raise httpx.HTTPStatusError("transient poll status", request=r.request, response=r)
            if r.status_code != 200:
                raise RuntimeError(f"Replicate poll failed: HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            if not isinstance(data, dict):
                raise ValueError("poll body is not an object")
        except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as error:
            transient += 1
            if transient > POLL_TRANSIENT_LIMIT:
                await _cancel_prediction(client, headers, pred_id)
                raise RuntimeError(
                    f"Replicate poll unavailable after {transient} attempts "
                    f"(prediction {pred_id}): {error!r}"
                ) from error
            await _sleep(POLL_INTERVAL_SECONDS)
            continue
        transient = 0
        status = data.get("status", "")
        if status == "succeeded":
            return status, data.get("output")
        if status in ("failed", "canceled"):
            return status, data.get("error") or data.get("logs") or "unknown error (no details from API)"
        await _sleep(POLL_INTERVAL_SECONDS)
    await _cancel_prediction(client, headers, pred_id)
    raise RuntimeError(
        f"Replicate generation timed out after {int(PREDICTION_DEADLINE_SECONDS)}s "
        f"(prediction {pred_id})"
    )


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

    for submission in range(INTERRUPTED_RESUBMITS + 1):
        pred_id = await _start_prediction(client, headers, model_id, input_params)
        entry["provider_request_id"] = pred_id
        entry["status"] = "polling"
        status, result = await _await_prediction(client, headers, pred_id)
        if status == "succeeded":
            if isinstance(result, str):
                return result
            if isinstance(result, list) and result:
                return result[0]
            raise RuntimeError(f"Replicate unexpected output: {result}")
        if (
            status == "failed"
            and _INTERRUPTED_MARKER in str(result)
            and submission < INTERRUPTED_RESUBMITS
        ):
            continue
        raise RuntimeError(f"Replicate {status}: {result}")
    raise RuntimeError("Replicate generation failed: resubmissions exhausted")  # pragma: no cover


async def _poll_prediction(
    client: httpx.AsyncClient,
    headers: dict,
    pred_id: str,
    label: str,
    timeout_secs: int = 180,
) -> str | list:
    """Poll a Replicate prediction until it completes. Returns the output."""
    poll_url = f"{REPLICATE_API}/predictions/{pred_id}"
    deadline = time.time() + timeout_secs

    while time.time() < deadline:
        r = await client.get(poll_url, headers=headers, timeout=30)
        data = r.json()
        status = data.get("status", "")
        if status == "succeeded":
            return data.get("output")
        if status in ("failed", "canceled"):
            raise RuntimeError(
                f"Replicate {label} {status}: {data.get('error', 'unknown')}"
            )
        await asyncio.sleep(3)

    raise RuntimeError(f"Replicate {label} timed out after {timeout_secs}s")


def _generate_text_mask(image_data_uri: str) -> str:
    """Generate a binary mask highlighting burned-in text regions.

    TikTok captions are typically white/bright text with dark stroke, positioned
    in the center-lower area. We detect these by:
    1. Convert to grayscale
    2. Threshold for bright pixels (text is white/near-white)
    3. Dilate to connect letter strokes into solid regions
    4. Return as a base64 PNG data URI (white = remove, black = keep)
    """
    import base64
    import io

    from PIL import Image, ImageFilter

    # Decode the data URI
    _, b64data = image_data_uri.split(",", 1)
    img = Image.open(io.BytesIO(base64.b64decode(b64data)))
    w, h = img.size

    # Convert to grayscale
    gray = img.convert("L")

    # Threshold: pixels brighter than 200 are likely text
    threshold = 200
    mask = gray.point(lambda p: 255 if p > threshold else 0, mode="1")

    # Convert back to L mode for filtering
    mask = mask.convert("L")

    # Dilate to connect strokes and cover text edges/stroke outlines
    # MaxFilter expands white regions — run multiple passes for solid coverage
    for _ in range(5):
        mask = mask.filter(ImageFilter.MaxFilter(5))

    # Only keep the center 80% horizontally
    # (TikTok captions are typically centered, not in corners)
    final_mask = Image.new("L", (w, h), 0)
    # Crop region: center 80% width, full height (captions can be anywhere vertically)
    margin_x = int(w * 0.10)
    final_mask.paste(mask.crop((margin_x, 0, w - margin_x, h)), (margin_x, 0))

    # Encode as PNG data URI
    buf = io.BytesIO()
    final_mask.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


_LAMA_VERSION = "40e67426e1bf78199d78b36580389fbbdcb4c9cdc2bc2b489e99d713f167b3c5"


async def remove_text(
    image_data_uri: str, client: httpx.AsyncClient | None = None
) -> str:
    """Remove burned-in text from an image using local mask + LaMa inpainting.

    1. Locally generates a binary mask detecting bright text pixels (Pillow).
    2. Sends image + mask to LaMa (dpakkk/image-object-removal) for inpainting.

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
        # Step 1: Generate text mask locally (fast, no API call)
        mask_data_uri = _generate_text_mask(image_data_uri)

        # Step 2: LaMa inpainting — community model, version-based endpoint
        resp = await client.post(
            f"{REPLICATE_API}/predictions",
            headers=headers,
            json={
                "version": _LAMA_VERSION,
                "input": {
                    "image": image_data_uri,
                    "mask": mask_data_uri,
                },
            },
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Replicate LaMa start failed: {resp.text}")

        pred_id = resp.json()["id"]
        output = await _poll_prediction(client, headers, pred_id, "LaMa")

        if isinstance(output, str):
            return output
        if isinstance(output, list) and output:
            return output[0]
        raise RuntimeError(f"LaMa unexpected output: {output}")

    finally:
        if owns_client:
            await client.aclose()
