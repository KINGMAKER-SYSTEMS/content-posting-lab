"""Title case and inverted captions: the two Dossier caption transforms.

The Dossier and Schedule previews draw "title" with CSS
``text-transform: capitalize`` and "inverted" with CSS ``rotate(180deg)``
about the centred caption box. These tests hold Content Lab's burn to that
same look, and keep the committed caption-style contract in step with the
model.
"""

import base64
import io
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageStat
from pydantic import ValidationError

from burn_quality_gate import overlay_geometry_reasons
from scripts.export_caption_style_contract import render as render_contract
from services.caption_render import (
    CAPTION_STYLE_CONTRACT_PATH,
    CaptionRenderRequest,
    CaptionStyle,
    _title_cased,
    render_caption_overlay,
)

FONT_FILE = "TikTokSans16pt-Bold.ttf"
CHROME_CAPTURE = Path(__file__).parent / "fixtures" / "real" / "chrome_capitalize.json"
CHROME_REAL_CAPTIONS = Path(__file__).parent / "fixtures" / "real" / "chrome_capitalize_real_captions.json"
CHROME_FUZZ = Path(__file__).parent / "fixtures" / "real" / "chrome_capitalize_fuzz.json"
STYLE = {
    "font": FONT_FILE,
    "size_pt": 32,
    "color": "#ffffff",
    "outline": "#000000",
    "position": "middle",
    "align": "center",
    "case": "as_written",
    "background": "none",
    "offset_pct": 0,
    "line_balance": 0,
}


@pytest.fixture
def font_dir(tmp_path):
    target = tmp_path / "fonts"
    target.mkdir()
    shutil.copy2(Path(__file__).parents[1] / "fonts" / FONT_FILE, target / FONT_FILE)
    return target


def caption_request(caption="one short line", **style):
    return CaptionRenderRequest.model_validate({
        "schema": "content-lab.caption-render-request.v1",
        "caption": caption,
        "style": {**STYLE, **style},
    })


def overlay_image(result) -> Image.Image:
    with Image.open(io.BytesIO(base64.b64decode(result.overlay.base64))) as image:
        image.load()
        return image.copy()


# --- The wire model -------------------------------------------------------


def test_style_accepts_title_case_and_inversion():
    style = CaptionStyle.model_validate({**STYLE, "case": "title", "inverted": True})
    assert (style.case, style.inverted) == ("title", True)


@pytest.mark.parametrize("value", [0, 1, "true", "false", None, [], {}])
def test_inverted_must_be_a_real_boolean(value):
    with pytest.raises(ValidationError):
        CaptionStyle.model_validate({**STYLE, "inverted": value})


@pytest.mark.parametrize("field,value", [("rotate", 180), ("mirror", True), ("case", "sentence"), ("case", "Title")])
def test_unknown_fields_and_values_are_still_refused(field, value):
    with pytest.raises(ValidationError):
        CaptionStyle.model_validate({**STYLE, field: value})


def test_upright_is_never_written_so_old_style_hashes_do_not_move():
    plain = CaptionStyle.model_validate(STYLE)
    explicit = CaptionStyle.model_validate({**STYLE, "inverted": False})
    assert "inverted" not in plain.model_dump() and "inverted" not in explicit.model_dump(mode="json")
    assert plain.model_dump() == explicit.model_dump()
    assert CaptionStyle.model_validate({**STYLE, "inverted": True}).model_dump()["inverted"] is True


@pytest.mark.parametrize("position,offset", [("top", 0), ("bottom", -12), ("middle", 20)])
def test_an_inverted_caption_is_always_centred(position, offset):
    # Same rule as the Worker (captionLayouts.js) and the Dossier preview (top 50%).
    style = CaptionStyle.model_validate({**STYLE, "position": position, "offset_pct": offset, "inverted": True})
    assert (style.position, style.offset_pct) == ("middle", 0)


# --- Title case -------------------------------------------------------------


def test_title_case_matches_real_chrome_capitalize_output():
    capture = json.loads(CHROME_CAPTURE.read_text(encoding="utf-8"))
    assert len(capture["cases"]) >= 1000
    wrong = [(given, chrome, _title_cased(given)) for given, chrome in capture["cases"]
             if _title_cased(given) != chrome]
    assert wrong == []


