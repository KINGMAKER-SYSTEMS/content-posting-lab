"""Unit tests for services/ffmpeg.py color-correction math.

Covers the matrix/vector transforms in build_cc_filter and the is_default_cc
fast-path guard. These verify the brightness/contrast/saturation/temperature/
tint 3x3 transforms and edge cases (fade clamps, shadow boundary, second
default check) so a silent regression can't produce wrong color on the burn
router's TikTok-optimized encodes.
"""

import asyncio
import math
import re
from pathlib import Path

import pytest

from services import ffmpeg as ffmpeg_module
from services.ffmpeg import (
    TIKTOK_ENCODE_ARGS,
    build_cc_filter,
    delivery_encode_args,
    is_default_cc,
    run_color_correct,
)


# --- helpers ---------------------------------------------------------------

class FakeStderr:
    """Small StreamReader-like stderr pipe that reaches EOF after its chunks."""

    def __init__(self, chunks=()):
        self._chunks = iter(chunks)

    async def read(self, _size=-1):
        return next(self._chunks, b"")


def _coeffs(vf: str) -> dict[str, float]:
    """Parse the colorchannelmixer coefficients out of a -vf filter string."""
    m = re.search(r"colorchannelmixer=([^,]+)", vf)
    assert m, f"no colorchannelmixer in {vf!r}"
    out = {}
    for part in m.group(1).split(":"):
        k, v = part.split("=")
        out[k] = float(v)
    return out


def _is_identity(vf: str) -> bool:
    """True if the mixer is the identity matrix with zero offsets."""
    c = _coeffs(vf)
    diag = {"rr", "gg", "bb"}
    for k, v in c.items():
        target = 1.0 if k in diag else 0.0
        if abs(v - target) > 1e-9:
            return False
    return True


# --- is_default_cc ---------------------------------------------------------

@pytest.mark.parametrize("cc", [None, {}, {"brightness": 0, "contrast": 0}])
def test_is_default_cc_true(cc):
    assert is_default_cc(cc) is True


def test_is_default_cc_nonzero():
    assert is_default_cc({"brightness": 5}) is False


def test_grain_and_vignette_are_real_non_default_treatments():
    assert is_default_cc({"grain": 20}) is False
    assert is_default_cc({"vignette": 30}) is False
    vf = build_cc_filter({"grain": 20, "vignette": 30})
    assert "noise=alls=3.00:allf=t+u" in vf
    assert "vignette=angle=PI/9.600:eval=frame" in vf


def test_playback_speed_changes_pts_even_without_color_treatment():
    assert build_cc_filter(None, playback_speed=1.25) == "setpts=PTS/1.250000"
    assert build_cc_filter({"grain": 20}, playback_speed=0.75).endswith(
        "setpts=PTS/0.750000"
    )
    for invalid in (0.49, 2.01, True, float("nan")):
        with pytest.raises(ValueError, match="playback_speed"):
            build_cc_filter(None, playback_speed=invalid)


def test_playback_speed_survives_an_effectively_neutral_color_treatment():
    assert build_cc_filter(
        {"temperature": 1}, playback_speed=2.0,
    ) == "setpts=PTS/2.000000"


def test_clip_crop_builds_a_normalized_vertical_filter():
    crop = {"zoom": 1.5, "focusX": 0.25, "focusY": 0.75}
    assert build_cc_filter(None, clip_crop=crop) == (
        "scale=1620:2880:force_original_aspect_ratio=increase:flags=lanczos,"
        "crop=1080:1920:(iw-1080)*0.250000:(ih-1920)*0.750000,setsar=1"
    )
    combined = build_cc_filter(None, playback_speed=0.75, clip_crop=crop)
    assert combined.endswith("setsar=1,setpts=PTS/0.750000")


