"""Shared ffmpeg helpers for color correction and video encoding.

Extracted from routers/burn.py so both routers/burn.py (caption burning) and
routers/video.py (generated-video color correction) can reuse the same
battle-tested color-matrix math without cross-router imports.
"""

import asyncio
import logging
import math

log = logging.getLogger("ffmpeg")


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


def _clip_crop_filter(value: dict | None) -> str | None:
    crop = _validated_clip_crop(value)
    if crop is None:
        return None
    zoom = crop["zoom"]
    focus_x = crop["focusX"]
    focus_y = crop["focusY"]
    width = int(round(1080 * zoom))
    height = int(round(1920 * zoom))
    width += width % 2
    height += height % 2
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop=1080:1920:(iw-1080)*{focus_x:.6f}:(ih-1920)*{focus_y:.6f},setsar=1"
    )


def build_cc_filter(
    cc: dict | None,
    scale: str | None = None,
    playback_speed: float = 1.0,
    clip_crop: dict | None = None,
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

    Returns:
        A comma-joined filter string ready for ffmpeg's `-vf` argument. When
        there's nothing to do (default CC + no scale), returns "null" (ffmpeg's
        no-op filter) so the command still validates.
    """
    speed = _validated_playback_speed(playback_speed)
    speed_filter = None if speed == 1.0 else f"setpts=PTS/{speed:.6f}"
    crop_filter = _clip_crop_filter(clip_crop)
    scale_filter = (
        f"scale={scale}:flags=lanczos,setsar=1" if scale else None
    )

    # Fast path: no CC → just the scale (or null if no scale either).
    if is_default_cc(cc):
        return ",".join(
            item for item in (crop_filter, speed_filter, scale_filter) if item
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
            item for item in (crop_filter, speed_filter, scale_filter) if item
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

    filters = ["format=rgb24", ccm]
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

    return ",".join(filters)


async def run_color_correct(
    input_path: str,
    output_path: str,
    cc: dict | None,
    scale: str | None = None,
    encode_args: list[str] | None = None,
    playback_speed: float = 1.0,
    clip_crop: dict | None = None,
    clip_start_ms: int | None = None,
    clip_duration_ms: int | None = None,
) -> None:
    """Run ffmpeg to produce a color-corrected copy of a video.

    Raises RuntimeError with the last ~500 chars of stderr on ffmpeg failure.
    """
    speed = _validated_playback_speed(playback_speed)
    window = _validated_clip_window(clip_start_ms, clip_duration_ms)
    vf = build_cc_filter(
        cc,
        scale=scale,
        playback_speed=speed,
        clip_crop=clip_crop,
    )
    enc = list(encode_args if encode_args is not None else STANDARD_ENCODE_ARGS)
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
        output_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = stderr.decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"ffmpeg color-correct failed: {tail}")
