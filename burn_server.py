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

from burn_quality_gate import run_quality_check
from services.ffmpeg import build_cc_filter
from services.caption_render import (
    ERROR_SCHEMA as CAPTION_RENDER_ERROR_SCHEMA,
    CaptionRenderError,
    CaptionRenderRequest,
    CaptionRenderRequestV2,
    CaptionRenderResult,
    CaptionRenderResultV2,
    CaptionStyle,
    picture_frame_rows,
    render_caption_overlay,
)

app = FastAPI()

BASE_DIR = Path(__file__).parent
VIDEO_DIR = BASE_DIR / "output"
CAPTION_DIR = BASE_DIR / "caption_output"
BURN_DIR = BASE_DIR / "burn_output"
FONT_DIR = BASE_DIR / "fonts"

VIDEO_DIR.mkdir(exist_ok=True)
CAPTION_DIR.mkdir(exist_ok=True)
BURN_DIR.mkdir(exist_ok=True)
FONT_DIR.mkdir(exist_ok=True)


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
    """Build the overlay graph with the shared CSS-equivalent colour path."""
    chain = build_cc_filter(color_correction, scale="1080:1920")
    return (
        f"[0:v]{chain}[corrected];"
        "[1:v]scale=1080:1920:flags=lanczos[ovr];"
        "[corrected][ovr]overlay=0:0"
    )


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
    """Use the same typed colour renderer as the hosted API."""
    return build_cc_filter(color_correction, scale="1080:1920")


# ── API Routes ───────────────────────────────────────────────────────


@app.get("/health")
async def api_health():
    """Rail preflight target for the local Burn runtime."""
    return {"ok": True}


@app.post("/api/quality-check")
async def api_quality_check(request: Request):
    """Fail-closed caption and overlay gate used by Rail."""
    body = await request.json()
    caption_style = None
    if body.get("captionStyle") is not None:
        try:
            caption_style = CaptionStyle.model_validate(body["captionStyle"]).model_dump(
                mode="json", exclude_none=True
            )
        except Exception:
            return JSONResponse(
                {"ok": False, "reasons": ["caption_style_invalid"]},
                status_code=422,
            )
    result = run_quality_check(
        caption=str(body.get("caption") or ""),
        persona=str(body.get("persona") or "male"),
        overlay_png=body.get("overlayPng"),
        require_overlay=True,
        caption_style=caption_style,
        picture_frame=body.get("picture_frame"),
    )
    if not result["ok"]:
        return JSONResponse(result, status_code=422)
    return result


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


@app.post(
    "/api/burn/caption-render/v1",
    response_model=CaptionRenderResult,
    response_model_exclude_none=True,
)
async def caption_render_v1(request: CaptionRenderRequest):
    """Render the exact Dossier caption text/style for the Rail compositor."""

    try:
        result = render_caption_overlay(request, font_dir=FONT_DIR)
        if result.plan.fitted_font_size_px is not None:
            raise CaptionRenderError(
                "CAPTION_FIT_REQUIRES_V2",
                "frame-aware caption fitting requires caption-render/v2",
            )
        return result
    except CaptionRenderError as error:
        return JSONResponse(
            {
                "schema": CAPTION_RENDER_ERROR_SCHEMA,
                "error": error.code,
                "message": error.message,
            },
            status_code=422,
        )


@app.post(
    "/api/burn/caption-render/v2",
    response_model=CaptionRenderResultV2,
    response_model_exclude_none=True,
)
async def caption_render_v2(request: CaptionRenderRequestV2):
    """Render against the required, explicit viewer-visible picture frame."""
    try:
        return render_caption_overlay(
            request,
            font_dir=FONT_DIR,
            fit_rows=picture_frame_rows(request.picture_frame),
        )
    except CaptionRenderError as error:
        return JSONResponse(
            {
                "schema": CAPTION_RENDER_ERROR_SCHEMA,
                "error": error.code,
                "message": error.message,
            },
            status_code=422,
        )


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

app.mount(
    "/",
    StaticFiles(directory=str(BASE_DIR / "static" / "burn"), html=True, check_dir=False),
    name="static",
)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
