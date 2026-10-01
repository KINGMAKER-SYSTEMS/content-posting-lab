"""Tests for POST /api/control-plane/v1/media-retention/remedy (the watcher remedy).

The control-plane lab-volume-watch job calls this route to revive a dead
retention/compaction thread and force one bounded floor pass. What matters:

  * bearer-only: without the machine token it refuses (401) and with no token
    configured it fails closed (503);
  * a dead thread is reported revived, and start_compaction_scheduler is called;
  * exactly one forced pass runs with pressure="floor" (never a normal window);
  * the pass runs off the event loop (anyio.to_thread), so the real sweep's disk
    and HTTP work cannot block a Railway request thread.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.control_plane as cp


TOKEN = "test-control-plane-token"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    return TestClient(app)


def _quiet_threads(monkeypatch):
    # The real loops spin forever; replace them so any thread the route starts
    # returns at once. The route reports what it *revived* (pre-call), not a
    # liveness re-read a microsecond later.
    monkeypatch.setattr(cp, "_compaction_loop", lambda: None)
    monkeypatch.setattr(cp, "_media_retention_loop", lambda: None)
    monkeypatch.setattr(cp, "_COMPACTION_THREAD", None)
    monkeypatch.setattr(cp, "_MEDIA_RETENTION_THREAD", None)


def _remedy(monkeypatch):
    calls = []
    monkeypatch.setattr(cp, "run_generated_media_retention_once",
                        lambda *, pressure=None: calls.append(pressure) or {
                            "clip_files": 1, "stray_files": 2, "failed": 0,
                            "bytes_freed": 123, "unconfirmed_bytes_kept": 0})
    return calls


def test_remedy_revives_dead_threads_and_runs_one_floor_pass(client, monkeypatch):
    _quiet_threads(monkeypatch)
    calls = _remedy(monkeypatch)
    response = client.post(
        "/api/control-plane/v1/media-retention/remedy",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-RT-Lane": "content-bucket-control-plane",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "content-lab.media-retention-remedy.v1"
    assert body["revivedThreads"] == ["compaction", "media_retention"]
    assert calls == ["floor"]                      # one forced pass, floor pressure
    assert body["summary"]["bytes_freed"] == 123


def test_remedy_is_idempotent_and_reports_no_revivals_when_threads_alive(client, monkeypatch):
    calls = _remedy(monkeypatch)
    # Simulate a healthy process: both threads already alive.
    class _Alive:
        def is_alive(self):
            return True
    monkeypatch.setattr(cp, "_COMPACTION_THREAD", _Alive())
    monkeypatch.setattr(cp, "_MEDIA_RETENTION_THREAD", _Alive())
    response = client.post(
        "/api/control-plane/v1/media-retention/remedy",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-RT-Lane": "content-bucket-control-plane",
        },
    )
    assert response.status_code == 200
    assert response.json()["revivedThreads"] == []
    assert calls == ["floor"]                      # still runs the forced pass


def test_remedy_refuses_without_the_machine_token(client):
    response = client.post(
        "/api/control-plane/v1/media-retention/remedy",
        headers={"X-RT-Lane": "content-bucket-control-plane"},
    )
    assert response.status_code == 401


def test_remedy_refuses_a_wrong_token(client):
    response = client.post(
        "/api/control-plane/v1/media-retention/remedy",
        headers={
            "Authorization": "Bearer wrong",
            "X-RT-Lane": "content-bucket-control-plane",
        },
    )
    assert response.status_code == 401


def test_remedy_refuses_a_non_control_plane_lane(client):
    response = client.post(
        "/api/control-plane/v1/media-retention/remedy",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-RT-Lane": "some-other-lane",
        },
    )
    assert response.status_code == 400


def test_remedy_fails_closed_when_no_token_is_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("CONTROL_PLANE_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    response = TestClient(app).post(
        "/api/control-plane/v1/media-retention/remedy",
        headers={
            "Authorization": "Bearer anything",
            "X-RT-Lane": "content-bucket-control-plane",
        },
    )
    assert response.status_code == 503
