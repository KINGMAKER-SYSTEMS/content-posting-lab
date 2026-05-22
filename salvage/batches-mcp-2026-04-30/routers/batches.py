"""
Batches router — CRUD + manifest + download.

A batch is a workflow grouping above 'job' that flows through generate ->
clip -> burn -> distribute. The data layer lives in services/batches.py.
"""
from __future__ import annotations

import logging
import os
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import project_manager
from project_manager import sanitize_project_name
from services import batches as batches_service

log = logging.getLogger("batches")

router = APIRouter()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class CreateBatchRequest(BaseModel):
    project: str
    label: str | None = None


class AttachJobRequest(BaseModel):
    project: str
    stage: str
    job_id: str


class UpdateNotesRequest(BaseModel):
    project: str
    notes: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _absolute_base(request: Request) -> str:
    """Build an absolute base URL for the current request.

    Honors X-Forwarded-Proto / X-Forwarded-Host so URLs work behind Railway's
    proxy.
    """
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    if not host:
        return ""
    return f"{proto}://{host}"


def _ensure_project(project: str) -> str:
    """Sanitize + validate the project exists."""
    try:
        sanitized = sanitize_project_name(project)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not (project_manager.PROJECTS_DIR / sanitized).exists():
        raise HTTPException(404, f"Project '{sanitized}' not found")
    return sanitized


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("")
@router.post("/")
async def create_batch(body: CreateBatchRequest):
    """Create a new batch in a project."""
    sanitized = _ensure_project(body.project)
    try:
        batch = batches_service.create_batch(sanitized, body.label)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(400, str(e))
    return {"batch": batch}


@router.get("")
@router.get("/")
async def list_batches(
    project: str = Query(...),
    status: str | None = Query(None, description="Filter by 'open' or 'closed'"),
    since: str | None = Query("7d", description="Window like '24h', '7d', 'all'"),
    include_legacy: bool = Query(True),
):
    """List batches in a project. Defaults to open + last 7 days for clean
    agent responses; pass since=all to see everything.
    """
    sanitized = _ensure_project(project)

    # Trigger idempotent legacy migration on every list call. Cheap when
    # there's nothing to do, fixes pre-batch projects on first access.
    batches_service.migrate_legacy_jobs(sanitized)

    since_td = batches_service.parse_since(since)
    batches = batches_service.list_batches(
        sanitized,
        status=status if status in ("open", "closed") else None,
        since=since_td,
        include_legacy=include_legacy,
    )
    return {"batches": batches, "count": len(batches)}


@router.get("/daily")
async def get_daily_batch(project: str = Query(...)):
    """Get or create today's default daily batch for a project."""
    sanitized = _ensure_project(project)
    try:
        batch = batches_service.get_or_create_daily_batch(sanitized)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return {"batch": batch}


@router.get("/{batch_id}")
async def get_batch(batch_id: str, project: str = Query(...)):
    """Fetch a single batch by ID."""
    sanitized = _ensure_project(project)
    batch = batches_service.get_batch(sanitized, batch_id)
    if not batch:
        raise HTTPException(404, f"Batch '{batch_id}' not found")
    return {"batch": batch}


@router.post("/{batch_id}/close")
async def close_batch(batch_id: str, project: str = Query(...)):
    """Mark a batch as closed."""
    sanitized = _ensure_project(project)
    batch = batches_service.close_batch(sanitized, batch_id)
    if not batch:
        raise HTTPException(404, f"Batch '{batch_id}' not found")
    return {"batch": batch}


@router.post("/{batch_id}/reopen")
async def reopen_batch(batch_id: str, project: str = Query(...)):
    """Reopen a closed batch."""
    sanitized = _ensure_project(project)
    batch = batches_service.reopen_batch(sanitized, batch_id)
    if not batch:
        raise HTTPException(404, f"Batch '{batch_id}' not found")
    return {"batch": batch}


@router.post("/{batch_id}/attach")
async def attach_job(batch_id: str, body: AttachJobRequest):
    """Attach a job to a batch's stage list."""
    sanitized = _ensure_project(body.project)
    try:
        batch = batches_service.attach_job(sanitized, batch_id, body.stage, body.job_id)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))
    return {"batch": batch}


