"""Per-page picture frame: the band a page's picture occupies on the canvas.

The delivered video always stays a 1080x1920 canvas. A non-vertical frame
(16:9, 1:1, 3:4, 4:3) is the centred horizontal band of that ratio; everything
outside it is plain black. The frame shapes the cut: sourced and generated
clips are cut into the band (``frameFit`` fill or fit, see ``geometry``), and the
prepared-post render letterboxes the same band, which is idempotent on a
band-shaped clip. The frame is not part of the source's applied video
treatment, so clips cut before a page changes frame stay usable (they post
through the centre band of their 9:16 picture).
"""
from __future__ import annotations

import math
from fractions import Fraction
from typing import Any

VERTICAL_FRAME = "9:16"
# Even band heights on the 1080-wide delivery canvas.
FRAME_BAND_HEIGHTS = {"16:9": 608, "1:1": 1080, "3:4": 1440, "4:3": 810}
ACCEPTED_FRAMES = (VERTICAL_FRAME, *FRAME_BAND_HEIGHTS)
FRAME_FITS = ("fill", "fit")
CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1920
# fit needs a source window of at least this many pixels each way; a smaller
# source is cut fill, which divides by nothing (F7).
MIN_FIT_SOURCE_PX = 2
# The first filter of every framed cut: the frame is stretched to square display
# pixels at an even width, so the band geometry works on what a viewer (and the
# Dossier preview) sees. ffmpeg's sar is 1 when unknown; a square, even-width
# frame passes through untouched. The 9:16 cut never uses it.
DISPLAY_PIXELS_FILTER = "scale=trunc(iw*sar/2)*2:ih,setsar=1"


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


def frame_fit(treatment: dict[str, Any]) -> str:
    """Validate the treatment's frame and frameFit together; return the fit.

    ``frameFit`` is legal only with a non-9:16 frame. Absent, it is ``fill`` for
    a full-bleed page and ``fit`` for every other frame.
    """
    band = frame_band_height(treatment)
    if "frameFit" not in treatment:
        return "fill" if band is None else "fit"
    if band is None:
        raise ValueError("frameFit requires a " + ", ".join(FRAME_BAND_HEIGHTS) + " frame")
    value = treatment["frameFit"]
    if not isinstance(value, str) or value not in FRAME_FITS:
        raise ValueError("frameFit must be one of " + ", ".join(FRAME_FITS))
    return value


def resolved_frame(treatment: dict[str, Any]) -> tuple[str, str]:
    """(frame, frameFit) with defaults applied; an absent frame is 9:16 fill."""
    fit = frame_fit(treatment)
    return treatment.get("frame", VERTICAL_FRAME), fit


def band_height(frame: str) -> int:
    if not isinstance(frame, str) or frame not in ACCEPTED_FRAMES:
        raise ValueError("frame must be one of " + ", ".join(ACCEPTED_FRAMES))
    return FRAME_BAND_HEIGHTS.get(frame, CANVAS_HEIGHT)


