"""Shared ffmpeg helpers for color correction and video encoding.

Extracted from routers/burn.py so both routers/burn.py (caption burning) and
routers/video.py (generated-video color correction) can reuse the same
battle-tested color-matrix math without cross-router imports.
"""

import asyncio
import json
import logging
import math
import os
import signal
import subprocess
from fractions import Fraction

from services.page_frame import (
    ACCEPTED_FRAMES,
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    DISPLAY_PIXELS_FILTER,
    FRAME_BAND_HEIGHTS,
    FRAME_FITS,
    VERTICAL_FRAME,
    band_height,
    default_frame_fit,
    display_size,
    fill_pad_filter,
    fit_cut_filters,
)

log = logging.getLogger("ffmpeg")

# A 1080x1920 libx264 encode can consume most of the memory available to the
# production container. Control-plane jobs are intentionally asynchronous, so
# a burst of page refills can otherwise start many independent ffmpeg processes
# at once and have the host kill them during encoder initialization. Keep one
# encode active per service process; callers remain queued in their durable job
# state and continue as permits are released.
_COLOR_CORRECT_GATE = asyncio.Semaphore(1)


# TikTok-optimized encode: 1080x1920, 30fps, H.264 High.
# Used by the burn router to guarantee consistent TikTok-ready output.
# -minrate 8M / -maxrate 20M keeps text overlays crisp on re-upload even
# when the source is simple (low-entropy) footage.
TIKTOK_ENCODE_ARGS: list[str] = [
    "-c:v", "libx264",
    "-preset", "medium",
    "-crf", "18",
    "-minrate", "8M",
    "-maxrate", "20M",
    "-bufsize", "20M",
    "-profile:v", "high",
    "-level", "4.2",
    "-pix_fmt", "yuv420p",
    "-r", "30",
    "-movflags", "+faststart",
    "-c:a", "aac",
    "-b:a", "192k",
]

DELIVERY_ENCODE_PRESETS: dict[str, tuple[str, ...]] = {
    "tiktok_delivery_v1": tuple(TIKTOK_ENCODE_ARGS),
}


def delivery_encode_args(preset: str) -> list[str]:
    """Resolve one closed, versioned delivery preset without a fallback."""
    try:
        return list(DELIVERY_ENCODE_PRESETS[preset])
    except (KeyError, TypeError) as exc:
        raise ValueError("delivery encode preset is unavailable") from exc


# Standard encode that preserves the source's frame rate and skips TikTok-
# specific rate caps. Used by the video router's /color-correct endpoint to
# apply color tweaks to arbitrary-aspect-ratio generated videos without
# resampling the frame rate or forcing an opinionated bitrate floor.
# -c:a copy preserves source audio fidelity (virtually all provider outputs
# ship AAC in MP4; if a container/codec mismatch ever arises, callers can
# swap this preset or add a retry).
STANDARD_ENCODE_ARGS: list[str] = [
    "-c:v", "libx264",
    "-preset", "medium",
    "-crf", "18",
    "-profile:v", "high",
    "-level", "4.2",
    "-pix_fmt", "yuv420p",
    "-movflags", "+faststart",
    "-c:a", "copy",
]


def is_default_cc(cc: dict | None) -> bool:
    """True if the CC dict is None, empty, or has all-zero values."""
    if not cc:
        return True
    for k in (
        "brightness", "contrast", "saturation", "sharpness",
        "shadow", "temperature", "tint", "fade", "grain", "vignette",
    ):
        try:
            if float(cc.get(k, 0)) != 0:
                return False
        except (TypeError, ValueError):
            continue
    return True


def _validated_playback_speed(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.5 <= float(value) <= 2.0
    ):
        raise ValueError("playback_speed must be a finite number from 0.5 through 2.0")
    return float(value)


