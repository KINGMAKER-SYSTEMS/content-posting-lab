"""Closed contracts for version-bound dossier approved-library execution."""

import hashlib
import json
from pathlib import Path
import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.control_plane as cp
import routers.control_plane_recipes as recipes
from services.control_plane_sources import resolve_source_recipe


TOKEN = "test-control-plane-token"
PAGE_ID = "tt-coffee"
BASE_RECIPE = {
    "recipeId": "brewpilled-coffee",
    "engine": "content_lab",
    "recipeVersion": "v62173591b07a",
    "maxQuantity": 20,
}


def publication(recipe_id="coffee-tok:master", format_slug="coffee-tok"):
    canonical = json.dumps({
        "schema": "dossier.recipe-spec.v1",
        "renderTreatment": {
            "stylePreset": "warm-coffee",
            "filters": {"brightness": 1.05, "warmth": 0.1},
            "captionStyle": {},
            "clipSpeed": 1.25,
        },
        "demand": {"formatMix": {format_slug: 1.0}},
    }, sort_keys=True, separators=(",", ":"))
    return {
        "schema": recipes.REQUEST_SCHEMA,
        "pageId": PAGE_ID,
        "lane": recipes.LANE,
        "recipeId": recipe_id,
        "engine": "sourced_video",
        "recipeVersion": "dossier-feedfacefeedface",
        "dossierRevision": "rev-coffee",
        "recipeSpecHash": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
        "recipeSpecCanonical": canonical,
    }


def headers(idempotency="source-job-0001"):
    return {
        "Authorization": f"Bearer {TOKEN}",
        "X-RT-Lane": recipes.LANE,
        "X-RT-Page-Id": PAGE_ID,
        "Idempotency-Key": idempotency,
    }


def job_body(quantity=2):
    payload = publication()
    return {
        "pageId": PAGE_ID,
        "lane": recipes.LANE,
        "engine": payload["engine"],
        "lockedRecipeId": payload["recipeId"],
        "recipeVersion": payload["recipeVersion"],
        "quantity": quantity,
        "constraints": {},
        "sourceIsolation": {"partitionKey": f"page:{PAGE_ID}"},
        "policyHash": "sha256:policy",
    }


@pytest.fixture
def lab(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("CONTENT_LAB_RECIPE_ROOT", str(tmp_path / "recipe-publications"))
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    monkeypatch.setattr(cp, "_generation_root", lambda: tmp_path / "generated")
    projects = tmp_path / "projects"
    videos = projects / BASE_RECIPE["recipeId"] / "videos"
    videos.mkdir(parents=True)
    for index in range(3):
        (videos / f"clip-{index}.mp4").write_bytes(f"approved-source-{index}".encode())
    monkeypatch.setattr(cp, "PROJECTS_DIR", projects)
    base = dict(BASE_RECIPE)
    monkeypatch.setattr(cp, "_registered_recipes", lambda: [dict(base)])
    started = []
    monkeypatch.setattr(cp, "_start_dossier_source", started.append)

    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    app.include_router(recipes.router, prefix="/api/control-plane")
    client = TestClient(app)
    response = client.post(
        "/api/control-plane/v1/recipes",
        json=publication(),
        headers=headers("source-register-0001"),
    )
    assert response.status_code == 200
    return client, tmp_path, base, started


def test_source_recipe_requires_exact_mapping_typed_format_and_live_base_version():
    payload = publication()
    resolved = resolve_source_recipe(
        payload,
        base_recipe_lookup=lambda _: dict(BASE_RECIPE),
    )
    assert resolved is not None
    assert resolved.base_recipe_id == "brewpilled-coffee"
    assert resolved.base_recipe_version == "v62173591b07a"
    assert resolved.served_ledger_key.endswith(":v62173591b07a")

    drifted = {**BASE_RECIPE, "recipeVersion": "v-drifted"}
    assert resolve_source_recipe(payload, base_recipe_lookup=lambda _: drifted) is None
    assert resolve_source_recipe(
        publication("pov-dirt-bike:master", "pov-dirt-bike"),
        base_recipe_lookup=lambda _: dict(BASE_RECIPE),
    ) is None


def test_capability_and_job_bind_exact_source_version_and_select_without_replacement(lab):
    client, _, _, started = lab
    capabilities = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID},
    ).json()["capabilities"]
    expected = publication()
    assert {
        "recipeId": expected["recipeId"],
        "engine": expected["engine"],
        "recipeVersion": expected["recipeVersion"],
        "maxQuantity": 10,
    } in capabilities

    response = client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(2),
        headers=headers("source-job-0001"),
    )
    assert response.status_code == 200
    job_id = response.json()["jobId"]
    assert started == [job_id]
    job = cp._load_jobs()["jobs"][job_id]
    assert job["sourceKind"] == "dossier_approved_library"
    assert job["baseRecipeId"] == BASE_RECIPE["recipeId"]
    assert job["baseRecipeVersion"] == BASE_RECIPE["recipeVersion"]
    assert len(job["sourceClips"]) == 2
    assert all(clip["sha256"] for clip in job["sourceClips"])

    exhausted = client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(2),
        headers=headers("source-job-0002"),
    )
    assert exhausted.status_code == 409
    assert exhausted.json()["detail"] == "insufficient_inventory"