def test_clip_crop_uses_the_typed_executor_output_size():
    crop = {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5}
    assert build_cc_filter(
        None, clip_crop=crop, clip_crop_size=(720, 1280),
    ) == (
        "scale=720:1280:force_original_aspect_ratio=increase:flags=lanczos,"
        "crop=720:1280:(iw-720)*0.500000:(ih-1280)*0.500000,setsar=1"
    )


def test_delivery_encode_preset_is_closed_and_returns_an_isolated_copy():
    first = delivery_encode_args("tiktok_delivery_v1")
    second = delivery_encode_args("tiktok_delivery_v1")
    assert first == second == TIKTOK_ENCODE_ARGS
    first.append("changed")
    assert second == TIKTOK_ENCODE_ARGS
    with pytest.raises(ValueError, match="delivery encode preset"):
        delivery_encode_args("unregistered")


@pytest.mark.parametrize("size", [(0, 1920), (1081, 1920), [1080, 1920]])
def test_clip_crop_rejects_invalid_output_size(size):
    with pytest.raises(ValueError, match="clip_crop_size"):
        build_cc_filter(
            None,
            clip_crop={"zoom": 1.0, "focusX": 0.5, "focusY": 0.5},
            clip_crop_size=size,
        )


@pytest.mark.parametrize(
    "crop",
    [
        {"zoom": 0.99, "focusX": 0.5, "focusY": 0.5},
        {"zoom": 3.01, "focusX": 0.5, "focusY": 0.5},
        {"zoom": 1.0, "focusX": -0.01, "focusY": 0.5},
        {"zoom": 1.0, "focusX": 0.5, "focusY": 1.01},
        {"zoom": 1.0, "focusX": 0.5},
    ],
)
def test_clip_crop_rejects_invalid_values(crop):
    with pytest.raises(ValueError, match="clip_crop"):
        build_cc_filter(None, clip_crop=crop)


@pytest.mark.asyncio
async def test_speed_render_keeps_optional_audio_in_lockstep(monkeypatch):
    captured = []

    class Process:
        returncode = 0
        stderr = FakeStderr()

        async def wait(self):
            return self.returncode

    async def fake_exec(*args, **kwargs):
        captured.extend(args)
        return Process()

    monkeypatch.setattr(ffmpeg_module.asyncio, "create_subprocess_exec", fake_exec)
    await run_color_correct("in.mp4", "out.mp4", None, playback_speed=1.25)

    assert captured[captured.index("-vf") + 1] == "setpts=PTS/1.250000"
    assert captured[captured.index("-af") + 1] == "atempo=1.250000"
    video_map = captured.index("-map")
    assert ["-map", "0:v:0"] == captured[video_map:video_map + 2]
    assert "0:a?" in captured
    assert captured[captured.index("-c:a") + 1] == "aac"


@pytest.mark.asyncio
async def test_source_window_is_an_input_bound_before_speed_treatment(monkeypatch):
    captured = []

    class Process:
        returncode = 0
        stderr = FakeStderr()

        async def wait(self):
            return self.returncode

    async def fake_exec(*args, **kwargs):
        captured.extend(args)
        return Process()

    monkeypatch.setattr(ffmpeg_module.asyncio, "create_subprocess_exec", fake_exec)
    await run_color_correct(
        "master.mp4", "cut.mp4", None, playback_speed=2.0,
        clip_start_ms=8_500, clip_duration_ms=7_000,
    )
    assert captured[:8] == [
        "ffmpeg", "-y", "-ss", "8.500", "-t", "7.000", "-i", "master.mp4",
    ]
    assert captured[captured.index("-vf") + 1].startswith("setpts=PTS/2.000000,")
    assert captured[-3:] == ["-t", "3.500000", "cut.mp4"]


