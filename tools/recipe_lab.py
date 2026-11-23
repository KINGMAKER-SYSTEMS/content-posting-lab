"""Recipe lab: tune local generation recipes by sweeping settings and scoring results.

Renders the Control Plane's real prompts on a local ComfyUI server under named
setting variations ("variants"), measures every clip with cheap automatic
checks, keeps a running pass rate per variant, writes a review sheet, and saves
a chosen variant as a ComfyUI workflow.

    python tools/recipe_lab.py sweep tools/recipe_lab_specs/truck.json --rounds 3
    python tools/recipe_lab.py sheet <run-dir>
    python tools/recipe_lab.py promote <run-dir> <variant>

Nothing here is used by the app at runtime and nothing here spends money: it
only talks to the local ComfyUI server (COMFYUI_SERVER_URL).
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import fcntl
import html
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from providers import comfyui  # noqa: E402
from services import control_plane_generation as cpg  # noqa: E402

LAB_DIR = ROOT / "projects" / "recipe-lab"      # served by the app at /projects/recipe-lab/
WORKFLOW_DIR = ROOT / "recipes" / "local-workflows"
COMFY_USER_WORKFLOWS = Path.home() / "ComfyUI" / "user" / "default" / "workflows" / "content-lab"
PROMPT_PLACEHOLDER = "{{PROMPT}}"


def comfy_url() -> str:
    return (os.getenv("COMFYUI_SERVER_URL") or "http://127.0.0.1:8188").rstrip("/")


# --- prompts ----------------------------------------------------------------

def catalog_prompt(format_slug: str, family: str, combination: int) -> tuple[str, dict[str, str]]:
    """One real prompt and its slot picks, composed exactly as the Control Plane executor does."""
    catalog, digest = cpg.load_prompt_catalog(format_slug)
    fam = catalog["families"][family]
    provider = fam.get("provider")
    recipe = cpg.GenerationRecipe(
        recipe_id=f"{format_slug}:master", format_slug=format_slug, family_name=family, engine=provider,
        provider_model="", engine_registry_hash="", format_contract_version="", material_source="",
        asset_type="", executor_version=digest, prompt_catalog_hash=digest, family=fam,
        provider_config={}, recipe_spec={})
    space = cpg.prompt_combination_space(recipe)
    return cpg._compose_prompt_combination(recipe, combination % space)


def variant_prompt(real_prompt: str, slots: dict[str, str], variant: dict) -> str:
    """The prompt a variant sends: the real one, edited, or its own template.

    A ``template`` replaces the catalog wording entirely but keeps the same
    slot picks ({angle}, {setting}, ...), so every variant in a round still
    describes the same shot. Paragraph breaks in a template are preserved.
    """
    template = variant.get("template")
    if variant.get("rewrite") == "h3":
        # Our own stand-in for H3's hosted prompt stage: a local LLM restates
        # the real prompt in H3's structured form (cached per exact prompt).
        import h3_prompt

        return h3_prompt.rewrite_for_h3(
            edit_prompt(real_prompt, variant.get("prompt_edits", [])), int(variant.get("duration", 3)))
    if not template:
        return edit_prompt(real_prompt, variant.get("prompt_edits", []))
    text = template
    for name, value in slots.items():
        text = text.replace("{" + name + "}", value)
    if "{" in text or "}" in text:
        raise SystemExit(f"variant {variant['name']!r} template has a slot the catalog does not fill")
    return "\n\n".join(" ".join(part.split()) for part in text.split("\n\n"))


def edit_prompt(prompt: str, edits: list[dict]) -> str:
    """Apply a variant's wording changes: remove / replace / append / prepend."""
    for edit in edits or []:
        if "remove" in edit:
            prompt = prompt.replace(edit["remove"], "")
        elif "replace" in edit:
            old, new = edit["replace"]
            prompt = prompt.replace(old, new)
        elif "append" in edit:
            prompt = prompt.rstrip() + " " + edit["append"]
        elif "prepend" in edit:
            prompt = edit["prepend"] + " " + prompt.lstrip()
    return " ".join(prompt.split())


# --- graph knobs ------------------------------------------------------------

KNOBS = ("steps", "lora_strength", "sampler", "scheduler", "sigma_shift", "easycache", "duration",
         "cfg", "negative", "lora_name", "sigmas")

HUNYUAN_FILES = {
    "unet": "hunyuanvideo1.5_720p_t2v_fp16.safetensors",
    "text_encoder": "qwen_2.5_vl_7b.safetensors",
    "glyph_encoder": "byt5_small_glyphxl_fp16.safetensors",
    "vae": "hunyuanvideo15_vae_fp16.safetensors",
}


def build_kandinsky_graph(prompt: str, base: dict, knobs: dict, seed: int) -> dict:
    """Kandinsky 5 Lite text-to-video, as ComfyUI's own template wires it."""
    seconds = int(knobs.get("duration", base.get("duration", 3)))
    frames = seconds * 24 // 4 * 4 + 1
    return {
        "clip": {"class_type": "DualCLIPLoader", "inputs": {
            "clip_name1": HUNYUAN_FILES["text_encoder"], "clip_name2": "clip_l.safetensors",
            "type": "kandinsky5", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": "hunyuan_video_vae_bf16.safetensors"}},
        "unet": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "kandinsky5lite_t2v_sft_5s.safetensors", "weight_dtype": "default"}},
        "shift": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["unet", 0], "shift": 5.0}},
        "positive": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["clip", 0]}},
        "negative": {"class_type": "CLIPTextEncode", "inputs": {"text": knobs.get("negative", ""), "clip": ["clip", 0]}},
        "k5": {"class_type": "Kandinsky5ImageToVideo", "inputs": {
            "positive": ["positive", 0], "negative": ["negative", 0], "vae": ["vae", 0],
            "width": int(base.get("width", 1280)), "height": int(base.get("height", 720)),
            "length": frames, "batch_size": 1}},
        "sample": {"class_type": "KSampler", "inputs": {
            "model": ["shift", 0], "positive": ["k5", 0], "negative": ["k5", 1], "latent_image": ["k5", 2],
            "seed": seed, "steps": int(knobs.get("steps", 50)), "cfg": float(knobs.get("cfg", 5)),
            "sampler_name": "euler_ancestral", "scheduler": "beta", "denoise": 1.0}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}},
        "video": {"class_type": "CreateVideo", "inputs": {"images": ["decode", 0], "fps": 24}},
        "save": {"class_type": "SaveVideo", "inputs": {
            "video": ["video", 0], "filename_prefix": "content-lab/recipe-lab", "format": "auto", "codec": "auto"}},
    }


LTX23_FILES = {
    "checkpoint": "ltx-2.3-22b-dev.safetensors",
    "text_encoder": "gemma_3_12B_it.safetensors",
    "lora": "ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors",
    "upscaler": "ltx-2.3-spatial-upscaler-x2-1.1.safetensors",
}


