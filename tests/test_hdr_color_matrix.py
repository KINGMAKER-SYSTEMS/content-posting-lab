"""HDR metadata restoration and diagnostic classification.

Declared PQ/HLG transfers restore each known source field independently after
colour correction. A complete BT.2020 tuple is diagnostic information; partial
or mixed metadata must not be described as SDR or cause known fields to be lost.

Committed real-file fixtures contain SDR footage with changed tags. They test
metadata handling, not HDR image quality. Self-contained synthetic tests encode
PQ/HLG values computed from linear light into 10-bit video. Optional external
master tests require HDR_FIXTURES_DIR. None proves phone display behavior.

Only run_color_correct callers receive this restoration. The separate Burn
commands use build_cc_filter/TIKTOK_ENCODE_ARGS and remain outside its scope.
"""

import asyncio
from array import array
import functools
import logging
import math
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from services import ffmpeg as ffmpeg_module
from services.ffmpeg import (
    _hdr_output_color_args,
    _is_hdr_color,
    _is_complete_hdr_color,
    _probe_input_color,
    delivery_encode_args,
    run_color_correct,
)


def _fixture(name: str) -> Path:
    root = os.environ.get("HDR_FIXTURES_DIR", "~/rt-base-wt/hdr-fixtures")
    return Path(root).expanduser() / name


_REPO_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "real"


def _repo_fixture(name: str) -> Path:
    """Resolve a fixture cut in-repo (tests/fixtures/real), not the read-only set."""
    return _REPO_FIXTURE_DIR / name


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
HLG_COLOR = {
    "color_space": "bt2020nc",
    "color_transfer": "arib-std-b67",
    "color_primaries": "bt2020",
    "color_range": "tv",
}
# A coherent constant-luminance HDR tuple: bt2020c matrix is legal, and any
# concrete range (tv or pc) is proof enough — we copy it, we never invent one.
PQ_BT2020C_COLOR = {
    "color_space": "bt2020c",
    "color_transfer": "smpte2084",
    "color_primaries": "bt2020",
    "color_range": "pc",
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
        (HDR_COLOR, True),       # complete coherent PQ
        (HLG_COLOR, True),       # complete coherent HLG
        (PQ_BT2020C_COLOR, True),  # bt2020c matrix + pc range are coherent
        # Declared HDR can have partial/mixed metadata. These cases distinguish
        # diagnostic completeness without suppressing known-field restoration.
        ({"color_transfer": "smpte2084", "color_primaries": "bt709",
          "color_space": "bt709", "color_range": "tv"}, False),  # PQ + bt709 primaries
        # Each field independently affects diagnostic completeness.
        ({"color_transfer": "smpte2084", "color_primaries": "bt709",
          "color_space": "bt2020nc", "color_range": "tv"}, False),  # PQ + bt709 primaries + bt2020nc matrix
        ({"color_transfer": "smpte2084", "color_primaries": "bt2020",
          "color_space": "bt2020nc", "color_range": "unknown"}, False),  # PQ + range "unknown"
        ({"color_transfer": "smpte2084", "color_primaries": "bt2020",
          "color_space": "bt709", "color_range": "tv"}, False),  # PQ + bt709 matrix
        ({"color_transfer": "smpte2084", "color_primaries": "bt2020",
          "color_space": "bt2020nc"}, False),  # PQ + missing range
        ({"color_transfer": "arib-std-b67", "color_primaries": "bt2020",
          "color_range": "tv"}, False),  # HLG + missing matrix
        # BT.2020 primaries are NOT proof of HDR on their own.
        ({"color_transfer": "bt709", "color_primaries": "bt2020"}, False),
        ({"color_transfer": "unknown", "color_primaries": "bt2020"}, False),
        ({"color_primaries": "bt2020"}, False),
        ({"color_transfer": "bt709"}, False),
    ],
)
def test_complete_hdr_tuple_is_diagnostic_only(color, expected):
    assert _is_complete_hdr_color(color) is expected


