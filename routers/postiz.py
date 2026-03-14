"""
Postiz API integration router.
Proxies requests to Postiz public API for managing connected social accounts
and publishing burned videos.
"""

import os
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from project_manager import PROJECTS_DIR, get_project_burn_dir, sanitize_project_name

router = APIRouter()

POSTIZ_BASE = "https://api.postiz.com/public/v1"


def _api_key() -> str:
    key = os.getenv("POSTIZ_API_KEY", "")
    if not key:
        raise HTTPException(status_code=503, detail="POSTIZ_API_KEY not configured")
    return key


def _headers() -> dict[str, str]:
    return {
        "Authorization": _api_key(),
        "Content-Type": "application/json",
    }


# ── Status ──────────────────────────────────────────────────────────────────


@router.get("/status")
async def postiz_status():
    """Check if Postiz API key is configured and the API is reachable."""
    key = os.getenv("POSTIZ_API_KEY", "")
    if not key:
        return {"configured": False, "reachable": False}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{POSTIZ_BASE}/integrations",
                headers=_headers(),
            )
            return {"configured": True, "reachable": resp.status_code == 200}
    except Exception:
        return {"configured": True, "reachable": False}


# ── Connected Accounts (Integrations) ───────────────────────────────────────


@router.get("/integrations")
async def list_integrations():
    """List all connected social media integrations from Postiz."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{POSTIZ_BASE}/integrations",
            headers=_headers(),
        )
        if resp.status_code != 200:
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"Postiz API error: {resp.text[:500]}",
            )
        return resp.json()


# ── Publishable Videos ──────────────────────────────────────────────────────


@router.get("/videos")
async def list_publishable_videos(project: str = Query(...)):
    """List burned videos available for publishing in the given project."""
    burned_dir = get_project_burn_dir(sanitize_project_name(project))
    if not burned_dir.exists():
        return {"batches": []}

    batches = []
    for batch_dir in sorted(burned_dir.iterdir(), reverse=True):
        if not batch_dir.is_dir():
            continue
        videos = []
        for f in sorted(batch_dir.iterdir()):
            if f.suffix.lower() in (".mp4", ".mov", ".webm"):
                videos.append({
                    "name": f.name,
                    "path": f"projects/{project}/burned/{batch_dir.name}/{f.name}",
                    "size": f.stat().st_size,
                })
        if videos:
            batches.append({
                "batch_id": batch_dir.name,
                "created": batch_dir.stat().st_mtime,
                "videos": videos,
            })

    return {"batches": batches}


# ── Upload Video to Postiz ──────────────────────────────────────────────────


class UploadRequest(BaseModel):
    path: str  # relative path to video file, e.g. "projects/foo/burned/abc/burned_001.mp4"


@router.post("/upload")
async def upload_video(req: UploadRequest):
    """Upload a video to Postiz via direct file upload."""
    # Resolve and validate file path
    file_path = Path(req.path)
    if file_path.is_absolute():
        raise HTTPException(status_code=400, detail="Path must be relative")
    resolved = (Path.cwd() / file_path).resolve()
    # Ensure path stays within project directory
    if not str(resolved).startswith(str(Path.cwd().resolve())):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {req.path}")

    content_type = "video/mp4"
    suffix = resolved.suffix.lower()
    if suffix == ".mov":
        content_type = "video/quicktime"
    elif suffix == ".webm":
        content_type = "video/webm"

    async with httpx.AsyncClient(timeout=120) as client:
        with open(resolved, "rb") as f:
            resp = await client.post(
                f"{POSTIZ_BASE}/upload",
                headers={"Authorization": _api_key()},
                files={"file": (resolved.name, f, content_type)},
            )
        if resp.status_code not in (200, 201):
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"Upload failed: {resp.text[:500]}",
            )
        return resp.json()


# ── Create Posts ────────────────────────────────────────────────────────────


class PostVideo(BaseModel):
    tag: str  # display label
    postiz_path: str  # path returned from Postiz upload


class CreatePostsRequest(BaseModel):
    integration_id: str
    videos: list[PostVideo]
    settings: dict | None = None


TIKTOK_DEFAULTS = {
    "__type": "tiktok",
    "privacy_level": "SELF_ONLY",
    "duet": False,
    "stitch": False,
    "comment": True,
    "autoAddMusic": "no",
    "brand_content_toggle": False,
    "brand_organic_toggle": False,
    "content_posting_method": "UPLOAD",
}


@router.post("/posts")
async def create_posts(req: CreatePostsRequest):
    """Create posts on a connected account via Postiz."""
    settings = req.settings or TIKTOK_DEFAULTS

    posts_array = []
    for v in req.videos:
        posts_array.append({
            "integration": {"id": req.integration_id},
            "value": [{"content": "", "image": [{"id": v.tag, "path": v.postiz_path}]}],
            "settings": settings,
        })

    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    payload = {
        "type": "now",
        "date": now,
        "shortLink": False,
        "tags": [],
        "posts": posts_array,
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{POSTIZ_BASE}/posts",
            headers=_headers(),
            json=payload,
        )
        if resp.status_code not in (200, 201):
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"Post creation failed: {resp.text[:500]}",
            )
        return resp.json()
