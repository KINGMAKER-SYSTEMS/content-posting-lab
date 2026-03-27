"""
Google Drive router.
Manages file listing, upload, download, and deletion for Drive folders
linked to roster pages.
"""

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.gdrive import (
    count_files,
    delete_file,
    download_file,
    get_folder_info,
    get_inventory,
    is_configured,
    list_files,
    upload_file,
)
from services.roster import list_all_pages

router = APIRouter()


# ── Status ───────────────────────────────────────────────────────────────────


@router.get("/status")
async def drive_status():
    """Check if Google Drive API is configured."""
    return {"configured": is_configured()}


# ── Folder operations ────────────────────────────────────────────────────────


@router.get("/folder/{folder_id}")
async def list_folder(folder_id: str):
    """List files in a Drive folder."""
    if not is_configured():
        raise HTTPException(status_code=503, detail="Google Drive not configured")

    info = get_folder_info(folder_id)
    if info is None:
        raise HTTPException(status_code=404, detail="Folder not found or not accessible")

    files = list_files(folder_id)
    return {
        "folder": info,
        "files": files,
        "count": len(files),
    }


@router.get("/folder/{folder_id}/count")
async def folder_count(folder_id: str):
    """Get file count for a Drive folder (lightweight)."""
    if not is_configured():
        raise HTTPException(status_code=503, detail="Google Drive not configured")
    return {"folder_id": folder_id, "count": count_files(folder_id)}


# ── Upload ───────────────────────────────────────────────────────────────────


class UploadRequest(BaseModel):
    folder_id: str
    file_path: str
    mime_type: str = "video/mp4"


@router.post("/upload")
async def upload_to_drive(req: UploadRequest):
    """Upload a local file to a Drive folder."""
    if not is_configured():
        raise HTTPException(status_code=503, detail="Google Drive not configured")

    if not os.path.exists(req.file_path):
        raise HTTPException(status_code=400, detail=f"File not found: {req.file_path}")

    result = upload_file(req.folder_id, req.file_path, req.mime_type)
    if result is None:
        raise HTTPException(status_code=502, detail="Upload to Drive failed")

    return {"file": result}


# ── Batch upload (burned videos) ─────────────────────────────────────────────


class BatchUploadRequest(BaseModel):
    folder_id: str
    file_paths: list[str]
    mime_type: str = "video/mp4"


@router.post("/upload-batch")
async def batch_upload_to_drive(req: BatchUploadRequest):
    """Upload multiple files to a Drive folder."""
    if not is_configured():
        raise HTTPException(status_code=503, detail="Google Drive not configured")

    results = []
    for fp in req.file_paths:
        if not os.path.exists(fp):
            results.append({"file_path": fp, "ok": False, "error": "File not found"})
            continue
        result = upload_file(req.folder_id, fp, req.mime_type)
        if result:
            results.append({"file_path": fp, "ok": True, "file": result})
        else:
            results.append({"file_path": fp, "ok": False, "error": "Upload failed"})

    return {
        "results": results,
        "uploaded": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok")),
    }


# ── Delete ───────────────────────────────────────────────────────────────────


@router.delete("/file/{file_id}")
async def delete_drive_file(file_id: str):
    """Delete a file from Drive."""
    if not is_configured():
        raise HTTPException(status_code=503, detail="Google Drive not configured")

    ok = delete_file(file_id)
    if not ok:
        raise HTTPException(status_code=502, detail="Delete failed")
    return {"deleted": True}


# ── Inventory ────────────────────────────────────────────────────────────────


@router.get("/inventory")
async def drive_inventory():
    """Get file counts for all roster pages with linked Drive folders.

    Returns a mapping of integration_id -> file count.
    """
    if not is_configured():
        return {"inventory": {}, "configured": False}

    pages = list_all_pages()
    folder_map = {}
    for page in pages:
        fid = page.get("drive_folder_id")
        if fid:
            folder_map[page["integration_id"]] = fid

    if not folder_map:
        return {"inventory": {}, "configured": True}

    inventory = get_inventory(folder_map)
    return {"inventory": inventory, "configured": True}
