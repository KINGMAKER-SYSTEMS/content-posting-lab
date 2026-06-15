"""Tests for the ABN dynamic-UI capture helpers (services/abn_capture.py).

Pins the contract of `_cleanup_dir`, the extracted best-effort temp-dir remover that
runs in the capture's destructive bail/finally paths. It must always leave no orphan
_rec_<name>/ dir behind and must never raise, regardless of what's in (or missing from)
the path it's handed.
"""
from pathlib import Path

from services.abn_capture import _cleanup_dir


def test_cleanup_dir_removes_populated_dir(tmp_path):
    rec = tmp_path / "_rec_demo"
    rec.mkdir()
    (rec / "video.webm").write_bytes(b"x")
    (rec / "trace.json").write_text("{}")

    _cleanup_dir(rec)

    assert not rec.exists()  # files unlinked, dir rmdir'd


def test_cleanup_dir_missing_dir_is_noop(tmp_path):
    missing = tmp_path / "_rec_never_made"
    # must not raise on a path that was never created (no-webm bail path hits this)
    _cleanup_dir(missing)
    assert not missing.exists()


def test_cleanup_dir_swallows_unlink_errors(tmp_path, monkeypatch):
    rec = tmp_path / "_rec_locked"
    rec.mkdir()
    (rec / "video.webm").write_bytes(b"x")

    def boom(self):
        raise OSError("file is locked")

    # a locked/undeletable file must not propagate out of the destructive path
    monkeypatch.setattr(Path, "unlink", boom)
    _cleanup_dir(rec)  # no exception == pass
