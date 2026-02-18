import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from project_manager import PROJECTS_DIR, ensure_default_project
from providers import PROVIDERS
from providers.base import API_KEYS
from routers.burn import router as burn_router
from routers.captions import router as captions_router
from routers.projects import list_all_projects, router as projects_router
from routers.video import router as video_router

load_dotenv()

FRONTEND_DIR = Path("frontend/dist")
CORS_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:8000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:8000",
]


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
        "fal": bool(os.getenv("FAL_KEY")),
        "luma": bool(os.getenv("LUMA_API_KEY")),
        "replicate": bool(os.getenv("REPLICATE_API_TOKEN")),
        "openai": bool(os.getenv("OPENAI_API_KEY")),
    }


def _provider_status() -> dict[str, bool]:
    return {
        provider_id: bool(API_KEYS.get(provider["key_id"]))
        for provider_id, provider in PROVIDERS.items()
    }


def _log_startup_validation(ffmpeg_ok: bool, ytdlp_ok: bool):
    print("✓ Content Posting Lab starting...")
    print(f"  ffmpeg: {'ok' if ffmpeg_ok else 'missing'}")
    print(f"  yt-dlp: {'ok' if ytdlp_ok else 'missing'}")
    print(f"  api keys: {_api_key_status()}")


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

    yield
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
