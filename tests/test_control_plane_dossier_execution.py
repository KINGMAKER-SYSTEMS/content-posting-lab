"""Generated dossier versions never hydrate legacy library clips."""

import asyncio
import hashlib
import json
from pathlib import Path
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


def job_body(quantity=2, **overrides):
    publication = recipe_publication()
    spec = json.loads(publication["recipeSpecCanonical"])
    body = {
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
    body.update(overrides)
    return body


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


def source_treatment_for(job_id, source_sha256, recipe_spec_hash=None):
    publication = recipe_publication()
    treatment = json.loads(publication["recipeSpecCanonical"])["renderTreatment"]
    return cp.source_treatment_receipt(
        {"jobId": job_id, "recipeSpecHash": recipe_spec_hash or publication["recipeSpecHash"]},
        treatment,
        source_sha256,
        clip_speed=treatment["clipSpeed"],
        clip_crop=treatment["clipCrop"],
    )


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
    assert stored["promptCatalogHash"] == "e7c2a13a818da636bb32ea3027cd3d2be1a88fd8c9c74e87cf67206f3188ceff"
    assert len(stored["promptPlan"]) == 1
    assert set(stored["promptPlan"][0]) == {"combinationId", "promptHash"}


def test_job_idempotency_rejects_a_different_validated_request(lab):
    client, _, _ = lab
    first = client.post("/api/control-plane/v1/jobs", json=job_body(), headers=HEADERS)
    assert first.status_code == 200
    same = client.post("/api/control-plane/v1/jobs", json=job_body(), headers=HEADERS)
    assert same.status_code == 200
    assert same.json()["jobId"] == first.json()["jobId"]

    different_quantity = client.post(
        "/api/control-plane/v1/jobs", json=job_body(quantity=3), headers=HEADERS,
    )
    assert different_quantity.status_code == 409
    assert different_quantity.json()["detail"] == "idempotency_key_reused_with_different_request"

    different_recipe_context = client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(policyHash="sha256:other-policy"),
        headers=HEADERS,
    )
    assert different_recipe_context.status_code == 409
    assert different_recipe_context.json()["detail"] == "idempotency_key_reused_with_different_request"


def test_job_idempotency_replays_a_legacy_row_without_a_request_hash(lab):
    client, _, _ = lab
    first = client.post("/api/control-plane/v1/jobs", json=job_body(), headers=HEADERS)
    assert first.status_code == 200
    store = cp._load_jobs()
    store["jobs"][first.json()["jobId"]].pop("jobRequestHash", None)
    cp.atomic_save(cp._jobs_path(), store)
    replay = client.post("/api/control-plane/v1/jobs", json=job_body(), headers=HEADERS)
    assert replay.status_code == 200
    assert replay.json()["jobId"] == first.json()["jobId"]


    client, _, _ = lab
    first = client.post(
        "/api/control-plane/v1/jobs", json=job_body(), headers=HEADERS,
    )
    assert first.status_code == 200
    first_job = cp._load_jobs()["jobs"][first.json()["jobId"]]

    second_headers = {
        **HEADERS,
        "Idempotency-Key": "tt-tucker-reeves:policy:fresh-second",
    }
    second = client.post(
        "/api/control-plane/v1/jobs", json=job_body(), headers=second_headers,
    )
    assert second.status_code == 200
    second_job = cp._load_jobs()["jobs"][second.json()["jobId"]]
    assert {
        item["promptHash"] for item in first_job["promptPlan"]
    }.isdisjoint({item["promptHash"] for item in second_job["promptPlan"]})


@pytest.mark.parametrize("remaining", [0, 1, 2, 10000])
def test_generated_capability_bounds_planning_without_changing_capacity(lab, monkeypatch, remaining):
    publication = recipes.load_registered_recipe(
        PAGE_ID, "truck-scenic:master", "ai_video", "dossier-1234567890abcdef",
    )
    recipe = cp.resolve_generation_recipe(publication)
    assert recipe is not None
    planner = cp.plan_prompt_combinations
    all_prompts = planner(recipe, "test-capacity", cp.prompt_combination_space(recipe), set())
    available = min(remaining, len(all_prompts))
    blocked = {row["promptHash"] for row in all_prompts[:len(all_prompts) - available]}
    monkeypatch.setattr(cp, "_generated_unavailable_prompts", lambda *args: (blocked, set()))
    requested = []

    def observed_planner(recipe, run_id, count, hashes, slots):
        requested.append(count)
        return planner(recipe, run_id, count, hashes, slots)

    monkeypatch.setattr(cp, "plan_prompt_combinations", observed_planner)
    # Isolate fresh-generation capacity from the separately tested truck recuts.
    quantity = cp._generated_capability_quantity(
        {"jobs": {}}, recipe, PAGE_ID, {"contentNiche": "COFFEE"}, publication["recipeSpecHash"],
    )
    assert quantity == min(cp.MAX_CAPABILITY_QUANTITY, available * recipe.clips_per_generation)
    assert requested == [recipe.planned_provider_calls(cp.MAX_CAPABILITY_QUANTITY)]