def _validated_clip_crop(value: dict | None) -> dict[str, float] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"zoom", "focusX", "focusY"}:
        raise ValueError("clip_crop must contain zoom, focusX, and focusY")
    zoom = value.get("zoom")
    focus_x = value.get("focusX")
    focus_y = value.get("focusY")
    if (
        isinstance(zoom, bool)
        or not isinstance(zoom, (int, float))
        or not math.isfinite(float(zoom))
        or not 1.0 <= float(zoom) <= 3.0
        or isinstance(focus_x, bool)
        or not isinstance(focus_x, (int, float))
        or not math.isfinite(float(focus_x))
        or not 0.0 <= float(focus_x) <= 1.0
        or isinstance(focus_y, bool)
        or not isinstance(focus_y, (int, float))
        or not math.isfinite(float(focus_y))
        or not 0.0 <= float(focus_y) <= 1.0
    ):
        raise ValueError("clip_crop must use 1-3x zoom and normalized 0-1 focal points")
    return {
        "zoom": float(zoom),
        "focusX": float(focus_x),
        "focusY": float(focus_y),
    }


def _validated_clip_window(
    start_ms: int | None,
    duration_ms: int | None,
) -> tuple[int, int] | None:
    if start_ms is None and duration_ms is None:
        return None
    if (
        isinstance(start_ms, bool)
        or not isinstance(start_ms, int)
        or start_ms < 0
        or isinstance(duration_ms, bool)
        or not isinstance(duration_ms, int)
        or not 1 <= duration_ms <= 86_400_000
        or start_ms + duration_ms > 86_400_000
    ):
        raise ValueError("clip window requires non-negative start_ms and positive bounded duration_ms")
    return start_ms, duration_ms


def _validated_clip_crop_size(value: tuple[int, int]) -> tuple[int, int]:
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
        or any(item <= 0 or item % 2 for item in value)
    ):
        raise ValueError("clip_crop_size must contain positive even width and height")
    return value


def _clip_crop_filter(
    value: dict | None,
    output_size: tuple[int, int] = (1_080, 1_920),
) -> str | None:
    crop = _validated_clip_crop(value)
    if crop is None:
        return None
    output_width, output_height = _validated_clip_crop_size(output_size)
    zoom = crop["zoom"]
    focus_x = crop["focusX"]
    focus_y = crop["focusY"]
    width = int(round(output_width * zoom))
    height = int(round(output_height * zoom))
    width += width % 2
    height += height % 2
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={output_width}:{output_height}:"
        f"(iw-{output_width})*{focus_x:.6f}:"
        f"(ih-{output_height})*{focus_y:.6f},setsar=1"
    )


def _framed_cut_filters(
    clip_crop: dict | None,
    clip_crop_size: tuple[int, int],
    scale: str | None,
    page_frame: str | None,
    frame_fit: str | None,
    source_size: tuple[int, int] | None,
) -> tuple[str, str, str] | None:
    """(square-pixel prefix, band-aware picture filter, canvas pad), or None for 9:16.

    A framed page's clip is cut into its band on the 1080x1920 canvas (see
    services.page_frame.geometry) on square display pixels: the chain starts
    with page_frame.DISPLAY_PIXELS_FILTER, so an anamorphic master is laid out
    as a viewer (and the Dossier preview) sees it. fill evaluates the same clip
    crop against the band; fit contains the zoomed source window, which needs
    the display size probe_display_size reports. A missing crop is the neutral
    centred crop, as on the sourced executor.
    """
    if page_frame is None or page_frame == VERTICAL_FRAME:
        if frame_fit is not None:
            raise ValueError("frameFit requires a " + ", ".join(FRAME_BAND_HEIGHTS) + " frame")
        return None
    if not isinstance(page_frame, str) or page_frame not in ACCEPTED_FRAMES:
        raise ValueError("frame must be one of " + ", ".join(ACCEPTED_FRAMES))
    fit = default_frame_fit(page_frame) if frame_fit is None else frame_fit
    if not isinstance(fit, str) or fit not in FRAME_FITS:
        raise ValueError("frameFit must be one of " + ", ".join(FRAME_FITS))
    if scale:
        raise ValueError("a framed cut ends on the 1080x1920 canvas; it takes no trailing scale")
    if _validated_clip_crop_size(clip_crop_size) != (CANVAS_WIDTH, CANVAS_HEIGHT):
        raise ValueError("clip_crop_size of a framed cut must be the 1080x1920 canvas")
    crop = _validated_clip_crop(clip_crop) or {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5}
    if fit == "fill":
        return (DISPLAY_PIXELS_FILTER,
                _clip_crop_filter(crop, (CANVAS_WIDTH, band_height(page_frame))),
                fill_pad_filter(page_frame))
    if (
        not isinstance(source_size, tuple)
        or len(source_size) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in source_size)
    ):
        raise ValueError("source_size of a framed fit cut must be the positive probed display size")
    picture_filter, pad_filter = fit_cut_filters(
        source_size[0], source_size[1], page_frame, crop["zoom"], crop["focusX"], crop["focusY"],
    )
    return DISPLAY_PIXELS_FILTER, picture_filter, pad_filter


