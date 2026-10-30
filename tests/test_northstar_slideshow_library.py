"""A North Star page's Syzygy slideshow library belongs to the page, not its handle.

Rising Tides pages address ``syzygy:{lane}:{handle slug}``. A page whose Master
Pages intent is owned outside Notion (``source: "northstar"``, registered by
``routers/control_plane_external_pages.py``) addresses
``syzygy:{lane}:external--{sha256(exact pageId)}`` instead, through the same
``slideshow_library_address`` the Dossier ingredients read and the job path
share. Registration refuses, by name, a subject any other roster page already
derives, so two pages never render from one picture pool.
"""

from __future__ import annotations

import hashlib
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import routers.control_plane as cp
import routers.control_plane_external_pages as external
import routers.control_plane_recipes as recipes
import services.control_plane_slideshows as slideshows
import services.dossier_ingredients as ingredients_module
import services.roster as roster
from services import json_store
from services.content_engine_registry import resolve_material_profile
from services.control_plane_slideshows import (
    SyzygyLibrary,
    SyzygyObject,
    library_subject,
    library_subject_holder,
    resolve_slideshow_recipe,
    slideshow_library_address,
)
from services.dossier_ingredients import (
    build_dossier_ingredient_catalog,
    catalog_selection_version,
)
from services.master_pages_contract import EXTERNAL_PAGE_SOURCES, intent_hash
from tests.master_pages_fixtures import bind_current_intent


TOKEN = "test-control-plane-token"
LANE_HEADER = "content-bucket-control-plane"
# The page id the control plane mints for a North Star workspace page.
NS_PAGE_ID = "ns-tt-6b3e9c1a-k7pq3m2x"
NS_SUBJECT = "external--" + hashlib.sha256(NS_PAGE_ID.encode()).hexdigest()
# The same handle as the Rising Tides fixture page in
# tests/test_control_plane_slideshow_execution.py, on purpose: a shared handle
# must not mean a shared library.
HANDLE = "holy.fumble"
HANDLE_SUBJECT = "holy-fumble"
# format slug -> (contentNiche, contentEngine, library lane), from
# recipes/content-engine-registry.v1.json and
# recipes/executors/syzygy-slideshow.v1.json.
FORMATS = {
    "meme-slideshow": ("meme", "sourced_slideshow", "meme"),
    "lyric-edits": ("lyric-edits", "lyrics_slideshows", "slideshow-webweaving"),
}
FORMAT = "meme-slideshow"
NICHE, ENGINE, LANE = FORMATS[FORMAT]
NS_LIBRARY_ID = f"syzygy:{LANE}:{NS_SUBJECT}"
HANDLE_LIBRARY_ID = f"syzygy:{LANE}:{HANDLE_SUBJECT}"
RECIPE_ID = f"{FORMAT}:master"
AUTH = {"Authorization": f"Bearer {TOKEN}", "X-RT-Lane": LANE_HEADER}


def northstar_intent(format_slug: str = FORMAT, **overrides) -> dict:
    """The intent the control plane writes for a North Star page on a format."""
    niche, engine, _ = FORMATS[format_slug]
    intent = {
        "schema": "master-pages.page-intent.v1",
        "pageId": NS_PAGE_ID,
        "handle": HANDLE,
        "group": "NORTH STAR",
        "groupLabel": None,
        "pageType": "Artist page",
        "accountType": "artist",
        "posterName": None,
        "status": "Active",
        "project": None,
        "tiktokUrl": None,
        "notionPageId": None,
        "source": "northstar",
        "contentNiche": niche,
        "contentEngine": engine,
        "automationMode": "Automation",
        "vaultUrl": f"https://shipstream.test/vault/{HANDLE}",
        "pipeline": None,
        "soundsReference": None,
        "archived": False,
    }
    intent.update(overrides)
    return intent


def _profile(format_slug: str, intent: dict):
    _, engine, _ = FORMATS[format_slug]
    profile = resolve_material_profile(
        {"recipeId": f"{format_slug}:master", "engine": engine},
        {"masterPages": intent, "demand": {"formatMix": {format_slug: 1.0}}},
    )
    assert profile is not None
    return profile


