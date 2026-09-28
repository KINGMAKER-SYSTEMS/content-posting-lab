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


def test_mid_read_in_place_rewrite_same_ino_mtime_size_is_detected(job_path, monkeypatch):
    """T1/stale-serve (Tides review of PR #185, round 2, defect 4): an
    in-place rewrite (`cp -p`, `touch -r`, a restore tool) that preserves
    inode, mtime AND size — landing WHILE a decode is mid-read — must never
    be published as if it were the pre-rewrite content, keyed to exactly
    what the file now reports. ctime cannot be forced back by the rewrite
    (it always reflects the last metadata change), which is the
    discriminator _jobs_read_consistency_signature must use here.

    Without the fix, this decode passes its (ino, mtime, size) check,
    publishes the STALE pre-rewrite bytes under the NEW (ctime-bearing)
    signature, and every later request is a cache hit on that stale data —
    forever, until the next write. With the fix, the rewrite is detected
    (fail closed, T3), and the next attempt decodes the real, current
    content."""
    cp._read_jobs_snapshot()  # publish "queued"

    # A signature change through the normal (external-replace) path so the
    # NEXT read is a genuine decode, starting the mid-read race clean.
    atomic_save(job_path, {"jobs": {"one": {"status": "queued"}}, "version": 1})
    cp._jobs_snapshot = None
    cp._jobs_decode_pending.clear()

    real_json_load = cp.json.load
    rewrite_happened = {}

    def load_then_rewrite_in_place(handle):
        data = real_json_load(handle)
        if rewrite_happened:
            # One-shot: only the FIRST decode races with a rewrite. Later
            # calls (recovery, after the file has genuinely settled) must
            # decode normally, or this mock would rewrite forever and no
            # read could ever succeed.
            return data
        before_stat = job_path.stat()
        rewritten = job_path.read_bytes().replace(b"queued", b"failed")
        assert len(rewritten) == len(job_path.read_bytes())  # same size
        job_path.write_bytes(rewritten)
        os.utime(job_path, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
        after_stat = job_path.stat()
        assert after_stat.st_ino == before_stat.st_ino
        assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
        assert after_stat.st_size == before_stat.st_size
        assert after_stat.st_ctime_ns != before_stat.st_ctime_ns
        rewrite_happened["done"] = True
        return data

    stalling_json = type(cp.json)("stalling_json_stub_2")
    stalling_json.__dict__.update(cp.json.__dict__)
    stalling_json.load = load_then_rewrite_in_place
    monkeypatch.setattr(cp, "json", stalling_json)

    with pytest.raises(cp._JobsDecodeFailed):
        cp._read_jobs_snapshot()
    assert rewrite_happened.get("done") is True

    # Never silently caches the pre-rewrite content, and never keeps
    # answering "queued" once the file has settled.
    atomic_save(job_path, {"jobs": {"one": {"status": "settled"}}})
    assert cp._read_jobs_snapshot()["jobs"]["one"]["status"] == "settled"


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


def test_non_decode_exception_never_strands_pending_ticket(job_path, monkeypatch):
    """Regression (Tides review of PR #185): the single-flight leader used to
    catch only `_JobsDecodeFailed`. Any other exception out of
    `_decode_jobs_snapshot`/`_publish_jobs_snapshot` (a `MemoryError` on a
    huge store, a bug, ...) skipped both the `_jobs_decode_pending.pop` and
    `pending.event.set()`, so every later reader of that signature blocked
    forever on `event.wait()` with no timeout — pinning threadpool threads
    until restart. Base released its lock via `with` on any exception; this
    must too. Every caller (leader included) must come back with
    `_JobsDecodeFailed` (503-mapped), never hang, regardless of interleaving."""
    atomic_save(job_path, {"jobs": {"one": {"status": "queued"}}, "version": 1})
    cp._jobs_snapshot = None
    cp._jobs_decode_pending.clear()

    def boom(generation):
        raise MemoryError("simulated: store too large to decode")

    monkeypatch.setattr(cp, "_decode_jobs_snapshot", boom)

    outcomes: dict[int, BaseException | None] = {}

    def call(index: int) -> None:
        try:
            cp._read_jobs_snapshot()
        except BaseException as error:  # capturing for assertion, not swallowing
            outcomes[index] = error
        else:
            outcomes[index] = None

    threads = [Thread(target=call, args=(i,), daemon=True) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads), (
        "a reader hung — the pending decode ticket was leaked"
    )
    assert len(outcomes) == 5
    assert all(isinstance(error, cp._JobsDecodeFailed) for error in outcomes.values()), outcomes
    # The ticket must be cleared, not stuck, so the next signature change
    # gets a fresh leader instead of joining a permanently-broken wait.
    assert cp._jobs_decode_pending == {}


def test_capabilities_returns_503_on_decode_failure(job_path, monkeypatch):
    monkeypatch.setattr(cp, "_current_intent_for_capabilities", lambda _: ({}, "hash"))
    job_path.write_text("{")
    with pytest.raises(HTTPException) as excinfo:
        cp.capabilities(x_rt_page_id="acct:page", x_page_id=None)
    assert excinfo.value.status_code == 503


def test_capabilities_returns_503_on_registry_failure_and_never_caches(job_path, monkeypatch):
    """A registry that fails to load must not become HTTP 200 with no
    capabilities, and must never cache that failure-derived emptiness."""
    monkeypatch.setattr(cp, "_current_intent_for_capabilities", lambda _: ({}, "hash"))
    monkeypatch.setattr(cp, "list_registered_recipe_bindings", lambda *args: [])

    def boom():
        raise ValueError("content engine registry is invalid")

    monkeypatch.setattr(cp, "load_engine_registry", boom)
    cp._capabilities_cache.clear()
    with pytest.raises(HTTPException) as excinfo:
        cp.capabilities(x_rt_page_id="acct:page", x_page_id=None)
    assert excinfo.value.status_code == 503
    assert excinfo.value.detail == "engine_registry_unavailable"
    assert cp._capabilities_cache == {}


def test_slow_external_decode_cannot_clobber_a_newer_write_cas(job_path, monkeypatch):
    """T2: compare-and-swap on generation. A decode that started against an
    OLDER file version must never overwrite a NEWER snapshot published (here,
    by an in-process write) while that decode was still working — no matter
    how long the decode finishes.

    Staging note (fixed after a Tides review of PR #185 caught this): the
    stall MUST happen AFTER `json.load` has actually read the stale bytes
    off disk, not before `_decode_jobs_snapshot` even opens the file. A
    stall staged too early means the "slow" decode's `json.load` call runs
    only once the file has already been overwritten by the fresh write, so
    it silently reads the FRESH bytes and never holds anything stale at
    all — every assertion about discarding a stale result then passes
    vacuously, whatever the CAS logic does. Proof: with the stall staged
    before the read (the original, buggy staging), a mutant that makes the
    leader return its own decoded snapshot instead of the CAS-published one
    (i.e. skips the compare-and-swap entirely) still passes this test."""
    cp._read_jobs_snapshot()
    baseline_generation = cp._jobs_snapshot.generation

    # An external replacement — not through _save_jobs — is the "older"
    # version whose decode is about to be slowed down.
    atomic_save(job_path, {"jobs": {"one": {"status": "external-slow"}}})

    real_json_load = cp.json.load
    entered = Event()
    release = Event()

    def slow_json_load(handle):
        # Read the stale "external-slow" bytes for real, THEN stall — so
        # the decode genuinely holds stale data when it is released, not
        # whatever happens to be on disk by the time it wakes up.
        data = real_json_load(handle)
        entered.set()
        release.wait(timeout=5)
        return data

    # Patch the `json` name as seen from inside control_plane.py, not the
    # shared `json` module globally — `_decode_jobs_snapshot` calls
    # `json.load` via that module-local binding.
    stalling_json = type(cp.json)("stalling_json_stub")
    stalling_json.__dict__.update(cp.json.__dict__)
    stalling_json.load = slow_json_load
    monkeypatch.setattr(cp, "json", stalling_json)

    result: dict = {}

    def read_in_background():
        result["snapshot"] = cp._read_jobs_snapshot()

    reader = Thread(target=read_in_background, daemon=True)
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
    # _publish_jobs_snapshot returns whichever snapshot is live AFTER the
    # CAS attempt — so even the reader that did the slow decode itself never
    # gets handed its own now-stale result; it gets the newer one instead.
    # That is stronger than merely "the cache isn't corrupted": no caller of
    # _read_jobs_snapshot(), winner or loser of the race, can ever observe a
    # value staler than what is already known to be current.
    assert result["snapshot"]["jobs"]["one"]["status"] == "fresh-write"


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
