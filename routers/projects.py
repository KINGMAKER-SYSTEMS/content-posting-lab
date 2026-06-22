"""Projects router for managing campaigns and workflows."""

import logging
import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from project_manager import (
    BASE_DIR,
    PROJECTS_DIR,
    create_project,
    delete_project,
    ensure_default_project,
    get_project,
    list_projects,
    sanitize_project_name,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class CreateProjectRequest(BaseModel):
    name: str


def _get_dir_stats(dir_path: Path) -> dict:
    if not dir_path.exists():
        return {"count": 0, "total_size_bytes": 0, "files": []}

    files = []
    total_size = 0
    for f in sorted(dir_path.rglob("*")):
        if f.is_file():
            stat = f.stat()
            total_size += stat.st_size
            files.append(
                {
                    "name": f.relative_to(dir_path).as_posix(),
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )

    return {"count": len(files), "total_size_bytes": total_size, "files": files}


def _get_last_activity(project_path: Path) -> str | None:
    latest = None
    for subdir_name in ("videos", "clips", "captions", "burned"):
        subdir = project_path / subdir_name
        if not subdir.exists():
            continue
        for f in subdir.rglob("*"):
            if f.is_file():
                mtime = f.stat().st_mtime
                if latest is None or mtime > latest:
                    latest = mtime

    if latest is not None:
        return datetime.fromtimestamp(latest).isoformat()
    return None


@router.get("/")
async def list_all_projects():
    """List all projects with stats (video count, caption count, burned count)."""
    projects = list_projects()

    # Auto-create "quick-test" if no projects exist
    if not projects:
        ensure_default_project()
        projects = list_projects()

    return {"projects": projects}


@router.post("", status_code=201, include_in_schema=False)
@router.post("/", status_code=201)
async def create_new_project(body: CreateProjectRequest):
    """Create a new project."""
    try:
        create_project(body.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))

    project_info = get_project(body.name)
    return {"project": project_info}


_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}


def _iter_project_video_items(project_name: str, project_path: Path) -> list[dict]:
    items: list[dict] = []
    # Generated clips land in `videos/`, clipper output in `clips/`, captioned
    # renders in `burned/` — all nested under provider/prompt subfolders.
    for kind in ("videos", "clips", "burned"):
        root = project_path / kind
        if not root.exists():
            continue
        for f in root.rglob("*"):
            if not f.is_file() or f.suffix.lower() not in _VIDEO_EXTS:
                continue
            stat = f.stat()
            rel = f.relative_to(PROJECTS_DIR).as_posix()
            items.append(
                {
                    "project": project_name,
                    "name": f.name,
                    "kind": kind,
                    "path": rel,
                    "url": f"/projects/{rel}",
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "modified_ts": stat.st_mtime,
                }
            )
    return items


@router.get("/videos/recent")
async def list_recent_project_videos(
    limit: int = Query(120, ge=1, le=500),
    project: str | None = Query(None),
):
    """List newest playable clips, scoped to a project when provided."""
    if not PROJECTS_DIR.exists():
        return {"count": 0, "videos": []}

    items: list[dict] = []
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        if project and project_dir.name != project:
            continue
        items.extend(_iter_project_video_items(project_dir.name, project_dir))

    items.sort(key=lambda v: v["modified_ts"], reverse=True)
    limited = items[:limit]
    for v in limited:
        del v["modified_ts"]

    return {"count": len(items), "videos": limited}