def test_title_case_matches_chrome_on_every_real_caption():
    # The captions pages actually post, not invented ones: Chrome's output for
    # each, captured the way the Dossier preview draws them.
    capture = json.loads(CHROME_REAL_CAPTIONS.read_text(encoding="utf-8"))
    assert len(capture["cases"]) >= 800
    assert sum(given != chrome for given, chrome in capture["cases"]) >= 700
    wrong = [(given, chrome, _title_cased(given)) for given, chrome in capture["cases"]
             if _title_cased(given) != chrome]
    assert wrong == []


def test_title_case_matches_chrome_on_random_joiner_mark_and_format_strings():
    capture = json.loads(CHROME_FUZZ.read_text(encoding="utf-8"))
    assert len(capture["cases"]) >= 6000
    wrong = [(given, chrome, _title_cased(given)) for given, chrome in capture["cases"]
             if _title_cased(given) != chrome]
    assert wrong == []


@pytest.mark.parametrize("given,expected", [
    ("don't stop me now", "Don't Stop Me Now"),
    ("it’s giving main character", "It’s Giving Main Character"),
    ("pov: ur bf’s mom likes u more", "Pov: Ur Bf’s Mom Likes U More"),
    ("lo-fi beats 24/7", "Lo-Fi Beats 24/7"),
    ("\U0001f525fire emoji \U0001f62d\U0001f62d crying", "\U0001f525Fire Emoji \U0001f62d\U0001f62d Crying"),
    ("iPhone LOL", "IPhone LOL"),
    ("90's kid", "90'S Kid"),
    ("straße élan", "Straße Élan"),
    ("don'", "Don'"),
    ("cafe\u0301's", "Cafe\u0301's"),
    ("a\u00ad'b", "A\u00ad'b"),
    ("a'\ufeffb", "A'\ufeffb"),
    ("col\u00b7legi", "Col\u00b7legi"),
    ("a\u2027b a\u0387b", "A\u2027b A\u0387b"),
    ("a.b a:b a\uff1ab", "A.B A:B A\uff1aB"),
    ("a\u200bb", "A\u200bB"),
])
def test_title_case_examples(given, expected):
    assert _title_cased(given) == expected


def test_title_case_reaches_the_rendered_lines(font_dir):
    result = render_caption_overlay(
        caption_request("when he says don't go \U0001f62d", case="title", line_balance=50), font_dir=font_dir)
    assert result.plan.rendered_text.replace("\n", " ") == "When He Says Don't Go \U0001f62d"
    assert result.plan.effective_style.case == "title"


def test_title_case_applies_to_saved_line_breaks(font_dir):
    result = render_caption_overlay(
        caption_request("one two three", case="title", line_breaks=["one two", "three"]), font_dir=font_dir)
    assert [line.text for line in result.plan.lines] == ["One Two", "Three"]


# --- Inverted renders ---------------------------------------------------------


@pytest.mark.parametrize("align", ["left", "center", "right"])
@pytest.mark.parametrize("background", ["none", "box", "highlight"])
def test_inverted_overlay_is_the_upright_overlay_turned_half_a_turn(font_dir, align, background):
    style = {"align": align, "background": background, "line_balance": 100,
             **({"background_color": "#112233"} if background != "none" else {})}
    upright = render_caption_overlay(caption_request("first line here", **style), font_dir=font_dir)
    inverted = render_caption_overlay(caption_request("first line here", inverted=True, **style), font_dir=font_dir)

    up, inv = overlay_image(upright), overlay_image(inverted)
    assert inv.size == (1080, 1920) and inv.mode == "RGBA"
    assert inv.tobytes() == up.transpose(Image.Transpose.ROTATE_180).tobytes()
    # A rotation, never a mirror: the upside-down copy is not a top-to-bottom flip.
    assert inv.tobytes() != up.transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes()

    # The ink box turns about the frame centre (= the caption box centre).
    x0, y0, x1, y1 = up.getchannel("A").getbbox()
    assert inv.getchannel("A").getbbox() == (1080 - x1, 1920 - y1, 1080 - x0, 1920 - y0)

    # Reading order is reversed on screen: the first line now sits lowest.
    first, *_, last = inverted.plan.lines
    assert first.text == "first" and last.text == "here"
    assert first.center_y_px > last.center_y_px
    assert [(line.x_px, line.center_y_px) for line in inverted.plan.lines] == [
        (1080 - line.x_px, 1920 - line.center_y_px) for line in upright.plan.lines]
    assert inverted.plan.effective_style.inverted is True
    assert inverted.style_sha256 != upright.style_sha256


