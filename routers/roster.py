"""
Page roster router.
Manages assignment of Postiz integrations (pages) to projects,
and linking of Google Drive folders to pages.
"""

import os

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.roster import (
    get_page,
    list_all_pages,
    list_pages_for_project,
    load_roster,
    parse_drive_folder_id,
    remove_page,
    save_roster,
    set_page,
)

router = APIRouter()

POSTIZ_BASE = "https://api.postiz.com/public/v1"


def _postiz_key() -> str:
    key = os.getenv("POSTIZ_API_KEY", "")
    if not key:
        raise HTTPException(status_code=503, detail="POSTIZ_API_KEY not configured")
    return key


# ── Roster CRUD ────────────────────────────────────────────────────────────


class UpdatePageRequest(BaseModel):
    project: str | None = None
    drive_folder_url: str | None = None


@router.get("/")
async def list_roster():
    """List all pages in the roster."""
    return {"pages": list_all_pages()}


@router.get("/project/{project_name}")
async def get_project_pages(project_name: str):
    """List pages assigned to a specific project."""
    return {"pages": list_pages_for_project(project_name)}


@router.put("/{integration_id}")
async def update_page(integration_id: str, req: UpdatePageRequest):
    """Assign or update a page in the roster."""
    data: dict = {}
    if req.project is not None:
        data["project"] = req.project
    if req.drive_folder_url is not None:
        data["drive_folder_url"] = req.drive_folder_url
        folder_id = parse_drive_folder_id(req.drive_folder_url)
        if req.drive_folder_url and not folder_id:
            raise HTTPException(
                status_code=400,
                detail="Could not parse Google Drive folder ID from URL",
            )
        data["drive_folder_id"] = folder_id
    entry = set_page(integration_id, data)
    return {"page": entry}


@router.delete("/{integration_id}")
async def delete_page(integration_id: str):
    """Remove a page from the roster."""
    removed = remove_page(integration_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Page not found in roster")
    return {"deleted": True}


# ── Sync with Postiz ──────────────────────────────────────────────────────


@router.post("/sync")
async def sync_integrations():
    """Fetch integrations from Postiz and merge into the roster.

    New integrations are added (unassigned). Existing entries keep their
    project and drive_folder assignments. Returns the updated roster.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{POSTIZ_BASE}/integrations",
            headers={
                "Authorization": _postiz_key(),
                "Content-Type": "application/json",
            },
        )
        if resp.status_code != 200:
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"Postiz API error: {resp.text[:500]}",
            )

    integrations = resp.json()
    if not isinstance(integrations, list):
        integrations = integrations.get("integrations", [])

    roster = load_roster()
    added = 0

    remote_ids = set()
    for ig in integrations:
        ig_id = ig.get("id", "")
        if not ig_id:
            continue
        remote_ids.add(ig_id)

        if ig_id not in roster["pages"]:
            added += 1

        # Merge: keep existing project/drive assignments, update name/provider/picture
        existing = roster["pages"].get(ig_id, {})
        set_page(ig_id, {
            "name": ig.get("name", existing.get("name", "")),
            "provider": ig.get("providerIdentifier", ig.get("provider", existing.get("provider", ""))),
            "picture": ig.get("picture", existing.get("picture")),
            "project": existing.get("project"),
            "drive_folder_url": existing.get("drive_folder_url"),
            "drive_folder_id": existing.get("drive_folder_id"),
        })

    # Count removed (in roster but not in Postiz anymore)
    roster = load_roster()  # re-read after set_page calls
    removed = 0
    for ig_id in list(roster["pages"].keys()):
        if ig_id not in remote_ids:
            removed += 1
            # Don't auto-delete — just flag. User can manually remove.

    return {
        "added": added,
        "removed": removed,
        "pages": list_all_pages(),
    }
