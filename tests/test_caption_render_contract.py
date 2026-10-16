import base64
import hashlib
import inspect
import io
import json
import shutil
import unicodedata
from pathlib import Path

import pytest
from PIL import Image, ImageFont, features
from pydantic import ValidationError

from services import caption_render
from services.caption_render import (
    CaptionRenderError,
    CaptionRenderRequest,
    CaptionRenderRequestV2,
    caption_fit_area,
    render_caption_overlay,
)


FONT_FILE = "TikTokSans16pt-Bold.ttf"


@pytest.fixture
def font_dir(tmp_path):
    source = Path(__file__).parents[1] / "fonts" / FONT_FILE
    target = tmp_path / "fonts"
    target.mkdir()
    shutil.copy2(source, target / FONT_FILE)
    return target


def request(**style_overrides):
    style = {
        "font": FONT_FILE,
        "size_pt": 32,
        "color": "#FFFFFF",
        "outline": "#000000",
        "position": "middle",
        "align": "center",
        "case": "as_written",
        "background": "none",
        "offset_pct": 0,
        "line_balance": 0,
    }
    style.update(style_overrides)
    return CaptionRenderRequest.model_validate(
        {
            "schema": "content-lab.caption-render-request.v1",
            "caption": "one short line",
            "style": style,
        }
    )


def test_renderer_returns_deterministic_9_by_16_png_and_hashes(font_dir):
    first = render_caption_overlay(request(), font_dir=font_dir)
    second = render_caption_overlay(request(), font_dir=font_dir)

    assert first == second
    result = first.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert result["schema"] == "content-lab.caption-render-result.v1"
    assert result["renderer"]["id"] == "content-lab.pillow-caption.v1"
    png = base64.b64decode(result["overlay"]["base64"])
    assert result["overlay"]["sha256"] == f"sha256:{hashlib.sha256(png).hexdigest()}"
    with Image.open(io.BytesIO(png)) as image:
        assert image.size == (1080, 1920)
        assert image.mode == "RGBA"
        assert image.getchannel("A").getbbox() is not None


def test_explicit_upright_is_byte_identical_without_mutating_input(font_dir):
    plain = request()
    payload = plain.model_dump(by_alias=True)
    payload["style"]["inverted"] = False
    upright = CaptionRenderRequest.model_validate(payload)
    assert payload["style"]["inverted"] is False
    assert render_caption_overlay(upright, font_dir=font_dir) == render_caption_overlay(plain, font_dir=font_dir)


@pytest.mark.parametrize("value", [0, 1, "false", "true", None])
def test_inverted_accepts_only_a_real_boolean(value):
    with pytest.raises(ValidationError):
        request(inverted=value)


def test_explicit_newlines_survive_balance_and_case_transform(font_dir):
    payload = request(case="upper", line_balance=50)
    payload = payload.model_copy(update={"caption": "one two three\n\nfour five"})

    result = render_caption_overlay(payload, font_dir=font_dir).model_dump(
        mode="json", by_alias=True, exclude_none=True
    )

    assert result["plan"]["rendered_text"].split("\n") == [
        "ONE TWO",
        "THREE",
        "",
        "FOUR",
        "FIVE",
    ]
    assert [line["text"] for line in result["plan"]["lines"]] == [
        "ONE TWO",
        "THREE",
        "",
        "FOUR",
        "FIVE",
    ]


def test_every_typed_style_control_reaches_the_effective_render_plan(font_dir):
    payload = request(
        size_pt=44,
        color="#AABBCC",
        outline="#102030",
        position="bottom",
        align="right",
        case="lower",
        background="highlight",
        background_color="#FFEEDD",
        offset_pct=-7.5,
        line_balance=100,
    )
    result = render_caption_overlay(payload, font_dir=font_dir).model_dump(
        mode="json", by_alias=True, exclude_none=True
    )

    assert result["plan"]["effective_style"] == {
        "font": FONT_FILE,
        "size_pt": 44.0,
        "color": "#aabbcc",
        "outline": "#102030",
        "position": "bottom",
        "align": "right",
        "case": "lower",
        "background": "highlight",
        "background_color": "#ffeedd",
        "offset_pct": -7.5,
        "line_balance": 100,
    }
    assert result["plan"]["font_size_px"] == 110
    assert result["plan"]["stroke_width_px"] == 3
    assert {line["x_px"] for line in result["plan"]["lines"]} == {972}
    assert result["plan"]["rendered_text"] == "one\nshort\nline"


@pytest.mark.parametrize("field", ["font", "size_pt", "color", "position", "align", "line_balance"])
def test_required_effective_style_fields_fail_closed(field):
    payload = request().model_dump()
    del payload["style"][field]
    with pytest.raises(ValidationError):
        CaptionRenderRequest.model_validate(payload)


def test_unknown_or_out_of_contract_style_fields_are_rejected():
    payload = request().model_dump()
    payload["style"]["max_width"] = 80
    with pytest.raises(ValidationError):
        CaptionRenderRequest.model_validate(payload)

    payload = request().model_dump()
    payload["style"]["line_balance"] = 101
    with pytest.raises(ValidationError):
        CaptionRenderRequest.model_validate(payload)


def test_non_content_lab_font_and_missing_background_color_are_rejected():
    payload = request().model_dump()
    payload["style"]["font"] = "Impact.ttf"
    with pytest.raises(ValidationError):
        CaptionRenderRequest.model_validate(payload)

    payload = request().model_dump()
    payload["style"]["background"] = "highlight"
    with pytest.raises(ValidationError):
        CaptionRenderRequest.model_validate(payload)


def test_font_must_exist_in_the_advertised_content_lab_directory(tmp_path):
    with pytest.raises(CaptionRenderError) as error:
        render_caption_overlay(request(), font_dir=tmp_path)
    assert error.value.code == "CAPTION_FONT_UNAVAILABLE"


def test_caption_too_wide_for_the_screen_shrinks_instead_of_refusing(font_dir):
    # Owner rule 2026-10-04: this used to refuse with CAPTION_LINE_TOO_WIDE.
    # Now it shrinks until it fits the visible picture; nothing is clipped.
    payload = request(size_pt=96, line_balance=0)
    payload = payload.model_copy(
        update={"caption": "this caption is deliberately much too wide for a single line"}
    )
    result = render_caption_overlay(payload, font_dir=font_dir)
    assert 30 <= result.plan.fitted_font_size_px < 240
    assert result.plan.rendered_text == payload.caption
    left, top, right, bottom = ink_box(result)
    assert 44 <= left and right <= 1035


def test_api_contract_returns_png_and_fail_closed_errors(sync_client, monkeypatch, font_dir):
    from routers import burn as burn_router

    monkeypatch.setattr(burn_router, "FONT_DIR", font_dir)
    payload = request(background="box", background_color="#112233").model_dump()

    response = sync_client.post("/api/burn/caption-render/v1", json=payload)
    assert response.status_code == 200
    result = response.json()
    assert result["schema"] == "content-lab.caption-render-result.v1"
    assert result["plan"]["effective_style"]["background"] == "box"
    assert base64.b64decode(result["overlay"]["base64"]).startswith(b"\x89PNG\r\n\x1a\n")

    missing_font = sync_client.post(
        "/api/burn/caption-render/v1",
        json={**payload, "style": {**payload["style"], "font": "TikTokSans16pt-Black.ttf"}},
    )
    assert missing_font.status_code == 422
    assert missing_font.json() == {
        "schema": "content-lab.caption-render-error.v1",
        "error": "CAPTION_FONT_UNAVAILABLE",
        "message": "font is not installed in Content Lab",
    }

    invented_field = sync_client.post(
        "/api/burn/caption-render/v1",
        json={**payload, "style": {**payload["style"], "stroke_width": 9}},
    )
    assert invented_field.status_code == 422


