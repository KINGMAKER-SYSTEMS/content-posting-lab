"""
Caption Burn Server
Burns scraped captions onto generated videos using Pillow + FFmpeg overlay.
Run: python burn_server.py  (serves on port 8002)
"""

import asyncio
import csv
import json
import os
import tempfile
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

app = FastAPI()

BASE_DIR = Path(__file__).parent
VIDEO_DIR = BASE_DIR / "video-output"
CAPTION_DIR = BASE_DIR / "caption_output"
BURN_DIR = BASE_DIR / "burn_output"
FONT_DIR = BASE_DIR / "fonts"
FONT_PATH = FONT_DIR / "TikTokSans16pt-Bold.ttf"

BURN_DIR.mkdir(exist_ok=True)


# ── Helpers ──────────────────────────────────────────────────────────


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    """Word-wrap text to fit within max_width pixels using actual font metrics."""
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        bbox = font.getbbox(test)
        if bbox[2] > max_width and current:
            lines.append(current)
            current = word
        else:
            current = test
    if current:
        lines.append(current)
    return "\n".join(lines)


def scan_videos() -> list[dict]:
    """Recursively find all mp4 files under video-output/."""
    videos = []
    if not VIDEO_DIR.exists():
        return videos
    for mp4 in sorted(VIDEO_DIR.rglob("*.mp4")):
        rel = mp4.relative_to(VIDEO_DIR)
        # folder = first directory level (e.g. "grok", "rep-minimax")
        parts = rel.parts
        folder = parts[0] if len(parts) > 1 else ""
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


def render_caption_overlay(
    caption: str,
    width: int,
    height: int,
    x_pct: float,
    y_pct: float,
    font_size: int,
    overlay_path: str,
    font_file: str | None = None,
    max_width_pct: float = 80,
) -> str:
    """Render caption as transparent PNG overlay using Pillow + Freetype.
    x_pct/y_pct: text center position as percentage (0-100) of video dimensions.
    """
    font_path = FONT_DIR / font_file if font_file else FONT_PATH
    font = ImageFont.truetype(str(font_path), font_size)
    max_text_width = int(width * max_width_pct / 100)
    wrapped = wrap_text(caption, font, max_text_width)

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Convert percentage to pixel position (text center)
    cx = int(width * x_pct / 100)
    cy = int(height * y_pct / 100)

    draw.multiline_text(
        (cx, cy),
        wrapped,
        font=font,
        fill="white",
        stroke_width=4,
        stroke_fill="black",
        anchor="mm",
        align="center",
    )
    img.save(overlay_path)
    return overlay_path


async def burn_caption(
    video_path: str,
    caption: str,
    x_pct: float,
    y_pct: float,
    font_size: int,
    output_path: str,
    font_file: str | None = None,
    max_width_pct: float = 80,
) -> str:
    """Burn caption onto video: Pillow renders text overlay, FFmpeg composites it."""
    w, h = await get_video_dimensions(video_path)

    overlay_path = tempfile.mktemp(suffix=".png")
    render_caption_overlay(caption, w, h, x_pct, y_pct, font_size, overlay_path, font_file, max_width_pct)

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", overlay_path,
        "-filter_complex", "[0:v][1:v]overlay=0:0",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-movflags", "+faststart",
        "-c:a", "copy",
        output_path,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    os.unlink(overlay_path)

    if proc.returncode != 0:
        raise RuntimeError(stderr.decode()[-500:])

    return output_path


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


# ── WebSocket for batch burn with progress ───────────────────────────


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
            caption = pair.get("caption", "")
            x_pct = pair.get("x", 50)
            y_pct = pair.get("y", 50)
            font_size = pair.get("fontSize", 58)
            font_file = pair.get("fontFile")
            max_width_pct = pair.get("maxWidthPct", 80)
            out_name = f"burned_{i:03d}.mp4"
            out_path = str(batch_dir / out_name)

            await ws.send_json({
                "event": "burning",
                "index": i,
                "total": total,
            })

            try:
                if caption.strip():
                    await burn_caption(
                        video_abs, caption, x_pct, y_pct,
                        font_size, out_path, font_file, max_width_pct,
                    )
                else:
                    # No caption — just copy the video
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

# Serve the UI (must be last)
app.mount("/", StaticFiles(directory="static/burn", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