def band_top(frame: str) -> int:
    """First band row, rounded down to an even row (#193: 656, 420, 240, 554)."""
    return ((CANVAS_HEIGHT - band_height(frame)) // 2) & ~1


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
    """Keep the centred band of an exact 1080x1920 frame and pad it with black.

    The top offset is rounded down to an even row so the band lands on the
    4:2:0 chroma grid the same way for every source: an odd offset (4:3's
    555) was kept or moved by a row depending on the pixel format ffmpeg
    chose for the graph. 4:3 is therefore rows 554-1363 (bars 554 and 556).
    """
    top = ((CANVAS_HEIGHT - band_height) // 2) & ~1
    return (f"crop={CANVAS_WIDTH}:{band_height}:0:{top},"
            f"pad={CANVAS_WIDTH}:{CANVAS_HEIGHT}:0:{top}:black")


def default_frame_fit(frame: str) -> str:
    return "fill" if frame == VERTICAL_FRAME else "fit"


def effective_frame_fit(src_w: int, src_h: int, frame: str, fit: str | None) -> str:
    """The fit a cut can honour: fit on a source under 2 px either way is fill."""
    fit = fit or default_frame_fit(frame)
    if fit == "fit" and (src_w < MIN_FIT_SOURCE_PX or src_h < MIN_FIT_SOURCE_PX):
        return "fill"
    return fit


def display_size(width: int, height: int, sample_aspect_ratio: Fraction = Fraction(1)) -> tuple[int, int]:
    """The frame DISPLAY_PIXELS_FILTER makes of a width x height frame with that SAR.

    Mirrors ffmpeg's expression arithmetic: (double) num/den, trunc(iw*sar/2)*2,
    an unknown (zero) SAR counts as square, and a width that truncates to 0
    keeps the input width, as ffmpeg's scale does for a zero size.
    """
    sar = sample_aspect_ratio.numerator / sample_aspect_ratio.denominator if sample_aspect_ratio else 1.0
    return (math.trunc(width * sar / 2) * 2 or width), height


def _even_nearest(value: float) -> int:
    return 2 * int(math.floor(value / 2 + 0.5))


def _even_up(value: float) -> int:
    n = int(round(value))  # Python round (half-even), exactly like services/ffmpeg.py
    return n + (n % 2)


def _rescale(a: int, b: int, c: int) -> int:
    # av_rescale(a, b, c) with AV_ROUND_NEAR_INF: round(a*b/c), halves away from zero
    return (a * b + c // 2) // c


def geometry(src_w: int, src_h: int, frame: str = VERTICAL_FRAME, fit: str | None = None,
             zoom: float = 1.0, focus_x: float = 0.5, focus_y: float = 0.5) -> dict:
    """Picture rectangle on the canvas and the source window it shows.

    The shared Team 2 contract (tests/fixtures/page-frame-geometry-fixtures.v1.json),
    identical to the reference ``frame_geometry_ref.geometry``; keep every
    integer rounding exactly as it is.

    fill covers the band: today's clip crop evaluated at output (1080, band)
    (scale to cover, then crop at the focal point), padded at the band's top
    row. fit shows the whole zoomed window at the source's own aspect,
    contained in the band and centred, on black. 9:16 is fill only.
    """
    if frame not in ACCEPTED_FRAMES:
        raise ValueError("frame")
    fit = fit or default_frame_fit(frame)
    if fit not in FRAME_FITS or (frame == VERTICAL_FRAME and fit != "fill"):
        raise ValueError("fit")
    if (any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (src_w, src_h))
            or not 1.0 <= zoom <= 3.0 or not 0.0 <= focus_x <= 1.0 or not 0.0 <= focus_y <= 1.0):
        raise ValueError("source size and clip crop must be positive and in range")
    fit = effective_frame_fit(src_w, src_h, frame, fit)
    band = band_height(frame)
    top = band_top(frame)
    if fit == "fill":
        sw, sh = _even_up(CANVAS_WIDTH * zoom), _even_up(band * zoom)
        scaled_w = max(sw, _rescale(sh, src_w, src_h))
        scaled_h = max(sh, _rescale(sw, src_h, src_w))
        x = int(math.floor((scaled_w - CANVAS_WIDTH) * focus_x)) & ~1
        y = int(math.floor((scaled_h - band) * focus_y)) & ~1
        return {
            "band": {"top": top, "height": band},
            "picture": {"x": 0, "y": top, "w": CANVAS_WIDTH, "h": band},
            "window": {"x": x * src_w / scaled_w, "y": y * src_h / scaled_h,
                       "w": CANVAS_WIDTH * src_w / scaled_w, "h": band * src_h / scaled_h},
            "scaled": {"w": scaled_w, "h": scaled_h, "cropX": x, "cropY": y},
        }
    max_w, max_h = src_w - src_w % 2, src_h - src_h % 2
    cw = min(max_w, max(2, _even_nearest(src_w / zoom)))
    ch = min(max_h, max(2, _even_nearest(src_h / zoom)))
    cx = int(math.floor((src_w - cw) * focus_x)) & ~1
    cy = int(math.floor((src_h - ch) * focus_y)) & ~1
    if cw * band >= CANVAS_WIDTH * ch:
        pw, ph = CANVAS_WIDTH, min(band, max(2, _even_nearest(CANVAS_WIDTH * ch / cw)))
    else:
        pw, ph = min(CANVAS_WIDTH, max(2, _even_nearest(band * cw / ch))), band
    px = ((CANVAS_WIDTH - pw) // 2) & ~1
    py = top + (((band - ph) // 2) & ~1)
    return {
        "band": {"top": top, "height": band},
        "picture": {"x": px, "y": py, "w": pw, "h": ph},
        "window": {"x": cx, "y": cy, "w": cw, "h": ch},
        "scaled": {"w": pw, "h": ph, "cropX": 0, "cropY": 0},
    }


def canvas_pad_filter(x: int, y: int) -> str:
    """Place the picture on the black 1080x1920 canvas; always the cut's last step."""
    return f"pad={CANVAS_WIDTH}:{CANVAS_HEIGHT}:{x}:{y}:black"


def fit_cut_filters(src_w: int, src_h: int, frame: str, zoom: float, focus_x: float,
                    focus_y: float) -> tuple[str, str]:
    """(picture filter, canvas pad) for a framed fit cut of a square-pixel source.

    The picture filter crops the exact source window and scales it to the
    picture rectangle; the pad puts that rectangle on the canvas. Callers cut a
    source under 2 px either way fill (effective_frame_fit).
    """
    if effective_frame_fit(src_w, src_h, frame, "fit") != "fit":
        raise ValueError(f"fit needs a source of at least {MIN_FIT_SOURCE_PX} px each way")
    result = geometry(src_w, src_h, frame, "fit", zoom, focus_x, focus_y)
    window, picture = result["window"], result["picture"]
    return (f"crop={window['w']}:{window['h']}:{window['x']}:{window['y']}:exact=1,"
            f"scale={picture['w']}:{picture['h']}:flags=lanczos,setsar=1",
            canvas_pad_filter(picture["x"], picture["y"]))


def fill_pad_filter(frame: str) -> str:
    """Canvas pad for a framed fill cut: the full-width band at its top row."""
    return canvas_pad_filter(0, band_top(frame))
