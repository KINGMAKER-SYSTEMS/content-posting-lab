"""Unit tests for routers/clipper.py video-info parsing and clip geometry.

These cover the pure-logic parts of `_get_video_info` (ffprobe JSON parsing,
rotation swap, duration fallbacks, validation) and `_clip_segment` (9:16
detection + crop strategy) by faking `asyncio.create_subprocess_exec` so no real
ffmpeg/ffprobe binary is invoked.
"""

import json
from pathlib import Path

import pytest

from routers import clipper


class _FakeProc:
    """Minimal stand-in for an asyncio subprocess.

    `communicate()` returns the canned (stdout, stderr) and exposes `returncode`.
    """

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):  # pragma: no cover - only hit on the timeout path
        pass


def _patch_probe(monkeypatch, responses):
    """Patch clipper.asyncio.create_subprocess_exec to pop from `responses`.

    `responses` is a list of _FakeProc objects, returned in call order. Captures
    every argv passed so geometry assertions can inspect the built command.
    """
    calls: list[list[str]] = []
    queue = list(responses)

    async def fake_exec(*args, **kwargs):
        calls.append([str(a) for a in args])
        return queue.pop(0)

    monkeypatch.setattr(clipper.asyncio, "create_subprocess_exec", fake_exec)
    return calls


# ── _get_video_info ──────────────────────────────────────────────────


async def test_basic_landscape(monkeypatch):
    probe = {
        "streams": [{"width": 1920, "height": 1080, "duration": "30.0"}],
        "format": {"duration": "30.0"},
    }
    _patch_probe(monkeypatch, [_FakeProc(stdout=json.dumps(probe).encode())])
    info = await clipper._get_video_info(Path("x.mp4"))
    assert info == {"duration": 30.0, "width": 1920, "height": 1080}


@pytest.mark.parametrize("rotation", [90, -90, 270, -270])
async def test_rotation_swaps_dimensions(monkeypatch, rotation):
    # Stored 1920x1080 but rotated → displayed should be 1080x1920.
    probe = {
        "streams": [
            {
                "width": 1920,
                "height": 1080,
                "duration": "12.0",
                "side_data_list": [{"rotation": rotation}],
            }
        ],
        "format": {"duration": "12.0"},
    }
    _patch_probe(monkeypatch, [_FakeProc(stdout=json.dumps(probe).encode())])
    info = await clipper._get_video_info(Path("x.mp4"))
    assert info["width"] == 1080
    assert info["height"] == 1920


async def test_rotation_180_does_not_swap(monkeypatch):
    probe = {
        "streams": [
            {
                "width": 1920,
                "height": 1080,
                "duration": "12.0",
                "side_data_list": [{"rotation": 180}],
            }
        ],
    }
    _patch_probe(monkeypatch, [_FakeProc(stdout=json.dumps(probe).encode())])
    info = await clipper._get_video_info(Path("x.mp4"))
    assert info["width"] == 1920
    assert info["height"] == 1080


async def test_falls_back_to_format_duration(monkeypatch):
    # Stream has no usable duration → must read it from format.
    probe = {
        "streams": [{"width": 1080, "height": 1920}],
        "format": {"duration": "47.5"},
    }
    _patch_probe(monkeypatch, [_FakeProc(stdout=json.dumps(probe).encode())])
    info = await clipper._get_video_info(Path("x.mp4"))
    assert info["duration"] == 47.5


async def test_raw_format_probe_last_resort(monkeypatch):
    # Neither stream nor format JSON has a duration → second ffprobe call returns raw.
    probe = {"streams": [{"width": 1080, "height": 1920}], "format": {}}
    calls = _patch_probe(
        monkeypatch,
        [
            _FakeProc(stdout=json.dumps(probe).encode()),
            _FakeProc(stdout=b"63.2\n"),
        ],
    )
    info = await clipper._get_video_info(Path("x.mp4"))
    assert info["duration"] == 63.2
    assert len(calls) == 2  # confirms the raw-probe fallback actually ran


async def test_zero_stream_duration_is_ignored(monkeypatch):
    # duration "0" must not be accepted; format value should win.
    probe = {
        "streams": [{"width": 1080, "height": 1920, "duration": "0"}],
        "format": {"duration": "20.0"},
    }
    _patch_probe(monkeypatch, [_FakeProc(stdout=json.dumps(probe).encode())])
    info = await clipper._get_video_info(Path("x.mp4"))
    assert info["duration"] == 20.0


