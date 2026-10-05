"""Per-page picture frame, applied only when a prepared post is rendered.

Every source master and generated clip is already a 1080x1920 centre-crop, so
a frame is not part of the source's applied video treatment: sources stay
reusable and a page's frame change takes effect on its next prepared post.
The delivered video always stays a 1080x1920 canvas. A non-vertical frame keeps
the centred horizontal band of that ratio and fills the rest with plain black.
"""
from __future__ import annotations

from typing import Any

VERTICAL_FRAME = "9:16"
# Even band heights on the 1080-wide delivery canvas.
FRAME_BAND_HEIGHTS = {"16:9": 608, "1:1": 1080, "3:4": 1440, "4:3": 810}
ACCEPTED_FRAMES = (VERTICAL_FRAME, *FRAME_BAND_HEIGHTS)
CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1920


def frame_band_height(treatment: dict[str, Any]) -> int | None:
    """Return the band height for the treatment's frame; None is full-bleed 9:16.

    An absent frame and "9:16" are the same vertical delivery. Any other value
    is rejected rather than falling back to full-bleed.
    """
    if "frame" not in treatment:
        return None
    value = treatment["frame"]
    if not isinstance(value, str) or value not in ACCEPTED_FRAMES:
        raise ValueError("frame must be one of " + ", ".join(ACCEPTED_FRAMES))
    return FRAME_BAND_HEIGHTS.get(value)


def frame_band_rows(band_height: int) -> tuple[int, int]:
    """First and last canvas row (both inclusive) of the centred picture band.

    The top offset is rounded down to an even row so the band lands on the
    4:2:0 chroma grid the same way for every source: an odd offset (4:3's
    555) was kept or moved by a row depending on the pixel format ffmpeg
    chose for the graph. 4:3 is therefore rows 554-1363 (bars 554 and 556);
    16:9 is rows 656-1263.
    """
    top = ((CANVAS_HEIGHT - band_height) // 2) & ~1
    return top, top + band_height - 1


def letterbox_filter(band_height: int) -> str:
    """Keep the centred band of an exact 1080x1920 frame and pad it with black."""
    top, _ = frame_band_rows(band_height)
    return (f"crop={CANVAS_WIDTH}:{band_height}:0:{top},"
            f"pad={CANVAS_WIDTH}:{CANVAS_HEIGHT}:0:{top}:black")