def build_cc_filter(
    cc: dict | None,
    scale: str | None = None,
    playback_speed: float = 1.0,
    clip_crop: dict | None = None,
    clip_crop_size: tuple[int, int] = (1_080, 1_920),
    page_frame: str | None = None,
    frame_fit: str | None = None,
    source_size: tuple[int, int] | None = None,
) -> str:
    """Build an ffmpeg `-vf` filter string for color correction.

    Args:
        cc: Optional dict with keys brightness/contrast/saturation/sharpness/
            shadow/temperature/tint/fade/grain/vignette (each an integer slider
            value). None or all-default values produces a no-op (see `scale`
            behavior).
        scale: Optional trailing scale filter.
            - None → no scale filter, input dimensions pass through.
            - "1080:1920" → appends `scale=1080:1920:flags=lanczos,setsar=1`
              (Burn tab's TikTok default).
        playback_speed: Video and audio playback-rate multiplier, 0.5 through
            2.0. Video PTS is changed here; `run_color_correct` applies the
            matching audio tempo when an audio stream exists.
        clip_crop_size: Exact even-pixel output width and height used when a
            normalized crop is present. Sourced executors pass their typed,
            hash-bound delivery size here.
        page_frame / frame_fit / source_size: A non-9:16 page frame cuts the
            clip into its band (services.page_frame.geometry) on square
            display pixels (the chain starts with DISPLAY_PIXELS_FILTER): fill
            evaluates the crop against the band, fit (the default for such a
            frame) contains the source window and needs the probed display
            size. The grade and speed stay exactly where they are without a
            frame and a pad to the black 1080x1920 canvas comes last, so the
            grade never touches the bars. Absent or "9:16" is today's cut,
            byte for byte.

    Returns:
        A comma-joined filter string ready for ffmpeg's `-vf` argument. When
        there's nothing to do (default CC + no scale), returns "null" (ffmpeg's
        no-op filter) so the command still validates.
    """
    speed = _validated_playback_speed(playback_speed)
    speed_filter = None if speed == 1.0 else f"setpts=PTS/{speed:.6f}"
    framed = _framed_cut_filters(
        clip_crop, clip_crop_size, scale, page_frame, frame_fit, source_size,
    )
    if framed is None:
        square_filter = pad_filter = None
        crop_filter = _clip_crop_filter(clip_crop, clip_crop_size)
    else:
        square_filter, crop_filter, pad_filter = framed
    scale_filter = (
        f"scale={scale}:flags=lanczos,setsar=1" if scale else None
    )

    # Fast path: no CC → just the scale (or null if no scale either).
    if is_default_cc(cc):
        return ",".join(
            item for item in (square_filter, crop_filter, speed_filter, scale_filter, pad_filter)
            if item
        ) or "null"

    b_raw = float(cc.get("brightness", 0))
    c_raw = float(cc.get("contrast", 0))
    s_raw = float(cc.get("saturation", 0))
    sh_raw = float(cc.get("sharpness", 0))
    sd_raw = float(cc.get("shadow", 0))
    t_raw = float(cc.get("temperature", 0))
    ti_raw = float(cc.get("tint", 0))
    f_raw = float(cc.get("fade", 0))
    grain_raw = max(0.0, min(100.0, float(cc.get("grain", 0))))
    vignette_raw = max(0.0, min(100.0, float(cc.get("vignette", 0))))

    css_brightness = 1 + b_raw / 100
    css_contrast = 1 + c_raw / 100
    css_saturate = 1 + s_raw / 100

    if f_raw > 0:
        fade = f_raw / 100
        css_brightness = min(2.0, css_brightness + fade * 0.4)
        css_contrast = max(0.2, css_contrast - fade * 0.3)
        css_saturate = max(0.2, css_saturate - fade * 0.4)

    if sd_raw != 0:
        css_brightness += sd_raw / 400

    sharpness = sh_raw / 50

    # Second default check after the CSS-equivalent transforms — a combination
    # of sliders (e.g. fade + shadow) may cancel out to an effective no-op.
    is_default = (
        abs(css_brightness - 1.0) < 0.005
        and abs(css_contrast - 1.0) < 0.005
        and abs(css_saturate - 1.0) < 0.005
        and abs(t_raw) <= 1
        and abs(ti_raw) <= 1
        and sharpness < 0.001
        and grain_raw < 0.001
        and vignette_raw < 0.001
    )
    if is_default:
        return ",".join(
            item for item in (square_filter, crop_filter, speed_filter, scale_filter, pad_filter)
            if item
        ) or "null"

    mat = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    off = [0.0, 0.0, 0.0]

    def mat_mul(a: list, b: list) -> list:
        return [
            [sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)
        ]

    def mat_vec(m: list, v: list) -> list:
        return [sum(m[i][j] * v[j] for j in range(3)) for i in range(3)]

    if abs(css_brightness - 1.0) >= 0.005:
        b = css_brightness
        mat = [[b * mat[i][j] for j in range(3)] for i in range(3)]
        off = [b * o for o in off]

    if abs(css_contrast - 1.0) >= 0.005:
        c = css_contrast
        bias = 0.5 * (1 - c)
        mat = [[c * mat[i][j] for j in range(3)] for i in range(3)]
        off = [c * o + bias for o in off]

    if abs(css_saturate - 1.0) >= 0.005:
        s = css_saturate
        sr, sg, sb = 0.2126, 0.7152, 0.0722
        sat_mat = [
            [sr + (1 - sr) * s, sg - sg * s, sb - sb * s],
            [sr - sr * s, sg + (1 - sg) * s, sb - sb * s],
            [sr - sr * s, sg - sg * s, sb + (1 - sb) * s],
        ]
        off = mat_vec(sat_mat, off)
        mat = mat_mul(sat_mat, mat)

    if abs(t_raw) > 1:
        if t_raw > 0:
            amt = min(1.0, t_raw / 200)
            t_mat = [
                [1 - amt + amt * 0.393, amt * 0.769, amt * 0.189],
                [amt * 0.349, 1 - amt + amt * 0.686, amt * 0.168],
                [amt * 0.272, amt * 0.534, 1 - amt + amt * 0.131],
            ]
        else:
            rad = math.radians(t_raw / 5)
            cos_a, sin_a = math.cos(rad), math.sin(rad)
            t_mat = [
                [
                    0.213 + 0.787 * cos_a - 0.213 * sin_a,
                    0.715 - 0.715 * cos_a - 0.715 * sin_a,
                    0.072 - 0.072 * cos_a + 0.928 * sin_a,
                ],
                [
                    0.213 - 0.213 * cos_a + 0.143 * sin_a,
                    0.715 + 0.285 * cos_a + 0.140 * sin_a,
                    0.072 - 0.072 * cos_a - 0.283 * sin_a,
                ],
                [
                    0.213 - 0.213 * cos_a - 0.787 * sin_a,
                    0.715 - 0.715 * cos_a + 0.715 * sin_a,
                    0.072 + 0.928 * cos_a + 0.072 * sin_a,
                ],
            ]
        off = mat_vec(t_mat, off)
        mat = mat_mul(t_mat, mat)

    if abs(ti_raw) > 1:
        rad = math.radians(ti_raw / 3)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        ti_mat = [
            [
                0.213 + 0.787 * cos_a - 0.213 * sin_a,
                0.715 - 0.715 * cos_a - 0.715 * sin_a,
                0.072 - 0.072 * cos_a + 0.928 * sin_a,
            ],
            [
                0.213 - 0.213 * cos_a + 0.143 * sin_a,
                0.715 + 0.285 * cos_a + 0.140 * sin_a,
                0.072 - 0.072 * cos_a - 0.283 * sin_a,
            ],
            [
                0.213 - 0.213 * cos_a - 0.787 * sin_a,
                0.715 - 0.715 * cos_a + 0.715 * sin_a,
                0.072 + 0.928 * cos_a + 0.072 * sin_a,
            ],
        ]
        off = mat_vec(ti_mat, off)
        mat = mat_mul(ti_mat, mat)

    ccm = (
        f"colorchannelmixer="
        f"rr={mat[0][0]:.6f}:rg={mat[0][1]:.6f}:rb={mat[0][2]:.6f}:ra={off[0]:.6f}:"
        f"gr={mat[1][0]:.6f}:gg={mat[1][1]:.6f}:gb={mat[1][2]:.6f}:ga={off[1]:.6f}:"
        f"br={mat[2][0]:.6f}:bg={mat[2][1]:.6f}:bb={mat[2][2]:.6f}:ba={off[2]:.6f}"
    )

    filters = [item for item in (square_filter, "format=rgb24", ccm) if item]
    if sharpness >= 0.001:
        filters.append(f"unsharp=5:5:{sharpness:.2f}:5:5:{sharpness:.2f}")
    if grain_raw >= 0.001:
        # The dossier dial is 0..1. Keep the generated-media treatment subtle:
        # at 100 this is visible film grain, never destructive static.
        filters.append(f"noise=alls={grain_raw * 0.15:.2f}:allf=t+u")
    if vignette_raw >= 0.001:
        # FFmpeg's lens angle grows stronger as the PI denominator shrinks.
        # Map the dossier's 0..1 dial into a restrained PI/12..PI/4 range.
        denominator = 12.0 - (vignette_raw / 100.0) * 8.0
        filters.append(f"vignette=angle=PI/{denominator:.3f}:eval=frame")
    if crop_filter:
        filters.append(crop_filter)
    if speed_filter:
        filters.append(speed_filter)
    if scale_filter:
        filters.append(scale_filter)
    if pad_filter:
        filters.append(pad_filter)

    return ",".join(filters)


