#!/usr/bin/env python3
"""Post-deploy colour render proof for `fix/contrast-matches-preview`.

Proves that the LIVE Lab colour path reproduces the Dossier CSS preview for a
given look, within the tolerance documented in `docs/contrast-render-proof.md`
(MAE <= 1.0 and p99 <= 5 per 8-bit RGB channel).

Two modes:

* live (default) — extract a 1-2 s clip from a real master, send it through the
  Lab's HTTP colour endpoint, decode the returned frame and compare it to the
  CSS preview math applied to the same source frame.
* `--local` — run the identical comparison through
  `services.ffmpeg.run_color_correct` in-process (no network), so the comparison
  is testable offline.

Which endpoint and why
----------------------
The live mode POSTs to `POST /api/video/color-correct` (routers/video.py:776).
That is the Lab's synchronous single-video colour endpoint, and it routes through
`services.ffmpeg.run_color_correct(src, tmp, cc, scale=None)` — the exact same
shared `build_cc_filter` colour math that the Worker's production render
(routers/control_plane.py) applies to posted clips (`run_color_correct(...,
scale=None, encode_args=delivery_encode_args(...))`). The Burn `/overlay` route
is a background batch job (returns "queued", polled separately) that forces
`scale=1080:1920` and exists for caption-overlay compositing, so it is not the
pure colour path and would resize an arbitrary master. `build_cc_filter` is the
single source of truth for the colour math in both, which is what this proof
verifies; the delivery-preset difference (STANDARD vs tiktok_delivery_v1) is
codec rounding only and is absorbed by the proof's tolerance.

The API key is read from `APP_API_KEY` (the env var the Lab's own `/api/*` key
middleware checks in `app.py:232`) and is never printed.

The Lab's colour endpoints resolve server-side project video paths (no anonymous
byte upload), so the 1-2 s clip must already be reachable at
`<project>/videos/<remote-path>` on the Lab. See docs/contrast-render-proof.md
"After deploy" for the one-line command.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# Allow running from any cwd: the repo root is two levels above this file, and
# `services` is a repo-internal dependency (stdlib-only for the colour math).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_LOOK = "brightness=0.7,contrast=1.95,saturation=1.65"
API_KEY_ENV = "APP_API_KEY"

# Acceptance tolerance from docs/contrast-render-proof.md.
MAE_TOLERANCE = 1.0
P99_TOLERANCE = 5


# --- Dossier CSS preview math ----------------------------------------------
# Copied verbatim from tests/test_ffmpeg_cc.py (_matrix, _css_preview_pixel)
# and services/ffmpeg.py (_saturation_matrix) so the proof evaluates the exact
# same ordered CSS operations the new pixel tests assert against the ffmpeg
# filter stages. Do not "improve" these independently of those two files.


def _matrix(pixel: list[float], matrix: list[list[float]]) -> list[float]:
    return [sum(row[index] * pixel[index] for index in range(3)) for row in matrix]


def _saturation_matrix(value: float) -> list[list[float]]:
    sr, sg, sb = 0.2126, 0.7152, 0.0722
    return [
        [sr + (1 - sr) * value, sg - sg * value, sb - sb * value],
        [sr - sr * value, sg + (1 - sg) * value, sb - sb * value],
        [sr - sr * value, sg - sg * value, sb + (1 - sb) * value],
    ]


def css_preview_pixel(source: tuple[int, int, int], look: dict) -> tuple[int, int, int]:
    """Evaluate the CSS filter functions emitted by applyCSSFilterPreview."""
    brightness = float(look.get("brightness", 1.0))
    contrast = float(look.get("contrast", 1.0))
    saturation = float(look.get("saturation", 1.0))
    fade = float(look.get("fade", 0.0))
    if fade > 0:
        brightness = min(2.0, brightness + fade * 0.4)
        contrast = max(0.2, contrast - fade * 0.3)
        saturation = max(0.2, saturation - fade * 0.4)

    def clamp(values):
        # CSS filter functions are separate primitives; each primitive's
        # output is clamped before the next function consumes it.
        return [min(1.0, max(0.0, channel)) for channel in values]

    pixel = [channel / 255 for channel in source]
    pixel = clamp([brightness * channel for channel in pixel])
    pixel = clamp([contrast * channel + 0.5 * (1 - contrast) for channel in pixel])
    pixel = clamp(_matrix(pixel, _saturation_matrix(saturation)))

    warmth = float(look.get("warmth", 0.0))
    if warmth > 0:
        amount = min(1.0, warmth / 2)
        pixel = _matrix(pixel, [
            [1 - amount + amount * 0.393, amount * 0.769, amount * 0.189],
            [amount * 0.349, 1 - amount + amount * 0.686, amount * 0.168],
            [amount * 0.272, amount * 0.534, 1 - amount + amount * 0.131],
        ])
    elif warmth < 0:
        angle = math.radians(warmth * 20)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        pixel = _matrix(pixel, [
            [0.213 + 0.787 * cos_a - 0.213 * sin_a, 0.715 - 0.715 * cos_a - 0.715 * sin_a, 0.072 - 0.072 * cos_a + 0.928 * sin_a],
            [0.213 - 0.213 * cos_a + 0.143 * sin_a, 0.715 + 0.285 * cos_a + 0.140 * sin_a, 0.072 - 0.072 * cos_a - 0.283 * sin_a],
            [0.213 - 0.213 * cos_a - 0.787 * sin_a, 0.715 - 0.715 * cos_a + 0.715 * sin_a, 0.072 + 0.928 * cos_a + 0.072 * sin_a],
        ])
    return tuple(round(255 * min(1.0, max(0.0, channel))) for channel in pixel)


def dossier_cc(look: dict) -> dict:
    """Map a Dossier CSS look to the Lab's colour_correction slider dict.

    Copied verbatim from tests/test_ffmpeg_cc.py (_dossier_cc), which notes it
    mirrors services.control_plane_generation.dossier_filters_to_color_correction.
    """
    cc = {
        key: (float(look[key]) - 1.0) * 100.0
        for key in ("brightness", "contrast", "saturation")
        if key in look
    }
    if "warmth" in look:
        cc["temperature"] = float(look["warmth"]) * 100.0
    if "fade" in look:
        cc["fade"] = float(look["fade"]) * 100.0
    return cc


# --- Comparison math (pure; unit-tested without ffmpeg/network) ------------


def css_preview_rgb(source_rgb: bytes, look: dict) -> bytes:
    """Apply the CSS preview math to every pixel of an RGB24 buffer."""
    out = bytearray(len(source_rgb))
    for index in range(0, len(source_rgb), 3):
        r, g, b = css_preview_pixel(
            (source_rgb[index], source_rgb[index + 1], source_rgb[index + 2]), look
        )
        out[index], out[index + 1], out[index + 2] = r, g, b
    return bytes(out)


def channel_errors(expected_rgb: bytes, output_rgb: bytes) -> list[int]:
    """Per-channel absolute error between two equal-length RGB24 buffers."""
    if len(expected_rgb) != len(output_rgb):
        raise ValueError(
            f"frame byte counts differ: {len(expected_rgb)} vs {len(output_rgb)}"
        )
    return [abs(a - b) for a, b in zip(expected_rgb, output_rgb)]


def _percentile(sorted_values: list[int], p: float) -> float:
    """numpy-style linear interpolation of the p-th percentile (0..1)."""
    if not sorted_values:
        return 0.0
    if p <= 0:
        return sorted_values[0]
    if p >= 1:
        return sorted_values[-1]
    k = (len(sorted_values) - 1) * p
    lower = math.floor(k)
    upper = math.ceil(k)
    if lower == upper:
        return sorted_values[int(k)]
    return sorted_values[lower] * (upper - k) + sorted_values[upper] * (k - lower)


def summarize_errors(errors: list[int]) -> dict:
    """MAE / p95 / p99 / max of a per-channel absolute-error list."""
    if not errors:
        raise ValueError("empty error list")
    ordered = sorted(errors)
    count = len(errors)
    return {
        "samples": count,
        "mae": sum(errors) / count,
        "p95": _percentile(ordered, 0.95),
        "p99": _percentile(ordered, 0.99),
        "max": ordered[-1],
    }


def passes(
    metrics: dict,
    mae_tolerance: float = MAE_TOLERANCE,
    p99_tolerance: float = P99_TOLERANCE,
) -> bool:
    """True when MAE and p99 sit inside the proof tolerance."""
    return metrics["mae"] <= mae_tolerance and metrics["p99"] <= p99_tolerance


def evaluate(source_rgb: bytes, output_rgb: bytes, look: dict) -> tuple[dict, bool]:
    """Compare an output frame to the CSS preview math applied to a source frame."""
    expected = css_preview_rgb(source_rgb, look)
    metrics = summarize_errors(channel_errors(expected, output_rgb))
    return metrics, passes(metrics)


# --- CLI helpers -----------------------------------------------------------


def parse_look(spec: str) -> dict:
    """Parse ``brightness=0.7,contrast=1.95,...`` into a float dict."""
    look: dict = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"invalid look item {part!r} (expected key=value)")
        key, _, value = part.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"invalid look item {part!r} (empty key)")
        try:
            look[key] = float(value)
        except ValueError:
            raise ValueError(f"non-numeric look value {part!r}") from None
    if not look:
        raise ValueError("empty look")
    return look


def run_ffmpeg(args: list[str]) -> bytes:
    """Run ffmpeg (quiet), returning its stdout bytes."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not available on PATH")
    completed = subprocess.run(
        ["ffmpeg", "-v", "error", *args],
        check=True,
        capture_output=True,
    )
    return completed.stdout


