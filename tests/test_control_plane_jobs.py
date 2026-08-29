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


def test_legacy_projects_are_never_advertised_as_page_capabilities(lab):
    client, _, _, _ = lab
    response = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID},
    )
    assert response.status_code == 200
    assert response.json()["capabilities"] == []


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
