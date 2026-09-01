"""The Dossier ingredient view is a projection of existing authorities only."""

import copy

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.control_plane_dossier import REQUEST_SCHEMA, router
from routers.control_plane_recipes import LANE
from tests.master_pages_fixtures import master_pages


TOKEN = "test-control-plane-token"
PAGE_ID = "acct:truck-page"


def _client(monkeypatch) -> TestClient:
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    app = FastAPI()
    app.include_router(router, prefix="/api/control-plane")
    return TestClient(app)


def _request(content_niche="TRUCK", content_engine="ai_video"):
    intent, revision = master_pages(
        PAGE_ID,
        handle="truck.page",
        content_niche=content_niche,
        content_engine=content_engine,
    )
    return {
        "schema": REQUEST_SCHEMA,
        "masterPages": intent,
        "masterPagesHash": revision,
    }


def _headers():
    return {
        "Authorization": f"Bearer {TOKEN}",
        "X-RT-Lane": LANE,
        "X-RT-Page-Id": PAGE_ID,
    }


def _format(body, format_id):
    return next(entry for entry in body["formats"] if entry["formatId"] == format_id)


def _ingredient(format_entry, ingredient_id):
    return next(
        entry for entry in format_entry["ingredients"]
        if entry["ingredientId"] == ingredient_id
    )


def _production_selection(body, format_id, provider_id="hailuo"):
    selections = _format(body, format_id)["productionSelections"]
    return next(
        entry for entry in selections
        if entry["providerId"] == provider_id
    )


def _source_selection(body, format_id, library_id):
    return next(
        entry for entry in _format(body, format_id)["productionSelections"]
        if entry["sourceLibraryId"] == library_id
    )


