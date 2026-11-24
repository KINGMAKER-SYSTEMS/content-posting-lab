"""A listed pool page whose clips are finished OUTSIDE the Lab has no master.

Good Boy Records' Drop Line POV (``acct:user6390595910283``) posts finished,
reviewed dirt-bike cuts rendered on the Mac Studio. The Lab has no master
video for it, so before this change ``pov-dirt-bike`` reported
``master-source-video:missing``, offered zero production selections, and the
Worker could never save a policy. These tests pin the narrow rule:

- listed page + master missing (no binding, no option) -> pool_supplied,
  not required, one no-library production selection, registration accepts a
  null library, capabilities advertise zero (no paid Lab render);
- unlisted page with the same intent -> unchanged (still missing, blocked);
- listed page that DOES have master options -> unchanged (the rule never
  hides a real master library).
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
from services.dossier_ingredients import (
    build_dossier_ingredient_catalog,
    catalog_selection_version,
)
from tests.master_pages_fixtures import master_pages

TOKEN = "test-control-plane-token"
DROP_LINE = "acct:user6390595910283"
UNLISTED = "acct:pov-unlisted"
FORMAT = "pov-dirt-bike"


@pytest.fixture(autouse=True)
def _no_live_syzygy(monkeypatch):
    def unavailable(profile, master_pages, *, timeout):
        raise di.SyzygyError("test: no live library")
    monkeypatch.setattr(di, "load_syzygy_library", unavailable)


def _intent(page_id, handle):
    return master_pages(
        page_id, handle=handle,
        content_niche="POV - Dirtbike", content_engine="sourced_video",
    )


def _format(catalog, format_id):
    return next(entry for entry in catalog["formats"] if entry["formatId"] == format_id)


def _ingredient(entry, ingredient_id):
    return next(item for item in entry["ingredients"] if item["ingredientId"] == ingredient_id)


def test_drop_line_missing_master_is_pool_supplied_with_one_no_library_route():
    intent, revision = _intent(DROP_LINE, "user6390595910283")
    entry = _format(build_dossier_ingredient_catalog(DROP_LINE, intent, revision), FORMAT)
    master = _ingredient(entry, "master-source-video")
    assert master["status"] == "pool_supplied", master
    assert master["required"] is False
    assert master["binding"] is None and master["options"] == []
    assert not any(b.startswith("master-source-video:") for b in entry["blockers"]), entry["blockers"]
    selections = entry["productionSelections"]
    assert len(selections) == 1, selections
    assert selections[0]["sourceLibraryId"] is None
    assert selections[0]["providerId"] is None and selections[0]["modelId"] is None
    # The clip treatment stays a real, required ingredient (Look dials).
    assert _ingredient(entry, "clip-treatment")["required"] is True


def test_unlisted_page_with_the_same_intent_is_still_blocked():
    intent, revision = _intent(UNLISTED, "pov.unlisted")
    entry = _format(build_dossier_ingredient_catalog(UNLISTED, intent, revision), FORMAT)
    assert _ingredient(entry, "master-source-video")["status"] == "missing"
    assert "master-source-video:missing" in entry["blockers"]
    assert entry["productionSelections"] == []


def test_listed_page_with_master_options_keeps_its_master(monkeypatch):
    option = {"libraryId": "masters-x", "role": "master_video_library"}
    real = di._ingredient

    def with_option(ingredient_id, kind, **kwargs):
        if ingredient_id == "master-source-video":
            kwargs = {**kwargs, "status": "selection_required", "options": [option]}
        return real(ingredient_id, kind, **kwargs)

    monkeypatch.setattr(di, "_ingredient", with_option)
    intent, revision = _intent(DROP_LINE, "user6390595910283")
    entry = _format(build_dossier_ingredient_catalog(DROP_LINE, intent, revision), FORMAT)
    master = _ingredient(entry, "master-source-video")
    assert master["status"] == "selection_required" and master["required"] is True
    assert "master-source-video:selection_required" in entry["blockers"]


def _registration_body(page_id, intent, revision):
    catalog = build_dossier_ingredient_catalog(page_id, intent, revision)
    production = {
        "catalogVersion": "", "providerId": None, "modelId": None,
        "promptModuleId": None, "referenceSetId": None, "sourceLibraryId": None,
        "variationValues": {}, "controls": {},
    }
    production["catalogVersion"] = catalog_selection_version(catalog, FORMAT, production)
    assert production["catalogVersion"], "the no-library route must have a selection version"
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


@pytest.fixture
def lab(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("CONTENT_LAB_RECIPE_ROOT", str(tmp_path / "dossier-recipes"))
    app = FastAPI()
    app.include_router(recipes.router, prefix="/api/control-plane")
    return TestClient(app)


def _headers(page_id):
    return {
        "Authorization": f"Bearer {TOKEN}", "X-RT-Lane": recipes.LANE,
        "X-RT-Page-Id": page_id, "Idempotency-Key": "dossier:drop-line",
    }


def test_drop_line_registration_accepts_null_library(lab):
    intent, revision = _intent(DROP_LINE, "user6390595910283")
    body, _ = _registration_body(DROP_LINE, intent, revision)
    response = lab.post("/api/control-plane/v1/recipes", json=body, headers=_headers(DROP_LINE))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "registered"


def test_drop_line_capability_is_zero_so_the_lab_never_renders(monkeypatch, tmp_path):
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
        lambda _: ({"contentNiche": "POV - Dirtbike", "contentEngine": "sourced_video"}, "h"),
    )
    intent, revision = _intent(DROP_LINE, "user6390595910283")
    _, spec = _registration_body(DROP_LINE, intent, revision)
    publication = {
        "recipeId": f"{FORMAT}:master", "engine": "sourced_video", "recipeVersion": "v1",
        "recipeSpecHash": "sha256:" + "cd" * 32,
        "recipeSpecCanonical": json.dumps(spec, sort_keys=True, separators=(",", ":")),
    }
    monkeypatch.setattr(cp, "list_registered_recipe_bindings", lambda *a: [("k", publication)])
    monkeypatch.setattr(cp, "load_engine_registry", lambda: ({}, "h"))
    monkeypatch.setattr(cp, "resolve_slideshow_recipe", lambda publication: None)
    monkeypatch.setattr(cp, "resolve_generation_recipe_with_reason", lambda publication: (None, None))
    monkeypatch.setattr(cp, "_dossier_source_recipe", lambda publication, refusal_reasons=None: None)
    monkeypatch.setattr(cp, "syzygy_runtime_available", lambda: True)

    response = cp.capabilities(x_rt_page_id=DROP_LINE, x_page_id=None)
    assert response["capabilities"] == [{
        "recipeId": f"{FORMAT}:master", "engine": "sourced_video", "recipeVersion": "v1",
        "maxQuantity": 0, "zeroReason": reasons.POOL_SUPPLIED,
    }]
