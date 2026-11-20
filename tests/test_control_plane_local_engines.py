"""Control Plane orders on the studio Mac's free local engines."""

import hashlib
import json
import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from providers.base import API_KEYS
import routers.control_plane as cp
import routers.control_plane_recipes as recipes
from services import control_plane_generation as generation
from services import generation_budget
from services.dossier_ingredients import (
    build_dossier_ingredient_catalog,
    catalog_selection_version,
)
from tests.master_pages_fixtures import bind_current_intent, master_pages

TOKEN = "test-control-plane-token"
PAGE_ID = "tt-tucker-reeves"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "X-RT-Lane": recipes.LANE,
    "X-RT-Page-Id": PAGE_ID,
    "Idempotency-Key": "tt-tucker-reeves:policy:local",
}
TRUCK_CATALOG_HASH = "e7c2a13a818da636bb32ea3027cd3d2be1a88fd8c9c74e87cf67206f3188ceff"
LOCAL_ENGINES = {"local-h3", "local-h3-vertical", "local-wan-i2v"}


@pytest.fixture(autouse=True)
def ready_runtime(monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_GENERATION_MODE", "ready")
    monkeypatch.setitem(API_KEYS, "replicate", "test-key")
    monkeypatch.setitem(API_KEYS, "comfyui", "http://comfy.test:8188")


def _spec(provider_id: str, model_id: str, controls: dict) -> dict:
    intent, revision = master_pages(PAGE_ID, handle="tucker.reeves")
    spec = {
        "schema": "dossier.recipe-spec.v3",
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
    }
    catalog = build_dossier_ingredient_catalog(PAGE_ID, intent, revision)
    production = {
        "catalogVersion": "",
        "providerId": provider_id,
        "modelId": model_id,
        "promptModuleId": "truck",
        "referenceSetId": None,
        "sourceLibraryId": None,
        "variationValues": {},
        "controls": controls,
    }
    production["catalogVersion"] = catalog_selection_version(catalog, "truck-scenic", production)
    assert production["catalogVersion"], "the Dossier must advertise this selection"
    spec["production"] = production
    return spec


def _publication(provider_id="local-h3", model_id="comfyui/minimax-h3-fl2va",
                 controls=None) -> dict:
    spec = json.dumps(
        _spec(provider_id, model_id, controls or {"crop_mode": "both", "duration": 6}),
        sort_keys=True, separators=(",", ":"),
    )
    return {
        "schema": recipes.REQUEST_SCHEMA,
        "pageId": PAGE_ID,
        "lane": recipes.LANE,
        "recipeId": "truck-scenic:master",
        "engine": "ai_video",
        "recipeVersion": "dossier-10ca1e0000000001",
        "dossierRevision": "rev-local",
        "recipeSpecHash": "sha256:" + hashlib.sha256(spec.encode()).hexdigest(),
        "recipeSpecCanonical": spec,
    }


# --- catalog ----------------------------------------------------------------

def test_local_engines_join_the_catalog_without_changing_its_version():
    catalog, version = generation.load_prompt_catalog("truck-scenic")
    assert version == TRUCK_CATALOG_HASH
    assert LOCAL_ENGINES <= set(catalog["providers"])
    assert "hailuo" in catalog["providers"]
    for provider_id in LOCAL_ENGINES:
        assert catalog["providers"][provider_id]["billing"] == "local"
    # The Christmas catalog keeps its single H3 portrait preset for now.
    seasonal, _ = generation.load_prompt_catalog("pov-seasonal")
    assert not LOCAL_ENGINES & set(seasonal["providers"])


@pytest.mark.parametrize("entry", [
    {"hailuo": {"replicate_model": "minimax/hailuo-2.3", "billing": "local"}},
    {"grok": {"replicate_model": "x", "billing": "local", "cost_per_gen_usd": 0}},
    {"local-h3": {"replicate_model": "comfyui/minimax-h3-fl2va"}},
])
def test_local_engine_file_cannot_replace_or_free_a_paid_provider(tmp_path, monkeypatch, entry):
    overlay = tmp_path / "local_engines.v1.json"
    overlay.write_text(json.dumps({"providers": entry}))
    monkeypatch.setattr(generation, "LOCAL_ENGINES_PATH", overlay)
    with pytest.raises(ValueError):
        generation.load_prompt_catalog("truck-scenic")


def test_dossier_offers_local_engines_without_moving_existing_selection_versions(monkeypatch):
    intent, revision = master_pages(PAGE_ID, handle="tucker.reeves")
    hailuo = {
        "catalogVersion": "", "providerId": "hailuo", "modelId": "minimax/hailuo-2.3",
        "promptModuleId": "truck", "referenceSetId": None, "sourceLibraryId": None,
        "variationValues": {}, "controls": {},
    }
    catalog = build_dossier_ingredient_catalog(PAGE_ID, intent, revision)
    truck = next(f for f in catalog["formats"] if f["formatId"] == "truck-scenic")
    model = next(i for i in truck["ingredients"] if i["ingredientId"] == "visual-model")
    offered = {option["providerId"] for option in model["options"]}
    assert {"local-h3", "local-h3-vertical"} <= offered
    # An image-only engine is offered only where the format has reference images.
    assert "local-wan-i2v" not in offered
    with_local = catalog_selection_version(catalog, "truck-scenic", hailuo)

    monkeypatch.setattr(generation, "_merge_local_engines", lambda catalog: None)
    without_local = catalog_selection_version(
        build_dossier_ingredient_catalog(PAGE_ID, intent, revision), "truck-scenic", hailuo,
    )
    assert with_local and with_local == without_local


# --- recipe resolution -------------------------------------------------------

def test_truck_recipe_can_select_local_h3_and_is_free():
    recipe = generation.resolve_generation_recipe(_publication())
    assert recipe is not None
    assert recipe.engine == "local-h3"
    assert recipe.provider_model == "comfyui/minimax-h3-fl2va"
    assert recipe.prompt_catalog_hash == TRUCK_CATALOG_HASH
    assert recipe.clips_per_generation == 5  # the 16:9 master still yields five crops
    assert generation.is_local_engine(recipe.engine)
    assert not generation.is_local_engine("hailuo")


def test_local_recipe_is_unavailable_without_a_configured_server(monkeypatch):
    monkeypatch.delitem(API_KEYS, "comfyui", raising=False)
    assert generation.generation_recipe_unavailable_reason(_publication()) == "provider_runtime"


# --- a full order --------------------------------------------------------------

@pytest.fixture
def lab(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("CONTENT_LAB_RECIPE_ROOT", str(tmp_path / "recipes"))
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    monkeypatch.setattr(cp, "_generation_root", lambda: tmp_path / "generated")
    intent, revision = master_pages(PAGE_ID, handle="tucker.reeves")
    bind_current_intent(monkeypatch, cp, intent, revision)
    started: list = []
    monkeypatch.setattr(cp, "_start_dossier_generation", started.append)
    monkeypatch.setattr(cp, "_start_truck_master_recovery", started.append)
    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    app.include_router(recipes.router, prefix="/api/control-plane")
    client = TestClient(app)
    registration = client.post("/api/control-plane/v1/recipes", json=_publication(), headers=HEADERS)
    assert registration.status_code == 200, registration.text
    return client, started


async def test_local_order_runs_at_zero_budget_without_any_charge(lab, monkeypatch):
    client, started = lab
    monkeypatch.setenv("LAB_GENERATION_DAILY_BUDGET_USD", "0")
    debits: list = []
    monkeypatch.setattr(generation_budget, "debit_generation_spend_at",
                        lambda *args, **kwargs: debits.append(args) or True)
    calls: list = []

    async def fake_generate_one(provider_job_id, index, provider, prompt, aspect_ratio, resolution,
                                duration, image_data_uri, jobs, output_dir, url_prefix, **extra):
        calls.append({"provider": provider, "model_id": extra.get("model_id"),
                      "cost_usd": extra.get("cost_usd"), "crop_mode": extra.get("crop_mode")})
        folder = output_dir / provider / provider_job_id
        folder.mkdir(parents=True, exist_ok=True)
        master = folder / "master.mp4"
        master.write_bytes(f"studio-render:{provider_job_id}".encode())
        crops = []
        for crop_index in range(5):
            crop = folder / f"crop-{crop_index}.mp4"
            crop.write_bytes(master.read_bytes() + f":crop-{crop_index}".encode())
            crops.append({"file": str(crop.relative_to(output_dir)), "cropMode": "both",
                          "cropIndex": crop_index, "cropCount": 5, "width": 432, "height": 768})
        jobs[provider_job_id]["videos"][index].update({
            "status": "done", "file": str(master.relative_to(output_dir)),
            "provider_master_file": str(master.relative_to(output_dir)), "crops": crops,
            "provider_request_id": f"comfy-{provider_job_id}",
        })

    async def fake_color_correct(source, destination, color_correction, scale=None,
                                 playback_speed=1.0, clip_crop=None):
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

    capabilities = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID, "Authorization": f"Bearer {TOKEN}"},
    ).json()["capabilities"]
    assert any(c["recipeVersion"] == "dossier-10ca1e0000000001" and c["maxQuantity"] == 10
               for c in capabilities)

    intent, revision = master_pages(PAGE_ID, handle="tucker.reeves")
    response = client.post("/api/control-plane/v1/jobs", headers=HEADERS, json={
        "pageId": PAGE_ID, "lane": recipes.LANE, "engine": "ai_video",
        "lockedRecipeId": "truck-scenic:master", "recipeVersion": "dossier-10ca1e0000000001",
        "quantity": 5, "constraints": {}, "sourceIsolation": {"partitionKey": f"page:{PAGE_ID}"},
        "policyHash": "sha256:policy", "masterPages": intent, "masterPagesHash": revision,
    })
    assert response.status_code == 200, response.text  # not 429 at a $0 budget
    job_id = response.json()["jobId"]
    assert started == [job_id]

    await cp._run_dossier_generation(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert len(job["clips"]) == 5
    assert calls == [{"provider": "local-h3", "model_id": "comfyui/minimax-h3-fl2va",
                      "cost_usd": 0.0, "crop_mode": "both"}]
    assert debits == []
