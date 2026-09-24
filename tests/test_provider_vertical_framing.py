"""Regression coverage for landscape-only provider framing."""

import asyncio
import json
from pathlib import Path

import pytest
from PIL import Image

from providers.base import fit_to_vertical, multi_crop_vertical, still_to_video


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


@pytest.mark.asyncio
async def test_truck_both_mode_emits_five_real_vertical_crops(tmp_path: Path):
    source = tmp_path / "truck-master.mp4"
    rc, _, stderr = await _run(
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=160x90:d=1:r=10",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
    )
    assert rc == 0, stderr.decode()

    crops = await multi_crop_vertical(source, "both")

    assert [path.name for path in crops] == [
        "truck-master_crop0.mp4",
        "truck-master_crop1.mp4",
        "truck-master_crop2.mp4",
        "truck-master_crop3.mp4",
        "truck-master_crop4.mp4",
    ]
    for crop in crops:
        rc, stdout, stderr = await _run(
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "json", str(crop),
        )
        assert rc == 0, stderr.decode()
        stream = json.loads(stdout)["streams"][0]
        assert (stream["width"], stream["height"]) == (50, 90)


@pytest.mark.asyncio
async def test_still_to_video_holds_one_image_in_a_delivery_mp4(tmp_path: Path):
    source = tmp_path / "silhouette.jpg"
    Image.new("RGB", (576, 1024), (24, 12, 38)).save(source, quality=95)
    output = tmp_path / "silhouette.mp4"

    await still_to_video(source, output, 2)

    rc, stdout, stderr = await _run(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate:format=duration",
        "-of", "json", str(output),
    )
    assert rc == 0, stderr.decode()
    probe = json.loads(stdout)
    assert (probe["streams"][0]["width"], probe["streams"][0]["height"]) == (1080, 1920)
    assert probe["streams"][0]["r_frame_rate"] == "30/1"
    assert float(probe["format"]["duration"]) == pytest.approx(2.0, abs=0.05)
