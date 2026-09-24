"""Capability bursts decode one job history, without caching any writes."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

import pytest

from routers import control_plane as cp
from services.json_store import atomic_save


@pytest.fixture
def job_path(tmp_path, monkeypatch):
    path = tmp_path / "jobs.json"
    monkeypatch.setattr(cp, "_jobs_path", lambda: path)
    cp._jobs_snapshot = None
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


def test_missing_corrupt_and_in_place_changes_never_serve_old_jobs(job_path):
    cp._read_jobs_snapshot()
    job_path.write_text("{")
    assert cp._read_jobs_snapshot() == cp._empty_jobs()
    atomic_save(job_path, {"jobs": {"two": {"status": "running"}}})
    assert set(cp._read_jobs_snapshot()["jobs"]) == {"two"}
    job_path.unlink()
    assert cp._read_jobs_snapshot() == cp._empty_jobs()


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
