"""The Dossier ingredient view is a projection of existing authorities only."""

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
    assert treatment["kind"] == "render_treatment"
    assert treatment["binding"]["clipSpeed"] == {
        "type": "range", "minimum": 0.5, "maximum": 2.0, "default": 1.0,
    }


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
