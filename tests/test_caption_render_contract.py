import base64
import hashlib
import io
import shutil
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from services.caption_render import (
    CaptionRenderError,
    CaptionRenderRequest,
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
    assert result["plan"]["stroke_width_px"] == 10
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


def test_caption_that_would_clip_fails_instead_of_silently_resizing(font_dir):
    payload = request(size_pt=96, line_balance=0)
    payload = payload.model_copy(
        update={"caption": "this caption is deliberately much too wide for a single line"}
    )
    with pytest.raises(CaptionRenderError) as error:
        render_caption_overlay(payload, font_dir=font_dir)
    assert error.value.code == "CAPTION_LINE_TOO_WIDE"


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