@pytest.mark.parametrize("status", ["queued", "running"])
def test_capability_reaches_zero_when_every_active_prompt_is_reserved(lab, status):
    client, _, started = lab
    publication = recipes.load_registered_recipe(
        PAGE_ID,
        "truck-scenic:master",
        "ai_video",
        "dossier-1234567890abcdef",
    )
    recipe = cp.resolve_generation_recipe(publication)
    assert recipe is not None
    every_prompt = cp.plan_prompt_combinations(
        recipe,
        "reserve-the-whole-effective-space",
        cp.prompt_combination_space(recipe),
        set(),
    )
    assert len(every_prompt) == cp.prompt_combination_space(recipe)

    store = cp._load_jobs()
    store["jobs"]["cpl-4444444444444444"] = {
        **current_generation_authority(),
        "jobId": "cpl-4444444444444444",
        "pageId": PAGE_ID,
        "sourceKind": "generated",
        "status": status,
        "runtimeId": cp._GENERATION_RUNTIME_ID,
        "family": recipe.family_name,
        "promptPlan": every_prompt,
        "clips": [],
    }
    cp.atomic_save(cp._jobs_path(), store)

    capabilities = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID},
    ).json()["capabilities"]
    assert capabilities == [{
        "recipeId": "truck-scenic:master",
        "engine": "ai_video",
        "recipeVersion": "dossier-1234567890abcdef",
        "maxQuantity": 0,
    }]

    response = client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(quantity=1),
        headers={**HEADERS, "Idempotency-Key": "prompt-space-exhausted"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "prompt_inventory_exhausted"
    assert started == []



def test_completed_prompt_space_can_refill_with_new_job_without_reusing_media(lab):
    client, _, started = lab
    publication = recipes.load_registered_recipe(
        PAGE_ID, "truck-scenic:master", "ai_video", "dossier-1234567890abcdef",
    )
    recipe = cp.resolve_generation_recipe(publication)
    every_prompt = cp.plan_prompt_combinations(
        recipe, "completed-approved-prompts", cp.prompt_combination_space(recipe), set(),
    )
    old_id = "cpl-4444444444444444"
    old_clip = {"sha256": "a" * 64, "path": "old-delivery.mp4"}
    store = cp._load_jobs()
    store["jobs"][old_id] = {
        **current_generation_authority(),
        "jobId": old_id, "pageId": PAGE_ID, "sourceKind": "generated",
        "status": "completed", "family": recipe.family_name,
        "promptPlan": every_prompt, "clips": [old_clip],
    }
    cp.atomic_save(cp._jobs_path(), store)

    capabilities = client.get(
        "/api/control-plane/v1/capabilities", headers={"X-RT-Page-Id": PAGE_ID},
    ).json()["capabilities"]
    assert capabilities[0]["maxQuantity"] > 0
    headers = {**HEADERS, "Idempotency-Key": "refill-completed-prompt-space"}
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(quantity=1), headers=headers,
    )
    assert response.status_code == 200
    new_id = response.json()["jobId"]
    assert new_id != old_id
    stored = cp._load_jobs()
    assert stored["jobs"][old_id]["clips"] == [old_clip]
    assert stored["jobs"][new_id]["sourceKind"] == "generated"
    assert stored["jobs"][new_id]["clips"] == []
    assert started == [new_id]
    replay = client.post(
        "/api/control-plane/v1/jobs", json=job_body(quantity=1), headers=headers,
    )
    assert replay.status_code == 200
    assert replay.json()["jobId"] == new_id
    assert started == [new_id]


def test_master_pages_strategy_change_withdraws_old_capability_and_job(lab, monkeypatch):
    client, _, started = lab
    prior, _ = master_pages(PAGE_ID, handle="tucker.reeves")
    changed = {
        **prior,
        "contentNiche": "POV — Night Core",
        "contentEngine": "sourced_video",
    }
    bind_current_intent(monkeypatch, cp, changed, recipes.intent_hash(changed))

    capabilities = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID},
    ).json()["capabilities"]
    assert capabilities == [{
        "recipeId": "pov-night-core:master",
        "engine": "sourced_video",
        "recipeVersion": next(
            profile.format_contract_version
            for profile in cp.load_engine_registry()[0].values()
            if profile.format_slug == "pov-night-core"
        ),
        "maxQuantity": 10,
    }]

    response = client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(quantity=1),
        headers={**HEADERS, "Idempotency-Key": "stale-strategy-job"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "job Master Pages intent does not match the current roster"
    )
    assert started == []


