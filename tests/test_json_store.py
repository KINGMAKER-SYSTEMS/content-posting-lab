"""Tests for the shared atomic JSON store (services/json_store.py)."""

from datetime import datetime

import pytest

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
