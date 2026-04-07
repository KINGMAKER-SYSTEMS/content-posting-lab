"""
Caption burning router.
Burns overlay PNGs (from html2canvas) onto videos using FFmpeg.
All endpoints are project-scoped via `project` query param.
"""

import asyncio
import base64
import csv
import json as _json
import math
import os
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from project_manager import (
    BASE_DIR,
    PROJECTS_DIR,
    get_project_burn_dir,
    get_project_caption_dir,
    get_project_clips_dir,
    get_project_video_dir,
    sanitize_project_name,
)

router = APIRouter()

FONT_DIR = BASE_DIR / "fonts"

# Limit concurrent ffmpeg burn processes to avoid resource exhaustion
_burn_semaphore = asyncio.Semaphore(4)


def _make_batch_id(project: str, burn_dir: Path, label: str | None = None) -> str:
    """Generate a systematic batch ID: {project}-{MMDDHHmm}-{run}.

    Example: "my-project-04011430-1", "my-project-04011430-2"
    If a label is provided, use: {label}-{MMDDHHmm}-{run}
    """
    ts = datetime.now().strftime("%m%d%H%M")
    prefix = (label or project).lower().replace(" ", "-")[:30]
    # Find next run number for this prefix+timestamp
    run = 1
    if burn_dir.exists():
        for d in burn_dir.iterdir():
            if d.is_dir() and d.name.startswith(f"{prefix}-{ts}-"):
                try:
                    existing_run = int(d.name.rsplit("-", 1)[-1])
                    run = max(run, existing_run + 1)
                except ValueError:
                    pass
    return f"{prefix}-{ts}-{run}"


def _save_batch_meta(batch_dir: Path, meta: dict) -> None:
    """Write batch metadata sidecar JSON."""
    meta_path = batch_dir / "batch_meta.json"
    meta_path.write_text(_json.dumps(meta, indent=2), encoding="utf-8")


def _load_batch_meta(batch_dir: Path) -> dict | None:
    """Load batch metadata if it exists."""
    meta_path = batch_dir / "batch_meta.json"
    if meta_path.exists():
        try:
            return _json.loads(meta_path.read_text(encoding="utf-8"))
        except (_json.JSONDecodeError, OSError):
            pass
    return None


# ── Helpers ──────────────────────────────────────────────────────────


def _extract_job_id(filename: str) -> str | None:
    """Extract the job_id prefix from a generated video filename.

    Filenames follow the pattern: {job_id}_{index}.mp4 or {job_id}_{index}_crop{n}.mp4
    where job_id can be:
    - Legacy hex string (8-12 chars): "a1b2c3d4_0.mp4"
    - New readable format: "grok-stars-and-gal-04011430-a1b2_0.mp4"
    """
    import re
    # New format: everything before the last _N or _N_cropN
    m = re.match(r"^(.+?)_\d+(?:_crop\d+)?\.mp4$", filename)
    return m.group(1) if m else None