def test_reusable_layout_uses_exact_lines_and_saved_outline_geometry(font_dir):
    payload = request(
        line_balance=100,
        line_breaks=["one", "short line"],
        outline_width_px=7,
    )

    result = render_caption_overlay(payload, font_dir=font_dir).model_dump(
        mode="json", by_alias=True, exclude_none=True
    )

    assert result["plan"]["rendered_text"] == "one\nshort line"
    assert [line["text"] for line in result["plan"]["lines"]] == ["one", "short line"]
    assert result["plan"]["stroke_width_px"] == 7


def test_reusable_layout_cannot_change_the_caption_words():
    with pytest.raises(ValidationError, match="line_breaks must preserve the caption text"):
        request(line_breaks=["different", "words"])


def test_port_8002_burn_server_exposes_the_same_typed_contract(
    monkeypatch, font_dir, tmp_path
):
    from fastapi.testclient import TestClient

    (tmp_path / "static" / "burn").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    import burn_server

    monkeypatch.setattr(burn_server, "FONT_DIR", font_dir)
    payload = request(
        position="top",
        align="left",
        background="box",
        background_color="#112233",
    ).model_dump(mode="json", by_alias=True, exclude_none=True)

    response = TestClient(burn_server.app).post(
        "/api/burn/caption-render/v1",
        json=payload,
    )
    assert response.status_code == 200
    result = response.json()
    assert result["schema"] == "content-lab.caption-render-result.v1"
    assert result["plan"]["effective_style"] == payload["style"]
    overlay = base64.b64decode(result["overlay"]["base64"])
    assert result["overlay"]["sha256"] == (
        f"sha256:{hashlib.sha256(overlay).hexdigest()}"
    )

    invalid = TestClient(burn_server.app).post(
        "/api/burn/caption-render/v1",
        json={**payload, "style": {**payload["style"], "weight": 700}},
    )
    assert invalid.status_code == 422


def test_long_caption_renders_without_word_count_rejection(font_dir):
    lines = [f"these are the original words number {i}" for i in range(8)]
    caption = " ".join(lines)
    raw = request(size_pt=18).model_dump(mode="json", by_alias=True)
    raw["caption"] = caption
    raw["style"]["line_breaks"] = lines
    payload = CaptionRenderRequest.model_validate(raw)
    result = render_caption_overlay(payload, font_dir=font_dir).model_dump(mode="json", by_alias=True)
    assert result["plan"]["rendered_text"] == "\n".join(lines)
    from burn_quality_gate import run_quality_check
    check = run_quality_check(caption, overlay_png=result["overlay"]["base64"],
                              caption_style=raw["style"])
    assert check["ok"] is True, check["reasons"]


# Owner rule 2026-10-04: a caption stays inside the visible picture - the
# whole canvas on a full-screen page, the band of rows a framed page keeps.
# Its size is the maximum: inside, it is drawn as styled however big;
# leaving, it only shrinks. Band rows are what services/page_frame.py keeps.
FRAME_BAND_ROWS = {"16:9": (656, 1263), "4:3": (554, 1363), "1:1": (420, 1499), "3:4": (240, 1679)}
# Inclusive fit areas: the picture's rows, 44 px in from each side.
SAFE_AREAS = {None: (44, 0, 1035, 1919), "16:9": (44, 656, 1035, 1263), "4:3": (44, 554, 1035, 1363),
              "1:1": (44, 420, 1035, 1499), "3:4": (44, 240, 1035, 1679)}
SHORT_LINES = ["when you", "finally", "see the", "light", "again", "at last",
               "and it", "all ends", "so well", "tonight"]


def lines_request(lines, **style_overrides):
    raw = request(**style_overrides).model_dump(mode="json", by_alias=True)
    raw["caption"] = " ".join(lines)
    raw["style"]["line_breaks"] = lines
    return CaptionRenderRequest.model_validate(raw)


def ink_box(result):
    png = base64.b64decode(result.overlay.base64)
    with Image.open(io.BytesIO(png)) as image:
        box = image.getchannel("A").getbbox()
    return box[0], box[1], box[2] - 1, box[3] - 1


def inside(box, area):
    return area[0] <= box[0] and area[1] <= box[1] and box[2] <= area[2] and box[3] <= area[3]


def test_fit_areas_are_the_picture_inset_by_the_gate_side_margins():
    from services.caption_render import caption_fit_area
    from services.page_frame import FRAME_BAND_HEIGHTS, frame_band_rows
    assert {frame: frame_band_rows(height) for frame, height in FRAME_BAND_HEIGHTS.items()} == FRAME_BAND_ROWS
    assert {frame: caption_fit_area(FRAME_BAND_ROWS.get(frame)) for frame in SAFE_AREAS} == SAFE_AREAS
    # 44 px is the quality gate's 4% side margin on 1080 columns.
    assert 44 / 1080 >= 0.04 > 43 / 1080


@pytest.mark.parametrize("frame,line_count", [("16:9", 4), ("4:3", 6), ("1:1", 8), ("3:4", 10)])
@pytest.mark.parametrize("variant", [{}, {"inverted": True},
                                     {"background": "box", "background_color": "#112233"},
                                     {"background": "highlight", "background_color": "#112233"}])
def test_tall_caption_on_a_framed_page_shrinks_to_fit_inside_the_band(font_dir, frame, line_count, variant):
    payload = lines_request(SHORT_LINES[:line_count], size_pt=60, **variant)
    top, bottom = FRAME_BAND_ROWS[frame]

    # At the full 60 pt it fits the full screen but would cover the black bars.
    full_screen = render_caption_overlay(payload, font_dir=font_dir)
    assert full_screen.plan.font_size_px == 150 and full_screen.plan.fitted_font_size_px is None
    _, full_top, _, full_bottom = ink_box(full_screen)
    assert full_top < top or full_bottom > bottom

    result = render_caption_overlay(payload, font_dir=font_dir, fit_rows=(top, bottom))
    assert inside(ink_box(result), SAFE_AREAS[frame])
    plan = result.plan
    assert plan.font_size_px == 150
    assert 30 <= plan.fitted_font_size_px < 150
    assert plan.line_height_px == round(plan.fitted_font_size_px * 1.08)
    # The lines are scaled, never re-wrapped, and stay centred in the band.
    assert plan.rendered_text == "\n".join(SHORT_LINES[:line_count])
    _, first, _, last = ink_box(result)
    assert abs((first + last) / 2 - (top + bottom) / 2) <= 24
    assert result.style_sha256 == full_screen.style_sha256


