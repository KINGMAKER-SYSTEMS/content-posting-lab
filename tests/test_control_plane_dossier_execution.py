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
from services.content_engine_registry import REGISTRY_PATH
from tests.master_pages_fixtures import bind_current_intent, master_pages


TOKEN = "test-control-plane-token"
PAGE_ID = "tt-tucker-reeves"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "X-RT-Lane": recipes.LANE,
    "X-RT-Page-Id": PAGE_ID,
    "Idempotency-Key": "tt-tucker-reeves:policy:source",
}


def recipe_publication():
    intent, revision = master_pages(PAGE_ID, handle="tucker.reeves")
    spec = json.dumps(
        {
            "schema": "dossier.recipe-spec.v2",
            "masterPages": intent,
            "masterPagesHash": revision,
            "renderTreatment": {
                "stylePreset": "Dramatic Cool",
                "filters": {"brightness": 0.94, "contrast": 1.08},
                "captionStyle": {},
                "clipSpeed": 0.75,
                "clipCrop": {"zoom": 1.5, "focusX": 0.2, "focusY": 0.8},
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
        "engine": "ai_video",
        "recipeVersion": "dossier-1234567890abcdef",
        "dossierRevision": "rev-1",
        "recipeSpecHash": "sha256:" + hashlib.sha256(spec.encode()).hexdigest(),
        "recipeSpecCanonical": spec,
    }


def job_body(quantity=2):
    publication = recipe_publication()
    spec = json.loads(publication["recipeSpecCanonical"])
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
        "masterPages": spec["masterPages"],
        "masterPagesHash": spec["masterPagesHash"],
    }


def current_generation_authority():
    publication = recipes.load_registered_recipe(
        PAGE_ID,
        "truck-scenic:master",
        "ai_video",
        "dossier-1234567890abcdef",
    )
    recipe = cp.resolve_generation_recipe(publication)
    assert recipe is not None
    return {
        "engine": "ai_video",
        "recipeId": recipe.recipe_id,
        "engineRegistryHash": recipe.engine_registry_hash,
        "formatContractVersion": recipe.format_contract_version,
        "executorVersion": recipe.executor_version,
        "promptCatalogHash": recipe.prompt_catalog_hash,
        "providerModel": recipe.provider_model,
    }


@pytest.fixture
def lab(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("CONTENT_LAB_GENERATION_MODE", "ready")
    monkeypatch.setenv("CONTENT_LAB_RECIPE_ROOT", str(tmp_path / "recipes"))
    monkeypatch.setitem(API_KEYS, "replicate", "test-key")
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    monkeypatch.setattr(cp, "_generation_root", lambda: tmp_path / "generated")
    intent, revision = master_pages(PAGE_ID, handle="tucker.reeves")
    bind_current_intent(monkeypatch, cp, intent, revision)
    started = []
    monkeypatch.setattr(cp, "_start_dossier_generation", started.append)
    monkeypatch.setattr(
        cp, "_start_truck_master_recovery",
        lambda job_id: started.append(f"recovery:{job_id}"),
    )

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
    assert stored["engineRegistryHash"] == hashlib.sha256(
        REGISTRY_PATH.read_bytes(),
    ).hexdigest()
    assert stored["materialSource"] == "generated_video"
    assert stored["assetType"] == "video/mp4"
    assert stored["promptCatalogHash"] == "c80cc32e6e762be05e6655190432e945c55afeb196fc488f3b09ad2fad51b9f1"


def test_canonical_page_queues_from_one_exact_notion_bound_operational_publication(
    lab, monkeypatch,
):
    client, _, started = lab
    exact = recipe_publication()
    recipes._record_path(recipes._root(), exact).unlink()

    operational_id = "acct:rail:legacy-tucker"
    publication = {**exact, "pageId": operational_id}
    spec = json.loads(publication["recipeSpecCanonical"])
    spec["masterPages"] = {**spec["masterPages"], "pageId": operational_id}
    spec["masterPagesHash"] = recipes.intent_hash(spec["masterPages"])
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    publication["recipeSpecCanonical"] = canonical
    publication["recipeSpecHash"] = "sha256:" + hashlib.sha256(
        canonical.encode()
    ).hexdigest()
    headers = {
        **HEADERS,
        "X-RT-Page-Id": operational_id,
        "Idempotency-Key": "acct:rail:legacy-tucker:publication",
    }
    assert client.post(
        "/api/control-plane/v1/recipes", json=publication, headers=headers,
    ).status_code == 200

    target_intent, target_hash = master_pages(PAGE_ID, handle="tucker.reeves")

    def resolve_current(page_id, asserted=None):
        if page_id == PAGE_ID:
            return target_intent, target_hash
        if (
            page_id == operational_id
            and isinstance(asserted, dict)
            and asserted.get("notionPageId") == target_intent["notionPageId"]
        ):
            rebound = {**target_intent, "pageId": operational_id}
            return rebound, recipes.intent_hash(rebound)
        return None

    monkeypatch.setattr(cp, "_current_master_pages_intent", resolve_current)

    operational_capabilities = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": operational_id},
    ).json()["capabilities"]
    assert len(operational_capabilities) == 1

    capabilities = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID},
    ).json()["capabilities"]
    assert capabilities == [{
        "recipeId": publication["recipeId"],
        "engine": publication["engine"],
        "recipeVersion": publication["recipeVersion"],
        "maxQuantity": 10,
    }]

    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(), headers=HEADERS,
    )
    assert response.status_code == 200
    job_id = response.json()["jobId"]
    stored = cp._load_jobs()["jobs"][job_id]
    assert stored["pageId"] == PAGE_ID
    assert stored["recipePublicationPageId"] == operational_id
    assert started == [job_id]


