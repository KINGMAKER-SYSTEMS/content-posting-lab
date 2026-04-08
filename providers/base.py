"""Shared utilities for all video generation providers."""

import asyncio
import logging
import os
import re
from pathlib import Path

import httpx
from dotenv import load_dotenv

log = logging.getLogger("providers")

load_dotenv()

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# API keys (all optional -- only configured providers appear in /api/providers)
# ---------------------------------------------------------------------------
API_KEYS = {
    "xai": (os.getenv("XAI_API_KEY") or "").strip() or None,
    "replicate": (os.getenv("REPLICATE_API_TOKEN") or "").strip() or None,
    "openai": (os.getenv("OPENAI_API_KEY") or "").strip() or None,
}

# Providers that always output 16:9 regardless of aspect_ratio setting
FORCE_LANDSCAPE = {"hailuo"}


async def download_video(client: httpx.AsyncClient, url: str, dest: Path):
    """Download a video from URL to local file."""
    async with client.stream("GET", url, timeout=120) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            async for chunk in resp.aiter_bytes(8192):
                f.write(chunk)


async def crop_to_vertical(src: Path) -> None:
    """Center-crop a 16:9 video to 9:16 in-place using ffmpeg."""
    tmp = src.with_suffix(".tmp.mp4")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-vf",
        "crop=ih*9/16:ih",
        "-c:a",
        "copy",
        str(tmp),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg crop failed: {stderr.decode()[-200:]}")
    tmp.replace(src)


async def multi_crop_vertical(src: Path, mode: str) -> list[Path]:
    """Crop a 16:9 video into multiple 9:16 segments.

    Args:
        src: Source 16:9 video.
        mode: "dual" for 2 staggered crops, "triptych" for 3 even crops,
              "both" for dual + triptych combined (5 crops).

    Returns:
        List of output file Paths (does NOT delete the source).
    """
    # Probe source width
    probe = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        str(src),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await probe.communicate()
    parts = stdout.decode().strip().split(",")
    src_w, src_h = int(parts[0]), int(parts[1])

    crop_w = int(src_h * 9 / 16)  # 9:16 width from height
    margin = src_w - crop_w

    if mode == "both":
        # Dual (2 staggered) + Triptych (3 even) = 5 unique crops
        tri_spacing = margin / 2
        x_offsets = [
            # Dual crops
            int(margin * 0.15),
            int(margin * 0.85),
            # Triptych crops
            0,
            int(tri_spacing),
            margin,
        ]
    elif mode == "triptych":
        # 3 even crops: left, center, right
        spacing = margin / 2
        x_offsets = [0, int(spacing), margin]
    else:
        # dual: 2 staggered crops offset by 1/3 of remaining space
        x_offsets = [int(margin * 0.15), int(margin * 0.85)]

    outputs: list[Path] = []
    for i, x in enumerate(x_offsets):
        suffix = f"_crop{i}"
        out = src.with_stem(src.stem + suffix)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-i", str(src),
            "-vf", f"crop={crop_w}:{src_h}:{x}:0",
            "-c:a", "copy",
            str(out),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            out.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg multi-crop failed: {stderr.decode()[-200:]}")
        outputs.append(out)

    return outputs


def slugify(text: str, max_len: int = 40) -> str:
    """Turn a prompt into a filesystem-safe folder name."""
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s[:max_len] or "untitled"


async def generate_one(
    job_id: str,
    index: int,
    provider: str,
    prompt: str,
    aspect_ratio: str,
    resolution: str,
    duration: int,
    image_data_uri: str | None,
    jobs: dict,
    output_dir: Path | None = None,
    url_prefix: str = "/output",
    on_complete=None,
    **extra,
):
    """Orchestrate a single video generation: call provider, download, crop.

    Args:
        output_dir: Base directory for video files. Defaults to OUTPUT_DIR.
        url_prefix: URL path prefix for serving videos. Defaults to "/output".
    """
    from . import PROVIDERS

    base_dir = output_dir or OUTPUT_DIR
    entry = jobs[job_id]["videos"][index]
    try:
        # Organize: <base_dir>/<provider>/<prompt_slug>_<job_short>/
        # Each run gets its own folder so repeat prompts don't merge.
        slug = slugify(prompt)
        job_short = job_id[:8]
        folder = f"{slug}_{job_short}"
        sub_dir = base_dir / provider / folder
        sub_dir.mkdir(parents=True, exist_ok=True)
        rel_dir = f"{provider}/{folder}"

        async with httpx.AsyncClient() as client:
            entry["status"] = "generating"
            log.info("job=%s idx=%d provider=%s starting", job_id, index, provider)

            provider_info = PROVIDERS[provider]
            mod = provider_info["module"]

            params = {
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
                "duration": duration,
                "image_data_uri": image_data_uri,
                "entry": entry,
                "model_id": provider_info["models"][0],
                **extra,
            }

            video_url = await mod.generate(prompt, params, client)

            filename = f"{job_id}_{index}.mp4"
            dest = sub_dir / filename
            entry["status"] = "downloading"
            await download_video(client, video_url, dest)

            # Multi-crop mode: split one 16:9 into multiple 9:16 crops
            crop_mode = extra.get("crop_mode")
            if crop_mode in ("dual", "triptych", "both") and provider in FORCE_LANDSCAPE:
                entry["status"] = "cropping"
                crop_paths = await multi_crop_vertical(dest, crop_mode)
                # Store crop files in the entry
                entry["status"] = "done"
                entry["crops"] = []
                for cp in crop_paths:
                    crop_rel = f"{rel_dir}/{cp.name}"
                    entry["crops"].append({
                        "file": crop_rel,
                        "url": f"{url_prefix}/{crop_rel}",
                    })
                # Use the first crop as the primary file
                entry["file"] = entry["crops"][0]["file"]
                entry["url"] = entry["crops"][0]["url"]
            elif aspect_ratio == "9:16" and provider in FORCE_LANDSCAPE:
                # Auto-crop to 9:16 for providers that only output 16:9
                entry["status"] = "cropping"
                await crop_to_vertical(dest)
                entry["status"] = "done"
                entry["file"] = f"{rel_dir}/{filename}"
                entry["url"] = f"{url_prefix}/{rel_dir}/{filename}"
            else:
                entry["status"] = "done"
                entry["file"] = f"{rel_dir}/{filename}"
                entry["url"] = f"{url_prefix}/{rel_dir}/{filename}"
            log.info("job=%s idx=%d done: %s", job_id, index, entry['file'])
            if on_complete:
                on_complete(job_id)
    except Exception as e:
        err_msg = str(e) or repr(e)
        log.error("job=%s idx=%d provider=%s error: %s", job_id, index, provider, err_msg, exc_info=True)
        entry["status"] = "error"
        entry["error"] = err_msg
        if on_complete:
            on_complete(job_id)
