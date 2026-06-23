"""Tests for the shared atomic JSON store (services/json_store.py)."""

import os
import threading
from datetime import datetime

import pytest

from services import json_store
from services.json_store import atomic_load, atomic_save, lock_for


def test_round_trip(tmp_path):
    p = tmp_path / "data.json"
    payload = {"version": 1, "items": [1, 2, 3], "name": "café"}
    atomic_save(p, payload)
    assert atomic_load(p) == payload


def test_missing_file_returns_default(tmp_path):
    p = tmp_path / "nope.json"
    assert atomic_load(p) is None
    assert atomic_load(p, default=[]) == []
    assert atomic_load(p, default={"x": 1}) == {"x": 1}


def test_corrupt_file_returns_default(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert atomic_load(p, default={}) == {}


def test_non_ascii_preserved(tmp_path):
    p = tmp_path / "u.json"
    atomic_save(p, {"emoji": "🎬", "name": "naïve"})
    # ensure_ascii=False keeps the raw characters on disk
    assert "🎬" in p.read_text(encoding="utf-8")
    assert atomic_load(p)["name"] == "naïve"


def test_default_serializer_for_datetimes(tmp_path):
    p = tmp_path / "jobs.json"
    jobs = [{"job_id": "a", "created": datetime(2026, 6, 15, 12, 0, 0)}]
    # Without a default serializer this raises; with default=str it works (upload.py case).
    with pytest.raises(TypeError):
        atomic_save(p, jobs)
    atomic_save(p, jobs, default=str)
    assert atomic_load(p)[0]["job_id"] == "a"


def test_atomic_no_leftover_tmp(tmp_path):
    p = tmp_path / "x.json"
    atomic_save(p, {"ok": True})
    # tmp sidecar must be renamed away, not left behind
    assert p.exists()
    assert not (tmp_path / "x.json.tmp").exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_accepts_str_path(tmp_path):
    p = tmp_path / "s.json"
    atomic_save(str(p), {"a": 1})
    assert atomic_load(str(p)) == {"a": 1}


def test_failed_write_leaves_original_intact(tmp_path):
    """A serialization failure must not corrupt an existing good file."""
    p = tmp_path / "g.json"
    atomic_save(p, {"good": 1})
    with pytest.raises(TypeError):
        atomic_save(p, {"bad": object()})  # not JSON-serializable
    # original is untouched because the write went to a tmp file first
    assert atomic_load(p) == {"good": 1}


# --- failure-recovery + corruption-protection gaps (this ticket) ------------


def test_serialize_failure_cleans_up_tmp(tmp_path):
    """A failed serialize must not leak a tmp sidecar."""
    p = tmp_path / "g.json"
    atomic_save(p, {"good": 1})
    with pytest.raises(TypeError):
        atomic_save(p, {"bad": object()})
    assert not list(tmp_path.glob("*.tmp")), "tmp leaked after serialize failure"
    assert atomic_load(p) == {"good": 1}


def test_fsync_failure_preserves_original_and_no_tmp(tmp_path, monkeypatch):
    """If os.fsync raises mid-write, the original survives and no tmp is left.

    fsync failing on a real volume (ENOSPC / EIO) is exactly the crash class the
    atomic write exists to guard against.
    """
    p = tmp_path / "g.json"
    atomic_save(p, {"good": 1})

    def boom(_fd):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(json_store.os, "fsync", boom)
    with pytest.raises(OSError):
        atomic_save(p, {"new": 2})

    assert atomic_load(p) == {"good": 1}  # untouched
    assert not list(tmp_path.glob("*.tmp")), "tmp leaked after fsync failure"


def test_replace_failure_preserves_original_and_no_tmp(tmp_path, monkeypatch):
    """If os.replace raises, the original survives and the tmp is cleaned up."""
    p = tmp_path / "g.json"
    atomic_save(p, {"good": 1})

    def boom(_src, _dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(json_store.os, "replace", boom)
    with pytest.raises(OSError):
        atomic_save(p, {"new": 2})

    assert atomic_load(p) == {"good": 1}  # untouched
    assert not list(tmp_path.glob("*.tmp")), "tmp leaked after replace failure"


def test_recovers_after_leftover_tmp(tmp_path):
    """A stale tmp left behind by an earlier crash must never break load or save.

    atomic_load reads ``path`` (not the tmp), and a fresh atomic_save must still
    succeed and leave exactly one good file.
    """
    p = tmp_path / "g.json"
    atomic_save(p, {"good": 1})
    # simulate a crash between fsync and replace: a stale tmp sidecar lingers
    leftover = p.with_suffix(p.suffix + ".66666.99999.tmp")
    leftover.write_text("{ half-written garbage", encoding="utf-8")

    # load ignores the stale tmp entirely
    assert atomic_load(p) == {"good": 1}
    # a subsequent save still works (uses its own unique tmp, replaces cleanly)
    atomic_save(p, {"good": 2})
    assert atomic_load(p) == {"good": 2}
    # our save's own tmp is gone; only the pre-existing stale one (if any) remains
    assert not p.with_suffix(p.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp").exists()


def test_keyboardinterrupt_propagates_without_cleanup(tmp_path, monkeypatch):
    """A BaseException (SystemExit/KeyboardInterrupt) must propagate, not be
    swallowed/masked by the best-effort tmp cleanup.

    The cleanup now catches ``Exception`` (not ``BaseException``), so a
    KeyboardInterrupt mid-write tears out cleanly and the unlink branch is never
    entered for it — a stale tmp is harmless (atomic_load reads ``path``).
    """
    p = tmp_path / "g.json"
    atomic_save(p, {"good": 1})

    def boom(_src, _dst):
        raise KeyboardInterrupt("simulated shutdown")

    unlink_called = []
    real_unlink = json_store.os.unlink

    def tracking_unlink(path):
        unlink_called.append(path)
        return real_unlink(path)

    monkeypatch.setattr(json_store.os, "replace", boom)
    monkeypatch.setattr(json_store.os, "unlink", tracking_unlink)

    with pytest.raises(KeyboardInterrupt):
        atomic_save(p, {"new": 2})

    # cleanup branch must be skipped for BaseException
    assert not unlink_called, "best-effort cleanup ran on KeyboardInterrupt"
    # original is untouched regardless
    assert atomic_load(p) == {"good": 1}


def test_concurrent_saves_same_path_no_corruption(tmp_path):
    """Many threads atomic_save distinct payloads to the SAME path at once.

    A shared tmp filename would let writers clobber each other's bytes and make
    os.replace race on an already-renamed-away tmp. The per-writer unique tmp
    suffix prevents that: every save lands a *complete, valid* document and the
    final file is one of the written payloads (never a half-merged mess), with no
    tmp sidecars leaked.
    """
    p = tmp_path / "race.json"
    atomic_save(p, {"writer": -1})  # seed so concurrent loads/saves have a base

    n = 50
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def writer(i):
        barrier.wait()  # release all threads together to maximize overlap
        try:
            atomic_save(p, {"writer": i, "payload": list(range(i))})
        except BaseException as exc:  # pragma: no cover - surfaced via assert
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent atomic_save raised: {errors[:3]}"
    # final file must be a complete, valid doc written by exactly one of the writers
    final = atomic_load(p)
    assert final is not None and "writer" in final
    assert final["payload"] == list(range(final["writer"]))
    # no tmp sidecar may survive the storm
    assert not list(tmp_path.glob("*.tmp")), "tmp leaked under concurrent saves"


# --- orphan-tmp sweep: reap crash-leftover tmps by dead pid (this ticket) ----


def _dead_pid():
    """A pid that is essentially guaranteed not to be running."""
    pid = 999999
    while json_store._pid_alive(pid):
        pid += 1
    return pid


def test_save_sweeps_orphan_tmp_of_dead_pid(tmp_path):
    """A successful save reaps a leftover tmp whose owning process is gone.

    This closes the documented hazard: a crash between fsync and os.replace
    orphans a <name>.<pid>.<tid>.tmp on the long-lived volume. The next save
    cleans it up (only when the pid is dead, so live writers are safe).
    """
    p = tmp_path / "g.json"
    atomic_save(p, {"good": 1})
    orphan = p.with_suffix(p.suffix + f".{_dead_pid()}.123.tmp")
    orphan.write_text("{ half-written", encoding="utf-8")

    atomic_save(p, {"good": 2})

    assert not orphan.exists(), "orphan tmp of a dead pid was not reaped"
    assert atomic_load(p) == {"good": 2}
    assert not list(tmp_path.glob("*.tmp"))


def test_save_does_not_sweep_tmp_of_live_pid(tmp_path):
    """A tmp belonging to a still-running process (e.g. a concurrent writer)
    must NOT be reaped — only dead-pid orphans are safe to remove."""
    p = tmp_path / "g.json"
    atomic_save(p, {"good": 1})
    # use the current process pid -> it is alive, so the sweep must spare it
    live = p.with_suffix(p.suffix + f".{os.getpid()}.999999.tmp")
    live.write_text("in-flight", encoding="utf-8")

    atomic_save(p, {"good": 2})

    assert live.exists(), "sweep removed a live writer's in-flight tmp"
    live.unlink()


def test_sweep_ignores_unrelated_and_malformed_files(tmp_path):
    """The sweep only touches this path's well-formed tmp sidecars."""
    p = tmp_path / "g.json"
    atomic_save(p, {"good": 1})
    other = tmp_path / "other.json.5.6.tmp"   # different base name
    other.write_text("x", encoding="utf-8")
    weird = p.with_suffix(p.suffix + ".notapid.tmp")  # no pid.tid pattern
    weird.write_text("x", encoding="utf-8")

    atomic_save(p, {"good": 2})

    assert other.exists(), "sweep reaped an unrelated file"
    assert weird.exists(), "sweep reaped a non-matching tmp"
    other.unlink()
    weird.unlink()


# --- lock_for(): per-path reentrant transaction guard (this ticket) ----------


def test_lock_for_is_reentrant_rlock(tmp_path):
    """lock_for returns an RLock that one thread can acquire nested without
    self-deadlocking — the property roster/telegram transactions rely on when a
    load-modify-save calls into another transaction on the same file."""
    lock = lock_for(tmp_path / "cfg.json")
    assert isinstance(lock, type(threading.RLock()))
    with lock:
        with lock:  # nested acquire on the same thread must not block
            assert True


def test_lock_for_same_path_returns_same_lock(tmp_path):
    """Same logical path -> identical lock object (so racing callers actually
    serialize on one another), regardless of str vs Path or trailing forms."""
    p = tmp_path / "cfg.json"
    a = lock_for(p)
    b = lock_for(str(p))
    c = lock_for(tmp_path / "cfg.json")
    assert a is b is c


def test_lock_for_different_paths_return_different_locks(tmp_path):
    """Distinct files must not share a lock — guarding one file shouldn't block
    an unrelated transaction on another."""
    a = lock_for(tmp_path / "one.json")
    b = lock_for(tmp_path / "two.json")
    assert a is not b


def test_lock_for_serializes_concurrent_transactions(tmp_path):
    """The lock actually serializes racing read-modify-write transactions.

    Without holding lock_for, many threads doing load->mutate->save on the same
    file lose increments (TOCTOU: each reads the same base before any writes).
    Holding the per-path lock for the whole transaction makes the final count
    exact — this is the race the function exists to close.
    """
    p = tmp_path / "counter.json"
    atomic_save(p, {"n": 0})
    lock = lock_for(p)

    n = 40
    barrier = threading.Barrier(n)

    def bump():
        barrier.wait()
        with lock:  # whole load-modify-save serialized
            data = atomic_load(p, default={"n": 0})
            data["n"] += 1
            atomic_save(p, data)

    threads = [threading.Thread(target=bump) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert atomic_load(p)["n"] == n, "lost updates: lock did not serialize transactions"


def test_lock_for_provides_mutual_exclusion(tmp_path):
    """Only one thread may hold a given path's lock at a time. A second thread's
    non-blocking acquire must fail while the first holds it, then succeed after
    release."""
    lock = lock_for(tmp_path / "excl.json")
    acquired_while_held = []
    released = threading.Event()
    holding = threading.Event()

    def holder():
        with lock:
            holding.set()
            released.wait(timeout=5)

    t = threading.Thread(target=holder)
    t.start()
    assert holding.wait(timeout=5)
    # a different thread trying non-blocking acquire must be refused while held
    def probe():
        got = lock.acquire(blocking=False)
        acquired_while_held.append(got)
        if got:
            lock.release()

    pt = threading.Thread(target=probe)
    pt.start()
    pt.join()
    released.set()
    t.join()

    assert acquired_while_held == [False], "lock was not mutually exclusive across threads"
    # after release the lock is free again
    assert lock.acquire(blocking=False)
    lock.release()