@pytest.mark.parametrize("frame", [None, "16:9"])
def test_fit_is_the_largest_size_that_fits(font_dir, frame):
    rows = FRAME_BAND_ROWS.get(frame)
    lines = SHORT_LINES[:4] if frame else SHORT_LINES[:10]
    size_pt = 60 if frame else 80
    fitted = render_caption_overlay(lines_request(lines, size_pt=size_pt), font_dir=font_dir,
                                    fit_rows=rows).plan.fitted_font_size_px
    assert fitted is not None
    # Asked for exactly the fitted size, nothing shrinks; one pixel more does not fit.
    exact = render_caption_overlay(lines_request(lines, size_pt=fitted / 2.5), font_dir=font_dir, fit_rows=rows)
    assert exact.plan.font_size_px == fitted and exact.plan.fitted_font_size_px is None
    bigger = lines_request(lines, size_pt=(fitted + 1) / 2.5)
    assert render_caption_overlay(bigger, font_dir=font_dir, fit_rows=rows).plan.fitted_font_size_px == fitted


@pytest.mark.parametrize("frame", list(FRAME_BAND_ROWS))
def test_small_caption_on_a_framed_page_is_not_shrunk(font_dir, frame):
    full = render_caption_overlay(request(), font_dir=font_dir)
    framed = render_caption_overlay(request(), font_dir=font_dir, fit_rows=FRAME_BAND_ROWS[frame])
    # Byte-identical overlay and plan: a caption that fits keeps its hashes.
    assert framed == full
    assert framed.plan.font_size_px == 80 and framed.plan.fitted_font_size_px is None
    assert "fitted_font_size_px" not in framed.model_dump(mode="json", by_alias=True)["plan"]


def test_huge_caption_inside_the_full_screen_is_drawn_as_styled(font_dir):
    # 983 px wide: it used to refuse (CAPTION_LINE_TOO_WIDE, over 864 px), but
    # it is inside the screen and its side margins, so it is drawn exactly as styled.
    payload = request(size_pt=72).model_copy(update={"caption": "keep going"})
    result = render_caption_overlay(payload, font_dir=font_dir)
    assert result.plan.font_size_px == 180 and result.plan.fitted_font_size_px is None
    assert result.plan.lines[0].width_px > 864
    assert inside(ink_box(result), SAFE_AREAS[None])
    from burn_quality_gate import overlay_geometry_reasons
    assert overlay_geometry_reasons(result.overlay.base64, payload.style.model_dump(exclude_none=True)) == []


def test_caption_slightly_too_wide_for_the_full_screen_shrinks_to_fit(font_dir):
    # 1028 px wide at 72 pt: more than the 992 px between the side margins, so it shrinks.
    payload = request(size_pt=72).model_copy(update={"caption": "never again"})
    result = render_caption_overlay(payload, font_dir=font_dir)
    assert 170 <= result.plan.fitted_font_size_px < 180
    assert inside(ink_box(result), SAFE_AREAS[None])


def test_too_tall_caption_on_the_full_screen_shrinks_to_fit(font_dir):
    # Ten lines at 96 pt are 2590 px of line height: taller than the screen.
    # This used to refuse with CAPTION_OUT_OF_FRAME; now it shrinks.
    result = render_caption_overlay(lines_request(SHORT_LINES, size_pt=96), font_dir=font_dir)
    assert result.plan.fitted_font_size_px < 240
    assert inside(ink_box(result), SAFE_AREAS[None])


def test_framed_caption_with_a_too_wide_line_shrinks_into_the_picture(font_dir):
    payload = request(size_pt=96).model_copy(update={"caption": "this line is much too wide"})
    result = render_caption_overlay(payload, font_dir=font_dir, fit_rows=FRAME_BAND_ROWS["16:9"])
    assert result.plan.fitted_font_size_px < 240
    assert inside(ink_box(result), SAFE_AREAS["16:9"])


@pytest.mark.parametrize("position,offset", [("top", -10), ("bottom", 10)])
def test_caption_near_the_edge_but_inside_the_picture_is_unchanged(font_dir, position, offset):
    # Two lines centred about 96 rows from the screen's edge: inside the
    # picture, so drawn exactly as styled and passed by the gate.
    from burn_quality_gate import overlay_geometry_reasons

    payload = request(size_pt=16, position=position, offset_pct=offset, line_balance=50)
    result = render_caption_overlay(payload, font_dir=font_dir)
    assert result.plan.fitted_font_size_px is None
    box = ink_box(result)
    assert inside(box, SAFE_AREAS[None]) and (box[1] < 77 or box[3] > 1842)
    assert overlay_geometry_reasons(result.overlay.base64, payload.style.model_dump(exclude_none=True)) == []


@pytest.mark.parametrize("lines,size_pt,fits", [(SHORT_LINES[:4], 60, True),
                                               ([f"line {i}" for i in range(24)], 12, False)])
def test_drawn_pixels_are_rechecked_even_when_the_measurement_under_reports(
        font_dir, monkeypatch, lines, size_pt, fits):
    # Fake a fit measurement that always says the style's size fits. The check
    # of the drawn pixels must still shrink the caption into the band, or
    # refuse it, instead of letting it cover the black bars.
    from services import caption_render

    monkeypatch.setattr(caption_render, "_fit_font_size", lambda font_at, lines, **kw: kw["max_px"])
    payload = lines_request(lines, size_pt=size_pt)
    rows = FRAME_BAND_ROWS["16:9"]
    if not fits:
        with pytest.raises(CaptionRenderError) as error:
            render_caption_overlay(payload, font_dir=font_dir, fit_rows=rows)
        assert error.value.code == "CAPTION_OUT_OF_FRAME"
        return
    result = render_caption_overlay(payload, font_dir=font_dir, fit_rows=rows)
    assert result.plan.fitted_font_size_px is not None and result.plan.fitted_font_size_px < 150
    assert inside(ink_box(result), SAFE_AREAS["16:9"])


def test_caption_that_cannot_fit_even_at_12_pt_still_refuses(font_dir):
    lines = [f"line {i}" for i in range(24)]
    payload = lines_request(lines, size_pt=12)
    # Full screen it fits; inside the 16:9 band 24 lines cannot.
    render_caption_overlay(payload, font_dir=font_dir)
    with pytest.raises(CaptionRenderError) as error:
        render_caption_overlay(payload, font_dir=font_dir, fit_rows=FRAME_BAND_ROWS["16:9"])
    assert error.value.code == "CAPTION_OUT_OF_FRAME"


@pytest.mark.parametrize("frame,line_count", [(None, 10), ("16:9", 4), ("1:1", 8), ("3:4", 10)])
def test_quality_gate_agrees_with_the_fit(font_dir, frame, line_count):
    from burn_quality_gate import overlay_geometry_reasons

    payload = lines_request(SHORT_LINES[:line_count], size_pt=96)
    style = payload.style.model_dump(exclude_none=True)
    rows = FRAME_BAND_ROWS.get(frame)
    fitted = render_caption_overlay(payload, font_dir=font_dir, fit_rows=rows)
    # Taller than 45% of the screen is fine when it sits inside the picture.
    assert overlay_geometry_reasons(fitted.overlay.base64, style, band_rows=rows) == []
    if frame is not None:
        full_screen = render_caption_overlay(payload, font_dir=font_dir)
        reasons = overlay_geometry_reasons(full_screen.overlay.base64, style, band_rows=rows)
        assert any(reason.startswith("outside_area:") for reason in reasons)


