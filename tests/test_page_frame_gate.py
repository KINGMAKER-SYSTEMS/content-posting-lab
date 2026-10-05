"""Frame-cut integration keeps typed captions inside their visible picture band.

The caption renderer and quality gate share full-canvas pixel coordinates.
A caption that fits the band keeps its exact bytes; a taller caption shrinks
before the gate instead of painting onto the bars.
"""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from burn_quality_gate import overlay_geometry_reasons
from services import page_frame
from services import post_render as render
from services.caption_render import render_caption_overlay
from tests import test_post_render as post_tests

FONTS = Path(__file__).parents[1] / "fonts"
FRAMES = ("16:9", "1:1", "3:4", "4:3")
FITS = (None, "fill", "fit")
BASE_STYLE = {"font": "TikTokSans16pt-Bold.ttf", "size_pt": 24, "color": "#ffffff", "outline": "#000000",
              "position": "middle", "align": "center", "case": "as_written", "background": "none",
              "offset_pct": 0, "line_balance": 0}
# (style name, style change, caption name, caption): renderable pairs covering
# the three alignments, a box background, multi-line and a caption taller than
# the 16:9 and 4:3 bands.
PAIRS = (
    ("24pt-centre", {}, "one-line", "already treated"),
    ("32pt-left", {"size_pt": 32, "align": "left"}, "three-lines",
     "first line here\nsecond line here\nthird line here"),
    ("30pt-right-box", {"size_pt": 30, "align": "right", "background": "box", "background_color": "#101010"},
     "one-line", "already treated"),
    ("52pt-centre", {"size_pt": 52}, "six-lines", "one\ntwo\nthree\nfour\nfive\nsix"),
)
PAGE_PLACEMENTS = (("bottom", -12), ("middle", 0))


def _request(style: dict, caption: str, frame=None, fit=None):
    treatment = {**post_tests.TREATMENT, "captionStyle": style}
    if frame is not None:
        treatment["frame"] = frame
    if fit is not None:
        treatment["frameFit"] = fit
    raw = json.dumps(treatment, sort_keys=True, separators=(",", ":"))
    return post_tests.request(render_treatment_json=raw, treatment_sha256=render.sha256(raw.encode()),
                              caption=caption, caption_sha256=render.sha256(caption.encode()))


def _gate(request):
    """Exactly render_post's typed gate call."""
    caption_request = render._caption_request(request)
    height = render._frame_band_height(request)
    band_rows = page_frame.frame_band_rows(height) if height is not None else None
    overlay = render_caption_overlay(caption_request, font_dir=FONTS, fit_rows=band_rows)
    reasons = overlay_geometry_reasons(overlay.overlay.base64, caption_request.style.model_dump(exclude_none=True), band_rows=band_rows)
    return overlay, reasons, caption_request.style.model_dump(exclude_none=True)


def _ink_box(overlay_b64: str):
    with Image.open(io.BytesIO(base64.b64decode(overlay_b64))) as image:
        return image.convert("RGBA").split()[3].point(lambda a: 255 if a > 16 else 0).getbbox()


def _block_centre_y(overlay) -> float:
    """Centre of the laid-out caption block (the renderer's own plan, not glyph ink)."""
    lines, height = overlay.plan.lines, overlay.plan.line_height_px
    return (lines[0].center_y_px - height / 2 + lines[-1].center_y_px + height / 2) / 2


