"""
Caption Burn Server
Burns scraped captions onto generated videos using browser-rendered overlays + FFmpeg.
Run: python burn_server.py  (serves on port 8002)
"""

import asyncio
import base64
import csv
import os
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

BASE_DIR = Path(__file__).parent
VIDEO_DIR = BASE_DIR / "output"
CAPTION_DIR = BASE_DIR / "caption_output"
BURN_DIR = BASE_DIR / "burn_output"
FONT_DIR = BASE_DIR / "fonts"

BURN_DIR.mkdir(exist_ok=True)


# ── Helpers ──────────────────────────────────────────────────────────


def scan_videos() -> list[dict]:
    """Recursively find all mp4 files under video-output/.
    Groups by full subfolder path (e.g. 'rep-minimax/prompt_slug')."""
    videos = []
    if not VIDEO_DIR.exists():
        return videos
    for mp4 in sorted(VIDEO_DIR.rglob("*.mp4")):
        rel = mp4.relative_to(VIDEO_DIR)
        parts = rel.parts
        # folder = full parent path (e.g. "rep-minimax/a_black_ford_pickup...")
        folder = str(rel.parent) if len(parts) > 1 else ""
        videos.append({
            "path": str(rel),
            "name": mp4.name,
            "folder": folder,
        })
    return videos


def load_captions(csv_path: Path) -> list[dict]:
    """Load captions from a CSV file."""
    captions = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            text = (row.get("caption") or "").strip()
            if text:
                captions.append({
                    "text": text,
                    "video_id": row.get("video_id", ""),
                    "video_url": row.get("video_url", ""),
                })
    return captions


def scan_caption_sources() -> list[dict]:
    """Find all caption CSVs in caption_output/."""
    sources = []
    if not CAPTION_DIR.exists():
        return sources
    for user_dir in sorted(CAPTION_DIR.iterdir()):
        if not user_dir.is_dir() or user_dir.name.startswith("."):
            continue
        csv_path = user_dir / "captions.csv"
        if csv_path.exists():
            caps = load_captions(csv_path)
            sources.append({
                "username": user_dir.name,
                "csv_path": str(csv_path.relative_to(BASE_DIR)),
                "count": len(caps),
                "captions": caps,
            })
    return sources


def list_fonts() -> list[dict]:
    """List available TikTokSans fonts (only Bold/ExtraBold, no Italic)."""
    fonts = []
    if not FONT_DIR.exists():
        return fonts
    for f in sorted(FONT_DIR.glob("TikTokSans*.ttf")):
        if "Italic" in f.name:
            continue
        name = f.stem.replace("TikTokSans", "TikTok Sans ").replace("16pt-", "").replace("12pt-", "").replace("36pt-", "")
        fonts.append({"file": f.name, "name": name.strip()})
    return fonts


