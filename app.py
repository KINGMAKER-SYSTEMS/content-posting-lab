"""
Unified FastAPI entry point for Content Posting Lab.
Combines video generation, caption scraping, and caption burning.
Run: python -m uvicorn app:app --reload --port 8000
"""

import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from routers import video, captions, burn, projects

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Lifespan handler: startup/shutdown logic
# ─────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    # Startup
    print("✓ Content Posting Lab starting...")
    yield
    # Shutdown
    print("✓ Content Posting Lab shutting down...")


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Content Posting Lab",
    description="TikTok-style video generation, caption scraping, and caption burning",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware for localhost:5173 (Vite dev server)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────────────────────
# Health check endpoint
# ─────────────────────────────────────────────────────────────────────────────


def _check_ffmpeg() -> bool:
    """Check if ffmpeg is on PATH."""
    try:
        subprocess.run(
            ["ffmpeg", "-version"],
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


def _check_ytdlp() -> bool:
    """Check if yt-dlp is on PATH."""
    try:
        subprocess.run(
            ["yt-dlp", "--version"],
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


def _check_api_keys() -> dict[str, bool]:
    """Check which API keys are configured."""
    return {
        "xai": bool(os.getenv("XAI_API_KEY")),
        "fal": bool(os.getenv("FAL_KEY")),
        "luma": bool(os.getenv("LUMA_API_KEY")),
        "replicate": bool(os.getenv("REPLICATE_API_TOKEN")),
        "openai": bool(os.getenv("OPENAI_API_KEY")),
        "openai_vision": bool(os.getenv("OPENAI_API_KEY")),  # GPT-4o for captions
    }


@app.get("/api/health")
async def health_check():
    """Health check endpoint. Returns status of dependencies."""
    ffmpeg_ok = _check_ffmpeg()
    ytdlp_ok = _check_ytdlp()
    api_keys = _check_api_keys()

    return {
        "status": "ok" if (ffmpeg_ok and ytdlp_ok) else "degraded",
        "dependencies": {
            "ffmpeg": ffmpeg_ok,
            "yt_dlp": ytdlp_ok,
        },
        "api_keys": api_keys,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Router registration
# ─────────────────────────────────────────────────────────────────────────────

app.include_router(video.router, prefix="/api/video", tags=["video"])
app.include_router(captions.router, prefix="/api/captions", tags=["captions"])
app.include_router(burn.router, prefix="/api/burn", tags=["burn"])
app.include_router(projects.router, prefix="/api/projects", tags=["projects"])

# ─────────────────────────────────────────────────────────────────────────────
# Static file serving
# ─────────────────────────────────────────────────────────────────────────────

FRONTEND_DIR = Path("frontend/dist")


@app.get("/")
async def serve_frontend():
    """Serve frontend index.html if it exists, else return helpful message."""
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    raise HTTPException(
        status_code=404,
        detail="Frontend not built. Run 'npm run build' in frontend/ directory.",
    )


# Mount output directories for serving generated files
app.mount("/output", StaticFiles(directory="output"), name="output")
app.mount(
    "/caption_output", StaticFiles(directory="caption_output"), name="caption_output"
)
app.mount("/burn_output", StaticFiles(directory="burn_output"), name="burn_output")

# Mount frontend dist if it exists
if FRONTEND_DIR.exists():
    app.mount(
        "/assets", StaticFiles(directory=str(FRONTEND_DIR / "assets")), name="assets"
    )
