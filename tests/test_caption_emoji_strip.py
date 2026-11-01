"""Emoji in burned captions must never draw a .notdef box, and must not change the wire.

TikTok Sans has no emoji cmap entries, so the old renderer drew every emoji as
the font's missing-glyph box. ``render_caption_overlay`` now drops characters
the resolved font cannot draw before measuring. The plan and result keys are
exactly the base keys (the Rail bridge rejects any extra key), and every
"unchanged" claim is checked against the BASE renderer, loaded at test time
from ``git show 58c909e8:services/caption_render.py`` (not stored hashes).
"""

import importlib.util
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from services.caption_render import (
    CaptionRenderError,
    CaptionRenderRequest,
    render_caption_overlay,
)

BASE_SHA = "58c909e8"
FONT_FILE = "TikTokSans16pt-Bold.ttf"
REPO_ROOT = Path(__file__).parents[1]
REAL_CAPTIONS = json.loads(
    (REPO_ROOT / "tests" / "fixtures" / "real" / "chrome_capitalize_real_captions.json").read_text(
        encoding="utf-8"
    )
)["cases"]

EMOJI_CAPTIONS = {
    "Btw, you're the only girl I've ever felt this comfortable around. \U0001f496": [0x1F496],
    "If you asked me how many times you came into my mind, I would say once, because you came and never left. \U0001f495": [0x1F495],
    "it still feels unreal that i get to dream like this with you \U0001f97a❤️": [0x2764, 0xFE0F, 0x1F97A],
}

# Literal key sets of the base wire (read from `git show 58c909e8:services/caption_render.py`:
# CaptionRenderResult[V2], CaptionRenderPlan[V2], CaptionRenderOverlay). fitted_font_size_px
# appears only for a shrunk caption, which neither test caption is.
BASE_RESULT_KEYS = {
    "schema", "renderer", "caption_sha256", "style_sha256",
    "render_plan_sha256", "plan", "overlay",
}
BASE_PLAN_KEYS_V1 = {
    "renderer", "canvas", "effective_style", "font_sha256", "font_size_px",
    "stroke_width_px", "line_height_px", "rendered_text", "lines",
}
BASE_PLAN_KEYS_V2 = BASE_PLAN_KEYS_V1 | {"picture_frame"}
BASE_OVERLAY_KEYS = {"media_type", "width", "height", "sha256", "base64"}


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    """The base renderer (58c909e8), loaded from git into a throwaway module."""
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


def wire(caption, size_pt=24, line_breaks=None, line_balance=0):
    style = {
        "font": FONT_FILE,
        "size_pt": size_pt,
        "color": "#FFFFFF",
        "outline": "#000000",
        "position": "middle",
        "align": "center",
        "case": "as_written",
        "background": "none",
        "offset_pct": 0,
        "line_balance": line_balance,
    }
    if line_breaks is not None:
        style["line_breaks"] = line_breaks
    return {
        "schema": "content-lab.caption-render-request.v1",
        "caption": caption,
        "style": style,
    }


def request(caption, **kwargs):
    return CaptionRenderRequest.model_validate(wire(caption, **kwargs))


def dump(result):
    return result.model_dump(mode="json", by_alias=True, exclude_none=True)


def head_and_base(base, font_dir, caption, **kwargs):
    head = render_caption_overlay(request(caption, **kwargs), font_dir=font_dir)
    base_result = base.render_caption_overlay(
        base.CaptionRenderRequest.model_validate(wire(caption, **kwargs)), font_dir=font_dir
    )
    return head, base_result


def test_emoji_is_dropped_from_the_drawn_lines(font_dir):
    result = render_caption_overlay(request("the problem \U0001f497"), font_dir=font_dir)
    assert result.plan.rendered_text == "the problem"
    assert [line.text for line in result.plan.lines] == ["the problem"]


def test_dropped_codepoints_are_logged_not_sent(font_dir, caplog):
    with caplog.at_level(logging.INFO, logger="services.caption_render"):
        result = render_caption_overlay(request("the problem \U0001f497"), font_dir=font_dir)
    assert any("U+1F497" in record.getMessage() and record.levelno == logging.INFO
               for record in caplog.records)
    assert "dropped_glyphs" not in json.dumps(dump(result))


def test_doubled_spaces_and_line_ends_are_cleaned_after_a_drop(font_dir):
    result = render_caption_overlay(request("hello  \U0001f497  world"), font_dir=font_dir)
    assert result.plan.rendered_text == "hello world"
    result = render_caption_overlay(request("\U0001f497 hello"), font_dir=font_dir)
    assert result.plan.rendered_text == "hello"


def test_emoji_captions_drop_exact_codepoints(font_dir):
    from PIL import ImageFont

    from services.caption_render import _strip_unsupported_lines

    font = ImageFont.truetype(str(REPO_ROOT / "fonts" / FONT_FILE), 48)
    for caption, expected in EMOJI_CAPTIONS.items():
        _stripped, dropped = _strip_unsupported_lines(font, caption.split("\n"))
        assert dropped == expected, caption


@pytest.mark.parametrize("caption", [
    "one short line",
    '"Just leave them"\nNo because at this point\nthe occasional pain is\nworth not being alone',
    '"i can fix him" no i can\'t. i\'m a leaver, a quitter, and a walk awayer.',
])
def test_non_emoji_captions_equal_the_base_renderer(base, font_dir, caption):
    head, base_result = head_and_base(base, font_dir, caption)
    assert dump(head) == dump(base_result)