@router.get("/{name}")
async def get_single_project(name: str):
    """Get a single project's details."""
    try:
        project_info = get_project(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if project_info is None:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")

    return {"project": project_info}


@router.delete("/{name}")
async def delete_existing_project(name: str):
    """Delete a project and all its contents."""
    try:
        deleted = delete_project(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not deleted:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")

    return {"deleted": True, "name": name}


@router.get("/{name}/stats")
async def get_project_stats(name: str):
    """Get detailed stats for a project (file sizes, last activity)."""
    try:
        sanitized = sanitize_project_name(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    project_path = PROJECTS_DIR / sanitized

    if not project_path.exists():
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")

    videos_stats = _get_dir_stats(project_path / "videos")
    captions_stats = _get_dir_stats(project_path / "captions")
    burned_stats = _get_dir_stats(project_path / "burned")
    last_activity = _get_last_activity(project_path)

    return {
        "name": sanitized,
        "videos": videos_stats,
        "captions": captions_stats,
        "burned": burned_stats,
        "last_activity": last_activity,
        "total_size_bytes": (
            videos_stats["total_size_bytes"]
            + captions_stats["total_size_bytes"]
            + burned_stats["total_size_bytes"]
        ),
    }

@router.get("/{name}/videos")
async def list_project_videos(name: str):
    """List every playable clip in a project as ready-to-use URLs.

    Unlike `/stats` (which only counts the top level of `videos/`), this walks
    the `videos/` and `burned/` trees RECURSIVELY — generated clips land in nested
    provider/prompt subfolders (e.g. `videos/<provider>/<prompt>/clip.mp4`), so a
    flat listing reports zero. Each item's `url` is the public path served by the
    `/projects` static mount, so callers (e.g. the Slack content-agent) can fetch
    the bytes directly or hand the URL to a client.

    Returns newest-first. `kind` distinguishes raw `videos/` clips from
    caption-`burned/` ones.
    """
    try:
        sanitized = sanitize_project_name(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    project_path = PROJECTS_DIR / sanitized
    if not project_path.exists():
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")

    items = _iter_project_video_items(sanitized, project_path)

    items.sort(key=lambda v: v["modified_ts"], reverse=True)
    for v in items:
        del v["modified_ts"]  # internal sort key, not part of the response shape
        v.pop("project", None)
        v.pop("path", None)

    return {"name": sanitized, "count": len(items), "videos": items}


LEGACY_PROJECT_NAME = "legacy-imports"


def _symlink_files(
    source_dir: Path, target_dir: Path, extensions: set[str]
) -> list[str]:
    """
    Recursively find files matching extensions in source_dir and create
    symlinks in target_dir. Returns list of created symlink names.

    Uses absolute symlinks so they work regardless of cwd.
    Skips files that already have a symlink in target_dir.
    """
    created = []
    if not source_dir.exists():
        return created

    for f in source_dir.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in extensions:
            continue

        relative = f.relative_to(source_dir)
        flat_name = str(relative).replace(os.sep, "_")
        link_path = target_dir / flat_name

        if link_path.exists():
            continue

        try:
            link_path.symlink_to(f.resolve())
            created.append(flat_name)
        except OSError as e:
            logger.warning("Failed to symlink %s -> %s: %s", f, link_path, e)

    return created


@router.post("/import-legacy")
async def import_legacy_content():
    """
    Create a 'legacy-imports' project with symlinks to content from the old
    output/, caption_output/, and burn_output/ directories.

    Does NOT copy files — creates symlinks to preserve disk space.
    Returns 409 if the project already exists (delete it first to re-import).
    """
    project_path = PROJECTS_DIR / LEGACY_PROJECT_NAME

    if project_path.exists():
        raise HTTPException(
            status_code=409,
            detail=(
                f"Project '{LEGACY_PROJECT_NAME}' already exists. "
                "Delete it first if you want to re-import."
            ),
        )

    try:
        create_project(LEGACY_PROJECT_NAME)
    except (ValueError, FileExistsError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    videos_dir = project_path / "videos"
    captions_dir = project_path / "captions"
    burned_dir = project_path / "burned"

    video_links = _symlink_files(
        BASE_DIR / "output", videos_dir, {".mp4", ".mov", ".webm"}
    )
    caption_links = _symlink_files(BASE_DIR / "caption_output", captions_dir, {".csv"})
    burned_links = _symlink_files(
        BASE_DIR / "burn_output", burned_dir, {".mp4", ".mov", ".webm"}
    )

    summary = {
        "project": LEGACY_PROJECT_NAME,
        "imported": {
            "videos": len(video_links),
            "captions": len(caption_links),
            "burned": len(burned_links),
        },
        "details": {
            "video_files": video_links[:20],
            "caption_files": caption_links[:20],
            "burned_files": burned_links[:20],
        },
    }

    logger.info(
        "Legacy import complete: %d videos, %d captions, %d burned",
        len(video_links),
        len(caption_links),
        len(burned_links),
    )

    return summary