def decode_frame_rgb(path: str) -> bytes:
    """Decode the first frame of an mp4 to raw RGB24 bytes."""
    return run_ffmpeg(
        ["-i", path, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    )


def extract_clip(master: str, at: float, duration: float, dest: str) -> None:
    """Extract a frame-accurate clip at `at` seconds for `duration` seconds.

    Input seeking (`-ss` before `-i`) plus a re-encode yields a first output
    frame that is exactly the master's frame at `at`, which is what the proof
    compares against. `-crf 18` keeps the clip high quality so the source frame
    the colour path receives stays close to the master's true pixels. When
    running live mode, upload a clip extracted with the SAME command so the
    server's input bytes match the local source frame.
    """
    run_ffmpeg(
        [
            "-y",
            "-ss",
            f"{at:.3f}",
            "-i",
            master,
            "-t",
            f"{duration:.3f}",
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-an",
            dest,
        ]
    )


def render_local(clip_path: str, output_path: str, cc: dict) -> None:
    """Colour-correct in-process, mirroring /api/video/color-correct (scale=None)."""
    import asyncio

    from services.ffmpeg import run_color_correct

    asyncio.run(run_color_correct(clip_path, output_path, cc, scale=None))


def render_live(
    lab_url: str,
    api_key: str,
    project: str,
    remote_path: str,
    cc: dict,
    output_path: str,
) -> None:
    """POST the clip (already present on the Lab) through /api/video/color-correct."""
    url = f"{lab_url.rstrip('/')}/api/video/color-correct"
    payload = {"project": project, "path": remote_path, "color_correction": cc}
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Lab returned HTTP {error.code} for {url}: {detail[-500:]}"
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"could not reach {url}: {error}") from error
    with open(output_path, "wb") as handle:
        handle.write(body)


