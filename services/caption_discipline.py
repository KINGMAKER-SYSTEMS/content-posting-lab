"""Closed wire contract for the caption selection saved by Dossier.

Content Lab does not choose captions here.  It only validates and preserves
the exact corpus/register selection that the control plane already accepted.
"""

from __future__ import annotations

import math
import re
from typing import Any


CAPTION_SENTIMENTS = (
    "relatable_meme",
    "friendship",
    "heartbreak_life",
    "heartbreak_relationship",
    "relationship_playful",
    "relationship_sincere",
    "faith",
    "family",
    "self_worth",
    "nostalgia",
    "encouragement",
)
CAPTION_DISCIPLINE_FIELDS = {"captionSet", "register", "slingshotShare"}
CAPTION_SET_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
CAPTIONLESS_CLIP_FORMATS = {
    "acct:user8696130597870": ("outfit-faceless", "Off Duty Fits"),
    "acct:user9795207473816": ("glass-architecture", "Glass Signal"),
}


def validate_caption_discipline(
    value: Any, *, page_id: str | None = None, format_mix: Any = None,
) -> dict[str, Any]:
    """Return an exact validated copy or raise ``ValueError``.

    Corpus membership is resolved by the control plane.  This boundary pins
    only the shared typed selection so Content Lab cannot discard or reinterpret
    it while registering and executing the immutable recipe.
    """
    if not isinstance(value, dict) or set(value) != CAPTION_DISCIPLINE_FIELDS:
        raise ValueError("caption discipline schema mismatch")
    captionless_route = CAPTIONLESS_CLIP_FORMATS.get(page_id)
    if captionless_route is not None:
        format_id, label = captionless_route
        if (
            isinstance(format_mix, dict)
            and set(format_mix) == {format_id}
            and type(format_mix[format_id]) in (int, float)
            and format_mix[format_id] == 1
        ):
            empty = {"captionSet": None, "register": [], "slingshotShare": None}
            if value != empty:
                if format_id == "outfit-faceless":
                    raise ValueError("Off Duty Fits uses the finished outfit clip with no caption library")
                raise ValueError(f"{label} uses the finished architecture clip with no caption library")
            return empty
    caption_set = value.get("captionSet")
    if not isinstance(caption_set, str) or CAPTION_SET_SLUG.fullmatch(caption_set) is None:
        raise ValueError("captionSet must be a lowercase corpus slug")
    register = value.get("register")
    if (
        not isinstance(register, list)
        or not 1 <= len(register) <= len(CAPTION_SENTIMENTS)
        or any(not isinstance(sentiment, str) for sentiment in register)
        or len(set(register)) != len(register)
        or any(sentiment not in CAPTION_SENTIMENTS for sentiment in register)
    ):
        raise ValueError("caption register must contain unique canonical sentiments")
    share = value.get("slingshotShare")
    if share is not None and (
        isinstance(share, bool)
        or not isinstance(share, (int, float))
        or not math.isfinite(float(share))
        or not 0.0 <= float(share) <= 1.0
    ):
        raise ValueError("slingshotShare must be null or a number from 0 through 1")
    return {
        "captionSet": caption_set,
        "register": list(register),
        "slingshotShare": share,
    }
