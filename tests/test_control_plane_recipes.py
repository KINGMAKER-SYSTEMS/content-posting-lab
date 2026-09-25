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
from services.control_plane_generation import resolve_generation_recipe
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


def _v4_payload(**overrides):
    body = _v3_payload(**overrides)
    spec = json.loads(body["recipeSpecCanonical"])
    spec["schema"] = "dossier.recipe-spec.v4"
    spec["captionDiscipline"] = {
        "captionSet": "truck-tok",
        "register": ["heartbreak_relationship", "relationship_sincere"],
        "slingshotShare": None,
    }
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


def _with_spec(body, mutate):
    spec = json.loads(body["recipeSpecCanonical"])
    mutate(spec)
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return {
        **body,
        "recipeSpecCanonical": canonical,
        "recipeSpecHash": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
    }


def _record_file(publication):
    return recipes._record_path(recipes._root(), publication)


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

    changed = _with_spec(
        _payload(), lambda spec: spec["renderTreatment"].update(clipSpeed=1.5),
    )
    assert lab.post(
        "/api/control-plane/v1/recipes", json=changed, headers=_publication_headers(),
    ).status_code == 409


def test_identical_recipe_bytes_reregister_under_a_new_dossier_revision(lab):
    """A content-neutral dossier relock re-registers the same recipe tuple.

    Only the dossier revision and idempotency key change; the recipe bytes are
    identical. That must succeed like a fresh registration, advance the stored
    registration, and keep the superseded one as history.
    """
    original = _payload()
    assert lab.post(
        "/api/control-plane/v1/recipes", json=original,
        headers=_publication_headers(**{"Idempotency-Key": "dossier:727"}),
    ).status_code == 200

    relocked = _payload(dossierRevision="rev-901")
    response = lab.post(
        "/api/control-plane/v1/recipes", json=relocked,
        headers=_publication_headers(**{"Idempotency-Key": "dossier:901"}),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "schema": recipes.RESPONSE_SCHEMA,
        "recipeId": relocked["recipeId"],
        "engine": relocked["engine"],
        "recipeVersion": relocked["recipeVersion"],
        "dossierRevision": "rev-901",
        "recipeSpecHash": original["recipeSpecHash"],
        "status": "registered",
    }

    stored = recipes.load_registered_recipe(
        PAGE_ID, "trucks", "ai_video", original["recipeVersion"],
    )
    assert stored["dossierRevision"] == "rev-901"
    assert stored["idempotencyKey"] == "dossier:901"
    assert stored["recipeSpecCanonical"] == original["recipeSpecCanonical"]
    assert stored["recipeSpecHash"] == original["recipeSpecHash"]
    assert stored["priorRegistrations"] == [
        {"dossierRevision": "rev-1", "idempotencyKey": "dossier:727"},
    ]
    # The capability listing sees the advanced registration, not a stale cache.
    listed = recipes.list_registered_recipes(PAGE_ID)
    assert [row["dossierRevision"] for row in listed] == ["rev-901"]
    assert _record_file(original).stat().st_mode & 0o777 == 0o600


def test_different_recipe_bytes_under_a_registered_tuple_still_conflict(lab):
    original = _payload()
    assert lab.post(
        "/api/control-plane/v1/recipes", json=original,
        headers=_publication_headers(**{"Idempotency-Key": "dossier:727"}),
    ).status_code == 200
    before = _record_file(original).read_bytes()

    mutations = (
        lambda spec: spec["renderTreatment"].update(clipSpeed=1.5),
        lambda spec: spec["renderTreatment"]["filters"].update(brightness=1.04),
        lambda spec: spec["renderTreatment"]["captionStyle"].update(case="as_written"),
    )
    for index, mutate in enumerate(mutations):
        for revision, key in (("rev-1", "dossier:727"), (f"rev-x{index}", f"dossier:x{index}")):
            changed = _with_spec(_payload(dossierRevision=revision), mutate)
            response = lab.post(
                "/api/control-plane/v1/recipes", json=changed,
                headers=_publication_headers(**{"Idempotency-Key": key}),
            )
            assert response.status_code == 409
            assert response.json()["detail"] == (
                "recipe tuple is already registered with different bytes"
            )
    assert _record_file(original).read_bytes() == before


