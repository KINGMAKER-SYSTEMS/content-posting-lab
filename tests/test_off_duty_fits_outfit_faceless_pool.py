"""Off Duty Fits posts finished faceless outfit clips from the Worker's vault pool.

Good Boy Records' Off Duty Fits (``acct:user8696130597870``) had no Lab format
that described its clips, so its Master Pages niche could not be set and it sat
at needs_policy with zero policies. ``outfit-faceless`` is that format. The Lab
makes zero videos for it: the page is pool-supplied, so its Dossier saves with
no Lab library and its capability advertises zero.

These tests go through the same entry points as the Drop Line tests: the
Dossier catalog, the recipe registration route and the capabilities function.

- listed page + Outfit - Faceless -> Dossier resolves, master is pool_supplied,
  one no-library selection, registration accepts it, capability is zero;
- unlisted page with the same niche -> no pool-supplied exemption (blocked);
- a niche the Lab does not know -> still refused, exactly as before.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.control_plane_recipes as recipes
import services.dossier_ingredients as di
from services import control_plane_plan_reasons as reasons
from services.clip_transforms import SOURCE_CUT_FORMATS
from services.dossier_ingredients import (
    build_dossier_ingredient_catalog,
    catalog_selection_version,
)
from tests.master_pages_fixtures import master_pages
from tests.source_dna_fixture import isolate_source_dna_registry

TOKEN = "test-control-plane-token"
OFF_DUTY = "acct:user8696130597870"
OFF_DUTY_HANDLE = "user8696130597870"
UNLISTED = "acct:outfit-unlisted"
FORMAT = "outfit-faceless"
NICHE = "Outfit - Faceless"


@pytest.fixture(autouse=True)
def _isolated_source_dna(monkeypatch, tmp_path):
    isolate_source_dna_registry(monkeypatch, tmp_path)


@pytest.fixture(autouse=True)
def _no_vault_manifest(monkeypatch):
    # Off Duty Fits has no ShipStream source manifest and no Lab master.
    from services.shipstream_source_manifest import ShipStreamSourceMissing

    def missing(*args, **kwargs):
        raise ShipStreamSourceMissing("no vault manifest for this page")

    monkeypatch.setattr(di, "load_shipstream_source_projection", missing)


def _intent(page_id, handle, niche=NICHE):
    return master_pages(
        page_id, handle=handle, content_niche=niche, content_engine="sourced_video",
    )


def _format(catalog, format_id):
    return next(entry for entry in catalog["formats"] if entry["formatId"] == format_id)


def _ingredient(entry, ingredient_id):
    return next(item for item in entry["ingredients"] if item["ingredientId"] == ingredient_id)


def _spec_and_body(page_id, intent, revision, production):
    spec = {
        "schema": "dossier.recipe-spec.v3",
        "masterPages": intent, "masterPagesHash": revision,
        "renderTreatment": {"stylePreset": None, "filters": {}, "captionStyle": {}},
        "demand": {"formatMix": {FORMAT: 1}},
        "production": production,
    }
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return {
        "schema": recipes.REQUEST_SCHEMA, "pageId": page_id, "lane": recipes.LANE,
        "recipeId": f"{FORMAT}:master", "engine": "sourced_video",
        "recipeVersion": "dossier-1234567890abcdef", "dossierRevision": "rev-1",
        "recipeSpecHash": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
        "recipeSpecCanonical": canonical,
    }, spec


def _no_library_production(catalog):
    production = {
        "catalogVersion": "", "providerId": None, "modelId": None,
        "promptModuleId": None, "referenceSetId": None, "sourceLibraryId": None,
        "variationValues": {}, "controls": {},
    }
    production["catalogVersion"] = catalog_selection_version(catalog, FORMAT, production) or ""
    return production


@pytest.fixture
def lab(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("CONTENT_LAB_RECIPE_ROOT", str(tmp_path / "dossier-recipes"))
    app = FastAPI()
    app.include_router(recipes.router, prefix="/api/control-plane")
    return TestClient(app)


def _post(lab, page_id, body):
    return lab.post("/api/control-plane/v1/recipes", json=body, headers={
        "Authorization": f"Bearer {TOKEN}", "X-RT-Lane": recipes.LANE,
        "X-RT-Page-Id": page_id, "Idempotency-Key": f"dossier:{page_id}",
    })


def test_off_duty_fits_resolves_a_pool_supplied_dossier_and_registers(lab):
    intent, revision = _intent(OFF_DUTY, OFF_DUTY_HANDLE)
    catalog = build_dossier_ingredient_catalog(OFF_DUTY, intent, revision)
    assert catalog["currentFormatCandidates"] == [FORMAT]
    entry = _format(catalog, FORMAT)
    assert entry["contentNiche"] == NICHE and entry["contentEngine"] == "sourced_video"
    assert entry["executionStatus"] == "commissioned" and entry["definitionStatus"] == "complete"
    master = _ingredient(entry, "master-source-video")
    assert master["status"] == "pool_supplied", master
    assert master["required"] is False and master["binding"] is None and master["options"] == []
    assert entry["blockers"] == [], entry["blockers"]
    # Exactly one route, and it carries no Lab library, model or provider.
    assert [
        (s["sourceLibraryId"], s["providerId"], s["modelId"])
        for s in entry["productionSelections"]
    ] == [(None, None, None)]

    production = _no_library_production(catalog)
    assert production["catalogVersion"] == entry["productionSelections"][0]["catalogVersion"]
    body, _ = _spec_and_body(OFF_DUTY, intent, revision, production)
    response = _post(lab, OFF_DUTY, body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "registered"


def test_outfit_clips_stay_exactly_as_delivered():
    # No mirror / motion / reverse transform may ever touch these clips.
    assert FORMAT not in SOURCE_CUT_FORMATS


def test_off_duty_fits_capability_is_zero_so_the_lab_never_renders(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTENT_LAB_ADVERTISE_ZERO_REASONS", "1")
    from routers import control_plane as cp
    from services.json_store import atomic_save

    cp._jobs_snapshot = None
    cp._jobs_decode_pending.clear()
    cp._capabilities_cache.clear()
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    atomic_save(tmp_path / "jobs.json", {"jobs": {}, "version": 1, "byIdempotency": {}, "served": {}})
    monkeypatch.setattr(
        cp, "_current_intent_for_capabilities",
        lambda _: ({"contentNiche": NICHE, "contentEngine": "sourced_video"}, "h"),
    )
    intent, revision = _intent(OFF_DUTY, OFF_DUTY_HANDLE)
    catalog = build_dossier_ingredient_catalog(OFF_DUTY, intent, revision)
    _, spec = _spec_and_body(OFF_DUTY, intent, revision, _no_library_production(catalog))
    publication = {
        "recipeId": f"{FORMAT}:master", "engine": "sourced_video", "recipeVersion": "v1",
        "recipeSpecHash": "sha256:" + "ab" * 32,
        "recipeSpecCanonical": json.dumps(spec, sort_keys=True, separators=(",", ":")),
    }
    monkeypatch.setattr(cp, "list_registered_recipe_bindings", lambda *a: [("k", publication)])
    monkeypatch.setattr(cp, "syzygy_runtime_available", lambda: True)
    calls = []
    monkeypatch.setattr(
        cp, "_dossier_source_recipe",
        lambda publication, refusal_reasons=None: calls.append(publication) or None,
    )
    # The real committed engine registry is used here (not stubbed).
    response = cp.capabilities(x_rt_page_id=OFF_DUTY, x_page_id=None)
    assert response["capabilities"] == [{
        "recipeId": f"{FORMAT}:master", "engine": "sourced_video", "recipeVersion": "v1",
        "maxQuantity": 0, "zeroReason": reasons.POOL_SUPPLIED,
    }]
    assert calls == [], "a no-library pool policy must never reach the source planner"


def test_unlisted_page_with_the_same_niche_gets_no_pool_exemption(lab):
    intent, revision = _intent(UNLISTED, "outfit.unlisted")
    catalog = build_dossier_ingredient_catalog(UNLISTED, intent, revision)
    entry = _format(catalog, FORMAT)
    assert _ingredient(entry, "master-source-video")["status"] == "missing"
    assert _ingredient(entry, "master-source-video")["required"] is True
    assert "master-source-video:missing" in entry["blockers"]
    assert entry["productionSelections"] == []
    # The no-library registration the listed page uses is refused here.
    body, _ = _spec_and_body(UNLISTED, intent, revision, _no_library_production(catalog))
    response = _post(lab, UNLISTED, body)
    assert response.status_code == 409, response.text


def test_unknown_niche_still_refuses(lab):
    intent, revision = _intent(OFF_DUTY, OFF_DUTY_HANDLE, niche="Outfit")
    catalog = build_dossier_ingredient_catalog(OFF_DUTY, intent, revision)
    assert catalog["currentFormatCandidates"] == []
    body, _ = _spec_and_body(OFF_DUTY, intent, revision, _no_library_production(catalog))
    response = _post(lab, OFF_DUTY, body)
    assert response.status_code == 409, response.text
    assert "does not match Master Pages intent" in response.json()["detail"]
