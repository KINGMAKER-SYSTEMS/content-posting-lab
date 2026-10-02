"""HDR colour-matrix tag restoration (lead decision, option a).

When the input is HDR (transfer smpte2084/arib-std-b67, or primaries bt2020) the
final Lab output must carry ``color_space=bt2020nc`` plus the input's
primaries/transfer/range, set explicitly on the output. No tone-mapping and no
pixel change happen: only the stream metadata tags move. SDR inputs must keep
the byte-identical ffmpeg command and filter graph.

The unit tests prove the command-level invariant (and are the red test on the
base commit: they assert the explicit tags are emitted for HDR input). The
integration test renders the real HDR excerpt through the production colour path
and checks the restored matrix tag plus pixel parity with the pre-change encode.
"""

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from services import ffmpeg as ffmpeg_module
from services.ffmpeg import (
    _hdr_output_color_args,
    _is_hdr_color,
    _probe_input_color,
    delivery_encode_args,
    run_color_correct,
)


def _fixture(name: str) -> Path:
    root = os.environ.get("HDR_FIXTURES_DIR", "~/rt-base-wt/hdr-fixtures")
    return Path(root).expanduser() / name


HDR_COLOR = {
    "color_space": "bt2020nc",
    "color_transfer": "smpte2084",
    "color_primaries": "bt2020",
    "color_range": "tv",
}
SDR_COLOR = {
    "color_space": "bt709",
    "color_transfer": "bt709",
    "color_primaries": "bt709",
    "color_range": "tv",
}


class _FakeProcess:
    """Minimal create_subprocess_exec double that records its argv."""

    returncode = 0

    class stderr:
        async def read(self, _size=-1):
            return b""

    async def wait(self):
        return self.returncode


def _capture_argv(monkeypatch, **kwargs):
    captured: list[str] = []

    async def fake_exec(*args, **_kw):
        captured.extend(args)
        return _FakeProcess()

    monkeypatch.setattr(ffmpeg_module.asyncio, "create_subprocess_exec", fake_exec)
    return captured


# --- pure detection / arg helpers ------------------------------------------

@pytest.mark.parametrize(
    "color,expected",
    [
        (None, False),
        ({}, False),
        (SDR_COLOR, False),
        (HDR_COLOR, True),
        ({"color_transfer": "arib-std-b67", "color_primaries": "bt2020"}, True),
        ({"color_transfer": "bt709", "color_primaries": "bt2020"}, True),
        ({"color_transfer": "smpte2084", "color_primaries": "bt709"}, True),
    ],
)
def test_is_hdr_color_uses_transfer_or_primaries(color, expected):
    assert _is_hdr_color(color) is expected


def test_hdr_output_color_args_fix_matrix_and_echo_input_metadata():
    assert _hdr_output_color_args(HDR_COLOR) == [
        "-colorspace", "bt2020nc",
        "-color_primaries", "bt2020",
        "-color_trc", "smpte2084",
        "-color_range", "tv",
    ]


def test_probe_input_color_parses_key_value_fields(monkeypatch):
    class Completed:
        returncode = 0

        def __init__(self, stdout):
            self.stdout = stdout

    monkeypatch.setattr(
        ffmpeg_module.subprocess, "run",
        lambda *a, **k: Completed(
            b"color_range=tv\ncolor_space=bt2020nc\n"
            b"color_transfer=smpte2084\ncolor_primaries=bt2020\n"
        ),
    )
    assert _probe_input_color("in.mp4") == HDR_COLOR


def test_probe_input_color_returns_none_on_failure(monkeypatch):
    def boom(*_a, **_k):
        raise FileNotFoundError

    monkeypatch.setattr(ffmpeg_module.subprocess, "run", boom)
    assert _probe_input_color("in.mp4") is None


# --- command-level invariant (red test on the base commit) -----------------

HDR_TAGS = [
    "-colorspace", "bt2020nc",
    "-color_primaries", "bt2020",
    "-color_trc", "smpte2084",
    "-color_range", "tv",
]


@pytest.mark.asyncio
async def test_hdr_input_emits_explicit_matrix_tags(monkeypatch):
    captured = _capture_argv(monkeypatch)
    await run_color_correct(
        "in.mp4", "out.mp4", {"brightness": 20},
        input_color=HDR_COLOR, source_duration_ms=2000,
    )
    argv = captured
    assert argv[-9:] == [*HDR_TAGS, "out.mp4"]


@pytest.mark.asyncio
async def test_sdr_input_command_is_byte_identical_to_pre_change(monkeypatch):
    captured = _capture_argv(monkeypatch)
    await run_color_correct(
        "in.mp4", "out.mp4", {"brightness": 20},
        input_color=SDR_COLOR, source_duration_ms=2000,
    )
    assert captured == [
        "ffmpeg", "-y",
        "-i", "in.mp4",
        "-vf",
        "format=rgba64le,colorchannelmixer=rr=1.200000:rg=0.000000:rb=0.000000:"
        "ra=-0.000000:gr=0.000000:gg=1.200000:gb=0.000000:ga=-0.000000:"
        "br=0.000000:bg=0.000000:bb=1.200000:ba=-0.000000,format=rgb24",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-profile:v", "high",
        "-level", "4.2",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-c:a", "copy",
        "out.mp4",
    ]
    for token in ("-colorspace", "-color_primaries", "-color_trc", "-color_range"):
        assert token not in captured


# --- integration: real HDR excerpt through the production colour path ------

_EXCERPT = "master-excerpt-400-416.mp4"
LOOK = {"brightness": -15, "contrast": 75, "saturation": 55}  # 0.85 / 1.75 / 1.55


def _probe_color(path: Path) -> dict[str, str]:
    out = subprocess.check_output(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries",
            "stream=color_space,color_transfer,color_primaries,color_range",
            "-of", "default=noprint_wrappers=1",
            str(path),
        ],
        text=True,
    )
    return {
        line.split("=")[0]: line.split("=")[1]
        for line in out.strip().splitlines()
        if "=" in line
    }


def _framemd5(path: Path) -> list[str]:
    out = subprocess.check_output(
        [
            "ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0",
            "-f", "framemd5", "-",
        ],
        text=True,
    )
    return out.strip().splitlines()


def _render(excerpt: Path, output: Path) -> None:
    asyncio.run(run_color_correct(
        str(excerpt), str(output), LOOK, scale=None,
        encode_args=delivery_encode_args("tiktok_delivery_v1"),
        playback_speed=1.0,
        clip_crop={"zoom": 1.0, "focusX": 0.5, "focusY": 0.5},
        clip_crop_size=(1080, 1920),
        clip_start_ms=6000, clip_duration_ms=2000,
    ))


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe required",
)
@pytest.mark.skipif(not _fixture(_EXCERPT).is_file(), reason="HDR fixture not present")
def test_hdr_render_restores_matrix_tag_and_keeps_pixels_identical(tmp_path, monkeypatch):
    excerpt = _fixture(_EXCERPT)
    tagged = tmp_path / "tagged.mp4"
    untagged = tmp_path / "untagged.mp4"

    # Head: production path emits the explicit HDR matrix/primaries/transfer tags.
    _render(excerpt, tagged)
    assert _probe_color(tagged) == {
        "color_space": "bt2020nc",
        "color_transfer": "smpte2084",
        "color_primaries": "bt2020",
        "color_range": "tv",
    }

    # Base: same pixels, tags suppressed (simulates the pre-change command).
    monkeypatch.setattr(ffmpeg_module, "_is_hdr_color", lambda color: False)
    _render(excerpt, untagged)
    assert _framemd5(tagged) == _framemd5(untagged)
