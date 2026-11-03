"""Pool-supplied pages: the Lab treats their image library as non-blocking.

The Worker posts FINISHED clips for a small committed list of pages from
``vault/<handle>/pool``, not from a Lab-rendered image library. Those pages
must still Save & apply their recipe (registration accepts ``sourceLibraryId``
null) and still appear in getCapabilities (at zero, so replenish is blocked),
while every unlisted page keeps today's catalog bytes and registration rules
byte-for-byte.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.control_plane_recipes as recipes
import services.dossier_ingredients as di
import services.pool_supplied_pages as psp
from services import control_plane_plan_reasons as reasons
from services.dossier_ingredients import (
    build_dossier_ingredient_catalog,
    catalog_selection_version,
)
from tests.master_pages_fixtures import master_pages


TOKEN = "test-control-plane-token"
LISTED_PAGE = "acct:chemically-lit"
UNLISTED_TRUCK = "acct:truck-page"

# Base (origin/main 08db4f5) byte-identity pins for an unlisted ai_video page.
# These must stay byte-identical after the pool-supplied change.
BASE_TRUCK_CATALOG_VERSION = "sha256:1ed143705f3c69627d88d6889eb12e01d70d28081699416a5541db2b8a6e8d57"
BASE_TRUCK_CANONICAL_SHA256 = "68858d5b102d2b49493aa9440496f55c158a945a5be62701897baadf4203a3bf"
BASE_TRUCK_SELECTION_VERSION = "sha256:2b853d4639dc71241435790429d1cfd1b8508c311ec447bcfab4254d9275c442"


@pytest.fixture(autouse=True)
def _no_live_syzygy(monkeypatch):
    def unavailable(profile, master_pages, *, timeout):
        raise di.SyzygyError("test: no live library")
    monkeypatch.setattr(di, "load_syzygy_library", unavailable)


def _listed_intent():
    return master_pages(
        LISTED_PAGE,
        handle="chemically.lit",
        content_niche="lyric-edits",
        content_engine="lyrics_slideshows",
    )


def _format(catalog, format_id):
    return next(entry for entry in catalog["formats"] if entry["formatId"] == format_id)


def _ingredient(format_entry, ingredient_id):
    return next(
        entry for entry in format_entry["ingredients"]
        if entry["ingredientId"] == ingredient_id
    )


def _v3_body(page_id, *, intent, revision, format_id, source_library_id):
    catalog = build_dossier_ingredient_catalog(page_id, intent, revision)
    production = {
        "catalogVersion": "",
        "providerId": None,
        "modelId": None,
        "promptModuleId": None,
        "referenceSetId": None,
        "sourceLibraryId": source_library_id,
        "variationValues": {},
        "controls": {},
    }
    production["catalogVersion"] = (
        catalog_selection_version(catalog, format_id, production)
        or catalog["catalogVersion"]
    )
    spec = {
        "schema": "dossier.recipe-spec.v3",
        "masterPages": intent,
        "masterPagesHash": revision,
        "renderTreatment": {
            "stylePreset": None,
            "filters": {},
            "captionStyle": {},
        },
        "demand": {"formatMix": {format_id: 1}},
        "production": production,
    }
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    body = {
        "schema": recipes.REQUEST_SCHEMA,
        "pageId": page_id,
        "lane": recipes.LANE,
        "recipeId": f"{format_id}:master",
        "engine": "lyrics_slideshows" if format_id in {"lyric-edits", "lyric-video"} else "sourced_slideshow",
        "recipeVersion": "dossier-1234567890abcdef",
        "dossierRevision": "rev-1",
        "recipeSpecHash": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
        "recipeSpecCanonical": canonical,
    }
    return body


@pytest.fixture
def lab(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("CONTENT_LAB_RECIPE_ROOT", str(tmp_path / "dossier-recipes"))
    app = FastAPI()
    app.include_router(recipes.router, prefix="/api/control-plane")
    return TestClient(app)


def _headers(page_id=LISTED_PAGE):
    return {
        "Authorization": f"Bearer {TOKEN}",
        "X-RT-Lane": recipes.LANE,
        "X-RT-Page-Id": page_id,
        "Idempotency-Key": "dossier:pool-supplied",
    }


# ── (a) listed page: no image-library blocker + registration accepts null ────
def test_listed_page_lab_ingredients_are_pool_supplied_and_not_blockers():
    intent, revision = _listed_intent()
    catalog = build_dossier_ingredient_catalog(LISTED_PAGE, intent, revision)

    edits = _format(catalog, "lyric-edits")
    for ingredient_id in ("image-library", "slideshow-treatment"):
        ingredient = _ingredient(edits, ingredient_id)
        assert ingredient["required"] is False, ingredient
        assert ingredient["status"] == "pool_supplied", ingredient
    assert not any(
        blocker.startswith(("image-library:", "slideshow-treatment:", "lyric-timing:"))
        for blocker in edits["blockers"]
    )

    video = _format(catalog, "lyric-video")
    timing = _ingredient(video, "lyric-timing")
    assert timing["required"] is False
    assert timing["status"] == "pool_supplied"
    assert "lyric-timing:missing" not in video["blockers"]

    # caption-bank / sound-collection page_binding_required are unchanged.
    assert _ingredient(edits, "caption-bank") == {
        "ingredientId": "caption-bank",
        "kind": "caption_bank",
        "required": True,
        "status": "page_binding_required",
        "binding": None,
        "options": [],
    }


def test_listed_page_registration_accepts_null_source_library(lab):
    intent, revision = _listed_intent()
    body = _v3_body(
        LISTED_PAGE, intent=intent, revision=revision,
        format_id="lyric-edits", source_library_id=None,
    )
    response = lab.post(
        "/api/control-plane/v1/recipes", json=body, headers=_headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "registered"


def test_listed_lyric_video_registration_without_timing_is_refused_like_base(lab):
    """A listed page with no bound lyricTimingSetId cannot register lyric-video.

    Base refused this (no expressible selection, so the stale-catalog 409 fires).
    A timing-less lyric-video selection can never be expressed on the wire (the
    Worker requires controls exactly {lyricTimingSetId}), so the fix restores
    base behavior: no selection, registration refused.
    """
    intent, revision = _listed_intent()
    body = _v3_body(
        LISTED_PAGE, intent=intent, revision=revision,
        format_id="lyric-video", source_library_id=None,
    )
    response = lab.post(
        "/api/control-plane/v1/recipes", json=body, headers=_headers(),
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "production selection uses a stale Content Lab catalog"


def test_listed_page_explicit_source_library_must_match_an_advertised_option(lab, monkeypatch):
    intent, revision = _listed_intent()
    body = _v3_body(
        LISTED_PAGE, intent=intent, revision=revision,
        format_id="lyric-edits", source_library_id="syzygy:not-advertised",
    )
    # Bypass only the stale-catalog gate (as a pinned legacy publication would)
    # so the explicit-source check is the one that fires. Its exact message must
    # be the advertised-option 409, proving that check runs rather than the
    # stale-catalog one.
    monkeypatch.setattr(
        recipes, "is_pinned_legacy_catalog_version", lambda *a, **k: True,
    )
    response = lab.post(
        "/api/control-plane/v1/recipes", json=body, headers=_headers(),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "selected source library is not registered as master source DNA"


# ── (b) unlisted page: catalog bytes + versions + registration 409 unchanged ─
def test_unlisted_page_catalog_bytes_and_versions_match_base():
    intent, revision = master_pages(
        UNLISTED_TRUCK, handle="truck.page",
        content_niche="TRUCK", content_engine="ai_video",
    )
    catalog = build_dossier_ingredient_catalog(UNLISTED_TRUCK, intent, revision)
    raw = di.canonical_catalog_bytes(UNLISTED_TRUCK, intent, revision)

    assert hashlib.sha256(raw).hexdigest() == BASE_TRUCK_CANONICAL_SHA256
    assert catalog["catalogVersion"] == BASE_TRUCK_CATALOG_VERSION

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
    production["catalogVersion"] = catalog_selection_version(
        catalog, "truck-scenic", production,
    )
    assert production["catalogVersion"] == BASE_TRUCK_SELECTION_VERSION


def test_unlisted_slideshow_registration_still_requires_source_library(lab):
    page_id = "acct:meme-page"
    intent, revision = master_pages(
        page_id, handle="meme.page",
        content_niche="meme", content_engine="sourced_slideshow",
    )
    body = _v3_body(
        page_id, intent=intent, revision=revision,
        format_id="meme-slideshow", source_library_id=None,
    )
    response = lab.post(
        "/api/control-plane/v1/recipes", json=body, headers=_headers(page_id=page_id),
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "sourceLibraryId is required" in detail or "stale" in detail


# ── (c) malformed list fails closed for everyone + logs ALERT ────────────────
def test_malformed_list_fails_closed_and_logs_alert(monkeypatch, tmp_path, caplog):
    import logging
    path = tmp_path / "pool-supplied-pages.v1.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(psp, "_PATH", path)
    caplog.set_level(logging.ERROR)

    assert psp.load_pool_supplied_page_ids() == frozenset()
    assert psp.is_pool_supplied(LISTED_PAGE) is False
    assert any("ALERT pool-supplied-pages" in r.getMessage() for r in caplog.records)


def test_wrong_schema_or_duplicate_page_fails_closed(monkeypatch, tmp_path):
    # Each round breaks exactly one rule, so each fail-closed check in the
    # loader is the only thing that can turn its round into an empty list.
    for bad in (
        [LISTED_PAGE],  # top level is not an object
        {"schema": "wrong", "pages": [LISTED_PAGE]},
        {"schema": psp.SCHEMA, "pages": [LISTED_PAGE], "extra": 1},
        {"schema": psp.SCHEMA, "pages": {LISTED_PAGE: True}},  # not a list
        {"schema": psp.SCHEMA, "pages": [1]},  # id is not a string
        {"schema": psp.SCHEMA, "pages": ["../not-safe"]},
        {"schema": psp.SCHEMA, "pages": [LISTED_PAGE, LISTED_PAGE]},
    ):
        path = tmp_path / "pool-supplied-pages.v1.json"
        path.write_text(json.dumps(bad), encoding="utf-8")
        monkeypatch.setattr(psp, "_PATH", path)
        # The list is cached per path, so drop the cache each round; otherwise
        # later rounds would only see round 1's cached answer.
        psp.reset_pool_supplied_page_ids_cache()
        assert psp._load_pool_supplied_page_ids_uncached() == frozenset(), bad
        assert psp.load_pool_supplied_page_ids() == frozenset(), bad


def test_missing_list_file_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(psp, "_PATH", tmp_path / "no-such-list.json")
    psp.reset_pool_supplied_page_ids_cache()
    assert psp.load_pool_supplied_page_ids() == frozenset()
    assert psp.is_pool_supplied(LISTED_PAGE) is False


def test_invalid_utf8_list_fails_closed_with_alert(monkeypatch, tmp_path, caplog):
    import logging
    path = tmp_path / "pool-supplied-pages.v1.json"
    # A lone 0xff byte inside the JSON string: json.loads(bytes) decodes UTF-8
    # and raises UnicodeDecodeError (a ValueError), which the base loader did
    # not catch and crashed every request on.
    path.write_bytes(b'{"schema": "\xff"}')
    monkeypatch.setattr(psp, "_PATH", path)
    caplog.set_level(logging.ERROR)

    assert psp.load_pool_supplied_page_ids() == frozenset()
    assert psp.is_pool_supplied(LISTED_PAGE) is False
    assert any("ALERT pool-supplied-pages" in r.getMessage() for r in caplog.records)


def test_deeply_nested_list_fails_closed_with_alert(monkeypatch, tmp_path, caplog):
    import logging
    path = tmp_path / "pool-supplied-pages.v1.json"
    # 200k nested arrays raise RecursionError in CPython 3.14's json parser
    # (verified); base did not catch it and crashed every request on.
    path.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
    monkeypatch.setattr(psp, "_PATH", path)
    caplog.set_level(logging.ERROR)

    assert psp.load_pool_supplied_page_ids() == frozenset()
    assert psp.is_pool_supplied(LISTED_PAGE) is False
    assert any("ALERT pool-supplied-pages" in r.getMessage() for r in caplog.records)


def test_list_is_loaded_once_and_alerts_once_per_process(monkeypatch, tmp_path, caplog):
    import logging
    path = tmp_path / "pool-supplied-pages.v1.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(psp, "_PATH", path)
    psp.reset_pool_supplied_page_ids_cache()
    caplog.set_level(logging.ERROR)

    assert psp.load_pool_supplied_page_ids() == frozenset()
    assert psp.load_pool_supplied_page_ids() == frozenset()
    assert psp.is_pool_supplied(LISTED_PAGE) is False

    alerts = [r for r in caplog.records if "ALERT pool-supplied-pages" in r.getMessage()]
    assert len(alerts) == 1, [r.getMessage() for r in caplog.records]


# ── registration list check is authoritative even if the catalog drifts ─────
def _registration_unit_test(monkeypatch, page_id):
    """Drive _validate_production_selection with a crafted catalog whose image
    library is still required=True (the pre-fix dossier view), so the null
    sourceLibraryId decision is made by the registration list check itself."""
    from fastapi import HTTPException

    catalog = {
        "currentFormatCandidates": ["lyric-edits"],
        "formats": [{
            "formatId": "lyric-edits",
            "ingredients": [{
                "ingredientId": "image-library",
                "kind": "image_library",
                "required": True,
                "status": "missing",
                "binding": None,
                "options": [],
            }],
            "productionSelections": [{
                "catalogVersion": "sha256:unit",
                "providerId": None,
                "modelId": None,
                "promptModuleId": None,
                "referenceSetId": None,
                "sourceLibraryId": None,
            }],
        }],
    }
    monkeypatch.setattr(
        recipes, "build_dossier_ingredient_catalog",
        lambda *a, **k: catalog,
    )
    spec = {
        "schema": "dossier.recipe-spec.v3",
        "masterPages": {"pageId": page_id},
        "masterPagesHash": "sha256:" + "ab" * 32,
        "renderTreatment": {"stylePreset": None, "filters": {}, "captionStyle": {}},
        "demand": {"formatMix": {"lyric-edits": 1}},
        "production": {
            "catalogVersion": "sha256:unit",
            "providerId": None, "modelId": None, "promptModuleId": None,
            "referenceSetId": None, "sourceLibraryId": None,
            "variationValues": {}, "controls": {},
        },
    }
    try:
        recipes._validate_production_selection(
            spec, page_id=page_id, recipe_id="lyric-edits:master",
            recipe_version="v1", dossier_revision="r1",
            recipe_spec_hash="sha256:" + "cd" * 32,
        )
    except HTTPException as error:
        return error.status_code, error.detail
    return None, None


def test_registration_list_check_accepts_only_listed_pages(monkeypatch):
    listed_status, _ = _registration_unit_test(monkeypatch, LISTED_PAGE)
    assert listed_status is None

    unlisted_status, unlisted_detail = _registration_unit_test(
        monkeypatch, "acct:unlisted-page",
    )
    assert unlisted_status == 409
    assert unlisted_detail == "sourceLibraryId is required for sourced master DNA"



def test_listed_page_capability_advertises_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTENT_LAB_ADVERTISE_ZERO_REASONS", "1")
    from routers import control_plane as cp
    from services.json_store import atomic_save

    cp._jobs_snapshot = None
    cp._jobs_decode_pending.clear()
    cp._capabilities_cache.clear()
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    atomic_save(
        tmp_path / "jobs.json",
        {"jobs": {}, "version": 1, "byIdempotency": {}, "served": {}},
    )
    monkeypatch.setattr(
        cp, "_current_intent_for_capabilities",
        lambda _: ({"contentNiche": "lyric-edits", "contentEngine": "lyrics_slideshows"}, "h"),
    )
    spec = {
        "schema": "dossier.recipe-spec.v3",
        "masterPages": {"pageId": LISTED_PAGE},
        "masterPagesHash": "sha256:" + "ab" * 32,
        "renderTreatment": {"stylePreset": None, "filters": {}, "captionStyle": {}},
        "demand": {"formatMix": {"lyric-edits": 1}},
        "production": {"sourceLibraryId": None},
    }
    publication = {
        "recipeId": "lyric-edits:master",
        "engine": "lyrics_slideshows",
        "recipeVersion": "v1",
        "recipeSpecHash": "sha256:" + "cd" * 32,
        "recipeSpecCanonical": json.dumps(spec, sort_keys=True, separators=(",", ":")),
    }
    monkeypatch.setattr(cp, "list_registered_recipe_bindings", lambda *a: [("k", publication)])
    monkeypatch.setattr(cp, "load_engine_registry", lambda: ({}, "h"))
    monkeypatch.setattr(cp, "resolve_slideshow_recipe", lambda publication: None)
    monkeypatch.setattr(cp, "resolve_generation_recipe_with_reason", lambda publication: (None, None))
    monkeypatch.setattr(cp, "_dossier_source_recipe", lambda publication, refusal_reasons=None: None)
    monkeypatch.setattr(cp, "syzygy_runtime_available", lambda: True)

    response = cp.capabilities(x_rt_page_id=LISTED_PAGE, x_page_id=None)
    assert response["capabilities"] == [{
        "recipeId": "lyric-edits:master",
        "engine": "lyrics_slideshows",
        "recipeVersion": "v1",
        "maxQuantity": 0,
        "zeroReason": reasons.POOL_SUPPLIED,
    }]
