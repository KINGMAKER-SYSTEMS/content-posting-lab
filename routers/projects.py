"""Projects router for managing campaigns and workflows."""

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from project_manager import (
    PROJECTS_DIR,
    create_project,
    delete_project,
    ensure_default_project,
    get_project,
    list_projects,
    sanitize_project_name,
)

router = APIRouter()


class CreateProjectRequest(BaseModel):
    name: str


def _get_dir_stats(dir_path: Path) -> dict:
    if not dir_path.exists():
        return {"count": 0, "total_size_bytes": 0, "files": []}

    files = []
    total_size = 0
    for f in sorted(dir_path.iterdir()):
        if f.is_file():
            stat = f.stat()
            total_size += stat.st_size
            files.append(
                {
                    "name": f.name,
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )

    return {"count": len(files), "total_size_bytes": total_size, "files": files}


def _get_last_activity(project_path: Path) -> str | None:
    latest = None
    for subdir_name in ("videos", "captions", "burned"):
        subdir = project_path / subdir_name
        if not subdir.exists():
            continue
        for f in subdir.iterdir():
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
