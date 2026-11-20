"""Worker-facing endpoints for local generation (see services/local_worker_queue).

Every route requires ``X-Worker-Key`` matching ``LOCAL_WORKER_KEY``. When the
key is unset the routes answer 503, so a deployment that never configured a
worker exposes nothing. The browser API key neither grants nor blocks access.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from providers.base import _provider_download_limit
from services import local_worker_queue as queue

router = APIRouter()

_MAX_CLAIM_WAIT_SECONDS = 25.0


def _require_worker_key(x_worker_key: str | None) -> None:
    expected = queue.worker_key()
    if not expected:
        raise HTTPException(status_code=503, detail="local worker access is not configured")
    supplied = x_worker_key.strip() if isinstance(x_worker_key, str) else ""
    if not supplied or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="invalid or missing worker key")


def _spool_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "content-lab-local-worker"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


@router.post("/claim")
async def claim(wait: float = 0.0, busy: bool = False, x_worker_key: str | None = Header(default=None)):
    """Long-poll for the next job. ``{"job": null}`` means nothing is waiting.

    ``busy=true`` is a check-in from a worker whose ComfyUI server is rendering
    something else: it stays online but takes no job, so new jobs go to their
    hosted twin (providers/backend_order.py) instead of waiting behind it.
    """
    _require_worker_key(x_worker_key)
    if busy:
        queue.touch(busy=True)
        return {"job": None}
    deadline = time.monotonic() + max(0.0, min(wait, _MAX_CLAIM_WAIT_SECONDS))
    while True:
        job = queue.claim()
        if job is not None or time.monotonic() >= deadline:
            return {"job": job}
        await asyncio.sleep(0.5)


@router.post("/jobs/{job_id}/heartbeat")
async def heartbeat(
    job_id: str,
    x_worker_key: str | None = Header(default=None),
    x_claim_token: str | None = Header(default=None),
):
    _require_worker_key(x_worker_key)
    try:
        queue.heartbeat(job_id, x_claim_token or "")
    except queue.ClaimRejected:
        raise HTTPException(status_code=409, detail="job is not held under this claim")
    return {"ok": True}


class _Failure(BaseModel):
    error: str


@router.post("/jobs/{job_id}/fail")
async def fail(
    job_id: str,
    body: _Failure,
    x_worker_key: str | None = Header(default=None),
    x_claim_token: str | None = Header(default=None),
):
    _require_worker_key(x_worker_key)
    try:
        queue.fail(job_id, x_claim_token or "", body.error)
    except queue.ClaimRejected:
        raise HTTPException(status_code=409, detail="job is not held under this claim")
    return {"ok": True}


@router.post("/jobs/{job_id}/result")
async def result(
    job_id: str,
    request: Request,
    x_worker_key: str | None = Header(default=None),
    x_claim_token: str | None = Header(default=None),
):
    """Receive the finished MP4 as the raw request body, bounded in bytes."""
    _require_worker_key(x_worker_key)
    try:
        queue.heartbeat(job_id, x_claim_token or "")
    except queue.ClaimRejected:
        raise HTTPException(status_code=409, detail="job is not held under this claim")

    limit = _provider_download_limit()
    descriptor, filename = tempfile.mkstemp(prefix=f"{job_id}-", suffix=".mp4", dir=_spool_dir())
    dest = Path(filename)
    accepted = False
    total = 0
    head = b""
    try:
        with os.fdopen(descriptor, "wb") as f:
            async for chunk in request.stream():
                total += len(chunk)
                if total > limit:
                    raise HTTPException(status_code=413, detail="video exceeds the artifact size limit")
                if len(head) < 12:
                    head += chunk[: 12 - len(head)]
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        # ISO base media (MP4/MOV) carries "ftyp" at byte 4; refuse anything else.
        if total < 12 or head[4:8] != b"ftyp":
            raise HTTPException(status_code=400, detail="upload is not an MP4 video")
        queue.complete(job_id, x_claim_token or "", dest)
        accepted = True
    except queue.ClaimRejected:
        raise HTTPException(status_code=409, detail="job is not held under this claim")
    finally:
        if not accepted:
            dest.unlink(missing_ok=True)
    return {"ok": True, "bytes": total}