async def test_no_duration_raises(monkeypatch):
    probe = {"streams": [{"width": 1080, "height": 1920}], "format": {}}
    _patch_probe(
        monkeypatch,
        [
            _FakeProc(stdout=json.dumps(probe).encode()),
            _FakeProc(stdout=b""),  # raw probe also empty
        ],
    )
    with pytest.raises(RuntimeError, match="Could not determine video duration"):
        await clipper._get_video_info(Path("x.mp4"))


async def test_image_like_short_duration_raises(monkeypatch):
    probe = {
        "streams": [{"width": 1080, "height": 1080, "duration": "0.04"}],
        "format": {"duration": "0.04"},
    }
    _patch_probe(monkeypatch, [_FakeProc(stdout=json.dumps(probe).encode())])
    with pytest.raises(RuntimeError, match="looks like an image"):
        await clipper._get_video_info(Path("x.mp4"))


async def test_ffprobe_nonzero_returncode_raises(monkeypatch):
    _patch_probe(monkeypatch, [_FakeProc(stderr=b"boom", returncode=1)])
    with pytest.raises(RuntimeError, match="ffprobe failed"):
        await clipper._get_video_info(Path("x.mp4"))


# ── _clip_segment geometry ───────────────────────────────────────────


def _last_cmd(calls):
    """The argv of the (single) ffmpeg invocation _clip_segment makes."""
    assert len(calls) == 1
    return calls[0]


def _vf(cmd):
    """Extract the -vf filtergraph argument, or None if absent."""
    if "-vf" in cmd:
        return cmd[cmd.index("-vf") + 1]
    return None


async def test_clip_already_9_16_full_res_no_crop(monkeypatch, tmp_path):
    out = tmp_path / "clip.mp4"
    out.write_bytes(b"x")  # make output.exists() True
    calls = _patch_probe(monkeypatch, [_FakeProc(returncode=0)])
    ok = await clipper._clip_segment(
        source=tmp_path / "src.mp4", output=out, start=0.0,
        clip_duration=10.0, src_width=1080, src_height=1920,
    )
    assert ok
    # Already 9:16 at full res → re-encode only, no scale/crop filter.
    assert _vf(_last_cmd(calls)) is None


async def test_clip_already_9_16_low_res_scales_up(monkeypatch, tmp_path):
    out = tmp_path / "clip.mp4"
    out.write_bytes(b"x")
    calls = _patch_probe(monkeypatch, [_FakeProc(returncode=0)])
    await clipper._clip_segment(
        source=tmp_path / "src.mp4", output=out, start=0.0,
        clip_duration=10.0, src_width=540, src_height=960,
    )
    assert _vf(_last_cmd(calls)) == "scale=1080:1920:flags=lanczos"


async def test_clip_landscape_crops_sides(monkeypatch, tmp_path):
    out = tmp_path / "clip.mp4"
    out.write_bytes(b"x")
    calls = _patch_probe(monkeypatch, [_FakeProc(returncode=0)])
    await clipper._clip_segment(
        source=tmp_path / "src.mp4", output=out, start=0.0,
        clip_duration=10.0, src_width=1920, src_height=1080,
    )
    # Wider-than-9:16 → crop width = height*0.5625 = 607, centered.
    vf = _vf(_last_cmd(calls))
    assert vf == "crop=607:1080:656:0,scale=1080:1920:flags=lanczos"


async def test_clip_taller_than_9_16_crops_top_bottom(monkeypatch, tmp_path):
    out = tmp_path / "clip.mp4"
    out.write_bytes(b"x")
    calls = _patch_probe(monkeypatch, [_FakeProc(returncode=0)])
    # 1080x2400 → ratio 0.45 < 0.5625, i.e. *narrower/taller* than 9:16.
    await clipper._clip_segment(
        source=tmp_path / "src.mp4", output=out, start=0.0,
        clip_duration=10.0, src_width=1080, src_height=2400,
    )
    # Taller than 9:16 → keep full width, crop height = width/0.5625 = 1920,
    # centered vertically: crop_y = (2400-1920)//2 = 240.
    vf = _vf(_last_cmd(calls))
    assert vf == "crop=1080:1920:0:240,scale=1080:1920:flags=lanczos"