def test_truck_job_reuses_preserved_paid_master_before_new_provider_spend(lab, monkeypatch):
    client, tmp_path, started = lab
    old_root = tmp_path / "generated" / PAGE_ID / "legacy" / "cpl-1111111111111111"
    old_root.mkdir(parents=True)
    master = old_root / "renders" / "paid-master.mp4"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"exact-paid-provider-master")
    master_sha = hashlib.sha256(master.read_bytes()).hexdigest()
    intent, revision = master_pages(PAGE_ID, handle="tucker.reeves")
    store = cp._load_jobs()
    store["jobs"]["cpl-1111111111111111"] = {
        **current_generation_authority(),
        "jobId": "cpl-1111111111111111",
        "pageId": PAGE_ID,
        "sourceKind": "generated",
        "status": "completed",
        "artifactRoot": str(old_root),
        "createdAt": "2026-08-30T00:00:00+00:00",
        "clips": [{
            "path": "renders/paid-master.mp4",
            "sha256": master_sha,
            "bytes": master.stat().st_size,
            "source": {
                "recipeId": "truck-scenic:master",
                "recipeVersion": "dossier-legacy0000000",
                "path": "renders/paid-master.mp4",
                "sha256": master_sha,
                "bytes": master.stat().st_size,
                "pageId": PAGE_ID,
                "masterPagesHash": revision,
                "contentNiche": intent["contentNiche"],
                "contentEngine": intent["contentEngine"],
                "vaultUrl": intent["vaultUrl"],
            },
        }],
    }
    cp.atomic_save(cp._jobs_path(), store)
    monkeypatch.setattr(cp, "_is_exact_16x9_video", lambda _: True)

    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(quantity=1), headers=HEADERS,
    )
    assert response.status_code == 200
    job_id = response.json()["jobId"]
    stored = cp._load_jobs()["jobs"][job_id]
    assert stored["sourceKind"] == "truck_master_recovery"
    assert stored["providerCallsPlanned"] == 0
    assert [entry["sha256"] for entry in stored["recoveryMasters"]] == [master_sha]
    assert started == [f"recovery:{job_id}"]


def test_truck_job_never_recrops_a_master_from_stale_creative_authority(lab, monkeypatch):
    client, tmp_path, started = lab
    old_root = tmp_path / "generated" / PAGE_ID / "legacy" / "cpl-3333333333333333"
    old_root.mkdir(parents=True)
    master = old_root / "renders" / "stale-master.mp4"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"stale-prompt-provider-master")
    master_sha = hashlib.sha256(master.read_bytes()).hexdigest()
    intent, revision = master_pages(PAGE_ID, handle="tucker.reeves")
    stale_authority = current_generation_authority()
    stale_authority["promptCatalogHash"] = "0" * 64
    store = cp._load_jobs()
    store["jobs"]["cpl-3333333333333333"] = {
        **stale_authority,
        "jobId": "cpl-3333333333333333",
        "pageId": PAGE_ID,
        "sourceKind": "generated",
        "status": "completed",
        "artifactRoot": str(old_root),
        "createdAt": "2026-08-29T00:00:00+00:00",
        "clips": [{
            "path": "renders/stale-master.mp4",
            "sha256": master_sha,
            "bytes": master.stat().st_size,
            "source": {
                "recipeId": "truck-scenic:master",
                "recipeVersion": "dossier-stale00000000",
                "path": "renders/stale-master.mp4",
                "sha256": master_sha,
                "bytes": master.stat().st_size,
                "pageId": PAGE_ID,
                "masterPagesHash": revision,
                "contentNiche": intent["contentNiche"],
                "contentEngine": intent["contentEngine"],
                "vaultUrl": intent["vaultUrl"],
            },
        }],
    }
    cp.atomic_save(cp._jobs_path(), store)
    monkeypatch.setattr(cp, "_is_exact_16x9_video", lambda _: True)

    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(quantity=1), headers=HEADERS,
    )
    assert response.status_code == 200
    stored = cp._load_jobs()["jobs"][response.json()["jobId"]]
    assert stored["sourceKind"] == "generated"
    assert stored["providerCallsPlanned"] == 1
    assert started == [response.json()["jobId"]]


