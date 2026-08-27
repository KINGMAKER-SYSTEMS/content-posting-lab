"""Tests for routers/control_plane.py POST/GET /api/control-plane/v1/jobs.

A replenish job selects never-served clips from a registered recipe's
footage library — no generation, no spend — durably and idempotently.
The properties that matter:

  * the exact recipe version the plane pinned must still be current —
    serving under a moved version fills a bucket with unapproved content;
  * the same Idempotency-Key never selects twice (a retried create cannot
    double-serve or double-spend);
  * a clip once served to a page is never served to it again;
  * free-form prompt fields die here as they do on the plane side;
  * artifact URLs carry a per-job unguessable token, and downloads refuse
    without it;
  * a dry library fails 409 insufficient_inventory rather than generating
    spend nobody armed.

Everything runs against a throwaway tmp project/recipes/jobs store.
"""

import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.control_plane as cp

TOKEN = "test-control-plane-token"


@pytest.fixture
def lab(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    project = projects / "trucks"
    videos = project / "videos"
    videos.mkdir(parents=True)
    prompts = project / "prompts.json"
    prompts.write_text(json.dumps([{"prompt": "locked recipe prompt", "provider": "wan-t2v"}]))
    for name in ["a.mp4", "b.mp4", "c.mp4", "d.mp4"]:
        (videos / name).write_bytes(f"clip-bytes-{name}".encode())
    (videos / "empty.mp4").write_bytes(b"")  # zero-byte: not inventory

    recipes = tmp_path / "recipes"
    recipes.mkdir()
    (recipes / "trucks.json").write_text(json.dumps({
        "registered": True, "project": "trucks", "maxQuantity": 3,
    }))

    monkeypatch.setattr(cp, "PROJECTS_DIR", projects)
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "control_plane_jobs.json")
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)

    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    client = TestClient(app)
    version = "v" + hashlib.sha256(prompts.read_bytes()).hexdigest()[:12]
    return client, version


def _job_body(version, quantity=2, **overrides):
    body = {
        "pageId": "acct:truck-page",
        "lane": "content-bucket-control-plane",
        "engine": "content_lab",
        "lockedRecipeId": "trucks",
        "recipeVersion": version,
        "quantity": quantity,
        "constraints": {},
        "sourceIsolation": {"partitionKey": "page:acct:truck-page"},
        "policyHash": "abc123",
    }
    body.update(overrides)
    return body


HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "X-RT-Page-Id": "acct:truck-page",
    "X-RT-Lane": "content-bucket-control-plane",
    "Idempotency-Key": "acct:truck-page:hash1:source_replenish",
}


def _create(client, body, headers=None):
    return client.post("/api/control-plane/v1/jobs", json=body, headers=headers or HEADERS)


def test_create_selects_never_served_clips_and_completes(lab):
    client, version = lab
    res = _create(client, _job_body(version))
    assert res.status_code == 200
    body = res.json()
    assert body["schema"] == "content-lab.response.v1"
    assert body["status"] == "completed"
    job_id = body["jobId"]

    status = client.get(f"/api/control-plane/v1/jobs/{job_id}", headers=HEADERS).json()
    assert status["status"] == "completed"
    assert status["progress"] == 100

    artifacts = client.get(f"/api/control-plane/v1/jobs/{job_id}/artifacts", headers=HEADERS).json()
    assert len(artifacts["artifacts"]) == 2
    for artifact in artifacts["artifacts"]:
        assert artifact["type"] == "video"
        assert artifact["url"].startswith("http")


def test_idempotent_replay_returns_the_same_job_without_reselecting(lab):
    client, version = lab
    first = _create(client, _job_body(version)).json()
    second = _create(client, _job_body(version)).json()
    assert first["jobId"] == second["jobId"]

    # A different key is a different job — and must get the NEXT clips,
    # because the first two are now served to this page.
    third = _create(
        client,
        _job_body(version),
        headers={**HEADERS, "Idempotency-Key": "acct:truck-page:hash2:source_replenish"},
    ).json()
    assert third["jobId"] != first["jobId"]
    first_urls = client.get(f"/api/control-plane/v1/jobs/{first['jobId']}/artifacts", headers=HEADERS).json()["artifacts"]
    third_urls = client.get(f"/api/control-plane/v1/jobs/{third['jobId']}/artifacts", headers=HEADERS).json()["artifacts"]
    assert {a["url"] for a in first_urls}.isdisjoint({a["url"] for a in third_urls})


def test_served_clips_are_never_served_again_and_dry_library_fails_409(lab):
    client, version = lab
    # Library has 4 real clips (the zero-byte file is not inventory); the
    # ceiling is 3 per job, so two full jobs drain 3+1... second key asks 2.
    _create(client, _job_body(version, quantity=3))
    _create(client, _job_body(version, quantity=2),
            headers={**HEADERS, "Idempotency-Key": "key-two-000000"})
    # 3 + 1 served (partial fill of the second ask), library dry for this page.
    res = _create(client, _job_body(version, quantity=1),
                  headers={**HEADERS, "Idempotency-Key": "key-three-0000"})
    assert res.status_code == 409
    assert "insufficient_inventory" in res.json()["detail"]


def test_stale_recipe_version_is_a_409_never_a_serve(lab):
    client, _ = lab
    res = _create(client, _job_body("v000000000000"))
    assert res.status_code == 409
    assert "recipe_version_mismatch" in res.json()["detail"]


def test_quantity_above_the_recipe_ceiling_is_rejected(lab):
    client, version = lab
    assert _create(client, _job_body(version, quantity=4)).status_code == 400


def test_freeform_prompt_fields_die_here_too(lab):
    client, version = lab
    assert _create(client, _job_body(version, prompt="write anything")).status_code == 400
    nested = _job_body(version, constraints={"negative_prompt": "x"})
    assert _create(client, nested).status_code == 400


def test_unknown_fields_and_missing_headers_are_rejected(lab):
    client, version = lab
    assert _create(client, _job_body(version, nonsense=1)).status_code == 400
    assert client.post("/api/control-plane/v1/jobs", json=_job_body(version)).status_code == 401
    no_key = {k: v for k, v in HEADERS.items() if k != "Idempotency-Key"}
    assert _create(client, _job_body(version), headers=no_key).status_code == 400


def test_download_requires_the_job_token_and_streams_the_clip(lab):
    client, version = lab
    job_id = _create(client, _job_body(version, quantity=1)).json()["jobId"]
    artifacts = client.get(f"/api/control-plane/v1/jobs/{job_id}/artifacts", headers=HEADERS).json()["artifacts"]
    url = artifacts[0]["url"]

    good = client.get(url)
    assert good.status_code == 200
    assert good.content.startswith(b"clip-bytes-")

    bad = client.get(url.split("?token=")[0] + "?token=wrong")
    assert bad.status_code == 403


def test_jobs_answer_only_to_their_own_page(lab):
    client, version = lab
    job_id = _create(client, _job_body(version)).json()["jobId"]
    other = {**HEADERS, "X-RT-Page-Id": "acct:someone-else"}
    assert client.get(f"/api/control-plane/v1/jobs/{job_id}", headers=other).status_code == 404
    assert client.get(f"/api/control-plane/v1/jobs/{job_id}/artifacts", headers=other).status_code == 404
