"""Regression coverage for landscape-only provider framing."""

import asyncio
import json
from pathlib import Path

import pytest
from PIL import Image

from providers.base import fit_to_vertical


async def _run(*args: str) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout, stderr


@pytest.mark.asyncio
async def test_fit_to_vertical_preserves_full_landscape_inside_vertical_canvas(tmp_path: Path):
    source = tmp_path / "source.mp4"
    rc, _, stderr = await _run(
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=160x90:d=1:r=10",
        "-vf", "drawbox=x=0:y=20:w=20:h=50:color=red:t=fill,"
        "drawbox=x=140:y=20:w=20:h=50:color=green:t=fill",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
    )
    assert rc == 0, stderr.decode()

    await fit_to_vertical(source)

    rc, stdout, stderr = await _run(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json", str(source),
    )
    assert rc == 0, stderr.decode()
    stream = json.loads(stdout)["streams"][0]
    assert (stream["width"], stream["height"]) == (50, 90)

    frame = tmp_path / "frame.png"
    rc, _, stderr = await _run(
        "ffmpeg", "-y", "-ss", "0.5", "-i", str(source), "-frames:v", "1", str(frame),
    )
    assert rc == 0, stderr.decode()
    assert frame.stat().st_size > 0
    pixels = Image.open(frame).convert("RGB")
    middle = [pixels.getpixel((x, pixels.height // 2)) for x in range(pixels.width)]
    assert any(red > 100 and red > green * 1.5 for red, green, _ in middle[:10])
    assert any(green > 60 and green > red * 1.5 for red, green, _ in middle[-10:])