def _scan_project_videos(project: str) -> list[dict]:
    """Recursively find all mp4 files under projects/{name}/videos/ and clips/.

    Excludes original source videos when multi-crop variants (_crop0, _crop1, …)
    exist in the same directory, so triptych/dual burns only pick up the crops.

    Groups videos by job run: if a directory contains files from multiple jobs,
    each job gets its own virtual sub-folder so runs aren't merged in the burn UI.
    """
    videos = []
    video_dir = get_project_video_dir(project)
    if video_dir.exists():
        all_mp4s = sorted(video_dir.rglob("*.mp4"))
        # Build a set of stems that have crop variants so we can skip the source
        crop_sources: set[Path] = set()
        for mp4 in all_mp4s:
            if "_crop" in mp4.stem:
                # e.g. "abc_0_crop1" → source stem is "abc_0"
                base_stem = mp4.stem.rsplit("_crop", 1)[0]
                crop_sources.add(mp4.parent / f"{base_stem}.mp4")

        # Group files by directory to detect multi-job folders
        from collections import defaultdict
        dir_jobs: dict[Path, dict[str, list[Path]]] = defaultdict(lambda: defaultdict(list))
        kept_mp4s = [mp4 for mp4 in all_mp4s if mp4 not in crop_sources]
        for mp4 in kept_mp4s:
            job_id = _extract_job_id(mp4.name)
            dir_jobs[mp4.parent][job_id or "unknown"].append(mp4)

        for parent_dir, jobs_in_dir in dir_jobs.items():
            # If only one job in the directory, use the normal folder path
            # If multiple jobs, append /run_{job_id} to split them
            multi_job = len(jobs_in_dir) > 1
            for job_id, mp4s in jobs_in_dir.items():
                for mp4 in mp4s:
                    rel = mp4.relative_to(video_dir)
                    base_folder = str(rel.parent) if len(rel.parts) > 1 else ""
                    if multi_job and job_id != "unknown":
                        folder = f"{base_folder}/run_{job_id}" if base_folder else f"run_{job_id}"
                    else:
                        folder = base_folder
                    videos.append(
                        {
                            "path": str(rel),
                            "name": mp4.name,
                            "folder": folder,
                        }
                    )

    # Also include clips — prefixed with "clips/" so the burn resolver can find them
    clips_dir = get_project_clips_dir(project)
    if clips_dir.exists():
        for mp4 in sorted(clips_dir.rglob("clip_*.mp4")):
            rel = mp4.relative_to(clips_dir)
            # path = "clips/{job_id}/clip_001.mp4", folder = "clips/{job_id}"
            prefixed = Path("clips") / rel
            videos.append(
                {
                    "path": str(prefixed),
                    "name": mp4.name,
                    "folder": str(prefixed.parent),
                }
            )

    return videos


def _load_captions(csv_path: Path) -> list[dict]:
    """Load captions from a CSV file."""
    captions = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            text = (row.get("caption") or "").strip()
            if text:
                captions.append(
                    {
                        "text": text,
                        "video_id": row.get("video_id", ""),
                        "video_url": row.get("video_url", ""),
                    }
                )
    return captions


def _scan_project_captions(project: str) -> list[dict]:
    """Find all caption CSVs in projects/{name}/captions/."""
    sources = []
    caption_dir = get_project_caption_dir(project)
    if not caption_dir.exists():
        return sources
    for user_dir in sorted(caption_dir.iterdir()):
        if not user_dir.is_dir() or user_dir.name.startswith("."):
            continue
        csv_path = user_dir / "captions.csv"
        if csv_path.exists():
            caps = _load_captions(csv_path)
            sources.append(
                {
                    "username": user_dir.name,
                    "csv_path": str(csv_path.relative_to(BASE_DIR)),
                    "count": len(caps),
                    "captions": caps,
                }
            )
    return sources


def _list_fonts() -> list[dict]:
    """List available TikTokSans fonts (only Bold/ExtraBold, no Italic)."""
    fonts = []
    if not FONT_DIR.exists():
        return fonts
    for f in sorted(FONT_DIR.glob("TikTokSans*.ttf")):
        if "Italic" in f.name:
            continue
        name = (
            f.stem.replace("TikTokSans", "TikTok Sans ")
            .replace("16pt-", "")
            .replace("12pt-", "")
            .replace("36pt-", "")
        )
        fonts.append({"file": f.name, "name": name.strip()})
    return fonts


# ── FFmpeg Pipeline (preserved exactly from burn_server.py) ──────────


