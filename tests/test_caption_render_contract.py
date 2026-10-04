import base64
import hashlib
import io
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

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
def test_v2_port_8002_route_matches_hosted_frame_fit(
    sync_client, monkeypatch, font_dir, tmp_path, frame, line_count
):
    from fastapi.testclient import TestClient

    from routers import burn as burn_router

    (tmp_path / "static" / "burn").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    import burn_server

    monkeypatch.setattr(burn_router, "FONT_DIR", font_dir)
    monkeypatch.setattr(burn_server, "FONT_DIR", font_dir)
    payload = lines_request(SHORT_LINES[:line_count], size_pt=96)
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