def build_ltx23_graph(prompt: str, base: dict, knobs: dict, seed: int) -> dict:
    """LTX 2.3 text-to-video as ComfyUI's own template runs it.

    Two passes: sample at half size, enlarge the latent with LTX's upscaler,
    then refine at full size. Sound is generated alongside but not decoded.
    """
    seconds = int(knobs.get("duration", base.get("duration", 3)))
    width, height = int(base.get("width", 1344)), int(base.get("height", 768))
    frames = seconds * 24 + 1                               # lengths are 8n + 1
    files = LTX23_FILES
    negative = knobs.get("negative") or "pc game, console game, video game, cartoon, childish, ugly"
    return {
        "ckpt": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": files["checkpoint"]}},
        "clip": {"class_type": "LTXAVTextEncoderLoader", "inputs": {
            "text_encoder": files["text_encoder"], "ckpt_name": files["checkpoint"], "device": "default"}},
        "audio_vae": {"class_type": "LTXVAudioVAELoader", "inputs": {"ckpt_name": files["checkpoint"]}},
        "upscaler": {"class_type": "LatentUpscaleModelLoader", "inputs": {"model_name": files["upscaler"]}},
        "lora": {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["ckpt", 0], "lora_name": files["lora"],
            "strength_model": float(knobs.get("lora_strength", 0.5))}},
        "positive": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["clip", 0]}},
        "negative": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["clip", 0]}},
        "cond": {"class_type": "LTXVConditioning", "inputs": {
            "positive": ["positive", 0], "negative": ["negative", 0], "frame_rate": 24}},
        # pass 1: half size
        "latent": {"class_type": "EmptyLTXVLatentVideo", "inputs": {
            "width": width // 2, "height": height // 2, "length": frames, "batch_size": 1}},
        "audio_latent": {"class_type": "LTXVEmptyLatentAudio", "inputs": {
            "frames_number": frames, "frame_rate": 24, "batch_size": 1, "audio_vae": ["audio_vae", 0]}},
        "av1": {"class_type": "LTXVConcatAVLatent", "inputs": {
            "video_latent": ["latent", 0], "audio_latent": ["audio_latent", 0]}},
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "sampler": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "sigmas1": {"class_type": "ManualSigmas", "inputs": {
            "sigmas": "1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0"}},
        "guider1": {"class_type": "CFGGuider", "inputs": {
            "model": ["lora", 0], "positive": ["cond", 0], "negative": ["cond", 1], "cfg": 1.0}},
        "sample1": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise", 0], "guider": ["guider1", 0], "sampler": ["sampler", 0],
            "sigmas": ["sigmas1", 0], "latent_image": ["av1", 0]}},
        "split1": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["sample1", 0]}},
        # pass 2: enlarge and refine
        "upsample": {"class_type": "LTXVLatentUpsampler", "inputs": {
            "samples": ["split1", 0], "upscale_model": ["upscaler", 0], "vae": ["ckpt", 2]}},
        "av2": {"class_type": "LTXVConcatAVLatent", "inputs": {
            "video_latent": ["upsample", 0], "audio_latent": ["split1", 1]}},
        "crop": {"class_type": "LTXVCropGuides", "inputs": {
            "positive": ["cond", 0], "negative": ["cond", 1], "latent": ["split1", 0]}},
        "noise2": {"class_type": "RandomNoise", "inputs": {"noise_seed": 42}},
        "sigmas2": {"class_type": "ManualSigmas", "inputs": {"sigmas": "0.85, 0.7250, 0.4219, 0.0"}},
        "guider2": {"class_type": "CFGGuider", "inputs": {
            "model": ["lora", 0], "positive": ["crop", 0], "negative": ["crop", 1], "cfg": 1.0}},
        "sample2": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise2", 0], "guider": ["guider2", 0], "sampler": ["sampler", 0],
            "sigmas": ["sigmas2", 0], "latent_image": ["av2", 0]}},
        "split2": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["sample2", 0]}},
        "decode": {"class_type": "VAEDecodeTiled", "inputs": {
            "samples": ["split2", 0], "vae": ["ckpt", 2], "tile_size": 768, "overlap": 64,
            "temporal_size": 4096, "temporal_overlap": 4}},
        "video": {"class_type": "CreateVideo", "inputs": {"images": ["decode", 0], "fps": 24}},
        "save": {"class_type": "SaveVideo", "inputs": {
            "video": ["video", 0], "filename_prefix": "content-lab/recipe-lab", "format": "auto", "codec": "auto"}},
    }


def ltx_with_first_frame(graph: dict, image_name: str, strength: float = 0.7) -> dict:
    """Start an LTX 2.3 clip from an uploaded picture, as its image-to-video template does."""
    full_w = graph["latent"]["inputs"]["width"] * 2
    full_h = graph["latent"]["inputs"]["height"] * 2
    graph["first_frame"] = {"class_type": "LoadImage", "inputs": {"image": image_name}}
    graph["first_fit"] = {"class_type": "ImageScale", "inputs": {
        "image": ["first_frame", 0], "upscale_method": "lanczos", "width": full_w, "height": full_h, "crop": "center"}}
    graph["first_prep"] = {"class_type": "LTXVPreprocess", "inputs": {"image": ["first_fit", 0], "img_compression": 18}}
    graph["i2v1"] = {"class_type": "LTXVImgToVideoInplace", "inputs": {
        "vae": ["ckpt", 2], "image": ["first_prep", 0], "latent": ["latent", 0], "strength": strength, "bypass": False}}
    graph["av1"]["inputs"]["video_latent"] = ["i2v1", 0]
    graph["i2v2"] = {"class_type": "LTXVImgToVideoInplace", "inputs": {
        "vae": ["ckpt", 2], "image": ["first_prep", 0], "latent": ["upsample", 0], "strength": 1.0, "bypass": False}}
    graph["av2"]["inputs"]["video_latent"] = ["i2v2", 0]
    return graph


def build_hunyuan_graph(prompt: str, base: dict, knobs: dict, seed: int) -> dict:
    """HunyuanVideo 1.5 text-to-video. Unlike H3 it has real guidance and a negative prompt."""
    seconds = int(knobs.get("duration", base.get("duration", 3)))
    frames = seconds * 24 // 4 * 4 + 1                     # 24 fps, lengths are 4n + 1
    model = ["unet", 0]
    graph: dict = {
        "clip": {"class_type": "DualCLIPLoader", "inputs": {
            "clip_name1": HUNYUAN_FILES["text_encoder"], "clip_name2": HUNYUAN_FILES["glyph_encoder"],
            "type": "hunyuan_video_15", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": HUNYUAN_FILES["vae"]}},
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": HUNYUAN_FILES["unet"], "weight_dtype": "default"}},
        "positive": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["clip", 0]}},
        "negative": {"class_type": "CLIPTextEncode", "inputs": {"text": knobs.get("negative", ""), "clip": ["clip", 0]}},
        "latent": {"class_type": "EmptyHunyuanVideo15Latent", "inputs": {
            "width": int(base.get("width", 1280)), "height": int(base.get("height", 720)),
            "length": frames, "batch_size": 1}},
    }
    if knobs.get("easycache") is not None:
        graph["easycache"] = {"class_type": "EasyCache", "inputs": {
            "model": model, "reuse_threshold": float(knobs["easycache"]),
            "start_percent": 0.15, "end_percent": 0.95, "verbose": False}}
        model = ["easycache", 0]
    graph["shift"] = {"class_type": "ModelSamplingSD3", "inputs": {
        "model": model, "shift": float(knobs.get("sigma_shift", 7))}}
    graph["noise"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}}
    graph["sampler"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": knobs.get("sampler", "euler")}}
    graph["sigmas"] = {"class_type": "BasicScheduler", "inputs": {
        "model": model, "scheduler": knobs.get("scheduler", "simple"),
        "steps": int(knobs.get("steps", 20)), "denoise": 1.0}}
    graph["guider"] = {"class_type": "CFGGuider", "inputs": {
        "model": ["shift", 0], "positive": ["positive", 0], "negative": ["negative", 0],
        "cfg": float(knobs.get("cfg", 6))}}
    graph["sample"] = {"class_type": "SamplerCustomAdvanced", "inputs": {
        "noise": ["noise", 0], "guider": ["guider", 0], "sampler": ["sampler", 0],
        "sigmas": ["sigmas", 0], "latent_image": ["latent", 0]}}
    graph["decode"] = {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}}
    graph["video"] = {"class_type": "CreateVideo", "inputs": {"images": ["decode", 0], "fps": 24}}
    graph["save"] = {"class_type": "SaveVideo", "inputs": {
        "video": ["video", 0], "filename_prefix": "content-lab/recipe-lab", "format": "auto", "codec": "auto"}}
    return graph


def build_graph(prompt: str, base: dict, knobs: dict, seed: int) -> dict:
    """The provider's own graph, then this variant's sampler/model adjustments."""
    if base.get("engine") == "hunyuan15":
        return build_hunyuan_graph(prompt, base, knobs, seed)
    if base.get("engine") == "ltx23":
        return build_ltx23_graph(prompt, base, knobs, seed)
    if base.get("engine") == "kandinsky5":
        return build_kandinsky_graph(prompt, base, knobs, seed)
    params = {k: v for k, v in base.items() if k != "engine"} | {"seed": seed}
    if "duration" in knobs:
        params["duration"] = knobs["duration"]
    if knobs.get("lora_strength") == 0:
        params["go_fast"] = False
        params["sample_steps"] = knobs.get("steps", 20)
    if "negative" in knobs:
        params["negative_prompt"] = knobs["negative"]
    graph = copy.deepcopy(comfyui.build_workflow(prompt, params))
    if "h3" not in graph:
        # Another engine the Lab's provider builds (Wan 2.2): its graph as the
        # Lab would send it, with only the output folder changed.
        for node in graph.values():
            if "filename_prefix" in node.get("inputs", {}):
                node["inputs"]["filename_prefix"] = "content-lab/recipe-lab"
        return graph

    if "steps" in knobs:
        graph["sigmas"]["inputs"]["steps"] = int(knobs["steps"])
    if "lora" in graph and knobs.get("lora_strength"):
        graph["lora"]["inputs"]["strength_model"] = float(knobs["lora_strength"])
    if "sampler" in knobs:
        graph["sampler"]["inputs"]["sampler_name"] = knobs["sampler"]
    if "scheduler" in knobs:
        graph["sigmas"]["inputs"]["scheduler"] = knobs["scheduler"]
    if knobs.get("lora_name") and "lora" in graph:
        # A different distillation of the same model, e.g. Pika's H3 Flex.
        graph["lora"]["inputs"]["lora_name"] = knobs["lora_name"]
    if knobs.get("sigmas"):
        graph["sigmas"] = {"class_type": "ManualSigmas", "inputs": {"sigmas": knobs["sigmas"]}}

    model = graph["guider"]["inputs"]["model"]
    if knobs.get("sigma_shift") is not None:
        graph["sigma_shift"] = {"class_type": "MiniMaxH3SigmaShift", "inputs": {
            "model": model, "shift_video": float(knobs["sigma_shift"]), "shift_audio": 3.0}}
        model = ["sigma_shift", 0]
    if knobs.get("easycache") is not None:
        graph["easycache"] = {"class_type": "EasyCache", "inputs": {
            "model": model, "reuse_threshold": float(knobs["easycache"]),
            "start_percent": 0.15, "end_percent": 0.95, "verbose": False}}
        model = ["easycache", 0]
    graph["guider"]["inputs"]["model"] = model
    if "model" in graph["sigmas"]["inputs"]:
        graph["sigmas"]["inputs"]["model"] = model
    graph["save"]["inputs"]["filename_prefix"] = "content-lab/recipe-lab"
    return graph