@pytest.mark.asyncio
async def test_color_correct_serializes_ffmpeg_processes(monkeypatch):
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    active = 0
    peak = 0

    class Process:
        returncode = 0
        stderr = FakeStderr()

        async def wait(self):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            first_started.set()
            await release_first.wait()
            active -= 1
            return self.returncode

    async def fake_exec(*args, **kwargs):
        return Process()

    monkeypatch.setattr(ffmpeg_module.asyncio, "create_subprocess_exec", fake_exec)
    first = asyncio.create_task(run_color_correct("one.mp4", "one-out.mp4", None))
    await first_started.wait()
    second = asyncio.create_task(run_color_correct("two.mp4", "two-out.mp4", None))
    await asyncio.sleep(0)
    assert peak == 1
    release_first.set()
    await asyncio.gather(first, second)
    assert peak == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start", "duration"),
    [(None, 7_000), (0, None), (-1, 7_000), (0, 0), (True, 7_000)],
)
async def test_source_window_rejects_incomplete_or_invalid_bounds(start, duration):
    with pytest.raises(ValueError, match="clip window"):
        await run_color_correct(
            "master.mp4", "cut.mp4", None,
            clip_start_ms=start, clip_duration_ms=duration,
        )


def test_is_default_cc_string_number_is_truthy_nonzero():
    # Values are float()-coerced, so a numeric string still counts as set.
    assert is_default_cc({"brightness": "5"}) is False


def test_is_default_cc_unparseable_value_is_ignored():
    # float("x") raises -> that key is skipped, not treated as a change.
    assert is_default_cc({"brightness": "x", "contrast": 0}) is True


def test_is_default_cc_ignores_unknown_keys():
    # Only the known slider keys are inspected.
    assert is_default_cc({"gamma": 99}) is True


# --- scale / no-op fast paths ----------------------------------------------

def test_default_cc_no_scale_is_null():
    assert build_cc_filter(None) == "null"


def test_default_cc_with_scale_passes_through_scale_only():
    assert build_cc_filter(None, scale="1080:1920") == (
        "scale=1080:1920:flags=lanczos,setsar=1"
    )


def test_active_cc_uses_alpha_bearing_precision_and_appends_scale_last():
    vf = build_cc_filter({"brightness": 20}, scale="1080:1920")
    assert vf.startswith("format=rgba64le,colorchannelmixer=")
    assert ",format=rgb24,scale=1080:1920:flags=lanczos,setsar=1" in vf


# --- second default check (L120-131) ---------------------------------------

def test_shadow_below_brightness_epsilon_is_noop():
    # shadow=2 -> brightness 1 + 2/400 = 1.005, but the float is just under the
    # 0.005 threshold, so the post-transform default check collapses to null.
    assert build_cc_filter({"shadow": 2}) == "null"


def test_shadow_above_brightness_epsilon_applies():
    # shadow=3 -> 1 + 3/400 = 1.0075 clears the threshold and emits a mixer.
    vf = build_cc_filter({"shadow": 3})
    c = _coeffs(vf)
    assert c["rr"] == pytest.approx(1.0075)


@pytest.mark.parametrize("key", ["temperature", "tint"])
def test_tiny_temp_tint_within_deadband_is_noop(key):
    # |t|<=1 and |ti|<=1 are inside the dead-band -> no-op.
    assert build_cc_filter({key: 1}) == "null"


# --- brightness / contrast / saturation ------------------------------------

def test_brightness_scales_diagonal():
    c = _coeffs(build_cc_filter({"brightness": 20}))  # b = 1.2
    assert c["rr"] == pytest.approx(1.2)
    assert c["gg"] == pytest.approx(1.2)
    assert c["bb"] == pytest.approx(1.2)
    assert c["ra"] == pytest.approx(0.0)


def test_contrast_adds_pivot_bias_offset():
    # c = 1.5 -> diagonal 1.5, offset bias = 0.5*(1-1.5) = -0.25 on each channel.
    c = _coeffs(build_cc_filter({"contrast": 50}))
    assert c["rr"] == pytest.approx(1.5)
    assert c["ra"] == pytest.approx(-0.25)
    assert c["ga"] == pytest.approx(-0.25)
    assert c["ba"] == pytest.approx(-0.25)


