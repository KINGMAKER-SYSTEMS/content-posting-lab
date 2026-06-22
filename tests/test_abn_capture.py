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

    import shutil

    def boom(*a, **kw):
        raise OSError("dir is locked")

    # a locked/undeletable tree must not propagate out of the destructive path;
    # _cleanup_dir now delegates to fsutil.safe_rmtree, which catches OSError
    monkeypatch.setattr(shutil, "rmtree", boom)
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
    fake.TimeoutError = type("TimeoutError", (Exception,), {})
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


def _fake_playwright(inner_text_side_effect, timeout_exc):
    """Build a fake playwright.sync_api whose page.inner_text triggers a side effect.

    Returns the module so a test can install it via monkeypatch.setitem. Records whether
    the page got as far as scrolling (page.evaluate) — proof the bail happened BEFORE the
    worthless recording was driven."""
    state = {"scrolled": False}

    class _Page:
        def goto(self, *a, **k): pass
        def wait_for_timeout(self, *a, **k): pass
        def get_by_role(self, *a, **k):
            raise timeout_exc("no consent button")  # skip the cookie loop
        def inner_text(self, *a, **k):
            return inner_text_side_effect()
        def evaluate(self, *a, **k):
            state["scrolled"] = True
            return 2000

    class _Ctx:
        def new_page(self): return _Page()
        def close(self): pass

    class _Browser:
        def new_context(self, *a, **k): return _Ctx()
        def close(self): pass

    class _Chromium:
        def launch(self, *a, **k): return _Browser()

    class _PW:
        chromium = _Chromium()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    fake = types.ModuleType("playwright.sync_api")
    fake.sync_playwright = lambda: _PW()
    fake.TimeoutError = timeout_exc
    return fake, state


def _reload_capture(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("ABN_ASSETS_DIR", str(tmp_path))
    import services.agenticnews as an
    importlib.reload(an)
    import services.abn_assets as A
    importlib.reload(A)
    import services.abn_capture as cap
    importlib.reload(cap)
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)
    return cap


def test_inner_text_non_timeout_propagates_to_outer_handler(tmp_path, monkeypatch):
    """An UNEXPECTED inner_text failure (e.g. browser crash / nav error) must NOT be
    swallowed into txt='' — that made real 504/bot-wall breakage indistinguishable from a
    clean page. The exception must propagate to the outer handler: capture bails (None),
    the worthless scroll/record is never driven, and the temp rec dir is cleaned up."""
    timeout_exc = type("TimeoutError", (Exception,), {})

    def boom():
        raise RuntimeError("Target page, context or browser has been closed")

    fake, state = _fake_playwright(boom, timeout_exc)
    cap = _reload_capture(tmp_path, monkeypatch, fake)

    # Count cleanup calls to distinguish the two code paths:
    #  - FIXED (re-raise): exception propagates straight to the outer handler, so the only
    #    cleanup is the finally{} call  -> exactly 1 call.
    #  - BROKEN (bare except -> txt=""): the error is swallowed, the near-empty guard then
    #    runs its OWN explicit _cleanup_dir before the finally{} one -> 2 calls. This is the
    #    exact regression: a real browser/nav failure was masquerading as an empty page.
    calls = {"n": 0}
    real_cleanup = cap._cleanup_dir

    def counting_cleanup(p):
        calls["n"] += 1
        return real_cleanup(p)

    monkeypatch.setattr(cap, "_cleanup_dir", counting_cleanup)

    assert cap.capture_sync("https://example.com", "ep_deadbeef_s1") is None
    assert state["scrolled"] is False  # bailed BEFORE recording worthless footage
    assert calls["n"] == 1  # propagated to outer handler, did NOT route through near-empty bail
    # finally{} cleanup ran — no orphan _rec_ dir under the episode footage dir
    foot = Path(tmp_path) / "ep_deadbeef" / "footage"
    assert not list(foot.glob("_rec_*"))


def test_inner_text_timeout_degrades_to_empty_and_bails_near_empty(tmp_path, monkeypatch):
    """A genuine inner_text TIMEOUT is the one case allowed to degrade to txt='' — which the
    near-empty guard then treats as worthless footage and bails (None), same clean path. The
    scroll/record must NOT be driven for an empty page either."""
    timeout_exc = type("TimeoutError", (Exception,), {})

    def slow():
        raise timeout_exc("inner_text exceeded timeout")

    fake, state = _fake_playwright(slow, timeout_exc)
    cap = _reload_capture(tmp_path, monkeypatch, fake)

    assert cap.capture_sync("https://example.com", "ep_cafef0_s2") is None
    assert state["scrolled"] is False  # near-empty guard bailed before recording
    foot = Path(tmp_path) / "ep_cafef0" / "footage"
    assert not list(foot.glob("_rec_*"))


def test_inner_text_none_bails_near_empty(tmp_path, monkeypatch):
    """Playwright inner_text() can return None (no readable body) WITHOUT raising. The
    `... or ""` coercion on line 83 must turn that into txt='' so the near-empty guard treats
    it as worthless footage and bails (None) — never an AttributeError on None.lower()."""
    timeout_exc = type("TimeoutError", (Exception,), {})

    fake, state = _fake_playwright(lambda: None, timeout_exc)
    cap = _reload_capture(tmp_path, monkeypatch, fake)

    assert cap.capture_sync("https://example.com", "ep_d00d11_s3") is None
    assert state["scrolled"] is False  # near-empty guard bailed before recording
    foot = Path(tmp_path) / "ep_d00d11" / "footage"
    assert not list(foot.glob("_rec_*"))


def test_inner_text_empty_string_bails_near_empty(tmp_path, monkeypatch):
    """A body that returns "" (or near-empty markup) before any timeout — e.g. a challenge page
    whose content lives in an iframe — must hit the near-empty guard and bail (None) without
    driving the worthless scroll/record."""
    timeout_exc = type("TimeoutError", (Exception,), {})

    fake, state = _fake_playwright(lambda: "   \n  ", timeout_exc)  # whitespace-only -> strip()<40
    cap = _reload_capture(tmp_path, monkeypatch, fake)

    assert cap.capture_sync("https://example.com", "ep_beef22_s4") is None
    assert state["scrolled"] is False  # near-empty guard bailed before recording
    foot = Path(tmp_path) / "ep_beef22" / "footage"
    assert not list(foot.glob("_rec_*"))
