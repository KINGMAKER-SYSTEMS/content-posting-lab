"""Legacy project-library jobs cannot bypass the hash-bound dossier contract."""

import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.control_plane as cp
from tests.master_pages_fixtures import bind_current_intent, master_pages


TOKEN = "test-control-plane-token"
PAGE_ID = "acct:truck-page"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "X-RT-Page-Id": PAGE_ID,
    "X-RT-Lane": "content-bucket-control-plane",
    "Idempotency-Key": "acct:truck-page:hash1:source_replenish",
}


@pytest.fixture
def lab(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    project = projects / "trucks"
    videos = project / "videos"
    videos.mkdir(parents=True)
    prompts = project / "prompts.json"
    prompts.write_text(json.dumps([{"prompt": "server-owned", "provider": "wan-t2v"}]))
    (videos / "a.mp4").write_bytes(b"clip-bytes")
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir()
    (recipes_dir / "trucks.json").write_text(json.dumps({
        "registered": True, "project": "trucks", "maxQuantity": 3,
    }))
    monkeypatch.setattr(cp, "PROJECTS_DIR", projects)
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    intent, revision = master_pages(PAGE_ID, handle="truck.page")
    bind_current_intent(monkeypatch, cp, intent, revision)
    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    version = "v" + hashlib.sha256(prompts.read_bytes()).hexdigest()[:12]
    return TestClient(app), version, intent, revision


def job_body(version, intent, revision, **overrides):
    body = {
        "pageId": PAGE_ID,
        "lane": "content-bucket-control-plane",
        "engine": "content_lab",
        "lockedRecipeId": "trucks",
        "recipeVersion": version,
        "quantity": 1,
        "constraints": {},
        "sourceIsolation": {"partitionKey": f"page:{PAGE_ID}"},
        "policyHash": "sha256:policy",
        "masterPages": intent,
        "masterPagesHash": revision,
    }
    body.update(overrides)
    return body


def test_legacy_project_recipe_cannot_execute_without_master_pages_engine_alignment(lab):
    client, version, intent, revision = lab
    response = client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(version, intent, revision),
        headers=HEADERS,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "job Master Pages intent is missing, stale, or engine-mismatched"
    assert not cp._jobs_path().exists()


def test_intent_format_is_advertised_before_recipe_publication(lab):
    client, _, _, _ = lab
    response = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID, "Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    capabilities = response.json()["capabilities"]
    assert any(entry["recipeId"] == "truck-scenic:master"
               and entry["engine"] == "ai_video"
               and entry["maxQuantity"] > 0 for entry in capabilities)
    assert all(entry["recipeId"] != "trucks" for entry in capabilities)


def test_job_contract_rejects_prompt_fields_unknown_fields_and_missing_auth(lab):
    client, version, intent, revision = lab
    assert client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(version, intent, revision, prompt="caller text"),
        headers=HEADERS,
    ).status_code == 400
    assert client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(version, intent, revision, nonsense=1),
        headers=HEADERS,
    ).status_code == 400
    assert client.post(
        "/api/control-plane/v1/jobs", json=job_body(version, intent, revision),
    ).status_code == 401


def test_job_master_pages_hash_and_current_roster_are_exact(lab, monkeypatch):
    client, version, intent, revision = lab
    mismatched = job_body(version, intent, "sha256:" + "0" * 64)
    assert client.post("/api/control-plane/v1/jobs", json=mismatched, headers=HEADERS).status_code == 409
    monkeypatch.setattr(cp, "_current_master_pages_intent", lambda *_: None)
    assert client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(version, intent, revision),
        headers=HEADERS,
    ).status_code == 409


def _store_status_job(status, error=None):
    job_id = "cpl-0123456789abcdef"
    job = {
        "jobId": job_id,
        "pageId": PAGE_ID,
        "status": status,
        "progress": 0,
        "clips": [],
    }
    if error is not None:
        job["error"] = error
    store = cp._empty_jobs()
    store["jobs"][job_id] = job
    cp._save_jobs(store)
    return job_id


def test_status_serves_named_reason_for_legacy_blank_terminal_error(lab):
    client, _, _, _ = lab
    job_id = _store_status_job("failed", " \t ")

    response = client.get(f"/api/control-plane/v1/jobs/{job_id}", headers=HEADERS)

    assert response.status_code == 200
    error = response.json()["error"]
    assert error == "lab_failure_reason_unrecorded"
    assert error == error.strip()
    assert 0 < len(error) <= 300


@pytest.mark.parametrize("status", ["queued", "running"])
def test_status_omits_error_for_nonterminal_jobs(lab, status):
    client, _, _, _ = lab
    job_id = _store_status_job(status, "stale failure should not leak")

    response = client.get(f"/api/control-plane/v1/jobs/{job_id}", headers=HEADERS)

    assert response.status_code == 200
    assert "error" not in response.json()


def test_message_less_generation_failure_writer_records_named_reason(lab):
    _client, _, _, _ = lab
    job_id = _store_status_job("running")

    # This is the generic generation-runner seam: its old `str(error)[:300]`
    # was empty for RuntimeError(), leaving the Worker unable to validate polls.
    cp._update_job(
        job_id,
        status="failed",
        error=cp._exception_failure_reason(RuntimeError()),
    )

    stored = cp._load_jobs()["jobs"][job_id]
    assert stored["error"] == "generation_failed_without_message:RuntimeError"
    assert 0 < len(stored["error"]) <= 300
