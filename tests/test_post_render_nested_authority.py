"""Offline nested type failures: refuse without rewriting or rerendering."""
import json
from hashlib import sha256

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import post_renders as routes
from services import post_render_jobs as jobs
from tests.test_post_render_jobs import service
from tests.test_post_render_dual_dedup import dual_render, payload
from tests.test_post_render_legacy_upgrade import offline


# Four T154 failures, then the same unsafe operations at their sibling fields.
CASES = [
    ("receipt.completed_at_ms", None, "artifact_binding_mismatch"),
    ("decode.decoded_video_frames", "thirty", "decode_evidence_missing"),
    ("decode.final_probe", None, "decode_evidence_missing"),
    ("burned_fallback.decode", [], "burned_fallback_decode_missing"),
    ("receipt.duration_ms", "long", "decode_evidence_missing"),
    ("burned_fallback.decode.final_probe", None, "burned_fallback_decode_missing"),
    ("burned_fallback.decode.decoded_video_frames", "thirty", "burned_fallback_decode_missing"),
]


def snapshot(worker):
    with worker._db() as db:
        tables = {name: [dict(row) for row in db.execute("SELECT * FROM " + name)]
                  for name in ("jobs", "attempts", "idempotency")}
    files = {str(p.relative_to(worker.root)): sha256(p.read_bytes()).hexdigest()
             for p in (worker.root / "attempts").rglob("*") if p.is_file()}
    return tables, files


def pair(worker, slot):
    body = payload("slot:" + slot, slot)
    key = "request:" + slot
    jid = worker.enqueue(body, key)["id"]
    assert worker.run_one()
    assert worker.status(jid)["state"] == "succeeded"
    return jid, body, key


@pytest.mark.parametrize("path,bad,code", CASES)
@pytest.mark.parametrize("shared", [False, True])
def test_nested_authority_http_refusal_preserves_evidence_and_next_job(tmp_path, monkeypatch, path, bad, code, shared):
    worker = service(tmp_path, renderer=dual_render)
    first = pair(worker, "first")
    second = pair(worker, "second")
    jid, body, key = second if shared else first
    healthy = first if shared else second
    assert (worker._row(jid)["final_sha256"] is None) == shared
    authority = json.loads(worker._row(jid)["output_authority_json"])
    target = authority
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = bad
    # Reach the duration ordering operation after its consistency check.
    if path == "receipt.duration_ms":
        authority["decode"]["final_probe"]["duration_ms"] = bad
    with worker._db() as db:
        db.execute("UPDATE jobs SET output_authority_json=? WHERE id=?", (json.dumps(authority), jid))
    before = snapshot(worker)
    monkeypatch.setattr(routes, "service", lambda: worker)
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "offline-nested")
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/control-plane")
    headers = {"Authorization": "Bearer offline-nested", "X-RT-Page-Id": "page-a"}
    with TestClient(app, raise_server_exceptions=False) as client:
        responses = [
            client.get(f"/api/control-plane/v1/post-renders/{jid}", headers=headers),
            client.post("/api/control-plane/v1/post-renders", headers={**headers, "Idempotency-Key": key},
                        json=body.model_dump(by_alias=True)),
        ]
        assert [r.status_code for r in responses] == [409, 409]
        assert [r.json() for r in responses] == [{"detail": code}] * 2
        assert client.get(f"/api/control-plane/v1/post-renders/{healthy[0]}", headers=headers).status_code == 200
        assert client.post("/api/control-plane/v1/post-renders", headers={**headers, "Idempotency-Key": healthy[2]},
                           json=healthy[1].model_dump(by_alias=True)).status_code == 202
    assert snapshot(worker) == before
    # Refusal itself must never initiate work or delete paid bytes.
    assert not worker.run_one()
    assert snapshot(worker) == before
    # A subsequent independent job still completes with its captioned fallback.
    next_job, _, _ = pair(worker, "next")
    assert worker.artifact(next_job, "burned_final").read_bytes() == b"burned:next"
    assert worker.artifact(healthy[0], "burned_final").is_file()
    assert dict(worker._row(jid)) == next(row for row in before[0]["jobs"] if row["id"] == jid)


@pytest.mark.parametrize("path,bad,code", CASES[:4])
@pytest.mark.parametrize("shared", [False, True])
def test_nested_refusal_through_full_app_auth(tmp_path, monkeypatch, path, bad, code, shared):
    import app as app_module
    worker = service(tmp_path, renderer=dual_render)
    first = pair(worker, "first")
    second = pair(worker, "second")
    jid, body, key = second if shared else first
    authority = json.loads(worker._row(jid)["output_authority_json"])
    target = authority
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = bad
    with worker._db() as db:
        db.execute("UPDATE jobs SET output_authority_json=? WHERE id=?", (json.dumps(authority), jid))
    before = snapshot(worker)
    monkeypatch.setattr(routes, "service", lambda: worker)
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "offline-nested")
    monkeypatch.setattr(app_module, "_APP_API_KEY", "offline-app")
    headers = {"Authorization": "Bearer offline-nested", "X-RT-Page-Id": "page-a", "X-API-Key": "offline-app"}
    # Do not enter lifespan: startup workers/tools are outside this offline test.
    client = TestClient(app_module.app, raise_server_exceptions=False)
    try:
        url = f"/api/control-plane/v1/post-renders/{jid}"
        for response in (
            client.get(url, headers=headers),
            client.post("/api/control-plane/v1/post-renders", headers={**headers, "Idempotency-Key": key},
                        json=body.model_dump(by_alias=True)),
        ):
            assert response.status_code == 409, response.text
            assert response.json() == {"detail": code}
        assert client.get(url, headers={**headers, "X-API-Key": "wrong"}).status_code == 401
        assert client.get(url, headers={**headers, "Authorization": "Bearer wrong"}).status_code == 401
        assert client.get(url, headers={**headers, "X-RT-Page-Id": "foreign"}).status_code == 404
    finally:
        client.close()
    assert snapshot(worker) == before