def test_registration_replays_are_idempotent_and_never_rewind_the_record(lab):
    original = _payload()
    original_headers = _publication_headers(**{"Idempotency-Key": "dossier:727"})
    first = lab.post(
        "/api/control-plane/v1/recipes", json=original, headers=original_headers,
    )
    assert first.status_code == 200
    replay = lab.post(
        "/api/control-plane/v1/recipes", json=original, headers=original_headers,
    )
    assert replay.status_code == 200
    assert replay.json() == first.json()

    relocked = _payload(dossierRevision="rev-901")
    relocked_headers = _publication_headers(**{"Idempotency-Key": "dossier:901"})
    advanced = lab.post(
        "/api/control-plane/v1/recipes", json=relocked, headers=relocked_headers,
    )
    assert advanced.status_code == 200
    settled = _record_file(original).read_bytes()

    # Exact replay of the newest registration: same answer, no rewrite.
    again = lab.post(
        "/api/control-plane/v1/recipes", json=relocked, headers=relocked_headers,
    )
    assert again.status_code == 200
    assert again.json() == advanced.json()
    assert _record_file(original).read_bytes() == settled

    # A late retry of the superseded request answers for itself (the Worker
    # checks the echoed dossierRevision) without rewinding the stored record.
    late = lab.post(
        "/api/control-plane/v1/recipes", json=original, headers=original_headers,
    )
    assert late.status_code == 200
    assert late.json() == first.json()
    assert _record_file(original).read_bytes() == settled
    stored = recipes.load_registered_recipe(
        PAGE_ID, "trucks", "ai_video", original["recipeVersion"],
    )
    assert stored["dossierRevision"] == "rev-901"
    assert stored["priorRegistrations"] == [
        {"dossierRevision": "rev-1", "idempotencyKey": "dossier:727"},
    ]


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
    # The pin belongs to one exact publication; identical bytes under another
    # revision are refused before the stored pinned record can be advanced.
    pinned = recipes.load_registered_recipe(
        PAGE_ID, legacy["recipeId"], "ai_video", legacy["recipeVersion"],
    )
    assert pinned["dossierRevision"] == "rev-v3-legacy"
    assert pinned["idempotencyKey"] == "dossier:v3-legacy"
    assert "priorRegistrations" not in pinned

    relocked = {**exact, "dossierRevision": "rev-v3-relocked"}
    assert lab.post(
        "/api/control-plane/v1/recipes", json=relocked,
        headers=_publication_headers(**{"Idempotency-Key": "dossier:v3-relocked"}),
    ).status_code == 200
    advanced = recipes.load_registered_recipe(
        PAGE_ID, exact["recipeId"], "ai_video", exact["recipeVersion"],
    )
    assert advanced["dossierRevision"] == "rev-v3-relocked"
    assert resolve_generation_recipe(advanced, require_runtime=False) is not None

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


def test_v4_publish_preserves_the_exact_caption_selection(lab):
    body = _v4_payload(
        dossierRevision="rev-v4-exact", recipeVersion="dossier-v4-exact0001",
    )
    response = lab.post(
        "/api/control-plane/v1/recipes", json=body,
        headers=_publication_headers(**{"Idempotency-Key": "dossier:v4-exact"}),
    )
    assert response.status_code == 200
    stored = recipes.load_registered_recipe(
        PAGE_ID, body["recipeId"], body["engine"], body["recipeVersion"],
    )
    assert stored["recipeSpecCanonical"] == body["recipeSpecCanonical"]
    assert json.loads(stored["recipeSpecCanonical"])["captionDiscipline"] == {
        "captionSet": "truck-tok",
        "register": ["heartbreak_relationship", "relationship_sincere"],
        "slingshotShare": None,
    }


@pytest.mark.parametrize(
    "caption_discipline",
    [
        {"captionSet": None, "register": ["relationship_sincere"], "slingshotShare": None},
        {"captionSet": "Truck Tok", "register": ["relationship_sincere"], "slingshotShare": None},
        {"captionSet": "truck-tok", "register": [], "slingshotShare": None},
        {"captionSet": "truck-tok", "register": ["made_up"], "slingshotShare": None},
        {"captionSet": "truck-tok", "register": [{}], "slingshotShare": None},
        {"captionSet": "truck-tok", "register": ["faith", "faith"], "slingshotShare": None},
        {"captionSet": "truck-tok", "register": ["faith"], "slingshotShare": 1.01},
        {"captionSet": "truck-tok", "register": ["faith"]},
    ],
)
def test_v4_publish_rejects_untyped_caption_selection(lab, caption_discipline):
    body = _v4_payload(
        dossierRevision="rev-v4-invalid", recipeVersion="dossier-v4-invalid01",
    )
    spec = json.loads(body["recipeSpecCanonical"])
    spec["captionDiscipline"] = caption_discipline
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    body["recipeSpecCanonical"] = canonical
    body["recipeSpecHash"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    assert lab.post(
        "/api/control-plane/v1/recipes", json=body,
        headers=_publication_headers(**{
            "Idempotency-Key": "dossier:v4-invalid-" + hashlib.sha256(canonical.encode()).hexdigest()[:12],
        }),
    ).status_code == 400


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
