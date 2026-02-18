"""Shared utilities for all video generation providers."""

import asyncio
import os
import re
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# API keys (all optional -- only configured providers appear in /api/providers)
# ---------------------------------------------------------------------------
API_KEYS = {
    "xai": os.getenv("XAI_API_KEY"),
    "fal": os.getenv("FAL_KEY"),
    "luma": os.getenv("LUMA_API_KEY"),
    "replicate": os.getenv("REPLICATE_API_TOKEN"),
    "openai": os.getenv("OPENAI_API_KEY"),
}

# Providers that always output 16:9 regardless of aspect_ratio setting
FORCE_LANDSCAPE = {"rep-minimax"}


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
):
    """Orchestrate a single video generation: call provider, download, crop."""
    from . import PROVIDERS

    entry = jobs[job_id]["videos"][index]
    try:
        # Organize: output/<provider>/<prompt_slug>/
        slug = slugify(prompt)
        sub_dir = OUTPUT_DIR / provider / slug
        sub_dir.mkdir(parents=True, exist_ok=True)
        rel_dir = f"{provider}/{slug}"

        async with httpx.AsyncClient() as client:
            entry["status"] = "generating"

            provider_info = PROVIDERS[provider]
            mod = provider_info["module"]

            params = {
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
                "duration": duration,
                "image_data_uri": image_data_uri,
                "entry": entry,
                "model_id": provider_info["models"][0],
            }

            if provider == "sora":
                filename = f"{job_id}_{index}.mp4"
                dest = sub_dir / filename
                params["dest"] = dest
                await mod.generate(prompt, params, client)
                entry["status"] = "done"
                entry["file"] = f"{rel_dir}/{filename}"
                entry["url"] = f"/output/{rel_dir}/{filename}"
                return

            video_url = await mod.generate(prompt, params, client)

            filename = f"{job_id}_{index}.mp4"
            dest = sub_dir / filename
            entry["status"] = "downloading"
            await download_video(client, video_url, dest)

            # Auto-crop to 9:16 for providers that only output 16:9
            if aspect_ratio == "9:16" and provider in FORCE_LANDSCAPE:
                entry["status"] = "cropping"
                await crop_to_vertical(dest)

            entry["status"] = "done"
            entry["file"] = f"{rel_dir}/{filename}"
            entry["url"] = f"/output/{rel_dir}/{filename}"
    except Exception as e:
        entry["status"] = "error"
        entry["error"] = str(e)
