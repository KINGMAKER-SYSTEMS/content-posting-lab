"""Offline typed-refusal regressions for corrupt retained output authority."""
import json
import statistics
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import post_renders as routes
from services import post_render_jobs as jobs
from tests.test_post_render_jobs import service
from tests.test_post_render_dual_dedup import dual_render, payload
from tests.test_post_render_legacy_upgrade import legacy, offline


@pytest.mark.parametrize("bad", [None, "{", "null", "[]", "0", '"bad"'])
@pytest.mark.parametrize("shared", [False, True])
def test_corrupt_authority_status_and_http_replay_refuse_without_rewrite(tmp_path, monkeypatch, bad, shared):
    worker, jid = legacy(tmp_path)
    worker.fetcher = lambda r, p: p.write_bytes(b"source")
    worker.renderer = dual_render
    first = worker.enqueue(payload("slot:first", "first"), "request:first")
    assert worker.run_one()
    second_body = payload("slot:second", "second")
    second = worker.enqueue(second_body, "request:second")
    assert worker.run_one()
    if shared:
        jid, body, key = second["id"], second_body, "request:second"
        assert worker._row(jid)["final_sha256"] is None
    else:
        body = jobs.RenderJobSubmission.model_validate_json(worker._row(jid)["submission_json"])
        key = "request:legacy"
    with worker._db() as db:
        db.execute("UPDATE jobs SET output_authority_json=? WHERE id=?", (bad, jid))
    original = dict(worker._row(jid))
    with pytest.raises(jobs.RenderJobError, match="output_authority_(missing|invalid)"):
        worker.status(jid)
    with pytest.raises(jobs.RenderJobError, match="output_authority_(missing|invalid)"):
        worker.enqueue(body, key)
    monkeypatch.setattr(routes, "service", lambda: worker)
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "offline-only-token")
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/control-plane")
    headers = {"Authorization": "Bearer offline-only-token", "X-RT-Page-Id": "page-a"}
    with TestClient(app, raise_server_exceptions=False) as client:
        status = client.get(f"/api/control-plane/v1/post-renders/{jid}", headers=headers)
        replay = client.post("/api/control-plane/v1/post-renders", headers={**headers, "Idempotency-Key": key}, json=body.model_dump(by_alias=True))
        for response in (status, replay):
            assert response.status_code == 409
            assert response.json()["detail"] == ("output_authority_missing" if bad is None else "output_authority_invalid")
        healthy = client.get(f"/api/control-plane/v1/post-renders/{first['id']}", headers=headers)
        assert healthy.status_code == 200
    assert dict(worker._row(jid)) == original
    assert worker._output(original).is_dir()


def test_status_cost(tmp_path):
    worker, jid = legacy(tmp_path)
    samples = []
    for _ in range(500):
        start = time.perf_counter_ns()
        worker.status(jid)
        samples.append((time.perf_counter_ns() - start) / 1e6)
    print(f"STATUS_COST n=500 median_ms={statistics.median(samples):.4f} max_ms={max(samples):.4f}")