def test_non_emoji_real_caption_pool_equals_the_base_renderer(base, font_dir):
    compared = 0
    for caption, _expected in REAL_CAPTIONS:
        if caption in EMOJI_CAPTIONS:
            continue
        try:
            base_result = base.render_caption_overlay(
                base.CaptionRenderRequest.model_validate(wire(caption)), font_dir=font_dir
            )
        except base.CaptionRenderError as error:
            # Some real captions do not fit at 24 pt: head must refuse them the same way.
            with pytest.raises(CaptionRenderError) as head_error:
                render_caption_overlay(request(caption), font_dir=font_dir)
            assert head_error.value.code == error.code, caption
            continue
        head = render_caption_overlay(request(caption), font_dir=font_dir)
        assert dump(head) == dump(base_result), caption
        compared += 1
    assert compared >= 400


@pytest.mark.parametrize("caption", ["hello\tworld", "a b", "x　y", "a b c"])
def test_whitespace_is_never_treated_as_missing(base, font_dir, caption):
    head, base_result = head_and_base(base, font_dir, caption)
    assert head.plan.rendered_text == base_result.plan.rendered_text
    assert dump(head) == dump(base_result)
    assert head.overlay.sha256 == base_result.overlay.sha256


def test_emoji_only_caption_renders_exactly_like_the_base(base, font_dir, caplog):
    with caplog.at_level(logging.WARNING, logger="services.caption_render"):
        head, base_result = head_and_base(base, font_dir, "\U0001f497")
    assert dump(head) == dump(base_result)
    assert head.overlay.base64 == base_result.overlay.base64
    assert head.render_plan_sha256 == base_result.render_plan_sha256
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


# Choice for a line that is only emoji: it is DROPPED, not kept as a blank row.
# A kept blank row would shift the block up by half a line and stretch a box
# background over an empty row, for text nobody can see. Lines that are blank in
# the caption as written are not touched (they are not dropped by the strip).
def test_explicit_newline_that_is_only_emoji_is_dropped(font_dir):
    plain = render_caption_overlay(request("caption"), font_dir=font_dir)
    result = render_caption_overlay(request("caption\n\U0001f62d\U0001f62d"), font_dir=font_dir)
    assert [line.text for line in result.plan.lines] == ["caption"]
    assert result.plan.rendered_text == "caption"
    assert result.overlay.sha256 == plain.overlay.sha256


def test_exact_line_breaks_path_drops_emoji_and_emoji_only_lines(font_dir):
    plain = render_caption_overlay(request("text", line_breaks=["text"]), font_dir=font_dir)
    result = render_caption_overlay(
        request("text \U0001f97a", line_breaks=["text", "\U0001f97a"]), font_dir=font_dir
    )
    assert [line.text for line in result.plan.lines] == ["text"]
    assert result.overlay.sha256 == plain.overlay.sha256
    mixed = render_caption_overlay(
        request("hi \U0001f496 there", line_breaks=["hi \U0001f496", "there"]), font_dir=font_dir
    )
    assert [line.text for line in mixed.plan.lines] == ["hi", "there"]


def post(sync_client, monkeypatch, font_dir, version, caption):
    from routers import burn as burn_router

    monkeypatch.setattr(burn_router, "FONT_DIR", font_dir)
    body = wire(caption)
    if version == "v2":
        body["schema"] = "content-lab.caption-render-request.v2"
        body["picture_frame"] = "9:16"
    response = sync_client.post(f"/api/burn/caption-render/{version}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize("caption", ["one short line", "the problem \U0001f497"])
def test_endpoint_wire_keys_equal_the_base_key_set(
    sync_client, monkeypatch, font_dir, version, caption
):
    body = post(sync_client, monkeypatch, font_dir, version, caption)
    assert set(body) == BASE_RESULT_KEYS
    assert set(body["plan"]) == (BASE_PLAN_KEYS_V2 if version == "v2" else BASE_PLAN_KEYS_V1)
    assert set(body["overlay"]) == BASE_OVERLAY_KEYS


def test_no_drawn_glyph_is_missing_across_real_caption_pool(font_dir):
    # Independent oracle: render the pool through the renderer and require every
    # drawn character to be in the font's cmap (read by fontTools-free Pillow
    # getmask comparison against .notdef). A disabled strip leaves emoji in the
    # lines, so this fails.
    from PIL import ImageFont

    from services.caption_render import _NOTDEF_PROBE

    font = ImageFont.truetype(str(REPO_ROOT / "fonts" / FONT_FILE), 48)
    notdef_mask = bytes(font.getmask(_NOTDEF_PROBE))
    exercised = set()
    for caption, _expected in REAL_CAPTIONS:
        try:
            result = render_caption_overlay(request(caption, line_balance=50), font_dir=font_dir)
        except CaptionRenderError:
            continue  # does not fit even balanced over several lines
        if caption in EMOJI_CAPTIONS:
            exercised.add(caption)
        for line in result.plan.lines:
            for char in line.text:
                assert bytes(font.getmask(char)) != notdef_mask, (
                    f"{char!r} (U+{ord(char):04X}) drawn as .notdef for {caption!r}"
                )
    assert exercised == set(EMOJI_CAPTIONS)
