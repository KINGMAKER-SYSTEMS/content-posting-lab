"""Band-aware cut: a framed page's clip is cut into its band on the 1080x1920 canvas.

A framed cut first stretches the frame to square display pixels at an even
width (page_frame.DISPLAY_PIXELS_FILTER). fill then evaluates today's clip crop
against the band (1080 x band); fit contains the whole zoomed source window in
the band. Speed and grade stay exactly where today's cut applies them, and the
pad to the black canvas is always last, so grade, grain and vignette touch the
picture only. Unframed pages are covered by
tests/test_page_frame_no_frame_identity.py (byte identity with 7a97021).
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from services import ffmpeg as ffmpeg_module
from services import page_frame
from services.ffmpeg import (
    FrameGeometryUnavailable,
    build_cc_filter,
    delivery_encode_args,
    probe_display_size,
    run_color_correct,
)

SQUARE = page_frame.DISPLAY_PIXELS_FILTER + ","
CROP = {"zoom": 1.5, "focusX": 0.2, "focusY": 0.7}
NEUTRAL = {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5}
GRADE = {"brightness": -15.0, "saturation": -50.0, "fade": 20.0, "grain": 25.0, "vignette": 40.0}


def test_fill_evaluates_the_clip_crop_against_the_band_and_pads_last():
    assert build_cc_filter(None, clip_crop=CROP, page_frame="16:9", frame_fit="fill") == (
        "scale=trunc(iw*sar/2)*2:ih,setsar=1,"
        "scale=1620:912:force_original_aspect_ratio=increase:flags=lanczos,"
        "crop=1080:608:(iw-1080)*0.200000:(ih-608)*0.700000,setsar=1,"
        "pad=1080:1920:0:656:black")
    # Same zoom/focus semantics as the vertical cut, only the output size is the band.
    assert build_cc_filter(None, clip_crop=CROP, page_frame="4:3", frame_fit="fill") == (
        SQUARE + ffmpeg_module._clip_crop_filter(CROP, (1080, 810)) + ",pad=1080:1920:0:554:black")


def test_fit_contains_the_source_window_from_the_shared_geometry():
    assert build_cc_filter(None, clip_crop=NEUTRAL, page_frame="16:9", frame_fit="fit",
                           source_size=(1920, 1080)) == (
        "scale=trunc(iw*sar/2)*2:ih,setsar=1,"
        "crop=1920:1080:0:0:exact=1,scale=1080:608:flags=lanczos,setsar=1,"
        "pad=1080:1920:0:656:black")
    # A vertical master on a 16:9 page is pillarboxed inside the band.
    assert build_cc_filter(None, clip_crop=NEUTRAL, page_frame="16:9", frame_fit="fit",
                           source_size=(1080, 1920)) == (
        SQUARE + "crop=1080:1920:0:0:exact=1,scale=342:608:flags=lanczos,setsar=1,"
        "pad=1080:1920:368:656:black")
    picture_filter, pad = page_frame.fit_cut_filters(1920, 1080, "1:1", 1.5, 0.2, 0.7)
    assert build_cc_filter(None, clip_crop=CROP, page_frame="1:1", source_size=(1920, 1080)) == (
        f"{SQUARE}{picture_filter},{pad}")
    # An anamorphic master's window is laid out on its display size (852x480 for 720x480 SAR 32:27).
    assert build_cc_filter(None, clip_crop=NEUTRAL, page_frame="16:9", frame_fit="fit",
                           source_size=(852, 480)) == (
        SQUARE + "crop=852:480:0:0:exact=1,scale=1080:608:flags=lanczos,setsar=1,pad=1080:1920:0:656:black")


@pytest.mark.parametrize("fit,source_size", [("fill", None), ("fit", (1920, 1080))])
@pytest.mark.parametrize("grade", [None, GRADE])
def test_every_framed_cut_starts_on_square_display_pixels_and_no_vertical_cut_does(fit, source_size, grade):
    framed = build_cc_filter(grade, clip_crop=CROP, page_frame="16:9", frame_fit=fit, source_size=source_size)
    assert framed.startswith(SQUARE) and framed.count("iw*sar") == 1
    for frame in (None, "9:16"):
        assert "iw*sar" not in build_cc_filter(grade, clip_crop=CROP, page_frame=frame)



@pytest.mark.parametrize("fit,source_size", [("fill", None), ("fit", (1920, 1080))])
def test_speed_and_grade_stay_where_they_are_and_the_pad_is_the_last_filter(fit, source_size):
    framed = build_cc_filter(GRADE, playback_speed=0.75, clip_crop=CROP, page_frame="1:1",
                             frame_fit=fit, source_size=source_size)
    unframed = build_cc_filter(GRADE, playback_speed=0.75, clip_crop=CROP)
    filters = framed.split(",")
    # After the square-pixel prefix, the grade (format, mixer, grain, vignette)
    # is byte-identical to today's.
    grade_prefix = unframed[:unframed.index(",scale=")]
    assert framed.startswith(SQUARE + grade_prefix + ",")
    assert filters[-2:] == ["setpts=PTS/0.750000", filters[-1]]
    assert filters[-1].startswith("pad=1080:1920:") and filters[-1].endswith(":black")
    assert sum(item.startswith("pad=") for item in filters) == 1
    assert framed.index("vignette=") < framed.index("crop=") < framed.index("setpts=") < framed.index("pad=")


def test_absent_crop_on_a_framed_page_is_the_neutral_centred_crop():
    assert build_cc_filter(None, page_frame="16:9", frame_fit="fill") == build_cc_filter(
        None, clip_crop=NEUTRAL, page_frame="16:9", frame_fit="fill")
    assert build_cc_filter(None, page_frame="3:4", source_size=(704, 1280)) == build_cc_filter(
        None, clip_crop=NEUTRAL, page_frame="3:4", frame_fit="fit", source_size=(704, 1280))


@pytest.mark.parametrize("frame", [None, "9:16"])
def test_vertical_page_is_todays_cut(frame):
    for grade in (None, GRADE):
        assert build_cc_filter(grade, playback_speed=1.5, clip_crop=CROP, page_frame=frame) == (
            build_cc_filter(grade, playback_speed=1.5, clip_crop=CROP))


@pytest.mark.parametrize("kwargs,match", [
    ({"frame_fit": "fill"}, "frameFit"),
    ({"page_frame": "9:16", "frame_fit": "fill"}, "frameFit"),
    ({"page_frame": "9:16", "frame_fit": "fit", "source_size": (1920, 1080)}, "frameFit"),
    ({"page_frame": "2:3"}, "frame"),
    ({"page_frame": "16:9", "frame_fit": "stretch"}, "frameFit"),
    ({"page_frame": "16:9", "frame_fit": "fit"}, "source_size"),
    ({"page_frame": "16:9", "frame_fit": "fit", "source_size": (0, 1080)}, "source_size"),
    ({"page_frame": "16:9", "frame_fit": "fit", "source_size": [1920, 1080]}, "source_size"),
    ({"page_frame": "16:9", "frame_fit": "fit", "source_size": (True, 1080)}, "source_size"),
    ({"page_frame": "16:9", "frame_fit": "fill", "scale": "1080:1920"}, "scale"),
    ({"page_frame": "16:9", "frame_fit": "fill", "clip_crop_size": (720, 1280)}, "clip_crop_size"),
])
def test_framed_cut_rejects_incomplete_or_contradictory_arguments(kwargs, match):
    with pytest.raises(ValueError, match=match):
        build_cc_filter(None, clip_crop=CROP, **kwargs)


class _Stderr:
    async def read(self, _size=-1):
        return b""


class _Process:
    returncode = 0
    stderr = _Stderr()

    async def wait(self):
        return 0


def _argv(monkeypatch, **kwargs):
    captured = []

    async def fake_exec(*args, **_kwargs):
        captured.extend(args)
        return _Process()

    monkeypatch.setattr(ffmpeg_module.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(run_color_correct("master.mp4", "cut.mp4", GRADE, **kwargs))
    return captured


def test_framed_cut_changes_only_the_filter_argument_of_the_sourced_cut(monkeypatch):
    common = {"scale": None, "encode_args": delivery_encode_args("tiktok_delivery_v1"),
              "playback_speed": 0.75, "clip_crop": CROP, "clip_crop_size": (1080, 1920),
              "clip_start_ms": 8_500, "clip_duration_ms": 7_000}
    plain = _argv(monkeypatch, **common)
    framed = _argv(monkeypatch, **common, page_frame="16:9", frame_fit="fit", source_size=(1920, 1080))
    index = plain.index("-vf") + 1
    assert framed[:index] == plain[:index] and framed[index + 1:] == plain[index + 1:]
    assert framed[index] == build_cc_filter(
        GRADE, playback_speed=0.75, clip_crop=CROP, page_frame="16:9", frame_fit="fit",
        source_size=(1920, 1080)) + ",setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration=0.1"


def _require_ffmpeg():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe are required")


def _tiny_clip(path: Path, size: str, *extra: str) -> Path:
    subprocess.run(["nice", "-n", "10", "ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
                    "-i", f"color=c=gray:s={size}:r=30:d=0.4", *extra, "-threads", "2", "-c:v", "libx264",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)],
                   check=True, timeout=60, capture_output=True)
    return path


def _rotated(source: Path, path: Path, degrees: int) -> Path:
    """Stream-copy with a display rotation (ffmpeg 7+ option, else the legacy mov tag)."""
    for args in (["-display_rotation", str(degrees), "-i", str(source)],
                 ["-i", str(source), "-metadata:s:v:0", f"rotate={degrees % 360}"]):
        written = subprocess.run(["nice", "-n", "10", "ffmpeg", "-nostdin", "-v", "error", "-y", *args,
                                  "-c", "copy", str(path)], timeout=60, capture_output=True)
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream_side_data=rotation:stream_tags=rotate", "-of", "json", str(path)],
                               timeout=60, capture_output=True, text=True)
        if written.returncode == 0 and probe.returncode == 0 and "rotat" in probe.stdout:
            return path
    pytest.skip("this ffmpeg cannot write a display rotation")


def test_probe_returns_the_display_size_the_filter_chain_sees(tmp_path):
    _require_ffmpeg()
    landscape = _tiny_clip(tmp_path / "landscape.mp4", "320x180")
    assert asyncio.run(probe_display_size(landscape)) == (320, 180)
    for degrees in (90, -90, 270):
        rotated = _rotated(landscape, tmp_path / f"rotated-{degrees}.mp4", degrees)
        assert asyncio.run(probe_display_size(rotated)) == (180, 320)
    upside_down = _rotated(landscape, tmp_path / "rotated-180.mp4", 180)
    assert asyncio.run(probe_display_size(upside_down)) == (320, 180)


def test_probe_returns_the_display_size_of_an_anamorphic_master(tmp_path):
    """No refusal: a non-square master is laid out on its square display pixels."""
    _require_ffmpeg()
    anamorphic = _tiny_clip(tmp_path / "anamorphic.mp4", "320x240", "-vf", "setsar=4/3")
    # trunc(320 * 4/3 / 2) * 2 = 426
    assert asyncio.run(probe_display_size(anamorphic)) == (426, 240) == page_frame.display_size(
        320, 240, Fraction(4, 3))
    # A quarter turn swaps the frame and inverts its SAR: 240x320 at 3:4 -> 180x320.
    rotated = _rotated(anamorphic, tmp_path / "anamorphic-rotated.mp4", 90)
    assert asyncio.run(probe_display_size(rotated)) == (180, 320)


def test_unreadable_media_is_reported_for_the_fill_fallback(tmp_path):
    _require_ffmpeg()
    (tmp_path / "junk.mp4").write_bytes(b"not a video")
    for path in (tmp_path / "junk.mp4", tmp_path / "missing.mp4"):
        with pytest.raises(FrameGeometryUnavailable) as error:
            asyncio.run(probe_display_size(path))
        assert error.value.reason == "ffprobe_failed"
        assert str(error.value).startswith("frame_source_geometry_unavailable")


class _ProbeReply:
    def __init__(self, data: bytes, hang: bool):
        self._chunks = [data[index:index + 4096] for index in range(0, len(data), 4096)]
        self._hang = hang

    async def read(self, _size=-1):
        if self._chunks:
            return self._chunks.pop(0)
        if self._hang:
            await asyncio.sleep(3600)
        return b""


class _ProbeProcess:
    pid = 4_000_000  # never signalled: os.killpg is stubbed below

    def __init__(self, data=b"", returncode=0, hang=False):
        self.stdout = _ProbeReply(data, hang)
        self._code, self._hang = returncode, hang
        self.returncode = None

    async def wait(self):
        if self._hang:
            await asyncio.sleep(3600)
        self.returncode = self._code
        return self._code


def _stub_probe(monkeypatch, process):
    async def fake_exec(*_args, **_kwargs):
        if process is None:
            raise FileNotFoundError("ffprobe")
        return process

    monkeypatch.setattr(ffmpeg_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(ffmpeg_module.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(ffmpeg_module, "_INPUT_PROBE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(ffmpeg_module, "_ENCODE_REAP_TIMEOUT_SECONDS", 0.05)


def _reply(**stream):
    return json.dumps({"streams": [stream]}).encode()


@pytest.mark.parametrize("stream,expected", [
    ({"width": 720, "height": 480, "sample_aspect_ratio": "32:27"}, (852, 480)),
    ({"width": 720, "height": 480, "sample_aspect_ratio": "32:27", "side_data_list": [{"rotation": -90}]},
     (404, 720)),
    ({"width": 720, "height": 480, "sample_aspect_ratio": "32:27", "tags": {"rotate": "270"}}, (404, 720)),
    ({"width": 1920, "height": 1080, "sample_aspect_ratio": "1216:1215"}, (1920, 1080)),
    ({"width": 1920, "height": 1080, "sample_aspect_ratio": "0:1"}, (1920, 1080)),
    ({"width": 1920, "height": 1080, "sample_aspect_ratio": "N/A", "side_data_list": [{"rotation": 180}]},
     (1920, 1080)),
    ({"width": 1920, "height": 1080}, (1920, 1080)),
    ({"width": 1919, "height": 1079, "sample_aspect_ratio": "1:1"}, (1918, 1079)),
])
def test_probe_returns_the_frame_the_square_pixel_filter_makes(monkeypatch, stream, expected):
    _stub_probe(monkeypatch, _ProbeProcess(_reply(**stream)))
    assert asyncio.run(probe_display_size("master.mp4")) == expected


@pytest.mark.parametrize("process,reason", [
    (lambda: None, "ffprobe_unavailable"),
    (lambda: _ProbeProcess(b"", hang=True), "timeout"),
    (lambda: _ProbeProcess(b"x" * (64 * 1024 + 1)), "reply_too_large"),
    (lambda: _ProbeProcess(b"{}", returncode=1), "ffprobe_failed"),
    (lambda: _ProbeProcess(b"not json"), "reply_invalid"),
    (lambda: _ProbeProcess(json.dumps({"streams": []}).encode()), "reply_invalid"),
    (lambda: _ProbeProcess(_reply(width=0, height=1080)), "size_invalid"),
    (lambda: _ProbeProcess(_reply(width=True, height=1080)), "size_invalid"),
    (lambda: _ProbeProcess(_reply(width=720, height=480, sample_aspect_ratio="wide")), "sample_aspect_ratio_invalid"),
    (lambda: _ProbeProcess(_reply(width=720, height=480, sample_aspect_ratio="1:0")), "sample_aspect_ratio_invalid"),
    (lambda: _ProbeProcess(_reply(width=720, height=480, sample_aspect_ratio="-4:3")), "sample_aspect_ratio_invalid"),
])
def test_every_unprovable_probe_is_reported_for_the_fill_fallback(monkeypatch, process, reason):
    _stub_probe(monkeypatch, process())
    with pytest.raises(FrameGeometryUnavailable) as error:
        asyncio.run(probe_display_size("master.mp4"))
    assert error.value.reason == reason


def _frame_luma(path: Path, at: float = 0.1):
    from PIL import Image
    png = path.with_suffix(".png")
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", str(path),
                    "-frames:v", "1", "-update", "1", str(png)], check=True, timeout=60, capture_output=True)
    with Image.open(png) as image:
        return image.convert("L")


def _bbox(luma, threshold=40):
    return luma.point(lambda value: 255 if value > threshold else 0).getbbox()


@pytest.mark.parametrize("fit", ["fit", "fill"])
def test_rotated_master_is_framed_on_its_upright_size(tmp_path, fit):
    """A phone master stored 1920x1080 with a quarter-turn plays 1080x1920."""
    _require_ffmpeg()
    coded = _tiny_clip(tmp_path / "coded.mp4", "480x270")
    rotated = _rotated(coded, tmp_path / "rotated.mp4", 90)
    size = asyncio.run(probe_display_size(rotated))
    assert size == (270, 480)
    output = tmp_path / f"cut-{fit}.mp4"
    kwargs = {"page_frame": "16:9", "frame_fit": fit}
    if fit == "fit":
        kwargs["source_size"] = size
    asyncio.run(run_color_correct(
        str(rotated), str(output), None, clip_crop=NEUTRAL,
        encode_args=[*delivery_encode_args("tiktok_delivery_v1"), "-threads", "2"], **kwargs))
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                       "stream=width,height", "-of", "json", str(output)],
                                      check=True, capture_output=True, text=True).stdout)["streams"][0]
    assert (probe["width"], probe["height"]) == (1080, 1920)
    expected = page_frame.geometry(270, 480, "16:9", fit)["picture"]
    left, top, right, bottom = _bbox(_frame_luma(output))
    assert max(abs(left - expected["x"]), abs(top - expected["y"]),
               abs(right - (expected["x"] + expected["w"])),
               abs(bottom - (expected["y"] + expected["h"]))) <= 2


@pytest.mark.parametrize("fit", ["fit", "fill"])
def test_rotated_anamorphic_master_is_framed_on_its_display_size(tmp_path, fit):
    """720x480 at SAR 32:27 turned a quarter: ffmpeg shows 480x720 at 27:32, i.e. 404x720 square pixels."""
    _require_ffmpeg()
    coded = _tiny_clip(tmp_path / "dvd.mp4", "720x480", "-vf", "setsar=32/27")
    rotated = _rotated(coded, tmp_path / "dvd-rotated.mp4", 90)
    size = asyncio.run(probe_display_size(rotated))
    assert size == (404, 720)
    output = tmp_path / f"cut-{fit}.mp4"
    kwargs = {"page_frame": "16:9", "frame_fit": fit}
    if fit == "fit":
        kwargs["source_size"] = size
    asyncio.run(run_color_correct(
        str(rotated), str(output), None, clip_crop=NEUTRAL,
        encode_args=[*delivery_encode_args("tiktok_delivery_v1"), "-threads", "2"], **kwargs))
    expected = page_frame.geometry(404, 720, "16:9", fit)["picture"]
    left, top, right, bottom = _bbox(_frame_luma(output))
    assert max(abs(left - expected["x"]), abs(top - expected["y"]),
               abs(right - (expected["x"] + expected["w"])),
               abs(bottom - (expected["y"] + expected["h"]))) <= 2