def _build_filter_complex(color_correction: dict | None = None) -> str:
    """Build ffmpeg filter_complex that replicates CSS filter behavior.

    All color transforms are composed into a SINGLE colorchannelmixer filter
    to avoid multiple YUV<>RGB conversions that degrade video quality.
    Each CSS filter (brightness, contrast, saturate, sepia, hue-rotate) is
    a linear per-pixel transform expressible as a 3x3 matrix + offset.
    We pre-multiply them into one combined matrix.
    """
    if not color_correction:
        return "[0:v]scale=1080:1920:flags=lanczos,setsar=1[vid];[1:v]scale=1080:1920:flags=lanczos[ovr];[vid][ovr]overlay=0:0"

    # Raw slider integers
    b_raw = float(color_correction.get("brightness", 0))
    c_raw = float(color_correction.get("contrast", 0))
    s_raw = float(color_correction.get("saturation", 0))
    sh_raw = float(color_correction.get("sharpness", 0))
    sd_raw = float(color_correction.get("shadow", 0))
    t_raw = float(color_correction.get("temperature", 0))
    ti_raw = float(color_correction.get("tint", 0))
    f_raw = float(color_correction.get("fade", 0))

    # Map to CSS-equivalent values
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

    is_default = (
        abs(css_brightness - 1.0) < 0.005
        and abs(css_contrast - 1.0) < 0.005
        and abs(css_saturate - 1.0) < 0.005
        and abs(t_raw) <= 1
        and abs(ti_raw) <= 1
        and sharpness < 0.001
    )
    if is_default:
        return "[0:v]scale=1080:1920:flags=lanczos,setsar=1[vid];[1:v]scale=1080:1920:flags=lanczos[ovr];[vid][ovr]overlay=0:0"

    # --- Compose all transforms into one 3x3 matrix + offset ---
    mat = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    off = [0.0, 0.0, 0.0]

    def mat_mul(a: list, b: list) -> list:
        return [
            [sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)
        ]

    def mat_vec(m: list, v: list) -> list:
        return [sum(m[i][j] * v[j] for j in range(3)) for i in range(3)]

    # CSS brightness(b): out = in * b
    if abs(css_brightness - 1.0) >= 0.005:
        b = css_brightness
        mat = [[b * mat[i][j] for j in range(3)] for i in range(3)]
        off = [b * o for o in off]

    # CSS contrast(c): out = (in - 0.5) * c + 0.5 = in*c + 0.5*(1-c)
    if abs(css_contrast - 1.0) >= 0.005:
        c = css_contrast
        bias = 0.5 * (1 - c)
        mat = [[c * mat[i][j] for j in range(3)] for i in range(3)]
        off = [c * o + bias for o in off]

    # CSS saturate(s): BT.709 saturation matrix
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

    # Temperature: warm = CSS sepia(), cool = CSS hue-rotate(negative deg)
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

    # Tint: CSS hue-rotate
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

    # --- Build filter string ---
    ccm = (
        f"colorchannelmixer="
        f"rr={mat[0][0]:.6f}:rg={mat[0][1]:.6f}:rb={mat[0][2]:.6f}:ra={off[0]:.6f}:"
        f"gr={mat[1][0]:.6f}:gg={mat[1][1]:.6f}:gb={mat[1][2]:.6f}:ga={off[1]:.6f}:"
        f"br={mat[2][0]:.6f}:bg={mat[2][1]:.6f}:bb={mat[2][2]:.6f}:ba={off[2]:.6f}"
    )

    filters = ["format=rgb24", ccm]

    if sharpness >= 0.001:
        filters.append(f"unsharp=5:5:{sharpness:.2f}:5:5:{sharpness:.2f}")

    chain = ",".join(filters)
    return f"[0:v]{chain},scale=1080:1920:flags=lanczos,setsar=1[corrected];[1:v]scale=1080:1920:flags=lanczos[ovr];[corrected][ovr]overlay=0:0"


