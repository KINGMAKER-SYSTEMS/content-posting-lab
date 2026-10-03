import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import post_renders as routes
from services import post_render_jobs as jobs
from tests.test_post_render import NOW
from tests.test_post_render_jobs import submission


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_real_worker_death_is_observed_and_fails_readiness(tmp_path, monkeypatch):
    import app as app_module

    crashed = threading.Event()
    settle = threading.Event()

    def fatal_renderer(*_args, **_kwargs):
        crashed.set()
        # A process-level failure is not an ordinary render error: it escapes the
        # worker's Exception guards and terminates the real worker thread.
        raise SystemExit("simulated fatal worker failure")

    root = tmp_path / "render"
    monkeypatch.setenv("CONTENT_LAB_POST_RENDER_ROOT", str(root))
    monkeypatch.setenv("CONTENT_LAB_POST_RENDER_WORKERS", "1")
    worker = jobs.PostRenderJobs(
        root,
        renderer=fatal_renderer,
        fetcher=lambda _request, path: path.write_bytes(b"source"),
        clock_ms=lambda: NOW,
    )
    monkeypatch.setattr(routes, "_startup_failure", None)
    monkeypatch.setattr(routes, "_started_service", worker)

    probe = FastAPI()
    probe.add_api_route("/api/ready", app_module.ready_check, methods=["GET"])

    worker.start()
    try:
        assert worker.worker_count == 1
        assert worker.workers_alive() == 1
        worker.enqueue(submission(), "fatal-worker-test")
        assert crashed.wait(3)
        for _ in range(300):
            if worker.workers_alive() == 0:
                break
            settle.wait(0.01)
        assert worker.workers_alive() == worker.worker_count - 1 == 0

        with TestClient(probe) as client:
            response = client.get("/api/ready")
        assert response.status_code == 503
        detail = response.json()["post_render"]
        assert detail["state"] == "workers_died"
        assert detail["reason"] == "no_post_render_workers_alive"
        assert detail["workers"] == 0
        assert detail["worker_count"] == 1
        assert detail["queue_age_ms"] >= 0
    finally:
        worker.stop()