def test_saturation_zero_collapses_to_luma_weights():
    # saturation=-100 -> s=0 -> every output row is the Rec.709 luma vector.
    c = _coeffs(build_cc_filter({"saturation": -100}))
    for row in ("r", "g", "b"):
        assert c[f"{row}r"] == pytest.approx(0.2126)
        assert c[f"{row}g"] == pytest.approx(0.7152)
        assert c[f"{row}b"] == pytest.approx(0.0722)


# --- temperature: warm (sepia blend) vs cool (hue rotate) ------------------

def test_temperature_warm_blends_toward_sepia():
    # t=100 -> amt = min(1, 100/200) = 0.5; rr = 1 - amt + amt*0.393.
    c = _coeffs(build_cc_filter({"temperature": 100}))
    assert c["rr"] == pytest.approx(0.6965)
    assert c["rg"] == pytest.approx(0.5 * 0.769)
    assert c["rb"] == pytest.approx(0.5 * 0.189)


def test_temperature_warm_amt_clamped_at_one():
    # t=400 -> 400/200 = 2 but amt clamps to 1.0 -> rr = 0.393 exactly.
    c = _coeffs(build_cc_filter({"temperature": 400}))
    assert c["rr"] == pytest.approx(0.393)


def test_temperature_cool_uses_hue_rotation():
    # t<0 takes the hue-rotate branch: rad = radians(t/5).
    t = -50
    rad = math.radians(t / 5)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    expected_rr = 0.213 + 0.787 * cos_a - 0.213 * sin_a
    c = _coeffs(build_cc_filter({"temperature": t}))
    assert c["rr"] == pytest.approx(expected_rr)


# --- tint: phase-shift hue rotation ----------------------------------------

def test_tint_phase_shift_matrix():
    # tint uses rad = radians(ti/3); verify the first mixer row. The mixer is
    # emitted with %.6f formatting, so allow a half-ULP (5e-7) of slack.
    ti = 30
    rad = math.radians(ti / 3)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    c = _coeffs(build_cc_filter({"tint": ti}))
    assert c["rr"] == pytest.approx(0.213 + 0.787 * cos_a - 0.213 * sin_a, abs=5e-7)
    assert c["rg"] == pytest.approx(0.715 - 0.715 * cos_a - 0.715 * sin_a, abs=5e-7)
    assert c["rb"] == pytest.approx(0.072 - 0.072 * cos_a + 0.928 * sin_a, abs=5e-7)


def test_tint_zero_phase_is_near_identity():
    # A tiny tint just past the dead-band rotates by a small angle: still a
    # valid (non-null) mixer, close to identity, rows summing to ~1. The %.6f
    # output formatting means the row sum can drift up to ~1.5e-6.
    c = _coeffs(build_cc_filter({"tint": 2}))
    assert c["rr"] + c["rg"] + c["rb"] == pytest.approx(1.0, abs=2e-6)


# --- fade clamps -----------------------------------------------------------

def test_fade_clamps_are_bounded():
    # A huge fade hits the min/max clamps (brightness<=2, contrast/sat>=0.2)
    # and must still emit a finite, non-null mixer.
    vf = build_cc_filter({"fade": 200})
    assert vf != "null"
    c = _coeffs(vf)
    for v in c.values():
        assert math.isfinite(v)


# --- sharpness -------------------------------------------------------------

def test_sharpness_appends_unsharp_filter():
    vf = build_cc_filter({"sharpness": 25})  # 25/50 = 0.5
    assert "unsharp=5:5:0.50:5:5:0.50" in vf


def test_sharpness_alone_does_not_invent_a_colour_matrix():
    vf = build_cc_filter({"sharpness": 25})
    assert "colorchannelmixer" not in vf
    assert vf == "format=rgba64le,format=rgb24,unsharp=5:5:0.50:5:5:0.50"