def _build_color_only_filter(color_correction: dict | None) -> str:
    """Build a -vf filter string for color correction only (no overlay input)."""
    TIKTOK_SCALE = "scale=1080:1920:flags=lanczos,setsar=1"

    if not color_correction:
        return TIKTOK_SCALE

    b_raw = float(color_correction.get("brightness", 0))
    c_raw = float(color_correction.get("contrast", 0))
    s_raw = float(color_correction.get("saturation", 0))
    sh_raw = float(color_correction.get("sharpness", 0))
    sd_raw = float(color_correction.get("shadow", 0))
    t_raw = float(color_correction.get("temperature", 0))
    ti_raw = float(color_correction.get("tint", 0))
    f_raw = float(color_correction.get("fade", 0))

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

    is_default = (
        abs(css_brightness - 1.0) < 0.005
        and abs(css_contrast - 1.0) < 0.005
        and abs(css_saturate - 1.0) < 0.005
        and abs(t_raw) <= 1
        and abs(ti_raw) <= 1
        and sharpness < 0.001
    )
    if is_default:
        return TIKTOK_SCALE

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
    filters.append(TIKTOK_SCALE)

    return ",".join(filters)


async def _burn_video(
    video_path: str,
    overlay_png_b64: str | None,
    output_path: str,
    color_correction: dict | None = None,
) -> str:
    """Burn overlay onto video using a browser-rendered PNG + ffmpeg color correction.

    overlay_png_b64: base64-encoded PNG from browser canvas (text overlay at full video res).
                     If None/empty, only color correction is applied.
    """
    overlay_path = None

    try:
        # Write browser-rendered overlay PNG to temp file
        if overlay_png_b64:
            # Strip data URL prefix if present (e.g. "data:image/png;base64,...")
            if "," in overlay_png_b64:
                overlay_png_b64 = overlay_png_b64.split(",", 1)[1]
            # Guard against OOM from oversized payloads (50MB base64 ≈ 37.5MB decoded)
            if len(overlay_png_b64) > 50_000_000:
                raise ValueError("Overlay PNG too large (>50MB base64). Reduce overlay resolution.")
            png_bytes = base64.b64decode(overlay_png_b64)
            fd, overlay_path = tempfile.mkstemp(suffix=".png")
            os.write(fd, png_bytes)
            os.close(fd)

        filter_complex = _build_filter_complex(color_correction)

        # TikTok-optimized encode: 1080x1920, 30fps, H.264 High
        # -crf 18 + medium preset = high quality with 2-3x faster encoding
        # -minrate 8M ensures the encoder doesn't produce low-bitrate output
        # even for simple scenes — keeps text overlays crisp on re-upload
        tiktok_encode = [
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-minrate",
            "8M",
            "-maxrate",
            "20M",
            "-bufsize",
            "20M",
            "-profile:v",
            "high",
            "-level",
            "4.2",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-movflags",
            "+faststart",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
        ]

        if overlay_path:
            # Have overlay — use it
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                video_path,
                "-i",
                overlay_path,
                "-filter_complex",
                filter_complex,
                *tiktok_encode,
                output_path,
            ]
        else:
            # No overlay — just apply color correction to the video directly
            cc_filter = _build_color_only_filter(color_correction)
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                video_path,
                "-vf",
                cc_filter,
                *tiktok_encode,
                output_path,
            ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(stderr.decode()[-2000:])

        return output_path

    finally:
        if overlay_path and os.path.exists(overlay_path):
            os.unlink(overlay_path)


# ── API Routes ───────────────────────────────────────────────────────


@router.get("/videos")
async def list_videos(project: str = Query(..., description="Project name")):
    """List all videos in project's videos/ directory, grouped by folder."""
    try:
        return {"videos": _scan_project_videos(project)}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.get("/captions")
async def list_captions(project: str = Query(..., description="Project name")):
    """List caption CSVs from project's captions/ directory."""
    try:
        return {"sources": _scan_project_captions(project)}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.get("/fonts")
async def list_fonts():
    """List available fonts from fonts/ directory."""
    return {"fonts": _list_fonts()}


