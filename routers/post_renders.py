"""Authenticated durable final-render preparation; independent of phone leases."""
from __future__ import annotations

import asyncio
import os
import logging
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse

from routers.control_plane_recipes import require_control_plane_bearer
from services.post_render_jobs import PostRenderJobs, RenderJobError, RenderJobSubmission
from services.route_auth import require_worker

router = APIRouter()
_service = None
_service_lock = threading.Lock()
_startup_failure: str | None = None
_started_service: PostRenderJobs | None = None
_READINESS_QUEUE_TIMEOUT_SECONDS = 2.0


def service() -> PostRenderJobs:
    global _service
    root = os.getenv("CONTENT_LAB_POST_RENDER_ROOT", "")
    if not root or not Path(root).is_absolute():
        raise HTTPException(503, "Persistent post-render root is not configured")
    with _service_lock:
        if _service is None or _service.root != Path(root):
            _service = PostRenderJobs(Path(root))
        return _service


def start_workers() -> PostRenderJobs | None:
    global _startup_failure, _started_service
    if not os.getenv("CONTENT_LAB_POST_RENDER_ROOT"):
        return None
    try:
        jobs = service()
        jobs.start()
        _started_service = jobs
        _startup_failure = None
        return jobs
    except Exception as error:
        _startup_failure = str(error) or error.__class__.__name__
        logging.getLogger("content_lab.post_render_jobs").exception("post render worker startup failed")
        return None


async def readiness() -> tuple[bool, dict]:
    """Readiness for the configured post-render lane.

    Returns ``(ready, detail)``. An unconfigured lane is simply disabled (ready
    and inert); a configured lane that failed to start, or whose workers have
    all died, is NOT ready and reports a latched reason plus queue age.
    """
    if not os.getenv("CONTENT_LAB_POST_RENDER_ROOT"):
        return True, {"post_render": {"state": "disabled"}}
    if _startup_failure is not None:
        return False, {"post_render": {"state": "startup_failed", "reason": _startup_failure}}
    jobs = _started_service
    if jobs is None:
        return False, {"post_render": {"state": "not_started"}}
    alive = jobs.workers_alive()
    try:
        queue_age_ms = await asyncio.wait_for(
            asyncio.to_thread(jobs.queue_age_ms),
            timeout=_READINESS_QUEUE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return False, {"post_render": {
            "state": "queue_check_failed",
            "reason": "queue_age_timeout",
            "workers": alive,
            "worker_count": jobs.worker_count,
        }}
    except Exception:
        logging.getLogger("content_lab.post_render_jobs").exception(
            "post render readiness queue check failed",
        )
        return False, {"post_render": {
            "state": "queue_check_failed",
            "reason": "queue_age_unavailable",
            "workers": alive,
            "worker_count": jobs.worker_count,
        }}
    detail = {
        "post_render": {
            "state": "healthy",
            "workers": alive,
            "worker_count": jobs.worker_count,
            "queue_age_ms": queue_age_ms,
        },
    }
    if jobs.worker_count and alive == 0:
        detail["post_render"]["state"] = "workers_died"
        detail["post_render"]["reason"] = "no_post_render_workers_alive"
        return False, detail
    return True, detail


def _authorize(authorization: str | None, page_id: str | None, *, expected: str | None = None):
    require_control_plane_bearer(authorization)
    if not page_id or len(page_id) > 512 or page_id.strip() != page_id:
        raise HTTPException(400, "X-RT-Page-Id header is required")
    if expected is not None and page_id != expected:
        raise HTTPException(404, "job not found")


def _http_error(error: RenderJobError):
    status = 404 if error.code in {"job_not_found", "artifact_not_found"} else 409
    if error.code in {"invalid_idempotency_key", "request_future_dated"}:
        status = 400
    raise HTTPException(status, error.code) from error


@router.post("/v1/post-renders", status_code=202, dependencies=[Depends(require_worker)])
def submit(body: RenderJobSubmission, authorization: str | None = Header(default=None),
           idempotency_key: str = Header(default=""), x_rt_page_id: str | None = Header(default=None)):
    _authorize(authorization, x_rt_page_id, expected=body.request.page_id)
    try:
        return service().enqueue(body, idempotency_key)
    except RenderJobError as error:
        _http_error(error)


@router.get("/v1/post-renders/{job_id}")
def status(job_id: str, authorization: str | None = Header(default=None), x_rt_page_id: str | None = Header(default=None)):
    _authorize(authorization, x_rt_page_id)
    try:
        service().require_page(job_id, x_rt_page_id)
        return service().status(job_id)
    except RenderJobError as error:
        _http_error(error)


@router.post("/v1/post-renders/{job_id}/retry", status_code=202)
def retry(job_id: str, authorization: str | None = Header(default=None), x_rt_page_id: str | None = Header(default=None)):
    _authorize(authorization, x_rt_page_id)
    try:
        service().require_page(job_id, x_rt_page_id)
        return service().retry(job_id)
    except RenderJobError as error:
        _http_error(error)


@router.post("/v1/post-renders/{job_id}/provenance", status_code=202, dependencies=[Depends(require_worker)])
def provenance(job_id: str, body: RenderJobSubmission, authorization: str | None = Header(default=None),
               x_rt_page_id: str | None = Header(default=None)):
    _authorize(authorization, x_rt_page_id, expected=body.request.page_id)
    try:
        service().require_page(job_id, x_rt_page_id)
        return service().provide_provenance(job_id, body)
    except RenderJobError as error:
        _http_error(error)


@router.post("/v1/post-renders/{job_id}/acknowledge", status_code=202)
def acknowledge(job_id: str, authorization: str | None = Header(default=None), x_rt_page_id: str | None = Header(default=None)):
    _authorize(authorization, x_rt_page_id)
    try:
        service().require_page(job_id, x_rt_page_id)
        return service().acknowledge(job_id)
    except RenderJobError as error:
        _http_error(error)


@router.post("/v1/post-renders/{job_id}/retire", status_code=202)
def retire(job_id: str, authorization: str | None = Header(default=None), x_rt_page_id: str | None = Header(default=None)):
    _authorize(authorization, x_rt_page_id)
    try:
        service().require_page(job_id, x_rt_page_id)
        return service().retire(job_id)
    except RenderJobError as error:
        _http_error(error)


@router.get("/v1/post-renders/{job_id}/artifacts/{kind}")
def artifact(job_id: str, kind: str, authorization: str | None = Header(default=None), x_rt_page_id: str | None = Header(default=None)):
    _authorize(authorization, x_rt_page_id)
    try:
        service().require_page(job_id, x_rt_page_id)
        path = service().artifact(job_id, kind)
    except RenderJobError as error:
        _http_error(error)
    media_type = {"final": "video/mp4", "qa": "image/jpeg", "receipt": "application/json"}[kind]
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})
