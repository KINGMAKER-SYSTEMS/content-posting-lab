"""Real-ffmpeg checks for each creative transform (services/clip_transforms.py)."""
import shutil
import subprocess
from pathlib import Path

import pytest

from services.clip_transforms import CutTransforms, MOTIONS, _probe, apply_transforms
from services.ffmpeg import delivery_encode_args

ENCODE = delivery_encode_args("tiktok_delivery_v1")


def _clip(path: Path, seconds: float = 6.0) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required")
    # A left-to-right gradient whose brightness rises over time: mirror,
    # motion and reverse each change a measurable pixel.
    subprocess.run([
        "ffmpeg", "-nostdin", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=black:s=1080x1920:r=30:d={seconds},format=gray,"
        "geq=lum='clip(X*255/W*0.5+T*20,0,255)'",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:d={seconds}",
        "-shortest", *ENCODE, str(path),
    ], check=True, capture_output=True)


def _pixel(path: Path, at: float, x: int, y: int = 960) -> int:
    raw = subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-ss", f"{at}", "-i", str(path), "-frames:v", "1",
        "-vf", f"crop=1:1:{x}:{y},format=gray", "-f", "rawvideo", "pipe:1",
    ], check=True, capture_output=True).stdout
    return raw[0]


@pytest.mark.parametrize("transforms", [
    CutTransforms(mirror=True), *[CutTransforms(motion=motion) for motion in MOTIONS],
    CutTransforms(reverse=True), CutTransforms(mirror=True, motion="push_in", reverse=True),
], ids=lambda value: "+".join(name for name, on in value.manifest().items() if on) or "none")
def test_each_transform_keeps_the_length_size_and_audio(tmp_path, transforms):
    source = tmp_path / "cut.mp4"
    _clip(source)
    before = _probe(source)
    target = tmp_path / "out.mp4"
    shutil.copy(source, target)
    apply_transforms(target, transforms, ENCODE)
    after = _probe(target)
    assert after[1:] == before[1:], "1080x1920 with its audio"
    assert after[0] == pytest.approx(before[0], abs=0.1)
    assert not list(tmp_path.glob(".*transforms")), "work files are removed"


def test_mirror_reverse_and_motion_each_change_the_picture(tmp_path):
    source = tmp_path / "cut.mp4"
    _clip(source)
    left, right = _pixel(source, 1.0, 40), _pixel(source, 1.0, 1040)
    assert right > left + 50
    mirrored = tmp_path / "mirror.mp4"
    shutil.copy(source, mirrored)
    apply_transforms(mirrored, CutTransforms(mirror=True), ENCODE)
    assert _pixel(mirrored, 1.0, 40) > _pixel(mirrored, 1.0, 1040) + 50
    reversed_clip = tmp_path / "reverse.mp4"
    shutil.copy(source, reversed_clip)
    apply_transforms(reversed_clip, CutTransforms(reverse=True), ENCODE)
    assert _pixel(source, 0.5, 540) + 40 < _pixel(source, 5.0, 540)
    assert _pixel(reversed_clip, 0.5, 540) > _pixel(reversed_clip, 5.0, 540) + 40, "brightness now falls"
    pushed = tmp_path / "push.mp4"
    shutil.copy(source, pushed)
    apply_transforms(pushed, CutTransforms(motion="push_in"), ENCODE)
    # Zooming in moves the left edge's pixel toward the brighter centre by the end.
    assert _pixel(pushed, 5.5, 40) > _pixel(source, 5.5, 40) + 3


def _streams(path: Path) -> dict:
    import json as _json
    raw = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,start_time,duration,color_primaries,color_transfer,color_space",
                          "-of", "json", str(path)], check=True, capture_output=True, text=True).stdout
    return {stream["codec_type"]: stream for stream in _json.loads(raw)["streams"]}


def test_reverse_keeps_audio_in_step_with_the_picture(tmp_path):
    source = tmp_path / "cut.mp4"
    _clip(source, seconds=6.5)
    target = tmp_path / "reverse.mp4"
    shutil.copy(source, target)
    apply_transforms(target, CutTransforms(reverse=True), ENCODE)
    streams = _streams(target)
    video, audio = streams["video"], streams["audio"]
    assert abs(float(audio["start_time"]) - float(video["start_time"])) <= 0.001
    assert abs(float(audio["duration"]) - float(video["duration"])) <= 0.034, "within one frame"


def test_transforms_keep_the_cut_colour_tags(tmp_path):
    source = tmp_path / "hdr.mp4"
    _clip(source)
    tagged = tmp_path / "tagged.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(source), "-c", "copy",
                    "-bsf:v", "h264_metadata=colour_primaries=9:transfer_characteristics=16:matrix_coefficients=9",
                    str(tagged)], check=True, capture_output=True)
    before = _streams(tagged)["video"]
    assert before["color_primaries"] == "bt2020"
    apply_transforms(tagged, CutTransforms(mirror=True, reverse=True), ENCODE)
    after = _streams(tagged)["video"]
    for key in ("color_primaries", "color_transfer", "color_space"):
        assert after[key] == before[key], key


def test_disabled_transforms_keep_exact_bytes_without_probing_or_encoding(tmp_path, monkeypatch):
    source = tmp_path / "cut.mp4"
    _clip(source, seconds=0.5)
    before = source.read_bytes()
    entries = set(tmp_path.iterdir())

    def unexpected(*_args, **_kwargs):
        raise AssertionError("disabled effects must not run a subprocess")

    monkeypatch.setattr(subprocess, "run", unexpected)
    apply_transforms(source, CutTransforms(), ENCODE)
    assert source.read_bytes() == before
    assert set(tmp_path.iterdir()) == entries