# A hung encode must not own the only treatment-lane permit forever. Four times
# the input duration plus two minutes tolerates slow shared hosts, while the
# ten-minute floor covers startup-heavy short media and the three-hour ceiling
# prevents a corrupt duration or unknown input from turning into a multi-day
# lock. Unknown duration deliberately receives the ceiling, never a short guess.
_ENCODE_TIMEOUT_FLOOR_SECONDS = 600.0
_ENCODE_TIMEOUT_MULTIPLIER = 4.0
_ENCODE_TIMEOUT_GRACE_SECONDS = 120.0
_ENCODE_TIMEOUT_CEILING_SECONDS = 3 * 60 * 60.0
_ENCODE_STDERR_TAIL_BYTES = 64 * 1024
_ENCODE_STDERR_READ_BYTES = 8 * 1024
_ENCODE_REAP_TIMEOUT_SECONDS = 1.0
_INPUT_PROBE_TIMEOUT_SECONDS = 30.0


def _encode_timeout_seconds(input_duration_seconds: float | None) -> float:
    if input_duration_seconds is None:
        return _ENCODE_TIMEOUT_CEILING_SECONDS
    return min(
        _ENCODE_TIMEOUT_CEILING_SECONDS,
        max(
            _ENCODE_TIMEOUT_FLOOR_SECONDS,
            input_duration_seconds * _ENCODE_TIMEOUT_MULTIPLIER
            + _ENCODE_TIMEOUT_GRACE_SECONDS,
        ),
    )


