"""Real v1 typed registration accepts only the advertised lyric timing control.

No source generation or provider request; the source library transport is a
local fixture. The catalog, selector hash and HTTP recipe registry are real.
"""
import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import control_plane as cp, control_plane_recipes as recipes
from services import dossier_ingredients as ingredients
from services.master_pages_contract import intent_hash
from tests.master_pages_fixtures import master_pages, bind_current_intent
from tests.test_control_plane_lyric_video import PAGE, ACCOUNT, recipe_and_library

TOKEN = "fixture-control-plane-token"
SET = "burnt-heavy-foot-chorus-v1"

@pytest.fixture
def registration(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("CONTENT_LAB_RECIPE_ROOT", str(tmp_path / "recipes"))
    monkeypatch.setenv("SYZYGY_API_URL", "https://syzygy.example")
    monkeypatch.setenv("SYZYGY_API_KEY", "fixture-only")
    _, library = recipe_and_library()
    monkeypatch.setattr(ingredients, "load_syzygy_library", lambda *a, **kw: library)
    monkeypatch.setattr(cp, "load_syzygy_library", lambda *a, **kw: library)
    monkeypatch.setattr(cp, "load_recipe_library", lambda *a, **kw: library)
    intent, _ = master_pages(PAGE, handle=ACCOUNT, content_niche="lyric-edits", content_engine="lyrics_slideshows")
    intent["notionPageId"] = "3451465b-b829-80a8-b9d1-fb7e34b98ff5"
    revision = intent_hash(intent)
    bind_current_intent(monkeypatch, cp, intent, revision)
    catalog = ingredients.build_dossier_ingredient_catalog(PAGE, intent, revision)
    entry = next(f for f in catalog["formats"] if f["formatId"] == "lyric-video")
    selected = next(s for s in entry["productionSelections"] if s.get("controls") == {"lyricTimingSetId": SET})
    production = {key: selected.get(key) for key in ["catalogVersion", "providerId", "modelId", "promptModuleId", "referenceSetId", "sourceLibraryId"]}
    production.update(variationValues={}, controls={"lyricTimingSetId": SET})
    spec = {"schema": "dossier.recipe-spec.v4", "masterPages": intent, "masterPagesHash": revision,
            "production": production, "renderTreatment": {"stylePreset": None, "filters": {"brightness": .85},
                "captionStyle": {"font": "TikTokSans16pt-SemiBold.ttf", "size_pt": 12, "color": "#ffffff", "position": "middle", "align": "center", "line_balance": 30},
                "clipSpeed": 1.0, "clipCrop": {"zoom": 1.0, "focusX": .5, "focusY": .5}},
            "demand": {"formatMix": {"lyric-video": 1.0}},
            "captionDiscipline": {"captionSet": "lyric", "register": ["heartbreak_life"], "slingshotShare": None}}
    app = FastAPI(); app.include_router(cp.router, prefix="/api/control-plane"); app.include_router(recipes.router, prefix="/api/control-plane")
    return TestClient(app), spec


def request(spec):
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return {"schema": recipes.REQUEST_SCHEMA, "pageId": PAGE, "lane": recipes.LANE,
            "recipeId": "lyric-video:master", "engine": "lyrics_slideshows", "recipeVersion": "registered-timing-v1",
            "dossierRevision": "fixture-rev-1", "recipeSpecHash": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
            "recipeSpecCanonical": canonical}


def headers():
    return {"Authorization": "Bearer " + TOKEN, "X-RT-Lane": recipes.LANE, "X-RT-Page-Id": PAGE,
            "Idempotency-Key": "timing-control-v1-regression"}


def test_v1_public_registration_preserves_advertised_timing_control_and_is_idempotent(registration):
    client, spec = registration
    body = request(spec)
    first = client.post("/api/control-plane/v1/recipes", json=body, headers=headers())
    assert first.status_code == 200, first.json()
    repeated = client.post("/api/control-plane/v1/recipes", json=body, headers=headers())
    assert repeated.status_code == 200, repeated.json()
    assert first.json() == repeated.json()
    assert spec["production"]["controls"] == {"lyricTimingSetId": SET}


def test_unadvertised_lyric_timing_control_is_refused_by_current_catalog(registration):
    client, spec = registration
    spec["production"]["controls"]["lyricTimingSetId"] = "foreign-unregistered-set"
    response = client.post("/api/control-plane/v1/recipes", json=request(spec), headers=headers())
    assert response.status_code == 409
    assert response.json()["detail"] == "production selection uses a stale Content Lab catalog"


def test_capabilities_advertise_a_point_seventy_five_lyric_publication(registration, monkeypatch):
    client, spec = registration
    monkeypatch.setenv("CONTENT_LAB_GENERATION_MODE", "ready")
    monkeypatch.setenv("SYZYGY_API_KEY", "syz_fixture_key")
    spec["renderTreatment"]["filters"]["brightness"] = 0.75
    registered = client.post("/api/control-plane/v1/recipes", json=request(spec), headers=headers())
    assert registered.status_code == 200, registered.json()
    capabilities = client.get(
        "/api/control-plane/v1/capabilities", headers=headers(),
    ).json()["capabilities"]
    row = next(item for item in capabilities if item["recipeId"] == "lyric-video:master")
    assert row["engine"] == "lyrics_slideshows"
    assert row["maxQuantity"] > 0
