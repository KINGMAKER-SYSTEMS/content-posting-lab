"""Unit tests for services/ffmpeg.py color-correction math.

Covers the matrix/vector transforms in build_cc_filter and the is_default_cc
fast-path guard. These verify the brightness/contrast/saturation/temperature/
tint 3x3 transforms and edge cases (fade clamps, shadow boundary, second
default check) so a silent regression can't produce wrong color on the burn
router's TikTok-optimized encodes.
"""

import math
import re

import pytest

from services import ffmpeg as ffmpeg_module
from services.ffmpeg import build_cc_filter, is_default_cc, run_color_correct


# --- helpers ---------------------------------------------------------------

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


@pytest.mark.asyncio
async def test_speed_render_keeps_optional_audio_in_lockstep(monkeypatch):
    captured = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"", b""

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


def test_active_cc_appends_scale_filter_last():
    vf = build_cc_filter({"brightness": 20}, scale="1080:1920")
    assert vf.startswith("format=rgb24,colorchannelmixer=")
    assert vf.endswith(",scale=1080:1920:flags=lanczos,setsar=1")


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


def test_sharpness_alone_keeps_identity_mixer():
    # Sharpness doesn't touch the color matrix; the mixer stays identity.
    vf = build_cc_filter({"sharpness": 25})
    assert _is_identity(vf)
