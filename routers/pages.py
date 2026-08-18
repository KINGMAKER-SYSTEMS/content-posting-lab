"""Per-page content surface — what actually goes on a page, and the edits
that change it.

The Lab already owns the whole chain: a roster page links to ONE project
(the library it draws from), the project holds the clips and the caption
banks, and /projects serves them statically. What never existed is the
surface that treats the PAGE as the unit. This is that surface's backend.

The edits are the Lab's own ontology, not new machinery:
  - assign a page to a different project  -> the page draws from a
    different library (the roster's `project` field is the link);
  - move a clip to another project's library -> the clip leaves this
    page's library and joins another page's (or the shared pool's).

Nothing here invents state the Lab doesn't already keep. Posted state is
tracked by the posting boards (ShipStream/device lanes), not the Lab —
so it is deliberately absent rather than fabricated.
"""
from __future__ import annotations

import csv
import logging
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from project_manager import PROJECTS_DIR, get_project, sanitize_project_name
from services.roster import get_page, list_all_pages, set_page

log = logging.getLogger("pages")

router = APIRouter()

MAX_CLIPS = 60
MAX_CAPTIONS = 60
MOVEABLE_EXTENSIONS = {".mp4", ".mov", ".webm", ".m4v"}


def _public_page(page: dict[str, Any]) -> dict[str, Any]:
    """The page as the surface needs it — never credentials."""
    return {
        "pageId": page.get("integration_id"),
        "handle": page.get("name"),
        "project": page.get("project") or None,
        "group": page.get("group") or None,
        "groupLabel": page.get("group_label") or None,
        "contentNiche": page.get("content_niche") or None,
        "posterName": page.get("poster_name") or None,
        "status": page.get("status") or None,
        "tiktokUrl": page.get("tiktok_url") or None,
        "source": page.get("source") or None,
    }


def _project_counts(project_name: str | None) -> dict[str, int]:
    if not project_name:
        return {"clips": 0, "captions": 0, "burned": 0}
    info = get_project(project_name)
    if not info:
        return {"clips": 0, "captions": 0, "burned": 0}
    return {
        "clips": int(info.get("video_count") or 0),
        "captions": int(info.get("caption_count") or 0),
        "burned": int(info.get("burned_count") or 0),
    }


@router.get("/")
def list_pages() -> dict[str, Any]:
    """Every roster page with its library assignment and content counts."""
    out = []
    for page in list_all_pages():
        public = _public_page(page)
        public["counts"] = _project_counts(public["project"])
        out.append(public)
    out.sort(key=lambda p: ((p["group"] or ""), (p["handle"] or "")))
    return {"pages": out}


def _clips_for(project: str) -> list[dict[str, Any]]:
    """Newest clips in the project's library, as static-mount URLs.

    Imported lazily — routers.burn pulls in a lot of the app (same reason
    services/poster_content.py lazy-imports it).
    """
    from routers.burn import _scan_project_videos

    videos = _scan_project_videos(project)
    videos.sort(key=lambda v: v.get("created") or 0, reverse=True)
    sanitized = sanitize_project_name(project)
    clips = []
    for video in videos[:MAX_CLIPS]:
        path = video["path"]
        url = (f"/projects/{sanitized}/{path}" if path.startswith("clips/")
               else f"/projects/{sanitized}/videos/{path}")
        clips.append({
            "path": path,
            "name": video.get("name"),
            "folder": video.get("folder", ""),
            "created": video.get("created"),
            "url": url,
        })
    return clips


