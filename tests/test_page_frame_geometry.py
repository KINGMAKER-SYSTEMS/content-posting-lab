"""Page-frame picture geometry parity with the shared Team 2 fixture table.

tests/fixtures/page-frame-geometry-fixtures.v1.json is the parity contract the
Worker and the Dossier preview copy byte-for-byte. The Lab's geometry must equal
it for every case: band, picture rectangle (exact integers) and source window.
"""
from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from pathlib import Path

import pytest

from services import page_frame

FIXTURES = Path(__file__).parent / "fixtures" / "page-frame-geometry-fixtures.v1.json"
# The shared table's bytes; a different copy is a different contract.
FIXTURES_SHA256 = "3fd1466e6d01a4f06d13c765917369f6ad54ada5ce404270ef4c1bd41ea92a83"


def _cases():
    table = json.loads(FIXTURES.read_text())
    assert table["schema"] == "page-frame-geometry-fixtures.v1"
    assert table["canvas"] == {"width": 1080, "height": 1920}
    return table["cases"]


def test_fixture_table_is_the_shared_contract():
    assert hashlib.sha256(FIXTURES.read_bytes()).hexdigest() == FIXTURES_SHA256
    cases = _cases()
    assert len(cases) == 360
    assert {(case["frame"], case["frameFit"]) for case in cases} == {
        ("9:16", "fill"), *((frame, fit) for frame in ("16:9", "1:1", "3:4", "4:3") for fit in ("fill", "fit"))}


@pytest.mark.parametrize("case", _cases(), ids=lambda case: (
    f"{case['source']['name']}-{case['frame']}-{case['frameFit']}-"
    f"z{case['clipCrop']['zoom']}-{case['clipCrop']['focusX']}-{case['clipCrop']['focusY']}"))
def test_geometry_equals_the_fixture_table(case):
    crop = case["clipCrop"]
    result = page_frame.geometry(case["source"]["width"], case["source"]["height"], case["frame"],
                                 case["frameFit"], crop["zoom"], crop["focusX"], crop["focusY"])
    assert result["band"] == case["band"]
    assert result["picture"] == case["picture"]
    assert {key: round(value, 3) for key, value in result["window"].items()} == case["window"]


@pytest.mark.parametrize("frame,band,top", [
    ("9:16", 1920, 0), ("16:9", 608, 656), ("1:1", 1080, 420), ("3:4", 1440, 240), ("4:3", 810, 554)])
def test_band_top_is_the_193_letterbox_row(frame, band, top):
    assert page_frame.band_height(frame) == band
    assert page_frame.band_top(frame) == top
    if frame != "9:16":
        # #193's prepared-post letterbox keeps exactly the same rows.
        assert page_frame.letterbox_filter(band) == (
            f"crop=1080:{band}:0:{top},pad=1080:1920:0:{top}:black")


def test_absent_fit_resolves_to_fill_on_vertical_and_fit_on_every_other_frame():
    assert page_frame.geometry(1920, 1080) == page_frame.geometry(1920, 1080, "9:16", "fill")
    for frame in ("16:9", "1:1", "3:4", "4:3"):
        assert page_frame.geometry(1920, 1080, frame) == page_frame.geometry(1920, 1080, frame, "fit")


@pytest.mark.parametrize("frame,fit", [
    ("9:16", "fit"), ("2:3", "fill"), ("16x9", "fit"), ("16:9", "stretch"), ("1:1", "FIT"), (None, "fill")])
def test_geometry_rejects_invalid_frame_and_fit_combinations(frame, fit):
    with pytest.raises(ValueError):
        page_frame.geometry(1920, 1080, frame, fit)


@pytest.mark.parametrize("treatment,expected", [
    ({}, ("9:16", "fill")),
    ({"frame": "9:16"}, ("9:16", "fill")),
    ({"frame": "16:9"}, ("16:9", "fit")),
    ({"frame": "1:1", "frameFit": "fill"}, ("1:1", "fill")),
    ({"frame": "3:4", "frameFit": "fit"}, ("3:4", "fit")),
    ({"frame": "4:3"}, ("4:3", "fit")),
])
def test_treatment_frame_fit_resolves_its_default(treatment, expected):
    assert page_frame.resolved_frame(treatment) == expected
    assert page_frame.frame_fit(treatment) == expected[1]