@router.post("/{batch_id}/detach")
async def detach_job(batch_id: str, body: AttachJobRequest):
    """Remove a job from a batch's stage list."""
    sanitized = _ensure_project(body.project)
    batch = batches_service.detach_job(sanitized, batch_id, body.stage, body.job_id)
    if not batch:
        raise HTTPException(404, f"Batch '{batch_id}' not found")
    return {"batch": batch}


@router.patch("/{batch_id}/notes")
async def update_notes(batch_id: str, body: UpdateNotesRequest):
    """Update the free-text notes field on a batch."""
    sanitized = _ensure_project(body.project)
    batch = batches_service.update_notes(sanitized, batch_id, body.notes)
    if not batch:
        raise HTTPException(404, f"Batch '{batch_id}' not found")
    return {"batch": batch}


@router.delete("/{batch_id}")
async def delete_batch(batch_id: str, project: str = Query(...)):
    """Delete a batch entry. Underlying job files stay on disk."""
    sanitized = _ensure_project(project)
    deleted = batches_service.delete_batch(sanitized, batch_id)
    if not deleted:
        raise HTTPException(404, f"Batch '{batch_id}' not found")
    return {"deleted": True, "batch_id": batch_id}


@router.get("/{batch_id}/manifest")
async def get_manifest(
    batch_id: str,
    request: Request,
    project: str = Query(...),
    absolute_urls: bool = Query(True, description="Return absolute https URLs"),
):
    """Agent-friendly manifest of every output file across every stage."""
    sanitized = _ensure_project(project)
    base = _absolute_base(request) if absolute_urls else ""
    manifest = batches_service.hydrate_manifest(sanitized, batch_id, base_url=base)
    if manifest is None:
        raise HTTPException(404, f"Batch '{batch_id}' not found")
    return manifest


@router.get("/{batch_id}/download")
async def download_batch(batch_id: str, project: str = Query(...)):
    """Stream a ZIP of every deliverable file in the batch.

    Includes generate-stage videos and burn-stage outputs. Skips clips/slideshow
    for now — those have their own download flows.
    """
    sanitized = _ensure_project(project)
    project_path = project_manager.PROJECTS_DIR / sanitized
    project_path_resolved = project_path.resolve()
    manifest = batches_service.hydrate_manifest(sanitized, batch_id)
    if manifest is None:
        raise HTTPException(404, f"Batch '{batch_id}' not found")

    def _safe_inside(candidate: Path) -> bool:
        """Reject paths that escape the project directory via ../ or absolute roots."""
        try:
            candidate.resolve().relative_to(project_path_resolved)
            return True
        except (ValueError, OSError):
            return False

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    os.close(tmp_fd)
    file_count = 0
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
            for gen_job in manifest["stages"]["generate"]:
                jid = gen_job["job_id"]
                for f in gen_job["files"]:
                    rel = f.get("file") or ""
                    if not rel or rel.startswith("/") or ".." in rel.split("/"):
                        continue
                    src = project_path / "videos" / rel
                    if not _safe_inside(src):
                        continue
                    if src.exists() and src.is_file():
                        zf.write(src, f"generate/{jid}/{Path(rel).name}")
                        file_count += 1
            for burn_job in manifest["stages"]["burn"]:
                bid = burn_job["burn_batch_id"]
                for f in burn_job["files"]:
                    rel = f.get("file") or ""
                    name = Path(rel).name
                    if not name or "/" in name or "\\" in name or name.startswith("."):
                        continue
                    src = project_path / "burned" / bid / name
                    if not _safe_inside(src):
                        continue
                    if src.exists() and src.is_file():
                        zf.write(src, f"burn/{bid}/{name}")
                        file_count += 1
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    if file_count == 0:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise HTTPException(404, "No deliverables found in this batch")

    zip_size = os.path.getsize(tmp_path)
    label = manifest.get("label") or batch_id
    zip_name = f"batch_{label}_{file_count}files.zip"

    async def _stream():
        try:
            with open(tmp_path, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return StreamingResponse(
        _stream(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{zip_name}"',
            "Content-Length": str(zip_size),
        },
    )


@router.post("/migrate")
async def migrate_legacy(project: str = Query(...)):
    """Manually trigger legacy job migration for a project. Idempotent."""
    sanitized = _ensure_project(project)
    summary = batches_service.migrate_legacy_jobs(sanitized)
    return {"project": sanitized, **summary}