@pytest.mark.asyncio
async def test_source_runner_always_treats_into_isolated_root_with_provenance(lab, monkeypatch):
    client, tmp_path, _, _ = lab
    response = client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(2),
        headers=headers("source-job-0003"),
    )
    job_id = response.json()["jobId"]
    calls = []

    async def fake_color_correct(source, destination, correction, scale=None, playback_speed=1.0):
        calls.append((source, destination, correction, playback_speed))
        shutil.copyfile(source, destination)

    monkeypatch.setattr(cp, "run_color_correct", fake_color_correct)
    await cp._run_dossier_source(job_id)

    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed"
    assert len(calls) == 2
    assert all(call[3] == pytest.approx(1.25) for call in calls)
    assert len(job["clips"]) == 2
    root = (tmp_path / "generated").resolve()
    for clip in job["clips"]:
        output = Path(job["artifactRoot"]) / clip["path"]
        assert root in output.resolve().parents
        assert clip["source"]["recipeId"] == BASE_RECIPE["recipeId"]
        assert clip["source"]["recipeVersion"] == BASE_RECIPE["recipeVersion"]
        assert clip["source"]["sha256"]
        assert clip["sha256"]

    response = client.get(
        f"/api/control-plane/v1/jobs/{job_id}/artifacts",
        headers={"Authorization": f"Bearer {TOKEN}", "X-RT-Page-Id": PAGE_ID},
    )
    assert response.status_code == 200
    artifacts = response.json()["artifacts"]
    assert len(artifacts) == 2
    for artifact, clip in zip(artifacts, job["clips"], strict=True):
        assert artifact["sha256"] == clip["sha256"]
        assert artifact["bytes"] == clip["bytes"]
        assert artifact["source"] == clip["source"]


@pytest.mark.asyncio
async def test_source_runner_fails_closed_when_selected_library_bytes_change(lab, monkeypatch):
    client, tmp_path, _, _ = lab
    response = client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(1),
        headers=headers("source-job-mutated"),
    )
    job_id = response.json()["jobId"]
    selected = cp._load_jobs()["jobs"][job_id]["sourceClips"][0]
    source = (
        tmp_path / "projects" / BASE_RECIPE["recipeId"] / "videos" / selected["path"]
    )
    source.write_bytes(b"changed-after-hash-pinned-selection")

    calls = []

    async def fake_color_correct(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(cp, "run_color_correct", fake_color_correct)
    await cp._run_dossier_source(job_id)

    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "failed"
    assert job["error"] == "source_recipe_artifact_changed"
    assert job["clips"] == []
    assert calls == []


def test_base_version_drift_withdraws_capability_and_refuses_job(lab):
    client, _, base, _ = lab
    base["recipeVersion"] = "v-drifted"
    capabilities = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID},
    ).json()["capabilities"]
    assert all(entry["recipeId"] != "coffee-tok:master" for entry in capabilities)
    response = client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(1),
        headers=headers("source-job-drift"),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "recipe_executor_unavailable"


def test_source_manifest_refuses_a_symlink_that_escapes_the_approved_library(lab):
    _, tmp_path, _, _ = lab
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"not-approved-library-bytes")
    videos = tmp_path / "projects" / BASE_RECIPE["recipeId"] / "videos"
    (videos / "escape.mp4").symlink_to(outside)
    assert cp._clip_manifest(BASE_RECIPE["recipeId"], "escape.mp4") is None
