"""Caption / overlay quality gate for Content Lab burn + rail pre-render.

Contract (operator-approved burned_003 / contentlab-v4, 2026-08-05):
  Overlay alpha-bbox geometry:
    - side margins >= 4%
    - width <= 70%
    - centered within +/- 5% of frame center
    - vertical center between 30% and 62%
    - height <= 45%
  Caption:
    - <= 30 words
    - persona voice blacklist (male persona rejects female-voiced lines)

Standalone POST /api/quality-check never accepts force. /api/burn-overlay may
pass force:true to bypass after an operator override.
"""

from __future__ import annotations

import base64
import io
import re
from typing import Any

from PIL import Image

# Female-voiced / mismatched-persona phrases that burned live truck posts.
# Keep in sync with rail data/caption_gate.json defaults where possible.
MALE_PERSONA_BLOCKED = (
    "to my man",
    "my man",
    "her man",
    "a girl who",
    "as a girl",
    "girls who",
    "he compliments me",
    "when he ",
    "if he ",
    "he texts",
    "he calls",
    "he says",
    "my boyfriend",
    "my husband",
    "my bf ",
    "my hubby",
    "his girl",
    "his wife",
    "boyfriend nah",
    "wife material",
)

MAX_WORDS = 30
FRAME_W = 1080
FRAME_H = 1920
ALPHA_THRESHOLD = 8  # treat near-transparent as empty


def _word_count(caption: str) -> int:
    return len([w for w in re.split(r"\s+", (caption or "").strip()) if w])


def caption_reasons(caption: str, persona: str = "male") -> list[str]:
    reasons: list[str] = []
    words = _word_count(caption)
    if words > MAX_WORDS:
        reasons.append(f"caption_too_long:{words}_words_max_{MAX_WORDS}")
    lowered = (caption or "").lower()
    if (persona or "male").lower() == "male":
        for phrase in MALE_PERSONA_BLOCKED:
            if phrase in lowered:
                reasons.append(f"persona_voice:{phrase.strip()}")
                break
    return reasons


def _decode_overlay(overlay_b64: str) -> Image.Image:
    raw = overlay_b64
    if "," in raw and raw.strip().startswith("data:"):
        raw = raw.split(",", 1)[1]
    data = base64.b64decode(raw)
    return Image.open(io.BytesIO(data)).convert("RGBA")


def overlay_geometry_reasons(
    overlay_b64: str | None, caption_style: dict[str, Any] | None = None
) -> list[str]:
    """Measure alpha geometry against legacy v4 or the typed Dossier style."""
    if not overlay_b64:
        return ["overlay_missing"]
    try:
        img = _decode_overlay(overlay_b64)
    except Exception as exc:  # noqa: BLE001 — surface any decode failure as a gate reason
        return [f"overlay_decode_failed:{type(exc).__name__}"]

    w, h = img.size
    if w < 1 or h < 1:
        return ["overlay_empty"]

    alpha = img.split()[3]
    # Threshold near-transparent pixels so compression noise does not inflate the box.
    mask = alpha.point(lambda a: 255 if a > ALPHA_THRESHOLD else 0)
    bbox = mask.getbbox()
    if not bbox:
        return ["overlay_no_ink"]
    min_x, min_y, max_x, max_y = bbox
    # getbbox max edges are exclusive; convert to inclusive for margin math.
    max_x -= 1
    max_y -= 1

    box_w = max_x - min_x + 1
    box_h = max_y - min_y + 1
    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0

    reasons: list[str] = []
    left_margin = min_x / w
    right_margin = (w - 1 - max_x) / w
    if left_margin < 0.04:
        reasons.append(f"side_margin_left:{left_margin:.3f}<0.04")
    if right_margin < 0.04:
        reasons.append(f"side_margin_right:{right_margin:.3f}<0.04")

    width_pct = box_w / w
    max_width = 0.80 if caption_style is not None else 0.70
    if width_pct > max_width:
        reasons.append(f"width:{width_pct:.3f}>{max_width:.2f}")

    if caption_style is None:
        center_off = abs(cx - (w / 2.0)) / w
        if center_off > 0.05:
            reasons.append(f"not_centered:{center_off:.3f}>0.05")

        v_center = cy / h
        if v_center < 0.30 or v_center > 0.62:
            reasons.append(f"vertical_center:{v_center:.3f}_not_in_0.30-0.62")
    else:
        align = caption_style["align"]
        if align == "left":
            edge_off = abs((min_x / w) - 0.10)
        elif align == "right":
            edge_off = abs((max_x / w) - 0.90)
        else:
            edge_off = abs((cx / w) - 0.50)
        if edge_off > 0.05:
            reasons.append(f"typed_align:{align}:{edge_off:.3f}>0.05")

        target = {"top": 0.15, "middle": 0.50, "bottom": 0.85}[
            caption_style["position"]
        ] + float(caption_style["offset_pct"]) / 100.0
        position_off = abs((cy / h) - target)
        if position_off > 0.08:
            reasons.append(
                f"typed_position:{caption_style['position']}:{position_off:.3f}>0.08"
            )

    height_pct = box_h / h
    if height_pct > 0.45:
        reasons.append(f"height:{height_pct:.3f}>0.45")

    return reasons


def run_quality_check(
    caption: str,
    persona: str = "male",
    overlay_png: str | None = None,
    *,
    require_overlay: bool = True,
    caption_style: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reasons = caption_reasons(caption, persona)
    if require_overlay or overlay_png:
        reasons.extend(overlay_geometry_reasons(overlay_png, caption_style))
    return {
        "ok": len(reasons) == 0,
        "reasons": reasons,
        "caption_words": _word_count(caption),
        "persona": persona or "male",
    }

