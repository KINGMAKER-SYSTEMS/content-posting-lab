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
import re
import unicodedata
from pathlib import Path
from typing import Literal

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
ERROR_SCHEMA = "content-lab.caption-render-error.v1"
RENDERER_ID = "content-lab.pillow-caption.v1"

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
            self.position = "middle"
            self.offset_pct = 0
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
        if self.background != "none" and self.background_color is None:
            raise ValueError("background_color is required for box or highlight")
        return self

    @field_validator("line_breaks")
    @classmethod
    def validate_line_breaks(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not 1 <= len(value) <= 24 or any("\n" in line or "\r" in line or len(line) > 500 for line in value):
            raise ValueError("line_breaks must be 1 through 24 bounded lines")
        return value


CAPTION_STYLE_CONTRACT_SCHEMA = "content-lab.caption-style-contract.v1"
CAPTION_STYLE_CONTRACT_PATH = Path(__file__).parents[1] / "contracts" / "caption-style.v1.json"


def caption_style_contract() -> dict:
    """Every CaptionStyle field and value Content Lab accepts, read off the model.

    The committed copy (contracts/caption-style.v1.json) lets the Worker check
    in its own CI that every caption control it offers is one this renderer
    takes. tests/test_caption_transforms.py fails when the copy drifts;
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
    return {
        "schema": CAPTION_STYLE_CONTRACT_SCHEMA,
        "model": "services/caption_render.py CaptionStyle",
        "unknown_fields": "rejected",
        "fields": fields,
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
    font_size_px: int = Field(gt=0)
    stroke_width_px: int = Field(ge=0)
    line_height_px: int = Field(gt=0)
    rendered_text: str
    lines: list[CaptionRenderLine]


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
_MID_LETTER = frozenset("'\u2018\u2019\u00b7\uff07")


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


def _continues_word(char: str) -> bool:
    # Combining marks and invisible format characters (soft hyphen, joiners)
    # stay inside a word; the zero-width space does not.
    category = unicodedata.category(char)
    return (_starts_word(char) or category[0] == "M"
            or (category == "Cf" and char != "\u200b"))


def _title_cased(text: str) -> str:
    """Python port of the Dossier preview's CSS ``text-transform: capitalize``.

    The Dossier and Schedule previews title-case with CSS, so the burn must
    follow the browser's word rule, not Python's ``str.title``. The first
    character of each word is title-cased and every other character is left
    alone ("iPhone" -> "IPhone", "LOL" stays "LOL"). A word is a run of
    letters, decimal digits, "_" and combining marks. An apostrophe or middle
    dot between two letters stays inside the word ("don't" -> "Don't"); any
    other character, including ".", "-", "/", "\u00b2" and emoji, ends it
    ("lo-fi" -> "Lo-Fi", "90's" -> "90'S", "\U0001f525fire" -> "\U0001f525Fire").
    A character with no one-character title case (such as "\u00df") is kept as
    written, as the browser does. tests/fixtures/real/chrome_capitalize.json
    holds real Chrome output this function is tested against.
    """

    out: list[str] = []
    in_word = False
    previous = ""
    for index, char in enumerate(text):
        if in_word and _continues_word(char):
            out.append(char)
        elif (in_word and char in _MID_LETTER and _is_letter(previous)
              and index + 1 < len(text) and _is_letter(text[index + 1])):
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


def render_caption_overlay(
    request: CaptionRenderRequest,
    *,
    font_dir: Path,
) -> CaptionRenderResult:
    """Render and return a deterministic transparent 1080x1920 caption PNG."""

    style = request.style
    font_path, font_bytes = _resolve_font(font_dir, style.font)
    font_size_px = max(1, round(style.size_pt * _OUTPUT_SCALE))
    stroke_width_px = (style.outline_width_px if style.outline_width_px is not None else _OUTPUT_STROKE_PX) if style.outline else 0
    line_height_px = max(1, round(font_size_px * _LINE_HEIGHT_MULTIPLIER))

    try:
        font = ImageFont.truetype(str(font_path), size=font_size_px)
    except OSError as error:
        raise CaptionRenderError(
            "CAPTION_FONT_INVALID",
            "installed font could not be loaded",
        ) from error

    rendered_source = _cased(request.caption, style.case)
    lines = ([_cased(line, style.case) for line in style.line_breaks]
             if style.line_breaks is not None
             else _wrap_preserving_explicit_newlines(rendered_source, style.line_balance))
    if not lines:
        raise CaptionRenderError("CAPTION_EMPTY", "caption produced no renderable lines")

    image = Image.new("RGBA", (FRAME_WIDTH, FRAME_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    max_width_px = round(FRAME_WIDTH * (_MAX_WIDTH_PCT / 100))
    horizontal_margin_px = (FRAME_WIDTH - max_width_px) // 2
    x_px = {
        "left": horizontal_margin_px,
        "center": FRAME_WIDTH // 2,
        "right": FRAME_WIDTH - horizontal_margin_px,
    }[style.align]
    anchor = {"left": "lm", "center": "mm", "right": "rm"}[style.align]

    block_height_px = len(lines) * line_height_px
    center_y_px = round(
        FRAME_HEIGHT
        * ((_POSITION_Y_PCT[style.position] + style.offset_pct) / 100)
    )
    block_top_px = center_y_px - block_height_px / 2
    block_bottom_px = block_top_px + block_height_px
    if block_top_px < 0 or block_bottom_px > FRAME_HEIGHT:
        raise CaptionRenderError(
            "CAPTION_OUT_OF_FRAME",
            "caption block does not fit vertically in the 9:16 frame",
        )

    line_records: list[dict] = []
    text_boxes: list[tuple[int, int, int, int] | None] = []
    for index, line in enumerate(lines):
        center_line_y = round(block_top_px + (index + 0.5) * line_height_px)
        if not line:
            text_boxes.append(None)
            line_records.append(
                {
                    "text": "",
                    "x_px": x_px,
                    "center_y_px": center_line_y,
                    "width_px": 0,
                }
            )
            continue
        bbox = draw.textbbox(
            (x_px, center_line_y),
            line,
            font=font,
            anchor=anchor,
            stroke_width=stroke_width_px,
        )
        width_px = bbox[2] - bbox[0]
        if width_px > max_width_px:
            raise CaptionRenderError(
                "CAPTION_LINE_TOO_WIDE",
                "caption line exceeds the established 80% render width; increase line_balance or reduce size_pt",
            )
        if bbox[0] < 0 or bbox[2] > FRAME_WIDTH:
            raise CaptionRenderError(
                "CAPTION_OUT_OF_FRAME",
                "caption line does not fit horizontally in the 9:16 frame",
            )
        text_boxes.append(bbox)
        line_records.append(
            {
                "text": line,
                "x_px": x_px,
                "center_y_px": center_line_y,
                "width_px": width_px,
            }
        )

    background_fill = style.background_color
    if style.background == "box":
        nonempty_boxes = [box for box in text_boxes if box is not None]
        left = min(box[0] for box in nonempty_boxes) - 18
        top = min(box[1] for box in nonempty_boxes) - 12
        right = max(box[2] for box in nonempty_boxes) + 18
        bottom = max(box[3] for box in nonempty_boxes) + 12
        draw.rectangle((left, top, right, bottom), fill=background_fill)
    elif style.background == "highlight":
        for box in text_boxes:
            if box is not None:
                draw.rectangle(
                    (box[0] - 14, box[1] - 8, box[2] + 14, box[3] + 8),
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
        "stroke_width_px": stroke_width_px,
        "line_height_px": line_height_px,
        "rendered_text": "\n".join(lines),
        "lines": line_records,
    }
    return CaptionRenderResult.model_validate(
        {
            "schema": RESULT_SCHEMA,
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
    )