def _captions_for(project: str) -> list[dict[str, Any]]:
    """Flattened caption banks for the project, with their source batch."""
    caption_dir = PROJECTS_DIR / sanitize_project_name(project) / "captions"
    if not caption_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for user_dir in sorted(caption_dir.iterdir()):
        if not user_dir.is_dir():
            continue
        csv_path = user_dir / "captions.csv"
        if not csv_path.is_file():
            continue
        try:
            with csv_path.open("r", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    text = (row.get("caption") or "").strip()
                    if not text:
                        continue
                    out.append({
                        "text": text,
                        "mood": row.get("mood") or None,
                        "videoId": row.get("video_id") or None,
                        "batch": user_dir.name,
                        "views": int(row["views"]) if (row.get("views") or "").isdigit() else None,
                    })
        except Exception:
            log.warning("unreadable caption CSV: %s", csv_path, exc_info=True)
    out.sort(key=lambda c: (c["views"] or 0), reverse=True)
    return out[:MAX_CAPTIONS]


@router.get("/{page_id}/content")
def page_content(page_id: str) -> dict[str, Any]:
    """One page's actual content: its library's clips and caption bank."""
    page = get_page(page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="page not found")
    public = _public_page(page)
    project = public["project"]
    if not project:
        return {
            "page": public,
            "clips": [],
            "captions": [],
            "counts": {"clips": 0, "captions": 0, "burned": 0},
            "note": "no_project_assigned",
        }
    if get_project(project) is None:
        raise HTTPException(status_code=409, detail=f"assigned project does not exist: {project}")
    return {
        "page": public,
        "clips": _clips_for(project),
        "captions": _captions_for(project),
        "counts": _project_counts(project),
    }


class AssignProjectRequest(BaseModel):
    project: str | None = None


@router.post("/{page_id}/project")
def assign_project(page_id: str, req: AssignProjectRequest) -> dict[str, Any]:
    """Point the page at a different library — THE edit that changes what
    content the page draws from. None unassigns (page goes dark, honestly)."""
    page = get_page(page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="page not found")
    project = (req.project or "").strip() or None
    if project is not None:
        project = sanitize_project_name(project)
        if get_project(project) is None:
            raise HTTPException(status_code=404, detail=f"project not found: {project}")
    set_page(page_id, {"project": project})
    updated = _public_page(get_page(page_id) or {})
    updated["counts"] = _project_counts(project)
    return {"page": updated}


class MoveClipRequest(BaseModel):
    path: str
    toProject: str


@router.post("/{page_id}/clips/move")
def move_clip(page_id: str, req: MoveClipRequest) -> dict[str, Any]:
    """Move a clip out of this page's library into another project's.

    This is a filesystem move between two libraries — the clip stops
    feeding this page and starts feeding whoever draws from the target
    project. Caption CSV rows are NOT moved (they belong to their scrape
    batch); the caption bank for a moved clip stays with the source.
    """
    page = get_page(page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="page not found")
    source_project = page.get("project")
    if not source_project:
        raise HTTPException(status_code=409, detail="page has no project assigned")
    target_project = sanitize_project_name((req.toProject or "").strip())
    if not target_project or target_project == source_project:
        raise HTTPException(status_code=400, detail="toProject must be a different project")
    if get_project(target_project) is None:
        raise HTTPException(status_code=404, detail=f"project not found: {target_project}")

    rel = Path(req.path)
    if rel.is_absolute() or ".." in rel.parts or rel.suffix.lower() not in MOVEABLE_EXTENSIONS:
        raise HTTPException(status_code=400, detail="invalid clip path")

    source_root = (PROJECTS_DIR / sanitize_project_name(source_project) / "videos").resolve()
    source = (source_root / rel).resolve()
    if source_root not in source.parents or not source.is_file():
        raise HTTPException(status_code=404, detail="clip not found in this page's library")

    target_root = PROJECTS_DIR / target_project / "videos"
    target_root.mkdir(parents=True, exist_ok=True)
    target = target_root / source.name
    if target.exists():
        raise HTTPException(status_code=409, detail="a clip with this name already exists in the target library")

    shutil.move(str(source), str(target))
    return {
        "moved": source.name,
        "from": source_project,
        "to": target_project,
        "counts": {
            source_project: _project_counts(source_project),
            target_project: _project_counts(target_project),
        },
    }