@pytest.mark.asyncio
async def test_truck_master_recovery_emits_five_crops_without_model_call(lab, monkeypatch):
    client, tmp_path, started = lab
    old_root = tmp_path / "generated" / PAGE_ID / "legacy" / "cpl-2222222222222222"
    old_root.mkdir(parents=True)
    master = old_root / "renders" / "paid-master.mp4"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"another-exact-paid-provider-master")
    master_sha = hashlib.sha256(master.read_bytes()).hexdigest()
    intent, revision = master_pages(PAGE_ID, handle="tucker.reeves")
    store = cp._load_jobs()
    store["jobs"]["cpl-2222222222222222"] = {
        **current_generation_authority(),
        "jobId": "cpl-2222222222222222",
        "pageId": PAGE_ID,
        "sourceKind": "generated",
        "status": "completed",
        "artifactRoot": str(old_root),
        "createdAt": "2026-08-30T00:00:00+00:00",
        "clips": [{
            "path": "renders/paid-master.mp4",
            "sha256": master_sha,
            "bytes": master.stat().st_size,
            "source": {
                "recipeId": "truck-scenic:master",
                "recipeVersion": "dossier-legacy0000000",
                "path": "renders/paid-master.mp4",
                "sha256": master_sha,
                "bytes": master.stat().st_size,
                "pageId": PAGE_ID,
                "masterPagesHash": revision,
                "contentNiche": intent["contentNiche"],
                "contentEngine": intent["contentEngine"],
                "vaultUrl": intent["vaultUrl"],
            },
        }],
    }
    cp.atomic_save(cp._jobs_path(), store)
    monkeypatch.setattr(cp, "_is_exact_16x9_video", lambda _: True)
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(quantity=1), headers=HEADERS,
    )
    job_id = response.json()["jobId"]
    assert started == [f"recovery:{job_id}"]

    async def fake_crops(copied, mode):
        assert mode == "both"
        paths = []
        for index in range(5):
            path = copied.with_stem(f"{copied.stem}_crop{index}")
            path.write_bytes(f"portrait-crop-{index}".encode())
            paths.append(path)
        return paths

    async def fake_thumbnail(job_root, video, index):
        target = job_root / "thumbnails" / f"{index:04d}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"jpeg-{index}".encode())
        return cp._generated_manifest(job_root, target)

    async def fail_generate(*_args, **_kwargs):
        raise AssertionError("master recovery must not call a provider")

    monkeypatch.setattr(cp, "_video_geometry", lambda _: _async_value((1920, 1080)))
    monkeypatch.setattr(cp, "_video_geometry_for_delivery", lambda _: _async_value((606, 1080)))
    monkeypatch.setattr(cp, "multi_crop_vertical", fake_crops)
    monkeypatch.setattr(cp, "_thumbnail_manifest", fake_thumbnail)
    monkeypatch.setattr(cp, "generate_one", fail_generate)
    await cp._run_truck_master_recovery(job_id)

    stored = cp._load_jobs()["jobs"][job_id]
    assert stored["status"] == "completed"
    assert len(stored["clips"]) == 5
    assert [clip["delivery"]["crop"]["index"] for clip in stored["clips"]] == list(range(5))
    assert {clip["delivery"]["crop"]["sourceSha256"] for clip in stored["clips"]} == {master_sha}
    artifacts = client.get(
        f"/api/control-plane/v1/jobs/{job_id}/artifacts",
        headers={"Authorization": f"Bearer {TOKEN}", "X-RT-Page-Id": PAGE_ID},
    )
    assert artifacts.status_code == 200
    assert len(artifacts.json()["artifacts"]) == 5

    # A job persisted by the former keeper-yield planner could contain more
    # completed crop groups than its requested delivery count. The transport
    # contract exposes only one complete five-crop group for quantity=1.
    store = cp._load_jobs()
    store["jobs"][job_id]["clips"] = stored["clips"] * 2
    cp.atomic_save(cp._jobs_path(), store)
    bounded = client.get(
        f"/api/control-plane/v1/jobs/{job_id}/artifacts",
        headers={"Authorization": f"Bearer {TOKEN}", "X-RT-Page-Id": PAGE_ID},
    )
    assert bounded.status_code == 200
    assert len(bounded.json()["artifacts"]) == 5