async def test_clip_square_crops_sides(monkeypatch, tmp_path):
    out = tmp_path / "clip.mp4"
    out.write_bytes(b"x")
    calls = _patch_probe(monkeypatch, [_FakeProc(returncode=0)])
    # A square is *wider* than 9:16, so it crops the sides, not top/bottom.
    await clipper._clip_segment(
        source=tmp_path / "src.mp4", output=out, start=0.0,
        clip_duration=10.0, src_width=1080, src_height=1080,
    )
    vf = _vf(_last_cmd(calls))
    assert vf == "crop=607:1080:236:0,scale=1080:1920:flags=lanczos"


async def test_clip_returns_false_on_ffmpeg_failure(monkeypatch, tmp_path):
    out = tmp_path / "clip.mp4"  # not created → also tests existence guard
    _patch_probe(monkeypatch, [_FakeProc(stderr=b"encode error", returncode=1)])
    ok = await clipper._clip_segment(
        source=tmp_path / "src.mp4", output=out, start=0.0,
        clip_duration=10.0, src_width=1080, src_height=1920,
    )
    assert ok is False


# ── _plan_segments boundary math ─────────────────────────────────────


def test_plan_even_splits_into_equal_clips():
    # 30s, clips 8-15s → clip_dur capped at 15, floor(30/15)=2 clips of 15s.
    segs = clipper._plan_segments(30.0, 8.0, 15.0, "even")
    assert segs == [(0.0, 15.0), (15.0, 15.0)]


def test_plan_even_bumps_count_when_clip_too_long():
    # 40s / floor(40/15)=2 → 20s each which exceeds clip_max(15), so N bumps
    # to 3 → ~13.33s each, all within range.
    segs = clipper._plan_segments(40.0, 8.0, 15.0, "even")
    assert len(segs) == 3
    assert all(d <= 15.0 + 1e-9 for _, d in segs)
    assert abs(sum(d for _, d in segs) - 40.0) < 1e-6  # tiles the whole source
    # contiguous, no gaps
    assert segs[0][0] == 0.0
    assert abs(segs[1][0] - segs[0][1]) < 1e-9


def test_plan_even_short_source_is_single_clip():
    # Shorter than clip_min → still produce exactly one clip covering it all.
    segs = clipper._plan_segments(5.0, 8.0, 15.0, "even")
    assert segs == [(0.0, 5.0)]


def test_plan_sequential_chops_from_start_and_drops_short_tail():
    # 35s, max 15 → 15,15, then 5s tail < clip_min(8) is dropped.
    segs = clipper._plan_segments(35.0, 8.0, 15.0, "sequential")
    assert segs == [(0.0, 15.0), (15.0, 15.0)]


def test_plan_sequential_keeps_tail_at_least_clip_min():
    # 38s, max 15 → 15,15, then 8s tail == clip_min is kept.
    segs = clipper._plan_segments(38.0, 8.0, 15.0, "sequential")
    assert segs == [(0.0, 15.0), (15.0, 15.0), (30.0, 8.0)]


def test_plan_sequential_too_short_yields_no_clips():
    # Below clip_min and sequential → nothing to cut.
    assert clipper._plan_segments(5.0, 8.0, 15.0, "sequential") == []


# ── _send WebSocket fan-out ──────────────────────────────────────────


class _FakeWS:
    """Records sent text; optionally raises to simulate a dead client."""

    def __init__(self, fail: bool = False):
        self.sent: list[str] = []
        self.fail = fail

    async def send_text(self, msg: str):
        if self.fail:
            raise RuntimeError("client gone")
        self.sent.append(msg)


async def test_send_broadcasts_event_payload(monkeypatch):
    ws = _FakeWS()
    monkeypatch.setitem(clipper._ws_clients, "job1", [ws])
    await clipper._send("job1", "progress", {"idx": 2})
    assert len(ws.sent) == 1
    assert json.loads(ws.sent[0]) == {"event": "progress", "idx": 2}


async def test_send_prunes_dead_clients(monkeypatch):
    good, dead = _FakeWS(), _FakeWS(fail=True)
    clients = [good, dead]
    monkeypatch.setitem(clipper._ws_clients, "job2", clients)
    await clipper._send("job2", "done", {})
    # dead client removed, good one survives and received the message
    assert dead not in clients
    assert good in clients
    assert json.loads(good.sent[0]) == {"event": "done"}


async def test_send_no_clients_is_noop():
    # Unknown job id must not raise.
    await clipper._send("nope", "x", {"a": 1})


# ── _faststart moov-atom relocation ──────────────────────────────────


