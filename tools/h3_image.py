"""Still-first pieces for local H3: draw one frame, clean it, hand it to the video model.

Prompt wording alone does not stop H3 drawing emblems, bed decals, tailgate
lettering and plates on trucks (see the truck sweeps in the recipe lab). The
video model does keep whatever its first frame shows, so this file makes a
clean first frame:

  1. still_graph: one frame from the H3 video model itself (no extra download).
  2. edit_graph:  MiniMax H3-Image redraws that frame from an instruction such
                  as "remove every emblem, decal and plate".
  3. the cleaned frame is uploaded and wired in as MiniMaxH3ImageToVideo's
     first_frame.

Usage (ComfyUI running on this Mac):
  python tools/h3_image.py still  "<prompt>" out.png
  python tools/h3_image.py clean  frame.png clean.png [--instruction "..."] [--expand]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from providers import comfyui  # noqa: E402

IMAGE_FILES = {
    "unet": "minimax_h3_image_pruned_bf16.safetensors",
    "vae": "minimax_h3_image_vae_fp16.safetensors",
}
# The image model is tuned for about 4 to 20 megapixels; ComfyUI's own template
# scales a small picture up to 4 MP before editing. Twice the video frame is
# 2688x1536 (4.1 MP), which also keeps both sides on the 32 px grid.
WORK_SCALE = 2
# Written as plain "remove ..." orders, the way the model's own examples are.
# A first version that described every surface as bare also erased the lamps,
# and one that switched the lamps off produced blacked-out lamps that the video
# model then "repaired" mid-clip. The Lab's truck format only asks for unmarked
# surfaces and no lettering or plates, so the lamps are left alone. A clause about "round
# logos on the bumpers" was tried and removed: on a still that had none, the edit drew some.
TRUCK_CLEAN = (
    "In <Picture 1>, remove the maker's emblem from the truck's grille and tailgate, remove every badge, decal, "
    "sticker and piece of lettering from its body and windows, and remove the licence plate, leaving plain surfaces "
    "in the same paint. Keep everything else exactly the same: the truck's shape, its glossy dark paint "
    "and reflections, its wheels, its head lamps and tail lamps just as they are, the camera angle, the landscape "
    "and the light.")


def still_graph(prompt: str, width: int, height: int, seed: int, steps: int = 8) -> dict:
    """One frame from the H3 video model, as ComfyUI's own text-to-image template does it."""
    files = comfyui._H3_FILES
    return {
        "clip": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": files["text_encoder"], "type": "minimax", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": files["vae"]}},
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": files["unet"], "weight_dtype": "default"}},
        "lora": {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["unet", 0], "lora_name": files["lora"], "strength_model": 1.0}},
        "positive": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["clip", 0]}},
        "latent": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "sampler": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "sigmas": {"class_type": "BasicScheduler", "inputs": {
            "model": ["lora", 0], "scheduler": "simple", "steps": steps, "denoise": 1.0}},
        "guider": {"class_type": "BasicGuider", "inputs": {"model": ["lora", 0], "conditioning": ["positive", 0]}},
        "sample": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["noise", 0], "guider": ["guider", 0], "sampler": ["sampler", 0],
            "sigmas": ["sigmas", 0], "latent_image": ["latent", 0]}},
        "split": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["sample", 0]}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["split", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {
            "images": ["decode", 0], "filename_prefix": "content-lab/h3-still"}},
    }


def edit_graph(image_name: str, prompt: str, width: int, height: int, seed: int, steps: int = 6,
               full_size: bool = False) -> dict:
    """MiniMax H3-Image redraws one uploaded picture; the result comes back at width x height.

    full_size keeps the picture at the size the model drew it (WORK_SCALE times larger, about
    4 megapixels for a 1344x768 source) instead of shrinking it back: the detail a 1080p video's
    first frame needs. Same seed, same picture, so it is the larger twin of the normal result.
    """
    work_w, work_h = width * WORK_SCALE, height * WORK_SCALE
    if full_size:
        width, height = work_w, work_h
    return {
        "clip": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": comfyui._H3_FILES["text_encoder"], "type": "minimax", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": IMAGE_FILES["vae"]}},
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": IMAGE_FILES["unet"], "weight_dtype": "default"}},
        "source": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "enlarge": {"class_type": "ImageScale", "inputs": {
            "image": ["source", 0], "upscale_method": "lanczos", "width": work_w, "height": work_h, "crop": "center"}},
        "edit": {"class_type": "MiniMaxH3ImageConditioning", "inputs": {
            "clip": ["clip", 0], "vae": ["vae", 0], "prompt": prompt, "width": work_w, "height": work_h,
            "images.image_1": ["enlarge", 0]}},
        "negative": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["edit", 0]}},
        "sample": {"class_type": "KSampler", "inputs": {
            "model": ["unet", 0], "positive": ["edit", 0], "negative": ["negative", 0], "latent_image": ["edit", 1],
            "seed": seed, "steps": steps, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}},
        "shrink": {"class_type": "ImageScale", "inputs": {
            "image": ["decode", 0], "upscale_method": "area", "width": width, "height": height, "crop": "disabled"}},
        "save": {"class_type": "SaveImage", "inputs": {
            "images": ["shrink", 0], "filename_prefix": "content-lab/h3-clean"}},
    }


