"""Local ComfyUI provider: unpaid generation on hardware the operator owns.

``COMFYUI_URL`` names one ComfyUI server (for example the studio Mac). A
generation uploads the optional start image, submits an API-format workflow to
``/prompt``, polls ``/history`` and returns the server's ``/view`` URL for the
finished MP4, which ``generate_one`` downloads like any provider artifact.

When the lab cannot reach that server (a cloud deployment), ``LOCAL_WORKER_KEY``
enables the reverse path instead: the job is queued for a worker running beside
the server, which renders it with ``run_on_server`` and uploads the result.

Nothing here is billable, so this module never touches the generation budget.
"""

import asyncio
import base64
import binascii
import io
import math
import os
import secrets
import time
import uuid
from urllib.parse import urlencode

import httpx

from .base import API_KEYS

WAN_T2V = "comfyui/wan-2.2-t2v-14b"
WAN_I2V = "comfyui/wan-2.2-i2v-14b"
H3_FL2VA = "comfyui/minimax-h3-fl2va"

# Full-precision files: the studio Mac has the memory for them, and the
# NVIDIA-oriented fp8/int8 repacks buy nothing on Apple silicon.
_WAN_FILES = {
    WAN_T2V: {
        "high": "wan2.2_t2v_high_noise_14B_fp16.safetensors",
        "low": "wan2.2_t2v_low_noise_14B_fp16.safetensors",
        "lora_high": "wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors",
        "lora_low": "wan2.2_t2v_lightx2v_4steps_lora_v1.1_low_noise.safetensors",
    },
    WAN_I2V: {
        "high": "wan2.2_i2v_high_noise_14B_fp16.safetensors",
        "low": "wan2.2_i2v_low_noise_14B_fp16.safetensors",
        "lora_high": "wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors",
        "lora_low": "wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors",
    },
}
_WAN_TEXT_ENCODER = "umt5_xxl_fp16.safetensors"
_WAN_VAE = "wan_2.1_vae.safetensors"

# Wan's own default negative prompt (from the reference workflow).
_WAN_NEGATIVE = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，"
    "低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，"
    "毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走"
)

_WAN_SIZES = {
    ("480p", "16:9"): (832, 480),
    ("480p", "9:16"): (480, 832),
    ("480p", "1:1"): (624, 624),
    ("720p", "16:9"): (1280, 720),
    ("720p", "9:16"): (720, 1280),
    ("720p", "1:1"): (960, 960),
}
_WAN_PIXELS = {"480p": 832 * 480, "720p": 1280 * 720}

# MiniMax H3 (first/last-frame model): 768p base output, dimensions in x32.
_H3_FILES = {
    "unet": "minimax_h3_fl2va_bf16.safetensors",
    "text_encoder": "qwen3vl_32b_minimax_h3_bf16.safetensors",
    "vae": "minimax_h3_video_vae_fp16.safetensors",
    "lora": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
}
_H3_SIZES = {"16:9": (1344, 768), "9:16": (768, 1344), "1:1": (1024, 1024)}
_H3_PIXELS = 1344 * 768
_H3_FPS = 24

_DEFAULT_TIMEOUT_SECONDS = 60 * 60
_POLL_SECONDS = 2.0
_QUEUE_CHECK_EVERY = 5   # polls between checks that the server still has the job

# One render at a time per server, and only one model family resident. ComfyUI
# keeps every model it has loaded; on unified-memory Macs three families at
# once (H3 plus both Wan pairs) got the server killed by the OS for memory.
# Waiting jobs are let in batch order, so a loaded model is used up before the
# server switches (services.local_render_order).
_FREE_SETTLE_SECONDS = 2.0
_server_model: dict[str, str] = {}