@pytest.mark.parametrize("treatment", [
    {"frameFit": "fill"},
    {"frameFit": "fit"},
    {"frame": "9:16", "frameFit": "fill"},
    {"frame": "9:16", "frameFit": "fit"},
    {"frame": "16:9", "frameFit": "stretch"},
    {"frame": "16:9", "frameFit": None},
    {"frame": "16:9", "frameFit": ""},
    {"frame": "16:9", "frameFit": "Fit"},
    {"frame": "16:9", "frameFit": ["fit"]},
    {"frame": "16:9", "frameFit": True},
    {"frame": "2:3", "frameFit": "fit"},
])
def test_treatment_rejects_frame_fit_without_a_frame_or_with_an_unknown_value(treatment):
    with pytest.raises(ValueError):
        page_frame.frame_fit(treatment)
    with pytest.raises(ValueError):
        page_frame.resolved_frame(treatment)


def test_fit_cut_filters_render_the_table_rectangles_exactly():
    # 16:9 master, 1:1 page, zoom 1.5 focus (0.2, 0.7): the table's own numbers.
    # (1080 - 720) * 0.7 is 251.99999999999997 in IEEE doubles (Python and JS
    # alike), so the window's top row floors to 251 and aligns down to 250.
    picture_filter, pad = page_frame.fit_cut_filters(1920, 1080, "1:1", 1.5, 0.2, 0.7)
    assert picture_filter == "crop=1280:720:128:250:exact=1,scale=1080:608:flags=lanczos,setsar=1"
    assert pad == "pad=1080:1920:0:656:black"
    geometry = page_frame.geometry(1920, 1080, "1:1", "fit", 1.5, 0.2, 0.7)
    assert geometry["window"] == {"x": 128, "y": 250, "w": 1280, "h": 720}
    assert geometry["picture"] == {"x": 0, "y": 656, "w": 1080, "h": 608}


@pytest.mark.parametrize("width,height", [(1, 1), (1, 1080), (1080, 1), (1, 2), (2, 1)])
@pytest.mark.parametrize("frame", ["16:9", "1:1", "3:4", "4:3"])
def test_fit_on_a_source_under_two_pixels_falls_back_to_fill(width, height, frame):
    """No division by zero and no zero-size crop: such a source is cut fill (F7)."""
    assert page_frame.effective_frame_fit(width, height, frame, "fit") == "fill"
    assert page_frame.effective_frame_fit(width, height, frame, None) == "fill"
    result = page_frame.geometry(width, height, frame, "fit")
    assert result == page_frame.geometry(width, height, frame, "fill")
    assert result["window"]["w"] > 0 and result["window"]["h"] > 0
    with pytest.raises(ValueError, match="2 px"):
        page_frame.fit_cut_filters(width, height, frame, 1.0, 0.5, 0.5)


def test_two_pixel_sources_still_fit():
    assert page_frame.effective_frame_fit(2, 2, "16:9", "fit") == "fit"
    assert page_frame.geometry(2, 2, "16:9", "fit")["window"] == {"x": 0, "y": 0, "w": 2, "h": 2}


@pytest.mark.parametrize("width,height,sar,expected", [
    (1920, 1080, Fraction(1), (1920, 1080)),
    (1919, 1079, Fraction(1), (1918, 1079)),       # even display width, height untouched
    (720, 480, Fraction(32, 27), (852, 480)),       # DVD NTSC widescreen
    (720, 480, Fraction(8, 9), (640, 480)),         # DVD NTSC 4:3
    (1080, 608, Fraction(1216, 1215), (1080, 608)),  # plain -vf scale=1080:608 of 1920x1080
    (1080, 1920, Fraction(256, 81), (3412, 1920)),  # 16:9 picture stored squeezed into 9:16
    (480, 720, Fraction(27, 32), (404, 720)),       # the DVD frame after a quarter turn
    (1920, 1080, Fraction(0), (1920, 1080)),        # unknown SAR is square, as in ffmpeg
    (1, 1080, Fraction(1), (1, 1080)),              # a width that truncates to 0 keeps the input width
])
def test_display_size_mirrors_the_square_pixel_filter(width, height, sar, expected):
    assert page_frame.DISPLAY_PIXELS_FILTER == "scale=trunc(iw*sar/2)*2:ih,setsar=1"
    assert page_frame.display_size(width, height, sar) == expected