@pytest.mark.parametrize(
    "frame,line_count",
    [("9:16", 10), ("16:9", 4), ("4:3", 6), ("1:1", 8), ("3:4", 10)],
)
def test_v2_api_binds_fit_to_the_required_picture_frame(
    sync_client, monkeypatch, font_dir, frame, line_count
):
    from routers import burn as burn_router

    monkeypatch.setattr(burn_router, "FONT_DIR", font_dir)
    payload = lines_request(SHORT_LINES[:line_count], size_pt=96)
    wire = payload.model_dump(mode="json", by_alias=True)
    wire["schema"] = "content-lab.caption-render-request.v2"
    wire["picture_frame"] = frame
    response = sync_client.post("/api/burn/caption-render/v2", json=wire)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema"] == "content-lab.caption-render-result.v2"
    assert body["plan"]["picture_frame"] == frame
    assert 30 <= body["plan"]["fitted_font_size_px"] < body["plan"]["font_size_px"]
    plan_bytes = json.dumps(
        body["plan"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    assert body["render_plan_sha256"] == "sha256:" + hashlib.sha256(plan_bytes).hexdigest()


@pytest.mark.parametrize(
    "frame,rows",
    [("16:9", (656, 1263)), ("4:3", (554, 1363)), ("1:1", (420, 1499)),
     ("3:4", (240, 1679))],
)
@pytest.mark.parametrize(
    "position,percentage",
    [("top", 15), ("middle", 50), ("bottom", 85)],
)
def test_v2_places_all_short_caption_positions_inside_the_picture_band(
    font_dir, frame, rows, position, percentage
):
    raw = request(size_pt=16.0, position=position).model_dump(mode="json", by_alias=True)
    raw.update(schema="content-lab.caption-render-request.v2", picture_frame=frame)
    payload = CaptionRenderRequestV2.model_validate(raw)

    result = render_caption_overlay(
        payload, font_dir=font_dir, fit_rows=rows
    )

    assert inside(ink_box(result), SAFE_AREAS[frame])
    expected_center = (
        960 if position == "middle"
        else round(rows[0] + (rows[1] - rows[0]) * percentage / 100)
    )
    assert result.plan.lines[0].center_y_px == expected_center
    assert result.plan.fitted_font_size_px is None


@pytest.mark.parametrize("frame", ["16:9", "4:3", "1:1", "3:4"])
@pytest.mark.parametrize("position", ["top", "bottom"])
def test_v2_endpoint_does_not_refuse_short_top_or_bottom_caption(
    sync_client, monkeypatch, font_dir, frame, position
):
    from routers import burn as burn_router

    monkeypatch.setattr(burn_router, "FONT_DIR", font_dir)
    raw = request(size_pt=16.0, position=position).model_dump(mode="json", by_alias=True)
    raw.update(schema="content-lab.caption-render-request.v2", picture_frame=frame)

    response = sync_client.post("/api/burn/caption-render/v2", json=raw)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema"] == "content-lab.caption-render-result.v2"
    assert body["plan"]["picture_frame"] == frame
    assert body["plan"]["effective_style"]["position"] == position


@pytest.mark.parametrize("frame,rows", [("16:9", (656, 1263)),
                                         ("4:3", (554, 1363)),
                                         ("1:1", (420, 1499))])
@pytest.mark.parametrize("position", ["top", "bottom"])
def test_v2_fits_tall_top_and_bottom_captions_without_false_out_of_frame(
    font_dir, frame, rows, position
):
    payload = lines_request(SHORT_LINES[:4], size_pt=60.0, position=position)
    request_wire = payload.model_dump(mode="json", by_alias=True)
    request_wire.update(schema="content-lab.caption-render-request.v2", picture_frame=frame)
    payload = CaptionRenderRequestV2.model_validate(request_wire)

    result = render_caption_overlay(payload, font_dir=font_dir, fit_rows=rows)

    assert inside(ink_box(result), SAFE_AREAS[frame])
    assert result.plan.rendered_text == "\n".join(SHORT_LINES[:4])
    # Prefer the requested band-relative position, but nudge only as needed
    # to fit a tall block inside the picture.
    assert rows[0] <= result.plan.lines[0].center_y_px <= rows[1]
    assert result.plan.fitted_font_size_px is None or 30 <= result.plan.fitted_font_size_px < 150


@pytest.mark.parametrize("position", ["top", "middle", "bottom"])
def test_v2_portrait_positions_keep_the_v1_coordinates_and_overlay(font_dir, position):
    legacy = request(size_pt=16.0, position=position)
    legacy_result = render_caption_overlay(legacy, font_dir=font_dir)
    raw = legacy.model_dump(mode="json", by_alias=True)
    raw.update(schema="content-lab.caption-render-request.v2", picture_frame="9:16")
    framed_result = render_caption_overlay(
        CaptionRenderRequestV2.model_validate(raw), font_dir=font_dir
    )

    assert framed_result.overlay == legacy_result.overlay
    assert framed_result.plan.lines == legacy_result.plan.lines


def test_v1_keeps_its_old_plan_shape_and_refuses_new_fit_behavior(
    sync_client, monkeypatch, font_dir
):
    from routers import burn as burn_router

    monkeypatch.setattr(burn_router, "FONT_DIR", font_dir)
    payload = request(size_pt=96).model_copy(
        update={"caption": "this line is much too wide"}
    )
    response = sync_client.post(
        "/api/burn/caption-render/v1",
        json=payload.model_dump(mode="json", by_alias=True),
    )
    assert response.status_code == 422
    assert response.json()["error"] == "CAPTION_FIT_REQUIRES_V2"

    small = sync_client.post(
        "/api/burn/caption-render/v1",
        json=request().model_dump(mode="json", by_alias=True),
    )
    assert small.status_code == 200
    assert small.json()["schema"] == "content-lab.caption-render-result.v1"
    assert "fitted_font_size_px" not in small.json()["plan"]


@pytest.mark.parametrize(
    "frame,line_count",
    [("9:16", 10), ("16:9", 4), ("4:3", 6), ("1:1", 8), ("3:4", 10)],
)
@pytest.mark.parametrize("position", ["top", "middle", "bottom"])
def test_v2_port_8002_route_matches_hosted_frame_fit(
    sync_client, monkeypatch, font_dir, tmp_path, frame, line_count, position
):
    from fastapi.testclient import TestClient

    from routers import burn as burn_router

    (tmp_path / "static" / "burn").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    import burn_server

    monkeypatch.setattr(burn_router, "FONT_DIR", font_dir)
    monkeypatch.setattr(burn_server, "FONT_DIR", font_dir)
    payload = lines_request(SHORT_LINES[:line_count], size_pt=96, position=position)
    wire = payload.model_dump(mode="json", by_alias=True)
    wire.update(schema="content-lab.caption-render-request.v2", picture_frame=frame)
    hosted = sync_client.post("/api/burn/caption-render/v2", json=wire)
    local = TestClient(burn_server.app).post(
        "/api/burn/caption-render/v2", json=wire
    )
    assert hosted.status_code == local.status_code == 200
    assert hosted.json() == local.json()
    body = local.json()
    assert body["plan"]["picture_frame"] == frame
    assert body["plan"]["fitted_font_size_px"] < body["plan"]["font_size_px"]
    from services.page_frame import FRAME_BAND_HEIGHTS, frame_band_rows

    rows = FRAME_BAND_HEIGHTS.get(frame)
    band_rows = frame_band_rows(rows) if rows is not None else None
    with Image.open(io.BytesIO(base64.b64decode(body["overlay"]["base64"]))) as image:
        bounds = image.getchannel("A").getbbox()
    left, top, right, bottom = caption_fit_area(band_rows)
    assert left <= bounds[0] and top <= bounds[1]
    assert bounds[2] - 1 <= right and bounds[3] - 1 <= bottom
    png = base64.b64decode(body["overlay"]["base64"])
    assert body["overlay"]["sha256"] == "sha256:" + hashlib.sha256(png).hexdigest()


def test_v2_requires_a_supported_frame_and_rejects_unknown_fields():
    raw = request().model_dump(mode="json", by_alias=True)
    raw.update(schema="content-lab.caption-render-request.v2", picture_frame="2:1")
    with pytest.raises(ValidationError):
        CaptionRenderRequestV2.model_validate(raw)
    raw["picture_frame"] = "1:1"
    raw["surprise"] = True
    with pytest.raises(ValidationError):
        CaptionRenderRequestV2.model_validate(raw)


# --- Glyph gate: a caption the font cannot fully draw is refused, never drawn as boxes ------------------------------
# Owner rule 2026-10-15: no emoji on a caption, and never a post without its caption. Every allowed TikTokSans
# file draws its own missing-glyph box for a character it lacks (and there is no fallback font), and that box
# counts as ink, so only this gate stops it. The tests use the real fonts in fonts/.

REAL_FONT_DIR = Path(__file__).parents[1] / "fonts"
REAL_FONTS = sorted(
    path.name for path in REAL_FONT_DIR.glob("TikTokSans*.ttf") if "Italic" not in path.name
)

# The caption of slot-refresh:f596e07e7310f9056c0c24bd497fcbc50ba1bcd0 (acct lovenightdrives, posted
# 2026-10-02), verbatim from the Worker's 10-02 English-gate export. It went out with two boxes at the end.
LOVENIGHTDRIVES_1002 = (
    "my sister requested a separate bed because my self soothing routine is rubbing my feet together "
    "\U0001F62D\U0001F62D"
)


def caption_request(caption, **style_overrides):
    payload = request(**{k: v for k, v in style_overrides.items() if k != "line_breaks"}).model_dump(by_alias=True)
    payload["caption"] = caption
    if "line_breaks" in style_overrides:
        payload["style"]["line_breaks"] = style_overrides["line_breaks"]
    return CaptionRenderRequest.model_validate(payload)


@pytest.mark.parametrize("caption,expected", [
    pytest.param("\U0001F62D", "U+1F62D", id="one-emoji"),
    pytest.param("\U0001F62D\U0001F62D", "U+1F62D", id="two-emoji"),
    pytest.param("ok \U0001F62D", "U+1F62D", id="text-then-emoji-is-refused-not-trimmed"),
    pytest.param("\U0001F525 on fire", "U+1F525", id="emoji-first"),
    pytest.param("so sad\u2764", "U+2764", id="old-style-heart"),
    pytest.param("ok\ufe0f fine", "U+FE0F", id="variation-selector-inside-text"),
    pytest.param("\u2800", "U+2800", id="braille-blank-only"),
    pytest.param("\u3164", "U+3164", id="hangul-filler-only"),
    pytest.param("\uffa0", "U+FFA0", id="halfwidth-hangul-filler-only"),
    pytest.param("hello \u3042", "U+3042", id="kana"),
    pytest.param(LOVENIGHTDRIVES_1002, "U+1F62D", id="real-posted-caption-lovenightdrives-1002"),
])
def test_a_caption_with_a_character_the_font_cannot_draw_is_refused(font_dir, caption, expected):
    with pytest.raises(CaptionRenderError) as error:
        render_caption_overlay(caption_request(caption), font_dir=font_dir)
    assert error.value.code == "CAPTION_GLYPH_MISSING"
    assert expected in error.value.message


@pytest.mark.parametrize("caption", ["?!", ".", "...", "\u200b", "\u200b\u200b", "\ufeff", "- - -", "#", "\u2019"])
def test_a_caption_with_nothing_drawable_to_read_is_refused_as_no_text(font_dir, caption):
    # U+200B and U+FEFF have real glyphs with no ink; punctuation has ink but no letter or digit.
    with pytest.raises(CaptionRenderError) as error:
        render_caption_overlay(caption_request(caption), font_dir=font_dir)
    assert error.value.code == "CAPTION_NO_TEXT"


@pytest.mark.parametrize("caption", [
    "it's 2am",
    "caf\u00e9",
    "#fyp",
    "WHEN YOU FINALLY SEE THE LIGHT",
    "\u0443 \u0442\u0435\u0431\u044f \u0432\u0441\u0451 \u043f\u043e\u043b\u0443\u0447\u0438\u0442\u0441\u044f",
    "rock\u2019n\u2019roll \u2014 \u201cquoted\u201d",
    "7",
    "ti\u1ebfng vi\u1ec7t",
    "two\nlines",
    "ok\u00a0nbsp and zero\u200bwidth",
])
def test_a_caption_the_font_can_draw_still_renders(font_dir, caption):
    result = render_caption_overlay(caption_request(caption), font_dir=font_dir)
    assert ink_box(result)


@pytest.mark.parametrize("font", REAL_FONTS)
def test_every_installed_font_refuses_the_posted_caption_and_draws_plain_text(font):
    assert len(REAL_FONTS) >= 20
    with pytest.raises(CaptionRenderError) as error:
        render_caption_overlay(caption_request(LOVENIGHTDRIVES_1002, font=font), font_dir=REAL_FONT_DIR)
    assert error.value.code == "CAPTION_GLYPH_MISSING"
    # The same words without the emoji draw fine in that font.
    plain = LOVENIGHTDRIVES_1002.replace("\U0001F62D", "").strip()
    assert render_caption_overlay(caption_request(plain, font=font, size_pt=12, line_balance=100), font_dir=REAL_FONT_DIR)


def test_the_gate_checks_the_text_as_it_will_be_drawn(font_dir):
    # Explicit line_breaks are drawn verbatim, so an emoji in one is refused too.
    broken = caption_request("fine words \U0001F62D", line_breaks=["fine words", "\U0001F62D"])
    with pytest.raises(CaptionRenderError) as error:
        render_caption_overlay(broken, font_dir=font_dir)
    assert error.value.code == "CAPTION_GLYPH_MISSING"
    # The case transform runs first: \u00df becomes SS, a letter the font has.
    assert render_caption_overlay(caption_request("stra\u00dfe", case="upper"), font_dir=font_dir)


def test_the_refusal_lists_each_missing_character_once_and_never_the_caption(font_dir):
    with pytest.raises(CaptionRenderError) as error:
        render_caption_overlay(caption_request("my secret words \U0001F62D\U0001F62D\U0001F525"), font_dir=font_dir)
    assert error.value.message == "caption has characters the font cannot draw: U+1F62D, U+1F525"


def test_the_preview_api_refuses_the_same_caption(sync_client, monkeypatch, font_dir):
    from routers import burn as burn_router

    monkeypatch.setattr(burn_router, "FONT_DIR", font_dir)
    payload = request().model_dump()
    payload["caption"] = LOVENIGHTDRIVES_1002
    response = sync_client.post("/api/burn/caption-render/v1", json=payload)
    assert response.status_code == 422
    assert response.json()["error"] == "CAPTION_GLYPH_MISSING"


def test_caption_empty_is_gone_because_no_input_can_reach_it():
    # The old CAPTION_EMPTY arm needed zero lines. A blank caption is refused at the request, a blank
    # explicit line still counts as one line, and an empty line_breaks list is refused at the style.
    with pytest.raises(ValidationError):
        CaptionRenderRequest.model_validate({**request().model_dump(by_alias=True), "caption": " \u00a0 "})
    with pytest.raises(ValidationError):
        request(line_breaks=[])
    # The deletion itself: no code path in the module can name the dead code any more.
    assert "CAPTION_EMPTY" not in inspect.getsource(caption_render)


# --- Format and zero-width characters: removed first, like the Worker's captionAdmission -------------------------------
# The Worker removes every Unicode Cf character (soft hyphen, U+200B-U+200D, U+2060, U+FEFF, bidi marks) before it
# checks glyphs, so it admits a caption that holds one. The Lab removes the same set before the glyph gate and before
# drawing, and refuses what is left only when the font has no glyph for it. U+FE0F and emoji are not Cf and stay refused.

@pytest.mark.parametrize("caption,drawn", [
    ("good\u00adcaption", "goodcaption"),
    ("a\u200db words", "ab words"),
    ("\ufeffhello there", "hello there"),
    ("zero\u200bwidth\u2060join", "zerowidthjoin"),
])
def test_format_characters_are_removed_and_the_rest_is_drawn(font_dir, caption, drawn):
    result = render_caption_overlay(caption_request(caption), font_dir=font_dir)
    assert " ".join(line.text for line in result.plan.lines) == drawn
    assert ink_box(result)


def test_format_characters_in_saved_line_breaks_are_removed_too(font_dir):
    result = render_caption_overlay(
        caption_request("good\u00adcap \u200b tion", line_breaks=["good\u00adcap", "\u200b", "tion"]), font_dir=font_dir)
    assert [line.text for line in result.plan.lines] == ["goodcap", "", "tion"]


@pytest.mark.parametrize("caption,only", [
    ("good\u00adcaption \U0001F62D", "U+1F62D"),
    ("a\u200db words \U0001F525", "U+1F525"),
    ("\ufeffhello there\uFE0F", "U+FE0F"),
    ("hello\u200b\u3164there", "U+3164"),
])
def test_removing_format_characters_never_hides_a_character_with_no_glyph(font_dir, caption, only):
    with pytest.raises(CaptionRenderError) as error:
        render_caption_overlay(caption_request(caption), font_dir=font_dir)
    assert error.value.code == "CAPTION_GLYPH_MISSING"
    assert error.value.message.endswith(": " + only)


def test_a_caption_of_only_format_characters_is_no_text(font_dir):
    with pytest.raises(CaptionRenderError) as error:
        render_caption_overlay(caption_request("\u00ad\u200d\u2060\ufeff"), font_dir=font_dir)
    assert error.value.code == "CAPTION_NO_TEXT"


# --- Round 3: ordering, saved lines, layout engines, every font ---------------------------------------------------------

@pytest.fixture(params=["basic", "raqm"])
def drawing_engine(request, monkeypatch):
    """Draw captions with the named Pillow layout engine (the glyph check reads the font's cmap and ignores it)."""
    if request.param == "raqm" and not features.check("raqm"):
        pytest.skip("this host's Pillow has no raqm; the production image may (ffmpeg pulls libass and fribidi)")
    engine = {"basic": ImageFont.Layout.BASIC, "raqm": ImageFont.Layout.RAQM}[request.param]
    monkeypatch.setattr(caption_render, "_load_font",
                        lambda path, size: ImageFont.truetype(str(path), size=size, layout_engine=engine))
    return request.param


@pytest.mark.parametrize("caption,code", [
    pytest.param("ok\ufe0f fine", "CAPTION_GLYPH_MISSING", id="variation-selector-inside-text"),
    pytest.param("\u3164", "CAPTION_GLYPH_MISSING", id="hangul-filler-only"),
    pytest.param("\uffa0", "CAPTION_GLYPH_MISSING", id="halfwidth-hangul-filler-only"),
    pytest.param("hello\u200b\u3164there", "CAPTION_GLYPH_MISSING", id="filler-between-words"),
    pytest.param("\u2800", "CAPTION_GLYPH_MISSING", id="braille-blank-only"),
    pytest.param("good\u00adcaption \u200d\U0001F62D", "CAPTION_GLYPH_MISSING", id="emoji-after-format-characters"),
])
def test_the_verdict_is_the_same_under_either_pillow_layout_engine(font_dir, drawing_engine, caption, code):
    # Raqm (HarfBuzz) can hide a missing default-ignorable character that BASIC layout draws as a box. Coverage is
    # read from the font's cmap table, so the layout engine does not change the verdict. (Where the host's Pillow
    # has no raqm, the RAQM runs are skipped.)
    with pytest.raises(CaptionRenderError) as error:
        render_caption_overlay(caption_request(caption), font_dir=font_dir)
    assert error.value.code == code


def test_glyph_coverage_is_read_from_the_font_table_not_from_rendered_pixels(monkeypatch):
    # A rendered probe depends on the layout engine; the cmap does not. Nothing may be drawn to decide coverage.
    def no_drawing(*args, **kwargs):
        raise AssertionError("the glyph check must not render anything")
    monkeypatch.setattr(ImageFont.FreeTypeFont, "getmask2", no_drawing)
    monkeypatch.setattr(ImageFont.FreeTypeFont, "getmask", no_drawing)
    caption_render._font_codepoints.cache_clear()
    covered = caption_render._font_codepoints(str(REAL_FONT_DIR / FONT_FILE))
    assert ord("a") in covered and 0x1F62D not in covered and 0xFE0F not in covered and 0 not in covered
    caption_render._require_drawable_text(REAL_FONT_DIR / FONT_FILE, ["plain words"])
    caption_render._font_codepoints.cache_clear()


@pytest.mark.parametrize("case", ["as_written", "lower", "upper", "title"])
@pytest.mark.parametrize("saved_lines", [False, True])
def test_the_gate_runs_on_the_cased_text_that_is_drawn(font_dir, monkeypatch, case, saved_lines):
    seen = []
    real = caption_render._require_drawable_text
    monkeypatch.setattr(caption_render, "_require_drawable_text", lambda path, lines: seen.append(list(lines)) or real(path, lines))
    style = {"case": case, "line_balance": 100}
    if saved_lines:
        style["line_breaks"] = ["mixed Case words", "seCond line"]
        render_caption_overlay(caption_request("mixed Case words seCond line", **style), font_dir=font_dir)
        expected = {"as_written": ["mixed Case words", "seCond line"], "lower": ["mixed case words", "second line"],
                    "upper": ["MIXED CASE WORDS", "SECOND LINE"], "title": ["Mixed Case Words", "SeCond Line"]}[case]
    else:
        render_caption_overlay(caption_request("mixed Case words", **style), font_dir=font_dir)
        expected = {"as_written": "mixed Case words", "lower": "mixed case words",
                    "upper": "MIXED CASE WORDS", "title": "Mixed Case Words"}[case]
    assert len(seen) == 1
    got = seen[0] if saved_lines else [" ".join(seen[0])]
    assert got == (expected if saved_lines else [expected])


def test_saved_line_breaks_are_normalised_exactly_like_the_caption(font_dir):
    # The wrap path rebuilds lines from single-space words (tab, NBSP, U+3000, runs of spaces become one space)
    # and the glyph gate skips space-class characters, so a saved line has to get the same treatment or a
    # space-class character the font lacks would be drawn as a box unchecked.
    messy = ["one\u3000 two", "\tthree\u2003\u2003four \u200b", "", "five\u00a0six\u00ad"]
    caption = " ".join(messy)
    result = render_caption_overlay(caption_request(caption, line_breaks=messy), font_dir=font_dir)
    assert [line.text for line in result.plan.lines] == ["one two", "three four", "", "five six"]
    wrapped = render_caption_overlay(caption_request(caption, line_balance=0), font_dir=font_dir)
    assert " ".join(line.text for line in wrapped.plan.lines) == "one two three four five six"
    # A saved line with a real missing glyph is still refused, spaces or not.
    with pytest.raises(CaptionRenderError) as error:
        render_caption_overlay(caption_request("one\u3000\u3164 two", line_breaks=["one\u3000\u3164", "two"]), font_dir=font_dir)
    assert error.value.code == "CAPTION_GLYPH_MISSING"


@pytest.mark.parametrize("font", REAL_FONTS)
@pytest.mark.parametrize("char", ["\u3164", "\uffa0", "\u2800", "\ufe0f", "\u3042", "\U0001F62D"],
                         ids=["hangul-filler", "halfwidth-filler", "braille-blank", "fe0f", "kana", "emoji"])
def test_no_installed_font_has_these_characters_and_every_font_has_plain_text(font, char):
    # The comment in caption_render says these are missing from every TikTokSans file; this is its proof, per font,
    # for non-emoji characters as well.
    path = str(REAL_FONT_DIR / font)
    covered = caption_render._font_codepoints(path)
    assert ord(char) not in covered
    assert ord("a") in covered
    with pytest.raises(CaptionRenderError) as error:
        render_caption_overlay(caption_request("hello " + char + " there", font=font), font_dir=REAL_FONT_DIR)
    assert error.value.code == "CAPTION_GLYPH_MISSING"


# --- Round 3: NFC first, then Cf; the shared Worker vectors; the cmap against the Worker's table -------------------

VECTOR_DIR = Path(__file__).parent / "fixtures" / "caption_admission"
# sha256 of the two files exactly as committed on ecfromthedc/rotational-posting-agent, branch tr03p5-capvisible at
# 6fe81f9c (vectors, control-plane-worker/test/fixtures/caption-admission-vectors.json; unchanged since) and
# 8e171c9a7167e454963eb914d1e11ceaed8e88cf (the table, control-plane-worker/data/drawable-codepoints.json, whose
# header embeds the Lab font shas and ranges_sha256). The rail copies the vectors byte for byte too.
VECTORS_SHA256 = "92b416b26990d5d8e4ec647044c33a76c9e256c1976b6862748b24a702fcf929"
DRAWABLE_TABLE_SHA256 = "104cc07c5b9cafa09e14063f44da46411c35193da06ed00ac4eeb7733a671beb"


def _vectors():
    raw = (VECTOR_DIR / "caption-admission-vectors.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == VECTORS_SHA256, "vectors file changed: re-pin it from the Worker branch"
    return json.loads(raw)["vectors"]


def lab_verdict(text, font=FONT_FILE):
    """None when the Lab admits and draws the text, else the refusal code (REQUEST for a request-level refusal)."""
    try:
        render_caption_overlay(caption_request(text, font=font, size_pt=24, line_balance=100), font_dir=REAL_FONT_DIR)
    except CaptionRenderError as error:
        return error.code
    except (ValidationError, UnicodeError):
        return "REQUEST"
    return None


def test_the_vector_files_are_the_pinned_ones():
    assert hashlib.sha256((VECTOR_DIR / "caption-admission-vectors.json").read_bytes()).hexdigest() == VECTORS_SHA256
    assert hashlib.sha256((VECTOR_DIR / "drawable-codepoints.json").read_bytes()).hexdigest() == DRAWABLE_TABLE_SHA256
    assert len(_vectors()) >= 80


def test_every_vector_the_worker_admits_the_lab_admits_and_draws():
    # Canonical order (Worker and rail): NFC, strip Cf, every other non-whitespace character drawable, a 2+ word.
    # The Lab only needs a drawable letter or digit, so it is laxer on the word rule; it must never refuse what the
    # Worker admits, or the Worker would learn a good caption as refused for that look.
    admitted = [vector for vector in _vectors() if vector["admit"]]
    assert len(admitted) >= 30
    for vector in admitted:
        assert lab_verdict(vector["text"]) is None, (vector["text"], vector["why"])


def test_every_vector_the_worker_refuses_for_a_glyph_the_lab_refuses_too():
    # emoji and character refusals are about the same font table, so the Lab agrees. visible_word refusals are the
    # Worker's stricter word rule: the Lab's verdict is recorded in the report but not required to match.
    for vector in _vectors():
        if vector["rule"] in ("emoji", "character"):
            assert lab_verdict(vector["text"]) is not None, (vector["text"], vector["why"])


@pytest.mark.parametrize("font", REAL_FONTS)
def test_every_installed_font_gives_the_same_verdict_on_the_vectors(font):
    for vector in _vectors():
        if vector["admit"]:
            assert lab_verdict(vector["text"], font) is None, (font, vector["text"])
        elif vector["rule"] in ("emoji", "character"):
            assert lab_verdict(vector["text"], font) is not None, (font, vector["text"])


def _ranges_of(codes):
    ranges, first, last = [], None, None
    for code in sorted(codes):
        if first is None:
            first = last = code
        elif code == last + 1:
            last = code
        else:
            ranges.append([first, last])
            first = last = code
    ranges.append([first, last])
    return ranges


def test_the_labs_fonts_are_exactly_the_ones_the_workers_drawable_table_was_made_from():
    # Font provenance tripwire; it never skips. The Worker admits a character only if it is in this table, and the
    # table is the intersection of the cmaps of the Lab's non-italic TikTokSans fonts. Adding, removing or changing
    # any font file in fonts/ changes what the Lab can draw, so this test goes red until the table is regenerated
    # and re-pinned from the Worker branch.
    table = json.loads((VECTOR_DIR / "drawable-codepoints.json").read_bytes())
    listed = {entry["file"]: entry for entry in table["fonts"]}
    assert len(listed) == len(table["fonts"]) == 23
    # 1. The same set of files: none added, none removed.
    assert sorted(listed) == REAL_FONTS, "fonts/ no longer matches the fonts the drawable table was built from"
    # 2. Each file is byte-for-byte the one in the table's header: sha256 and git blob sha.
    for name, entry in listed.items():
        data = (REAL_FONT_DIR / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], name
        assert hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() == entry["git_blob_sha"], name
    # 3. Re-derive the table body with the Lab's own cmap reader: per font, then the intersection, then its ranges.
    per_font = {}
    for name in REAL_FONTS:
        per_font[name] = caption_render._font_codepoints(str(REAL_FONT_DIR / name))
        assert len(per_font[name]) == listed[name]["codepoints"], name
    intersection = frozenset.intersection(*per_font.values())
    assert frozenset.union(*per_font.values()) == intersection
    assert _ranges_of(intersection) == table["ranges"]
    assert len(intersection) == table["intersection_count"] == table["union_count"] == 910
    # 4. The body hash in the header is the hash of those ranges (compact JSON), so header and body cannot drift.
    derived = hashlib.sha256(json.dumps(_ranges_of(intersection), separators=(",", ":")).encode()).hexdigest()
    assert derived == table["ranges_sha256"] == "bdf7498ffadd18531264e8497edda27ab90b1079634f5a339b6633fae8fe1c43"


def test_nfc_comes_before_the_format_strip(font_dir):
    # A decomposed e + acute is the drawn \u00e9 once composed; it must not be probed as two characters.
    result = render_caption_overlay(caption_request("cafe\u0301 vibes"), font_dir=font_dir)
    assert " ".join(line.text for line in result.plan.lines) == "caf\u00e9 vibes"
    # k + caron composes to U+01E9, which the font lacks: refused as the composed character, not hidden.
    with pytest.raises(CaptionRenderError) as error:
        render_caption_overlay(caption_request("ok k\u030c"), font_dir=font_dir)
    assert error.value.code == "CAPTION_GLYPH_MISSING" and error.value.message.endswith(": U+01E9")
    # Saved lines are composed too.
    saved = render_caption_overlay(caption_request("cafe\u0301 vibes", line_breaks=["cafe\u0301", "vibes"]), font_dir=font_dir)
    assert [line.text for line in saved.plan.lines] == ["caf\u00e9", "vibes"]


# --- Round 6: the saved-lines check uses the same canonical rule as the drawn caption ---------------------------------

@pytest.mark.parametrize("caption,lines,drawn", [
    ("good\u00adcaption here", ["goodcaption", "here"], "goodcaption here"),
    ("goodcaption here", ["good\u00adcaption", "here"], "goodcaption here"),
    ("a\u200db words", ["ab", "words"], "ab words"),
    ("\ufeffhello there", ["hello", "there"], "hello there"),
    ("hello there", ["hel\u2060lo", "\u200bthere"], "hello there"),
    ("cafe\u0301 vibes", ["caf\u00e9", "vibes"], "caf\u00e9 vibes"),
    ("caf\u00e9 vibes", ["cafe\u0301", "vibes"], "caf\u00e9 vibes"),
    ("Good\u00ad Caption", ["good", "CAPTION"], "good CAPTION"),
    ("one  two\u3000three", ["one\u200b  two", "\u3000three"], "one two three"),
    ("red car", ["red \u200b", "car"], "red car"),
], ids=["caption-has-soft-hyphen", "lines-have-soft-hyphen", "zwj", "bom", "format-chars-in-lines", "nfc-caption-decomposed",
        "nfc-lines-decomposed", "case-and-format", "whitespace-and-format", "format-char-between-spaces"])
def test_saved_lines_that_differ_only_by_format_characters_or_nfc_are_accepted(font_dir, caption, lines, drawn):
    # The Worker compares saved lines with the caption after NFC, stripping Cf, collapsing whitespace and
    # lowercasing; a Lab that did not would 422 a post the Worker admits. Accepted at the request, and the DRAWN
    # words are exactly the saved lines' words, without the format characters.
    result = render_caption_overlay(caption_request(caption, line_breaks=lines), font_dir=font_dir)
    assert " ".join(line.text for line in result.plan.lines) == drawn
    assert len(result.plan.lines) == len(lines)


@pytest.mark.parametrize("caption,lines", [
    ("good\u00adcaption here", ["good", "caption", "here"]),
    ("goodcaption here", ["good\u00ad", "caption here"]),
    ("hello there", ["hello", "there now"]),
    ("hello there", ["hello"]),
    ("hello there", ["hello", "\U0001F62D there"]),
], ids=["format-char-split-a-word", "format-char-line-split-a-word", "extra-words", "missing-words", "emoji-line"])
def test_saved_lines_that_really_differ_from_the_caption_are_still_refused(caption, lines):
    with pytest.raises(ValidationError):
        caption_request(caption, line_breaks=lines)


# --- Round 8: the Worker's shared line-spelling vectors ------------------------------------------------------------------
# control-plane-worker/test/fixtures/line-spelling-vectors.json from ecfromthedc/rotational-posting-agent, branch
# tr03p5-lostpost, commit 38e4271c271edde0701151ab8ea26704df122c3c (25 cases), copied byte for byte. Does a list of saved line breaks spell the caption? Canonical
# order: NFC, strip Cf, collapse whitespace (Python split set), lowercase; lines joined with one space.
LINE_SPELLING_SHA256 = "35ba51d206c245b94c66d4c1fa358e7301b84b87277876cb4c73d43b088f2e7b"


def _line_spelling_vectors():
    raw = (VECTOR_DIR / "line-spelling-vectors.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == LINE_SPELLING_SHA256, "vectors file changed: re-pin it from the Worker branch"
    return json.loads(raw)["vectors"]


def _saved_lines_verdict(caption, lines):
    """True when the saved-lines check accepts these lines for this caption; False when it says they do not spell it."""
    payload = request().model_dump(by_alias=True)
    payload["caption"] = caption
    payload["style"]["line_breaks"] = lines
    try:
        CaptionRenderRequest.model_validate(payload)
    except ValidationError as error:
        assert "line_breaks must preserve the caption text" in str(error), (caption, lines, str(error))
        return False
    return True


def test_the_line_spelling_vectors_are_the_pinned_ones():
    vectors = _line_spelling_vectors()
    assert len(vectors) == 25
    assert any(vector["accept"] for vector in vectors) and any(not vector["accept"] for vector in vectors)


def test_every_line_spelling_vector_gets_the_same_answer_as_the_worker():
    disagreements = []
    for vector in _line_spelling_vectors():
        got = _saved_lines_verdict(vector["caption"], vector["lines"])
        if got != vector["accept"]:
            disagreements.append((vector["name"], "worker accept=%s" % vector["accept"], "lab accept=%s" % got))
    assert not disagreements, disagreements


def test_every_accepted_line_spelling_vector_renders_and_draws_the_lines():
    for vector in _line_spelling_vectors():
        if vector["accept"]:
            result = render_caption_overlay(
                caption_request(vector["caption"], line_breaks=vector["lines"]), font_dir=REAL_FONT_DIR)
            assert len(result.plan.lines) == len(vector["lines"]), vector["name"]
            assert not any(unicodedata.category(char) == "Cf" for line in result.plan.lines for char in line.text)


def test_lowercase_not_casefold_and_the_whitespace_step_comes_after_the_format_strip():
    # Two points of the canonical rule the shared vectors do not exercise, pinned here: the comparison lowercases
    # (it does not casefold, so "Stra\u00dfe" and "STRASSE" are different spellings), and whitespace is collapsed
    # AFTER the Cf strip, so a format character between two spaces leaves one space, not two.
    assert _saved_lines_verdict("Stra\u00dfe", ["STRA\u00dfE"]) is True
    assert _saved_lines_verdict("Stra\u00dfe", ["STRASSE"]) is False
    assert _saved_lines_verdict("red car", ["red \u200b", "car"]) is True
