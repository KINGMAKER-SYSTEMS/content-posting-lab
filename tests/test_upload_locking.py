"""Cross-process upload claims use the real stable store lock; no browser runs."""

import asyncio
from contextlib import contextmanager
import fcntl
import multiprocessing

import pytest

import services.upload as upload_svc


def _claim_owner(store, job_id, ready, release_claim, started, release_provider, results):
    upload_svc.UPLOAD_JOBS_FILE = store
    save = upload_svc._save_jobs

    def pause_claim(jobs):
        if jobs[0]["status"] == "uploading":
            ready.set()
            assert release_claim.wait(15)
        save(jobs)

    def provider(job):
        results.put(("provider", job["job_id"]))
        started.set()
        assert release_provider.wait(15)
        return "Completed"

    upload_svc._save_jobs = pause_claim
    upload_svc._run_upload_sync = provider
    asyncio.run(upload_svc._process_single_job(job_id))


def _contender(store, job_id, operation, attempting, read, release_read, results):
    upload_svc.UPLOAD_JOBS_FILE = store
    lock = upload_svc._job_store_write_lock
    load = upload_svc._read_stored_jobs

    @contextmanager
    def announce_lock():
        attempting.set()
        with lock():
            yield

    def pause_read(*, _lock_held=False):
        jobs = load(_lock_held=_lock_held)
        read.set()
        assert release_read.wait(15)
        return jobs

    def provider(job):
        results.put(("provider", job["job_id"]))
        return "Completed"

    upload_svc._job_store_write_lock = announce_lock
    upload_svc._read_stored_jobs = pause_read
    upload_svc._run_upload_sync = provider
    if operation == "claim":
        asyncio.run(upload_svc._process_single_job(job_id))
        results.put(("claim", "finished"))
    else:
        results.put(("cancel", upload_svc.cancel_job(job_id)["status"]))


def _store_lock_is_held(store):
    with open(f"{store}.lock", "a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


@pytest.mark.parametrize("operation", ["claim", "cancel"])
def test_process_claim_serializes_competing_transition(tmp_path, monkeypatch, operation):
    """Pause before saving the winning claim, while the other process enters.

    The contender's read is held until the claim is durable. Without flock it
    retains a stale queued snapshot and either posts twice or cancels a Post.
    """
    store = str(tmp_path / "upload_jobs.json")
    monkeypatch.setattr(upload_svc, "UPLOAD_JOBS_FILE", store)
    monkeypatch.setattr(upload_svc, "require_upload_runtime", lambda: None)
    job = upload_svc.submit_job(video_path="/tmp/clip.mp4", account_name="linked")
    ctx = multiprocessing.get_context("spawn")
    ready, release_claim, started, release_provider, attempting, read, release_read = (
        ctx.Event() for _ in range(7)
    )
    results = ctx.Queue()
    owner = ctx.Process(target=_claim_owner, args=(
        store, job["job_id"], ready, release_claim, started, release_provider, results,
    ))
    contender = ctx.Process(target=_contender, args=(
        store, job["job_id"], operation, attempting, read, release_read, results,
    ))
    processes = []
    try:
        owner.start()
        processes.append(owner)
        assert ready.wait(15)
        contender.start()
        processes.append(contender)
        assert attempting.wait(15)
        if _store_lock_is_held(store):
            # The real lock keeps the contender out until the owner saves.
            assert not read.is_set()
        else:
            # A lockless build must reach and pause after its stale read before
            # the owner is allowed to save.
            assert read.wait(15)
        release_claim.set()
        assert started.wait(15)
        assert read.wait(15)
        release_read.set()
        contender.join(15)
        assert contender.exitcode == 0
        observed = [results.get(timeout=5), results.get(timeout=5)]
        if operation == "cancel":
            assert ("cancel", "uploading") in observed
        else:
            assert observed.count(("provider", job["job_id"])) == 1
            assert ("claim", "finished") in observed
        assert upload_svc.get_job(job["job_id"])["status"] == "uploading"
        release_provider.set()
        owner.join(15)
        assert owner.exitcode == 0
        assert upload_svc.get_job(job["job_id"])["status"] == "completed"
    finally:
        release_claim.set()
        release_read.set()
        release_provider.set()
        for process in processes:
            process.join(2)
            if process.is_alive():
                process.terminate()
                process.join(5)
        results.close()
        results.join_thread()