def _probe_input_duration_seconds(input_path: str) -> float | None:
    """Return a local input's duration, or None when ffprobe cannot prove it."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                input_path,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_INPUT_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
        duration = float(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, TypeError, ValueError, OSError):
        return None
    if result.returncode != 0 or not math.isfinite(duration) or duration <= 0:
        return None
    return duration


async def _bounded_encode_stderr(proc) -> bytes:
    """Drain stderr continuously while retaining only its diagnostic tail."""
    tail = bytearray()
    while True:
        chunk = await proc.stderr.read(_ENCODE_STDERR_READ_BYTES)
        if not chunk:
            return bytes(tail)
        tail.extend(chunk)
        if len(tail) > _ENCODE_STDERR_TAIL_BYTES:
            del tail[:-_ENCODE_STDERR_TAIL_BYTES]


async def _wait_for_encode(proc) -> bytes:
    _, stderr = await asyncio.gather(proc.wait(), _bounded_encode_stderr(proc))
    return stderr


async def _terminate_encode_process(proc) -> None:
    """Kill the encode's process group and reap it for a bounded time."""
    if proc.returncode is None:
        try:
            # start_new_session=True made the child a session/group leader, so
            # its pid is the process-group id. Never signal the caller's group.
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    # A killed process should normally be reaped immediately. Keep cleanup
    # bounded too: Process.wait() may still be waiting for subprocess transport
    # shutdown (and test doubles or unusual loop implementations may never
    # complete it). The process group has already received SIGKILL, so leaving
    # this waiter behind is safer than holding the request and global encode
    # gate forever.
    wait_task = asyncio.create_task(proc.wait())
    done, _ = await asyncio.wait(
        {wait_task}, timeout=_ENCODE_REAP_TIMEOUT_SECONDS,
    )
    if wait_task not in done:
        wait_task.cancel()
        wait_task.add_done_callback(_consume_encode_wait_result)
        return
    try:
        wait_task.result()
    except Exception:
        pass


