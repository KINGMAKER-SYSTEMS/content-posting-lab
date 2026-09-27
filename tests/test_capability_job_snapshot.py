"""Capability bursts decode one job history, without caching any writes."""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock, Thread

import pytest
from fastapi import HTTPException

from routers import control_plane as cp
from services.json_store import atomic_save


@pytest.fixture
def job_path(tmp_path, monkeypatch):
    path = tmp_path / "jobs.json"
    monkeypatch.setattr(cp, "_jobs_path", lambda: path)
    cp._jobs_snapshot = None
    cp._jobs_decode_pending.clear()
    atomic_save(path, {"jobs": {"one": {"status": "queued"}}, "version": 1})
    return path


def test_concurrent_capability_reads_decode_once(job_path, monkeypatch):
    original = Path.open
    reads = []
    lock = Lock()

    def counted(path, *args, **kwargs):
        if path == job_path:
            with lock:
                reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted)
    with ThreadPoolExecutor(max_workers=40) as pool:
        snapshots = list(pool.map(lambda _: cp._read_jobs_snapshot(), range(120)))
    assert len(reads) == 1
    assert all(row["jobs"]["one"]["status"] == "queued" for row in snapshots)


def test_progress_write_is_fresh_and_does_not_mutate_old_snapshot(job_path):
    original = cp._read_jobs_snapshot()
    cp._update_job("one", status="completed")
    assert original["jobs"]["one"]["status"] == "queued"
    assert cp._read_jobs_snapshot()["jobs"]["one"]["status"] == "completed"
    mutable = cp._load_jobs()
    mutable["jobs"]["one"]["status"] = "not persisted"
    assert cp._read_jobs_snapshot()["jobs"]["one"]["status"] == "completed"


def test_replacement_detected_even_with_same_size_and_mtime(job_path):
    cp._read_jobs_snapshot()
    before = job_path.stat()
    replacement = job_path.with_name("replacement.json")
    replacement.write_bytes(job_path.read_bytes().replace(b"queued", b"failed"))
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    replacement.replace(job_path)
    assert cp._read_jobs_snapshot()["jobs"]["one"]["status"] == "failed"


def test_missing_and_in_place_changes_never_serve_old_jobs(job_path):
    cp._read_jobs_snapshot()
    atomic_save(job_path, {"jobs": {"two": {"status": "running"}}})
    assert set(cp._read_jobs_snapshot()["jobs"]) == {"two"}
    job_path.unlink()
    assert cp._read_jobs_snapshot() == cp._empty_jobs()


def test_corrupt_file_fails_closed_never_poisons_cache_never_serves_stale(job_path):
    """T3 (C2b): a JSON error or partial file must never be treated as an
    empty store (that would silently under- or over-admit against whatever
    the file actually holds) and must never fall back to the old, now
    signature-mismatched snapshot either. It raises; capabilities() turns
    that into a 503, which the Worker already treats exactly like today's
    client timeout."""
    original = cp._read_jobs_snapshot()
    assert original["jobs"]["one"]["status"] == "queued"

    job_path.write_text("{")  # partial/invalid JSON, written in place
    with pytest.raises(cp._JobsDecodeFailed):
        cp._read_jobs_snapshot()

    # The cache must still hold the last GOOD snapshot, unpoisoned — not an
    # empty store, and not something built from the corrupt bytes.
    assert cp._jobs_snapshot is not None
    assert cp._jobs_snapshot.data["jobs"]["one"]["status"] == "queued"

    # A concurrent/later reader must also fail (never quietly serve the
    # stale "queued" snapshot whose signature no longer matches the file).
    with pytest.raises(cp._JobsDecodeFailed):
        cp._read_jobs_snapshot()

    # Recovery: once the file is valid again, the next read decodes fresh.
    atomic_save(job_path, {"jobs": {"two": {"status": "running"}}})
    assert set(cp._read_jobs_snapshot()["jobs"]) == {"two"}


def test_capabilities_returns_503_on_decode_failure(job_path, monkeypatch):
    monkeypatch.setattr(cp, "_current_intent_for_capabilities", lambda _: ({}, "hash"))
    job_path.write_text("{")
    with pytest.raises(HTTPException) as excinfo:
        cp.capabilities(x_rt_page_id="acct:page", x_page_id=None)
    assert excinfo.value.status_code == 503


def test_slow_external_decode_cannot_clobber_a_newer_write_cas(job_path, monkeypatch):
    """T2: compare-and-swap on generation. A decode that started against an
    OLDER file version must never overwrite a NEWER snapshot published (here,
    by an in-process write) while that decode was still working — no matter
    how long the decode takes to finish."""
    cp._read_jobs_snapshot()
    baseline_generation = cp._jobs_snapshot.generation

    # An external replacement — not through _save_jobs — is the "older"
    # version whose decode is about to be slowed down.
    atomic_save(job_path, {"jobs": {"one": {"status": "external-slow"}}})

    real_decode = cp._decode_jobs_snapshot
    entered = Event()
    release = Event()

    def slow_decode(generation):
        entered.set()
        release.wait(timeout=5)
        return real_decode(generation)

    monkeypatch.setattr(cp, "_decode_jobs_snapshot", slow_decode)

    result: dict = {}

    def read_in_background():
        result["snapshot"] = cp._read_jobs_snapshot()

    reader = Thread(target=read_in_background)
    reader.start()
    try:
        assert entered.wait(timeout=5), "background decode never started"

        # While that decode is stuck mid-flight, a fresh, unrelated write
        # happens and publishes immediately (no decode involved at all).
        cp._update_job("one", status="fresh-write")
        fresh_generation = cp._jobs_snapshot.generation
        assert fresh_generation > baseline_generation

        # Now let the slow, now-stale decode finish and try to publish.
        release.set()
        reader.join(timeout=5)
        assert not reader.is_alive()
    finally:
        release.set()
        reader.join(timeout=5)

    # The slow decode's result must have been discarded on publish: the live
    # snapshot is still the fresh write's, never the stale "external-slow"
    # content the slow decode read.
    assert cp._jobs_snapshot.generation == fresh_generation
    assert cp._jobs_snapshot.data["jobs"]["one"]["status"] == "fresh-write"
    # The background reader itself still got a self-consistent answer (the
    # file version it actually decoded), even though it lost the race.
    assert result["snapshot"]["jobs"]["one"]["status"] == "external-slow"


def test_capabilities_use_snapshot_not_mutable_transaction_load(job_path, monkeypatch):
    monkeypatch.setattr(cp, "_current_intent_for_capabilities", lambda _: ({}, "hash"))
    monkeypatch.setattr(cp, "list_registered_recipe_bindings", lambda *args: [])
    monkeypatch.setattr(cp, "load_engine_registry", lambda: ({}, "hash"))

    def unexpected():
        raise AssertionError("capability poll decoded the mutable job store")

    monkeypatch.setattr(cp, "_load_jobs", unexpected)
    before = json.dumps(cp._read_jobs_snapshot(), sort_keys=True)
    assert cp.capabilities(x_rt_page_id="acct:page", x_page_id=None)["capabilities"] == []
    assert json.dumps(cp._read_jobs_snapshot(), sort_keys=True) == before