# ── Background burn job tracking ────────────────────────────────────
# Stores per-batch item status so the frontend can poll progress.
# Each batch_id → {index: {"status": "queued"|"burning"|"done"|"error", ...}}
_burn_jobs: dict[str, dict[int, dict]] = {}


async def _burn_background(
    batch_id: str,
    idx: int,
    video_abs: str,
    overlay_b64: str | None,
    mp4_path: str,
    color_correction: dict | None,
    video_rel: str,
) -> None:
    """Run a single burn in the background, updating _burn_jobs status."""
    _burn_jobs.setdefault(batch_id, {})[idx] = {"status": "burning"}
    try:
        if overlay_b64 or color_correction:
            async with _burn_semaphore:
                await _burn_video(video_abs, overlay_b64, mp4_path, color_correction)
        else:
            shutil.copy2(video_abs, mp4_path)

        out_file = f"{batch_id}/burned_{idx:03d}.mp4"
        print(f"[burn] OK #{idx} -> {out_file}", flush=True)
        _burn_jobs[batch_id][idx] = {
            "status": "done",
            "index": idx,
            "ok": True,
            "file": out_file,
        }
    except Exception as e:
        import traceback
        print(f"[burn] FAIL #{idx} Exception: {e}", flush=True)
        traceback.print_exc()
        _burn_jobs[batch_id][idx] = {
            "status": "error",
            "index": idx,
            "ok": False,
            "error": str(e)[:2000],
        }


@router.post("/overlay")
async def burn_overlay(request: Request):
    """Accept a burn request and process it in the background.

    Returns immediately with {"index": N, "ok": true, "status": "queued"}.
    Frontend polls GET /batch-status/{batch_id} for progress.
    This avoids Railway's ~30s proxy timeout on long ffmpeg operations.
    """
    body = await request.json()

    project = body.get("project")
    if not project:
        return JSONResponse({"error": "project is required"}, status_code=400)

    batch_id = body.get("batchId") or body.get("batch_id", "")
    idx = int(body["index"])
    video_rel = body["videoPath"]
    overlay_b64 = body.get("overlayPng")
    color_correction = body.get("colorCorrection")

    print(f"[burn] overlay #{idx} project={project} batch={batch_id} video={video_rel} overlay={'yes' if overlay_b64 else 'no'} cc={'yes' if color_correction else 'no'}", flush=True)

    try:
        burn_dir = get_project_burn_dir(project)
        burn_dir.mkdir(parents=True, exist_ok=True)
        batch_dir = burn_dir / batch_id
        batch_dir.mkdir(exist_ok=True)

        if video_rel.startswith("clips/"):
            project_dir = PROJECTS_DIR / sanitize_project_name(project)
            video_abs = str(project_dir / video_rel)
        else:
            video_dir = get_project_video_dir(project)
            video_abs = str(video_dir / video_rel)

        if not Path(video_abs).exists():
            print(f"[burn] ERROR #{idx}: video not found: {video_abs}", flush=True)
            return JSONResponse({"index": idx, "ok": False, "error": f"Video not found: {video_rel}"}, status_code=404)

        mp4_path = str(batch_dir / f"burned_{idx:03d}.mp4")

        # Track as queued and fire background task
        _burn_jobs.setdefault(batch_id, {})[idx] = {"status": "queued"}
        asyncio.create_task(
            _burn_background(batch_id, idx, video_abs, overlay_b64, mp4_path, color_correction, video_rel)
        )

        return {"index": idx, "ok": True, "status": "queued"}

    except ValueError as e:
        print(f"[burn] FAIL #{idx} ValueError: {e}", flush=True)
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        import traceback
        print(f"[burn] FAIL #{idx} Exception: {e}", flush=True)
        traceback.print_exc()
        return JSONResponse(
            {"index": idx, "ok": False, "error": str(e)[:2000]},
            status_code=500,
        )