def _library(library_id: str, lane: str, subject: str, count: int = 6) -> SyzygyLibrary:
    objects = tuple(sorted(
        (
            SyzygyObject(
                key=f"{lane}/{subject}/photos/img-{index:02d}.jpg",
                bytes=2_000 + index,
                url=f"https://r2.example/{lane}/{subject}/photos/img-{index:02d}.jpg",
            )
            for index in range(count)
        ),
        key=lambda item: item.key,
    ))
    canonical = json.dumps(
        [[item.key, item.bytes] for item in objects],
        separators=(",", ":"), ensure_ascii=False,
    )
    return SyzygyLibrary(
        library_id=library_id,
        lane=lane,
        subject=subject,
        objects=objects,
        snapshot_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def _image_library(catalog: dict, format_slug: str) -> dict:
    entry = next(item for item in catalog["formats"] if item["formatId"] == format_slug)
    return next(
        item for item in entry["ingredients"] if item["ingredientId"] == "image-library"
    )


# ── Derivation ──────────────────────────────────────────────────────────────


def test_external_subjects_are_reserved_exact_page_hashes_and_ordinary_addresses_unchanged():
    assert EXTERNAL_PAGE_SOURCES == frozenset({"northstar"})
    assert external.EXTERNAL_PAGE_SOURCES is EXTERNAL_PAGE_SOURCES
    # North Star: the exact page identity, however the handle is spelled.
    assert library_subject(NS_PAGE_ID, HANDLE, "northstar") == NS_SUBJECT
    assert library_subject(NS_PAGE_ID, "some.other.handle", "northstar") == NS_SUBJECT
    assert library_subject(NS_PAGE_ID, None, "northstar") == NS_SUBJECT
    assert library_subject("NS.TT_6B3E9C1A:k7pq3m2x", HANDLE, "northstar") != NS_SUBJECT
    assert library_subject("ns-" + "x" * 126, HANDLE, "northstar") is None
    assert library_subject("n" * 128, HANDLE, "northstar") == "external--" + hashlib.sha256(("n" * 128).encode()).hexdigest()
    assert library_subject("acct:future", NS_SUBJECT, "notion") != NS_SUBJECT
    assert library_subject("...", HANDLE, "northstar") is None
    assert library_subject(None, HANDLE, "northstar") is None
    # Rising Tides (Notion, legacy, pipeline): the handle slug, never the id.
    assert library_subject("tt-holy-fumble", HANDLE, "notion") == HANDLE_SUBJECT
    assert library_subject("acct:holy-fumble", HANDLE, "notion") == HANDLE_SUBJECT
    assert library_subject("tt-holy-fumble", HANDLE, None) == HANDLE_SUBJECT
    assert library_subject("tt-holy-fumble", "Holy..Fumble_2", "notion") == "holy-fumble-2"
    assert library_subject("tt-holy-fumble", None, "notion") is None
    # An unknown owner is not external; it keeps the handle derivation.
    assert library_subject(NS_PAGE_ID, HANDLE, "postiz") == HANDLE_SUBJECT


def test_subject_holder_names_the_other_page_that_derives_it():
    pages = [
        ("acct:holy-fumble", "holy.fumble", "notion"),
        ("acct:ns-tt-6b3e9c1a-k7pq3m2x", "ns.tt.6b3e9c1a.k7pq3m2x", "notion"),
        ("ns-tt-ffffffff-aaaaaaaa", "another.artist", "northstar"),
    ]
    assert library_subject_holder(NS_SUBJECT, pages) is None
    assert library_subject_holder(library_subject("ns-tt-ffffffff-aaaaaaaa", "", "northstar"), pages) == "ns-tt-ffffffff-aaaaaaaa"
    assert library_subject_holder(HANDLE_SUBJECT, pages) == "acct:holy-fumble"
    assert library_subject_holder("another-artist", pages) is None
    assert library_subject_holder(None, pages) is None


@pytest.mark.parametrize("format_slug", sorted(FORMATS))
def test_each_commissioned_format_addresses_the_page_subject_on_its_lane(format_slug):
    _, _, lane = FORMATS[format_slug]
    intent = northstar_intent(format_slug)
    profile = _profile(format_slug, intent)
    assert slideshow_library_address(profile, intent) == (
        f"syzygy:{lane}:{NS_SUBJECT}", lane, NS_SUBJECT,
    )
    # Only the owner flips the derivation: the same page as a Notion row
    # would address its handle library.
    notion = {**intent, "source": "notion", "notionPageId": "2f4b1c40aaaabbbbccccddddeeeeffff"}
    assert slideshow_library_address(profile, notion) == (
        f"syzygy:{lane}:{HANDLE_SUBJECT}", lane, HANDLE_SUBJECT,
    )
    # A page id that cannot slug has no library rather than a borrowed one.
    assert slideshow_library_address(profile, {**intent, "pageId": "..."}) is None


# ── Dossier ingredients read ────────────────────────────────────────────────


def test_dossier_ingredients_read_the_page_library_and_never_the_handle_library(monkeypatch):
    fetched: list[tuple[str, str, str]] = []

    def fake_fetch(library_id, lane, subject, *, timeout=15.0):
        fetched.append((library_id, lane, subject))
        return _library(library_id, lane, subject)

    monkeypatch.setattr(slideshows, "_fetch_library", fake_fetch)
    intent = northstar_intent()
    catalog = build_dossier_ingredient_catalog(NS_PAGE_ID, intent, intent_hash(intent))

    assert fetched == [(NS_LIBRARY_ID, LANE, NS_SUBJECT)]
    ingredient = _image_library(catalog, FORMAT)
    assert ingredient["status"] == "bound"
    assert [option["libraryId"] for option in ingredient["options"]] == [NS_LIBRARY_ID]
    assert ingredient["binding"]["subject"] == NS_SUBJECT
    assert ingredient["binding"]["lane"] == LANE
    assert ingredient["binding"]["mediaCount"] == 6
    assert HANDLE_LIBRARY_ID not in json.dumps(catalog)
    # The page's own library id is the only selectable source library.
    assert catalog_selection_version(catalog, FORMAT, {
        "providerId": None, "modelId": None, "promptModuleId": None,
        "referenceSetId": None, "sourceLibraryId": NS_LIBRARY_ID,
    }) is not None
    assert catalog_selection_version(catalog, FORMAT, {
        "providerId": None, "modelId": None, "promptModuleId": None,
        "referenceSetId": None, "sourceLibraryId": HANDLE_LIBRARY_ID,
    }) is None


# ── Jobs ────────────────────────────────────────────────────────────────────


def publication(monkeypatch, *, source_library_id: str = NS_LIBRARY_ID) -> dict:
    library = _library(NS_LIBRARY_ID, LANE, NS_SUBJECT)
    monkeypatch.setattr(
        ingredients_module, "load_syzygy_library", lambda *_args, **_kwargs: library,
    )
    intent = northstar_intent()
    revision = intent_hash(intent)
    catalog = build_dossier_ingredient_catalog(NS_PAGE_ID, intent, revision)
    production = {
        "catalogVersion": "",
        "providerId": None,
        "modelId": None,
        "promptModuleId": None,
        "referenceSetId": None,
        "sourceLibraryId": NS_LIBRARY_ID,
        "variationValues": {},
        "controls": {},
    }
    version = catalog_selection_version(catalog, FORMAT, production)
    assert version is not None
    production["catalogVersion"] = version
    production["sourceLibraryId"] = source_library_id
    recipe_spec = {
        "schema": "dossier.recipe-spec.v4",
        "masterPages": intent,
        "masterPagesHash": revision,
        "production": production,
        "renderTreatment": {
            "stylePreset": None,
            "filters": {},
            "captionStyle": {},
            "clipSpeed": 1.0,
            "clipCrop": {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5},
        },
        "demand": {"formatMix": {FORMAT: 1.0}},
        "captionDiscipline": {
            "captionSet": "meme",
            "register": ["relatable_meme"],
            "slingshotShare": None,
        },
    }
    canonical = json.dumps(recipe_spec, sort_keys=True, separators=(",", ":"))
    return {
        "schema": recipes.REQUEST_SCHEMA,
        "pageId": NS_PAGE_ID,
        "lane": recipes.LANE,
        "recipeId": RECIPE_ID,
        "engine": ENGINE,
        "recipeVersion": "dossier-northstar0001",
        "dossierRevision": "rev-northstar",
        "recipeSpecHash": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
        "recipeSpecCanonical": canonical,
    }


def headers(idempotency: str) -> dict:
    return {**AUTH, "X-RT-Page-Id": NS_PAGE_ID, "Idempotency-Key": idempotency}


def job_body(payload: dict, quantity: int = 2) -> dict:
    spec = json.loads(payload["recipeSpecCanonical"])
    return {
        "pageId": NS_PAGE_ID,
        "lane": recipes.LANE,
        "engine": payload["engine"],
        "lockedRecipeId": payload["recipeId"],
        "recipeVersion": payload["recipeVersion"],
        "quantity": quantity,
        "constraints": {},
        "sourceIsolation": {"partitionKey": f"page:{NS_PAGE_ID}"},
        "policyHash": "sha256:policy",
        "masterPages": spec["masterPages"],
        "masterPagesHash": spec["masterPagesHash"],
    }


@pytest.fixture
def ns_lab(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("CONTENT_LAB_RECIPE_ROOT", str(tmp_path / "recipes"))
    monkeypatch.setenv("CONTENT_LAB_GENERATION_MODE", "ready")
    monkeypatch.setenv("SYZYGY_API_URL", "https://syzygy.example")
    monkeypatch.setenv("SYZYGY_API_KEY", "syz_test_key")
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    monkeypatch.setattr(cp, "_generation_root", lambda: tmp_path / "generated")
    library = _library(NS_LIBRARY_ID, LANE, NS_SUBJECT)
    monkeypatch.setattr(cp, "load_recipe_library", lambda *_args, **_kwargs: library)
    monkeypatch.setattr(cp, "load_syzygy_library", lambda *_args, **_kwargs: library)
    intent = northstar_intent()
    bind_current_intent(monkeypatch, cp, intent, intent_hash(intent))
    started: list = []
    monkeypatch.setattr(cp, "_start_syzygy_slideshow", started.append)
    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    app.include_router(recipes.router, prefix="/api/control-plane")
    return TestClient(app), started, library


def test_jobs_reserve_from_the_page_library_under_its_own_keys(ns_lab, monkeypatch):
    client, started, library = ns_lab
    payload = publication(monkeypatch)
    registered = client.post(
        "/api/control-plane/v1/recipes", json=payload, headers=headers("ns-register-0001"),
    )
    assert registered.status_code == 200, registered.json()
    recipe = resolve_slideshow_recipe(payload)
    assert recipe is not None
    assert (recipe.library_id, recipe.lane, recipe.subject) == (NS_LIBRARY_ID, LANE, NS_SUBJECT)

    capabilities = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": NS_PAGE_ID, "Authorization": f"Bearer {TOKEN}"},
    ).json()["capabilities"]
    row = next(item for item in capabilities if item["recipeId"] == RECIPE_ID)
    assert row["engine"] == ENGINE
    assert row["maxQuantity"] > 0

    created = client.post(
        "/api/control-plane/v1/jobs", json=job_body(payload), headers=headers("ns-job-0001"),
    )
    assert created.status_code == 200, created.json()
    job = cp._load_jobs()["jobs"][created.json()["jobId"]]
    assert job["sourceKind"] == "syzygy_slideshow"
    assert job["sourceLibraryId"] == NS_LIBRARY_ID
    assert job["librarySnapshotHash"] == library.snapshot_hash
    assert len(job["slideshowPlan"]) == 2
    for plan in job["slideshowPlan"]:
        assert plan["mediaKeys"]
        assert all(key.startswith(f"{LANE}/{NS_SUBJECT}/photos/") for key in plan["mediaKeys"])
        assert not any(f"/{HANDLE_SUBJECT}/" in key for key in plan["mediaKeys"])
    assert len(started) == 1


def test_a_recipe_bound_to_the_handle_library_is_refused_for_an_external_page(ns_lab, monkeypatch):
    client, started, _ = ns_lab
    payload = publication(monkeypatch, source_library_id=HANDLE_LIBRARY_ID)
    assert resolve_slideshow_recipe(payload, require_runtime=False) is None
    registered = client.post(
        "/api/control-plane/v1/recipes", json=payload, headers=headers("ns-register-handle"),
    )
    assert registered.status_code == 409, registered.json()
    created = client.post(
        "/api/control-plane/v1/jobs", json=job_body(payload), headers=headers("ns-job-handle"),
    )
    assert created.status_code != 200
    assert started == []


# ── Registration refuses a shared subject by name ───────────────────────────


NOTION_ROW = {
    "integration_id": "acct:holy-fumble",
    "name": HANDLE,
    "provider": "tiktok",
    "group": "WARNER",
    "group_label": "Warner UGC",
    "page_type": "UGC theme page",
    "account_type": "ugc",
    "poster_name": "mon",
    "status": "Active",
    "tiktok_url": f"https://www.tiktok.com/@{HANDLE}",
    "notion_page_id": "2f4b1c40aaaabbbbccccddddeeeeffff",
    "source": "notion",
    "content_niche": NICHE,
    "content_engine": ENGINE,
    "automation_mode": "Automation",
    "vault_url": f"https://shipstream.test/vault/{HANDLE}",
    "archived": False,
}


@pytest.fixture
def roster_lab(monkeypatch, tmp_path):
    monkeypatch.setattr(roster, "ROSTER_PATH", tmp_path / "page_roster.json")
    monkeypatch.setattr(json_store, "_LOCKS", {})
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    app.include_router(external.router, prefix="/api/control-plane")
    return TestClient(app)


def _seed(*rows):
    roster.save_roster({
        "version": 1,
        "pages": {row["integration_id"]: dict(row) for row in rows},
    })


def _roster_bytes():
    path = roster.ROSTER_PATH
    return path.read_bytes() if path.exists() else None


def _register(client, intent):
    return client.put(
        f"/api/control-plane/v1/external-pages/{intent['pageId']}", headers=AUTH, json=intent,
    )


def test_registration_keeps_external_subject_disjoint_from_existing_notion_handle(roster_lab):
    _seed({**NOTION_ROW, "integration_id": "acct:future-collision", "name": NS_PAGE_ID})
    registered = _register(roster_lab, northstar_intent())
    assert registered.status_code == 200
    assert library_subject(NS_PAGE_ID, HANDLE, "northstar") != library_subject(
        "acct:future-collision", NS_PAGE_ID, "notion")


def test_case_and_punctuation_distinct_external_ids_get_distinct_pools(roster_lab):
    assert _register(roster_lab, northstar_intent()).status_code == 200
    twin = northstar_intent(pageId="NS.TT_6B3E9C1A:k7pq3m2x", handle="other.artist",
                           vaultUrl="https://shipstream.test/vault/other.artist")
    assert _register(roster_lab, twin).status_code == 200
    assert library_subject(twin["pageId"], twin["handle"], "northstar") != NS_SUBJECT


def test_a_handle_shared_with_a_rising_tides_page_is_not_a_shared_library(roster_lab):
    # Rising Tides owns holy.fumble -> syzygy:meme:holy-fumble. A North Star
    # page whose handle slugs alike takes its own subject, so it registers.
    _seed(NOTION_ROW)
    intent = northstar_intent(handle="holy_fumble", vaultUrl="https://shipstream.test/vault/holy_fumble")
    created = _register(roster_lab, intent)
    assert created.status_code == 200, created.json()
    row = roster.get_page(NS_PAGE_ID)
    assert library_subject(NS_PAGE_ID, row["name"], row["source"]) == NS_SUBJECT
    assert library_subject("acct:holy-fumble", NOTION_ROW["name"], "notion") == HANDLE_SUBJECT
    resolved, _ = cp._current_master_pages_intent(NS_PAGE_ID)
    profile = _profile(FORMAT, resolved)
    assert slideshow_library_address(profile, resolved) == (NS_LIBRARY_ID, LANE, NS_SUBJECT)


def test_future_notion_row_cannot_claim_external_pool(roster_lab):
    assert _register(roster_lab, northstar_intent()).status_code == 200
    # Notion can legitimately add a handle after external registration. Its
    # ordinary slug must never alias the existing external page's pool.
    _seed({**NOTION_ROW, 'integration_id': 'acct:future-notion', 'name': NS_PAGE_ID})
    assert library_subject(NS_PAGE_ID, HANDLE, 'northstar') != library_subject(
        'acct:future-notion', NS_PAGE_ID, 'notion')