def gate_identity_rows() -> list[dict]:
    """Every frame x fit x style/caption pair: framed verdict vs 9:16 verdict (lab-matrix.md)."""
    rows = []
    for style_name, style_change, caption_name, caption in PAIRS:
        vertical_style = {**BASE_STYLE, **style_change, "position": "middle", "offset_pct": 0}
        vertical_overlay, vertical_reasons, _ = _gate(_request(vertical_style, caption))
        left, top, right, bottom = _ink_box(vertical_overlay.overlay.base64)
        for frame in FRAMES:
            band = page_frame.band_height(frame)
            band_top = page_frame.band_top(frame)
            for fit in FITS:
                for position, offset in PAGE_PLACEMENTS:
                    page_style = {**BASE_STYLE, **style_change, "position": position, "offset_pct": offset}
                    overlay, reasons, _ = _gate(_request(page_style, caption, frame, fit))
                    left, top, right, bottom = _ink_box(overlay.overlay.base64)
                    rows.append({
                        "style": style_name, "caption": caption_name, "frame": frame, "fit": fit,
                        "pagePlacement": f"{position}/{offset}",
                        "framedReasons": reasons, "verticalReasons": vertical_reasons,
                        "identicalOverlay": overlay.overlay.sha256 == vertical_overlay.overlay.sha256,
                        "captionBox": {"top": top, "bottom": bottom, "height": bottom - top},
                        "captionCentreY": _block_centre_y(overlay),
                        "band": {"top": band_top, "height": band}, "bandCentreY": band_top + band / 2,
                        "reachesBars": top < band_top or bottom > band_top + band,
                    })
    return rows


def test_framed_gate_verdict_is_the_vertical_verdict_for_the_same_caption_and_style():
    rows = gate_identity_rows()
    assert len(rows) == len(PAIRS) * len(FRAMES) * len(FITS) * len(PAGE_PLACEMENTS)
    for row in rows:
        assert not row["reachesBars"], row
        if row["style"] != "52pt-centre":
            assert row["identicalOverlay"], row
        assert row["framedReasons"] == row["verticalReasons"], row
        if row["style"] != "52pt-centre":
            # A caption that already fits retains its exact middle anchor.
            assert abs(row["captionCentreY"] - row["bandCentreY"]) <= 1.0, row
        else:
            # Tall glyph ink can require a small vertical correction because
            # its font bearings differ from the line block's centre. It stays
            # visually centred in the band as well as entirely inside it.
            ink_centre = (row["captionBox"]["top"] + row["captionBox"]["bottom"]) / 2
            assert abs(ink_centre - row["bandCentreY"]) <= row["band"]["height"] * 0.05, row
    # The grid includes passing captions, not only refusals.
    assert any(not row["framedReasons"] for row in rows)


@pytest.mark.parametrize("frame", FRAMES)
def test_band_gate_uses_full_canvas_coordinates(frame):
    """The gate receives a full overlay and the visible band separately."""
    band, top = page_frame.band_height(frame), page_frame.band_top(frame)
    checked = 0
    for _, style_change, _, caption in PAIRS:
        overlay, reasons, style = _gate(_request({**BASE_STYLE, **style_change}, caption, frame))
        left, ink_top, right, bottom = _ink_box(overlay.overlay.base64)
        if ink_top < top or bottom > top + band:
            continue  # taller than the band: see the observation test below
        with Image.open(io.BytesIO(base64.b64decode(overlay.overlay.base64))) as image:
            banded = image.convert("RGBA").crop((0, top, 1080, top + band))
            buffer = io.BytesIO()
            banded.save(buffer, format="PNG")
        band_reasons = overlay_geometry_reasons(base64.b64encode(buffer.getvalue()).decode(), style)
        assert any(reason.startswith("outside_area:") for reason in band_reasons)
        assert reasons == []
        checked += 1
    assert checked >= 3


def test_a_caption_taller_than_the_band_shrinks_and_passes():
    """The integrated smart-fit renderer keeps tall captions off the bars."""
    overlay, reasons, _ = _gate(_request({**BASE_STYLE, "size_pt": 52}, PAIRS[-1][3], "16:9"))
    _, top, _, bottom = _ink_box(overlay.overlay.base64)
    assert bottom - top <= page_frame.band_height("16:9")
    assert top >= page_frame.band_top("16:9") and bottom <= page_frame.band_top("16:9") + 608
    assert overlay.plan.fitted_font_size_px < overlay.plan.font_size_px
    assert reasons == []