async def _async_value(value):
    return value


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
        clip_crop=None,
    ):
        corrections.append((color_correction, playback_speed, clip_crop))
        shutil.copyfile(source, destination)

    async def fake_thumbnail(job_root, video, index):
        target = job_root / "thumbnails" / f"{index:04d}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"jpeg-thumbnail")
        return cp._generated_manifest(job_root, target)

    monkeypatch.setattr(cp, "generate_one", fake_generate_one)
    monkeypatch.setattr(cp, "run_color_correct", fake_color_correct)
    monkeypatch.setattr(cp, "_thumbnail_manifest", fake_thumbnail)
    await cp._run_dossier_generation(job_id)

    stored = cp._load_jobs()["jobs"][job_id]
    assert stored["status"] == "completed"
    assert stored["progress"] == 100
    assert len(stored["clips"]) == 1
    assert len(corrections) == 1
    assert all(speed == pytest.approx(0.75) for _, speed, _ in corrections)
    assert all(crop == {"zoom": 1.5, "focusX": 0.2, "focusY": 0.8} for _, _, crop in corrections)
    assert all(clip["clipSpeed"] == pytest.approx(0.75) for clip in stored["clips"])
    assert all(
        clip["clipCrop"] == {"zoom": 1.5, "focusX": 0.2, "focusY": 0.8}
        for clip in stored["clips"]
    )
    root = (tmp_path / "generated").resolve()
    assert all(root in (root / PAGE_ID / stored["recipeVersion"] / job_id / clip["path"]).resolve().parents for clip in stored["clips"])
    assert all(clip["sha256"] for clip in stored["clips"])


@pytest.mark.asyncio
async def test_truck_artifacts_trace_five_vertical_crops_to_one_provider_master(lab, monkeypatch):
    client, _, _ = lab
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(quantity=5), headers=HEADERS,
    )
    job_id = response.json()["jobId"]

    async def fake_generate_one(
        provider_job_id, index, provider, prompt, aspect_ratio, resolution,
        duration, image_data_uri, jobs, output_dir, url_prefix, **extra,
    ):
        folder = output_dir / provider / provider_job_id
        folder.mkdir(parents=True, exist_ok=True)
        master = folder / "provider-master.mp4"
        master.write_bytes(f"exact-16x9-provider-master:{provider_job_id}".encode())
        crops = []
        for crop_index in range(5):
            crop = folder / f"provider-master_crop{crop_index}.mp4"
            crop.write_bytes(f"vertical-crop-{crop_index}".encode())
            crops.append({
                "file": str(crop.relative_to(output_dir)),
                "cropMode": "both", "cropIndex": crop_index, "cropCount": 5,
                "width": 606, "height": 1080,
            })
        jobs[provider_job_id]["videos"][index].update({
            "status": "done",
            "file": crops[0]["file"],
            "provider_master_file": str(master.relative_to(output_dir)),
            "crops": crops,
        })

    async def fake_color_correct(
        source, destination, color_correction, scale=None, playback_speed=1.0,
        clip_crop=None,
    ):
        shutil.copyfile(source, destination)

    async def fake_thumbnail(job_root, video, index):
        target = job_root / "thumbnails" / f"{index:04d}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"jpeg-thumbnail-{index}".encode())
        return cp._generated_manifest(job_root, target)

    monkeypatch.setattr(cp, "generate_one", fake_generate_one)
    monkeypatch.setattr(cp, "run_color_correct", fake_color_correct)
    monkeypatch.setattr(cp, "_thumbnail_manifest", fake_thumbnail)
    await cp._run_dossier_generation(job_id)

    stored = cp._load_jobs()["jobs"][job_id]
    assert stored["status"] == "completed"
    assert len(stored["clips"]) == 5
    group = stored["clips"]
    master_sha = group[0]["source"]["sha256"]
    assert len({clip["source"]["sha256"] for clip in group}) == 1
    assert [clip["delivery"]["crop"]["index"] for clip in group] == list(range(5))
    assert all(clip["delivery"]["crop"]["groupId"] == f"sha256:{master_sha}" for clip in group)
    for crop_index, clip in enumerate(stored["clips"][:5]):
        master_sha = clip["source"]["sha256"]
        assert clip["source"]["path"].endswith("provider-master.mp4")
        assert clip["delivery"] == {
            "aspectRatio": "9:16", "width": 606, "height": 1080,
            "crop": {
                "mode": "both", "index": crop_index, "count": 5,
                "groupId": f"sha256:{master_sha}", "sourceSha256": master_sha,
            },
        }

    artifacts = client.get(
        f"/api/control-plane/v1/jobs/{job_id}/artifacts",
        headers={"Authorization": f"Bearer {TOKEN}", "X-RT-Page-Id": PAGE_ID},
    )
    assert artifacts.status_code == 200
    assert [entry["delivery"]["crop"]["index"] for entry in artifacts.json()["artifacts"][:5]] == list(range(5))


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
