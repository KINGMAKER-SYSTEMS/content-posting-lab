"""Hand local generations to a worker running on operator-owned hardware.

A deployed lab cannot reach a ComfyUI server on a private network, so the
connection is reversed: a worker next to that server polls this queue, renders
the job and uploads the finished video. Nothing on the private network is
exposed, and the lab never calls out.

State is in memory, like the generation job table it serves: a restart drops
queued hand-offs and the owning job fails closed rather than replaying.

Claims are not first-come-first-served: jobs for the model the worker's server
already holds go first (see services.local_render_order). The loaded model is
inferred from the last claim, which is exact for the one worker per server this
is built for.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from services import local_render_order

# A worker polls every few seconds while idle and heartbeats while rendering.
# Silence this long means nobody is there to take or finish the job.
WORKER_STALE_SECONDS = 180.0
_WAIT_SLICE_SECONDS = 2.0


class LocalWorkerError(RuntimeError):
    """The worker could not be reached or reported a failed render."""


class ClaimRejected(Exception):
    """The job is unknown, already finished, or held under another claim."""


@dataclass
class Job:
    id: str
    payload: dict
    future: asyncio.Future
    created: float
    claim_token: str | None = None
    claimed_at: float | None = None


_jobs: dict[str, Job] = {}
_pending: list[str] = []          # arrival order
_last_seen: float | None = None
_loaded_model: str | None = None  # model of the most recently claimed job


def reset() -> None:
    """Drop all state (tests)."""
    global _last_seen, _loaded_model
    for job in _jobs.values():
        if not job.future.done():
            job.future.cancel()
    _jobs.clear()
    _pending.clear()
    _last_seen = None
    _loaded_model = None


def worker_key() -> str:
    return os.getenv("LOCAL_WORKER_KEY", "").strip()


def touch() -> None:
    """Record that an authenticated worker just spoke to the lab."""
    global _last_seen
    _last_seen = time.monotonic()


def worker_online() -> bool:
    return _last_seen is not None and time.monotonic() - _last_seen < WORKER_STALE_SECONDS


def enqueue(payload: dict) -> Job:
    job = Job(
        id=uuid.uuid4().hex,
        payload=payload,
        future=asyncio.get_running_loop().create_future(),
        created=time.monotonic(),
    )
    _jobs[job.id] = job
    _pending.append(job.id)
    return job


def claim() -> dict | None:
    """Give the next job in batch order to the calling worker, or None."""
    global _loaded_model
    touch()
    # Drop hand-offs whose owner already gave up (timed out or cancelled).
    _pending[:] = [
        job_id for job_id in _pending
        if (job := _jobs.get(job_id)) is not None and not job.future.done()
    ]
    index = local_render_order.pick(
        [(_jobs[job_id].payload.get("model_id"), _jobs[job_id].created) for job_id in _pending],
        _loaded_model,
        time.monotonic(),
        local_render_order.max_wait_seconds(),
    )
    if index is None:
        return None
    job = _jobs[_pending.pop(index)]
    _loaded_model = job.payload.get("model_id")
    job.claim_token = secrets.token_urlsafe(24)
    job.claimed_at = time.monotonic()
    return {"id": job.id, "claim_token": job.claim_token, **job.payload}


def _claimed(job_id: str, claim_token: str) -> Job:
    job = _jobs.get(job_id)
    if job is None or job.future.done() or not job.claim_token:
        raise ClaimRejected(job_id)
    if not secrets.compare_digest(job.claim_token.encode(), (claim_token or "").encode()):
        raise ClaimRejected(job_id)
    return job


def heartbeat(job_id: str, claim_token: str) -> None:
    _claimed(job_id, claim_token)
    touch()


def complete(job_id: str, claim_token: str, video: Path) -> None:
    job = _claimed(job_id, claim_token)
    touch()
    job.future.set_result(video)


def fail(job_id: str, claim_token: str, message: str) -> None:
    job = _claimed(job_id, claim_token)
    touch()
    job.future.set_exception(LocalWorkerError(f"local worker: {message}"[:600]))


async def wait(job: Job, entry: dict, render_timeout: float) -> Path:
    """Wait for the worker's video. Fails closed when nobody is rendering it.

    Time spent queued behind other jobs is unbounded while a worker is alive;
    ``render_timeout`` only bounds the render itself, from the claim.
    """
    try:
        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(job.future), _WAIT_SLICE_SECONDS)
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()
            if job.claimed_at is None:
                if not worker_online() and now - job.created >= WORKER_STALE_SECONDS:
                    raise LocalWorkerError("no local worker is online to render this job")
                continue
            entry["status"] = "polling"
            if not worker_online():
                raise LocalWorkerError("the local worker went offline during the render")
            if now - job.claimed_at >= render_timeout:
                raise LocalWorkerError("the local render timed out")
    finally:
        # Whatever the outcome, a late upload or failure report is refused.
        if not job.future.done():
            job.future.cancel()
        _jobs.pop(job.id, None)