# --- running ----------------------------------------------------------------

_FAMILY_NODES = {
    "MiniMaxH3ImageToVideo": "h3", "MiniMaxH3ReferenceToVideo": "h3",
    "LTXVSeparateAVLatent": "h3",                 # H3 still frames (tools/h3_image.py)
    "MiniMaxH3ImageConditioning": "h3image",
    "EmptyHunyuanVideo15Latent": "hunyuan15",
    "Kandinsky5ImageToVideo": "kandinsky5",
    "EmptyLTXVLatentVideo": "ltx",
    "WanImageToVideo": "wan", "EmptyHunyuanLatentVideo": "wan",
}


def graph_family(graph: dict) -> str:
    """Which set of large models a graph loads."""
    for node in graph.values():
        family = _FAMILY_NODES.get(node.get("class_type"))
        if family:
            return family
    return "other"


class ServerDown(RuntimeError):
    """ComfyUI stopped answering or lost the job: an infrastructure fault, not a result."""


async def patient_get(client: httpx.AsyncClient, url: str, minutes: float = 15, **kwargs) -> httpx.Response:
    """GET that waits out a busy server.

    ComfyUI answers nothing while it unloads one large model and loads another
    (a Wan-to-H3 swap took well over 30 s), which is not the same as being down.
    """
    deadline = time.monotonic() + minutes * 60
    while True:
        try:
            return await client.get(url, timeout=30, **kwargs)
        except httpx.TimeoutException:
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(5)


async def prepare_server(client: httpx.AsyncClient, graph: dict) -> None:
    """Make sure only this graph's model family is in memory before it loads.

    ComfyUI keeps every model it has loaded. On this Mac H3 alone holds about
    190 GB, and loading a second family beside it got the server killed by the
    OS twice. So: wait until no other family is rendering, and if the last
    thing the server ran was a different family, ask it to drop its models.
    """
    url = comfy_url()
    mine = graph_family(graph)
    while True:
        queue = (await patient_get(client, f"{url}/queue")).json()
        active = [graph_family(item[2]) for key in ("queue_running", "queue_pending") for item in queue.get(key) or []]
        if all(family == mine for family in active):
            break
        await asyncio.sleep(5)
    if active:
        return                                    # same family already loaded or loading
    history = (await patient_get(client, f"{url}/history", params={"max_items": 1})).json()
    last = next((graph_family(entry["prompt"][2]) for entry in history.values()), None)
    if last is not None and last != mine:
        await client.post(f"{url}/free", json={"unload_models": True, "free_memory": True}, timeout=30)
        await asyncio.sleep(8)                    # applied on the server's queue thread


async def wait_for_server(client: httpx.AsyncClient, seconds: int = 600) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if (await client.get(f"{comfy_url()}/system_stats", timeout=10)).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(5)
    raise ServerDown("ComfyUI did not come back")


async def render(client: httpx.AsyncClient, graph: dict, dest: Path) -> float:
    """Submit one graph, wait, download the video. Returns seconds taken."""
    try:
        return await _render(client, graph, dest)
    except httpx.HTTPError as exc:
        raise ServerDown(f"lost contact with ComfyUI: {exc!r}") from exc


async def _render(client: httpx.AsyncClient, graph: dict, dest: Path) -> float:
    url = comfy_url()
    await prepare_server(client, graph)
    started = time.monotonic()
    resp = await client.post(f"{url}/prompt", json={"prompt": graph, "client_id": "recipe-lab"}, timeout=600)
    if resp.status_code != 200:
        raise RuntimeError(f"ComfyUI rejected the graph: {resp.text[:400]}")
    prompt_id = resp.json()["prompt_id"]
    while True:
        poll = await patient_get(client, f"{url}/history/{prompt_id}")
        history = poll.json().get(prompt_id) if poll.status_code == 200 else None
        if history:
            error = comfyui._execution_error(history)
            if error:
                raise RuntimeError(error)
            if (history.get("status") or {}).get("completed"):
                output = comfyui._video_output(history)
                if output is None:
                    raise RuntimeError("finished without a video")
                video = await client.get(f"{url}/view", params={
                    "filename": output["filename"], "subfolder": output.get("subfolder") or "",
                    "type": output.get("type") or "output"}, timeout=300)
                video.raise_for_status()
                dest.write_bytes(video.content)
                return time.monotonic() - started
        elif not await comfyui._still_queued(client, url, prompt_id):
            raise ServerDown("ComfyUI no longer has this job (it restarted or the job was cancelled)")
        await asyncio.sleep(3)


# --- measuring --------------------------------------------------------------

