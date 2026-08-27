"""Dossier publication must become an executable, page-scoped Lab capability."""

import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.control_plane_recipes as recipes


TOKEN = "test-control-plane-token"
PAGE_ID = "acct:truck-page"


def _payload(**overrides):
    spec = json.dumps(
        {
            "schema": "dossier.recipe-spec.v1",
            "renderTreatment": {
                "stylePreset": "warm-truck",
                "filters": {"brightness": 1.03},
                "captionStyle": {},
                "clipSpeed": 1.25,
            },
            "demand": {"formatMix": {"truck-scenic": 1}},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    body = {
        "schema": recipes.REQUEST_SCHEMA,
        "pageId": PAGE_ID,
        "lane": recipes.LANE,
        "recipeId": "trucks",
        "engine": "content_lab",
        "recipeVersion": "dossier-1234567890abcdef",
        "dossierRevision": "rev-1",
        "recipeSpecHash": "sha256:" + hashlib.sha256(spec.encode()).hexdigest(),
        "recipeSpecCanonical": spec,
    }
    body.update(overrides)
    return body


@pytest.fixture
def lab(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("CONTENT_LAB_RECIPE_ROOT", str(tmp_path / "dossier-recipes"))

    app = FastAPI()
    app.include_router(recipes.router, prefix="/api/control-plane")
    return TestClient(app)


def _publication_headers(**overrides):
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "X-RT-Lane": recipes.LANE,
        "X-RT-Page-Id": PAGE_ID,
        "Idempotency-Key": "dossier:one",
    }
    headers.update(overrides)
    return headers


def test_publication_is_immutable_idempotent_and_requires_dedicated_auth(lab):
    first = lab.post(
        "/api/control-plane/v1/recipes", json=_payload(), headers=_publication_headers(),
    )
    second = lab.post(
        "/api/control-plane/v1/recipes", json=_payload(), headers=_publication_headers(),
    )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()

    missing = _publication_headers()
    missing.pop("Authorization")
    assert lab.post("/api/control-plane/v1/recipes", json=_payload(), headers=missing).status_code == 401

    changed = _payload(dossierRevision="rev-2")
    assert lab.post(
        "/api/control-plane/v1/recipes", json=changed, headers=_publication_headers(),
    ).status_code == 409


def test_registered_dossier_version_is_page_scoped_and_durably_stored(lab):
    publication = _payload()
    assert lab.post(
        "/api/control-plane/v1/recipes", json=publication, headers=_publication_headers(),
    ).status_code == 200

    stored = recipes.load_registered_recipe(
        PAGE_ID, "trucks", "content_lab", publication["recipeVersion"],
    )
    assert stored["recipeSpecHash"] == publication["recipeSpecHash"]
    assert stored["dossierRevision"] == publication["dossierRevision"]
    assert recipes.load_registered_recipe(
        "acct:other", "trucks", "content_lab", publication["recipeVersion"],
    ) is None


def test_hash_schema_and_prompt_shaped_fields_fail_closed(lab):
    bad_hash = _payload(recipeSpecHash="sha256:" + "0" * 64)
    assert lab.post(
        "/api/control-plane/v1/recipes", json=bad_hash, headers=_publication_headers(),
    ).status_code == 409

    bad_spec = _payload()
    decoded = json.loads(bad_spec["recipeSpecCanonical"])
    decoded["renderTreatment"]["prompt"] = "caller-controlled instruction"
    canonical = json.dumps(decoded, sort_keys=True, separators=(",", ":"))
    bad_spec["recipeSpecCanonical"] = canonical
    bad_spec["recipeSpecHash"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    assert lab.post(
        "/api/control-plane/v1/recipes", json=bad_spec, headers=_publication_headers(),
    ).status_code == 400


def test_clip_speed_is_bounded_and_old_recipe_bytes_remain_accepted(lab):
    old = _payload()
    decoded = json.loads(old["recipeSpecCanonical"])
    decoded["renderTreatment"].pop("clipSpeed")
    canonical = json.dumps(decoded, sort_keys=True, separators=(",", ":"))
    old["recipeSpecCanonical"] = canonical
    old["recipeSpecHash"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    assert lab.post(
        "/api/control-plane/v1/recipes", json=old, headers=_publication_headers(),
    ).status_code == 200

    for invalid in (0.49, 2.01, True, "fast", float("nan")):
        body = _payload(dossierRevision=f"rev-{invalid!s}")
        decoded = json.loads(body["recipeSpecCanonical"])
        decoded["renderTreatment"]["clipSpeed"] = invalid
        canonical = json.dumps(decoded, sort_keys=True, separators=(",", ":"))
        body["recipeSpecCanonical"] = canonical
        body["recipeSpecHash"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
        assert lab.post(
            "/api/control-plane/v1/recipes", json=body,
            headers=_publication_headers(**{"Idempotency-Key": f"dossier:speed-{invalid!s}"}),
        ).status_code == 400