@pytest.mark.asyncio
@pytest.mark.parametrize(('speed', 'raw_ms'), [(1.0, 6000), (0.8, 5000), (2.0, 12000)])
async def test_tail_cut_keeps_delivery_duration_at_fractional_frame(tmp_path, speed, raw_ms):
    import shutil
    import subprocess
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('ffmpeg and ffprobe required')
    source = tmp_path / 'master.mp4'
    output = tmp_path / 'cut.mp4'
    subprocess.run([
        'ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
        'testsrc2=size=180x320:rate=30', '-t', f'{(raw_ms + 1534) / 1000}',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source),
    ], check=True, capture_output=True)
    await run_color_correct(
        str(source), str(output), None, playback_speed=speed,
        encode_args=delivery_encode_args('tiktok_delivery_v1'),
        clip_start_ms=1534, clip_duration_ms=raw_ms,
    )
    actual = float(subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=nokey=1:noprint_wrappers=1', str(output),
    ], text=True).strip())
    assert 6 <= actual <= 11
    assert actual == pytest.approx(raw_ms / 1000 / speed, abs=1 / 30)


# --- Pixel-output parity with the Dossier CSS preview -----------------------

LOVENIGHTDRIVES = {"brightness": 0.7, "contrast": 1.95, "saturation": 1.65}


def _dossier_cc(look: dict) -> dict:
    # services.control_plane_generation.dossier_filters_to_color_correction
    cc = {
        key: (float(look[key]) - 1.0) * 100.0
        for key in ("brightness", "contrast", "saturation")
        if key in look
    }
    if "warmth" in look:
        cc["temperature"] = float(look["warmth"]) * 100.0
    if "fade" in look:
        cc["fade"] = float(look["fade"]) * 100.0
    return cc


def _mixer_coefficients(vf: str) -> list[dict[str, float]]:
    mixers = []
    for body in re.findall(r"colorchannelmixer=([^,]+)", vf):
        coefficients = dict(
            (key, float(value))
            for key, value in (part.split("=") for part in body.split(":"))
        )
        assert all(abs(value) <= 2.0 for value in coefficients.values()), body
        mixers.append(coefficients)
    return mixers


_PIXELS = [
    (18, 52, 91), (47, 113, 173), (79, 146, 201), (108, 72, 42),
    (137, 169, 94), (166, 104, 188), (196, 211, 126), (229, 181, 153),
] * 2


def _render_pixels(look: dict) -> list[tuple[int, int, int]]:
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")
    source = bytes(channel for pixel in _PIXELS for channel in pixel)
    completed = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "rawvideo", "-pixel_format", "rgb24",
            "-video_size", "8x2", "-i", "pipe:0", "-vf",
            build_cc_filter(_dossier_cc(look)), "-frames:v", "1", "-f", "rawvideo",
            "-pix_fmt", "rgb24", "pipe:1",
        ],
        input=source,
        check=True,
        capture_output=True,
    ).stdout
    return [tuple(completed[index:index + 3]) for index in range(0, len(completed), 3)]


def _matrix(pixel: list[float], matrix: list[list[float]]) -> list[float]:
    return [sum(row[index] * pixel[index] for index in range(3)) for row in matrix]