@router.get("/batch-status/{batch_id}")
async def batch_status(batch_id: str):
    """Poll endpoint for burn batch progress.

    Returns the status of all items in a batch. Each item has:
      - status: "queued" | "burning" | "done" | "error"
      - index, ok, file (when done)
      - error (when error)
    """
    items = _burn_jobs.get(batch_id, {})
    return {
        "batchId": batch_id,
        "items": items,
        "total": len(items),
        "done": sum(1 for v in items.values() if v["status"] in ("done", "error")),
        "ok": sum(1 for v in items.values() if v.get("ok")),
        "failed": sum(1 for v in items.values() if v["status"] == "error"),
    }


@router.get("/batches")
async def list_batches(project: str = Query(..., description="Project name")):
    """List all past burn batches with file counts and timestamps."""
    try:
        burn_dir = get_project_burn_dir(project)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    batches = []
    if not burn_dir.exists():
        return {"batches": batches}

    for d in sorted(burn_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        mp4s = list(d.glob("burned_*.mp4"))
        if not mp4s:
            continue
        meta = _load_batch_meta(d)
        batches.append(
            {
                "id": d.name,
                "label": meta.get("label") if meta else None,
                "count": len(mp4s),
                "created": int(d.stat().st_mtime),
            }
        )
    return {"batches": batches}


@router.patch("/batches/{batch_id}/rename")
async def rename_batch(
    batch_id: str,
    body: dict,
    project: str = Query(..., description="Project name"),
):
    """Rename a burn batch by updating its metadata label."""
    new_label = (body.get("label") or "").strip()
    if not new_label:
        return JSONResponse({"error": "Label is required"}, status_code=400)
    try:
        burn_dir = get_project_burn_dir(project)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    batch_dir = burn_dir / batch_id
    if not batch_dir.exists():
        return JSONResponse({"error": "Batch not found"}, status_code=404)

    meta = _load_batch_meta(batch_dir) or {"batch_id": batch_id, "project": project}
    meta["label"] = new_label
    _save_batch_meta(batch_dir, meta)
    return {"ok": True, "label": new_label}


@router.get("/zip/{batch_id}")
async def download_burn_zip(
    batch_id: str,
    project: str = Query(..., description="Project name"),
):
    """Zip all burned MP4s in a batch and return the archive."""
    try:
        burn_dir = get_project_burn_dir(project)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    batch_dir = burn_dir / batch_id
    if not batch_dir.exists():
        return JSONResponse({"error": "Batch not found"}, status_code=404)

    mp4s = sorted(batch_dir.glob("burned_*.mp4"))
    if not mp4s:
        return JSONResponse({"error": "No burned files in batch"}, status_code=404)

    # Use label from metadata for ZIP filename if available
    meta = _load_batch_meta(batch_dir)
    zip_label = meta.get("label") if meta else None
    zip_name = f"{zip_label or batch_id}.zip"

    # Write ZIP to temp file to avoid in-memory spikes for large batches
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    os.close(tmp_fd)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for mp4 in mp4s:
                zf.write(mp4, mp4.name)
    except Exception:
        os.unlink(tmp_path)
        raise

    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=zip_name,
        background=None,  # file cleanup handled by FileResponse
    )


# ── WebSocket for batch burn with progress (legacy) ──────────────────


