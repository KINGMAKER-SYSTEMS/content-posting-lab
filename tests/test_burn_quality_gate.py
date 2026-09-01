"""Quality gate for Content Lab burn server — the three 2026-08-05 proven cases.

1. Long + female-voiced caption fails (52 words / "to my man" class)
2. Oversized overlay fails (≈80% wide, ≈59% tall — v1 disaster)
3. contentlab-v4 overlay + short neutral caption passes
"""

from __future__ import annotations

import base64
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

import burn_server
from burn_quality_gate import run_quality_check


def _png_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def v4_overlay() -> str:
    """Approved burned_003-shaped ink: ~55% column, vertically mid-upper."""
    img = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # 55% of 1080 ≈ 594px wide, centered; y around 47% with modest height
    left = int(1080 * 0.225)
    right = int(1080 * 0.775)
    top = int(1920 * 0.40)
    bottom = int(1920 * 0.54)
    draw.rectangle([left, top, right, bottom], fill=(255, 255, 255, 255))
    return _png_b64(img)


def disaster_overlay() -> str:
    """v1 disaster: ~80% wide, ~59% tall."""
    img = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    left = int(1080 * 0.10)
    right = int(1080 * 0.90)
    top = int(1920 * 0.20)
    bottom = int(1920 * 0.79)
    draw.rectangle([left, top, right, bottom], fill=(255, 255, 255, 255))
    return _png_b64(img)


def typed_top_left_overlay() -> str:
    """Ink aligned to the typed top/left CaptionStyle anchors."""
    img = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([108, 240, 430, 350], fill=(255, 255, 255, 255))
    return _png_b64(img)


LONG_VOICE_CAPTION = (
    "to my man who always knows exactly what to say when the night gets quiet "
    "and the road feels longer than it should and every mile reminds me that "
    "a girl who waited through every storm still believes we make it home "
    "together no matter how far we drive tonight under these lights"
)


def test_long_voice_caption_fails():
    assert len(LONG_VOICE_CAPTION.split()) >= 52
    result = run_quality_check(
        LONG_VOICE_CAPTION, persona="male", overlay_png=v4_overlay()
    )
    assert result["ok"] is False
    joined = " ".join(result["reasons"])
    assert "caption_too_long" in joined
    assert "persona_voice" in joined


def test_oversized_overlay_fails():
    result = run_quality_check(
        "good at leaving", persona="male", overlay_png=disaster_overlay()
    )
    assert result["ok"] is False
    joined = " ".join(result["reasons"])
    assert "width:" in joined
    assert "height:" in joined


def test_v4_overlay_passes():
    result = run_quality_check(
        "good at leaving", persona="male", overlay_png=v4_overlay()
    )
    assert result["ok"] is True
    assert result["reasons"] == []


def test_typed_style_uses_its_declared_alignment_and_position():
    style = {
        "font": "TikTokSans16pt-Bold.ttf",
        "size_pt": 32.0,
        "color": "#ffffff",
        "outline": "#000000",
        "position": "top",
        "align": "left",
        "case": "as_written",
        "background": "none",
        "offset_pct": 0.0,
        "line_balance": 0,
    }
    result = run_quality_check(
        "top placement",
        persona="male",
        overlay_png=typed_top_left_overlay(),
        caption_style=style,
    )
    assert result["ok"] is True
    assert result["reasons"] == []

    mismatch = run_quality_check(
        "top placement",
        persona="male",
        overlay_png=typed_top_left_overlay(),
        caption_style={**style, "position": "bottom"},
    )
    assert mismatch["ok"] is False
    assert any("typed_position" in reason for reason in mismatch["reasons"])


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(burn_server, "VIDEO_DIR", tmp_path / "video")
    monkeypatch.setattr(burn_server, "CAPTION_DIR", tmp_path / "captions")
    monkeypatch.setattr(burn_server, "BURN_DIR", tmp_path / "burn")
    (tmp_path / "video").mkdir()
    (tmp_path / "captions").mkdir()
    (tmp_path / "burn").mkdir()
    return TestClient(burn_server.app)


def test_health_ok(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_quality_check_endpoint_rejects_long_voice(client):
    res = client.post(
        "/api/quality-check",
        json={
            "caption": LONG_VOICE_CAPTION,
            "persona": "male",
            "overlayPng": v4_overlay(),
        },
    )
    assert res.status_code == 422
    body = res.json()
    assert body["ok"] is False
    assert any("caption_too_long" in r for r in body["reasons"])


def test_quality_check_endpoint_rejects_disaster_overlay(client):
    res = client.post(
        "/api/quality-check",
        json={
            "caption": "good at leaving",
            "persona": "male",
            "overlayPng": disaster_overlay(),
        },
    )
    assert res.status_code == 422
    assert res.json()["ok"] is False


def test_quality_check_endpoint_passes_v4(client):
    res = client.post(
        "/api/quality-check",
        json={
            "caption": "good at leaving",
            "persona": "male",
            "overlayPng": v4_overlay(),
        },
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True


def test_quality_check_never_honours_force(client):
    """Standalone route must not accept force — rail builds cannot bypass."""
    res = client.post(
        "/api/quality-check",
        json={
            "caption": LONG_VOICE_CAPTION,
            "persona": "male",
            "overlayPng": v4_overlay(),
            "force": True,
        },
    )
    assert res.status_code == 422
    assert res.json()["ok"] is False