def _css_preview_pixel(source: tuple[int, int, int], look: dict) -> tuple[int, int, int]:
    """Evaluate the CSS filter functions emitted by applyCSSFilterPreview."""
    brightness = float(look.get("brightness", 1.0))
    contrast = float(look.get("contrast", 1.0))
    saturation = float(look.get("saturation", 1.0))
    fade = float(look.get("fade", 0.0))
    if fade > 0:
        brightness = min(2.0, brightness + fade * 0.4)
        contrast = max(0.2, contrast - fade * 0.3)
        saturation = max(0.2, saturation - fade * 0.4)

    def clamp(values):
        # CSS filter functions are separate primitives; each primitive's
        # output is clamped before the next function consumes it.
        return [min(1.0, max(0.0, channel)) for channel in values]

    pixel = [channel / 255 for channel in source]
    pixel = clamp([brightness * channel for channel in pixel])
    pixel = clamp([contrast * channel + 0.5 * (1 - contrast) for channel in pixel])
    pixel = clamp(_matrix(pixel, ffmpeg_module._saturation_matrix(saturation)))

    warmth = float(look.get("warmth", 0.0))
    if warmth > 0:
        amount = min(1.0, warmth / 2)
        pixel = _matrix(pixel, [
            [1 - amount + amount * 0.393, amount * 0.769, amount * 0.189],
            [amount * 0.349, 1 - amount + amount * 0.686, amount * 0.168],
            [amount * 0.272, amount * 0.534, 1 - amount + amount * 0.131],
        ])
    elif warmth < 0:
        angle = math.radians(warmth * 20)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        pixel = _matrix(pixel, [
            [0.213 + 0.787 * cos_a - 0.213 * sin_a, 0.715 - 0.715 * cos_a - 0.715 * sin_a, 0.072 - 0.072 * cos_a + 0.928 * sin_a],
            [0.213 - 0.213 * cos_a + 0.143 * sin_a, 0.715 + 0.285 * cos_a + 0.140 * sin_a, 0.072 - 0.072 * cos_a - 0.283 * sin_a],
            [0.213 - 0.213 * cos_a - 0.787 * sin_a, 0.715 - 0.715 * cos_a + 0.715 * sin_a, 0.072 + 0.928 * cos_a + 0.072 * sin_a],
        ])
    return tuple(round(255 * min(1.0, max(0.0, channel))) for channel in pixel)


@pytest.mark.parametrize("look", [
    {"brightness": 0.7},
    {"contrast": 1.95},
    {"saturation": 1.65},
    {"warmth": 0.8},
    {"warmth": -0.8},
    {"brightness": 0.85, "contrast": 1.35, "saturation": 1.2, "fade": 0.25},
    LOVENIGHTDRIVES,
])
def test_ffmpeg_pixels_match_the_dossier_css_preview_math(look):
    got = _render_pixels(look)
    expected = [_css_preview_pixel(pixel, look) for pixel in _PIXELS]
    worst = max(abs(actual - wanted) for output, preview in zip(got, expected) for actual, wanted in zip(output, preview))
    # rgba64le retains the calculated matrix between stages; conversion and
    # final integer rounding may differ from the browser by one 8-bit level.
    assert worst <= 1


def test_strong_look_uses_real_alpha_offsets_and_only_legal_stages():
    vf = build_cc_filter(_dossier_cc(LOVENIGHTDRIVES))
    assert vf.startswith("format=rgba64le,")
    assert vf.endswith(",format=rgb24")
    mixers = _mixer_coefficients(vf)
    assert len(mixers) == 3
    contrast = mixers[1]
    assert contrast["rr"] == pytest.approx(1.95)
    assert contrast["ra"] == pytest.approx(-0.475)
    assert contrast["ga"] == pytest.approx(-0.475)
    assert contrast["ba"] == pytest.approx(-0.475)


@pytest.mark.parametrize("brightness", [0.0, 1.0, 3.0])
@pytest.mark.parametrize("contrast", [0.0, 1.0, 3.0])
@pytest.mark.parametrize("saturation", [0.0, 1.0, 3.0])
@pytest.mark.parametrize("warmth,fade", [(0.0, 0.0), (1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (1.0, 1.0)])
def test_every_dossier_slider_extreme_stays_inside_the_mixer_range(brightness, contrast, saturation, warmth, fade):
    look = {"brightness": brightness, "contrast": contrast, "saturation": saturation, "warmth": warmth, "fade": fade}
    _mixer_coefficients(build_cc_filter(_dossier_cc(look)))
