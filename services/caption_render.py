"""Typed, deterministic caption overlay rendering for 9:16 post bytes.

The public wire model intentionally mirrors ``dossier-contracts::CaptionStyle``.
There is no second set of style controls here: this module validates the exact
field names saved by Dossier, resolves only Content Lab's installed TikTokSans
fonts, and turns the effective style into a transparent 1080x1920 PNG.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import re
import unicodedata
from pathlib import Path
from typing import Callable, Literal

from PIL import Image, ImageDraw, ImageFont, __version__ as pillow_version
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)


REQUEST_SCHEMA = "content-lab.caption-render-request.v1"
RESULT_SCHEMA = "content-lab.caption-render-result.v1"
REQUEST_SCHEMA_V2 = "content-lab.caption-render-request.v2"
RESULT_SCHEMA_V2 = "content-lab.caption-render-result.v2"
ERROR_SCHEMA = "content-lab.caption-render-error.v1"
RENDERER_ID = "content-lab.pillow-caption.v1"
PictureFrame = Literal["9:16", "16:9", "4:3", "1:1", "3:4"]

FRAME_WIDTH = 1080
FRAME_HEIGHT = 1920

# The production Burn canvas renders a 432x768 preview onto 1080x1920 bytes.
# Keep its established scale, width, line-height, stroke, and quick-position
# behavior so this backend path replaces browser PNG capture without changing
# the look of already-approved captions.
_PREVIEW_HEIGHT = 768
_OUTPUT_SCALE = FRAME_HEIGHT / _PREVIEW_HEIGHT
_MAX_WIDTH_PCT = 80
_LINE_HEIGHT_MULTIPLIER = 1.08
_OUTPUT_STROKE_PX = 3
_POSITION_Y_PCT = {"top": 15, "middle": 50, "bottom": 85}

_FONT_PATTERN = re.compile(r"^TikTokSans[A-Za-z0-9.-]{0,112}\.ttf$")
_HEX_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
_LINE_BREAKS_MAX = 24
_LINE_BREAK_MAX_CHARS = 500
_BACKGROUNDS_NEEDING_COLOR = ("box", "highlight")
# Where an inverted caption always sits (see CaptionStyle.center_inverted_caption).
_INVERTED_PLACEMENT = {"position": "middle", "offset_pct": 0}
# Fit inside the visible picture (owner rule, 2026-10-04). The style's size is
# the maximum: a caption that stays inside the picture is drawn exactly as
# styled, however big, and one that would leave it only shrinks. The picture is
# every row of the 1080x1920 canvas on a full-screen page, or the band of rows a
# framed page keeps, inset FIT_SIDE_MARGIN_PX on the left and right (the quality
# gate's 4% side margin: 44/1080 >= 0.04); there is no top or bottom inset. The
# smallest size a caption may shrink to is the smallest size_pt the style
# allows: 12 pt, 30 px.
FIT_SIDE_MARGIN_PX = 44
FIT_FLOOR_PX = round(12 * _OUTPUT_SCALE)
# Rows and columns a box or highlight background paints beyond the text ink.
_BACKGROUND_PAD_PX = {"box": (18, 12), "highlight": (14, 8)}


class CaptionRenderError(ValueError):
    """A stable, machine-readable fail-closed renderer error."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class CaptionStyle(BaseModel):
    """Exact effective ``dossier-contracts::CaptionStyle`` wire shape."""

    model_config = ConfigDict(extra="forbid")

    font: str = Field(min_length=1, max_length=128)
    size_pt: float = Field(ge=12, le=96, strict=True)
    color: str
    outline: str | None = None
    position: Literal["top", "middle", "bottom"]
    align: Literal["left", "center", "right"]
    case: Literal["as_written", "lower", "upper", "title"] = "as_written"
    background: Literal["none", "box", "highlight"] = "none"
    background_color: str | None = None
    offset_pct: float = Field(default=0, ge=-40, le=40, strict=True)
    line_balance: int = Field(ge=0, le=100, strict=True)
    outline_width_px: int | None = Field(default=None, ge=0, le=20, strict=True)
    line_breaks: list[str] | None = None
    # Turns the whole caption 180 degrees about its own centre, exactly like
    # the Dossier preview's CSS ``rotate(180deg)``. Only a real JSON boolean
    # is accepted.
    inverted: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def center_inverted_caption(self) -> "CaptionStyle":
        # An inverted caption is always centred, whatever the page placement
        # says. The Worker sends it that way (captionLayouts.js) and the
        # Dossier preview draws it that way (top 50%), so the burn matches.
        if self.inverted:
            for key, value in _INVERTED_PLACEMENT.items():
                setattr(self, key, value)
        return self

    @model_serializer(mode="wrap")
    def omit_upright(self, handler: SerializerFunctionWrapHandler):
        # Upright is the default and is never written out, so every caption
        # saved before inversion existed keeps byte-identical style hashes.
        data = handler(self)
        if isinstance(data, dict) and data.get("inverted") is False:
            data.pop("inverted")
        return data

    @field_validator("font")
    @classmethod
    def validate_font_name(cls, value: str) -> str:
        if not _FONT_PATTERN.fullmatch(value):
            raise ValueError("font must name a Content Lab TikTokSans TTF")
        return value

    @field_validator("color", "outline", "background_color")
    @classmethod
    def validate_color(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _HEX_PATTERN.fullmatch(value):
            raise ValueError("color must use #rrggbb")
        return value.lower()

    @model_validator(mode="after")
    def validate_background(self) -> "CaptionStyle":
        if self.background in _BACKGROUNDS_NEEDING_COLOR and self.background_color is None:
            raise ValueError("background_color is required for box or highlight")
        return self

    @field_validator("line_breaks")
    @classmethod
    def validate_line_breaks(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not 1 <= len(value) <= _LINE_BREAKS_MAX or any(
                "\n" in line or "\r" in line or len(line) > _LINE_BREAK_MAX_CHARS for line in value):
            raise ValueError("line_breaks must be 1 through 24 bounded lines")
        return value


CAPTION_STYLE_CONTRACT_SCHEMA = "content-lab.caption-style-contract.v1"
CAPTION_STYLE_CONTRACT_PATH = Path(__file__).parents[1] / "contracts" / "caption-style.v1.json"


def caption_style_contract() -> dict:
    """Every CaptionStyle field, value and cross-field rule Content Lab checks, read off the model.

    Types, enums and numeric ranges come from the model's JSON schema; the
    font and colour patterns, the line_breaks limits and the two cross-field
    rules come from the same constants the validators use. The committed copy
    (contracts/caption-style.v1.json) lets the Worker check in its own CI that
    every caption style it can send is one this renderer takes. Not in it:
    render-time refusals that depend on the installed fonts and the rendered
    size (CAPTION_FONT_UNAVAILABLE, italic fonts, and CAPTION_OUT_OF_FRAME for a
    caption that cannot fit the visible picture even at 12 pt), and the request rule that line_breaks must spell
    the caption. tests/test_caption_transforms.py fails when the copy drifts;
    regenerate it with ``python scripts/export_caption_style_contract.py``.
    """

    schema = CaptionStyle.model_json_schema()
    required = set(schema.get("required", []))
    fields: dict[str, dict] = {}
    for name, prop in sorted(schema["properties"].items()):
        variants = prop.get("anyOf", [prop])
        nullable = any(variant.get("type") == "null" for variant in variants)
        (value,) = [variant for variant in variants if variant.get("type") != "null"]
        field: dict = {"type": value["type"], "required": name in required, "nullable": nullable}
        for key in ("enum", "minimum", "maximum", "minLength", "maxLength"):
            if key in value:
                field[key] = value[key]
        if value["type"] == "array":
            field["items"] = value["items"]["type"]
        if "default" in prop and prop["default"] is not None:
            field["default"] = prop["default"]
        fields[name] = field
    fields["font"]["pattern"] = _FONT_PATTERN.pattern
    for name in ("color", "outline", "background_color"):
        fields[name]["pattern"] = _HEX_PATTERN.pattern
    fields["line_breaks"].update({
        "minItems": 1,
        "maxItems": _LINE_BREAKS_MAX,
        "itemMaxLength": _LINE_BREAK_MAX_CHARS,
        "itemForbids": ["\n", "\r"],
    })
    return {
        "schema": CAPTION_STYLE_CONTRACT_SCHEMA,
        "model": "services/caption_render.py CaptionStyle",
        "unknown_fields": "rejected",
        "fields": fields,
        "rules": [
            {"when": {"background": list(_BACKGROUNDS_NEEDING_COLOR)}, "requires": ["background_color"]},
            {"when": {"inverted": True}, "sets": dict(_INVERTED_PLACEMENT)},
        ],
    }


class CaptionRenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)

    schema_: Literal[REQUEST_SCHEMA] = Field(alias="schema")
    caption: str = Field(min_length=1, max_length=4_000)
    style: CaptionStyle

    @field_validator("caption")
    @classmethod
    def validate_caption(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("caption must contain visible text")
        if "\x00" in value:
            raise ValueError("caption must not contain NUL")
        return value

    @model_validator(mode="after")
    def validate_exact_lines(self) -> "CaptionRenderRequest":
        if self.style.line_breaks is not None:
            canonical = lambda value: " ".join(value.split()).lower()
            if canonical(" ".join(self.style.line_breaks)) != canonical(self.caption):
                raise ValueError("line_breaks must preserve the caption text")
        return self


class CaptionRenderRequestV2(CaptionRenderRequest):
    """Frame-aware request; v1 remains the full-screen compatibility contract."""

    schema_: Literal[REQUEST_SCHEMA_V2] = Field(alias="schema")
    picture_frame: PictureFrame


class CaptionRendererIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Literal[RENDERER_ID]
    pillow_version: str


class CaptionRenderCanvas(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: Literal[FRAME_WIDTH]
    height: Literal[FRAME_HEIGHT]


class CaptionRenderLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    x_px: int
    center_y_px: int
    width_px: int = Field(ge=0)


class CaptionRenderPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    renderer: CaptionRendererIdentity
    canvas: CaptionRenderCanvas
    effective_style: CaptionStyle
    font_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    # The style's size: the most the caption may use.
    font_size_px: int = Field(gt=0)
    # Only when the caption had to shrink to stay inside the visible picture:
    # the size actually drawn. line_height_px follows the drawn size. Absent
    # when it fit as styled, so those plans and their hashes are unchanged.
    fitted_font_size_px: int | None = Field(default=None, gt=0)
    stroke_width_px: int = Field(ge=0)
    line_height_px: int = Field(gt=0)
    rendered_text: str
    lines: list[CaptionRenderLine]

    @model_serializer(mode="wrap")
    def omit_unfitted(self, handler: SerializerFunctionWrapHandler):
        data = handler(self)
        if isinstance(data, dict) and data.get("fitted_font_size_px") is None:
            data.pop("fitted_font_size_px", None)
        return data


class CaptionRenderPlanV2(CaptionRenderPlan):
    """V2 plan binds the fit calculation to the caller's visible picture."""

    picture_frame: PictureFrame


class CaptionRenderOverlay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_type: Literal["image/png"]
    width: Literal[FRAME_WIDTH]
    height: Literal[FRAME_HEIGHT]
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    base64: str


class CaptionRenderResult(BaseModel):
    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)

    schema_: Literal[RESULT_SCHEMA] = Field(alias="schema")
    renderer: CaptionRendererIdentity
    caption_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    style_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    render_plan_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    plan: CaptionRenderPlan
    overlay: CaptionRenderOverlay


class CaptionRenderResultV2(BaseModel):
    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)

    schema_: Literal[RESULT_SCHEMA_V2] = Field(alias="schema")
    renderer: CaptionRendererIdentity
    caption_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    style_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    render_plan_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    plan: CaptionRenderPlanV2
    overlay: CaptionRenderOverlay


def picture_frame_rows(picture_frame: PictureFrame) -> tuple[int, int] | None:
    """Resolve the closed frame enum to the exact delivery-band rows."""
    from services.page_frame import FRAME_BAND_HEIGHTS, frame_band_rows

    band_height = FRAME_BAND_HEIGHTS.get(picture_frame)
    return frame_band_rows(band_height) if band_height is not None else None


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _wrap_balanced(text: str, balance: int) -> list[str]:
    """Exact Python port of ``dossier_contracts::wrap_balanced``."""

    words = text.split()
    if not words:
        return []

    line_count = 1 + (min(balance, 100) * (len(words) - 1) + 50) // 100
    line_count = max(1, min(line_count, len(words)))
    if line_count == 1:
        return [" ".join(words)]
    if line_count == len(words):
        return words

    widths = [len(word) for word in words]
    prefix = [0]
    for width in widths:
        prefix.append(prefix[-1] + width)

    def segment_width(start: int, end: int) -> int:
        return prefix[end] - prefix[start] + max(0, end - start - 1)

    infinity = 2**63 - 1
    costs = [[infinity] * (len(words) + 1) for _ in range(line_count + 1)]
    costs[0][0] = 0
    for line in range(1, line_count + 1):
        for end in range(line, len(words) + 1):
            best = infinity
            for split in range(line - 1, end):
                previous = costs[line - 1][split]
                if previous != infinity:
                    width = segment_width(split, end)
                    best = min(best, previous + width * width)
            costs[line][end] = best

    splits: list[tuple[int, int]] = []
    line = line_count
    end = len(words)
    while line > 0:
        for split in range(line - 1, end):
            width = segment_width(split, end)
            if costs[line - 1][split] + width * width == costs[line][end]:
                splits.append((split, end))
                end = split
                break
        line -= 1
    splits.reverse()
    return [" ".join(words[start:end]) for start, end in splits]


def _wrap_preserving_explicit_newlines(text: str, balance: int) -> list[str]:
    """Balance each explicit line independently and retain blank line breaks."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for paragraph in normalized.split("\n"):
        wrapped = _wrap_balanced(paragraph, balance)
        lines.extend(wrapped if wrapped else [""])
    return lines


def _resolve_font(font_dir: Path, filename: str) -> tuple[Path, bytes]:
    root = font_dir.resolve()
    candidate = (root / filename).resolve()
    if candidate.parent != root or not candidate.is_file():
        raise CaptionRenderError(
            "CAPTION_FONT_UNAVAILABLE",
            "font is not installed in Content Lab",
        )
    if "Italic" in candidate.name or not _FONT_PATTERN.fullmatch(candidate.name):
        raise CaptionRenderError(
            "CAPTION_FONT_UNSUPPORTED",
            "font is not in Content Lab's advertised TikTokSans render set",
        )
    try:
        font_bytes = candidate.read_bytes()
    except OSError as error:
        raise CaptionRenderError(
            "CAPTION_FONT_UNAVAILABLE",
            "font bytes could not be read",
        ) from error
    return candidate, font_bytes


# Characters that join two letters into one word, as Chrome's CSS
# ``capitalize`` treats them: "don't", "rock\u2019n\u2019roll", "l\u00b7l".
# Checked one by one in Chrome; ":", "." and "\uff1a" are not joiners there.
_MID_LETTER = frozenset("'\u2018\u2019\u00b7\uff07\u0387\u055f\u05f4\u2024\u2027\ufe13\ufe52")


def _is_letter(char: str) -> bool:
    # Letter-like numerals such as "\u216b" count as letters, as in Unicode's word rules.
    category = unicodedata.category(char)
    return (category[0] == "L" or category == "Nl") and not _is_wide_ideograph(char)


def _is_wide_ideograph(char: str) -> bool:
    # Han, kana (full or half width) and hangul are each a word on their own
    # in the browser, so a letter right after one starts a new word
    # ("\u65e5a" -> "\u65e5A").
    return (unicodedata.category(char) == "Lo"
            and unicodedata.east_asian_width(char) in ("W", "F", "H"))


def _starts_word(char: str) -> bool:
    return _is_letter(char) or unicodedata.category(char) in ("Nd", "Pc")


def _is_extend(char: str) -> bool:
    # Combining marks and invisible format characters (soft hyphen, joiners)
    # ride along with the character before them; the zero-width space does not.
    category = unicodedata.category(char)
    return category[0] == "M" or (category == "Cf" and char != "\u200b")


def _continues_word(char: str) -> bool:
    return _starts_word(char) or _is_extend(char)


def _next_base(text: str, index: int) -> str:
    # The first character after ``index`` that is not a mark or format character.
    for char in text[index + 1:]:
        if not _is_extend(char):
            return char
    return ""


def _title_cased(text: str) -> str:
    """Python port of the Dossier preview's CSS ``text-transform: capitalize``.

    The Dossier and Schedule previews title-case with CSS, so the burn must
    follow the browser's word rule, not Python's ``str.title``. The first
    character of each word is title-cased and every other character is left
    alone ("iPhone" -> "IPhone", "LOL" stays "LOL"). A word is a run of
    letters, decimal digits, "_" and combining marks. An apostrophe, middle
    dot or other ``_MID_LETTER`` joiner between two letters stays inside the
    word ("don't" -> "Don't"), looking past accents and invisible format
    characters on either side; any other character, including ".", ":", "-",
    "/", "\u00b2" and emoji, ends it
    ("lo-fi" -> "Lo-Fi", "90's" -> "90'S", "\U0001f525fire" -> "\U0001f525Fire").
    A character with no one-character title case (such as "\u00df") is kept as
    written, as the browser does. tests/fixtures/real/chrome_capitalize.json
    and chrome_capitalize_real_captions.json hold real Chrome output this
    function is tested against.
    """

    out: list[str] = []
    in_word = False
    previous = ""
    for index, char in enumerate(text):
        if in_word and _continues_word(char):
            out.append(char)
        elif (in_word and char in _MID_LETTER and _is_letter(previous)
              and (following := _next_base(text, index)) != "" and _is_letter(following)):
            out.append(char)
        elif not in_word and _starts_word(char):
            titled = char.title()
            out.append(titled if len(titled) == 1 else char)
            in_word = True
        else:
            out.append(char)
            in_word = False
        if _is_wide_ideograph(char):
            in_word = False
        if not _is_extend(char):
            previous = char
    return "".join(out)


def _cased(text: str, text_case: str) -> str:
    if text_case == "lower":
        return text.lower()
    if text_case == "upper":
        return text.upper()
    if text_case == "title":
        return _title_cased(text)
    return text


def caption_fit_area(picture_rows: tuple[int, int] | None = None) -> tuple[int, int, int, int]:
    """Inclusive ``(left, top, right, bottom)`` pixels a caption's ink must stay inside.

    ``picture_rows`` is the first and last visible canvas row (inclusive): a
    framed page's band, or None for the whole full-screen canvas.
    """

    first, last = picture_rows if picture_rows is not None else (0, FRAME_HEIGHT - 1)
    if not 0 <= first < last < FRAME_HEIGHT:
        raise ValueError("picture rows must be two increasing rows on the 1920-row canvas")
    return (FIT_SIDE_MARGIN_PX, first, FRAME_WIDTH - 1 - FIT_SIDE_MARGIN_PX, last)


def _load_font(font_path: Path, size_px: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(font_path), size=size_px)
    except OSError as error:
        raise CaptionRenderError(
            "CAPTION_FONT_INVALID",
            "installed font could not be loaded",
        ) from error


def _line_height_px(font_size_px: int) -> int:
    return max(1, round(font_size_px * _LINE_HEIGHT_MULTIPLIER))


def _layout_lines(
    draw: ImageDraw.ImageDraw,
    font: ImageFont.FreeTypeFont,
    lines: list[str],
    *,
    x_px: int,
    anchor: str,
    stroke_width_px: int,
    line_height_px: int,
    center_y_px: int,
) -> tuple[list[dict], list[tuple[int, int, int, int] | None]]:
    block_top_px = center_y_px - len(lines) * line_height_px / 2
    line_records: list[dict] = []
    text_boxes: list[tuple[int, int, int, int] | None] = []
    for index, line in enumerate(lines):
        center_line_y = round(block_top_px + (index + 0.5) * line_height_px)
        if not line:
            text_boxes.append(None)
            line_records.append({"text": "", "x_px": x_px, "center_y_px": center_line_y, "width_px": 0})
            continue
        bbox = draw.textbbox(
            (x_px, center_line_y),
            line,
            font=font,
            anchor=anchor,
            stroke_width=stroke_width_px,
        )
        text_boxes.append(bbox)
        line_records.append(
            {"text": line, "x_px": x_px, "center_y_px": center_line_y, "width_px": bbox[2] - bbox[0]}
        )
    return line_records, text_boxes


def _ink_box(text_boxes: list[tuple[int, int, int, int] | None], background: str) -> tuple[int, int, int, int]:
    """Every pixel the text, its outline and any box/highlight can paint (inclusive)."""

    pad_x, pad_y = _BACKGROUND_PAD_PX.get(background, (0, 0))
    boxes = [box for box in text_boxes if box is not None]
    return (min(box[0] for box in boxes) - pad_x, min(box[1] for box in boxes) - pad_y,
            max(box[2] for box in boxes) + pad_x, max(box[3] for box in boxes) + pad_y)


def _inside(box: tuple[int, int, int, int], area: tuple[int, int, int, int]) -> bool:
    return area[0] <= box[0] and area[1] <= box[1] and box[2] <= area[2] and box[3] <= area[3]


def _scale_about(centre: float, low: int, high: int, ink_low: int, ink_high: int) -> float:
    # The caption scales about its anchor, so each side scales on its own.
    scales = [1.0]
    if ink_low < centre:
        scales.append((centre - low) / (centre - ink_low))
    if ink_high > centre:
        scales.append((high - centre) / (ink_high - centre))
    return min(scales)


def _fit_font_size(
    font_at: Callable[[int], ImageFont.FreeTypeFont],
    lines: list[str],
    *,
    max_px: int,
    stroke_width_px: int,
    background: str,
    x_px: int,
    anchor: str,
    center_y_px: int,
    area: tuple[int, int, int, int],
) -> int:
    """Largest size up to ``max_px`` whose ink stays inside ``area``.

    The ink is measured exactly as the draw places it: every line's text box
    with its outline, grown by the box or highlight padding. The lines are
    kept as chosen and only scaled; the outline and background padding keep
    their pixel sizes. Never grows past ``max_px``; refuses below FIT_FLOOR_PX.
    """

    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    def ink(size: int) -> tuple[int, int, int, int]:
        _, boxes = _layout_lines(
            measure, font_at(size), lines, x_px=x_px, anchor=anchor,
            stroke_width_px=stroke_width_px, line_height_px=_line_height_px(size), center_y_px=center_y_px,
        )
        return _ink_box(boxes, background)

    size = max_px
    largest_fit: int | None = None
    smallest_miss: int | None = None
    while True:
        box = ink(size)
        if _inside(box, area):
            # A proportional jump can land a pixel low (line heights and glyph
            # boxes round), so creep back up to the largest size that fits.
            largest_fit = size
            if smallest_miss is None or size + 1 >= smallest_miss:
                return size
            size += 1
            continue
        smallest_miss = size
        if largest_fit is not None:
            return largest_fit
        if size <= FIT_FLOOR_PX:
            raise CaptionRenderError(
                "CAPTION_OUT_OF_FRAME",
                "caption does not fit inside the visible picture even at 12 pt",
            )
        scale = min(_scale_about(x_px, area[0], area[2], box[0], box[2]),
                    _scale_about(center_y_px, area[1], area[3], box[1], box[3]))
        size = max(FIT_FLOOR_PX, min(size - 1, math.floor(size * scale)))


def render_caption_overlay(
    request: CaptionRenderRequest | CaptionRenderRequestV2,
    *,
    font_dir: Path,
    fit_rows: tuple[int, int] | None = None,
) -> CaptionRenderResult:
    """Render and return a deterministic transparent 1080x1920 caption PNG.

    The caption is drawn at the style's size when its ink stays inside the
    visible picture (see ``caption_fit_area``); otherwise it
    shrinks to the largest size that does, down to 12 pt. ``fit_rows`` is the
    first and last row (inclusive) of a framed page's picture band; None is
    the whole full-screen canvas.
    """

    style = request.style
    font_path, font_bytes = _resolve_font(font_dir, style.font)
    font_size_px = max(1, round(style.size_pt * _OUTPUT_SCALE))
    stroke_width_px = (style.outline_width_px if style.outline_width_px is not None else _OUTPUT_STROKE_PX) if style.outline else 0
    fonts = {font_size_px: _load_font(font_path, font_size_px)}

    def font_at(size: int) -> ImageFont.FreeTypeFont:
        if size not in fonts:
            fonts[size] = _load_font(font_path, size)
        return fonts[size]

    rendered_source = _cased(request.caption, style.case)
    lines = ([_cased(line, style.case) for line in style.line_breaks]
             if style.line_breaks is not None
             else _wrap_preserving_explicit_newlines(rendered_source, style.line_balance))
    if not lines:
        raise CaptionRenderError("CAPTION_EMPTY", "caption produced no renderable lines")

    max_width_px = round(FRAME_WIDTH * (_MAX_WIDTH_PCT / 100))
    horizontal_margin_px = (FRAME_WIDTH - max_width_px) // 2
    x_px = {
        "left": horizontal_margin_px,
        "center": FRAME_WIDTH // 2,
        "right": FRAME_WIDTH - horizontal_margin_px,
    }[style.align]
    anchor = {"left": "lm", "center": "mm", "right": "rm"}[style.align]
    center_y_px = round(
        FRAME_HEIGHT
        * ((_POSITION_Y_PCT[style.position] + style.offset_pct) / 100)
    )

    area = caption_fit_area(fit_rows)
    if style.inverted:
        # The finished canvas is turned half a turn below, so fit against
        # where the area sits before that turn.
        area = (FRAME_WIDTH - 1 - area[2], FRAME_HEIGHT - 1 - area[3],
                FRAME_WIDTH - 1 - area[0], FRAME_HEIGHT - 1 - area[1])
    drawn_font_size_px = _fit_font_size(
        font_at,
        lines,
        max_px=font_size_px,
        stroke_width_px=stroke_width_px,
        background=style.background,
        x_px=x_px,
        anchor=anchor,
        center_y_px=center_y_px,
        area=area,
    )

    while True:
        font = font_at(drawn_font_size_px)
        line_height_px = _line_height_px(drawn_font_size_px)
        image = Image.new("RGBA", (FRAME_WIDTH, FRAME_HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        line_records, text_boxes = _layout_lines(
            draw, font, lines, x_px=x_px, anchor=anchor, stroke_width_px=stroke_width_px,
            line_height_px=line_height_px, center_y_px=center_y_px,
        )

        background_fill = style.background_color
        if style.background == "box":
            draw.rectangle(_ink_box(text_boxes, "box"), fill=background_fill)
        elif style.background == "highlight":
            pad_x, pad_y = _BACKGROUND_PAD_PX["highlight"]
            for box in text_boxes:
                if box is not None:
                    draw.rectangle(
                        (box[0] - pad_x, box[1] - pad_y, box[2] + pad_x, box[3] + pad_y),
                        fill=background_fill,
                    )

        for record in line_records:
            if not record["text"]:
                continue
            draw.text(
                (record["x_px"], record["center_y_px"]),
                record["text"],
                font=font,
                anchor=anchor,
                align=style.align,
                fill=style.color,
                stroke_width=stroke_width_px,
                stroke_fill=style.outline,
            )

        # The drawn pixels, not only the measurement, must stay inside.
        drawn = image.getchannel("A").getbbox()
        if drawn is None or _inside((drawn[0], drawn[1], drawn[2] - 1, drawn[3] - 1), area):
            break
        if drawn_font_size_px <= FIT_FLOOR_PX:
            raise CaptionRenderError(
                "CAPTION_OUT_OF_FRAME",
                "caption does not fit inside the visible picture even at 12 pt",
            )
        drawn_font_size_px -= 1

    if style.inverted:
        # The Dossier preview draws an inverted caption with CSS
        # ``rotate(180deg)`` about the caption box's own centre. That box is
        # 80% of the frame wide and always centred (left 10%, top 50%), so
        # its centre is the frame centre and turning the whole transparent
        # canvas half a turn is the same rotation: the text reads upside
        # down, the first line ends up at the bottom and a left-aligned
        # caption sits against the right margin. A rotation, never a mirror.
        image = image.transpose(Image.Transpose.ROTATE_180)
        for record in line_records:
            record["x_px"] = FRAME_WIDTH - record["x_px"]
            record["center_y_px"] = FRAME_HEIGHT - record["center_y_px"]

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    png_bytes = output.getvalue()

    effective_style = style.model_dump(mode="json", exclude_none=True)
    renderer = {
        "id": RENDERER_ID,
        "pillow_version": pillow_version,
    }
    plan = {
        "renderer": renderer,
        "canvas": {"width": FRAME_WIDTH, "height": FRAME_HEIGHT},
        "effective_style": effective_style,
        "font_sha256": _sha256(font_bytes),
        "font_size_px": font_size_px,
        # Only a shrunk caption records its drawn size, so every caption that
        # already fit keeps a byte-identical plan and render_plan_sha256.
        **({"fitted_font_size_px": drawn_font_size_px} if drawn_font_size_px != font_size_px else {}),
        "stroke_width_px": stroke_width_px,
        "line_height_px": line_height_px,
        "rendered_text": "\n".join(lines),
        "lines": line_records,
    }
    picture_frame = (
        request.picture_frame if isinstance(request, CaptionRenderRequestV2) else None
    )
    if picture_frame is not None:
        plan["picture_frame"] = picture_frame
    result_payload = {
            "schema": RESULT_SCHEMA_V2 if picture_frame is not None else RESULT_SCHEMA,
            "renderer": renderer,
            "caption_sha256": _sha256(request.caption.encode("utf-8")),
            "style_sha256": _sha256(_canonical_json(effective_style)),
            "render_plan_sha256": _sha256(_canonical_json(plan)),
            "plan": plan,
            "overlay": {
                "media_type": "image/png",
                "width": FRAME_WIDTH,
                "height": FRAME_HEIGHT,
                "sha256": _sha256(png_bytes),
                "base64": base64.b64encode(png_bytes).decode("ascii"),
            },
        }
    result_model = CaptionRenderResultV2 if picture_frame is not None else CaptionRenderResult
    return result_model.model_validate(result_payload)