def test_hdr_output_color_args_copy_probed_values_verbatim():
    # PQ: the bt2020nc matrix echoes through.
    assert _hdr_output_color_args(HDR_COLOR) == [
        "-colorspace", "bt2020nc",
        "-color_primaries", "bt2020",
        "-color_trc", "smpte2084",
        "-color_range", "tv",
    ]
    # HLG echoes verbatim.
    assert _hdr_output_color_args(HLG_COLOR) == [
        "-colorspace", "bt2020nc",
        "-color_primaries", "bt2020",
        "-color_trc", "arib-std-b67",
        "-color_range", "tv",
    ]
    # The probed matrix is copied through — never hardcoded to bt2020nc.
    assert _hdr_output_color_args(PQ_BT2020C_COLOR) == [
        "-colorspace", "bt2020c",
        "-color_primaries", "bt2020",
        "-color_trc", "smpte2084",
        "-color_range", "pc",
    ]


def test_hdr_output_color_args_preserves_mixed_source_matrix():
    color = {**HDR_COLOR, "color_space": "bt709", "color_primaries": "bt709"}
    assert _hdr_output_color_args(color) == [
        "-colorspace", "bt709",
        "-color_primaries", "bt709",
        "-color_trc", "smpte2084",
        "-color_range", "tv",
    ]


@pytest.mark.parametrize("unknown", [None, "unknown", "N/A"])
def test_hdr_output_color_args_never_invent_missing_fields(unknown):
    color = {"color_transfer": "smpte2084"}
    if unknown is not None:
        color.update(color_space=unknown, color_primaries=unknown, color_range=unknown)
    assert _hdr_output_color_args(color) == ["-color_trc", "smpte2084"]
    with pytest.raises(KeyError):
        _hdr_output_color_args({"color_primaries": "bt2020"})


@pytest.mark.parametrize("transfer", ["smpte2084", "arib-std-b67"])
def test_declared_hdr_does_not_require_other_metadata(transfer):
    assert _is_hdr_color({"color_transfer": transfer})
    assert not _is_complete_hdr_color({"color_transfer": transfer})


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


# --- the probe must never block the event loop ------------------------------

@pytest.mark.asyncio
async def test_colour_probe_is_offloaded_and_does_not_block_the_loop(monkeypatch):
    """A slow ffprobe must not stall the loop while it runs (review B).

    The probe is dispatched via asyncio.to_thread; a fake probe that sleeps in
    its own thread must run concurrently with another coroutine, which keeps
    making progress on the same loop the whole time. If the probe were ever
    made inline again, the heartbeat below could not tick until the probe
    finished and this test fails.
    """
    probe_started = threading.Event()
    release_probe = threading.Event()
    heartbeats = []

    def slow_fake_probe(_input_path):
        probe_started.set()
        # Block in the worker thread (never the loop) until the test releases.
        release_probe.wait(timeout=10)
        return HDR_COLOR

    async def fake_to_thread(function, *args, **kwargs):
        if function is ffmpeg_module._probe_input_color:
            return await asyncio.get_running_loop().run_in_executor(
                None, functools.partial(slow_fake_probe, *args, **kwargs)
            )
        return await asyncio.to_thread(function, *args, **kwargs)

    captured = _capture_argv(monkeypatch)
    monkeypatch.setattr(ffmpeg_module.asyncio, "to_thread", fake_to_thread)

    async def heartbeat():
        ticks = 0
        while not release_probe.is_set():
            heartbeats.append(ticks)
            ticks += 1
            await asyncio.sleep(0.01)

    render = asyncio.create_task(
        run_color_correct("slow.mp4", "slow-out.mp4", None, source_duration_ms=2000)
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0.05)  # let the render task reach the awaited probe
    assert probe_started.is_set(), "colour probe never ran"

    pumper = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.15)
    # The loop stayed live while the probe thread was blocked.
    assert len(heartbeats) >= 3, heartbeats

    release_probe.set()
    await pumper
    await render
    # The probe result still reached the argv: HDR tags were emitted.
    assert captured[-9:] == [*HDR_TAGS, "slow-out.mp4"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("probe_result", "expected_line"),
    [
        (HDR_COLOR, "hdr"),
        (SDR_COLOR, "sdr"),
        (None, "failed"),
    ],
)
async def test_probe_outcome_is_logged_with_a_named_line(
    monkeypatch, caplog, probe_result, expected_line
):
    """Review C: 'SDR' must be distinguishable from 'probe failed' in the logs."""

    async def fake_to_thread(function, *_args, **_kwargs):
        if function is ffmpeg_module._probe_input_color:
            return probe_result
        return None

    _capture_argv(monkeypatch)
    monkeypatch.setattr(ffmpeg_module.asyncio, "to_thread", fake_to_thread)
    with caplog.at_level(logging.INFO, logger="ffmpeg"):
        await run_color_correct(
            f"{expected_line}-input.mp4", f"{expected_line}-out.mp4", None,
            source_duration_ms=2000,
        )
    named = [r for r in caplog.records if r.getMessage().startswith("hdr_probe:")]
    assert len(named) == 1
    assert f"hdr_probe: {expected_line} input=" in named[0].getMessage()
    assert f"{expected_line}-input.mp4" in named[0].getMessage()


