"""Generated dossier versions never hydrate legacy library clips."""

import hashlib
import json
import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from providers.base import API_KEYS
import routers.control_plane as cp
import routers.control_plane_recipes as recipes


TOKEN = "test-control-plane-token"
PAGE_ID = "tt-tucker-reeves"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "X-RT-Lane": recipes.LANE,
    "X-RT-Page-Id": PAGE_ID,
    "Idempotency-Key": "tt-tucker-reeves:policy:source",
}


def recipe_publication():
    spec = json.dumps(
        {
            "schema": "dossier.recipe-spec.v1",
            "renderTreatment": {
                "stylePreset": "Dramatic Cool",
                "filters": {"brightness": 0.94, "contrast": 1.08},
                "captionStyle": {},
            },
            "demand": {"formatMix": {"truck-scenic": 1.0}},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "schema": recipes.REQUEST_SCHEMA,
        "pageId": PAGE_ID,
        "lane": recipes.LANE,
        "recipeId": "truck-scenic:master",
        "engine": "hailuo",
        "recipeVersion": "dossier-1234567890abcdef",
        "dossierRevision": "rev-1",
        "recipeSpecHash": "sha256:" + hashlib.sha256(spec.encode()).hexdigest(),
        "recipeSpecCanonical": spec,
    }


def job_body(quantity=2):
    publication = recipe_publication()
    return {
        "pageId": PAGE_ID,
        "lane": recipes.LANE,
        "engine": publication["engine"],
        "lockedRecipeId": publication["recipeId"],
        "recipeVersion": publication["recipeVersion"],
        "quantity": quantity,
        "constraints": {},
        "sourceIsolation": {"partitionKey": f"page:{PAGE_ID}"},
        "policyHash": "sha256:policy",
    }


@pytest.fixture
def lab(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("CONTENT_LAB_GENERATION_MODE", "ready")
    monkeypatch.setenv("CONTENT_LAB_RECIPE_ROOT", str(tmp_path / "recipes"))
    monkeypatch.setitem(API_KEYS, "replicate", "test-key")
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    monkeypatch.setattr(cp, "_generation_root", lambda: tmp_path / "generated")
    started = []
    monkeypatch.setattr(cp, "_start_dossier_generation", started.append)

    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    app.include_router(recipes.router, prefix="/api/control-plane")
    client = TestClient(app)
    registration = client.post(
        "/api/control-plane/v1/recipes",
        json=recipe_publication(),
        headers=HEADERS,
    )
    assert registration.status_code == 200
    return client, tmp_path, started


def test_registered_dossier_is_advertised_and_queues_new_media_only(lab, monkeypatch):
    client, _, started = lab
    capabilities = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID},
    ).json()["capabilities"]
    expected = recipe_publication()
    assert {
        "recipeId": expected["recipeId"],
        "engine": expected["engine"],
        "recipeVersion": expected["recipeVersion"],
        "maxQuantity": 10,
    } in capabilities

    monkeypatch.setattr(
        cp,
        "_scan_library",
        lambda *_: (_ for _ in ()).throw(AssertionError("legacy library was scanned")),
    )
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(), headers=HEADERS,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "queued"
    assert started == [payload["jobId"]]
    stored = cp._load_jobs()["jobs"][payload["jobId"]]
    assert stored["sourceKind"] == "generated"
    assert stored["clips"] == []
    assert stored["promptCatalogHash"] == "259bccb63fa0f03e6f55138236b0f93ecffeab6d924d7d0a32fd8f51f2d5b361"


@pytest.mark.asyncio
async def test_generation_runner_lands_treated_artifacts_under_the_isolated_job_root(lab, monkeypatch):
    client, tmp_path, _ = lab
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(), headers=HEADERS,
    )
    job_id = response.json()["jobId"]
    corrections = []

    async def fake_generate_one(
        provider_job_id, index, provider, prompt, aspect_ratio, resolution,
        duration, image_data_uri, jobs, output_dir, url_prefix, **extra,
    ):
        folder = output_dir / provider / provider_job_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "candidate.mp4"
        path.write_bytes(f"new-media:{provider_job_id}".encode())
        jobs[provider_job_id]["videos"][index].update({
            "status": "done",
            "file": str(path.relative_to(output_dir)),
        })

    async def fake_color_correct(
        source, destination, color_correction, scale=None, playback_speed=1.0,
    ):
        corrections.append((color_correction, playback_speed))
        shutil.copyfile(source, destination)

    monkeypatch.setattr(cp, "generate_one", fake_generate_one)
    monkeypatch.setattr(cp, "run_color_correct", fake_color_correct)
    await cp._run_dossier_generation(job_id)

    stored = cp._load_jobs()["jobs"][job_id]
    assert stored["status"] == "completed"
    assert stored["progress"] == 100
    assert len(stored["clips"]) == 2
    assert len(corrections) == 2
    assert all(speed == pytest.approx(1.0) for _, speed in corrections)
    assert all(clip["clipSpeed"] == pytest.approx(1.0) for clip in stored["clips"])
    root = (tmp_path / "generated").resolve()
    assert all(root in (root / PAGE_ID / stored["recipeVersion"] / job_id / clip["path"]).resolve().parents for clip in stored["clips"])
    assert all(clip["sha256"] for clip in stored["clips"])


def test_generation_jobs_require_the_dedicated_bearer(lab):
    client, _, _ = lab
    headers = {key: value for key, value in HEADERS.items() if key != "Authorization"}
    assert client.post(
        "/api/control-plane/v1/jobs", json=job_body(), headers=headers,
    ).status_code == 401


def test_inflight_generation_from_a_previous_runtime_fails_closed(lab):
    client, _, _ = lab
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(), headers=HEADERS,
    )
    job_id = response.json()["jobId"]

    store = cp._load_jobs()
    store["jobs"][job_id]["runtimeId"] = "previous-process"
    cp.atomic_save(cp._jobs_path(), store)

    status = client.get(
        f"/api/control-plane/v1/jobs/{job_id}", headers=HEADERS,
    ).json()
    assert status["status"] == "failed"
    assert cp._load_jobs()["jobs"][job_id]["error"] == "generation_runtime_restarted"