@router.websocket("/ws")
async def ws_burn(ws: WebSocket):
    """Legacy WebSocket endpoint for batch burning with real-time progress.

    Client sends JSON:
      {
        "project": "project-name",
        "pairs": [
          {"videoPath": "rel/path.mp4", "overlayPng": "base64...", "colorCorrection": {...}},
          ...
        ]
      }
    """
    await ws.accept()
    print(f"[burn] WS connected", flush=True)
    try:
        data = await ws.receive_json()
        project = data.get("project", "quick-test")
        pairs = data.get("pairs", [])
        label = data.get("label")  # optional user-provided label

        try:
            video_dir = get_project_video_dir(project)
            burn_dir = get_project_burn_dir(project)
            burn_dir.mkdir(parents=True, exist_ok=True)
        except ValueError as e:
            await ws.send_json({"event": "error", "error": str(e)})
            return

        batch_id = _make_batch_id(project, burn_dir, label)
        batch_dir = burn_dir / batch_id
        batch_dir.mkdir(exist_ok=True)

        # Persist batch metadata
        _save_batch_meta(batch_dir, {
            "batch_id": batch_id,
            "project": project,
            "label": label,
            "created": datetime.now().isoformat(),
            "total": len(pairs),
        })

        total = len(pairs)
        results = []

        # Keepalive: send pings every 30s to prevent proxy/browser timeout
        async def keepalive():
            try:
                while True:
                    await asyncio.sleep(30)
                    await ws.send_json({"event": "ping"})
            except Exception:
                pass

        keepalive_task = asyncio.create_task(keepalive())

        for i, pair in enumerate(pairs):
            vp = pair["videoPath"]
            # clips/ paths resolve from project root, regular paths from videos/
            if vp.startswith("clips/"):
                project_dir = PROJECTS_DIR / sanitize_project_name(project)
                video_abs = str(project_dir / vp)
            else:
                video_abs = str(video_dir / vp)
            overlay_png = pair.get("overlayPng")  # base64 PNG from browser canvas
            color_correction = pair.get("colorCorrection")
            out_name = f"burned_{i:03d}.mp4"
            out_path = str(batch_dir / out_name)

            await ws.send_json(
                {
                    "event": "burning",
                    "index": i,
                    "total": total,
                }
            )

            try:
                if overlay_png or color_correction:
                    await _burn_video(
                        video_abs,
                        overlay_png,
                        out_path,
                        color_correction,
                    )
                else:
                    shutil.copy2(video_abs, out_path)

                results.append(
                    {
                        "index": i,
                        "ok": True,
                        "file": f"{batch_id}/{out_name}",
                    }
                )
            except Exception as e:
                print(f"[burn] burn failed for pair {i}: {e}", flush=True)
                results.append(
                    {
                        "index": i,
                        "ok": False,
                        "error": str(e)[:2000],
                    }
                )

            await ws.send_json(
                {
                    "event": "burned",
                    "index": i,
                    "total": total,
                    "result": results[-1],
                }
            )

        keepalive_task.cancel()

        await ws.send_json(
            {
                "event": "complete",
                "batchId": batch_id,
                "results": results,
                "successCount": sum(1 for r in results if r["ok"]),
                "total": total,
            }
        )

        # Auto-upload burned videos to linked Drive folders
        try:
            from services.gdrive import is_configured as drive_configured, upload_file as drive_upload
            from services.roster import list_pages_for_project

            if drive_configured():
                pages = list_pages_for_project(project)
                drive_folders = [p["drive_folder_id"] for p in pages if p.get("drive_folder_id")]
                if drive_folders:
                    burned_files = [
                        str(batch_dir / r["file"].split("/")[-1])
                        for r in results
                        if r.get("ok") and r.get("file")
                    ]
                    uploaded = 0
                    for fp in burned_files:
                        for fid in drive_folders:
                            try:
                                drive_upload(fid, fp)
                                uploaded += 1
                            except Exception as de:
                                print(f"[burn] Drive upload failed: {de}", flush=True)
                    if uploaded > 0:
                        await ws.send_json(
                            {"event": "drive_uploaded", "count": uploaded, "folders": len(drive_folders)}
                        )
        except ImportError:
            pass  # Drive deps not installed
        except Exception as de:
            print(f"[burn] Drive auto-upload error: {de}", flush=True)

    except WebSocketDisconnect:
        print(f"[burn] WS disconnected", flush=True)
    except Exception as e:
        print(f"[burn] WS pipeline error: {e}", flush=True)
        try:
            await ws.send_json({"event": "error", "error": str(e)})
        except Exception:
            pass
