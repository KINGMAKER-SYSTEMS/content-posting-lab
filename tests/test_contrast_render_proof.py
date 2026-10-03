"""Unit tests for scripts/contrast_render_proof.py comparison math.

Synthetic frames only — no master, no ffmpeg, no network. Verifies the
CSS-preview pixel helper, the Dossier look -> slider mapping, the per-channel
error statistics (MAE / p95 / p99 / max) and the PASS/FAIL tolerance the
post-deploy render proof applies.
"""

import pytest

from scripts.contrast_render_proof import (
    P99_TOLERANCE,
    channel_errors,
    css_preview_pixel,
    css_preview_rgb,
    dossier_cc,
    evaluate,
    parse_look,
    passes,
    summarize_errors,
)

LOVENIGHTDRIVES = {"brightness": 0.7, "contrast": 1.95, "saturation": 1.65}

# Eight distinct mid-tone pixels, repeated so the frame has enough channel
# samples for meaningful percentiles (mirrors tests/test_ffmpeg_cc.py _PIXELS).
_PIXELS = [
    (18, 52, 91), (47, 113, 173), (79, 146, 201), (108, 72, 42),
    (137, 169, 94), (166, 104, 188), (196, 211, 126), (229, 181, 153),
]


def _frame(pixels=_PIXELS, repeats=32) -> bytes:
    return bytes(channel for _ in range(repeats) for pixel in pixels for channel in pixel)


# --- look parsing / slider mapping -----------------------------------------


def test_parse_look_default_and_whitespace():
    assert parse_look("brightness=0.7,contrast=1.95,saturation=1.65") == LOVENIGHTDRIVES
    assert parse_look(" brightness=0.7 , contrast=1.95 , saturation=1.65 ") == (
        LOVENIGHTDRIVES
    )


@pytest.mark.parametrize("spec", ["", "brightness", "brightness=x", "a==1", "=1"])
def test_parse_look_rejects_bad_specs(spec):
    with pytest.raises(ValueError):
        parse_look(spec)


def test_dossier_cc_maps_css_look_to_sliders():
    assert dossier_cc(LOVENIGHTDRIVES) == pytest.approx(
        {"brightness": -30.0, "contrast": 95.0, "saturation": 65.0}
    )
    assert dossier_cc({"warmth": 0.5, "fade": 0.25}) == pytest.approx(
        {"temperature": 50.0, "fade": 25.0}
    )


# --- CSS preview pixel math -------------------------------------------------


def test_css_preview_rgb_matches_per_pixel():
    source = _frame()
    expected = css_preview_rgb(source, LOVENIGHTDRIVES)
    assert len(expected) == len(source)
    for index in range(0, len(source), 3):
        pixel = (source[index], source[index + 1], source[index + 2])
        got = (expected[index], expected[index + 1], expected[index + 2])
        assert got == css_preview_pixel(pixel, LOVENIGHTDRIVES)


def test_css_preview_pixel_known_value():
    # Neutral mid-grey: brightness(0.7) -> contrast(1.95, pivot 0.5) -> saturate.
    #   128/255 = 0.50196
    #   brightness: 0.7 * 0.50196 = 0.35137
    #   contrast:   1.95 * 0.35137 - 0.475 = 0.21018
    #   saturation leaves grey unchanged -> 255 * 0.21018 = 54 (rounded)
    got = css_preview_pixel((128, 128, 128), LOVENIGHTDRIVES)
    assert got == (54, 54, 54)


# --- error statistics / tolerance ------------------------------------------


def test_summarize_errors_percentiles():
    metrics = summarize_errors([0] * 98 + [3] * 2)
    assert metrics["samples"] == 100
    assert metrics["mae"] == pytest.approx(0.06)
    assert metrics["p95"] == 0
    assert metrics["p99"] == 3
    assert metrics["max"] == 3


def test_summarize_errors_empty_rejected():
    with pytest.raises(ValueError):
        summarize_errors([])


def test_channel_errors_length_mismatch_rejected():
    with pytest.raises(ValueError):
        channel_errors(b"\x00\x01\x02", b"\x00\x01")


def test_passes_tolerance_boundaries():
    assert passes({"mae": 1.0, "p99": 5.0}) is True
    assert passes({"mae": 0.1, "p99": 3.0}) is True
    assert passes({"mae": 1.5, "p99": 3.0}) is False
    assert passes({"mae": 0.1, "p99": 5.5}) is False


# --- evaluate -----------------------------------------------------------------


def test_evaluate_exact_expected_frame_passes():
    source = _frame()
    expected = css_preview_rgb(source, LOVENIGHTDRIVES)
    metrics, ok = evaluate(source, expected, LOVENIGHTDRIVES)
    assert ok is True
    assert metrics["mae"] == 0
    assert metrics["p95"] == 0
    assert metrics["p99"] == 0
    assert metrics["max"] == 0


def test_evaluate_accepts_small_codec_rounding():
    source = _frame()
    expected = bytearray(css_preview_rgb(source, LOVENIGHTDRIVES))
    expected[0] = min(255, expected[0] + 1)
    expected[5] = max(0, expected[5] - 1)
    metrics, ok = evaluate(source, bytes(expected), LOVENIGHTDRIVES)
    assert ok is True
    assert metrics["mae"] <= 1.0
    assert metrics["p99"] <= P99_TOLERANCE


def test_evaluate_flags_uncorrected_frame_as_fail():
    source = _frame()
    metrics, ok = evaluate(source, source, LOVENIGHTDRIVES)
    assert ok is False
    assert metrics["max"] > P99_TOLERANCE
