import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from project_manager import PROJECTS_DIR, ensure_default_project
from providers import PROVIDERS
from providers.base import API_KEYS
from routers.burn import router as burn_router
from routers.captions import router as captions_router
from routers.clipper import router as clipper_router
from routers.projects import list_all_projects, router as projects_router
from routers.recreate import router as recreate_router
from routers.postiz import router as postiz_router
from routers.roster import router as roster_router
from routers.slideshow import router as slideshow_router
from routers.telegram import router as telegram_router
from routers.email_routing import router as email_router
from routers.upload import router as upload_router
from routers.gdrive import router as gdrive_router
from routers.video import router as video_router

load_dotenv()

FRONTEND_DIR = Path("frontend/dist")
_DEFAULT_CORS = [
    "http://localhost:5173",
    "http://localhost:8000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:8000",
]
_EXTRA_CORS = os.getenv("CORS_ORIGINS")
CORS_ORIGINS = _DEFAULT_CORS + (
    [o.strip() for o in _EXTRA_CORS.split(",") if o.strip()] if _EXTRA_CORS else []
)


def _check_command(command: list[str]) -> bool:
    try:
        subprocess.run(
            command,
            capture_output=True,
            timeout=5,
            check=True,
        )
        return True
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ):
        return False


def _check_ffmpeg() -> bool:
    return _check_command(["ffmpeg", "-version"])


def _check_ytdlp() -> bool:
    return _check_command(["yt-dlp", "--version"])


def _api_key_status() -> dict[str, bool]:
    return {
        "xai": bool(os.getenv("XAI_API_KEY")),
        "replicate": bool(os.getenv("REPLICATE_API_TOKEN")),
        "openai": bool(os.getenv("OPENAI_API_KEY")),
    }


def _provider_status() -> dict[str, bool]:
    return {
        provider_id: bool(API_KEYS.get(provider["key_id"]))
        for provider_id, provider in PROVIDERS.items()
    }


def _log_startup_validation(ffmpeg_ok: bool, ytdlp_ok: bool):
    print("✓ Content Posting Lab starting...", flush=True)
    print(f"  ffmpeg: {'ok' if ffmpeg_ok else 'missing'}", flush=True)
    print(f"  yt-dlp: {'ok' if ytdlp_ok else 'missing'}", flush=True)
    print(f"  api keys: {_api_key_status()}", flush=True)
    # Debug: log key lengths to catch whitespace/truncation issues
    for name in ("XAI_API_KEY", "REPLICATE_API_TOKEN", "OPENAI_API_KEY"):
        val = os.getenv(name, "")
        if val:
            print(f"  {name}: len={len(val)}, starts={val[:10]}..., ends=...{val[-4:]}", flush=True)
        else:
            print(f"  {name}: NOT SET", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path("output").mkdir(parents=True, exist_ok=True)
    Path("caption_output").mkdir(parents=True, exist_ok=True)
    Path("burn_output").mkdir(parents=True, exist_ok=True)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    ensure_default_project()

    ffmpeg_ok = _check_ffmpeg()
    ytdlp_ok = _check_ytdlp()
    _log_startup_validation(ffmpeg_ok, ytdlp_ok)

    # Start Telegram bot if token configured
    from services.telegram import get_bot_token as get_tg_token
    from telegram_bot import start_bot as start_tg_bot, stop_bot as stop_tg_bot

    tg_token = get_tg_token()
    if tg_token:
        try:
            await start_tg_bot(tg_token)
            print("  telegram bot: started", flush=True)
        except Exception as e:
            print(f"  telegram bot: failed ({e})", flush=True)
    else:
        print("  telegram bot: no token configured", flush=True)

    yield

    # Stop Telegram bot
    try:
        await stop_tg_bot()
    except Exception:
        pass
    print("✓ Content Posting Lab shutting down...")


app = FastAPI(
    title="Content Posting Lab",
    description="TikTok-style video generation, caption scraping, and caption burning",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(video_router, prefix="/api/video", tags=["video"])
app.include_router(captions_router, prefix="/api/captions", tags=["captions"])
app.include_router(burn_router, prefix="/api/burn", tags=["burn"])
app.include_router(projects_router, prefix="/api/projects", tags=["projects"])
app.include_router(recreate_router, prefix="/api/recreate", tags=["recreate"])
app.include_router(clipper_router, prefix="/api/clipper", tags=["clipper"])
app.include_router(postiz_router, prefix="/api/postiz", tags=["postiz"])
app.include_router(roster_router, prefix="/api/roster", tags=["roster"])
app.include_router(slideshow_router, prefix="/api/slideshow", tags=["slideshow"])
app.include_router(telegram_router, prefix="/api/telegram", tags=["telegram"])
app.include_router(email_router, prefix="/api/email", tags=["email"])
app.include_router(upload_router, prefix="/api/upload", tags=["upload"])
app.include_router(gdrive_router, prefix="/api/drive", tags=["drive"])


@app.get("/api/projects", include_in_schema=False)
async def list_projects_no_trailing_slash():
    return await list_all_projects()


@app.get("/api/health")
async def health_check():
    ffmpeg_ok = _check_ffmpeg()
    ytdlp_ok = _check_ytdlp()
    providers = _provider_status()
    return {
        "status": "ok" if ffmpeg_ok and ytdlp_ok else "degraded",
        "ffmpeg": ffmpeg_ok,
        "ytdlp": ytdlp_ok,
        "providers": providers,
        "postiz": bool(os.getenv("POSTIZ_API_KEY")),
    }


app.mount("/fonts", StaticFiles(directory="fonts", check_dir=False), name="fonts")
app.mount(
    "/projects",
    StaticFiles(directory="projects", check_dir=False),
    name="projects",
)
app.mount("/output", StaticFiles(directory="output", check_dir=False), name="output")
app.mount(
    "/caption-output",
    StaticFiles(directory="caption_output", check_dir=False),
    name="caption-output",
)
app.mount(
    "/burn-output",
    StaticFiles(directory="burn_output", check_dir=False),
    name="burn-output",
)


_FONT_PREVIEW = Path(__file__).parent / "font_preview.html"


@app.get("/font-preview")
async def serve_font_preview():
    if _FONT_PREVIEW.exists():
        return FileResponse(_FONT_PREVIEW, media_type="text/html")
    raise HTTPException(status_code=404, detail="font_preview.html not found")


if FRONTEND_DIR.exists():

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        requested = FRONTEND_DIR / full_path
        if full_path and requested.exists() and requested.is_file():
            return FileResponse(requested)

        index_path = FRONTEND_DIR / "index.html"
        if index_path.exists():
            return FileResponse(index_path)

        raise HTTPException(
            status_code=404,
            detail="Frontend not built. Run 'npm run build' in frontend/ directory.",
        )


if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        timeout_keep_alive=300,
        h11_max_incomplete_event_size=0,
    )