def _consume_encode_wait_result(task: asyncio.Task) -> None:
    """Retrieve a detached bounded-cleanup task's eventual exception."""
    if not task.cancelled():
        try:
            task.exception()
        except asyncio.CancelledError:
            pass


_FRAME_PROBE_OUTPUT_LIMIT_BYTES = 64 * 1024


class FrameGeometryUnavailable(RuntimeError):
    """ffprobe could not prove a framed fit cut's source size; that cut falls back to fill."""

    def __init__(self, reason: str):
        super().__init__(f"frame_source_geometry_unavailable:{reason}")
        self.reason = reason


def _sample_aspect_ratio(value) -> Fraction:
    """ffprobe's num:den; an unknown ratio (absent, N/A, 0:x) is square, as in ffmpeg."""
    if value in (None, "", "N/A"):
        return Fraction(1)
    try:
        numerator, denominator = (int(part) for part in str(value).split(":"))
    except ValueError as error:
        raise FrameGeometryUnavailable("sample_aspect_ratio_invalid") from error
    if numerator < 0 or denominator <= 0:
        raise FrameGeometryUnavailable("sample_aspect_ratio_invalid")
    return Fraction(numerator, denominator) if numerator else Fraction(1)


async def probe_display_size(input_path) -> tuple[int, int]:
    """Display width and height the framed cut's filters work on.

    run_color_correct lets ffmpeg autorotate: a quarter-turn display rotation
    swaps the coded width and height and inverts the sample aspect ratio (a
    half turn or any other angle keeps both). Every framed cut then starts
    with page_frame.DISPLAY_PIXELS_FILTER, so this returns
    page_frame.display_size of the upright frame. Anything ffprobe cannot
    prove raises FrameGeometryUnavailable; the caller then cuts fill, which
    needs no size. Bounded like the other probes: its own process group, a
    deadline and a byte cap on the reply.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
            "-select_streams", "v:0",
            "-show_entries",
            "stream=width,height,sample_aspect_ratio:stream_tags=rotate:stream_side_data=rotation",
            "-of", "json", str(input_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        raise FrameGeometryUnavailable("ffprobe_unavailable") from error

    async def bounded_reply() -> bytes:
        reply = bytearray()
        while chunk := await process.stdout.read(_ENCODE_STDERR_READ_BYTES):
            reply.extend(chunk)
            if len(reply) > _FRAME_PROBE_OUTPUT_LIMIT_BYTES:
                raise FrameGeometryUnavailable("reply_too_large")
        await process.wait()
        return bytes(reply)

    try:
        raw = await asyncio.wait_for(bounded_reply(), timeout=_INPUT_PROBE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as error:
        raise FrameGeometryUnavailable("timeout") from error
    finally:
        await _terminate_encode_process(process)
    if process.returncode != 0:
        raise FrameGeometryUnavailable("ffprobe_failed")
    try:
        stream = json.loads(raw)["streams"][0]
        width, height = stream["width"], stream["height"]
        rotations = [float(row.get("rotation", 0)) for row in stream.get("side_data_list", [])]
        rotations.append(float(stream.get("tags", {}).get("rotate", 0)))
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
        raise FrameGeometryUnavailable("reply_invalid") from error
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (width, height)):
        raise FrameGeometryUnavailable("size_invalid")
    sample_aspect_ratio = _sample_aspect_ratio(stream.get("sample_aspect_ratio"))
    rotation = next((value for value in rotations if value != 0), 0.0) % 360
    if abs(rotation - 90) < 1 or abs(rotation - 270) < 1:
        width, height, sample_aspect_ratio = height, width, 1 / sample_aspect_ratio
    return display_size(width, height, sample_aspect_ratio)


async def run_color_correct(
    input_path: str,
    output_path: str,
    cc: dict | None,
    scale: str | None = None,
    encode_args: list[str] | None = None,
    playback_speed: float = 1.0,
    clip_crop: dict | None = None,
    clip_crop_size: tuple[int, int] = (1_080, 1_920),
    clip_start_ms: int | None = None,
    clip_duration_ms: int | None = None,
    source_duration_ms: int | None = None,
    page_frame: str | None = None,
    frame_fit: str | None = None,
    source_size: tuple[int, int] | None = None,
) -> None:
    """Run ffmpeg to produce a color-corrected copy of a video.

    A non-9:16 ``page_frame`` cuts the picture into its band on the black
    1080x1920 canvas (see ``build_cc_filter``); only the -vf argument changes.
    Raises RuntimeError with the last ~500 chars of stderr on ffmpeg failure.
    """
    speed = _validated_playback_speed(playback_speed)
    window = _validated_clip_window(clip_start_ms, clip_duration_ms)
    if source_duration_ms is not None and (
        isinstance(source_duration_ms, bool)
        or not isinstance(source_duration_ms, int)
        or not 1 <= source_duration_ms <= 86_400_000
    ):
        raise ValueError("source_duration_ms must be a positive bounded integer")
    vf = build_cc_filter(
        cc,
        scale=scale,
        playback_speed=speed,
        clip_crop=clip_crop,
        clip_crop_size=clip_crop_size,
        page_frame=page_frame,
        frame_fit=frame_fit,
        source_size=source_size,
    )
    enc = list(encode_args if encode_args is not None else STANDARD_ENCODE_ARGS)
    output_window: list[str] = []
    if window is not None:
        # A millisecond seek at a master's tail can lose one frame. Normalize
        # timestamps and extend only that fractional tail to the declared cut.
        vf += ",setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration=0.1"
        output_window = ["-t", f"{window[1] / 1000 / speed:.6f}"]
    audio_args: list[str] = []
    if speed != 1.0:
        # `0:a?` preserves silent provider outputs while changing embedded
        # audio in lockstep when it exists. Filtered audio cannot stream-copy.
        audio_args = ["-map", "0:v:0", "-map", "0:a?", "-af", f"atempo={speed:.6f}"]
        for index, argument in enumerate(enc[:-1]):
            if argument == "-c:a" and enc[index + 1] == "copy":
                enc[index + 1] = "aac"
    input_window = (
        [
            "-ss", f"{window[0] / 1000:.3f}",
            "-t", f"{window[1] / 1000:.3f}",
        ] if window is not None else []
    )
    cmd = [
        "ffmpeg", "-y",
        *input_window,
        "-i", input_path,
        "-vf", vf,
        *audio_args,
        *enc,
        *output_window,
        output_path,
    ]
    if window is not None:
        input_duration_seconds = window[1] / 1000.0
    elif source_duration_ms is not None:
        input_duration_seconds = source_duration_ms / 1000.0
    else:
        input_duration_seconds = await asyncio.to_thread(
            _probe_input_duration_seconds,
            input_path,
        )
    timeout = _encode_timeout_seconds(input_duration_seconds)
    async with _COLOR_CORRECT_GATE:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stderr = await asyncio.wait_for(_wait_for_encode(proc), timeout=timeout)
        except asyncio.TimeoutError as error:
            await _terminate_encode_process(proc)
            try:
                os.unlink(output_path)
            except OSError:
                pass
            raise RuntimeError("ffmpeg_encode_timeout") from error
        except asyncio.CancelledError:
            await _terminate_encode_process(proc)
            try:
                os.unlink(output_path)
            except OSError:
                pass
            raise
        except Exception:
            await _terminate_encode_process(proc)
            try:
                os.unlink(output_path)
            except OSError:
                pass
            raise
    if proc.returncode != 0:
        try:
            os.unlink(output_path)
        except OSError:
            pass
        tail = stderr.decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"ffmpeg color-correct failed: {tail}")
