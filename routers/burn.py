"""
Caption burning router.
Burns overlay PNGs (from html2canvas) onto videos using FFmpeg.
All endpoints are project-scoped via `project` query param.
"""

import asyncio
import base64
import csv
import math
import os
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from project_manager import (
    BASE_DIR,
    PROJECTS_DIR,
    get_project_burn_dir,
    get_project_caption_dir,
    get_project_video_dir,
    sanitize_project_name,
)

router = APIRouter()

FONT_DIR = BASE_DIR / "fonts"


# ── Helpers ──────────────────────────────────────────────────────────


def _scan_project_videos(project: str) -> list[dict]:
    """Recursively find all mp4 files under projects/{name}/videos/."""
    videos = []
    video_dir = get_project_video_dir(project)
    if not video_dir.exists():
        return videos
    for mp4 in sorted(video_dir.rglob("*.mp4")):
        rel = mp4.relative_to(video_dir)
        parts = rel.parts
        folder = str(rel.parent) if len(parts) > 1 else ""
        videos.append(
            {
                "path": str(rel),
                "name": mp4.name,
                "folder": folder,
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
        return "[0:v][1:v]overlay=0:0,scale=1080:1920:flags=lanczos,setsar=1"

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
        return "[0:v][1:v]overlay=0:0"

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
    return f"[0:v]{chain}[corrected];[corrected][1:v]overlay=0:0,scale=1080:1920:flags=lanczos,setsar=1"


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
            png_bytes = base64.b64decode(overlay_png_b64)
            fd, overlay_path = tempfile.mkstemp(suffix=".png")
            os.write(fd, png_bytes)
            os.close(fd)

        filter_complex = _build_filter_complex(color_correction)

        # TikTok-optimized encode: 1080x1920, 30fps, ~15Mbps H.264 High
        tiktok_encode = [
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-maxrate",
            "15M",
            "-bufsize",
            "15M",
            "-profile:v",
            "high",
            "-level",
            "4.2",
            "-r",
            "30",
            "-movflags",
            "+faststart",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
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
            raise RuntimeError(stderr.decode()[-500:])

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


@router.post("/overlay")
async def burn_overlay(request: Request):
    """Receive html2canvas PNG + video path, composite with ffmpeg at full fps.

    Body JSON:
      - project: project name
      - batchId: batch identifier
      - index: video index in batch
      - videoPath: relative path to video within project's videos/ dir
      - overlayPng: base64 PNG from html2canvas (optional)
      - colorCorrection: dict of color correction params (optional)
    """
    body = await request.json()

    project = body.get("project")
    if not project:
        return JSONResponse({"error": "project is required"}, status_code=400)

    batch_id = body["batchId"]
    idx = int(body["index"])
    video_rel = body["videoPath"]
    overlay_b64 = body.get("overlayPng")  # base64 PNG from html2canvas
    color_correction = body.get("colorCorrection")

    try:
        burn_dir = get_project_burn_dir(project)
        burn_dir.mkdir(parents=True, exist_ok=True)
        batch_dir = burn_dir / batch_id
        batch_dir.mkdir(exist_ok=True)

        video_dir = get_project_video_dir(project)
        video_abs = str(video_dir / video_rel)
        mp4_path = str(batch_dir / f"burned_{idx:03d}.mp4")

        if overlay_b64 or color_correction:
            await _burn_video(video_abs, overlay_b64, mp4_path, color_correction)
        else:
            shutil.copy2(video_abs, mp4_path)

        return {
            "index": idx,
            "ok": True,
            "file": f"{batch_id}/burned_{idx:03d}.mp4",
        }
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse(
            {"index": idx, "ok": False, "error": str(e)[:300]},
            status_code=500,
        )


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
        batches.append(
            {
                "id": d.name,
                "count": len(mp4s),
                "created": int(d.stat().st_mtime),
            }
        )
    return {"batches": batches}


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

    zip_path = str(batch_dir / f"{batch_id}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for mp4 in mp4s:
            zf.write(mp4, mp4.name)

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"burned_{batch_id}.zip",
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
    try:
        data = await ws.receive_json()
        project = data.get("project", "quick-test")
        pairs = data.get("pairs", [])

        try:
            video_dir = get_project_video_dir(project)
            burn_dir = get_project_burn_dir(project)
            burn_dir.mkdir(parents=True, exist_ok=True)
        except ValueError as e:
            await ws.send_json({"event": "error", "error": str(e)})
            return

        batch_id = uuid.uuid4().hex[:8]
        batch_dir = burn_dir / batch_id
        batch_dir.mkdir(exist_ok=True)

        total = len(pairs)
        results = []

        for i, pair in enumerate(pairs):
            video_abs = str(video_dir / pair["videoPath"])
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
                results.append(
                    {
                        "index": i,
                        "ok": False,
                        "error": str(e)[:300],
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

        await ws.send_json(
            {
                "event": "complete",
                "batchId": batch_id,
                "results": results,
                "successCount": sum(1 for r in results if r["ok"]),
                "total": total,
            }
        )

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"event": "error", "error": str(e)})
        except Exception:
            pass
