"""Dossier publication must become an executable, page-scoped Lab capability."""

import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.control_plane as cp
import routers.control_plane_recipes as recipes


TOKEN = "test-control-plane-token"
PAGE_ID = "acct:truck-page"


def _payload(**overrides):
    spec = json.dumps(
        {
            "schema": "dossier.recipe-spec.v1",
            "renderTreatment": {
                "stylePreset": "warm-truck",
                "filters": {"brightness": 1.03},
                "captionStyle": {},
            },
            "demand": {"formatMix": {"truck-scenic": 1}},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    body = {
        "schema": recipes.REQUEST_SCHEMA,
        "pageId": PAGE_ID,
        "lane": recipes.LANE,
        "recipeId": "trucks",
        "engine": "content_lab",
        "recipeVersion": "dossier-1234567890abcdef",
        "dossierRevision": "rev-1",
        "recipeSpecHash": "sha256:" + hashlib.sha256(spec.encode()).hexdigest(),
        "recipeSpecCanonical": spec,
    }
    body.update(overrides)
    return body


@pytest.fixture
def lab(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    project = projects / "trucks"
    videos = project / "videos"
    videos.mkdir(parents=True)
    prompts = project / "prompts.json"
    prompts.write_text('[{"prompt":"server-owned and never accepted from ShipStream"}]')
    (videos / "a.mp4").write_bytes(b"clip-a")

    marker_root = tmp_path / "recipes"
    marker_root.mkdir()
    (marker_root / "trucks.json").write_text(json.dumps({
        "registered": True,
        "project": "trucks",
        "maxQuantity": 3,
    }))

    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("CONTENT_LAB_RECIPE_ROOT", str(tmp_path / "dossier-recipes"))
    monkeypatch.setattr(cp, "PROJECTS_DIR", projects)
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")

    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    app.include_router(recipes.router, prefix="/api/control-plane")
    return TestClient(app)


def _publication_headers(**overrides):
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "X-RT-Lane": recipes.LANE,
        "X-RT-Page-Id": PAGE_ID,
        "Idempotency-Key": "dossier:one",
    }
    headers.update(overrides)
    return headers


def test_publication_is_immutable_idempotent_and_requires_dedicated_auth(lab):
    first = lab.post(
        "/api/control-plane/v1/recipes", json=_payload(), headers=_publication_headers(),
    )
    second = lab.post(
        "/api/control-plane/v1/recipes", json=_payload(), headers=_publication_headers(),
    )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()

    missing = _publication_headers()
    missing.pop("Authorization")
    assert lab.post("/api/control-plane/v1/recipes", json=_payload(), headers=missing).status_code == 401

    changed = _payload(dossierRevision="rev-2")
    assert lab.post(
        "/api/control-plane/v1/recipes", json=changed, headers=_publication_headers(),
    ).status_code == 409


def test_registered_dossier_version_is_page_scoped_advertised_and_job_consumed(lab):
    publication = _payload()
    assert lab.post(
        "/api/control-plane/v1/recipes", json=publication, headers=_publication_headers(),
    ).status_code == 200

    capabilities = lab.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID},
    ).json()["capabilities"]
    assert {
        "recipeId": "trucks",
        "engine": "content_lab",
        "recipeVersion": publication["recipeVersion"],
        "maxQuantity": 3,
    } in capabilities
    other = lab.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": "acct:other"},
    ).json()["capabilities"]
    assert not any(item["recipeVersion"] == publication["recipeVersion"] for item in other)

    job = {
        "pageId": PAGE_ID,
        "lane": recipes.LANE,
        "engine": "content_lab",
        "lockedRecipeId": "trucks",
        "recipeVersion": publication["recipeVersion"],
        "quantity": 1,
        "constraints": {},
        "sourceIsolation": {"partitionKey": f"page:{PAGE_ID}"},
        "policyHash": "abc123",
    }
    response = lab.post(
        "/api/control-plane/v1/jobs",
        json=job,
        headers={
            "X-RT-Page-Id": PAGE_ID,
            "X-RT-Lane": recipes.LANE,
            "Idempotency-Key": "acct:truck-page:dossier:source",
        },
    )
    assert response.status_code == 200
    stored = cp._load_jobs()["jobs"][response.json()["jobId"]]
    assert stored["recipeVersion"] == publication["recipeVersion"]
    assert stored["recipeSpecHash"] == publication["recipeSpecHash"]
    assert stored["dossierRevision"] == publication["dossierRevision"]


def test_hash_schema_and_prompt_shaped_fields_fail_closed(lab):
    bad_hash = _payload(recipeSpecHash="sha256:" + "0" * 64)
    assert lab.post(
        "/api/control-plane/v1/recipes", json=bad_hash, headers=_publication_headers(),
    ).status_code == 409

    bad_spec = _payload()
    decoded = json.loads(bad_spec["recipeSpecCanonical"])
    decoded["renderTreatment"]["prompt"] = "caller-controlled instruction"
    canonical = json.dumps(decoded, sort_keys=True, separators=(",", ":"))
    bad_spec["recipeSpecCanonical"] = canonical
    bad_spec["recipeSpecHash"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    assert lab.post(
        "/api/control-plane/v1/recipes", json=bad_spec, headers=_publication_headers(),
    ).status_code == 400