def with_first_frame(graph: dict, image_name: str, also_last: bool = False) -> dict:
    """Wire an uploaded picture into an H3 video graph as its first frame.

    With also_last the same picture is the last frame too: the clip has to come
    back to the clean frame, which stops the model switching lamps on or
    growing lettering halfway through, and makes the clip loop.
    """
    graph["first_frame"] = {"class_type": "LoadImage", "inputs": {"image": image_name}}
    graph["h3"]["inputs"]["first_frame"] = ["first_frame", 0]
    if also_last:
        graph["h3"]["inputs"]["last_frame"] = ["first_frame", 0]
    return graph


def zoomed(src: Path, dest: Path, zoom: float) -> Path:
    """The same picture pushed in by `zoom` about its centre, at its original size."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf",
                    f"scale=iw*{zoom:.4f}:ih*{zoom:.4f}:flags=lanczos,crop=iw/{zoom:.4f}:ih/{zoom:.4f},"
                    f"scale={_size(src)[0]}:{_size(src)[1]}:flags=lanczos", str(dest)], check=True)
    return dest


def with_anchors(graph: dict, first_name: str, later: list[tuple[float, str]] | None = None,
                 fractions: tuple[float, ...] = (1 / 3, 2 / 3, 1.0)) -> dict:
    """Pin a picture at the first frame and at points along the clip.

    The first frame alone is not enough on rear views: between a clean first
    and last frame H3 still lit the tail lamps and embossed the tailgate. Each
    anchor is one MiniMaxH3AddGuide node chained onto the conditioning.

    `later` is [(fraction of the clip, uploaded picture)]. Pinning the very
    same picture everywhere freezes the shot, so pass progressively zoomed
    copies (see zoomed) and the anchors themselves describe a slow push-in.
    Without `later` the first picture is reused at `fractions`.
    """
    with_first_frame(graph, first_name)
    length = int(graph["h3"]["inputs"]["length"])
    conditioning = ["h3", 0]
    for n, (fraction, name) in enumerate(later or [(f, first_name) for f in fractions]):
        image = ["first_frame", 0]
        if name != first_name:
            graph[f"anchor{n}_image"] = {"class_type": "LoadImage", "inputs": {"image": name}}
            image = [f"anchor{n}_image", 0]
        index = -1 if fraction >= 1.0 else int(round((length - 1) * fraction))
        graph[f"anchor{n}"] = {"class_type": "MiniMaxH3AddGuide", "inputs": {
            "positive": conditioning, "latent": ["h3", 1], "frame_idx": index,
            "vae": ["vae", 0], "image": image}}
        conditioning = [f"anchor{n}", 0]
    graph["guider"]["inputs"]["conditioning"] = conditioning
    return graph


def _image_output(history: dict) -> dict | None:
    for output in (history.get("outputs") or {}).values():
        for item in (output.get("images") or []) if isinstance(output, dict) else []:
            if isinstance(item, dict) and str(item.get("filename", "")).lower().endswith((".png", ".jpg", ".webp")):
                return item
    return None


async def run_image(client: httpx.AsyncClient, graph: dict, dest: Path) -> float:
    """Submit an image graph, wait, save its picture. Returns seconds taken."""
    url = (os.getenv("COMFYUI_SERVER_URL") or "http://127.0.0.1:8188").rstrip("/")
    started = time.monotonic()
    resp = await client.post(f"{url}/prompt", json={"prompt": graph, "client_id": "recipe-lab"}, timeout=600)
    if resp.status_code != 200:
        raise RuntimeError(f"ComfyUI rejected the graph: {resp.text[:600]}")
    prompt_id = resp.json()["prompt_id"]
    while True:
        try:
            poll = await client.get(f"{url}/history/{prompt_id}", timeout=30)
        except httpx.TimeoutException:        # busy swapping models, not gone
            await asyncio.sleep(5)
            continue
        history = poll.json().get(prompt_id) if poll.status_code == 200 else None
        if history:
            error = comfyui._execution_error(history)
            if error:
                raise RuntimeError(error)
            if (history.get("status") or {}).get("completed"):
                output = _image_output(history)
                if output is None:
                    raise RuntimeError("finished without a picture")
                image = await client.get(f"{url}/view", params={
                    "filename": output["filename"], "subfolder": output.get("subfolder") or "",
                    "type": output.get("type") or "output"}, timeout=300)
                image.raise_for_status()
                dest.write_bytes(image.content)
                return time.monotonic() - started
        elif not await comfyui._still_queued(client, url, prompt_id):
            raise RuntimeError("ComfyUI no longer has this job (it restarted or the job was cancelled)")
        await asyncio.sleep(2)


async def upload(client: httpx.AsyncClient, path: Path) -> str:
    url = (os.getenv("COMFYUI_SERVER_URL") or "http://127.0.0.1:8188").rstrip("/")
    return await comfyui._upload_image(client, url, path.read_bytes(), path.suffix.lstrip(".") or "png")


# --- instruction expansion ----------------------------------------------------

_TEMPLATE = (Path.home() / "ComfyUI/.venv/lib/python3.13/site-packages/comfyui_workflow_templates_json/templates"
             / "image_minimax_h3_image_edit.json")


def _template_prompts() -> tuple[str, str]:
    """(system prompt, task prompt) that ComfyUI's own edit template gives its prompt writer."""
    nodes = json.loads(_TEMPLATE.read_text())["nodes"]
    texts = {(n.get("title") or ""): (n.get("widgets_values") or [""])[0]
             for n in nodes if n.get("type") == "PrimitiveStringMultiline"}
    return texts["Text (system_prompt)"], texts["Text (task_prompt)"]