async def test_faststart_replaces_original_on_success(monkeypatch, tmp_path):
    mp4 = tmp_path / "v.mp4"
    mp4.write_bytes(b"original")

    async def fake_exec(*args, **kwargs):
        # ffmpeg "-c copy -movflags +faststart" writes the temp output.
        out = Path(args[-1])
        out.write_bytes(b"faststarted")
        return _FakeProc(returncode=0)

    monkeypatch.setattr(clipper.asyncio, "create_subprocess_exec", fake_exec)
    await clipper._faststart(mp4)
    assert mp4.read_bytes() == b"faststarted"
    # temp file cleaned up by the rename
    assert not mp4.with_suffix(".faststart.mp4").exists()


async def test_faststart_keeps_original_and_cleans_temp_on_failure(monkeypatch, tmp_path):
    mp4 = tmp_path / "v.mp4"
    mp4.write_bytes(b"original")

    async def fake_exec(*args, **kwargs):
        out = Path(args[-1])
        out.write_bytes(b"garbage")  # temp written but ffmpeg "failed"
        return _FakeProc(stderr=b"boom", returncode=1)

    monkeypatch.setattr(clipper.asyncio, "create_subprocess_exec", fake_exec)
    await clipper._faststart(mp4)
    assert mp4.read_bytes() == b"original"  # untouched
    assert not mp4.with_suffix(".faststart.mp4").exists()  # temp removed


# ── _generate_thumbnail multi-seek fallback ──────────────────────────


async def test_thumbnail_succeeds_on_first_good_seek(monkeypatch, tmp_path):
    thumb = tmp_path / "t.jpg"

    async def fake_exec(*args, **kwargs):
        thumb.write_bytes(b"x" * 5000)  # > min_thumb_size
        return _FakeProc(returncode=0)

    monkeypatch.setattr(clipper.asyncio, "create_subprocess_exec", fake_exec)
    ok = await clipper._generate_thumbnail(tmp_path / "v.mp4", thumb, seek=2.0)
    assert ok
    assert thumb.stat().st_size >= 2000


async def test_thumbnail_retries_past_tiny_frames(monkeypatch, tmp_path):
    """A blank first frame (too small) must trigger the next seek point."""
    thumb = tmp_path / "t.jpg"
    sizes = iter([100, 100, 9000])  # first two blank, third good
    seeks: list[float] = []

    async def fake_exec(*args, **kwargs):
        # capture the -ss value for this attempt
        ss = float(args[args.index("-ss") + 1])
        seeks.append(ss)
        thumb.write_bytes(b"x" * next(sizes))
        return _FakeProc(returncode=0)

    monkeypatch.setattr(clipper.asyncio, "create_subprocess_exec", fake_exec)
    # seek<0 with no duration available → fixed [2.0, 0.5, 0.1] seek points
    monkeypatch.setattr(
        clipper, "_get_video_info",
        _raise_async(RuntimeError("no probe")),
    )
    ok = await clipper._generate_thumbnail(tmp_path / "v.mp4", thumb)
    assert ok
    assert len(seeks) == 3  # walked all three fallback seeks
    assert thumb.stat().st_size == 9000


async def test_thumbnail_accepts_small_frame_as_last_resort(monkeypatch, tmp_path):
    """If every seek yields a tiny frame, still accept the file that exists."""
    thumb = tmp_path / "t.jpg"

    async def fake_exec(*args, **kwargs):
        thumb.write_bytes(b"x" * 100)  # always below min_thumb_size
        return _FakeProc(returncode=0)

    monkeypatch.setattr(clipper.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        clipper, "_get_video_info",
        _raise_async(RuntimeError("no probe")),
    )
    ok = await clipper._generate_thumbnail(tmp_path / "v.mp4", thumb)
    assert ok  # accepts whatever exists rather than failing outright


async def test_thumbnail_fails_when_no_file_produced(monkeypatch, tmp_path):
    thumb = tmp_path / "t.jpg"  # never created

    async def fake_exec(*args, **kwargs):
        return _FakeProc(stderr=b"decode error", returncode=1)

    monkeypatch.setattr(clipper.asyncio, "create_subprocess_exec", fake_exec)
    ok = await clipper._generate_thumbnail(tmp_path / "v.mp4", thumb, seek=2.0)
    assert ok is False


def _raise_async(exc):
    async def _f(*args, **kwargs):
        raise exc
    return _f
