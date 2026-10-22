"""Per-cut speed and framing for pages whose library is one master (Eric 2026-10-22).

A page that cuts every clip from one master at the recipe's one speed and crop
posts near-identical videos; TikTok reads them as reused content. For a page
listed here every new source cut gets its own deterministic speed, zoom and
focus, seeded by the page and the cut's own slot id (``sha:start:duration``),
on top of the planner's new window and new length (control_plane_sources.py
plan_source_cuts). The applied values go into the clip's treatment receipt,
so the final render takes the clip as it is and the Worker posts it at exactly
those values (control-plane-worker src/domain/clipVariation.js, which holds the
same page list). Grade, caption and sound are never touched here.

Reverse is not offered: the receipt has no field for it, so no post could
attest it, and ffmpeg's reverse filter buffers every decoded frame of the cut.
"""
from __future__ import annotations

import hashlib

CLIP_VARIATION_PAGES = frozenset({"acct:between-the-lines66"})

# Mostly slow motion; about one cut in six runs slightly fast.
SLOW_SPEEDS = (0.78, 0.8, 0.82, 0.85, 0.88, 0.9, 0.92, 0.95)
FAST_SPEEDS = (1.06, 1.1, 1.12)
FAST_SHARE = 1 / 6
# Framing moves around the recipe's own crop, never far from it.
ZOOM_SPREAD = 0.2
FOCUS_SPREAD = 0.08
MAX_TRIES = 8


def varies_clips(page_id: str) -> bool:
    return page_id in CLIP_VARIATION_PAGES


def _unit(seed: str, part: str) -> float:
    digest = hashlib.sha256(f"{seed}\0{part}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _draw(seed: str, crop: dict[str, float]) -> tuple[float, dict[str, float]]:
    speeds = FAST_SPEEDS if _unit(seed, "fast") < FAST_SHARE else SLOW_SPEEDS
    speed = speeds[int(_unit(seed, "speed") * len(speeds))]
    return speed, {
        "zoom": round(_clamp(crop["zoom"] + (_unit(seed, "zoom") * 2 - 1) * ZOOM_SPREAD, 1.0, 3.0), 2),
        "focusX": round(_clamp(crop["focusX"] + (_unit(seed, "x") * 2 - 1) * FOCUS_SPREAD, 0.0, 1.0), 2),
        "focusY": round(_clamp(crop["focusY"] + (_unit(seed, "y") * 2 - 1) * FOCUS_SPREAD, 0.0, 1.0), 2),
    }


def _key(speed: float, crop: dict[str, float]) -> tuple[float, float, float, float]:
    return (float(speed), float(crop["zoom"]), float(crop["focusX"]), float(crop["focusY"]))


def cut_variant(page_id: str, cut_slot_id: str, base_speed: float, base_crop: dict[str, float],
                previous: tuple[float, dict[str, float]] | None = None) -> tuple[float, dict[str, float]]:
    """The (speed, crop) one cut is made at.

    Never the recipe's own look and never the look of ``previous`` (the cut
    made just before it in the same job), so two cuts in a row never match.
    """
    if not isinstance(cut_slot_id, str) or not cut_slot_id:
        raise ValueError("cut_variant requires the cut's slot id")
    crop = {name: float(base_crop[name]) for name in ("zoom", "focusX", "focusY")}
    avoid = {_key(base_speed, crop)}
    if previous is not None:
        avoid.add(_key(*previous))
    seed = f"{page_id}\0{cut_slot_id}"
    for attempt in range(MAX_TRIES):
        speed, varied = _draw(seed if attempt == 0 else f"{seed}#{attempt}", crop)
        if _key(speed, varied) not in avoid:
            return speed, varied
    # Eight colliding draws is not reachable with these ranges; walk the slow
    # list so the result still differs from both.
    speed, varied = _draw(seed, crop)
    for candidate in SLOW_SPEEDS:
        if _key(candidate, varied) not in avoid:
            return candidate, varied
    raise ValueError("no distinct cut variant")
