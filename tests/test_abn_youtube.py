"""Regression tests for services.abn_youtube + the publish endpoints that import it.

The module was simply MISSING: `import services.abn_youtube` in routers/agenticnews.py
(_publish_blockers, /publish-package, /publish) and services/abn_factory.py (auto-publish
loop) raised ModuleNotFoundError, 500-ing every caller. These pin:

  * the module imports and exposes is_configured() / upload(),
  * is_configured() reflects the three YT_* env vars (and gates everything),
  * upload() refuses cleanly when unconfigured (never raises),
  * the /publish-package and /publish endpoints no longer 500 — they return the
    dormant "not configured" path instead of an import crash.

No network is touched: the only real path tested is the unconfigured / guarded one.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import services.agenticnews as db
import services.abn_assets as abn_assets
import services.abn_youtube as yt
from app import app


_YT_ENV = ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")


@pytest.fixture(autouse=True)
def _no_yt_creds(monkeypatch):
    """Guarantee the module reads as UNconfigured regardless of the real environment,
    so tests never accidentally attempt a live upload."""
    for k in _YT_ENV:
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------- module surface
def test_module_exposes_required_api():
    # the two importers call exactly these two names
    assert callable(yt.is_configured)
    assert callable(yt.upload)


def test_is_configured_false_when_env_missing():
    assert yt.is_configured() is False


def test_is_configured_true_only_when_all_three_present(monkeypatch):
    monkeypatch.setenv("YT_CLIENT_ID", "cid")
    monkeypatch.setenv("YT_CLIENT_SECRET", "secret")
    assert yt.is_configured() is False  # refresh token still missing
    monkeypatch.setenv("YT_REFRESH_TOKEN", "refresh")
    assert yt.is_configured() is True


def test_upload_refuses_cleanly_when_unconfigured():
    # never raises, never touches the network when creds are absent
    res = yt.upload({"video_path": "/nope.mp4", "title": "x"}, "private")
    assert res["ok"] is False
    assert "configured" in res["error"].lower()


def test_upload_reports_missing_video_when_configured(monkeypatch):
    for k in _YT_ENV:
        monkeypatch.setenv(k, "x")
    res = yt.upload({"video_path": "/does/not/exist.mp4", "title": "x"}, "private")
    assert res["ok"] is False
    assert "not found" in res["error"].lower()


# ---------------------------------------------------------------- endpoint guards
@pytest.fixture
def abn_db(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    assets.mkdir()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "abn_test.db")
    monkeypatch.setattr(db, "ASSETS_DIR", assets)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", assets)
    monkeypatch.setattr(db, "_conn", None)
    db._init_sync()
    yield db
    if db._conn is not None:
        db._conn.close()
    monkeypatch.setattr(db, "_conn", None)


@pytest.fixture
def client(abn_db):
    return TestClient(app)


def test_publish_package_endpoint_does_not_500(client):
    """Previously raised ModuleNotFoundError via _publish_blockers(); now returns blockers."""
    v = client.post("/api/agenticnews/videos", json={"title": "Ep"}).json()
    r = client.get(f"/api/agenticnews/episodes/{v['id']}/publish-package")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is False  # no rendered video / package
    assert any("OAuth not configured" in b for b in body["blockers"])


def test_publish_endpoint_blocked_when_unconfigured(client):
    """The publish flip is dormant without creds — must report blocked, not crash."""
    v = client.post("/api/agenticnews/videos", json={"title": "Ep"}).json()
    r = client.post(f"/api/agenticnews/episodes/{v['id']}/publish", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["blocked"] is True
    assert any("OAuth not configured" in b for b in body["blockers"])