@pytest.mark.asyncio
async def test_queued_generation_fails_before_provider_when_page_strategy_changes(
    lab, monkeypatch,
):
    client, _, _ = lab
    queued = client.post(
        "/api/control-plane/v1/jobs", json=job_body(quantity=1), headers=HEADERS,
    )
    assert queued.status_code == 200
    prior, _ = master_pages(PAGE_ID, handle="tucker.reeves")
    changed = {
        **prior,
        "contentNiche": "POV — Night Core",
        "contentEngine": "sourced_video",
    }
    bind_current_intent(monkeypatch, cp, changed, recipes.intent_hash(changed))

    async def fail_generate(*_args, **_kwargs):
        raise AssertionError("stale strategy reached the provider")

    monkeypatch.setattr(cp, "generate_one", fail_generate)
    await cp._run_dossier_generation(queued.json()["jobId"])
    stored = cp._load_jobs()["jobs"][queued.json()["jobId"]]
    assert stored["status"] == "failed"
    assert stored["error"] == "master_pages_strategy_changed"


def test_active_prompt_reservation_survives_later_recipe_authority(lab):
    _, _, _ = lab
    store = cp._load_jobs()
    prompt_hash = "a" * 64
    store["jobs"]["cpl-authority-old000"] = {
        "jobId": "cpl-authority-old000",
        "pageId": PAGE_ID,
        "sourceKind": "generated",
        "status": "running",
        "promptCatalogHash": "b" * 64,
        "family": "retired-family",
        "providerModel": "retired-model",
        "promptPlan": [{"combinationId": 1, "promptHash": prompt_hash}],
        "clips": [],
    }
    cp.atomic_save(cp._jobs_path(), store)
    publication = recipes.load_registered_recipe(
        PAGE_ID,
        "truck-scenic:master",
        "ai_video",
        "dossier-1234567890abcdef",
    )
    recipe = cp.resolve_generation_recipe(publication)
    hashes, slots = cp._generated_unavailable_prompts(store, recipe, PAGE_ID)
    assert prompt_hash in hashes
    assert slots == set()


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