class _ServerGate:
    """Admit one render at a time to a server, in batch order rather than FIFO."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.busy = False
        self._waiting: list[tuple[str | None, float, asyncio.Future]] = []

    def locked(self) -> bool:
        return self.busy

    async def acquire(self, model_id: str | None) -> None:
        if not self.busy:
            self.busy = True
            return
        future = asyncio.get_running_loop().create_future()
        waiter = (model_id, time.monotonic(), future)
        self._waiting.append(waiter)
        try:
            await future
        except asyncio.CancelledError:
            if waiter in self._waiting:
                self._waiting.remove(waiter)
            elif future.done() and not future.cancelled():
                self.release()  # handed the server while being cancelled: pass it on
            raise

    def release(self) -> None:
        from services import local_render_order

        self._waiting = [w for w in self._waiting if not w[2].done()]
        index = local_render_order.pick(
            [(model, since) for model, since, _ in self._waiting],
            _server_model.get(self.url),
            time.monotonic(),
            local_render_order.max_wait_seconds(),
        )
        if index is None:
            self.busy = False
            return
        # The server stays busy: ownership passes straight to the chosen job.
        self._waiting.pop(index)[2].set_result(None)


_server_gates: dict[str, _ServerGate] = {}
# Entry states that mean the job was stopped (e.g. swept as stuck) while it
# waited in line. Such a job is not started (Lab #426).
STOPPED_STATUSES = frozenset({"error", "cancelled"})

# ``API_KEYS["comfyui"]`` holds this instead of a URL when jobs go to a worker.
WORKER_QUEUE = "worker-queue"
# The only request fields a worker receives; everything else stays in the lab.
_WORKER_PARAM_KEYS = (
    "aspect_ratio", "resolution", "duration", "num_frames", "frames_per_second",
    "sample_shift", "sample_steps", "go_fast", "negative_prompt", "seed",
    "image_data_uri", "last_image_data_uri", "variant",
)


def base_url() -> str:
    url = (API_KEYS.get("comfyui") or "").strip().rstrip("/")
    if not url or url == WORKER_QUEUE:
        raise RuntimeError("COMFYUI_URL is not configured")
    return url


def _timeout_seconds() -> float:
    raw = os.getenv("COMFYUI_TIMEOUT_SECONDS", "").strip()
    try:
        value = float(raw) if raw else 0.0
    except ValueError:
        value = 0.0
    return value if math.isfinite(value) and value > 0 else _DEFAULT_TIMEOUT_SECONDS


def _int_param(params: dict, key: str, default: int, low: int, high: int) -> int:
    value = params.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{key} must be a number")
    return max(low, min(int(value), high))


def _decode_data_uri(data_uri: str) -> tuple[bytes, str]:
    """Return (bytes, file extension) for one ``data:image/...;base64,`` URI."""
    if not isinstance(data_uri, str) or not data_uri.startswith("data:") or "," not in data_uri:
        raise ValueError("start image must be a base64 data URI")
    header, payload = data_uri.split(",", 1)
    content_type = header[5:].split(";", 1)[0].strip().lower()
    extension = {"image/png": "png", "image/webp": "webp"}.get(content_type, "jpg")
    try:
        return base64.b64decode(payload, validate=True), extension
    except (binascii.Error, ValueError) as exc:
        raise ValueError("start image is not valid base64") from exc


def _image_size(raw: bytes) -> tuple[int, int]:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(raw)) as image:
            return image.size
    except Exception as exc:  # noqa: BLE001 - Pillow raises many types
        raise ValueError("start image could not be read") from exc


def _fit_to_image(pixels: int, image_size: tuple[int, int], multiple: int = 16) -> tuple[int, int]:
    """Keep the start image's shape at a pixel budget, snapped to ``multiple``."""
    width, height = image_size
    if width <= 0 or height <= 0:
        raise ValueError("start image has no pixels")
    aspect = max(0.4, min(width / height, 2.5))
    out_height = math.sqrt(pixels / aspect)
    out_width = out_height * aspect
    return (max(multiple, round(out_width / multiple) * multiple),
            max(multiple, round(out_height / multiple) * multiple))


def _bool_param(params: dict, key: str, default: bool) -> bool:
    value = params.get(key)
    if value is None:
        return default
    if type(value) is not bool:
        raise ValueError(f"{key} must be a boolean")
    return value


def _seed(params: dict) -> int:
    seed = params.get("seed")
    return secrets.randbelow(2**48) if seed is None else seed