def build_filter_complex(color_correction: dict | None = None) -> str:
    """Build ffmpeg filter_complex that replicates CSS filter behavior.

    All color transforms are composed into a SINGLE colorchannelmixer filter
    to avoid multiple YUV↔RGB conversions that degrade video quality.
    Each CSS filter (brightness, contrast, saturate, sepia, hue-rotate) is
    a linear per-pixel transform expressible as a 3x3 matrix + offset.
    We pre-multiply them into one combined matrix.
    """
    import math

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

    # Map to CSS-equivalent values (same math as frontend applyCSSFilterPreview)
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
    # colorchannelmixer: out_r = in_r*rr + in_g*rg + in_b*rb + ra
    # where ra/ga/ba are offsets (fraction of full range, i.e. 0-1 maps to 0-255)

    mat = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    off = [0.0, 0.0, 0.0]

    def mat_mul(a, b):
        return [
            [sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)
        ]

    def mat_vec(m, v):
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
            [sr + (1 - sr) * s, sg - sg * s,       sb - sb * s],
            [sr - sr * s,       sg + (1 - sg) * s, sb - sb * s],
            [sr - sr * s,       sg - sg * s,       sb + (1 - sb) * s],
        ]
        off = mat_vec(sat_mat, off)
        mat = mat_mul(sat_mat, mat)

    # Temperature: warm = CSS sepia(), cool = CSS hue-rotate(negative deg)
    if abs(t_raw) > 1:
        if t_raw > 0:
            amt = min(1.0, t_raw / 200)
            t_mat = [
                [1 - amt + amt * 0.393, amt * 0.769,           amt * 0.189],
                [amt * 0.349,           1 - amt + amt * 0.686, amt * 0.168],
                [amt * 0.272,           amt * 0.534,           1 - amt + amt * 0.131],
            ]
        else:
            rad = math.radians(t_raw / 5)
            cos_a, sin_a = math.cos(rad), math.sin(rad)
            t_mat = [
                [0.213 + 0.787*cos_a - 0.213*sin_a, 0.715 - 0.715*cos_a - 0.715*sin_a, 0.072 - 0.072*cos_a + 0.928*sin_a],
                [0.213 - 0.213*cos_a + 0.143*sin_a, 0.715 + 0.285*cos_a + 0.140*sin_a, 0.072 - 0.072*cos_a - 0.283*sin_a],
                [0.213 - 0.213*cos_a - 0.787*sin_a, 0.715 - 0.715*cos_a + 0.715*sin_a, 0.072 + 0.928*cos_a + 0.072*sin_a],
            ]
        off = mat_vec(t_mat, off)
        mat = mat_mul(t_mat, mat)

    # Tint: CSS hue-rotate
    if abs(ti_raw) > 1:
        rad = math.radians(ti_raw / 3)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        ti_mat = [
            [0.213 + 0.787*cos_a - 0.213*sin_a, 0.715 - 0.715*cos_a - 0.715*sin_a, 0.072 - 0.072*cos_a + 0.928*sin_a],
            [0.213 - 0.213*cos_a + 0.143*sin_a, 0.715 + 0.285*cos_a + 0.140*sin_a, 0.072 - 0.072*cos_a - 0.283*sin_a],
            [0.213 - 0.213*cos_a - 0.787*sin_a, 0.715 - 0.715*cos_a + 0.715*sin_a, 0.072 + 0.928*cos_a + 0.072*sin_a],
        ]
        off = mat_vec(ti_mat, off)
        mat = mat_mul(ti_mat, mat)

    # --- Build filter string ---
    # Single colorchannelmixer with the composed matrix + offsets
    # format=rgb24 forces RGB processing, avoiding YUV chroma subsampling artifacts
    ccm = (
        f"colorchannelmixer="
        f"rr={mat[0][0]:.6f}:rg={mat[0][1]:.6f}:rb={mat[0][2]:.6f}:ra={off[0]:.6f}:"
        f"gr={mat[1][0]:.6f}:gg={mat[1][1]:.6f}:gb={mat[1][2]:.6f}:ga={off[1]:.6f}:"
        f"br={mat[2][0]:.6f}:bg={mat[2][1]:.6f}:bb={mat[2][2]:.6f}:ba={off[2]:.6f}"
    )

    filters = [f"format=rgb24", ccm]

    if sharpness >= 0.001:
        filters.append(f"unsharp=5:5:{sharpness:.2f}:5:5:{sharpness:.2f}")

    chain = ",".join(filters)
    return f"[0:v]{chain}[corrected];[corrected][1:v]overlay=0:0,scale=1080:1920:flags=lanczos,setsar=1"