@pytest.mark.parametrize("evidence", [
    "matching", "caption_only", "missing", "invalid_binding", "prior_recipe",
    "grade_changed", "speed_changed", "crop_changed",
])
def test_truck_job_reuses_only_preparable_paid_master_before_new_provider_spend(
    lab, monkeypatch, evidence,
):
    client, tmp_path, started = lab
    old_root = tmp_path / "generated" / PAGE_ID / "legacy" / "cpl-1111111111111111"
    old_root.mkdir(parents=True)
    master = old_root / "renders" / "paid-master.mp4"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"exact-paid-provider-master")
    master_sha = hashlib.sha256(master.read_bytes()).hexdigest()
    intent, revision = master_pages(PAGE_ID, handle="tucker.reeves")
    producer_recipe_hash = (
        "sha256:" + "9" * 64
        if evidence == "prior_recipe"
        else recipe_publication()["recipeSpecHash"]
    )
    actual = source_treatment_for(
        "cpl-1111111111111111", master_sha, producer_recipe_hash,
    )
    if evidence == "caption_only":
        actual["sourceRecipeTreatment"]["captionStyle"] = {"position": "bottom"}
    elif evidence == "missing":
        actual = None
    elif evidence == "invalid_binding":
        actual["sourceSha256"] = "0" * 64
    elif evidence == "grade_changed":
        actual["visualTreatment"]["filters"]["brightness"] = 1
        actual["sourceRecipeTreatment"]["filters"]["brightness"] = 1
    elif evidence == "speed_changed":
        actual["visualTreatment"]["clipSpeed"] = 1
        actual["sourceRecipeTreatment"]["clipSpeed"] = 1
    elif evidence == "crop_changed":
        actual["visualTreatment"]["clipCrop"]["zoom"] = 1
        actual["sourceRecipeTreatment"]["clipCrop"]["zoom"] = 1
    store = cp._load_jobs()
    store["jobs"]["cpl-1111111111111111"] = {
        **current_generation_authority(),
        "recipeSpecHash": producer_recipe_hash,
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
            **({"sourceTreatment": actual} if actual is not None else {}),
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
    if evidence in {"matching", "caption_only"}:
        assert stored["sourceKind"] == "truck_master_recovery"
        assert stored["providerCallsPlanned"] == 0
        assert [entry["sha256"] for entry in stored["recoveryMasters"]] == [master_sha]
        assert started == [f"recovery:{job_id}"]
    else:
        assert stored["sourceKind"] == "generated"
        assert stored["providerCallsPlanned"] == 1
        assert started == [job_id]


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
        "recipeSpecHash": recipe_publication()["recipeSpecHash"],
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
            "sourceTreatment": source_treatment_for(
                "cpl-2222222222222222", master_sha,
            ),
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
    parent_treatment = source_treatment_for(
        "cpl-2222222222222222", master_sha,
    )
    for artifact in artifacts.json()["artifacts"]:
        treatment = artifact["sourceTreatment"]
        assert treatment["sourceSha256"] == artifact["sha256"]
        assert treatment["generationJobId"] == job_id
        assert treatment["visualTreatment"] == parent_treatment["visualTreatment"]
        assert treatment["derivedFrom"] == {
            "sourceSha256": master_sha,
            "generationJobId": "cpl-2222222222222222",
        }

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
@pytest.mark.parametrize("later_provider_failure", [False, True])
async def test_generation_runner_lands_treated_artifacts_under_the_isolated_job_root(lab, monkeypatch, later_provider_failure):
    client, tmp_path, _ = lab
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(quantity=6 if later_provider_failure else 2), headers=HEADERS,
    )
    job_id = response.json()["jobId"]
    corrections = []

    async def fake_generate_one(
        provider_job_id, index, provider, prompt, aspect_ratio, resolution,
        duration, image_data_uri, jobs, output_dir, url_prefix, **extra,
    ):
        if later_provider_failure and provider_job_id.endswith("-g01"):
            jobs[provider_job_id]["videos"][index].update({"status": "error", "error": "provider timed out"})
            return
        folder = output_dir / provider / provider_job_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "candidate.mp4"
        path.write_bytes(f"new-media:{provider_job_id}".encode())
        crops = []
        for crop_index in range(5):
            crop = folder / f"crop-{crop_index}.mp4"
            crop.write_bytes(path.read_bytes() + f":crop-{crop_index}".encode())
            crops.append({"file": str(crop.relative_to(output_dir)), "cropMode": "both",
                          "cropIndex": crop_index, "cropCount": 5, "width": 606, "height": 1080})
        jobs[provider_job_id]["videos"][index].update({
            "status": "done", "file": str(path.relative_to(output_dir)),
            "provider_master_file": str(path.relative_to(output_dir)), "crops": crops,
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
    assert len(stored["clips"]) == 5
    assert stored["providerCallsCompleted"] == 1
    if later_provider_failure:
        assert stored["error"] == "provider_generation_failed"
        assert stored["quantityRequested"] == 6
        assert stored["providerCallsPlanned"] == 2
        replay = client.post("/api/control-plane/v1/jobs", json=job_body(quantity=6), headers=HEADERS)
        assert replay.status_code == 200 and replay.json()["jobId"] == job_id
        assert lab[2] == [job_id], "idempotent replay must not start paid generation again"
    for clip in stored["clips"]:
        assert (Path(stored["artifactRoot"]) / clip["path"]).is_file()
    assert len(corrections) == 5
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
    receipt = stored["clips"][0]["sourceTreatment"]
    assert receipt["sourceSha256"] == stored["clips"][0]["sha256"]
    assert receipt["generationJobId"] == job_id
    assert receipt["visualTreatment"]["clipSpeed"] == pytest.approx(0.75)
    assert receipt["visualTreatment"]["clipCrop"] == {
        "zoom": 1.5, "focusX": 0.2, "focusY": 0.8,
    }

    # Jobs completed while the executor persisted applied speed/crop but the
    # artifact serializer omitted the receipt can be recovered without another
    # provider call. The registered recipe and exact job hash are the authority.
    legacy = cp._load_jobs()
    legacy["jobs"][job_id]["clips"][0].pop("sourceTreatment")
    cp.atomic_save(cp._jobs_path(), legacy)
    artifacts = client.get(
        f"/api/control-plane/v1/jobs/{job_id}/artifacts",
        headers={"Authorization": f"Bearer {TOKEN}", "X-RT-Page-Id": PAGE_ID},
    )
    assert artifacts.status_code == 200
    recovered = artifacts.json()["artifacts"][0]["sourceTreatment"]
    assert recovered["sourceSha256"] == stored["clips"][0]["sha256"]
    assert recovered["generationJobId"] == job_id


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate_output", [False, True])
async def test_generation_runner_refills_completed_prompt_but_rejects_reused_bytes(
    lab, monkeypatch, duplicate_output,
):
    client, _, _ = lab

    async def fake_generate_one(
        provider_job_id, index, provider, prompt, aspect_ratio, resolution,
        duration, image_data_uri, jobs, output_dir, url_prefix, **extra,
    ):
        folder = output_dir / provider / provider_job_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "candidate.mp4"
        path.write_bytes(
            b"provider-returned-the-exact-same-video" if duplicate_output
            else f"fresh-media:{provider_job_id}".encode()
        )
        crops = []
        for crop_index in range(5):
            crop = folder / f"crop-{crop_index}.mp4"
            crop.write_bytes(path.read_bytes() + f":crop-{crop_index}".encode())
            crops.append({"file": str(crop.relative_to(output_dir)), "cropMode": "both",
                          "cropIndex": crop_index, "cropCount": 5, "width": 606, "height": 1080})
        jobs[provider_job_id]["videos"][index].update({
            "status": "done", "file": str(path.relative_to(output_dir)),
            "provider_master_file": str(path.relative_to(output_dir)), "crops": crops,
        })

    async def fake_color_correct(
        source, destination, color_correction, scale=None, playback_speed=1.0,
        clip_crop=None,
    ):
        shutil.copyfile(source, destination)

    async def fake_thumbnail(job_root, video, index):
        target = job_root / "thumbnails" / f"{index:04d}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"jpeg-thumbnail")
        return cp._generated_manifest(job_root, target)

    monkeypatch.setattr(cp, "generate_one", fake_generate_one)
    monkeypatch.setattr(cp, "run_color_correct", fake_color_correct)
    monkeypatch.setattr(cp, "_thumbnail_manifest", fake_thumbnail)
    monkeypatch.setattr(cp, "_is_exact_16x9_video", lambda _: False)

    first = client.post(
        "/api/control-plane/v1/jobs", json=job_body(), headers=HEADERS,
    )
    await cp._run_dossier_generation(first.json()["jobId"])
    assert cp._load_jobs()["jobs"][first.json()["jobId"]]["status"] == "completed"

    second_headers = {
        **HEADERS,
        "Idempotency-Key": "tt-tucker-reeves:policy:duplicate-output",
    }
    second = client.post(
        "/api/control-plane/v1/jobs", json=job_body(), headers=second_headers,
    )
    # Reuse the exact approved prompt, exercising the runner and final claim.
    store = cp._load_jobs()
    original = store["jobs"][first.json()["jobId"]]
    store["jobs"][second.json()["jobId"]]["promptPlan"] = original["promptPlan"]
    cp.atomic_save(cp._jobs_path(), store)
    await cp._run_dossier_generation(second.json()["jobId"])
    result = cp._load_jobs()["jobs"][second.json()["jobId"]]
    if duplicate_output:
        assert result["status"] == "failed"
        assert result["error"] == "duplicate_generated_artifact"
    else:
        assert result["status"] == "completed"
        assert result["clips"][0]["promptHash"] == original["clips"][0]["promptHash"]
        assert result["clips"][0]["sha256"] != original["clips"][0]["sha256"]
        artifacts = client.get(
            f"/api/control-plane/v1/jobs/{result['jobId']}/artifacts",
            headers={"Authorization": f"Bearer {TOKEN}", "X-RT-Page-Id": PAGE_ID},
        )
        assert artifacts.status_code == 200
        assert artifacts.json()["artifacts"][0]["sha256"] == result["clips"][0]["sha256"]



@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", [None, "missing", "short", "duplicate", "wrong_mode", "wrong_count"])
async def test_truck_artifacts_trace_five_vertical_crops_to_one_provider_master(lab, monkeypatch, corruption):
    client, _, _ = lab
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(quantity=2), headers=HEADERS,
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
        crops.reverse()  # Provider order does not define the commissioned index order.
        if corruption == "missing": crops = []
        elif corruption == "short": crops.pop()
        elif corruption == "duplicate": crops[-1]["cropIndex"] = crops[0]["cropIndex"]
        elif corruption == "wrong_mode": crops[0]["cropMode"] = "dual"
        elif corruption == "wrong_count": crops[0]["cropCount"] = 3
        jobs[provider_job_id]["videos"][index].update({
            "status": "done",
            "file": str(master.relative_to(output_dir)),
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
    if corruption:
        assert stored["status"] == "failed"
        assert stored["error"] == "provider_crop_set_invalid"
        assert not stored.get("clips")
        return
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
    store["jobs"][job_id].pop("generationCheckpointVersion", None)  # Legacy, no durable request identity.
    cp.atomic_save(cp._jobs_path(), store)

    status = client.get(
        f"/api/control-plane/v1/jobs/{job_id}", headers=HEADERS,
    ).json()
    assert status["status"] == "failed"
    assert cp._load_jobs()["jobs"][job_id]["error"] == "generation_runtime_restarted"


@pytest.mark.parametrize("other_status,same_job", [
    ("queued", False), ("running", False), ("running", True),
])
def test_generated_claim_preserves_active_and_same_job_prompt_exclusion(
    lab, other_status, same_job,
):
    job_id = "cpl-5555555555555555"
    other_id = job_id if same_job else "cpl-6666666666666666"
    store = cp._load_jobs()
    store["jobs"][job_id] = {
        "jobId": job_id, "pageId": PAGE_ID, "status": "running", "clips": [],
    }
    store["jobs"][other_id] = {
        "jobId": other_id, "pageId": PAGE_ID, "status": other_status,
        "clips": [{"sha256": "a" * 64, "promptHash": "b" * 64, "generationIndex": 0}],
    }
    cp.atomic_save(cp._jobs_path(), store)
    with pytest.raises(RuntimeError, match="duplicate_generated_prompt"):
        cp._claim_unique_generated_clip(job_id, {
            "sha256": "c" * 64, "promptHash": "b" * 64, "generationIndex": 1,
        })
    assert cp._load_jobs() == store


@pytest.mark.asyncio
async def test_truck_recovery_cancel_is_terminal_and_preserves_candidate_root(lab, monkeypatch):
    _client, tmp_path, _ = lab
    job_root = tmp_path / "recovery-job"; job_root.mkdir()
    candidate_root = tmp_path / "candidate"; candidate_root.mkdir()
    master = candidate_root / "masters" / "paid.mp4"; master.parent.mkdir(); master.write_bytes(b"paid-master")
    candidate_sha = hashlib.sha256(master.read_bytes()).hexdigest()
    job_id = "cpl-cancel-recovery"
    sibling_root = tmp_path / "sibling"; sibling_root.mkdir()
    store = cp._load_jobs()
    store["jobs"][job_id] = {"jobId": job_id, "pageId": PAGE_ID, "status": "queued",
        "artifactRoot": str(job_root), "recoveryMasters": [{"artifactRoot": str(candidate_root),
        "path": "masters/paid.mp4", "sha256": candidate_sha, "bytes": master.stat().st_size,
        "source": {"recipeId": "truck-scenic:master"}}]}
    sibling_id = "cpl-sibling"
    store["jobs"][sibling_id] = {"jobId": sibling_id, "pageId": PAGE_ID, "status": "completed",
        "artifactRoot": str(sibling_root), "clips": [{"path": "clip.mp4"}]}
    cp.atomic_save(cp._jobs_path(), store)
    started = asyncio.Event(); hold = asyncio.Event()
    async def stalled_geometry(_path):
        started.set(); await hold.wait(); return (1920, 1080)
    monkeypatch.setattr(cp, "_get_job_or_404", lambda _job_id: cp._load_jobs()["jobs"][_job_id])
    monkeypatch.setattr(cp, "_job_matches_current_master_pages", lambda _job: True)
    monkeypatch.setattr(cp, "_video_geometry", stalled_geometry)
    task = asyncio.create_task(cp._run_truck_master_recovery(job_id))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    saved = cp._load_jobs()["jobs"][job_id]
    assert saved["status"] == "failed" and saved["error"] == "generation_cancelled" and saved["completedAt"]
    assert not job_root.exists()
    assert sibling_root.exists() and saved["artifactRoot"] != str(sibling_root)
    assert master.read_bytes() == b"paid-master"

@pytest.mark.asyncio
async def test_generation_cancellation_is_behavioral_and_releases_prompt_reservation(lab, monkeypatch):
    client, tmp_path, _ = lab
    response = client.post("/api/control-plane/v1/jobs", json=job_body(), headers=HEADERS)
    job_id = response.json()["jobId"]
    started = asyncio.Event(); hold = asyncio.Event()
    async def stalled_generate(*args, **kwargs):
        started.set(); await hold.wait()
    monkeypatch.setattr(cp, "generate_one", stalled_generate)
    task = asyncio.create_task(cp._run_dossier_generation(job_id))
    await asyncio.wait_for(started.wait(), 5)
    saved = cp._load_jobs()["jobs"][job_id]
    root = Path(saved["artifactRoot"])
    assert root.exists()
    recipe = cp.resolve_generation_recipe(recipe_publication())
    prompt_hashes = {item["promptHash"] for item in saved["promptPlan"]}
    assert prompt_hashes & cp._generated_unavailable_prompts(cp._load_jobs(), recipe, PAGE_ID)[0]
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    saved = cp._load_jobs()["jobs"][job_id]
    assert saved["status"] == "failed" and saved["error"] == "generation_cancelled" and saved["completedAt"]
    assert not root.exists()
    assert not (prompt_hashes & cp._generated_unavailable_prompts(cp._load_jobs(), recipe, PAGE_ID)[0])

@pytest.mark.asyncio
async def test_generation_failure_removes_only_failed_root_and_keeps_completed_sibling(lab, monkeypatch):
    client, tmp_path, _ = lab
    failed = client.post("/api/control-plane/v1/jobs", json=job_body(), headers=HEADERS).json()["jobId"]
    sibling_headers = {**HEADERS, "Idempotency-Key": "tt-tucker-reeves:sibling"}
    sibling = client.post("/api/control-plane/v1/jobs", json=job_body(), headers=sibling_headers).json()["jobId"]
    store = cp._load_jobs()
    sibling_root = Path(store["jobs"][sibling]["artifactRoot"]); sibling_root.mkdir(parents=True, exist_ok=True)
    (sibling_root / "clip.mp4").write_bytes(b"completed-sibling")
    store["jobs"][sibling].update({"status": "completed", "clips": [{"path": "clip.mp4"}]})
    cp.atomic_save(cp._jobs_path(), store)
    async def failed_provider(*args, **kwargs):
        raise RuntimeError("provider-boom")
    monkeypatch.setattr(cp, "generate_one", failed_provider)
    failed_root = Path(cp._load_jobs()["jobs"][failed]["artifactRoot"])
    await cp._run_dossier_generation(failed)
    saved = cp._load_jobs()["jobs"][failed]
    assert saved["status"] == "failed" and saved["error"] == "provider-boom"
    assert not failed_root.exists()
    assert sibling_root.exists() and (sibling_root / "clip.mp4").read_bytes() == b"completed-sibling"


@pytest.mark.asyncio
async def test_zero_output_provider_failure_records_its_class_and_returns_it_on_status(lab, monkeypatch):
    # 2026-09-24: 43 of 70 failed AI refills were Replicate 402 "insufficient
    # credit", indistinguishable from transient faults behind one label once
    # the Railway logs rotated. The job store keeps the class, and the status
    # response keeps its error code and adds only the sanitised class/code.
    client, _, _ = lab
    job_id = client.post("/api/control-plane/v1/jobs", json=job_body(quantity=2), headers=HEADERS).json()["jobId"]

    async def refused(provider_job_id, index, provider, prompt, aspect_ratio, resolution,
                      duration, image_data_uri, jobs, output_dir, url_prefix, **extra):
        jobs[provider_job_id]["videos"][index].update({
            "status": "error",
            "provider_request_id": None,
            "error": 'Replicate start failed: {"title":"Insufficient credit","detail":"x","status":402}',
        })

    monkeypatch.setattr(cp, "generate_one", refused)
    await cp._run_dossier_generation(job_id)

    stored = cp._load_jobs()["jobs"][job_id]
    assert stored["status"] == "failed"
    assert stored["error"] == "provider_generation_failed"
    failure = stored["providerFailure"]
    assert failure["class"] == "insufficient_credit"
    recipe = cp.resolve_generation_recipe(recipes.load_registered_recipe(
        PAGE_ID, "truck-scenic:master", "ai_video", "dossier-1234567890abcdef",
    ))
    assert failure["provider"] == recipe.engine
    assert failure["model"] == current_generation_authority()["providerModel"]
    assert failure["generationIndex"] == 0
    assert "Insufficient credit" in failure["detail"]

    status = client.get(
        f"/api/control-plane/v1/jobs/{job_id}",
        headers={"Authorization": f"Bearer {TOKEN}", "X-RT-Page-Id": PAGE_ID},
    ).json()
    assert set(status) == {"schema", "jobId", "status", "progress", "error", "errorClass", "errorDetail"}
    assert status["error"] == "provider_generation_failed"
    assert (status["errorClass"], status["errorDetail"]) == ("insufficient_credit", "HTTP 402")


@pytest.mark.asyncio
async def test_moderation_failure_returns_error_class_and_code_on_job_status(lab, monkeypatch, caplog):
    # 2026-09-25: silhouette FLUX calls failed with Replicate E005 "flagged as
    # sensitive" and the Worker saw only provider_generation_failed. The class
    # and short provider code are kept on the job record, in the Lab log, and
    # on the terminal status response the Worker's tolerant validator accepts.
    client, _, _ = lab
    job_id = client.post("/api/control-plane/v1/jobs", json=job_body(quantity=2), headers=HEADERS).json()["jobId"]

    async def flagged(provider_job_id, index, provider, prompt, aspect_ratio, resolution,
                      duration, image_data_uri, jobs, output_dir, url_prefix, **extra):
        jobs[provider_job_id]["videos"][index].update({
            "status": "error",
            "provider_request_id": "pred-1",
            "error": (
                "Replicate failed: The input or output was flagged as sensitive. "
                "Please try again with different inputs. (E005)"
            ),
        })

    monkeypatch.setattr(cp, "generate_one", flagged)
    with caplog.at_level("WARNING", logger="control_plane"):
        await cp._run_dossier_generation(job_id)

    stored = cp._load_jobs()["jobs"][job_id]
    assert stored["status"] == "failed"
    assert stored["error"] == "provider_generation_failed"
    assert stored["errorClass"] == "moderation"
    assert stored["errorDetail"] == "E005"
    assert stored["providerFailure"]["class"] == "moderation"
    assert "errorClass=moderation errorDetail=E005" in caplog.text

    status = client.get(
        f"/api/control-plane/v1/jobs/{job_id}",
        headers={"Authorization": f"Bearer {TOKEN}", "X-RT-Page-Id": PAGE_ID},
    ).json()
    assert status == {
        "schema": status["schema"],
        "jobId": job_id,
        "status": "failed",
        "progress": status["progress"],
        "error": "provider_generation_failed",
        "errorClass": "moderation",
        "errorDetail": "E005",
    }
    assert "flagged" not in json.dumps(status)


def _status(client, job_id):
    return client.get(
        f"/api/control-plane/v1/jobs/{job_id}",
        headers={"Authorization": f"Bearer {TOKEN}", "X-RT-Page-Id": PAGE_ID},
    ).json()


def test_non_terminal_job_status_omits_the_failure_cause(lab):
    client, _, _ = lab
    job_id = client.post("/api/control-plane/v1/jobs", json=job_body(quantity=2), headers=HEADERS).json()["jobId"]
    for status in ("queued", "running", "completed"):
        cp._update_job(job_id, status=status, error="provider_generation_failed",
                       errorClass="moderation", errorDetail="E005")
        body = _status(client, job_id)
        assert set(body) == {"schema", "jobId", "status", "progress"}, status


def test_failure_cause_is_only_returned_for_a_provider_failure(lab):
    # A class left by an isolated refusal must not be attached to a later,
    # unrelated terminal failure.
    client, _, _ = lab
    job_id = client.post("/api/control-plane/v1/jobs", json=job_body(quantity=2), headers=HEADERS).json()["jobId"]
    cp._update_job(job_id, status="failed", error="generation_cancelled",
                   errorClass="moderation", errorDetail="E005")
    assert set(_status(client, job_id)) == {"schema", "jobId", "status", "progress", "error"}


@pytest.mark.parametrize("field,value", [
    ("errorDetail", 'E"5'),
    ("errorDetail", "E<b>"),
    ("errorDetail", "E&5"),
    ("errorDetail", "E005\n"),
    ("errorDetail", "E005\nx"),
    ("errorDetail", " E005"),
    ("errorDetail", "E005 "),
    ("errorDetail", ""),
    ("errorDetail", "E" * 65),
    ("errorDetail", "https://api.replicate.com/v1/predictions/x"),
    ("errorDetail", 5),
    ("errorClass", 'mod"x'),
    ("errorClass", "-moderation"),
    ("errorClass", "moderation " ),
    ("errorClass", "m" * 33),
    ("errorClass", None),
])
def test_disallowed_failure_cause_is_dropped_not_truncated(lab, field, value):
    client, _, _ = lab
    job_id = client.post("/api/control-plane/v1/jobs", json=job_body(quantity=2), headers=HEADERS).json()["jobId"]
    cause = {"errorClass": "moderation", "errorDetail": "E005", field: value}
    cp._update_job(job_id, status="failed", error="provider_generation_failed", **cause)
    body = _status(client, job_id)
    assert field not in body
    kept = "errorDetail" if field == "errorClass" else "errorClass"
    assert body[kept] == cause[kept]
    assert body["error"] == "provider_generation_failed"


def test_failure_cause_boundaries_match_the_worker_validator():
    assert cp._job_status_failure_cause({
        "status": "error", "error": "provider_generation_failed",
        "errorClass": "a" + "b" * 31, "errorDetail": "HTTP 402 (x) #1=a+b-c/d:e,f;g_h.i",
    }) == {"errorClass": "a" + "b" * 31, "errorDetail": "HTTP 402 (x) #1=a+b-c/d:e,f;g_h.i"}
    assert cp._job_status_failure_cause({
        "status": "cancelled", "error": "provider_generation_failed",
        "errorClass": "rate_limited", "errorDetail": "E" * 64,
    }) == {"errorClass": "rate_limited", "errorDetail": "E" * 64}
    assert cp._job_status_failure_cause({"status": "failed", "error": "provider_generation_failed"}) == {}


def test_provider_error_code_is_short_and_carries_no_message_text():
    from providers.base import provider_error_code

    assert provider_error_code("Replicate failed: flagged as sensitive (E005)") == "E005"
    assert provider_error_code(
        'Replicate start failed: {"title":"Insufficient credit","status":402}'
    ) == "HTTP 402"
    assert provider_error_code("Replicate failed: interrupted (code: PA)") == "PA"
    assert provider_error_code("ReadTimeout('')") is None