def build_workflow(prompt: str, params: dict) -> dict:
    """Build the API-format ComfyUI graph for one local generation.

    Pure and network-free: the route calls it to reject a bad request before a
    job exists. ``image_name`` / ``last_image_name`` are the uploaded files'
    names on the ComfyUI server; without them a placeholder stands in.
    """
    model_id = params.get("model_id")
    if model_id == H3_FL2VA:
        return _build_h3(prompt, params)
    if model_id in _WAN_FILES:
        return _build_wan(prompt, params)
    raise ValueError(f"no ComfyUI workflow for model: {model_id!r}")


def _build_h3(prompt: str, params: dict) -> dict:
    """MiniMax H3 text / first-frame / last-frame to video, video track only."""
    first, last = params.get("image_data_uri"), params.get("last_image_data_uri")
    variant = params.get("variant")
    if variant == "landscape":
        # Always 16:9 so the multi-crop pipeline can slice it into 9:16 clips,
        # the same contract as Hailuo. A start image is conformed to the frame.
        width, height = _H3_SIZES["16:9"]
        if first:
            _decode_data_uri(first)
    elif first:
        image_size = params.get("image_size")
        if image_size is None:
            image_size = _image_size(_decode_data_uri(first)[0])
        width, height = _fit_to_image(_H3_PIXELS, image_size, 32)
    elif variant == "vertical":
        width, height = _H3_SIZES["9:16"]
    else:
        size = _H3_SIZES.get(params.get("aspect_ratio") or "9:16")
        if size is None:
            raise ValueError("aspect ratio must be 16:9, 9:16 or 1:1")
        width, height = size
    if last:
        _decode_data_uri(last)

    # H3's latent grid: frame counts are 17n + 5 at 24 fps.
    frames = max(5, _int_param(params, "duration", 5, 2, 15) * _H3_FPS)
    frames += (5 - frames % 17) % 17

    fast = _bool_param(params, "go_fast", True)
    steps = 8 if fast else _int_param(params, "sample_steps", 20, 2, 50)

    graph: dict = {
        "clip": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": _H3_FILES["text_encoder"], "type": "minimax", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": _H3_FILES["vae"]}},
        "unet": {"class_type": "UNETLoader", "inputs": {
            "unet_name": _H3_FILES["unet"], "weight_dtype": "default"}},
    }
    model = ["unet", 0]
    if fast:
        graph["lora"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": model, "lora_name": _H3_FILES["lora"], "strength_model": 1.0}}
        model = ["lora", 0]
    conditioning: dict = {
        "clip": ["clip", 0], "vae": ["vae", 0], "prompt": prompt,
        "width": width, "height": height, "length": frames}
    if first:
        graph["first_frame"] = {"class_type": "LoadImage", "inputs": {
            "image": params.get("image_name") or "pending-upload"}}
        conditioning["first_frame"] = ["first_frame", 0]
    if last:
        graph["last_frame"] = {"class_type": "LoadImage", "inputs": {
            "image": params.get("last_image_name") or "pending-upload"}}
        conditioning["last_frame"] = ["last_frame", 0]
    graph["h3"] = {"class_type": "MiniMaxH3ImageToVideo", "inputs": conditioning}
    graph["noise"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": _seed(params)}}
    graph["sampler"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}}
    graph["sigmas"] = {"class_type": "BasicScheduler", "inputs": {
        "model": model, "scheduler": "simple", "steps": steps, "denoise": 1.0}}
    graph["guider"] = {"class_type": "BasicGuider", "inputs": {"model": model, "conditioning": ["h3", 0]}}
    graph["sample"] = {"class_type": "SamplerCustomAdvanced", "inputs": {
        "noise": ["noise", 0], "guider": ["guider", 0], "sampler": ["sampler", 0],
        "sigmas": ["sigmas", 0], "latent_image": ["h3", 1]}}
    # The lab posts with campaign sounds, so only the picture is decoded.
    graph["decode"] = {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}}
    graph["video"] = {"class_type": "CreateVideo", "inputs": {"images": ["decode", 0], "fps": _H3_FPS}}
    graph["save"] = {"class_type": "SaveVideo", "inputs": {
        "video": ["video", 0], "filename_prefix": "content-lab/h3",
        "format": "auto", "codec": "auto"}}
    return graph