@pytest.mark.parametrize("align", ["left", "right"])
def test_an_inverted_side_aligned_caption_sits_against_the_other_margin_and_passes_the_gate(font_dir, align):
    result = render_caption_overlay(caption_request("one short line", align=align, inverted=True),
                                    font_dir=font_dir)
    x0, _, x1, _ = overlay_image(result).getchannel("A").getbbox()
    if align == "left":
        assert abs(x1 - 972) <= 8 and x0 > 108 + 40
    else:
        assert abs(x0 - 108) <= 8 and x1 < 972 - 40
    style = result.plan.effective_style.model_dump(exclude_none=True)
    assert overlay_geometry_reasons(result.overlay.base64, style) == []
    # Without the turn the gate would (rightly) call the same pixels misaligned.
    upright_style = {key: value for key, value in style.items() if key != "inverted"}
    assert any(reason.startswith(f"typed_align:{align}") for reason in
               overlay_geometry_reasons(result.overlay.base64, upright_style))


def test_inverted_with_a_page_placement_renders_exactly_like_centred(font_dir):
    placed = render_caption_overlay(caption_request(position="top", offset_pct=-5, inverted=True), font_dir=font_dir)
    centred = render_caption_overlay(caption_request(position="middle", inverted=True), font_dir=font_dir)
    assert placed == centred


def test_both_transforms_together(font_dir):
    result = render_caption_overlay(caption_request("don't look down", case="title", inverted=True),
                                    font_dir=font_dir)
    assert result.plan.rendered_text == "Don't Look Down"
    assert overlay_geometry_reasons(result.overlay.base64,
                                    result.plan.effective_style.model_dump(exclude_none=True)) == []


# --- Through the post renderer, on real video frames -----------------------


def test_post_render_accepts_and_burns_both_transforms(tmp_path):
    from tests.test_post_render import NOW, REAL_PORTRAIT, _request_with_caption_style, _NO_FRAME
    from services import post_render as render

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe are required for actual render proof")
    sha = render.sha256(REAL_PORTRAIT.read_bytes())
    finals = {}
    for name, extra in (("upright", {}), ("inverted", {"inverted": True})):
        post = _request_with_caption_style(_NO_FRAME, case="title", align="left", line_balance=100,
                                           **extra).model_copy(update={"source_sha256": sha})
        result = render.render_post(REAL_PORTRAIT, tmp_path / name, post, clock_ms=lambda: NOW)
        with Image.open(result.qa_frame_path) as qa, Image.open(tmp_path / name / "overlay.png") as overlay:
            finals[name] = (qa.convert("L").copy(), overlay.convert("RGBA").copy())

    (qa_up, overlay_up), (qa_inv, overlay_inv) = finals["upright"], finals["inverted"]
    assert overlay_inv.tobytes() == overlay_up.transpose(Image.Transpose.ROTATE_180).tobytes()

    def fill_mask(overlay, other):
        # White caption fill drawn by this overlay and not by the other one.
        own = overlay.getchannel("A").point(lambda a: 255 if a == 255 else 0)
        white = overlay.getchannel("R").point(lambda r: 255 if r > 200 else 0)
        absent = other.getchannel("A").point(lambda a: 255 if a == 0 else 0)
        return ImageChops.multiply(ImageChops.multiply(own, white), absent)

    only_inverted, only_upright = fill_mask(overlay_inv, overlay_up), fill_mask(overlay_up, overlay_inv)
    assert only_inverted.getbbox() and only_upright.getbbox()
    # In the real decoded frames, the inverted post shows bright caption text
    # where the turned copy is, and the upright post shows it where the
    # upright copy is.
    luma = lambda frame, mask: ImageStat.Stat(frame, mask=mask).mean[0]
    assert luma(qa_inv, only_inverted) > 170 and luma(qa_inv, only_inverted) > luma(qa_up, only_inverted) + 40
    assert luma(qa_up, only_upright) > 170 and luma(qa_up, only_upright) > luma(qa_inv, only_upright) + 40


# --- The contract the Worker checks against ----------------------------------


def test_committed_caption_style_contract_matches_the_model():
    assert CAPTION_STYLE_CONTRACT_PATH.read_text(encoding="utf-8") == render_contract(), (
        "contracts/caption-style.v1.json is stale: run python scripts/export_caption_style_contract.py")


def test_caption_style_contract_lists_both_transforms():
    contract = json.loads(CAPTION_STYLE_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert contract["unknown_fields"] == "rejected"
    assert contract["fields"]["case"]["enum"] == ["as_written", "lower", "upper", "title"]
    assert contract["fields"]["inverted"] == {"type": "boolean", "required": False, "nullable": False,
                                              "default": False}
    assert set(contract["fields"]) == set(CaptionStyle.model_fields)