@pytest.mark.asyncio
async def test_explicit_input_color_is_not_reprobed(monkeypatch, caplog):
    """The input_color kwarg skips the probe entirely (no to_thread call)."""
    calls = []

    async def fail_to_thread(function, *args, **kwargs):
        calls.append(function)
        raise AssertionError("no probe should be offloaded when input_color is given")

    _capture_argv(monkeypatch)
    monkeypatch.setattr(ffmpeg_module.asyncio, "to_thread", fail_to_thread)
    with caplog.at_level(logging.INFO, logger="ffmpeg"):
        await run_color_correct(
            "known.mp4", "known-out.mp4", None,
            input_color=HDR_COLOR, source_duration_ms=2000,
        )
    assert calls == []


@pytest.mark.asyncio
async def test_partial_hdr_restores_known_fields_and_logs_partial_metadata(
    monkeypatch, caplog
):
    color = {
        "color_space": "bt2020nc",
        "color_transfer": "smpte2084",
        "color_primaries": "unknown",
        "color_range": "pc",
    }
    captured = _capture_argv(monkeypatch)
    with caplog.at_level(logging.INFO, logger="ffmpeg"):
        await run_color_correct(
            "partial-pq.mp4", "out.mp4", {"brightness": 20},
            input_color=color, source_duration_ms=1000,
        )
    assert captured[-7:] == [
        "-colorspace", "bt2020nc", "-color_trc", "smpte2084",
        "-color_range", "pc", "out.mp4",
    ]
    assert "hdr_probe: hdr_partial_or_mixed input=partial-pq.mp4" in caplog.text


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


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe required",
)
@pytest.mark.parametrize(
    "matrix,primaries,transfer",
    [
        ("bt709", "bt709", "smpte2084"),
        ("bt2020nc", "bt2020", "smpte2084"),
        ("bt2020nc", "bt2020", "arib-std-b67"),
    ],
)
def test_real_hdr_encode_preserves_source_tags_and_pixels(
    tmp_path, monkeypatch, matrix, primaries, transfer
):
    source = tmp_path / "source.mp4"
    tagged = tmp_path / "tagged.mp4"
    untagged = tmp_path / "untagged.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
            "testsrc2=size=128x228:rate=10", "-t", "1",
            "-vf", f"setparams=colorspace={matrix}:color_primaries={primaries}:"
            f"color_trc={transfer}:range=limited",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
        ],
        check=True, capture_output=True, timeout=30,
    )
    expected = {
        "color_space": matrix,
        "color_transfer": transfer,
        "color_primaries": primaries,
        "color_range": "tv",
    }
    assert _probe_color(source) == expected
    asyncio.run(run_color_correct(str(source), str(tagged), {"brightness": 20}))
    assert _probe_color(tagged) == expected

    monkeypatch.setattr(ffmpeg_module, "_is_hdr_color", lambda color: False)
    asyncio.run(run_color_correct(str(source), str(untagged), {"brightness": 20}))
    assert _framemd5(tagged) == _framemd5(untagged)


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe required",
)
@pytest.mark.parametrize("transfer,primaries,color_range", [
    ("smpte2084", "bt2020", "tv"),
    ("arib-std-b67", "bt2020", "tv"),
    ("smpte2084", "unknown", "pc"),
    ("arib-std-b67", "unknown", "tv"),
])
def test_synthetic_hdr_signal_preserves_output_tuple_and_pixels(
    tmp_path, monkeypatch, caplog, transfer, primaries, color_range
):
    """Encode actual PQ/HLG grey levels, not SDR values with HDR labels."""
    width, height = 64, 96
    samples = []
    for row in range(height):
        linear = (0.01, 0.1, 1.0)[row * 3 // height]
        if transfer == "smpte2084":
            # ST2084 absolute luminance: linear=1 represents 1000 nits.
            powered = (linear / 10) ** (2610 / 16384)
            encoded = ((3424 / 4096 + 2413 / 128 * powered)
                       / (1 + 2392 / 128 * powered)) ** (2523 / 32)
        else:
            # HLG scene-linear OETF.
            a = 0.17883277
            b = 1 - 4 * a
            c = 0.5 - a * math.log(4 * a)
            encoded = (
                math.sqrt(3 * linear) if linear <= 1 / 12
                else a * math.log(12 * linear - b) + c
            )
        offset, span = (64, 876) if color_range == "tv" else (0, 1023)
        samples.extend([round(offset + span * encoded)] * width)
    samples.extend([512] * (width * height // 2))  # neutral 10-bit chroma
    frame = array("H", samples)
    if sys.byteorder != "little":
        frame.byteswap()
    raw = tmp_path / "linear-hdr.raw"
    raw.write_bytes(frame.tobytes() * 4)
    source = tmp_path / "hdr.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "rawvideo",
        "-pixel_format", "yuv420p10le", "-video_size", f"{width}x{height}",
        "-framerate", "4", "-i", str(raw), "-vf",
        f"setparams=colorspace=bt2020nc:color_primaries={primaries}:"
        f"color_trc={transfer}:range={color_range}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p10le", str(source),
    ], check=True, capture_output=True, timeout=30)
    expected = {
        "color_space": "bt2020nc", "color_transfer": transfer,
        "color_primaries": primaries, "color_range": color_range,
    }
    assert _probe_color(source) == expected
    tagged = tmp_path / "tagged.mp4"
    with caplog.at_level(logging.INFO, logger="ffmpeg"):
        _render_probe_path(source, tagged)
    assert _probe_color(tagged) == expected
    status = "hdr" if primaries == "bt2020" else "hdr_partial_or_mixed"
    assert f"hdr_probe: {status} input=hdr.mp4" in caplog.text
    # The metadata flags must not change the existing decoded YUV pixels.
    monkeypatch.setattr(ffmpeg_module, "_hdr_output_color_args", lambda color: [])
    untagged = tmp_path / "untagged.mp4"
    _render_probe_path(source, untagged)
    assert _framemd5(tagged) == _framemd5(untagged)


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


# --- integration: real probe path on real files (review B) ------------------

# Real files, cut on seeno from the real SDR master
# ~/source-archive/lovenightwalks/master-2qo-3600-4500.mp4 (bt709/bt709/bt709).
# See ~/rt-base-wt/hdr-fixtures/README.md for the exact ffmpeg commands.
_SDR_FIXTURES = (
    "sdr-bt709.mp4",                               # plain BT.709 SDR
    "sdr-bt2020-primaries-bt709-transfer.mp4",     # bt2020 primaries + bt709 transfer
    "sdr-incomplete-metadata.mp4",                 # bt2020 primaries + unknown transfer
)
_HLG_FIXTURE = "hlg-arib-std-b67.mp4"

# SDR footage with declared HDR transfers: preserve its known metadata without
# pretending this is a colour conversion or camera-HDR quality proof.
_PARTIAL_OR_MIXED_HDR_FIXTURES = (
    ("hdr-pq-bt709-primaries.mp4", "smpte2084"),   # PQ + bt709 primaries (mixed)
    ("hdr-pq-absent-primaries.mp4", "smpte2084"),  # PQ + absent primaries/matrix (partial)
    ("hdr-hlg-absent-matrix.mp4", "arib-std-b67"),  # HLG + absent matrix (partial)
)

_COLOUR_TOKENS = ("-colorspace", "-color_primaries", "-color_trc", "-color_range")


def _render_probe_path(source: Path, output: Path, cc: dict | None = None) -> None:
    """Render a short fixture through the real probe path (no input_color, no crop)."""
    asyncio.run(run_color_correct(
        str(source), str(output), cc if cc is not None else {"brightness": 20},
        encode_args=delivery_encode_args("tiktok_delivery_v1"),
        playback_speed=1.0,
    ))


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe required",
)
@pytest.mark.asyncio
@pytest.mark.parametrize("fixture_name", _SDR_FIXTURES)
async def test_real_probe_path_sdr_inputs_keep_pre_change_argv(monkeypatch, fixture_name):
    """A real ffprobe on a real SDR file must append no colour tags (pre-change argv).

    This exercises the probe path directly (no ``input_color`` seam): the
    BT.2020 primaries alone must not synthesize a PQ/HLG transfer, including
    when the existing transfer is absent or unknown.
    """
    source = _fixture(fixture_name)
    if not source.is_file():
        pytest.skip(f"fixture {fixture_name} not present")

    probed = _probe_input_color(str(source))
    assert probed is not None, "fixture must be probeable"
    assert probed.get("color_transfer") not in {"smpte2084", "arib-std-b67"}

    captured = _capture_argv(monkeypatch)
    await run_color_correct(
        str(source), "out.mp4", {"brightness": 20}, source_duration_ms=1000,
    )
    for token in _COLOUR_TOKENS:
        assert token not in captured, f"{token} must not appear for SDR input"


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe required",
)
@pytest.mark.parametrize(
    "fixture_name,expected_transfer",
    [
        ("sdr-bt709.mp4", "bt709"),
        ("sdr-bt2020-primaries-bt709-transfer.mp4", "bt709"),
        ("sdr-incomplete-metadata.mp4", "unknown"),
    ],
)
def test_real_probe_path_sdr_output_is_never_tagged_hdr(
    tmp_path, fixture_name, expected_transfer
):
    """Rendering a real SDR file must not emit HDR tags on the output."""
    source = _fixture(fixture_name)
    if not source.is_file():
        pytest.skip(f"fixture {fixture_name} not present")
    output = tmp_path / "out.mp4"
    _render_probe_path(source, output)
    tags = _probe_color(output)
    assert tags.get("color_transfer") == expected_transfer
    assert tags.get("color_transfer") not in {"smpte2084", "arib-std-b67"}
    assert tags.get("color_space") != "bt2020nc"


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe required",
)
@pytest.mark.asyncio
@pytest.mark.parametrize("fixture_name,transfer", _PARTIAL_OR_MIXED_HDR_FIXTURES)
async def test_real_probe_path_partial_or_mixed_hdr_restores_known_flags(
    monkeypatch, fixture_name, transfer
):
    """The real probe must restore every known field even for partial metadata."""
    source = _repo_fixture(fixture_name)
    if not source.is_file():
        pytest.skip(f"fixture {fixture_name} not present")

    probed = _probe_input_color(str(source))
    assert probed is not None, "fixture must be probeable"
    assert probed.get("color_transfer") == transfer, "fixture must be transfer-positive"
    assert _is_hdr_color(probed) is True
    assert _is_complete_hdr_color(probed) is False

    captured = _capture_argv(monkeypatch)
    await run_color_correct(
        str(source), "out.mp4", {"brightness": 20}, source_duration_ms=1000,
    )

    expected = []
    for field, flag in (
        ("color_space", "-colorspace"), ("color_primaries", "-color_primaries"),
        ("color_transfer", "-color_trc"), ("color_range", "-color_range"),
    ):
        if probed[field] != "unknown":
            expected.extend((flag, probed[field]))
    assert captured[-len(expected)-1:] == [*expected, "out.mp4"]


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe required",
)
@pytest.mark.parametrize("fixture_name,transfer", _PARTIAL_OR_MIXED_HDR_FIXTURES)
def test_real_probe_path_partial_or_mixed_hdr_preserves_output_tuple(
    tmp_path, caplog, fixture_name, transfer
):
    """Partial/mixed metadata stays partial/mixed; it is not converted to SDR."""
    source = _repo_fixture(fixture_name)
    if not source.is_file():
        pytest.skip(f"fixture {fixture_name} not present")
    output = tmp_path / "out.mp4"
    expected = _probe_color(source)
    assert expected["color_transfer"] == transfer
    with caplog.at_level(logging.INFO, logger="ffmpeg"):
        _render_probe_path(source, output)
    tags = _probe_color(output)
    assert tags == expected
    assert f"hdr_probe: hdr_partial_or_mixed input={fixture_name}" in caplog.text


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe required",
)
@pytest.mark.skipif(not _fixture(_HLG_FIXTURE).is_file(), reason="HLG fixture not present")
def test_hlg_render_restores_matrix_tag(tmp_path):
    """HLG is HDR and must keep bt2020nc / arib-std-b67 / bt2020 / tv end to end."""
    source = _fixture(_HLG_FIXTURE)
    output = tmp_path / "hlg-out.mp4"
    _render_probe_path(source, output)
    assert _probe_color(output) == {
        "color_space": "bt2020nc",
        "color_transfer": "arib-std-b67",
        "color_primaries": "bt2020",
        "color_range": "tv",
    }