def _build_wan(prompt: str, params: dict) -> dict:
    """Wan 2.2 14B text-to-video or image-to-video (two-expert sampling)."""
    model_id = params.get("model_id")
    files = _WAN_FILES[model_id]
    resolution = params.get("resolution") or "480p"
    if resolution not in _WAN_PIXELS:
        raise ValueError("resolution must be 480p or 720p")

    is_i2v = model_id == WAN_I2V
    if is_i2v:
        if not params.get("image_data_uri"):
            raise ValueError("Wan I2V requires an image — upload one before generating.")
        image_size = params.get("image_size")
        if image_size is None:
            image_size = _image_size(_decode_data_uri(params["image_data_uri"])[0])
        width, height = _fit_to_image(_WAN_PIXELS[resolution], image_size)
    else:
        aspect_ratio = params.get("aspect_ratio") or "16:9"
        size = _WAN_SIZES.get((resolution, aspect_ratio))
        if size is None:
            raise ValueError("aspect ratio must be 16:9, 9:16 or 1:1")
        width, height = size

    # Wan's VAE packs four frames per latent step: lengths are 4n + 1.
    frames = _int_param(params, "num_frames", 81, 17, 161)
    frames = (frames - 1) // 4 * 4 + 1
    fps = _int_param(params, "frames_per_second", 16, 5, 30)
    shift = float(_int_param(params, "sample_shift", 5, 1, 20))

    fast = _bool_param(params, "go_fast", True)
    if fast:
        steps, cfg = 4, 1.0
    else:
        steps, cfg = _int_param(params, "sample_steps", 20, 2, 50), 3.5
    split = steps // 2

    seed = _seed(params)
    negative = (params.get("negative_prompt") or "").strip() or _WAN_NEGATIVE

    graph: dict = {
        "clip": {"class_type": "CLIPLoader",
                 "inputs": {"clip_name": _WAN_TEXT_ENCODER, "type": "wan", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": _WAN_VAE}},
        "positive": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["clip", 0]}},
        "negative": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["clip", 0]}},
        "unet_high": {"class_type": "UNETLoader",
                      "inputs": {"unet_name": files["high"], "weight_dtype": "default"}},
        "unet_low": {"class_type": "UNETLoader",
                     "inputs": {"unet_name": files["low"], "weight_dtype": "default"}},
    }
    model_high, model_low = ["unet_high", 0], ["unet_low", 0]
    if fast:
        graph["lora_high"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": model_high, "lora_name": files["lora_high"], "strength_model": 1.0}}
        graph["lora_low"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": model_low, "lora_name": files["lora_low"], "strength_model": 1.0}}
        model_high, model_low = ["lora_high", 0], ["lora_low", 0]
    graph["shift_high"] = {"class_type": "ModelSamplingSD3", "inputs": {"model": model_high, "shift": shift}}
    graph["shift_low"] = {"class_type": "ModelSamplingSD3", "inputs": {"model": model_low, "shift": shift}}

    if is_i2v:
        graph["start_image"] = {"class_type": "LoadImage",
                                "inputs": {"image": params.get("image_name") or "pending-upload"}}
        graph["latent"] = {"class_type": "WanImageToVideo", "inputs": {
            "positive": ["positive", 0], "negative": ["negative", 0], "vae": ["vae", 0],
            "start_image": ["start_image", 0],
            "width": width, "height": height, "length": frames, "batch_size": 1}}
        positive, negative_ref, latent = ["latent", 0], ["latent", 1], ["latent", 2]
    else:
        graph["latent"] = {"class_type": "EmptyHunyuanLatentVideo", "inputs": {
            "width": width, "height": height, "length": frames, "batch_size": 1}}
        positive, negative_ref, latent = ["positive", 0], ["negative", 0], ["latent", 0]

    # Two-stage Wan 2.2 sampling: the high-noise expert lays out motion, the
    # low-noise expert finishes detail from the leftover noise.
    graph["sample_high"] = {"class_type": "KSamplerAdvanced", "inputs": {
        "model": ["shift_high", 0], "add_noise": "enable", "noise_seed": seed,
        "steps": steps, "cfg": cfg, "sampler_name": "euler", "scheduler": "simple",
        "positive": positive, "negative": negative_ref, "latent_image": latent,
        "start_at_step": 0, "end_at_step": split, "return_with_leftover_noise": "enable"}}
    graph["sample_low"] = {"class_type": "KSamplerAdvanced", "inputs": {
        "model": ["shift_low", 0], "add_noise": "disable", "noise_seed": 0,
        "steps": steps, "cfg": cfg, "sampler_name": "euler", "scheduler": "simple",
        "positive": positive, "negative": negative_ref, "latent_image": ["sample_high", 0],
        "start_at_step": split, "end_at_step": steps, "return_with_leftover_noise": "disable"}}
    graph["decode"] = {"class_type": "VAEDecode", "inputs": {"samples": ["sample_low", 0], "vae": ["vae", 0]}}
    graph["video"] = {"class_type": "CreateVideo", "inputs": {"images": ["decode", 0], "fps": fps}}
    graph["save"] = {"class_type": "SaveVideo", "inputs": {
        "video": ["video", 0], "filename_prefix": "content-lab/wan",
        "format": "auto", "codec": "auto"}}
    return graph


async def _upload_image(client: httpx.AsyncClient, url: str, raw: bytes, extension: str) -> str:
    name = f"content-lab-{uuid.uuid4().hex}.{extension}"
    resp = await client.post(
        f"{url}/upload/image",
        files={"image": (name, raw, "application/octet-stream")},
        data={"overwrite": "true"},
        timeout=120,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"ComfyUI image upload failed ({resp.status_code}): {resp.text[:300]}")
    body = resp.json()
    uploaded = body.get("name")
    if not isinstance(uploaded, str) or not uploaded:
        raise RuntimeError("ComfyUI image upload returned no file name")
    subfolder = body.get("subfolder") or ""
    return f"{subfolder}/{uploaded}" if subfolder else uploaded


async def _still_queued(client: httpx.AsyncClient, url: str, prompt_id: str) -> bool:
    """True while ComfyUI lists the prompt as running or pending.

    An unreadable queue counts as still queued: only a clear "not here" answer
    may fail a job.
    """
    try:
        resp = await client.get(f"{url}/queue", timeout=30)
        if resp.status_code != 200:
            return True
        body = resp.json()
        for key in ("queue_running", "queue_pending"):
            for item in body.get(key) or []:
                if isinstance(item, list) and len(item) > 1 and item[1] == prompt_id:
                    return True
        return False
    except (httpx.HTTPError, ValueError):
        return True


def _execution_error(history: dict) -> str | None:
    status = history.get("status") or {}
    if status.get("status_str") != "error":
        return None
    for message in reversed(status.get("messages") or []):
        if isinstance(message, list) and len(message) == 2 and message[0] == "execution_error":
            detail = message[1] or {}
            return f"{detail.get('node_type', 'node')}: {detail.get('exception_message', 'failed')}".strip()
    return "workflow failed"


def _video_output(history: dict) -> dict | None:
    for output in (history.get("outputs") or {}).values():
        if not isinstance(output, dict):
            continue
        for items in output.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and str(item.get("filename", "")).lower().endswith((".mp4", ".webm", ".mov")):
                    return item
    return None


async def generate(prompt: str, params: dict, client: httpx.AsyncClient):
    """Render one local generation.

    Returns the ComfyUI ``/view`` URL when the lab talks to the server itself,
    or the uploaded file's ``Path`` when a worker rendered it.
    """
    if (API_KEYS.get("comfyui") or "").strip() == WORKER_QUEUE:
        return await _generate_via_worker(prompt, params)
    return await run_on_server(base_url(), prompt, params, client)


async def _generate_via_worker(prompt: str, params: dict):
    from services import local_worker_queue

    build_workflow(prompt, params)  # refuse a bad request before it is queued
    entry = params["entry"]
    job = local_worker_queue.enqueue({
        "model_id": params["model_id"],
        "prompt": prompt,
        "params": {key: params[key] for key in _WORKER_PARAM_KEYS if params.get(key) is not None},
    })
    entry["provider_request_id"] = job.id
    entry["status"] = "queued"
    return await local_worker_queue.wait(job, entry, _timeout_seconds())


async def _release_other_models(client: httpx.AsyncClient, url: str, model_id: str) -> None:
    """Ask the idle server to drop its models when the next job needs different ones."""
    if _server_model.get(url) == model_id:
        return
    try:
        await client.post(f"{url}/free", json={"unload_models": True, "free_memory": True}, timeout=30)
    except httpx.HTTPError:
        pass  # best effort: an older server without /free still renders
    _server_model[url] = model_id
    # The server applies the request on its queue thread; give it a moment
    # before the next prompt arrives and reloads.
    await asyncio.sleep(_FREE_SETTLE_SECONDS)


async def run_on_server(url: str, prompt: str, params: dict, client: httpx.AsyncClient) -> str:
    """Run one workflow on the ComfyUI server at ``url``. Returns the video URL.

    Jobs for one server run strictly one after another from this process, so
    the timeout bounds a single render rather than its time in line.
    """
    url = url.rstrip("/")
    build_workflow(prompt, params)  # refuse a bad request before waiting in line
    entry = params["entry"]
    gate = _server_gates.setdefault(url, _ServerGate(url))
    if gate.locked():
        entry["status"] = "queued"
    await gate.acquire(params.get("model_id"))
    try:
        if entry.get("status") in STOPPED_STATUSES:
            raise RuntimeError("local job was stopped while waiting in line; not started")
        entry["status"] = "generating"
        await _release_other_models(client, url, params.get("model_id"))
        return await _render(url, prompt, params, client)
    finally:
        gate.release()


async def _render(url: str, prompt: str, params: dict, client: httpx.AsyncClient) -> str:
    entry = params["entry"]
    params = dict(params)

    if params.get("image_data_uri"):
        raw, extension = _decode_data_uri(params["image_data_uri"])
        params["image_size"] = _image_size(raw)
        params["image_name"] = await _upload_image(client, url, raw, extension)
    if params.get("last_image_data_uri") and params.get("model_id") == H3_FL2VA:
        raw, extension = _decode_data_uri(params["last_image_data_uri"])
        params["last_image_name"] = await _upload_image(client, url, raw, extension)

    workflow = build_workflow(prompt, params)
    resp = await client.post(
        f"{url}/prompt",
        json={"prompt": workflow, "client_id": f"content-lab-{uuid.uuid4().hex}"},
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"ComfyUI rejected the workflow ({resp.status_code}): {resp.text[:600]}")
    prompt_id = resp.json().get("prompt_id")
    if not isinstance(prompt_id, str) or not prompt_id:
        raise RuntimeError("ComfyUI returned no prompt id")
    entry["provider_request_id"] = prompt_id
    entry["status"] = "polling"

    deadline = time.monotonic() + _timeout_seconds()
    polls = 0
    while time.monotonic() < deadline:
        poll = await client.get(f"{url}/history/{prompt_id}", timeout=30)
        history = poll.json().get(prompt_id) if poll.status_code == 200 else None
        polls += 1
        if history is None and polls % _QUEUE_CHECK_EVERY == 0 and not await _still_queued(client, url, prompt_id):
            # One more look: it may have finished between the two requests.
            poll = await client.get(f"{url}/history/{prompt_id}", timeout=30)
            history = poll.json().get(prompt_id) if poll.status_code == 200 else None
            if history is None:
                raise RuntimeError("ComfyUI no longer has this job (cancelled, or the server restarted)")
        if isinstance(history, dict):
            error = _execution_error(history)
            if error:
                raise RuntimeError(f"ComfyUI generation failed: {error}")
            if (history.get("status") or {}).get("completed"):
                output = _video_output(history)
                if output is None:
                    raise RuntimeError("ComfyUI finished without a video output")
                return f"{url}/view?" + urlencode({
                    "filename": output["filename"],
                    "subfolder": output.get("subfolder") or "",
                    "type": output.get("type") or "output",
                })
        await asyncio.sleep(_POLL_SECONDS)
    raise RuntimeError("ComfyUI generation timed out")