def expand_instruction(image: Path, instruction: str, ratio: str = "7:4") -> str:
    """Turn a short edit instruction into the long finished-picture description the model was trained on.

    MiniMax ships this step as a separate prompt-writer model and says any
    capable vision model can do it; here it is the local Ollama model.
    """
    system, task = _template_prompts()
    url = (os.getenv("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
    model = os.getenv("RECIPE_LAB_VISION_MODEL") or "qwen3.8:27b-nvfp4"
    png = subprocess.run(["ffmpeg", "-v", "error", "-i", str(image), "-vf", "scale=1024:-2", "-f", "image2pipe",
                          "-vcodec", "mjpeg", "-"], capture_output=True, check=True).stdout
    user = (f"{task}\n\nReference Media: <Picture 1>, aspect_ratio {ratio}\n"
            f"User Editing Instruction (JSON string): {json.dumps(instruction)}\n"
            f"User Aspect Ratio Selection: {ratio}")
    resp = httpx.post(f"{url}/api/chat", timeout=900, json={
        "model": model, "stream": False, "options": {"temperature": 0.2, "num_predict": 4096},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user, "images": [base64.b64encode(png).decode()]}]})
    resp.raise_for_status()
    text = resp.json()["message"]["content"].split("</think>")[-1]
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise RuntimeError("the prompt writer answered without JSON")
    caption = json.loads(match.group(0)).get("expanded_caption")
    if not isinstance(caption, str) or len(caption) < 80:
        raise RuntimeError("the prompt writer returned no usable description")
    return caption


# --- command line -------------------------------------------------------------

def _size(path: Path) -> tuple[int, int]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip().split(",")
    return int(out[0]), int(out[1])


async def _main(args: argparse.Namespace) -> None:
    async with httpx.AsyncClient() as client:
        if args.command == "still":
            took = await run_image(client, still_graph(args.prompt, args.width, args.height, args.seed), args.out)
        else:
            width, height = _size(args.image)
            prompt = args.instruction
            if args.expand:
                prompt = await asyncio.to_thread(expand_instruction, args.image, args.instruction)
                print("expanded:", prompt)
            name = await upload(client, args.image)
            took = await run_image(client, edit_graph(name, prompt, width, height, args.seed), args.out)
        print(f"{args.out} in {took:.1f}s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("still", help="one frame from the H3 video model")
    s.add_argument("prompt")
    s.add_argument("out", type=Path)
    s.add_argument("--width", type=int, default=1344)
    s.add_argument("--height", type=int, default=768)
    s.add_argument("--seed", type=int, default=1000)
    c = sub.add_parser("clean", help="redraw a picture from an instruction with MiniMax H3-Image")
    c.add_argument("image", type=Path)
    c.add_argument("out", type=Path)
    c.add_argument("--instruction", default=TRUCK_CLEAN)
    c.add_argument("--expand", action="store_true", help="let the local vision model write the long description first")
    c.add_argument("--seed", type=int, default=1000)
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