def _format_metrics(metrics: dict) -> list[str]:
    return [
        f"samples {metrics['samples']}",
        f"MAE {metrics['mae']:.3f}",
        f"p95 {round(metrics['p95'])}",
        f"p99 {round(metrics['p99'])}",
        f"max {metrics['max']}",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Post-deploy colour render proof (fix/contrast-matches-preview)."
    )
    parser.add_argument(
        "--lab-url",
        default=None,
        help="the deployed Lab base URL (required for the live mode; no host is built in)",
    )
    parser.add_argument("--master", required=True, help="path to a real master mp4")
    parser.add_argument(
        "--look",
        default=DEFAULT_LOOK,
        help="Dossier CSS look, e.g. brightness=0.7,contrast=1.95,saturation=1.65",
    )
    parser.add_argument("--at", type=float, default=2.0, help="clip start time (s)")
    parser.add_argument(
        "--duration", type=float, default=1.0, help="clip length in seconds (1-2)"
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="run the comparison through run_color_correct in-process (no network)",
    )
    parser.add_argument(
        "--project",
        default="contrast-proof",
        help="Lab project whose videos/ dir holds the remote clip (live mode)",
    )
    parser.add_argument(
        "--remote-path",
        default=None,
        help="server-side clip path under <project>/videos/ (default: master basename)",
    )
    parser.add_argument(
        "--api-key-env", default=API_KEY_ENV, help="env var holding the Lab API key"
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the temp clip/output for inspection"
    )
    args = parser.parse_args(argv)

    if not 1.0 <= args.duration <= 2.0:
        parser.error("--duration must be between 1 and 2 seconds")
    if args.at < 0:
        parser.error("--at must be >= 0")

    master = Path(args.master)
    if not master.is_file():
        print(f"master not found: {master}", file=sys.stderr)
        return 2

    try:
        look = parse_look(args.look)
    except ValueError as error:
        print(f"invalid --look: {error}", file=sys.stderr)
        return 2
    cc = dossier_cc(look)

    tmpdir = tempfile.mkdtemp(prefix="contrast-render-proof-")
    clip_path = os.path.join(tmpdir, "clip.mp4")
    output_path = os.path.join(tmpdir, "output.mp4")
    try:
        extract_clip(str(master), args.at, args.duration, clip_path)

        if not args.local and not args.lab_url:
            print("error: --lab-url is required for the live mode", file=sys.stderr)
            return 2
        if args.local:
            render_local(clip_path, output_path, cc)
        else:
            api_key = os.environ.get(args.api_key_env, "")
            if not api_key:
                print(
                    f"error: {args.api_key_env} is not set in the environment "
                    "(the Lab's /api/* key middleware requires it)",
                    file=sys.stderr,
                )
                return 2
            remote_path = args.remote_path or master.name
            render_live(
                args.lab_url, api_key, args.project, remote_path, cc, output_path
            )

        source_rgb = decode_frame_rgb(clip_path)
        output_rgb = decode_frame_rgb(output_path)
        if len(source_rgb) != len(output_rgb):
            print(
                f"error: decoded frame sizes differ "
                f"({len(source_rgb)} vs {len(output_rgb)} bytes)",
                file=sys.stderr,
            )
            return 2

        metrics, ok = evaluate(source_rgb, output_rgb, look)
        print(f"look {args.look}")
        mode = "local" if args.local else "live"
        route = "run_color_correct in-process" if args.local else "/api/video/color-correct"
        print(f"mode {mode} (colour path: {route})")
        for line in _format_metrics(metrics):
            print(line)
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        if not args.keep:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