def _frames(path: Path, width: int = 160) -> np.ndarray:
    """Every frame, downscaled, as uint8 RGB [n, h, w, 3]."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout.strip().split(",")
    src_w, src_h = int(probe[0]), int(probe[1])
    height = max(2, round(src_h * width / src_w / 2) * 2)
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"scale={width}:{height}", "-f", "rawvideo",
         "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, height, width, 3)


def measure(path: Path) -> dict:
    """Cheap, model-free facts about one clip."""
    frames = _frames(path).astype(np.float32)
    luma = frames @ np.array([0.299, 0.587, 0.114], np.float32)
    per_frame = luma.mean(axis=(1, 2))
    settled = float(np.median(per_frame))
    opening = float(per_frame[: max(1, len(per_frame) // 12)].mean())
    r, g, b = frames[..., 0], frames[..., 1], frames[..., 2]
    red = (r > 150) & (r > 1.6 * np.maximum(g, b))
    motion = float(np.abs(np.diff(luma, axis=0)).mean()) if len(luma) > 1 else 0.0
    # Letterboxing: rows at the top and bottom that stay black for the whole clip.
    # Real bars are pure black and sit on both edges; a dark foreground (night
    # ground, a silhouette) is neither, so one-sided or merely dim rows do not count.
    row_peak = luma.max(axis=0).max(axis=1)
    dark = row_peak < 6
    top = int(np.argmax(~dark)) if (~dark).any() else len(dark)
    bottom = int(np.argmax(~dark[::-1])) if (~dark).any() else 0
    bars = (top + bottom) / len(dark) if top >= 2 and bottom >= 2 else 0.0
    mid = luma[len(luma) // 2]
    lap = (-4 * mid[1:-1, 1:-1] + mid[:-2, 1:-1] + mid[2:, 1:-1] + mid[1:-1, :-2] + mid[1:-1, 2:])
    return {
        "black_bars": round(float(bars), 3),
        "frames": int(len(frames)),
        "brightness": round(settled, 1),                       # 0-255, settled level
        "opening_ratio": round(opening / max(settled, 1.0), 2),  # < 1 means it fades up
        "red_fraction": round(float(red.mean()), 5),          # lit tail lights, red badges
        "motion": round(motion, 2),                            # 0 = frozen frame
        "sharpness": round(float(lap.var()), 1),
    }


_VISION_QUESTION = (
    "You are inspecting one frame of a video of a pickup truck. Reply with one line of JSON and nothing else, using "
    'exactly these keys: {"lettering_or_decals": true or false, "where": "short phrase, or empty", '
    '"badge_or_emblem": true or false, "lights_lit": true or false, "people_visible": true or false, '
    '"truck_visible": true or false, "lamps_visible": true or false, "open_bed_pickup": true or false}. '
    "lettering_or_decals is true if any letters, words, numbers, stickers or decals appear anywhere on the truck body, "
    "tailgate or windows, even if garbled or unreadable. badge_or_emblem is true if a maker's emblem, logo or badge "
    "of any size is visible anywhere on the truck, including the grille, tailgate, fenders, wheels, bumpers and tow "
    "hitch. lights_lit is true if any tail light, brake light, headlight or marker light is glowing. truck_visible "
    "is true if a substantial part of a truck is in the picture. lamps_visible is true if the truck has ordinary "
    "head lamp or tail lamp units that can be seen, lit or not; it is false if the places where lamps belong are "
    "blank bodywork. open_bed_pickup is true if the vehicle is a pickup truck with an open cargo bed behind the "
    "cab; it is false if it looks like an SUV, a van, or a truck whose bed is covered by a canopy or cap.")
# The truck judge, and the shape every format's judge takes in a spec's
# "vision" object: a question whose JSON answer holds booleans, plus which of
# them must be false or true for a clip to count as clean.
TRUCK_VISION = {
    "question": _VISION_QUESTION,
    "must_be_false": {"no_lettering": "lettering_or_decals", "no_emblem": "badge_or_emblem",
                      "lights_off": "lights_lit", "no_people": "people_visible"},
    "must_be_true": {"truck_in_view": "truck_visible"},
    # Checked on the whole frame only: a middle slice rightly shows no lamps.
    # Catches an over-cleaned first frame, where the edit erased the lamps.
    # is_pickup: a look still was once picked for having no badges while its cab ran back over
    # the bed like an SUV.
    "frame_must_be_true": {"has_lamps": "lamps_visible", "is_pickup": "open_bed_pickup"},
}


BROKEN_QUESTION = (
    ' Also add the key "image_broken" to the JSON: true if the picture shows obvious rendering failure, such as '
    "rippled or wavy line patterns across objects, glowing halos around them, melted or smeared shapes, or abstract "
    "fragments that are not real objects; false for an ordinary photograph or video frame.")


def vision_config(spec: dict) -> dict | None:
    setting = spec.get("vision")
    if not setting:
        return None
    return TRUCK_VISION if setting is True else setting


_JUDGE_DOWN_UNTIL = 0.0


def _judge_post(url: str, body: dict) -> httpx.Response:
    """One judge request, failing fast while the judge is known to be out.

    The shared Ollama server restarts now and then (retry once), and its models
    live on the external drive: when that drive stalls a request just hangs. A
    clip must not wait 40 minutes on a judge that cannot answer, so after a
    failure every call for the next ten minutes raises at once and the clip is
    kept unjudged (rescore picks it up later).
    """
    global _JUDGE_DOWN_UNTIL
    if time.monotonic() < _JUDGE_DOWN_UNTIL:
        raise RuntimeError("vision judge unavailable (Ollama not answering); clip left unjudged")
    for attempt in range(2):
        try:
            resp = httpx.post(url, json=body, timeout=120)
            resp.raise_for_status()
            return resp
        except (httpx.TransportError, httpx.HTTPStatusError):
            if attempt == 0:
                time.sleep(15)
    _JUDGE_DOWN_UNTIL = time.monotonic() + 600
    raise RuntimeError("vision judge unavailable (Ollama not answering); clip left unjudged")


def vision_facts(path: Path, frames: int, config: dict | None = None,
                 crop: tuple[int, int] | None = None) -> dict:
    """Ask a local vision model (Ollama) what OCR cannot see: lettering, emblems, lit lights.

    Two frames are inspected. A "must be false" fault in either frame counts,
    and a "must be true" property has to hold in both. Raises when the model is
    unreachable or answers without JSON, so a clip is never scored clean on a
    judge that did not run.
    """
    import base64
    import re

    config = dict(config or TRUCK_VISION)
    # Asked of every format: a 4-step H3 clip came out rippled and haloed and the
    # content questions alone still passed it.
    config["question"] = config["question"] + BROKEN_QUESTION
    config["must_be_false"] = dict(config.get("must_be_false", {}), not_broken="image_broken")
    # A crop is judged at its own size: it is what gets posted.
    shape = f"crop={crop[1]}:ih:{crop[0]}:0" if crop else "scale=1000:-2"
    false_keys = list(config.get("must_be_false", {}).values())
    true_keys = list(config.get("must_be_true", {}).values())
    if crop is None:
        true_keys += list(config.get("frame_must_be_true", {}).values())
    url = (os.getenv("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
    # The team's small always-loaded seat model. The 27b judge used before took
    # 9 s a call and, under this server's memory cap, evicted whatever large
    # model a teammate had loaded; this one answers in under a second and
    # shares the seat instead of fighting for it.
    model = os.getenv("RECIPE_LAB_VISION_MODEL") or "mimo-v2.6:9b-nvfp4"
    merged: dict = {key: False for key in false_keys} | {key: True for key in true_keys}
    where: list[str] = []
    # Which frames to look at. By default one a third of the way in and one at the end. A format
    # with a deliberate whip-pan names its own ("frames_at", fractions of the clip): the boat
    # format's whip sits a third of the way in, and that one blurred frame was read as a broken
    # render on every night clip (2026-11-23).
    indexes = ({min(frames - 1, max(0, int(frames * at))) for at in config["frames_at"]}
               if config.get("frames_at") else {frames // 3, max(0, frames - 3)})
    for index in sorted(indexes):
        png = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"select='eq(n\\,{index})',{shape}",
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-"], capture_output=True, check=True).stdout
        body = {"model": model, "stream": False, "think": False, "options": {"temperature": 0},
                "messages": [{"role": "user", "content": config["question"],
                              "images": [base64.b64encode(png).decode()]}]}
        resp = _judge_post(f"{url}/api/chat", body)
        match = re.search(r"\{.*\}", resp.json()["message"]["content"], re.S)
        if not match:
            raise RuntimeError("vision judge answered without JSON")
        answer = json.loads(match.group(0))
        for key in false_keys:
            merged[key] = merged[key] or bool(answer.get(key))
        for key in true_keys:
            merged[key] = merged[key] and bool(answer.get(key))
        if str(answer.get("where") or "").strip().lower() not in ("", "empty", "none", "n/a"):
            where.append(str(answer["where"])[:60])
    merged["where"] = "; ".join(dict.fromkeys(where))
    merged["model"] = model
    return merged


def crop_windows(width: int, height: int, mode: str = "both") -> list[tuple[int, int]]:
    """(x, width) of each 9:16 slice the Lab cuts from a wide render.

    Same arithmetic as providers.base.multi_crop_vertical, which does the real
    cutting; keep the two in step.
    """
    crop_w = int(height * 9 / 16)
    crop_w -= crop_w % 2
    margin = width - crop_w
    if margin <= 0:
        return []
    if mode == "both":
        offsets = [int(margin * 0.15), int(margin * 0.85), 0, int(margin / 2), margin]
    elif mode == "triptych":
        offsets = [0, int(margin / 2), margin]
    else:
        offsets = [int(margin * 0.15), int(margin * 0.85)]
    return [(x, crop_w) for x in offsets]


def crop_facts(path: Path, frames: int, config: dict, mode: str) -> list[dict]:
    """Judge each posted slice on its own: a fault outside a slice does not spoil it."""
    size = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout.strip().split(",")
    out = []
    for x, crop_w in crop_windows(int(size[0]), int(size[1]), mode):
        seen = vision_facts(path, frames, config, crop=(x, crop_w))
        faults = [check for check, key in config.get("must_be_false", {}).items() if seen.get(key, True)]
        if seen.get("image_broken"):
            faults.append("not_broken")
        faults += [check for check, key in config.get("must_be_true", {}).items() if not seen.get(key, False)]
        out.append({"x": x, "faults": faults, "where": seen.get("where", "")})
    return out


def apply_advisory(facts: dict, checks: dict, spec: dict) -> dict:
    """Move checks the format does not actually require out of the pass/fail set.

    They stay visible (facts["advisory"], and each slice's "notes") so a
    reviewer can still see, say, that the tail lamps are lit.
    """
    advisory = set(spec.get("advisory") or [])
    facts["advisory"] = {name: ok for name, ok in checks.items() if name in advisory}
    for crop in facts.get("crops") or []:
        every = crop.get("faults", []) + crop.get("notes", [])
        crop["faults"] = [f for f in every if f not in advisory]
        crop["notes"] = [f for f in every if f in advisory]
    if "crops" in facts:
        facts["usable_crops"] = sum(1 for c in facts["crops"] if not c["faults"])
    return {name: ok for name, ok in checks.items() if name not in advisory}


def score(video: Path, spec: dict) -> tuple[dict, dict]:
    """Everything the lab measures about one finished clip: (facts, checks)."""
    facts = measure(video)
    vision = vision_config(spec)
    if vision:
        facts["vision"] = vision_facts(video, facts["frames"], vision)
        if spec.get("crops"):
            facts["crops"] = crop_facts(video, facts["frames"], vision, spec["crops"])
    checks = judge(facts, spec.get("targets", {}), vision)
    return facts, apply_advisory(facts, checks, spec)


def rejudge_stored(run_dir: Path, spec_path: Path | None = None) -> None:
    """Re-apply targets and advisory rules to what was already measured: no model calls."""
    spec = json.loads((spec_path or run_dir / "spec.json").read_text())
    (run_dir / "spec.json").write_text(json.dumps(spec, indent=1))
    lock_file = (run_dir / ".results.lock").open("w")
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    rows = load_results(run_dir)
    for row in rows:
        if "error" in row:
            continue
        row["checks"] = apply_advisory(row["facts"], judge(row["facts"], spec.get("targets", {}), vision_config(spec)), spec)
    tmp = run_dir / "results.jsonl.tmp"
    tmp.write_text("".join(json.dumps(row) + "\n" for row in rows))
    tmp.replace(run_dir / "results.jsonl")
    fcntl.flock(lock_file, fcntl.LOCK_UN)
    write_sheet(run_dir)


def judge(facts: dict, targets: dict, config: dict | None = None) -> dict:
    """Pass/fail per check against the spec's targets. Missing targets are skipped."""
    checks: dict[str, bool] = {}
    vision = facts.get("vision")
    if vision:
        config = config or TRUCK_VISION
        for check, key in config.get("must_be_false", {}).items():
            checks[check] = not vision.get(key, True)
        if "image_broken" in vision:
            checks["not_broken"] = not vision["image_broken"]
        for check, key in config.get("must_be_true", {}).items():
            checks[check] = bool(vision.get(key, False))
        for check, key in config.get("frame_must_be_true", {}).items():
            if key in vision:                    # clips judged before this question existed are not failed on it
                checks[check] = bool(vision[key])
    if "black_bars" in facts:
        checks["no_black_bars"] = facts["black_bars"] < 0.04
    if "brightness" in targets:
        low, high = targets["brightness"]
        checks["brightness"] = low <= facts["brightness"] <= high
    if "min_opening_ratio" in targets:
        checks["no_fade_in"] = facts["opening_ratio"] >= targets["min_opening_ratio"]
    if "max_red_fraction" in targets:
        checks["no_red_accents"] = facts["red_fraction"] <= targets["max_red_fraction"]
    if "motion" in targets:
        low, high = targets["motion"]
        checks["motion"] = low <= facts["motion"] <= high
    if "min_sharpness" in targets:
        checks["sharp"] = facts["sharpness"] >= targets["min_sharpness"]
    return checks


def contact_strip(path: Path, dest: Path, frames: int) -> None:
    picks = "+".join(f"eq(n\\,{n})" for n in (1, frames // 2, max(1, frames - 2)))
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-vf", f"select='{picks}',scale=320:-2,tile=3x1",
         "-frames:v", "1", str(dest)], check=True)


# --- sweep ------------------------------------------------------------------

async def sweep(spec_path: Path, rounds: int, run_dir: Path | None) -> Path:
    spec = json.loads(spec_path.read_text())
    run_dir = run_dir or LAB_DIR / f"{spec['name']}-{datetime.now():%Y%m%d-%H%M}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "spec.json").write_text(json.dumps(spec, indent=1))
    results_path = run_dir / "results.jsonl"
    done = {(r["variant"], r["round"]) for r in load_results(run_dir)}
    write_sheet(run_dir)
    lock_file = (LAB_DIR / ".round.lock").open("w")
    async with httpx.AsyncClient() as client:
        for round_index in range(rounds):
            # One sweep renders at a time, a whole round each, so two sweeps of
            # different models take turns instead of reloading models per clip.
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            await asyncio.to_thread(fcntl.flock, lock_file, fcntl.LOCK_EX)
            # Re-read the spec every round, so variations can be added, dropped
            # or retuned while a long sweep keeps running.
            spec = json.loads(spec_path.read_text())
            (run_dir / "spec.json").write_text(json.dumps(spec, indent=1))
            base = dict(spec["base"])
            combination = spec["combinations"][round_index % len(spec["combinations"])] + 97 * (round_index // len(spec["combinations"]))
            seed = spec.get("seed", 1000) + round_index
            real_prompt, slots = catalog_prompt(spec["format"], spec["family"], combination)
            for variant in spec["variants"]:
                name = variant["name"]
                if (name, round_index) in done or round_index >= int(variant.get("rounds", rounds)):
                    continue
                knobs = {k: variant[k] for k in KNOBS if k in variant}
                prompt = variant_prompt(real_prompt, slots, variant)
                graph = build_graph(prompt, base, knobs, seed)
                stem = f"{name}__r{round_index:02d}"
                video = run_dir / f"{stem}.mp4"
                row = {"variant": name, "round": round_index, "combination": combination, "seed": seed,
                       "knobs": knobs, "prompt_edits": variant.get("prompt_edits", []),
                       "template": bool(variant.get("template")), "prompt": prompt, "file": video.name}
                (run_dir / "status.json").write_text(json.dumps({
                    "variant": name, "round": round_index, "started": f"{datetime.now():%H:%M:%S}",
                    "position": spec["variants"].index(variant) + 1, "of": len(spec["variants"]), "rounds": rounds}))
                write_sheet(run_dir)
                for attempt in range(3):
                    try:
                        row["seconds"] = round(await render(client, graph, video), 1)
                        row["facts"], row["checks"] = await asyncio.to_thread(score, video, spec)
                        contact_strip(video, run_dir / f"{stem}.jpg", row["facts"]["frames"])
                        (run_dir / f"{stem}.graph.json").write_text(json.dumps(graph))
                        break
                    except ServerDown as exc:
                        # The server died or dropped the job: that says nothing
                        # about this variation, so wait for it and try again.
                        print(f"{datetime.now():%H:%M:%S} round {round_index} {name}: {exc}; "
                              f"waiting for ComfyUI (attempt {attempt + 1} of 3)", flush=True)
                        if attempt == 2:
                            row["error"] = f"ComfyUI failed three times: {exc}"[:400]
                        else:
                            await wait_for_server(client)
                    except Exception as exc:  # noqa: BLE001 - one bad variant must not stop the sweep
                        row["error"] = (str(exc) or repr(exc))[:400]
                        break
                with (run_dir / ".results.lock").open("w") as guard:
                    fcntl.flock(guard, fcntl.LOCK_EX)
                    with results_path.open("a") as f:
                        f.write(json.dumps(row) + "\n")
                if "error" in row:
                    outcome = "FAILED: " + row["error"]
                else:
                    outcome = f"{row['seconds']}s, checks {sum(row['checks'].values())}/{len(row['checks'])}"
                    if "usable_crops" in row["facts"]:
                        outcome += f", usable crops {row['facts']['usable_crops']}/{len(row['facts']['crops'])}"
                print(f"{datetime.now():%H:%M:%S} round {round_index} {name}: {outcome}", flush=True)
                write_sheet(run_dir)
    return run_dir


# --- still first ----------------------------------------------------------------

def still_faults(path: Path, spec: dict) -> list[str]:
    """Whole-frame faults of one still picture, by the same judge and targets as the clips."""
    vision = vision_config(spec)
    facts = measure(path)
    facts["vision"] = vision_facts(path, 1, vision)
    checks = judge(facts, {k: v for k, v in spec.get("targets", {}).items() if k == "brightness"}, vision)
    return [name for name, ok in checks.items() if not ok and name not in (spec.get("advisory") or [])]


def grade_still(src: Path, dest: Path, limits: list[float]) -> float:
    """Darken (or lift) a still so its brightness sits inside the format's range.

    H3 stills come out a stop brighter than its clips, and a clip keeps the
    exposure of its first frame. One gamma curve on luma, colour untouched.
    Returns the brightness of the result.
    """
    import math

    low, high = limits
    now = measure(src)["brightness"]
    if low <= now <= high:
        dest.write_bytes(src.read_bytes())
        return now
    target = min(max((low + high) / 2 + (high - low) / 4, low), high) if now > high else (low + high) / 2
    gamma = 1.0
    for _ in range(4):                       # mean brightness is not exactly a power law, so correct twice or so
        gamma *= math.log(target / 255) / math.log(max(now, 1.0) / 255)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf",
                        f"format=yuv444p,lutyuv=y='pow(val/maxval\\,{gamma:.4f})*maxval',format=rgb24",
                        str(dest)], check=True)
        now = measure(dest)["brightness"]
        if abs(now - target) < 3:
            break
    return now


