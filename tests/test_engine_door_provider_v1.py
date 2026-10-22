"""Provider-side engine-door contract samples (Content Lab, engine-door.v1).

Every route the Control Plane Worker calls is driven through the REAL FastAPI
routers via TestClient, reusing the hermetic setups already proven in
``test_control_plane_slideshow_execution.py``, ``test_control_plane_capability_auth.py``
and ``test_control_plane_dossier_execution.py``. Nothing here reaches the
network or a real Syzygy: the Syzygy library read, every async runner, the
ShipStream manifest fetch and the visual-admission sweep are all stubbed, and
the offline guard in ``tests/conftest.py`` would refuse any stray socket call.

Each test asserts the HTTP status and the top-level keys of the exact JSON
reply, then records that reply under ``tests/fixtures/engine_door_v1/`` as a
deterministic provider sample (fixed job ids via a deterministic ``secrets``
shim; fixed visual-admission clock) for the Worker schema suite to validate.

Formats covered end-to-end where the route depends on format:
  * Syzygy:       ugc-slideshow  (sourced_slideshow)
  * non-Syzygy:   pov-night-core (sourced_video, via the source-imports intake)
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.control_plane as cp
import routers.control_plane_recipes as recipes
import routers.control_plane_dossier as dossier
import routers.control_plane_source_libraries as source_router
import services.control_plane_source_libraries as source_libraries
import services.dossier_ingredients as ingredients_module
import services.roster as roster
import services.visual_admission as visual_admission
from services.control_plane_slideshows import SyzygyLibrary, SyzygyObject
from services.control_plane_source_imports import SOURCE_IMPORT_SCHEMA
from services.dossier_ingredients import (
    build_dossier_ingredient_catalog,
    catalog_selection_version,
)
from tests.master_pages_fixtures import bind_current_intent, master_pages


TOKEN = "test-control-plane-token"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "engine_door_v1"

# ── Ugc-slideshow (Syzygy) identity ──────────────────────────────────────
UGC_PAGE_ID = "acct:culture-ugc-test"
UGC_HANDLE = "French.Core"
UGC_NICHE = "slideshow"
UGC_ENGINE = "sourced_slideshow"
UGC_FORMAT = "ugc-slideshow"
UGC_RECIPE_ID = "ugc-slideshow:master"
UGC_LIBRARY_ID = "syzygy:ugc:french-core"
UGC_RECIPE_VERSION = "dossier-ugc00000000"

# ── pov-night-core (non-Syzygy) identity ─────────────────────────────────
POV_PAGE_ID = "acct:operator:night-walks"
POV_SOURCE_URL = "https://cdn.example.com/source/night-walk.mp4"

# Deterministic artifact bytes behind download/thumbnail/visual-admission.
VIDEO_BYTES = b"engine-door-provider-v1-sample-video-bytes"
THUMB_BYTES = b"engine-door-provider-v1-sample-thumbnail"
FIXED_NOW = _dt.datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=_dt.timezone.utc)


class _DeterministicSecrets:
    """Replaces cp._secrets so job ids and download tokens are reproducible."""

    def __init__(self):
        self._counter = 0

    def _next(self) -> int:
        self._counter += 1
        return self._counter

    def token_hex(self, nbytes: int) -> str:
        return format(self._next(), "x").rjust(nbytes * 2, "0")

    def token_urlsafe(self, nbytes: int) -> str:
        return "u" * nbytes

    @staticmethod
    def compare_digest(a, b) -> bool:
        import secrets

        return secrets.compare_digest(a, b)


class _FixedDatetime:
    def now(self, tz=None):
        return FIXED_NOW


def _write_fixture(route: str, case: str, payload) -> Path:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    target = FIXTURES / f"{route}__{case}.json"
    target.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    return target


def _ugc_intent():
    return master_pages(
        UGC_PAGE_ID,
        handle=UGC_HANDLE,
        content_niche=UGC_NICHE,
        content_engine=UGC_ENGINE,
        vault_url="https://shipstream.test/vault/french.core",
    )


def _ugc_library() -> SyzygyLibrary:
    keys = ["01-0058", "02-0051", "03-0052"]
    objects = tuple(
        SyzygyObject(
            key=f"ugc/french-core/photos/img-{suffix}.jpg",
            bytes=1_000 + index,
            url=f"https://r2.example/ugc/french-core/photos/img-{suffix}.jpg",
        )
        for index, suffix in enumerate(keys)
    )
    canonical = json.dumps(
        [[item.key, item.bytes] for item in objects],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return SyzygyLibrary(
        library_id=UGC_LIBRARY_ID,
        lane="ugc",
        subject="french-core",
        objects=objects,
        snapshot_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def _ugc_publication(monkeypatch, library, recipe_version=UGC_RECIPE_VERSION):
    intent, revision = _ugc_intent()
    monkeypatch.setattr(
        ingredients_module, "load_syzygy_library", lambda *a, **k: library,
    )
    catalog = build_dossier_ingredient_catalog(UGC_PAGE_ID, intent, revision)
    production = {
        "catalogVersion": "",
        "providerId": None,
        "modelId": None,
        "promptModuleId": None,
        "referenceSetId": None,
        "sourceLibraryId": UGC_LIBRARY_ID,
        "variationValues": {},
        "controls": {},
    }
    version = catalog_selection_version(catalog, UGC_FORMAT, production)
    assert version is not None
    production["catalogVersion"] = version
    spec = {
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
        "captionDiscipline": {
            "captionSet": "ugc",
            "register": ["relatable_meme"],
            "slingshotShare": None,
        },
        "demand": {"formatMix": {UGC_FORMAT: 1.0}},
    }
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return {
        "schema": recipes.REQUEST_SCHEMA,
        "pageId": UGC_PAGE_ID,
        "lane": recipes.LANE,
        "recipeId": UGC_RECIPE_ID,
        "engine": UGC_ENGINE,
        "recipeVersion": recipe_version,
        "dossierRevision": "rev-ugc",
        "recipeSpecHash": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
        "recipeSpecCanonical": canonical,
    }


def _ugc_headers(idempotency="ugc-job-0001"):
    return {
        "Authorization": f"Bearer {TOKEN}",
        "X-RT-Lane": recipes.LANE,
        "X-RT-Page-Id": UGC_PAGE_ID,
        "Idempotency-Key": idempotency,
    }


def _ugc_job_body(payload, quantity=1):
    spec = json.loads(payload["recipeSpecCanonical"])
    return {
        "pageId": UGC_PAGE_ID,
        "lane": recipes.LANE,
        "engine": payload["engine"],
        "lockedRecipeId": payload["recipeId"],
        "recipeVersion": payload["recipeVersion"],
        "quantity": quantity,
        "constraints": {},
        "sourceIsolation": {"partitionKey": f"page:{UGC_PAGE_ID}"},
        "policyHash": "sha256:policy",
        "masterPages": spec["masterPages"],
        "masterPagesHash": spec["masterPagesHash"],
    }


def _register_ugc(client, monkeypatch, library):
    payload = _ugc_publication(monkeypatch, library)
    intent, revision = _ugc_intent()
    bind_current_intent(monkeypatch, cp, intent, revision)
    registered = client.post(
        "/api/control-plane/v1/recipes",
        json=payload,
        headers=_ugc_headers("ugc-register-0001"),
    )
    assert registered.status_code == 200, registered.text
    return payload


def _create_ugc_job(client, payload, idempotency):
    created = client.post(
        "/api/control-plane/v1/jobs",
        json=_ugc_job_body(payload),
        headers=_ugc_headers(idempotency),
    )
    assert created.status_code == 200, created.text
    return created.json()


def _complete_ugc_job(job_id):
    """Write deterministic artifact bytes and mark the job completed."""
    job = cp._load_jobs()["jobs"][job_id]
    token = job["token"]
    root = Path(job["artifactRoot"])
    treated = root / "treated"
    treated.mkdir(parents=True, exist_ok=True)
    thumbs = root / "thumbnails"
    thumbs.mkdir(parents=True, exist_ok=True)
    video_path = treated / "slideshow-0000.mp4"
    video_path.write_bytes(VIDEO_BYTES)
    thumb_path = thumbs / "0000.jpg"
    thumb_path.write_bytes(THUMB_BYTES)
    video_sha = hashlib.sha256(VIDEO_BYTES).hexdigest()
    thumb_sha = hashlib.sha256(THUMB_BYTES).hexdigest()
    manifest = {
        "path": "treated/slideshow-0000.mp4",
        "name": "slideshow-0000.mp4",
        "sha256": video_sha,
        "bytes": len(VIDEO_BYTES),
        "source": {"kind": "syzygy_slideshow", "planSignature": "a" * 64},
        "thumbnail": {
            "path": "thumbnails/0000.jpg",
            "name": "0000.jpg",
            "sha256": thumb_sha,
            "bytes": len(THUMB_BYTES),
        },
    }
    cp._update_job(job_id, status="completed", progress=100, clips=[manifest])
    return manifest, token


@pytest.fixture
def door(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.delenv("APP_API_KEY", raising=False)
    monkeypatch.setenv("CONTENT_LAB_RECIPE_ROOT", str(tmp_path / "recipes"))
    monkeypatch.setenv("CONTENT_LAB_GENERATION_MODE", "ready")
    monkeypatch.setenv("SYZYGY_API_URL", "https://syzygy.example")
    monkeypatch.setenv("SYZYGY_API_KEY", "syz_test_key")
    monkeypatch.setenv("CONTENT_LAB_CONTROL_PLANE_ORIGIN", "https://control.example.com")
    monkeypatch.setenv("CONTENT_LAB_PUBLIC_ORIGIN", "https://content-lab.example.com")
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    monkeypatch.setattr(cp, "_generation_root", lambda: tmp_path / "generated")
    monkeypatch.setattr(cp, "_secrets", _DeterministicSecrets())
    monkeypatch.setattr(roster, "ROSTER_PATH", tmp_path / "page_roster.json")
    from services import json_store

    monkeypatch.setattr(json_store, "_LOCKS", {})

    started: list[str] = []
    monkeypatch.setattr(cp, "_start_syzygy_slideshow", started.append)
    monkeypatch.setattr(cp, "_start_dossier_generation", started.append)
    monkeypatch.setattr(cp, "_start_dossier_source", started.append)
    monkeypatch.setattr(cp, "_start_truck_master_recovery", started.append)
    monkeypatch.setattr(cp, "_start_page_source_import", started.append)

    sweeps: list[tuple] = []
    monkeypatch.setattr(
        cp, "_submit_visual_sweep", lambda *a: sweeps.append(a) or None,
    )
    monkeypatch.setattr(visual_admission, "datetime", _FixedDatetime())

    library = _ugc_library()
    monkeypatch.setattr(cp, "load_recipe_library", lambda *a, **k: library)
    monkeypatch.setattr(cp, "load_syzygy_library", lambda *a, **k: library)
    monkeypatch.setattr(
        ingredients_module, "load_syzygy_library", lambda *a, **k: library,
    )
    monkeypatch.setattr(cp, "validate_source_url", lambda value: value)

    import services.shipstream_source_manifest as source_manifest

    def _offline_manifest(_url):
        raise source_manifest.ShipStreamSourceUnavailable("ShipStream vault is offline in tests")

    monkeypatch.setattr(source_manifest, "_fetch_manifest", _offline_manifest)

    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    app.include_router(recipes.router, prefix="/api/control-plane")
    app.include_router(dossier.router, prefix="/api/control-plane")
    app.include_router(source_router.router, prefix="/api/control-plane")
    client = TestClient(app)
    return SimpleNamespace(
        client=client, tmp_path=tmp_path, started=started, sweeps=sweeps,
        library=library,
    )


# ── capabilities ─────────────────────────────────────────────────────────

def test_capabilities_ugc_slideshow(door, monkeypatch):
    _register_ugc(door.client, monkeypatch, door.library)
    response = door.client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": UGC_PAGE_ID, "Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "content-lab.response.v1"
    assert isinstance(body["capabilities"], list)
    row = next(item for item in body["capabilities"] if item["recipeId"] == UGC_RECIPE_ID)
    assert row["engine"] == UGC_ENGINE
    assert row["maxQuantity"] == 1
    _write_fixture("capabilities", "ugc-slideshow", body)


def test_capabilities_pov_night_core(door, monkeypatch):
    intent, revision = master_pages(
        POV_PAGE_ID,
        handle="night.walks",
        content_niche="POV — Night Core",
        content_engine="sourced_video",
        vault_url="https://shipstream.test/vault/night.walks",
    )
    intent["notionPageId"] = "notion-night-walks"
    from services.master_pages_contract import intent_hash

    revision = intent_hash(intent)
    bind_current_intent(monkeypatch, cp, intent, revision)
    response = door.client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": POV_PAGE_ID, "Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "content-lab.response.v1"
    rows = body["capabilities"]
    assert any(item["recipeId"] == "pov-night-core:master" for item in rows)
    _write_fixture("capabilities", "pov-night-core", body)


# ── format-contracts ─────────────────────────────────────────────────────

def test_format_contracts(door):
    response = door.client.get(
        "/api/control-plane/v1/format-contracts",
        headers={"X-RT-Lane": recipes.LANE, "Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "content-lab.format-contract-status.v1"
    assert body["contractsRegistryVersion"].startswith("sha256:")
    assert body["engineRegistryVersion"].startswith("sha256:")
    slugs = {row["formatSlug"] for row in body["formats"]}
    assert {"ugc-slideshow", "pov-night-core"} <= slugs
    _write_fixture("format-contracts", "read", body)


# ── roster / roster refresh ──────────────────────────────────────────────

ROSTER_ROW = {
    "name": "roster.page",
    "provider": "tiktok",
    "group": "WARNER",
    "group_label": "Warner UGC",
    "page_type": "UGC theme page",
    "account_type": "ugc",
    "poster_name": "mon",
    "status": "Active",
    "project": "trucks",
    "tiktok_url": "https://www.tiktok.com/@roster.page",
    "notion_page_id": "2f4b1c40-aaaa-bbbb-cccc-ddddeeeeffff",
    "source": "notion",
    "content_niche": "TRUCK",
    "content_engine": "ai_video",
    "automation_mode": "Automation",
    "vault_url": "https://shipstream.test/vault/roster.page",
    "pipeline": "Flow Stage",
    "sounds_reference": None,
    "archived": False,
}


def _seed_roster():
    data = {
        "version": 1,
        "pages": {"acct:roster-page": {
            "integration_id": "acct:roster-page", **ROSTER_ROW,
        }},
    }
    roster.save_roster(data)
    os.utime(roster.ROSTER_PATH, (FIXED_NOW.timestamp(), FIXED_NOW.timestamp()))


def _roster_headers():
    return {"X-RT-Lane": "content-bucket-control-plane", "Authorization": f"Bearer {TOKEN}"}


def test_roster(door):
    _seed_roster()
    response = door.client.get("/api/control-plane/v1/roster", headers=_roster_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "content-lab.response.v1"
    assert body["snapshotVersion"].startswith("r")
    assert body["projectionHash"].startswith("sha256:")
    assert body["pages"][0]["pageId"] == "acct:roster-page"
    assert "password" not in json.dumps(body)
    _write_fixture("roster", "snapshot", body)


def test_roster_refresh(door, monkeypatch):
    _seed_roster()
    import services.notion_pages as notion_pages

    monkeypatch.setattr(notion_pages, "is_configured", lambda: True)

    async def refresh():
        return {"added": 3, "updated": 19, "total_in_notion": 22, "errors": [], "pages": []}

    monkeypatch.setattr(notion_pages, "sync_into_roster", refresh)
    response = door.client.post(
        "/api/control-plane/v1/roster/refresh", headers=_roster_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "content-lab.response.v1"
    assert body["added"] == 3
    assert body["projectedCount"] == 1
    assert body["complete"] is True
    _write_fixture("roster", "refresh", body)


# ── source-imports (pov-night-core intake) ───────────────────────────────

def _pov_import_body(intent, revision):
    return {
        "schema": SOURCE_IMPORT_SCHEMA,
        "pageId": POV_PAGE_ID,
        "format": "pov-night-core",
        "sourceUrl": POV_SOURCE_URL,
        "masterPages": intent,
        "masterPagesHash": revision,
    }


def test_source_imports_pov_night_core(door, monkeypatch):
    intent, revision = master_pages(
        POV_PAGE_ID,
        handle="night.walks",
        content_niche="POV — Night Core",
        content_engine="sourced_video",
        vault_url="https://shipstream.test/vault/night.walks",
    )
    intent["notionPageId"] = "notion-night-walks"
    from services.master_pages_contract import intent_hash

    revision = intent_hash(intent)
    bind_current_intent(monkeypatch, cp, intent, revision)
    response = door.client.post(
        "/api/control-plane/v1/source-imports",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-RT-Page-Id": POV_PAGE_ID,
            "X-RT-Lane": "content-bucket-control-plane",
            "Idempotency-Key": "source:intake:night-walks:001",
        },
        json=_pov_import_body(intent, revision),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "content-lab.response.v1"
    assert body["status"] == "queued"
    assert door.started == [body["jobId"]]
    _write_fixture("source-imports", "pov-night-core", body)


# ── jobs (create / status / artifacts / download / thumbnail / visual) ──

def test_jobs_create_ugc_slideshow(door, monkeypatch):
    payload = _register_ugc(door.client, monkeypatch, door.library)
    created = _create_ugc_job(door.client, payload, "ugc-create-0001")
    assert created["schema"] == "content-lab.response.v1"
    assert created["status"] == "queued"
    assert created["jobId"] == "cpl-0000000000000001"
    assert door.started == [created["jobId"]]
    _write_fixture("jobs", "create-ugc-slideshow", created)


def test_jobs_status_ugc_slideshow(door, monkeypatch):
    payload = _register_ugc(door.client, monkeypatch, door.library)
    created = _create_ugc_job(door.client, payload, "ugc-status-0001")
    job_id = created["jobId"]
    response = door.client.get(
        f"/api/control-plane/v1/jobs/{job_id}",
        headers={"X-RT-Page-Id": UGC_PAGE_ID, "Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "content-lab.response.v1"
    assert body["jobId"] == job_id
    assert body["status"] == "queued"
    assert body["progress"] == 0
    _write_fixture("jobs", "status-ugc-slideshow", body)


def test_jobs_artifacts_ugc_slideshow(door, monkeypatch):
    payload = _register_ugc(door.client, monkeypatch, door.library)
    created = _create_ugc_job(door.client, payload, "ugc-artifacts-0001")
    job_id = created["jobId"]
    manifest, _token = _complete_ugc_job(job_id)
    response = door.client.get(
        f"/api/control-plane/v1/jobs/{job_id}/artifacts",
        headers={"X-RT-Page-Id": UGC_PAGE_ID, "Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "content-lab.response.v1"
    assert body["jobId"] == job_id
    assert len(body["artifacts"]) == 1
    artifact = body["artifacts"][0]
    assert artifact["type"] == "video"
    assert artifact["sha256"] == manifest["sha256"]
    assert artifact["bytes"] == manifest["bytes"]
    assert artifact["thumbnail"]["type"] == "image/jpeg"
    _write_fixture("jobs", "artifacts-ugc-slideshow", body)


def test_jobs_download_ugc_slideshow(door, monkeypatch):
    payload = _register_ugc(door.client, monkeypatch, door.library)
    created = _create_ugc_job(door.client, payload, "ugc-download-0001")
    job_id = created["jobId"]
    manifest, token = _complete_ugc_job(job_id)
    response = door.client.get(
        f"/api/control-plane/v1/jobs/{job_id}/download/0?token={token}",
    )
    assert response.status_code == 200
    assert response.content == VIDEO_BYTES
    assert response.headers["content-type"] == "video/mp4"
    envelope = {
        "status": 200,
        "contentType": "video/mp4",
        "sha256": manifest["sha256"],
        "bytes": len(VIDEO_BYTES),
    }
    _write_fixture("jobs", "download-ugc-slideshow", envelope)


def test_jobs_thumbnail_ugc_slideshow(door, monkeypatch):
    payload = _register_ugc(door.client, monkeypatch, door.library)
    created = _create_ugc_job(door.client, payload, "ugc-thumbnail-0001")
    job_id = created["jobId"]
    manifest, token = _complete_ugc_job(job_id)
    response = door.client.get(
        f"/api/control-plane/v1/jobs/{job_id}/thumbnail/0?token={token}",
    )
    assert response.status_code == 200
    assert response.content == THUMB_BYTES
    assert response.headers["content-type"] == "image/jpeg"
    envelope = {
        "status": 200,
        "contentType": "image/jpeg",
        "sha256": manifest["thumbnail"]["sha256"],
        "bytes": len(THUMB_BYTES),
    }
    _write_fixture("jobs", "thumbnail-ugc-slideshow", envelope)


def test_jobs_visual_admission_ugc_slideshow(door, monkeypatch):
    payload = _register_ugc(door.client, monkeypatch, door.library)
    created = _create_ugc_job(door.client, payload, "ugc-visual-0001")
    job_id = created["jobId"]
    manifest, _token = _complete_ugc_job(job_id)
    response = door.client.post(
        f"/api/control-plane/v1/jobs/{job_id}/visual-admission/0",
        headers={"X-RT-Page-Id": UGC_PAGE_ID, "Authorization": f"Bearer {TOKEN}"},
        json={"sha256": manifest["sha256"], "bytes": manifest["bytes"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "content-lab.visual-admission.v1"
    assert body["verdict"] == "unavailable"
    assert body["reason"] == "scan_pending"
    assert body["sha256"] == manifest["sha256"]
    assert body["bytes"] == manifest["bytes"]
    assert len(door.sweeps) == 1
    _write_fixture("jobs", "visual-admission-ugc-slideshow", body)


# ── recipes / dossier-ingredients ────────────────────────────────────────

def test_recipes(door, monkeypatch):
    payload = _ugc_publication(monkeypatch, door.library)
    intent, revision = _ugc_intent()
    bind_current_intent(monkeypatch, cp, intent, revision)
    response = door.client.post(
        "/api/control-plane/v1/recipes",
        json=payload,
        headers=_ugc_headers("ugc-register-recipe"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "content-lab.response.v1"
    assert body["recipeId"] == UGC_RECIPE_ID
    assert body["engine"] == UGC_ENGINE
    assert body["status"] == "registered"
    _write_fixture("recipes", "register-ugc-slideshow", body)


def test_dossier_ingredients_ugc_slideshow(door, monkeypatch):
    intent, revision = _ugc_intent()
    body = {
        "schema": "content-lab.dossier-ingredient-request.v1",
        "masterPages": intent,
        "masterPagesHash": revision,
    }
    response = door.client.post(
        "/api/control-plane/v1/dossier-ingredients",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-RT-Lane": recipes.LANE,
            "X-RT-Page-Id": UGC_PAGE_ID,
        },
        json=body,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == "content-lab.dossier-ingredient-catalog.v1"
    assert payload["pageId"] == UGC_PAGE_ID
    assert "ugc-slideshow" in payload["currentFormatCandidates"]
    _write_fixture("dossier-ingredients", "ugc-slideshow", payload)


# ── source-libraries (3 routes) ──────────────────────────────────────────

SL_LIBRARY_ID = "pov-dirt-bike-8791-20260828"
SL_PAGE_ID = "tt-dirt-bike"


def _sl_manifest(clips):
    rows = []
    for index, body in enumerate(clips):
        sha256 = hashlib.sha256(body).hexdigest()
        filename = f"source-{index:02d}.mp4"
        rows.append({
            "sha256": sha256,
            "bytes": len(body),
            "filename": filename,
            "railPath": f"out/clips/pov-dirt-bike/{filename}",
        })
    rows.sort(key=lambda row: row["sha256"])
    return json.dumps({
        "schema": source_libraries.MANIFEST_SCHEMA,
        "libraryId": SL_LIBRARY_ID,
        "format": "pov-dirt-bike",
        "authority": {
            "system": "8791",
            "clipBankPath": "out/clip_bank.json",
            "clipBankSha256": "a" * 64,
            "selection": "operator-approved and text-scan-passed exact bytes",
        },
        "clips": rows,
    }, sort_keys=True, separators=(",", ":")).encode()


def _sl_publication(recipe_version="dossier-d1d1d1d1d1d1d1d1"):
    intent, revision = master_pages(
        SL_PAGE_ID, handle="dirt.bike", content_niche="POV - Dirtbike",
        content_engine="sourced_video", vault_url="https://drive.example/dirt-bike",
    )
    canonical = json.dumps({
        "schema": "dossier.recipe-spec.v2",
        "masterPages": intent,
        "masterPagesHash": revision,
        "renderTreatment": {
            "stylePreset": "trail-pov",
            "filters": {},
            "captionStyle": {},
            "clipSpeed": 1.0,
        },
        "demand": {"formatMix": {"pov-dirt-bike": 1.0}},
    }, sort_keys=True, separators=(",", ":"))
    return {
        "schema": recipes.REQUEST_SCHEMA,
        "pageId": SL_PAGE_ID,
        "lane": recipes.LANE,
        "recipeId": "pov-dirt-bike:master",
        "engine": "sourced_video",
        "recipeVersion": recipe_version,
        "dossierRevision": "rev-dirt-bike",
        "recipeSpecHash": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
        "recipeSpecCanonical": canonical,
    }


def _sl_headers(manifest_sha=None):
    headers = {"Authorization": f"Bearer {TOKEN}", "X-RT-Lane": recipes.LANE}
    if manifest_sha is not None:
        headers["X-Source-Manifest-Sha256"] = "sha256:" + manifest_sha
    return headers


@pytest.fixture
def source_lab(monkeypatch, tmp_path):
    import services.content_engine_registry as engine_registry
    import services.content_format_contracts as format_contracts

    bodies = [b"first-approved-source", b"second-approved-source"]
    manifest_dir = tmp_path / "manifest"
    manifest_dir.mkdir()
    raw = _sl_manifest(bodies)
    manifest_path = manifest_dir / f"{SL_LIBRARY_ID}.json"
    manifest_path.write_bytes(raw)
    manifest_sha = hashlib.sha256(raw).hexdigest()
    projects = tmp_path / "projects"
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir()
    (recipes_dir / f"{SL_LIBRARY_ID}.json").write_text(json.dumps({
        "registered": True, "project": SL_LIBRARY_ID, "maxQuantity": 10, "pages": [],
    }))
    contracts_value = json.loads(format_contracts.CONTRACTS_PATH.read_text())
    dirtbike_contract = contracts_value["contracts"]["pov-dirt-bike"]
    dirtbike_contract["creativeAuthority"]["version"] = "sha256:" + manifest_sha
    contracts_value["contracts"] = {"pov-dirt-bike": dirtbike_contract}
    contracts = tmp_path / "format-contracts.json"
    contracts.write_text(json.dumps(
        contracts_value, sort_keys=True, separators=(",", ":"),
    ))
    contract_hash = hashlib.sha256(json.dumps(
        dirtbike_contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    registry = tmp_path / "engine-registry.json"
    registry.write_text(json.dumps({
        "schema": engine_registry.REGISTRY_SCHEMA,
        "profiles": {
            "pov-dirt-bike": {
                "contentNiche": "POV - Dirtbike",
                "contentEngine": "sourced_video",
                "materialSource": "source_library",
                "assetType": "video/mp4",
                "executionStatus": "commissioned",
                "executorKind": "source_library",
                "executorId": SL_LIBRARY_ID,
                "executorVersion": "sha256:" + manifest_sha,
                "formatContractVersion": "sha256:" + contract_hash,
                "maxQuantity": 10,
            },
        },
    }, sort_keys=True, separators=(",", ":")))
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.delenv("APP_API_KEY", raising=False)
    monkeypatch.setenv("CONTENT_LAB_RECIPE_ROOT", str(tmp_path / "publications"))
    monkeypatch.setenv("CONTENT_LAB_ENGINE_REGISTRY", str(registry))
    monkeypatch.setenv("CONTENT_LAB_FORMAT_CONTRACTS", str(contracts))
    monkeypatch.setattr(source_libraries, "MANIFEST_DIR", manifest_dir)
    monkeypatch.setattr(source_libraries, "PROJECTS_DIR", projects)
    monkeypatch.setattr(cp, "PROJECTS_DIR", projects)
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    monkeypatch.setattr(cp, "_generation_root", lambda: tmp_path / "generated")
    monkeypatch.setattr(cp, "_secrets", _DeterministicSecrets())

    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    app.include_router(recipes.router, prefix="/api/control-plane")
    app.include_router(source_router.router, prefix="/api/control-plane")
    client = TestClient(app)
    publication_headers = {
        "Authorization": f"Bearer {TOKEN}",
        "X-RT-Lane": recipes.LANE,
        "X-RT-Page-Id": SL_PAGE_ID,
        "Idempotency-Key": "register-dirt-bike-source",
    }
    assert client.post(
        "/api/control-plane/v1/recipes",
        json=_sl_publication(),
        headers=publication_headers,
    ).status_code == 200
    manifest = source_libraries.load_source_library_manifest(SL_LIBRARY_ID)
    return SimpleNamespace(client=client, manifest=manifest, bodies=bodies)


def _sl_upload(client, manifest, clip, body, **header_overrides):
    headers = _sl_headers(manifest.sha256)
    headers.update(header_overrides)
    return client.put(
        f"/api/control-plane/v1/source-libraries/{SL_LIBRARY_ID}/clips/{clip.sha256}",
        content=body,
        headers=headers,
    )


def test_source_libraries_status(source_lab):
    client, manifest, _bodies = source_lab.client, source_lab.manifest, source_lab.bodies
    response = client.get(
        f"/api/control-plane/v1/source-libraries/{SL_LIBRARY_ID}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "content-lab.source-library-response.v1"
    assert body["libraryId"] == SL_LIBRARY_ID
    assert body["finalized"] is False
    _write_fixture("source-libraries", "status", body)


def test_source_libraries_upload_clip(source_lab):
    client, manifest, bodies = source_lab.client, source_lab.manifest, source_lab.bodies
    clip = manifest.clips[0]
    body = next(b for b in bodies if hashlib.sha256(b).hexdigest() == clip.sha256)
    response = _sl_upload(client, manifest, clip, body)
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == "content-lab.source-library-response.v1"
    assert payload["clipSha256"] == clip.sha256
    assert payload["status"] == "staged"
    _write_fixture("source-libraries", "upload-clip", payload)


def test_source_libraries_finalize(source_lab):
    client, manifest, bodies = source_lab.client, source_lab.manifest, source_lab.bodies
    for clip in manifest.clips:
        body = next(b for b in bodies if hashlib.sha256(b).hexdigest() == clip.sha256)
        assert _sl_upload(client, manifest, clip, body).status_code == 200
    response = client.post(
        f"/api/control-plane/v1/source-libraries/{SL_LIBRARY_ID}/finalize",
        headers=_sl_headers(manifest.sha256),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema"] == "content-lab.source-library-response.v1"
    assert payload["finalized"] is True
    _write_fixture("source-libraries", "finalize", payload)
