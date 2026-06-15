"""Tests for the shared atomic JSON store (services/json_store.py)."""

import os
import threading
from datetime import datetime

import pytest

from services import json_store
from services.json_store import atomic_load, atomic_save


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