def test_catalog_is_rooted_in_exact_master_pages_and_content_lab_registries(monkeypatch):
    response = _client(monkeypatch).post(
        "/api/control-plane/v1/dossier-ingredients",
        json=_request(),
        headers=_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["pageId"] == PAGE_ID
    assert body["masterPages"] == _request()["masterPages"]
    assert body["masterPagesHash"] == _request()["masterPagesHash"]
    assert body["currentFormatCandidates"] == ["truck-scenic", "truck-ugc"]
    assert body["catalogVersion"].startswith("sha256:")

    truck = _format(body, "truck-scenic")
    model = _ingredient(truck, "visual-model")
    prompt = _ingredient(truck, "prompt-module")
    assert model["binding"]["modelId"] == "minimax/hailuo-2.3"
    assert model["binding"]["defaults"] == {
        "duration": 6,
        "optimize_prompt": False,
        "resolution": "1080p",
    }
    hailuo = next(
        option for option in model["options"]
        if option["providerId"] == "hailuo"
    )
    assert hailuo["controls"]["duration"]["options"] == [6, 10]
    assert prompt["binding"]["template"].startswith("Extreme wide")
    assert "setting" in prompt["binding"]["variationGroups"]
    treatment = _ingredient(truck, "clip-treatment")
    assert _production_selection(body, "truck-scenic")["catalogVersion"].startswith("sha256:")
    assert treatment["kind"] == "render_treatment"
    assert treatment["binding"]["clipSpeed"] == {
        "type": "range", "minimum": 0.5, "maximum": 2.0, "default": 1.0,
    }
    assert treatment["binding"]["filters"] == {
        "brightness": {"type": "range", "minimum": 0.0, "maximum": 3.0, "step": 0.05, "default": 1.0, "label": "Brightness"},
        "contrast": {"type": "range", "minimum": 0.0, "maximum": 3.0, "step": 0.05, "default": 1.0, "label": "Contrast"},
        "saturation": {"type": "range", "minimum": 0.0, "maximum": 3.0, "step": 0.05, "default": 1.0, "label": "Saturation"},
        "warmth": {"type": "range", "minimum": -1.0, "maximum": 1.0, "step": 0.05, "default": 0.0, "label": "Warmth"},
        "fade": {"type": "range", "minimum": 0.0, "maximum": 1.0, "step": 0.05, "default": 0.0, "label": "Fade"},
        "grain": {"type": "range", "minimum": 0.0, "maximum": 1.0, "step": 0.05, "default": 0.0, "label": "Grain"},
        "vignette": {"type": "range", "minimum": 0.0, "maximum": 1.0, "step": 0.05, "default": 0.0, "label": "Vignette"},
    }


def test_selected_version_ignores_unrelated_format_authority_changes(monkeypatch):
    import services.dossier_ingredients as ingredients

    request = _request()
    before = ingredients.build_dossier_ingredient_catalog(
        PAGE_ID, request["masterPages"], request["masterPagesHash"],
    )
    selected_before = _production_selection(before, "truck-scenic")["catalogVersion"]
    original = ingredients._format_entry

    def changed_unrelated(contract, profile, catalog, model_options, page_id):
        entry = original(contract, profile, catalog, model_options, page_id)
        if entry["formatId"] == "pov-night-core":
            entry = copy.deepcopy(entry)
            entry["review"]["gates"].append("new-unrelated-gate")
        return entry

    monkeypatch.setattr(ingredients, "_format_entry", changed_unrelated)
    after = ingredients.build_dossier_ingredient_catalog(
        PAGE_ID, request["masterPages"], request["masterPagesHash"],
    )
    assert after["formats"] != before["formats"]
    assert after["catalogVersion"] != before["catalogVersion"]
    assert _production_selection(after, "truck-scenic")["catalogVersion"] == selected_before


def test_selected_version_ignores_an_unused_same_niche_format(monkeypatch):
    import services.dossier_ingredients as ingredients

    request = _request()
    before = ingredients.build_dossier_ingredient_catalog(
        PAGE_ID, request["masterPages"], request["masterPagesHash"],
    )
    selected_before = _production_selection(before, "truck-scenic")["catalogVersion"]
    original = ingredients._format_entry

    def changed_unused_candidate(contract, profile, catalog, model_options, page_id):
        entry = original(contract, profile, catalog, model_options, page_id)
        if entry["formatId"] == "truck-ugc":
            entry = copy.deepcopy(entry)
            entry["review"]["gates"].append("new-unused-candidate-gate")
        return entry

    monkeypatch.setattr(ingredients, "_format_entry", changed_unused_candidate)
    after = ingredients.build_dossier_ingredient_catalog(
        PAGE_ID, request["masterPages"], request["masterPagesHash"],
    )
    assert _production_selection(after, "truck-scenic")["catalogVersion"] == selected_before


def test_selected_version_ignores_an_unselected_model_option(monkeypatch):
    import services.dossier_ingredients as ingredients

    request = _request()
    before = ingredients.build_dossier_ingredient_catalog(
        PAGE_ID, request["masterPages"], request["masterPagesHash"],
    )
    selected_before = _production_selection(before, "truck-scenic")["catalogVersion"]
    original = ingredients._model_options

    def with_unused_model(catalog):
        options = copy.deepcopy(original(catalog))
        option = copy.deepcopy(options[0])
        option["providerId"] = "unused-provider"
        option["modelId"] = "unused/model"
        options.append(option)
        return options

    monkeypatch.setattr(ingredients, "_model_options", with_unused_model)
    after = ingredients.build_dossier_ingredient_catalog(
        PAGE_ID, request["masterPages"], request["masterPagesHash"],
    )
    assert _production_selection(after, "truck-scenic")["catalogVersion"] == selected_before


def test_selected_version_ignores_display_only_model_labels(monkeypatch):
    import services.dossier_ingredients as ingredients

    request = _request()
    before = ingredients.build_dossier_ingredient_catalog(
        PAGE_ID, request["masterPages"], request["masterPagesHash"],
    )
    selected_before = _production_selection(before, "truck-scenic")["catalogVersion"]
    original = ingredients._model_options

    def relabeled(catalog):
        options = copy.deepcopy(original(catalog))
        for option in options:
            if option["providerId"] == "hailuo":
                option["providerLabel"] = "Display label only"
                option["controls"]["duration"]["label"] = "Display duration only"
                option["controls"]["duration"]["note"] = "Display note only"
        return options

    monkeypatch.setattr(ingredients, "_model_options", relabeled)
    after = ingredients.build_dossier_ingredient_catalog(
        PAGE_ID, request["masterPages"], request["masterPagesHash"],
    )
    assert _production_selection(after, "truck-scenic")["catalogVersion"] == selected_before


def test_selected_source_version_ignores_an_unselected_page_library(monkeypatch):
    import services.dossier_ingredients as ingredients

    page_id = "tt-chase-miles-4l"
    intent, revision = master_pages(
        page_id, handle="chase.miles.4l",
        content_niche="POV - Dirtbike", content_engine="sourced_video",
    )
    before = ingredients.build_dossier_ingredient_catalog(page_id, intent, revision)
    library_id = "pov-dirt-bike-chase-miles-4l-v1"
    selected_before = _source_selection(
        before, "pov-dirt-bike", library_id,
    )["catalogVersion"]
    original = ingredients._master_source_options

    def with_unused_source(format_slug, requested_page_id):
        options = copy.deepcopy(original(format_slug, requested_page_id))
        if options:
            unused = copy.deepcopy(options[0])
            unused["libraryId"] = "unused-page-library"
            unused["version"] = "sha256:" + "f" * 64
            options.append(unused)
        return options

    monkeypatch.setattr(ingredients, "_master_source_options", with_unused_source)
    after = ingredients.build_dossier_ingredient_catalog(page_id, intent, revision)
    assert _source_selection(
        after, "pov-dirt-bike", library_id,
    )["catalogVersion"] == selected_before


def test_selected_version_changes_with_its_exact_format_authority(monkeypatch):
    import services.dossier_ingredients as ingredients

    request = _request()
    before = ingredients.build_dossier_ingredient_catalog(
        PAGE_ID, request["masterPages"], request["masterPagesHash"],
    )
    selected_before = _production_selection(before, "truck-scenic")["catalogVersion"]
    original = ingredients._format_entry

    def changed_current(contract, profile, catalog, model_options, page_id):
        entry = original(contract, profile, catalog, model_options, page_id)
        if entry["formatId"] == "truck-scenic":
            entry = copy.deepcopy(entry)
            entry["review"]["gates"].append("new-current-format-gate")
        return entry

    monkeypatch.setattr(ingredients, "_format_entry", changed_current)
    after = ingredients.build_dossier_ingredient_catalog(
        PAGE_ID, request["masterPages"], request["masterPagesHash"],
    )
    assert _production_selection(after, "truck-scenic")["catalogVersion"] != selected_before


def test_selected_version_differs_from_the_pinned_legacy_fleet_hash():
    import services.dossier_ingredients as ingredients

    request = _request()
    catalog = ingredients.build_dossier_ingredient_catalog(
        PAGE_ID, request["masterPages"], request["masterPagesHash"],
    )
    assert _production_selection(catalog, "truck-scenic")["catalogVersion"] not in (
        ingredients.PINNED_LEGACY_DOSSIER_CATALOG_VERSIONS
    )


def test_live_legacy_exemption_is_bound_to_the_complete_immutable_publication():
    import services.dossier_ingredients as ingredients

    key, catalog_version = next(iter(
        ingredients.PINNED_LEGACY_DOSSIER_CATALOG_VERSIONS_BY_PUBLICATION.items()
    ))
    page_id, recipe_id, recipe_version, dossier_revision, recipe_spec_hash = key
    assert ingredients.is_pinned_legacy_catalog_version(
        catalog_version,
        page_id=page_id,
        recipe_id=recipe_id,
        recipe_version=recipe_version,
        dossier_revision=dossier_revision,
        recipe_spec_hash=recipe_spec_hash,
    )
    assert not ingredients.is_pinned_legacy_catalog_version(
        catalog_version,
        page_id=page_id,
        recipe_id=recipe_id,
        recipe_version=recipe_version,
        dossier_revision=dossier_revision + "-changed",
        recipe_spec_hash=recipe_spec_hash,
    )
    assert not ingredients.is_pinned_legacy_catalog_version(
        catalog_version,
        page_id=page_id,
        recipe_id=recipe_id,
        recipe_version=recipe_version,
        dossier_revision=dossier_revision,
        recipe_spec_hash="sha256:" + "f" * 64,
    )


def test_reference_and_source_slots_come_from_real_catalogs(monkeypatch):
    body = _client(monkeypatch).post(
        "/api/control-plane/v1/dossier-ingredients",
        json=_request(),
        headers=_headers(),
    ).json()
    boat_reference = _ingredient(_format(body, "boat-lake"), "reference-media")
    assert boat_reference["status"] == "bound"
    assert boat_reference["binding"]["sha256"] == "6fcf8daf2cc422457af7de83032b90f018f66ceec801eba9e22f7d80f7f6a583"

    dirtbike = _format(body, "pov-dirt-bike")
    master_source = _ingredient(dirtbike, "master-source-video")
    library = _ingredient(dirtbike, "approved-cut-library")
    treatment = _ingredient(dirtbike, "clip-treatment")
    assert master_source["status"] == "missing"
    assert "master-source-video:missing" in dirtbike["blockers"]
    assert library["status"] == "reference"
    assert library["binding"]["libraryId"] == "pov-dirt-bike-8791-20260828"
    assert library["binding"]["clipCount"] == 39
    assert library["binding"]["role"] == "approved_derivative_clips"
    assert library["binding"]["recutEligible"] is False
    assert library["binding"]["lineageStatus"] == "parent_source_and_cut_window_missing"
    assert len(library["binding"]["clips"]) == 39
    assert all(clip["parentSource"] is None and clip["cutWindow"] is None
               for clip in library["binding"]["clips"])
    assert treatment["binding"]["scope"] == "master_source_window"
    assert treatment["binding"]["recutWindow"] == "deterministic_without_replacement"
    assert treatment["binding"]["reservation"] == "active_jobs_and_completed_outputs"
    assert treatment["binding"]["output"] == {
        "aspectRatio": "9:16",
        "encodePreset": "tiktok_delivery_v1",
        "height": 1920,
        "width": 1080,
    }
    assert treatment["binding"]["controls"]["cutDurationMs"] == {
        "type": "range", "min": 6000, "max": 8000,
        "step": 1000, "default": 7000,
    }
    assert treatment["binding"]["clipSpeed"] == {
        "type": "range", "minimum": 0.5, "maximum": 2.0, "default": 1.0,
    }
    assert "window" not in treatment["binding"]

    coffee = _format(body, "coffee-tok")
    assert _ingredient(coffee, "visual-model")["status"] == "missing"
    assert _ingredient(coffee, "prompt-module")["status"] == "missing"


def test_request_fails_closed_on_auth_lane_or_master_pages_drift(monkeypatch):
    client = _client(monkeypatch)
    request = _request()
    assert client.post(
        "/api/control-plane/v1/dossier-ingredients", json=request,
        headers={**_headers(), "Authorization": "Bearer wrong"},
    ).status_code == 401
    assert client.post(
        "/api/control-plane/v1/dossier-ingredients", json=request,
        headers={**_headers(), "X-RT-Lane": "other"},
    ).status_code == 400
    request["masterPagesHash"] = "sha256:" + "0" * 64
    assert client.post(
        "/api/control-plane/v1/dossier-ingredients", json=request,
        headers=_headers(),
    ).status_code == 409
