import asyncio
import base64
import csv
import json
import re
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

load_dotenv()

app = FastAPI()

OUTPUT_DIR = Path("caption_output")
OUTPUT_DIR.mkdir(exist_ok=True)

_ws_clients: dict[str, list[WebSocket]] = {}


async def _broadcast(job_id: str, event: str, data: dict):
    clients = _ws_clients.get(job_id, [])
    msg = json.dumps({"event": event, **data})
    dead = []
    for ws in clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.remove(ws)


def _video_id(url: str) -> str:
    m = re.search(r"/video/(\d+)", url)
    return m.group(1) if m else "unknown"


# ── Pipeline (no browser) ────────────────────────────────────────────

async def _run_pipeline(job_id: str, profile_url: str, max_videos: int, sort: str):
    from scraper.frame_extractor import list_profile_videos, get_thumbnail
    from scraper.caption_extractor import extract_caption

    # Normalize URL
    pu = profile_url.strip()
    if pu.startswith("@"):
        profile_url = f"https://www.tiktok.com/{pu}"
    elif not pu.startswith("http"):
        profile_url = f"https://www.tiktok.com/@{pu}"
    m = re.search(r"@([\w.]+)", profile_url)
    username = m.group(1) if m else "unknown"

    job_dir = OUTPUT_DIR / username
    frames_dir = job_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Phase 1: List video URLs via yt-dlp (no browser)
        await _broadcast(job_id, "status", {"text": f"Listing videos for @{username}..."})
        video_urls = await list_profile_videos(profile_url, max_videos)
        total = len(video_urls)

        await _broadcast(job_id, "urls_collected", {"count": total, "urls": video_urls})

        if total == 0:
            await _broadcast(job_id, "all_complete", {"results": [], "csv": None, "username": username})
            return

        results: list[dict] = [None] * total

        # Phase 2: Download + extract frames (concurrent, batches of 5)
        DOWNLOAD_BATCH = 5

        async def _download_one(i: int, url: str):
            vid = _video_id(url)
            row = {"index": i, "video_url": url, "video_id": vid,
                   "frame_path": None, "caption": None, "error": None}
            try:
                await _broadcast(job_id, "downloading", {"index": i, "total": total, "video_id": vid})
                thumb_path = frames_dir / f"{vid}.jpg"
                await get_thumbnail(url, thumb_path)
                row["frame_path"] = str(thumb_path)
                b64 = base64.b64encode(thumb_path.read_bytes()).decode()
                await _broadcast(job_id, "frame_ready", {
                    "index": i, "total": total, "video_id": vid,
                    "b64": b64, "video_url": url,
                })
            except Exception as e:
                row["error"] = str(e)
                await _broadcast(job_id, "frame_error", {
                    "index": i, "total": total, "video_id": vid, "error": str(e),
                })
            results[i] = row

        for batch_start in range(0, total, DOWNLOAD_BATCH):
            batch = list(enumerate(video_urls))[batch_start:batch_start + DOWNLOAD_BATCH]
            await asyncio.gather(*[_download_one(i, url) for i, url in batch])

        # Phase 3: GPT-4o vision caption extraction (concurrent, batches of 10)
        OCR_BATCH = 10
        await _broadcast(job_id, "ocr_starting", {"total": total})

        async def _ocr_one(row: dict):
            i = row["index"]
            if row["error"] or not row["frame_path"]:
                await _broadcast(job_id, "ocr_done", {
                    "index": i, "total": total, "video_id": row["video_id"],
                    "caption": "", "error": row["error"],
                })
                return
            await _broadcast(job_id, "ocr_started", {
                "index": i, "total": total, "video_id": row["video_id"],
            })
            try:
                frame_bytes = Path(row["frame_path"]).read_bytes()
                caption = await extract_caption(frame_bytes)
                row["caption"] = caption
            except Exception as e:
                row["error"] = str(e)
                caption = ""
            await _broadcast(job_id, "ocr_done", {
                "index": i, "total": total, "video_id": row["video_id"],
                "caption": caption, "error": row.get("error"),
            })

        for batch_start in range(0, total, OCR_BATCH):
            batch = results[batch_start:batch_start + OCR_BATCH]
            await asyncio.gather(*[_ocr_one(row) for row in batch if row])

        # Write CSV
        csv_path = job_dir / "captions.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["video_id", "video_url", "caption", "error"])
            writer.writeheader()
            for row in results:
                writer.writerow({
                    "video_id": row["video_id"],
                    "video_url": row["video_url"],
                    "caption": row.get("caption", ""),
                    "error": row.get("error", ""),
                })

        await _broadcast(job_id, "all_complete", {
            "results": [
                {"index": r["index"], "video_id": r["video_id"], "video_url": r["video_url"],
                 "caption": r.get("caption", ""), "error": r.get("error")}
                for r in results
            ],
            "csv": str(csv_path),
            "username": username,
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        await _broadcast(job_id, "error", {"error": str(e)})


# ── WebSocket endpoint ────────────────────────────────────────────────

@app.websocket("/ws/{job_id}")
async def websocket_endpoint(ws: WebSocket, job_id: str):
    await ws.accept()
    _ws_clients.setdefault(job_id, []).append(ws)
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            if msg.get("action") == "start":
                asyncio.create_task(_run_pipeline(
                    job_id,
                    msg["profile_url"],
                    min(max(1, msg.get("max_videos", 20)), 50),
                    msg.get("sort", "latest"),
                ))
    except WebSocketDisconnect:
        pass
    finally:
        clients = _ws_clients.get(job_id, [])
        if ws in clients:
            clients.remove(ws)


@app.get("/api/export/{username}")
async def export_csv(username: str):
    from fastapi.responses import FileResponse
    from fastapi import HTTPException
    csv_path = OUTPUT_DIR / username / "captions.csv"
    if not csv_path.exists():
        raise HTTPException(404, "CSV not found")
    return FileResponse(csv_path, filename=f"{username}_captions.csv")


app.mount("/files", StaticFiles(directory="caption_output"), name="files")
app.mount("/", StaticFiles(directory="static/captions", html=True), name="static")