async def get_video_dimensions(video_path: str) -> tuple[int, int]:
    """Probe video width and height with ffprobe."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        video_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    w, h = out.decode().strip().split(",")
    return int(w), int(h)


async def burn_video(
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

        filter_complex = build_filter_complex(color_correction)

        # TikTok-optimized encode: 1080x1920, 30fps, ~15Mbps H.264 High
        tiktok_encode = [
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-maxrate", "15M",
            "-bufsize", "15M",
            "-profile:v", "high",
            "-level", "4.2",
            "-r", "30",
            "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "128k",
        ]

        if overlay_path:
            # Have overlay — use it
            cmd = [
                "ffmpeg", "-y",
                "-i", video_path,
                "-i", overlay_path,
                "-filter_complex", filter_complex,
                *tiktok_encode,
                output_path,
            ]
        else:
            # No overlay — just apply color correction to the video directly
            # Rewrite filter to not reference [1:v] overlay input
            cc_filter = _build_color_only_filter(color_correction)
            cmd = [
                "ffmpeg", "-y",
                "-i", video_path,
                "-vf", cc_filter,
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


def _build_color_only_filter(color_correction: dict | None) -> str:
    """Build a -vf filter string for color correction only (no overlay input)."""
    import math

    # TikTok-optimized output: always scale to 1080x1920
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

    # Reuse the same matrix composition as build_filter_complex
    mat = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    off = [0.0, 0.0, 0.0]

    def mat_mul(a, b):
        return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]

    def mat_vec(m, v):
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
                [0.213 + 0.787*cos_a - 0.213*sin_a, 0.715 - 0.715*cos_a - 0.715*sin_a, 0.072 - 0.072*cos_a + 0.928*sin_a],
                [0.213 - 0.213*cos_a + 0.143*sin_a, 0.715 + 0.285*cos_a + 0.140*sin_a, 0.072 - 0.072*cos_a - 0.283*sin_a],
                [0.213 - 0.213*cos_a - 0.787*sin_a, 0.715 - 0.715*cos_a + 0.715*sin_a, 0.072 + 0.928*cos_a + 0.072*sin_a],
            ]
        off = mat_vec(t_mat, off)
        mat = mat_mul(t_mat, mat)

    if abs(ti_raw) > 1:
        rad = math.radians(ti_raw / 3)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        ti_mat = [
            [0.213 + 0.787*cos_a - 0.213*sin_a, 0.715 - 0.715*cos_a - 0.715*sin_a, 0.072 - 0.072*cos_a + 0.928*sin_a],
            [0.213 - 0.213*cos_a + 0.143*sin_a, 0.715 + 0.285*cos_a + 0.140*sin_a, 0.072 - 0.072*cos_a - 0.283*sin_a],
            [0.213 - 0.213*cos_a - 0.787*sin_a, 0.715 - 0.715*cos_a + 0.715*sin_a, 0.072 + 0.928*cos_a + 0.072*sin_a],
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


# ── API Routes ───────────────────────────────────────────────────────


@app.get("/api/videos")
async def api_videos():
    """List all videos in video-output/ grouped by folder."""
    return {"videos": scan_videos()}


@app.get("/api/captions")
async def api_captions():
    return {"sources": scan_caption_sources()}


@app.get("/api/fonts")
async def api_fonts():
    return {"fonts": list_fonts()}


@app.post("/api/burn-overlay")
async def api_burn_overlay(request: Request):
    """Receive html2canvas PNG + video path, composite with ffmpeg at full fps."""
    body = await request.json()

    batch_id = body["batchId"]
    idx = int(body["index"])
    video_rel = body["videoPath"]
    overlay_b64 = body.get("overlayPng")  # base64 PNG from html2canvas
    color_correction = body.get("colorCorrection")

    batch_dir = BURN_DIR / batch_id
    batch_dir.mkdir(exist_ok=True)

    video_abs = str(VIDEO_DIR / video_rel)
    mp4_path = str(batch_dir / f"burned_{idx:03d}.mp4")

    try:
        if overlay_b64 or color_correction:
            await burn_video(video_abs, overlay_b64, mp4_path, color_correction)
        else:
            import shutil
            shutil.copy2(video_abs, mp4_path)

        return {"index": idx, "ok": True, "file": f"{batch_id}/burned_{idx:03d}.mp4"}
    except Exception as e:
        return JSONResponse(
            {"index": idx, "ok": False, "error": str(e)[:300]},
            status_code=500,
        )


@app.get("/api/batches")
async def api_batches():
    """List all past burn batches with file counts and timestamps."""
    batches = []
    if not BURN_DIR.exists():
        return {"batches": batches}
    for d in sorted(BURN_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        mp4s = list(d.glob("burned_*.mp4"))
        if not mp4s:
            continue
        batches.append({
            "id": d.name,
            "count": len(mp4s),
            "created": int(d.stat().st_mtime),
        })
    return {"batches": batches}


@app.get("/api/burn-zip/{batch_id}")
async def api_burn_zip(batch_id: str):
    """Zip all burned MP4s in a batch and return the archive."""
    import zipfile
    from fastapi.responses import FileResponse

    batch_dir = BURN_DIR / batch_id
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


@app.websocket("/ws/burn")
async def ws_burn(ws: WebSocket):
    await ws.accept()
    try:
        data = await ws.receive_json()
        pairs = data.get("pairs", [])

        batch_id = uuid.uuid4().hex[:8]
        batch_dir = BURN_DIR / batch_id
        batch_dir.mkdir(exist_ok=True)

        total = len(pairs)
        results = []

        for i, pair in enumerate(pairs):
            video_abs = str(VIDEO_DIR / pair["videoPath"])
            overlay_png = pair.get("overlayPng")  # base64 PNG from browser canvas
            color_correction = pair.get("colorCorrection")
            out_name = f"burned_{i:03d}.mp4"
            out_path = str(batch_dir / out_name)

            await ws.send_json({
                "event": "burning",
                "index": i,
                "total": total,
            })

            try:
                if overlay_png or color_correction:
                    await burn_video(
                        video_abs, overlay_png, out_path, color_correction,
                    )
                else:
                    import shutil
                    shutil.copy2(video_abs, out_path)

                results.append({
                    "index": i,
                    "ok": True,
                    "file": f"{batch_id}/{out_name}",
                })
            except Exception as e:
                results.append({
                    "index": i,
                    "ok": False,
                    "error": str(e)[:300],
                })

            await ws.send_json({
                "event": "burned",
                "index": i,
                "total": total,
                "result": results[-1],
            })

        await ws.send_json({
            "event": "complete",
            "batchId": batch_id,
            "results": results,
            "successCount": sum(1 for r in results if r["ok"]),
            "total": total,
        })

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"event": "error", "error": str(e)})
        except Exception:
            pass


# ── Static file serving ──────────────────────────────────────────────

# Serve source videos for preview
app.mount("/video", StaticFiles(directory=str(VIDEO_DIR)), name="video")

# Serve fonts for @font-face
app.mount("/fonts", StaticFiles(directory=str(FONT_DIR)), name="fonts")

# Serve burned videos for download
app.mount("/burned", StaticFiles(directory=str(BURN_DIR)), name="burned")

# Serve the UI — disable browser caching of HTML during development
from starlette.middleware.base import BaseHTTPMiddleware

class NoCacheHTMLMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        ct = response.headers.get("content-type", "")
        if "text/html" in ct:
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response

app.add_middleware(NoCacheHTMLMiddleware)

app.mount("/", StaticFiles(directory="static/burn", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
