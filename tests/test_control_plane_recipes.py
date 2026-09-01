"""Dossier publication must become an executable, page-scoped Lab capability."""

import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.control_plane_recipes as recipes
from services.dossier_ingredients import (
    PINNED_LEGACY_DOSSIER_CATALOG_VERSIONS_BY_PUBLICATION,
    build_dossier_ingredient_catalog,
    catalog_selection_version,
)
from tests.master_pages_fixtures import master_pages


TOKEN = "test-control-plane-token"
PAGE_ID = "acct:truck-page"


def _payload(**overrides):
    intent, revision = master_pages(PAGE_ID, handle="truck.page")
    spec = json.dumps(
        {
            "schema": "dossier.recipe-spec.v2",
            "masterPages": intent,
            "masterPagesHash": revision,
            "renderTreatment": {
                "stylePreset": "warm-truck",
                "filters": {"brightness": 1.03},
                "captionStyle": {},
                "clipSpeed": 1.25,
                "clipCrop": {"zoom": 1.6, "focusX": 0.25, "focusY": 0.7},
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
        "engine": "ai_video",
        "recipeVersion": "dossier-1234567890abcdef",
        "dossierRevision": "rev-1",
        "recipeSpecHash": "sha256:" + hashlib.sha256(spec.encode()).hexdigest(),
        "recipeSpecCanonical": spec,
    }
    body.update(overrides)
    return body


def _v3_payload(catalog_version=None, **overrides):
    body = _payload(recipeId="truck-scenic:master", **overrides)
    spec = json.loads(body["recipeSpecCanonical"])
    catalog = build_dossier_ingredient_catalog(
        PAGE_ID, spec["masterPages"], spec["masterPagesHash"],
    )
    production = {
        "catalogVersion": "",
        "providerId": "hailuo",
        "modelId": "minimax/hailuo-2.3",
        "promptModuleId": "truck",
        "referenceSetId": None,
        "sourceLibraryId": None,
        "variationValues": {},
        "controls": {},
    }
    production["catalogVersion"] = catalog_version or catalog_selection_version(
        catalog, "truck-scenic", production,
    )
    spec["schema"] = "dossier.recipe-spec.v3"
    spec["production"] = production
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    body["recipeSpecCanonical"] = canonical
    body["recipeSpecHash"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
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
        PAGE_ID, "trucks", "ai_video", publication["recipeVersion"],
    )
    assert stored["recipeSpecHash"] == publication["recipeSpecHash"]
    assert stored["dossierRevision"] == publication["dossierRevision"]
    assert recipes.load_registered_recipe(
        "acct:other", "trucks", "ai_video", publication["recipeVersion"],
    ) is None


def test_registered_recipe_binding_reuses_only_exact_notion_identity_after_page_id_migration(
    lab,
):
    canonical_intent, canonical_hash = master_pages(PAGE_ID, handle="truck.page")
    operational_id = "acct:rail:legacy-truck"
    publication = _payload(pageId=operational_id)
    spec = json.loads(publication["recipeSpecCanonical"])
    spec["masterPages"] = {**canonical_intent, "pageId": operational_id}
    spec["masterPagesHash"] = recipes.intent_hash(spec["masterPages"])
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    publication["recipeSpecCanonical"] = canonical
    publication["recipeSpecHash"] = "sha256:" + hashlib.sha256(
        canonical.encode()
    ).hexdigest()

    headers = _publication_headers(**{
        "X-RT-Page-Id": operational_id,
        "Idempotency-Key": "dossier:operational-binding",
    })
    assert lab.post(
        "/api/control-plane/v1/recipes", json=publication, headers=headers,
    ).status_code == 200

    binding = recipes.load_registered_recipe_binding(
        PAGE_ID, publication["recipeId"], publication["engine"],
        publication["recipeVersion"], canonical_intent, canonical_hash,
    )
    assert binding is not None
    assert binding[0] == operational_id
    assert binding[1]["recipeSpecHash"] == publication["recipeSpecHash"]

    changed = {**canonical_intent, "group": "WARNER"}
    changed_hash = recipes.intent_hash(changed)
    assert recipes.load_registered_recipe_binding(
        PAGE_ID, publication["recipeId"], publication["engine"],
        publication["recipeVersion"], changed, changed_hash,
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


def test_v3_publish_accepts_exact_and_full_pinned_legacy_but_rejects_reuse(
    lab, monkeypatch,
):
    exact = _v3_payload(
        dossierRevision="rev-v3-exact", recipeVersion="dossier-v3-exact0001",
    )
    assert lab.post(
        "/api/control-plane/v1/recipes", json=exact,
        headers=_publication_headers(**{"Idempotency-Key": "dossier:v3-exact"}),
    ).status_code == 200

    legacy_catalog_version = next(iter(
        PINNED_LEGACY_DOSSIER_CATALOG_VERSIONS_BY_PUBLICATION.values()
    ))
    legacy = _v3_payload(
        legacy_catalog_version,
        dossierRevision="rev-v3-legacy",
        recipeVersion="dossier-v3-legacy001",
    )
    monkeypatch.setitem(
        PINNED_LEGACY_DOSSIER_CATALOG_VERSIONS_BY_PUBLICATION,
        (
            legacy["pageId"], legacy["recipeId"], legacy["recipeVersion"],
            legacy["dossierRevision"], legacy["recipeSpecHash"],
        ),
        legacy_catalog_version,
    )
    assert lab.post(
        "/api/control-plane/v1/recipes", json=legacy,
        headers=_publication_headers(**{"Idempotency-Key": "dossier:v3-legacy"}),
    ).status_code == 200

    reused = {**legacy, "dossierRevision": "rev-v3-reused"}
    assert lab.post(
        "/api/control-plane/v1/recipes", json=reused,
        headers=_publication_headers(**{"Idempotency-Key": "dossier:v3-reused"}),
    ).status_code == 409

    changed_spec = json.loads(legacy["recipeSpecCanonical"])
    changed_spec["renderTreatment"]["clipSpeed"] = 1.5
    changed_canonical = json.dumps(
        changed_spec, sort_keys=True, separators=(",", ":"),
    )
    changed_bytes = {
        **legacy,
        "recipeSpecCanonical": changed_canonical,
        "recipeSpecHash": "sha256:" + hashlib.sha256(
            changed_canonical.encode()
        ).hexdigest(),
    }
    assert lab.post(
        "/api/control-plane/v1/recipes", json=changed_bytes,
        headers=_publication_headers(**{"Idempotency-Key": "dossier:v3-changed"}),
    ).status_code == 409

    stale = _v3_payload(
        "sha256:" + "f" * 64,
        dossierRevision="rev-v3-stale",
        recipeVersion="dossier-v3-stale0001",
    )
    assert lab.post(
        "/api/control-plane/v1/recipes", json=stale,
        headers=_publication_headers(**{"Idempotency-Key": "dossier:v3-stale"}),
    ).status_code == 409


@pytest.mark.parametrize("malformed", [{}, []])
def test_v3_publish_rejects_non_string_catalog_version_without_500(lab, malformed):
    body = _v3_payload(
        dossierRevision="rev-v3-malformed",
        recipeVersion="dossier-v3-malformed1",
    )
    spec = json.loads(body["recipeSpecCanonical"])
    spec["production"]["catalogVersion"] = malformed
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    body["recipeSpecCanonical"] = canonical
    body["recipeSpecHash"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    response = lab.post(
        "/api/control-plane/v1/recipes", json=body,
        headers=_publication_headers(**{
            "Idempotency-Key": f"dossier:v3-malformed-{type(malformed).__name__}",
        }),
    )
    assert response.status_code == 400


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


def test_clip_crop_is_bounded_and_old_recipe_bytes_remain_accepted(lab):
    old = _payload(dossierRevision="rev-no-crop")
    decoded = json.loads(old["recipeSpecCanonical"])
    decoded["renderTreatment"].pop("clipCrop")
    canonical = json.dumps(decoded, sort_keys=True, separators=(",", ":"))
    old["recipeSpecCanonical"] = canonical
    old["recipeSpecHash"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    assert lab.post(
        "/api/control-plane/v1/recipes", json=old,
        headers=_publication_headers(**{"Idempotency-Key": "dossier:no-crop"}),
    ).status_code == 200

    invalid_crops = (
        {"zoom": 0.99, "focusX": 0.5, "focusY": 0.5},
        {"zoom": 3.01, "focusX": 0.5, "focusY": 0.5},
        {"zoom": 1.0, "focusX": -0.01, "focusY": 0.5},
        {"zoom": 1.0, "focusX": 0.5, "focusY": 1.01},
        {"zoom": True, "focusX": 0.5, "focusY": 0.5},
        {"zoom": 1.0, "focusX": 0.5},
    )
    for index, invalid in enumerate(invalid_crops):
        body = _payload(dossierRevision=f"rev-crop-{index}")
        decoded = json.loads(body["recipeSpecCanonical"])
        decoded["renderTreatment"]["clipCrop"] = invalid
        canonical = json.dumps(decoded, sort_keys=True, separators=(",", ":"))
        body["recipeSpecCanonical"] = canonical
        body["recipeSpecHash"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
        assert lab.post(
            "/api/control-plane/v1/recipes", json=body,
            headers=_publication_headers(**{"Idempotency-Key": f"dossier:crop-{index}"}),
        ).status_code == 400
