"""Tests for the ABN dynamic-UI capture helpers (services/abn_capture.py).

Pins the contract of `_cleanup_dir`, the extracted best-effort temp-dir remover that
runs in the capture's destructive bail/finally paths. It must always leave no orphan
_rec_<name>/ dir behind and must never raise, regardless of what's in (or missing from)
the path it's handed.

Also pins that `capture_sync` writes the captured UI clip through the abn_assets GATEWAY
(episode-scoped {ep_id}/footage/s{N}_ui.mp4) rather than a flat ASSETS/{name}_ui.mp4 —
the off-schema flat write is the orphan-file blast radius the gateway exists to prevent.
"""
import importlib
import sys
import types
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


def test_capture_writes_through_gateway_not_flat(tmp_path, monkeypatch):
    """capture_sync must route the UI clip through asset_path_from_slug -> episode-scoped
    {ep_id}/footage/s{N}_ui.mp4, NEVER a flat ASSETS/{name}_ui.mp4. We point the gateway at a
    throwaway ASSETS_DIR and stub Playwright so it raises right after the path is computed; the
    computed output path is what we assert on (the orphan-file regression guard)."""
    monkeypatch.setenv("ABN_ASSETS_DIR", str(tmp_path))
    import services.agenticnews as an
    importlib.reload(an)
    import services.abn_assets as A
    importlib.reload(A)
    import services.abn_capture as cap
    importlib.reload(cap)  # rebinds capture_sync's module-level gateway imports to the reloaded A

    # Fake playwright so the import succeeds but sync_playwright() blows up inside the try —
    # capture_sync still executes the gateway path line + rec_dir mkdir before failing.
    fake = types.ModuleType("playwright.sync_api")

    def _boom():
        raise RuntimeError("no browser in CI")

    fake.sync_playwright = _boom
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)

    # spy on the gateway call to capture the path capture_sync actually requests
    seen = {}
    real = A.asset_path_from_slug

    def spy(flat_slug, kind, **kw):
        p = real(flat_slug, kind, **kw)
        seen["path"] = p
        return p

    monkeypatch.setattr(cap, "asset_path_from_slug", spy)

    name = "ep_648e806a_s0"
    assert cap.capture_sync("https://example.com", name) is None  # stubbed playwright bails

    out = seen["path"]
    assert out.parent.name == "footage"                      # webscroll layer subdir
    assert out.parent.parent.name == "ep_648e806a"           # episode-scoped, not flat
    assert out.name == "s0_ui.mp4"
    # the flat legacy path must NOT have been created at the ASSETS root
    assert not (Path(tmp_path) / f"{name}_ui.mp4").exists()