async def still_first(spec_path: Path, rounds: int, run_dir: Path, candidates: int, clean: bool,
                      first_round: int = 0) -> Path:
    """Pick (and optionally clean) a first frame, then let H3 animate it.

    Three passes over all rounds, so the big models are swapped twice in total
    instead of twice per clip: stills on the video model, cleaning on the image
    model, then the clips on the video model again.
    """
    import h3_image

    spec = json.loads(spec_path.read_text())
    if spec["base"].get("engine"):
        raise SystemExit("still-first is built for the H3 engine")
    name = (spec.get("still_first") or {}).get("variant", "h3-format")
    variant = next(v for v in spec["variants"] if v["name"] == name)
    knobs = {k: variant[k] for k in KNOBS if k in variant}
    width, height = comfyui._H3_SIZES["16:9"]
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "spec.json").write_text(json.dumps(spec, indent=1))
    done = {(r["variant"], r["round"]) for r in load_results(run_dir)}
    lock_file = (LAB_DIR / ".round.lock").open("w")
    await asyncio.to_thread(fcntl.flock, lock_file, fcntl.LOCK_EX)
    plan = []
    for round_index in range(first_round, first_round + rounds):
        combos = spec["combinations"]
        combination = combos[round_index % len(combos)] + 97 * (round_index // len(combos))
        real_prompt, slots = catalog_prompt(spec["format"], spec["family"], combination)
        plan.append({"round": round_index, "combination": combination, "seed": spec.get("seed", 1000) + round_index,
                     "prompt": variant_prompt(real_prompt, slots, variant), "catalog_prompt": real_prompt})

    def say(text: str) -> None:
        print(f"{datetime.now():%H:%M:%S} {text}", flush=True)

    async with httpx.AsyncClient() as client:
        # Pass 1: several stills per round, keep the one with the fewest faults.
        for job in plan:
            best = None
            for k in range(candidates):
                still = run_dir / f"still__r{job['round']:02d}_c{k}.png"
                if not still.exists():
                    graph = h3_image.still_graph(job["prompt"], width, height, job["seed"] * 100 + k)
                    await prepare_server(client, graph)
                    await h3_image.run_image(client, graph, still)
                faults = await asyncio.to_thread(still_faults, still, spec)
                say(f"round {job['round']} still {k}: {', '.join(faults) or 'clean'}")
                if best is None or len(faults) < len(best[1]):
                    best = (still, faults)
                if not faults:
                    break
            job["pick"], job["pick_faults"] = best
            limits = spec.get("targets", {}).get("brightness")
            if limits and "brightness" in job["pick_faults"]:
                graded = run_dir / f"still__r{job['round']:02d}_pick.png"
                level = grade_still(job["pick"], graded, limits)
                job["pick"] = graded
                job["pick_faults"] = [f for f in job["pick_faults"] if f != "brightness"]
                say(f"round {job['round']} pick graded to brightness {level:.0f}")
        # Pass 2: clean the picks that still have something drawn on the truck.
        if clean:
            instruction = (spec.get("still_first") or {}).get("instruction") or h3_image.TRUCK_CLEAN
            for job in plan:
                cleaned = run_dir / f"still__r{job['round']:02d}_clean.png"
                try:
                    if not cleaned.exists():
                        name_on_server = await h3_image.upload(client, job["pick"])
                        graph = h3_image.edit_graph(name_on_server, instruction, width, height, job["seed"])
                        await prepare_server(client, graph)
                        await h3_image.run_image(client, graph, cleaned)
                except Exception as exc:  # noqa: BLE001 - the unedited pick still gets animated
                    say(f"round {job['round']} cleaning FAILED: {(str(exc) or repr(exc))[:300]}")
                    continue
                job["clean"] = cleaned
                job["clean_faults"] = await asyncio.to_thread(still_faults, cleaned, spec)
                say(f"round {job['round']} cleaned still: {', '.join(job['clean_faults']) or 'clean'}")
        # Pass 3: animate.
        for job in plan:
            wanted = (spec.get("still_first") or {}).get("labels")
            for label, key in (("still-pick", "pick"), ("still-clean", "clean"), ("still-loop", "clean"),
                               ("still-anchored", "clean"), ("still-push", "clean"), ("still-ltx", "clean")):
                if key not in job or (label, job["round"]) in done or (wanted and label not in wanted):
                    continue
                stem = f"{label}__r{job['round']:02d}"
                video = run_dir / f"{stem}.mp4"
                row = {"variant": label, "round": job["round"], "combination": job["combination"],
                       "seed": job["seed"], "knobs": knobs, "prompt_edits": [], "template": True,
                       "prompt": job["prompt"], "file": video.name, "first_frame": job[key].name,
                       "still_faults": job[f"{key}_faults"]}
                try:
                    uploaded = await h3_image.upload(client, job[key])
                    if label == "still-ltx":
                        # LTX animates the cleaned frame several times faster than H3. It
                        # gets the Lab's plain prompt: the H3 field format means nothing to it.
                        row["prompt"] = job["catalog_prompt"]
                        graph = ltx_with_first_frame(build_ltx23_graph(
                            job["catalog_prompt"], {"width": width, "height": height, "duration": 3}, {}, job["seed"]),
                            uploaded)
                    else:
                        graph = build_graph(job["prompt"], dict(spec["base"]), knobs, job["seed"])
                    if label == "still-ltx":
                        pass
                    elif label == "still-anchored":
                        h3_image.with_anchors(graph, uploaded)
                    elif label == "still-push":
                        # The anchors are the same clean picture pushed in a little more each
                        # time, so the truck stays as drawn while the camera slowly moves.
                        push = float((spec.get("still_first") or {}).get("push", 1.06))
                        later = []
                        for n, fraction in enumerate((1 / 3, 2 / 3, 1.0)):
                            step = h3_image.zoomed(job[key], run_dir / f"still__r{job['round']:02d}_push{n}.png",
                                                   1 + (push - 1) * fraction)
                            later.append((fraction, await h3_image.upload(client, step)))
                        h3_image.with_anchors(graph, uploaded, later)
                    else:
                        h3_image.with_first_frame(graph, uploaded, also_last=label == "still-loop")
                    row["seconds"] = round(await render(client, graph, video), 1)
                    row["facts"], row["checks"] = await asyncio.to_thread(score, video, spec)
                    contact_strip(video, run_dir / f"{stem}.jpg", row["facts"]["frames"])
                    (run_dir / f"{stem}.graph.json").write_text(json.dumps(graph))
                    say(f"round {job['round']} {label}: {row['seconds']}s, checks "
                        f"{sum(row['checks'].values())}/{len(row['checks'])}, usable crops "
                        f"{row['facts'].get('usable_crops', '-')}/{len(row['facts'].get('crops', []))}")
                except Exception as exc:  # noqa: BLE001 - one bad clip must not stop the batch
                    row["error"] = (str(exc) or repr(exc))[:400]
                    say(f"round {job['round']} {label}: FAILED: {row['error']}")
                with (run_dir / ".results.lock").open("w") as guard:
                    fcntl.flock(guard, fcntl.LOCK_EX)
                    with (run_dir / "results.jsonl").open("a") as f:
                        f.write(json.dumps(row) + "\n")
                write_sheet(run_dir)
    return run_dir


def rescore(run_dir: Path, spec_path: Path | None = None, missing_only: bool = False) -> None:
    """Re-measure finished clips with the current checks (after adding or changing one).

    Safe beside a running sweep: scores are merged into whatever results.jsonl
    holds when the work is done, so clips the sweep added meanwhile are kept.
    """
    spec = json.loads((spec_path or run_dir / "spec.json").read_text())
    scored: dict[tuple[str, int], dict] = {}
    for row in load_results(run_dir):
        # A clip whose render finished but whose judging failed (the judge model
        # was busy or gone) is still a clip: score it now and clear the error.
        if "error" in row and not (run_dir / row.get("file", "-")).exists():
            continue
        if missing_only and "error" not in row and spec.get("crops") and "crops" in row.get("facts", {}):
            continue
        facts, checks = score(run_dir / row["file"], spec)
        scored[(row["variant"], row["round"])] = {"facts": facts, "checks": checks, "error": None}
        print(row["variant"], f"r{row['round']}", checks, f"usable crops {facts.get('usable_crops', '-')}", flush=True)
    lock_file = (run_dir / ".results.lock").open("w")
    fcntl.flock(lock_file, fcntl.LOCK_EX)
    rows = load_results(run_dir)
    for row in rows:
        row.update(scored.get((row["variant"], row["round"]), {}))
        if row.get("error") is None:
            row.pop("error", None)
    tmp = run_dir / "results.jsonl.tmp"
    tmp.write_text("".join(json.dumps(row) + "\n" for row in rows))
    tmp.replace(run_dir / "results.jsonl")
    fcntl.flock(lock_file, fcntl.LOCK_UN)
    write_sheet(run_dir)


def load_results(run_dir: Path) -> list[dict]:
    path = run_dir / "results.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def leaderboard(rows: list[dict]) -> list[dict]:
    board: dict[str, dict] = {}
    for row in rows:
        entry = board.setdefault(row["variant"], {"variant": row["variant"], "clips": 0, "clean": 0,
                                                  "errors": 0, "seconds": [], "fails": {}})
        if "error" in row:
            entry["errors"] += 1
            continue
        entry["clips"] += 1
        if row.get("seconds") is not None:
            entry["seconds"].append(row["seconds"])
        if "crops" in row["facts"]:
            entry["crops"] = entry.get("crops", 0) + len(row["facts"]["crops"])
            entry["usable_crops"] = entry.get("usable_crops", 0) + row["facts"]["usable_crops"]
        if all(row["checks"].values()):
            entry["clean"] += 1
        for check, ok in row["checks"].items():
            if not ok:
                entry["fails"][check] = entry["fails"].get(check, 0) + 1
    for entry in board.values():
        entry["avg_seconds"] = round(sum(entry["seconds"]) / len(entry["seconds"]), 1) if entry["seconds"] else None
        entry["clean_rate"] = round(entry["clean"] / entry["clips"], 2) if entry["clips"] else 0.0
        del entry["seconds"]
        entry["crop_rate"] = round(entry["usable_crops"] / entry["crops"], 2) if entry.get("crops") else None
    # What gets posted is the slices, so where they were judged they decide the
    # order; whole-frame cleanliness breaks ties.
    return sorted(board.values(), key=lambda e: (-(e["crop_rate"] or 0.0), -e["clean_rate"], e["avg_seconds"] or 1e9))


# --- review sheet -----------------------------------------------------------

_SHEET_CSS = """
:root{--bg:#f6f6f4;--fg:#1b1b1a;--muted:#6b6b66;--card:#fff;--line:#deded8;--ok:#1f7a3d;--bad:#b3261e}
@media (prefers-color-scheme: dark){:root{--bg:#141413;--fg:#ecece8;--muted:#9b9b94;--card:#1e1e1c;--line:#32322f;--ok:#6fcf8f;--bad:#f2867f}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:24px 0 8px}
p.note{color:var(--muted);margin:0 0 16px}
table{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.clip{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px}
.clip img,.clip video{width:100%;border-radius:4px;display:block}
.clip .meta{color:var(--muted);font-size:12px;margin-top:6px}
.ok{color:var(--ok)}.bad{color:var(--bad);font-weight:600}
code{font-size:12px}
"""


def _status_line(run_dir: Path) -> str:
    path = run_dir / "status.json"
    if not path.exists():
        return ""
    s = json.loads(path.read_text())
    return (f"<p class=note><b>Now rendering:</b> {html.escape(s['variant'])} "
            f"(variation {s['position']} of {s['of']}, round {s['round'] + 1} of {s['rounds']}), "
            f"sent to ComfyUI at {html.escape(s['started'])}. It may wait there behind other jobs.</p>")


def write_sheet(run_dir: Path) -> Path:
    rows = load_results(run_dir)
    spec = json.loads((run_dir / "spec.json").read_text())
    board = leaderboard(rows)
    out = [f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<meta http-equiv=refresh content=60><title>Recipe lab: {html.escape(spec['name'])}</title><style>{_SHEET_CSS}</style>",
           f"<h1>Recipe lab: {html.escape(spec['name'])}</h1>",
           f"<p class=note>{len(rows)} clips so far. Each wide render is posted as five tall slices; a slice is "
           f"usable when the judge finds no fault inside it. A clip is clean when the whole frame passes every check. "
           f"The checks catch fade-ins, wrong brightness, red accents, frozen or jittery motion and blur; "
           f"they cannot judge whether the truck looks right. This page reloads every minute. "
           f"Updated {datetime.now():%Y-%m-%d %H:%M:%S}.</p>",
           _status_line(run_dir),
           "<h2>Variants, best first</h2><table><tr><th>Variant</th><th>Usable slices</th><th>Clean whole frame</th><th>Clips</th>"
           "<th>Avg time</th><th>Failed checks</th><th>Settings</th></tr>"]
    knobs_by_variant = {v["name"]: {k: v[k] for k in KNOBS if k in v} | (
        {"prompt_edits": len(v["prompt_edits"])} if v.get("prompt_edits") else {}) | (
        {"prompt": "own template"} if v.get("template") else {}) for v in spec["variants"]}
    for entry in board:
        fails = ", ".join(f"{k} ×{n}" for k, n in sorted(entry["fails"].items())) or "none"
        out.append(
            f"<tr><td><b>{html.escape(entry['variant'])}</b></td><td>"
            + (f"{entry['usable_crops']}/{entry['crops']} ({round(entry['crop_rate'] * 100)}%)" if entry.get("crops") else "-")
            + f"</td><td>{entry['clean']}/{entry['clips']} "
            f"({round(entry['clean_rate'] * 100)}%)</td><td>{entry['clips']}"
            f"{' + ' + str(entry['errors']) + ' failed' if entry['errors'] else ''}</td>"
            f"<td>{entry['avg_seconds'] or '-'} s</td><td>{html.escape(fails)}</td>"
            f"<td><code>{html.escape(json.dumps(knobs_by_variant.get(entry['variant'], {})))}</code></td></tr>")
    out.append("</table>")
    for round_index in sorted({r["round"] for r in rows}, reverse=True):
        out.append(f"<h2>Round {round_index}</h2><div class=grid>")
        for row in [r for r in rows if r["round"] == round_index]:
            if "error" in row:
                out.append(f"<div class=clip><b>{html.escape(row['variant'])}</b>"
                           f"<div class='meta bad'>{html.escape(row['error'])}</div></div>")
                continue
            stem = row["file"][:-4]
            checks = " · ".join(f"<span class={'ok' if ok else 'bad'}>{html.escape(k)}</span>"
                                for k, ok in row["checks"].items())
            facts = row["facts"]
            out.append(
                f"<div class=clip><b>{html.escape(row['variant'])}</b>"
                f"<a href='{html.escape(row['file'])}'><img src='{html.escape(stem)}.jpg' alt=''></a>"
                f"<div class=meta>{checks}</div>"
                + (f"<div class=meta>slices: " + " ".join(
                    f"<span class={'ok' if not c['faults'] else 'bad'} "
                    f"title='{html.escape(', '.join(c['faults'] + c.get('notes', [])))}'>"
                    f"{'ok' if not c['faults'] else html.escape(c['faults'][0])}</span>" for c in facts["crops"])
                   + f" ({facts['usable_crops']}/{len(facts['crops'])} usable)</div>" if "crops" in facts else "")
                +
                f"<div class=meta>{row['seconds']} s · brightness {facts['brightness']} · opening "
                f"{facts['opening_ratio']} · red {facts['red_fraction']} · motion {facts['motion']}"
                f"{' · lettering: ' + html.escape(facts['vision']['where']) if facts.get('vision', {}).get('where') else ''}"
                f"</div></div>")
        out.append("</div>")
    sheet = run_dir / "index.html"
    sheet.write_text("\n".join(out))
    (run_dir / "leaderboard.json").write_text(json.dumps(board, indent=1))
    write_index(run_dir.parent)
    return sheet


def write_index(lab_dir: Path) -> Path:
    """One page listing every run with its current leader, newest first."""
    runs = sorted((p for p in lab_dir.iterdir() if (p / "leaderboard.json").exists()),
                  key=lambda p: (p / "leaderboard.json").stat().st_mtime, reverse=True)
    out = [f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<meta http-equiv=refresh content=60><title>Recipe lab</title><style>{_SHEET_CSS}</style>",
           "<h1>Recipe lab</h1><p class=note>Every tuning run on this Mac, most recently updated first. "
           "This page reloads every minute.</p>",
           "<table><tr><th>Run</th><th>Clips</th><th>Leader</th><th>Leader clean</th><th>Now rendering</th></tr>"]
    for run in runs:
        board = json.loads((run / "leaderboard.json").read_text())
        clips = sum(e["clips"] for e in board)
        leader = board[0] if board else None
        status = json.loads((run / "status.json").read_text()) if (run / "status.json").exists() else {}
        out.append(
            f"<tr><td><a href='{html.escape(run.name)}/index.html'><b>{html.escape(run.name)}</b></a></td>"
            f"<td>{clips}</td><td>{html.escape(leader['variant']) if leader else '-'}</td>"
            f"<td>{str(leader['clean']) + '/' + str(leader['clips']) if leader else '-'}</td>"
            f"<td>{html.escape(status.get('variant', '-'))} since {html.escape(status.get('started', '-'))}</td></tr>")
    out.append("</table>")
    index = lab_dir / "index.html"
    index.write_text("\n".join(out))
    return index


# --- post: what would actually be posted --------------------------------------

def slow_motion(src: Path, dest: Path, factor: float = 2.0) -> None:
    """Stretch a clip with motion-interpolated frames (CPU only, about 10 s for a 3 s clip)."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-filter:v",
         f"setpts={factor}*PTS,minterpolate=fps=24:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1",
         "-an", "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", str(dest)], check=True)


def post(run_dir: Path, factor: float = 2.0) -> Path:
    """Turn every finished clip into the pieces a page would post: slowed, then sliced.

    Skips clips already done, so it can be re-run while a sweep adds more.
    """
    spec = json.loads((run_dir / "spec.json").read_text())
    mode = spec.get("crops") or "both"
    out_dir = run_dir / "post"
    out_dir.mkdir(exist_ok=True)
    rows = [r for r in load_results(run_dir) if "error" not in r]
    for row in rows:
        stem = row["file"][:-4]
        slow = out_dir / f"{stem}.slow.mp4"
        if not slow.exists():
            slow_motion(run_dir / row["file"], slow, factor)
        size = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(slow)], capture_output=True, text=True, check=True).stdout.strip().split(",")
        for i, (x, crop_w) in enumerate(crop_windows(int(size[0]), int(size[1]), mode)):
            piece = out_dir / f"{stem}.slice{i}.mp4"
            if not piece.exists():
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(slow), "-vf", f"crop={crop_w}:ih:{x}:0",
                                "-an", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(piece)], check=True)
    out = [f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>Posted pieces: {html.escape(spec['name'])}</title><style>{_SHEET_CSS}"
           ".row{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin-bottom:6px}"
           ".row video{width:100%;border-radius:4px;display:block}.tag{font-size:12px;margin:2px 0 0}</style>",
           f"<h1>Posted pieces: {html.escape(spec['name'])}</h1>",
           f"<p class=note>Each render slowed {factor:g}x and cut into the five tall slices a page would post, "
           f"best variations first. A red label is the fault the judge found inside that slice. "
           f"<a href='../index.html'>Back to the run</a>. Updated {datetime.now():%Y-%m-%d %H:%M:%S}.</p>"]
    order = [e["variant"] for e in leaderboard(load_results(run_dir))]
    for variant in order:
        out.append(f"<h2>{html.escape(variant)}</h2>")
        for row in [r for r in rows if r["variant"] == variant]:
            stem = row["file"][:-4]
            crops = row["facts"].get("crops") or []
            out.append(f"<div class=meta>round {row['round']}"
                       + (f" · {row['facts']['usable_crops']}/{len(crops)} usable" if crops else "") + "</div><div class=row>")
            for i in range(len(crop_windows(1344, 768, mode))):
                faults = crops[i]["faults"] if i < len(crops) else None
                label = "" if faults is None else (
                    "<p class='tag ok'>ok</p>" if not faults else f"<p class='tag bad'>{html.escape(', '.join(faults))}</p>")
                out.append(f"<div><video src='{html.escape(stem)}.slice{i}.mp4' muted loop playsinline controls "
                           f"preload=metadata></video>{label}</div>")
            out.append("</div>")
    page = out_dir / "index.html"
    page.write_text("\n".join(out))
    return page


# --- compare: every model on the same scenes ------------------------------------

def compare(lab_dir: Path, runs: list[str], out_name: str = "bakeoff.html") -> Path:
    """One page: the same scenes down the side, each run's variations across."""
    table: dict[int, list[tuple[str, dict]]] = {}
    totals: dict[str, dict] = {}
    for run in runs:
        for row in load_results(lab_dir / run):
            label = f"{run} / {row['variant']}"
            total = totals.setdefault(label, {"clips": 0, "usable": 0, "slices": 0, "seconds": [], "errors": 0})
            if "error" in row:
                total["errors"] += 1
                continue
            total["clips"] += 1
            total["usable"] += row["facts"].get("usable_crops", 0)
            total["slices"] += len(row["facts"].get("crops", []))
            if row.get("seconds") is not None:
                total["seconds"].append(row["seconds"])
            table.setdefault(row["round"], []).append((run, row))
    out = [f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<meta http-equiv=refresh content=120><title>Model bake-off</title><style>{_SHEET_CSS}"
           ".grid{grid-template-columns:repeat(auto-fill,minmax(260px,1fr))}</style>",
           "<h1>Model bake-off: trucks</h1>",
           f"<p class=note>Every local model on the same scenes, scored by the same judge. A slice is one of the five "
           f"tall crops a page would post. Click a picture to play the clip. Render times are not comparable when "
           f"several jobs shared the queue. Updated {datetime.now():%Y-%m-%d %H:%M:%S}.</p>",
           "<table><tr><th>Run / variation</th><th>Usable slices</th><th>Clips</th><th>Avg render</th></tr>"]
    def rate(t):
        return t["usable"] / t["slices"] if t["slices"] else -1
    for label, t in sorted(totals.items(), key=lambda kv: -rate(kv[1])):
        avg = f"{sum(t['seconds']) / len(t['seconds']):.0f} s" if t["seconds"] else "-"
        share = f"{t['usable']}/{t['slices']} ({round(100 * rate(t))}%)" if t["slices"] else "-"
        out.append(f"<tr><td><b>{html.escape(label)}</b></td><td>{share}</td><td>{t['clips']}"
                   f"{' + ' + str(t['errors']) + ' failed' if t['errors'] else ''}</td><td>{avg}</td></tr>")
    out.append("</table>")
    for round_index in sorted(table):
        out.append(f"<h2>Scene {round_index + 1}</h2><div class=grid>")
        for run, row in sorted(table[round_index], key=lambda item: -item[1]["facts"].get("usable_crops", 0)):
            stem = row["file"][:-4]
            facts = row["facts"]
            crops = facts.get("crops", [])
            out.append(
                f"<div class=clip><b>{html.escape(run)}</b><br>{html.escape(row['variant'])}"
                f"<a href='{html.escape(run)}/{html.escape(row['file'])}'><img src='{html.escape(run)}/{html.escape(stem)}.jpg' alt=''></a>"
                f"<div class=meta>{facts.get('usable_crops', '-')}/{len(crops)} usable · {row.get('seconds', '-')} s · "
                f"motion {facts['motion']} · brightness {facts['brightness']}</div></div>")
        out.append("</div>")
    page = lab_dir / out_name
    page.write_text("\n".join(out))
    return page


# --- promote ----------------------------------------------------------------

def promote(run_dir: Path, variant: str) -> list[Path]:
    """Save a variant's exact graph as a reusable ComfyUI workflow (API format)."""
    spec = json.loads((run_dir / "spec.json").read_text())
    rows = [r for r in load_results(run_dir) if r["variant"] == variant and "error" not in r]
    if not rows:
        raise SystemExit(f"no finished clips for variant {variant!r} in {run_dir}")
    graph = json.loads((run_dir / f"{rows[-1]['file'][:-4]}.graph.json").read_text())
    if "h3" in graph:
        graph["h3"]["inputs"]["prompt"] = PROMPT_PLACEHOLDER
    else:
        graph["positive"]["inputs"]["text"] = PROMPT_PLACEHOLDER
    graph["noise"]["inputs"]["noise_seed"] = 0
    entry = next(e for e in leaderboard(load_results(run_dir)) if e["variant"] == variant)
    name = f"{spec['family']}.{variant}"
    record = {"name": name, "format": spec["format"], "family": spec["family"], "variant": variant,
              "knobs": rows[-1]["knobs"], "prompt_edits": rows[-1]["prompt_edits"],
              "evidence": {"run": run_dir.name, **{k: entry[k] for k in ("clips", "clean", "clean_rate", "avg_seconds")}},
              "prompt_placeholder": PROMPT_PLACEHOLDER, "workflow": graph}
    written = []
    WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)
    repo_file = WORKFLOW_DIR / f"{name}.json"
    repo_file.write_text(json.dumps(record, indent=1) + "\n")
    written.append(repo_file)
    if COMFY_USER_WORKFLOWS.parent.exists():
        COMFY_USER_WORKFLOWS.mkdir(parents=True, exist_ok=True)
        comfy_file = COMFY_USER_WORKFLOWS / f"{name}.json"
        comfy_file.write_text(json.dumps(graph, indent=1) + "\n")
        written.append(comfy_file)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sweep", help="render every variant for N rounds of real prompts")
    s.add_argument("spec", type=Path)
    s.add_argument("--rounds", type=int, default=1)
    s.add_argument("--run-dir", type=Path, help="continue an existing run")
    sh = sub.add_parser("sheet", help="rebuild the review sheet")
    sh.add_argument("run_dir", type=Path)
    p = sub.add_parser("promote", help="save a variant as a ComfyUI workflow")
    p.add_argument("run_dir", type=Path)
    p.add_argument("variant")
    sf = sub.add_parser("stillfirst", help="pick and clean a first frame, then animate it")
    sf.add_argument("spec", type=Path)
    sf.add_argument("--rounds", type=int, default=3)
    sf.add_argument("--first-round", type=int, default=0)
    sf.add_argument("--run-dir", type=Path, required=True)
    sf.add_argument("--candidates", type=int, default=3, help="stills to try per round before picking")
    sf.add_argument("--clean", action="store_true", help="also redraw the pick with MiniMax H3-Image")
    cm = sub.add_parser("compare", help="one page comparing several runs on the same scenes")
    cm.add_argument("runs", nargs="+", help="run folder names under projects/recipe-lab")
    po = sub.add_parser("post", help="slow each clip down and cut the slices a page would post")
    po.add_argument("run_dir", type=Path)
    po.add_argument("--factor", type=float, default=2.0)
    rs = sub.add_parser("rescore", help="re-measure finished clips with the current checks")
    rs.add_argument("run_dir", type=Path)
    rs.add_argument("--spec", type=Path, help="score against this spec instead of the run's saved copy")
    rs.add_argument("--missing", action="store_true", help="only clips whose slices were never judged")
    rs.add_argument("--stored", action="store_true",
                    help="re-apply the spec's targets and advisory list to stored measurements, without re-judging")
    args = parser.parse_args()
    if args.command == "sweep":
        print("run:", asyncio.run(sweep(args.spec, args.rounds, args.run_dir)))
    elif args.command == "sheet":
        print(write_sheet(args.run_dir))
    elif args.command == "stillfirst":
        print("run:", asyncio.run(still_first(args.spec, args.rounds, args.run_dir, args.candidates, args.clean,
                                               args.first_round)))
    elif args.command == "compare":
        print(compare(LAB_DIR, args.runs))
    elif args.command == "post":
        print(post(args.run_dir, args.factor))
    elif args.command == "rescore":
        if args.stored:
            rejudge_stored(args.run_dir, args.spec)
        else:
            rescore(args.run_dir, args.spec, args.missing)
    else:
        for path in promote(args.run_dir, args.variant):
            print("saved", path)


if __name__ == "__main__":
    main()
