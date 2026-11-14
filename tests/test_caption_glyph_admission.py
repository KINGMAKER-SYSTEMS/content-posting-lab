"""A caption is either drawn intact by its selected font or refused."""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from services.caption_render import CaptionRenderError, CaptionRenderRequest, render_caption_overlay


BASE_SHA = "58c909e8"
FONT_FILE = "TikTokSans16pt-Bold.ttf"
REPO_ROOT = Path(__file__).parents[1]
REAL_CAPTIONS = json.loads(
    (REPO_ROOT / "tests/fixtures/real/chrome_capitalize_real_captions.json").read_text(encoding="utf-8")
)["cases"]
EMOJI_CAPTIONS = {
    "Btw, you're the only girl I've ever felt this comfortable around. 💖",
    "If you asked me how many times you came into my mind, I would say once, because you came and never left. 💕",
    "it still feels unreal that i get to dream like this with you 🥺❤️",
}
ADVERTISED_FONTS = [
    path.name for path in sorted((REPO_ROOT / "fonts").glob("TikTokSans*.ttf"))
    if "Italic" not in path.name
]


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    """Compare ordinary output with the renderer before emoji handling was introduced."""
    source = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{BASE_SHA}:services/caption_render.py"],
        check=True, capture_output=True, text=True,
    ).stdout
    path = tmp_path_factory.mktemp("base") / "base_caption_render.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("base_caption_render", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["base_caption_render"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("base_caption_render", None)


@pytest.fixture
def font_dir(tmp_path):
    target = tmp_path / "fonts"
    target.mkdir()
    shutil.copy2(REPO_ROOT / "fonts" / FONT_FILE, target / FONT_FILE)
    return target


def wire(caption, *, line_breaks=None):
    style = {
        "font": FONT_FILE,
        "size_pt": 24,
        "color": "#FFFFFF",
        "outline": "#000000",
        "position": "middle",
        "align": "center",
        "case": "as_written",
        "background": "none",
        "offset_pct": 0,
        "line_balance": 0,
    }
    if line_breaks is not None:
        style["line_breaks"] = line_breaks
    return {"schema": "content-lab.caption-render-request.v1", "caption": caption, "style": style}


def render(caption, font_dir, *, line_breaks=None):
    request = CaptionRenderRequest.model_validate(wire(caption, line_breaks=line_breaks))
    return render_caption_overlay(request, font_dir=font_dir)


@pytest.mark.parametrize("caption,codepoint", [
    ("go 🚀 now", "U+1F680"),
    ("🚀", "U+1F680"),
    ("hi 💖 there", "U+1F496"),
    ("caption\n😭😭", "U+1F62D"),
    ("hi ❤️", "U+2764"),
    ("hi ️ there", "U+FE0F"),
])
def test_missing_glyph_refuses_the_entire_caption(font_dir, caption, codepoint):
    with pytest.raises(CaptionRenderError) as error:
        render(caption, font_dir)
    assert error.value.code == "CAPTION_GLYPH_MISSING"
    assert codepoint in error.value.message


def test_saved_line_with_missing_glyph_is_refused(font_dir):
    with pytest.raises(CaptionRenderError) as error:
        render("hi 💖 there", font_dir, line_breaks=["hi 💖", "there"])
    assert error.value.code == "CAPTION_GLYPH_MISSING"


@pytest.mark.parametrize("font", ADVERTISED_FONTS)
def test_every_advertised_font_refuses_missing_emoji(font):
    payload = wire("go 🚀 now")
    payload["style"]["font"] = font
    request = CaptionRenderRequest.model_validate(payload)
    with pytest.raises(CaptionRenderError) as error:
        render_caption_overlay(request, font_dir=REPO_ROOT / "fonts")
    assert error.value.code == "CAPTION_GLYPH_MISSING"


@pytest.mark.parametrize("caption,code", [
    ("?!", "CAPTION_NO_TEXT"),
    ("...", "CAPTION_NO_TEXT"),
    ("- - -", "CAPTION_NO_TEXT"),
    # A format character draws nothing and is stripped first (the Worker/rail rule), leaving no text.
    ("\u200c", "CAPTION_NO_TEXT"),
])
def test_caption_without_readable_text_is_refused(font_dir, caption, code):
    with pytest.raises(CaptionRenderError) as error:
        render(caption, font_dir)
    assert error.value.code == code


@pytest.mark.parametrize("caption", [
    "one short line",
    '"Just leave them"\nNo because at this point\nthe occasional pain is\nworth not being alone',
    '"i can fix him" no i can\'t. i\'m a leaver, a quitter, and a walk awayer.',
    "hello\tworld",
    "a b",
])
def test_ordinary_captions_keep_the_exact_prior_bytes(base, font_dir, caption):
    old = base.render_caption_overlay(
        base.CaptionRenderRequest.model_validate(wire(caption)), font_dir=font_dir
    )
    new = render(caption, font_dir)
    assert new.model_dump(mode="json", by_alias=True, exclude_none=True) == old.model_dump(
        mode="json", by_alias=True, exclude_none=True
    )


def test_real_caption_pool_keeps_ordinary_bytes_and_refuses_emoji(base, font_dir):
    compared = 0
    refused = set()
    for caption, _expected in REAL_CAPTIONS:
        if caption in EMOJI_CAPTIONS:
            with pytest.raises(CaptionRenderError) as error:
                render(caption, font_dir)
            assert error.value.code == "CAPTION_GLYPH_MISSING"
            refused.add(caption)
            continue
        try:
            old = base.render_caption_overlay(
                base.CaptionRenderRequest.model_validate(wire(caption)), font_dir=font_dir
            )
        except base.CaptionRenderError as error:
            with pytest.raises(CaptionRenderError) as new_error:
                render(caption, font_dir)
            assert new_error.value.code == error.code
            continue
        new = render(caption, font_dir)
        assert new.model_dump(mode="json", by_alias=True, exclude_none=True) == old.model_dump(
            mode="json", by_alias=True, exclude_none=True
        ), caption
        compared += 1
    assert compared >= 400
    assert refused == EMOJI_CAPTIONS


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_hosted_burn_returns_closed_glyph_error(sync_client, monkeypatch, font_dir, version):
    from routers import burn as burn_router

    monkeypatch.setattr(burn_router, "FONT_DIR", font_dir)
    body = wire("go 🚀 now")
    if version == "v2":
        body["schema"] = "content-lab.caption-render-request.v2"
        body["picture_frame"] = "9:16"
    response = sync_client.post(f"/api/burn/caption-render/{version}", json=body)
    assert response.status_code == 422
    assert response.json()["error"] == "CAPTION_GLYPH_MISSING"


def test_port_8002_burn_returns_the_same_glyph_error(monkeypatch, font_dir, tmp_path):
    from fastapi.testclient import TestClient

    (tmp_path / "static" / "burn").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    import burn_server

    monkeypatch.setattr(burn_server, "FONT_DIR", font_dir)
    response = TestClient(burn_server.app).post("/api/burn/caption-render/v1", json=wire("go 🚀 now"))
    assert response.status_code == 422
    assert response.json()["error"] == "CAPTION_GLYPH_MISSING"
