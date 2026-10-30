"""Closed contracts for page-scoped immutable-master recut execution."""

import asyncio
from dataclasses import replace
import hashlib
import json
import logging
from pathlib import Path
import shutil
import subprocess

import pytest
from services.clip_variation import GRADE_BANDS
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.control_plane as cp
import routers.control_plane_recipes as recipes
from services.control_plane_sources import (
    source_cut_durations,
    plan_source_cuts,
    resolve_source_recipe,
    source_cut_is_planned,
    planned_source_cut_duration,
)
from services.dossier_ingredients import (
    PINNED_LEGACY_DOSSIER_CATALOG_VERSIONS_BY_PUBLICATION,
    build_dossier_ingredient_catalog,
    catalog_selection_version,
)
from tests.master_pages_fixtures import bind_current_intent, master_pages
from tests.source_dna_fixture import isolate_source_dna_registry


@pytest.fixture(autouse=True)
def _isolated_source_dna(monkeypatch, tmp_path):
    """Retired chase/BTL libraries are deleted, so source-dna tests run against
    an isolated copy of the registry plus the non-retired dirt-bike fixture."""
    isolate_source_dna_registry(monkeypatch, tmp_path)


@pytest.fixture(autouse=True)
def _advertise_zero_reasons(monkeypatch):
    """These tests check the zero-plan reason fields, which the Lab sends
    only when CONTENT_LAB_ADVERTISE_ZERO_REASONS is on."""
    monkeypatch.setenv("CONTENT_LAB_ADVERTISE_ZERO_REASONS", "1")


TOKEN = "test-control-plane-token"
PAGE_ID = "tt-fixture-dirt-bike"
LIBRARY_ID = "pov-dirt-bike-fixture-v1"
MASTER_SHA = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
SOURCE_IDENTITY = "https://www.youtube.com/watch?v=vt5im2TRAKw"
_start_source_runner = cp._start_dossier_source


@pytest.fixture(autouse=True)
def offline_shipstream_manifest(monkeypatch):
    """publication() builds the Dossier catalog for a sourced page, which reads
    that page's ShipStream manifest. Tests that need a manifest patch
    _fetch_manifest themselves; any other read answers as an unreachable
    vault instead of leaving the process."""
    import services.shipstream_source_manifest as source_manifest

    def unreachable(_url):
        raise source_manifest.ShipStreamSourceUnavailable("ShipStream vault is offline in tests")

    monkeypatch.setattr(source_manifest, "_fetch_manifest", unreachable)


def publication(
    *,
    clip_speed=1.0,
    clip_crop=None,
    cut_duration_ms=6_000,
    source_start_ms=None,
    source_library_id=LIBRARY_ID,
    recipe_version="dossier-feedfacefeedface",
    schema="dossier.recipe-spec.v3",
):
    intent, revision = master_pages(
        PAGE_ID,
        handle="chase.miles.4l",
        content_niche="POV - Dirtbike",
        content_engine="sourced_video",
        vault_url="https://shipstream.test/vault/chase.miles.4l",
    )
    catalog = build_dossier_ingredient_catalog(PAGE_ID, intent, revision)
    render_treatment = {
        "stylePreset": None,
        "filters": {},
        "captionStyle": {},
        "clipSpeed": clip_speed,
        "clipCrop": clip_crop or {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5},
    }
    production = {
        "catalogVersion": "",
        "providerId": None,
        "modelId": None,
        "promptModuleId": None,
        "referenceSetId": None,
        "sourceLibraryId": source_library_id,
        "variationValues": {},
        "controls": {"cutDurationMs": cut_duration_ms},
    }
    if source_start_ms is not None:
        production["controls"]["sourceStartMs"] = source_start_ms
    production["catalogVersion"] = catalog_selection_version(
        catalog, "pov-dirt-bike", production,
    )
    recipe_spec = {
        "schema": schema,
        "masterPages": intent,
        "masterPagesHash": revision,
        "production": production,
        "renderTreatment": render_treatment,
        "demand": {"formatMix": {"pov-dirt-bike": 1.0}},
    }
    if schema == "dossier.recipe-spec.v4":
        recipe_spec["captionDiscipline"] = {
            "captionSet": "dirt-bike",
            "register": [
                "relatable_meme", "relationship_playful", "relationship_sincere",
            ],
            "slingshotShare": None,
        }
    canonical = json.dumps(recipe_spec, sort_keys=True, separators=(",", ":"))
    return {
        "schema": recipes.REQUEST_SCHEMA,
        "pageId": PAGE_ID,
        "lane": recipes.LANE,
        "recipeId": "pov-dirt-bike:master",
        "engine": "sourced_video",
        "recipeVersion": recipe_version,
        "dossierRevision": "rev-source-dna",
        "recipeSpecHash": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
        "recipeSpecCanonical": canonical,
    }


def test_source_recipe_accepts_only_a_full_pinned_publication(monkeypatch):
    payload = publication()
    spec = json.loads(payload["recipeSpecCanonical"])
    legacy_catalog_version = next(iter(
        PINNED_LEGACY_DOSSIER_CATALOG_VERSIONS_BY_PUBLICATION.values()
    ))
    spec["production"]["catalogVersion"] = legacy_catalog_version
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    payload["recipeSpecCanonical"] = canonical
    payload["recipeSpecHash"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    key = (
        payload["pageId"], payload["recipeId"], payload["recipeVersion"],
        payload["dossierRevision"], payload["recipeSpecHash"],
    )
    monkeypatch.setitem(
        PINNED_LEGACY_DOSSIER_CATALOG_VERSIONS_BY_PUBLICATION,
        key,
        legacy_catalog_version,
    )
    assert resolve_source_recipe(payload) is not None

    assert resolve_source_recipe({**payload, "dossierRevision": "rev-other"}) is None
    assert resolve_source_recipe({
        **payload, "recipeSpecHash": "sha256:" + "e" * 64,
    }) is None


def test_source_recipe_rejects_a_pinned_hash_on_another_publication():
    payload = publication()
    spec = json.loads(payload["recipeSpecCanonical"])
    spec["production"]["catalogVersion"] = next(iter(
        PINNED_LEGACY_DOSSIER_CATALOG_VERSIONS_BY_PUBLICATION.values()
    ))
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    payload["recipeSpecCanonical"] = canonical
    payload["recipeSpecHash"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    assert resolve_source_recipe(payload) is None


def test_source_recipe_rejects_an_arbitrary_stale_catalog_version():
    payload = publication()
    spec = json.loads(payload["recipeSpecCanonical"])
    spec["production"]["catalogVersion"] = "sha256:" + "f" * 64
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    payload["recipeSpecCanonical"] = canonical
    payload["recipeSpecHash"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    assert resolve_source_recipe(payload) is None


def test_source_recipe_resolves_the_exact_shipstream_page_library(monkeypatch):
    import services.shipstream_source_manifest as source_manifest

    sha256 = "a" * 64
    intent, _ = master_pages(
        PAGE_ID,
        handle="chase.miles.4l",
        content_niche="POV - Dirtbike",
        content_engine="sourced_video",
        vault_url="https://shipstream.test/vault/chase.miles.4l",
    )
    manifest = {
        "schema": "shipstream.source-manifest.v1",
        "page": "chase.miles.4l",
        "notion": {
            "pageId": intent["notionPageId"],
            "contentEngine": intent["contentEngine"],
            "contentNiche": intent["contentNiche"],
            "serviceMode": intent["automationMode"],
        },
        "format": "pov-dirt-bike",
        "sourceAuthority": {
            "kind": "historical_posted_cut_recovery",
            "pageHandle": "chase.miles.4l",
            "notionPageId": intent["notionPageId"],
            "pageBound": True,
            "replacementEligible": True,
        },
        "master": None,
        "historicalPostedCuts": [{
            "type": "historical_posted_cut",
            "pageHandle": "chase.miles.4l",
            "notionPageId": intent["notionPageId"],
            "sha256": sha256,
            "bytes": 12_345_678,
            "storageKey": f"vault/chase.miles.4l/pool/{sha256}.mp4",
            "uploadedAt": "2026-08-25T20:35:39.194Z",
            "media": {"durationSeconds": 10.0},
        }],
        "cuts": [],
    }
    current_manifest = [manifest]
    monkeypatch.setattr(
        source_manifest,
        "_fetch_manifest",
        lambda _url: json.dumps(current_manifest[0]).encode(),
    )
    library = source_manifest.load_shipstream_source_dna_library(
        intent, page_id=PAGE_ID, expected_format="pov-dirt-bike",
    )
    payload = publication(source_library_id=library.library_id)
    resolved = resolve_source_recipe(payload)
    assert resolved is not None
    assert resolved.source_library_id == library.library_id
    assert resolved.source_library_hash == library.sha256
    assert resolved.masters[0].storage_key == (
        f"vault/chase.miles.4l/pool/{sha256}.mp4"
    )
    changed = json.loads(json.dumps(manifest))
    second_sha = "b" * 64
    changed["historicalPostedCuts"].append({
        "type": "historical_posted_cut",
        "pageHandle": "chase.miles.4l",
        "notionPageId": intent["notionPageId"],
        "sha256": second_sha,
        "bytes": 12_345_679,
        "storageKey": f"vault/chase.miles.4l/pool/{second_sha}.mp4",
        "uploadedAt": "2026-08-25T20:36:39.194Z",
        "media": {"durationSeconds": 10.0},
    })
    current_manifest[0] = changed
    assert resolve_source_recipe(payload) is None


def test_scenic_source_recipe_resolves_the_pages_own_shipstream_library(monkeypatch):
    import services.shipstream_source_manifest as source_manifest

    page_id = "acct:scenic:city-pines"
    handle = "city.pines"
    source_sha = "c" * 64
    intent, revision = master_pages(
        page_id,
        handle=handle,
        content_niche="POV — Scenic",
        content_engine="sourced_video",
        vault_url=f"https://shipstream.test/vault/{handle}",
    )
    manifest = {
        "schema": "shipstream.source-manifest.v1",
        "page": handle,
        "notion": {
            "pageId": intent["notionPageId"],
            "contentEngine": intent["contentEngine"],
            "contentNiche": intent["contentNiche"],
            "serviceMode": intent["automationMode"],
        },
        "format": "pov-scenic",
        "sourceAuthority": {
            "kind": "historical_posted_cut_recovery",
            "pageHandle": handle,
            "notionPageId": intent["notionPageId"],
            "pageBound": True,
            "replacementEligible": True,
        },
        "master": None,
        "historicalPostedCuts": [{
            "type": "historical_posted_cut",
            "pageHandle": handle,
            "notionPageId": intent["notionPageId"],
            "sha256": source_sha,
            "bytes": 12_345_678,
            "storageKey": f"vault/{handle}/pool/{source_sha}.mp4",
            "uploadedAt": "2026-09-03T10:00:00.000Z",
            "media": {"durationSeconds": 10.0},
        }],
        "cuts": [],
    }
    monkeypatch.setattr(
        source_manifest, "_fetch_manifest",
        lambda _url: json.dumps(manifest).encode(),
    )
    library = source_manifest.load_shipstream_source_dna_library(
        intent, page_id=page_id, expected_format="pov-scenic",
    )
    catalog = build_dossier_ingredient_catalog(page_id, intent, revision)
    production = {
        "catalogVersion": "",
        "providerId": None,
        "modelId": None,
        "promptModuleId": None,
        "referenceSetId": None,
        "sourceLibraryId": library.library_id,
        "variationValues": {},
        "controls": {"cutDurationMs": 6_000},
    }
    production["catalogVersion"] = catalog_selection_version(
        catalog, "pov-scenic", production,
    )
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
            "captionSet": "night-core",
            "register": ["nostalgia"],
            "slingshotShare": None,
        },
        "demand": {"formatMix": {"pov-scenic": 1.0}},
    }
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    publication = {
        "schema": recipes.REQUEST_SCHEMA,
        "pageId": page_id,
        "lane": recipes.LANE,
        "recipeId": "pov-scenic:master",
        "engine": "sourced_video",
        "recipeVersion": "dossier-scenic000000",
        "dossierRevision": "rev-scenic-source",
        "recipeSpecHash": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
        "recipeSpecCanonical": canonical,
    }

    resolved = resolve_source_recipe(publication)
    assert resolved is not None
    assert resolved.format_slug == "pov-scenic"
    assert resolved.source_library_id == library.library_id
    assert resolved.masters[0].sha256 == source_sha


def headers(idempotency="source-job-0001"):
    return {
        "Authorization": f"Bearer {TOKEN}",
        "X-RT-Lane": recipes.LANE,
        "X-RT-Page-Id": PAGE_ID,
        "Idempotency-Key": idempotency,
    }


def job_body(quantity=2, payload=None):
    payload = payload or publication()
    spec = json.loads(payload["recipeSpecCanonical"])
    return {
        "pageId": PAGE_ID,
        "lane": recipes.LANE,
        "engine": payload["engine"],
        "lockedRecipeId": payload["recipeId"],
        "recipeVersion": payload["recipeVersion"],
        "quantity": quantity,
        "constraints": {},
        "sourceIsolation": {"partitionKey": f"page:{PAGE_ID}"},
        "policyHash": "sha256:policy",
        "masterPages": spec["masterPages"],
        "masterPagesHash": spec["masterPagesHash"],
    }


def _write_av_test_clip(path: Path, duration: float = 10.0) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required")
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=size=320x568:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000",
        "-t", str(duration), "-shortest", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart",
        str(path),
    ], check=True, capture_output=True, text=True)


def _probe_duration(path: Path) -> float:
    completed = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nokey=1:noprint_wrappers=1", str(path),
    ], check=True, capture_output=True, text=True)
    return float(completed.stdout.strip())


def _overlapping(left_cuts, right_cuts):
    """Pairs of stored cuts of the same master whose windows overlap."""
    return [
        (left, right)
        for left in left_cuts for right in right_cuts
        if left["masterSha256"] == right["masterSha256"]
        and left["startMs"] < right["startMs"] + right["durationMs"]
        and right["startMs"] < left["startMs"] + left["durationMs"]
    ]


def _pin_cuts_to_first_frame(job_id):
    """Move a planned job's cuts to start 0 so a short synthetic clip covers them."""
    store = cp._load_jobs()
    for cut in store["jobs"][job_id]["sourceCuts"]:
        cut.update({
            "slotId": f"{cut['masterSha256']}:0:{cut['durationMs']}",
            "libraryStartMs": 0,
            "startMs": 0,
        })
    cp.atomic_save(cp._jobs_path(), store)


@pytest.fixture
def lab(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("CONTENT_LAB_RECIPE_ROOT", str(tmp_path / "recipes"))
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    monkeypatch.setattr(cp, "_generation_root", lambda: tmp_path / "generated")
    intent, revision = master_pages(
        PAGE_ID,
        handle="chase.miles.4l",
        content_niche="POV - Dirtbike",
        content_engine="sourced_video",
        vault_url="https://shipstream.test/vault/chase.miles.4l",
    )
    bind_current_intent(monkeypatch, cp, intent, revision)
    started = []
    monkeypatch.setattr(cp, "_start_dossier_source", started.append)

    async def fake_thumbnail(job_root, video, index):
        target = job_root / "thumbnails" / f"{index:04d}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"jpeg-thumbnail")
        return cp._generated_manifest(job_root, target)

    monkeypatch.setattr(cp, "_thumbnail_manifest", fake_thumbnail)
    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    app.include_router(recipes.router, prefix="/api/control-plane")
    client = TestClient(app)
    assert client.post(
        "/api/control-plane/v1/recipes",
        json=publication(),
        headers=headers("source-register-0001"),
    ).status_code == 200
    return client, tmp_path, started


def test_source_recipe_requires_v3_page_scoped_master_and_exact_controls():
    resolved = resolve_source_recipe(publication())
    assert resolved is not None
    assert resolved.source_library_id == LIBRARY_ID
    assert resolved.masters[0].sha256 == MASTER_SHA
    assert resolved.masters[0].source_offset_ms == 120_000
    assert resolved.cut_duration_ms == 6_000
    assert resolved.minimum_source_start_ms == 0
    assert (resolved.output_width, resolved.output_height) == (1080, 1920)
    assert resolved.encode_preset == "tiktok_delivery_v1"

    assert resolve_source_recipe(publication(source_library_id="not-registered")) is None
    assert resolve_source_recipe(publication(cut_duration_ms=6_500)) is None
    assert resolve_source_recipe(publication(source_start_ms=True)) is None
    assert resolve_source_recipe(publication(source_start_ms=210_000.0)) is None
    assert resolve_source_recipe(publication(source_start_ms=210_500)) is None
    assert resolve_source_recipe(publication(source_start_ms=float("nan"))) is None
    assert resolve_source_recipe(publication(source_start_ms=7_200_001)) is None
    assert resolve_source_recipe(publication(source_start_ms=10**1_000)) is None
    assert resolve_source_recipe(publication(source_start_ms=-(10**1_000))) is None
    assert resolve_source_recipe(publication(cut_duration_ms=10**1_000)) is None
    legacy = publication()
    spec = json.loads(legacy["recipeSpecCanonical"])
    spec.pop("production")
    spec["schema"] = "dossier.recipe-spec.v2"
    legacy["recipeSpecCanonical"] = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    assert resolve_source_recipe(legacy) is None


def test_source_start_is_accepted_by_the_publication_api(lab):
    client, _, _ = lab
    payload = publication(
        source_start_ms=210_000,
        recipe_version="dossier-source-floor0001",
    )
    response = client.post(
        "/api/control-plane/v1/recipes",
        json=payload,
        headers=headers("source-register-floor-0001"),
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("value", [True, 210_500, -1_000, 7_201_000])
def test_source_start_publication_rejects_invalid_values(lab, value):
    client, _, _ = lab
    payload = publication(
        source_start_ms=value,
        recipe_version=f"dossier-invalid-floor-{str(value).replace('-', 'n').lower()}",
    )
    response = client.post(
        "/api/control-plane/v1/recipes",
        json=payload,
        headers=headers(f"source-register-invalid-{str(value).replace('-', 'n').lower()}"),
    )
    assert response.status_code == 409


def test_source_planner_rotates_five_to_nine_second_disjoint_windows():
    resolved = resolve_source_recipe(publication(cut_duration_ms=7_000))
    assert resolved is not None
    cuts = plan_source_cuts(resolved, 20, set())
    assert len(cuts) == 20
    lengths = [cut.duration_ms for cut in cuts]
    assert set(lengths) <= set(range(5_000, 9_001, 500))
    assert len(set(lengths)) >= 6, lengths
    assert lengths[0] == 7_000, "the first cut uses the page's Cut length"
    assert all(a != b for a, b in zip(lengths, lengths[1:])), lengths
    ordered = sorted(cuts, key=lambda cut: cut.start_ms)
    for previous, current in zip(ordered, ordered[1:]):
        assert previous.start_ms + previous.duration_ms <= current.start_ms


def test_source_planner_honors_page_earliest_original_timestamp():
    resolved = resolve_source_recipe(publication(source_start_ms=210_000))
    assert resolved is not None
    assert resolved.minimum_source_start_ms == 210_000
    cuts = plan_source_cuts(resolved, 5, set())
    assert len(cuts) == 5
    assert all(
        cut.master.source_offset_ms + cut.start_ms >= 210_000
        for cut in cuts
    )


def test_page_master_floor_is_compared_on_the_original_timeline():
    from dataclasses import replace

    recipe = resolve_source_recipe(publication(source_start_ms=135_000))
    assert recipe is not None
    master = replace(
        recipe.masters[0],
        source_offset_ms=120_000,
        provenance={
            **recipe.masters[0].provenance,
            "authority": "ShipStream source-manifest.v1 exact page master",
        },
    )
    recipe = replace(recipe, masters=(master,))
    cuts = plan_source_cuts(recipe, 100, set())
    assert all(cut.start_ms >= 15_000 for cut in cuts)
    _, served = _drain(recipe)
    assert min(int(slot.split(":")[1]) for slot in served) == 15_000
    assert not source_cut_is_planned(
        recipe,
        master,
        0,
        6_000,
        f"{master.sha256}:0",
    )
    assert not source_cut_is_planned(
        recipe, master, 14_000, 6_000, f"{master.sha256}:14000:6000",
    )
    assert source_cut_is_planned(
        recipe, master, 15_000, 6_000, f"{master.sha256}:15000:6000",
    )


def test_source_recipe_accepts_v4_and_preserves_the_caption_selection():
    resolved = resolve_source_recipe(publication(schema="dossier.recipe-spec.v4"))
    assert resolved is not None
    assert resolved.recipe_spec["captionDiscipline"] == {
        "captionSet": "dirt-bike",
        "register": [
            "relatable_meme", "relationship_playful", "relationship_sincere",
        ],
        "slingshotShare": None,
    }


def test_capability_and_jobs_bind_master_hash_and_unique_windows(lab):
    client, _, started = lab
    capabilities = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID, "Authorization": f"Bearer {TOKEN}"},
    ).json()["capabilities"]
    assert any(row["recipeId"] == "pov-dirt-bike:master" for row in capabilities)

    first = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2),
        headers=headers("source-job-first"),
    )
    assert first.status_code == 200
    first_job = cp._load_jobs()["jobs"][first.json()["jobId"]]
    assert first_job["sourceKind"] == "dossier_source_dna"
    assert first_job["sourceLibraryId"] == LIBRARY_ID
    assert first_job["sourceLibraryHash"]
    assert len(first_job["sourceCuts"]) == 2
    assert first_job["cutPlanSeed"]

    second_payload = publication(recipe_version="dossier-second000000")
    assert client.post(
        "/api/control-plane/v1/recipes", json=second_payload,
        headers=headers("source-register-second"),
    ).status_code == 200
    second = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2, second_payload),
        headers=headers("source-job-second"),
    )
    second_job = cp._load_jobs()["jobs"][second.json()["jobId"]]
    assert len(second_job["sourceCuts"]) == 2
    assert second_job["cutPlanSeed"] != first_job["cutPlanSeed"]
    assert not _overlapping(first_job["sourceCuts"], second_job["sourceCuts"])
    assert len(started) == 2


@pytest.mark.asyncio
async def test_queued_source_job_fails_before_media_work_when_strategy_changes(
    lab, monkeypatch,
):
    client, _, _ = lab
    queued = client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(1),
        headers=headers("source-job-before-strategy-change"),
    )
    assert queued.status_code == 200
    prior = job_body(1)["masterPages"]
    changed = {
        **prior,
        "contentNiche": "TRUCK",
        "contentEngine": "ai_video",
    }
    bind_current_intent(monkeypatch, cp, changed, recipes.intent_hash(changed))

    async def fail_source(*_args, **_kwargs):
        raise AssertionError("stale strategy reached source media")

    monkeypatch.setattr(cp, "_cached_source_master", fail_source)
    await cp._run_dossier_source(queued.json()["jobId"])
    stored = cp._load_jobs()["jobs"][queued.json()["jobId"]]
    assert stored["status"] == "failed"
    assert stored["error"] == "master_pages_strategy_changed"


@pytest.mark.asyncio
async def test_source_setup_exception_terminalizes_not_ghosts(lab, monkeypatch):
    client, _, _ = lab
    queued = client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(1),
        headers=headers("source-job-setup-failure"),
    )
    assert queued.status_code == 200
    job_id = queued.json()["jobId"]

    def boom(recipe):
        raise OSError("recipe read failed")

    monkeypatch.setattr(cp, "dossier_filters_to_color_correction", boom)
    # Drive the REAL entrypoint (captured at import time, before `lab`
    # monkeypatched _start_dossier_source to `started.append`).
    _start_source_runner(job_id)
    task = cp._source_tasks[job_id]
    await asyncio.wait_for(task, timeout=5)
    stored = cp._get_job_or_404(job_id)
    assert stored["status"] == "failed"
    assert "recipe read failed" in stored["error"]
    assert stored.get("completedAt")


@pytest.mark.asyncio
async def test_cached_source_master_hash_runs_off_the_event_loop(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import threading

    monkeypatch.setattr(cp, "_generation_root", lambda: tmp_path)
    master = SimpleNamespace(sha256="c" * 64, bytes=4)
    cache_root = tmp_path / "_source_dna"
    cache_root.mkdir(parents=True, exist_ok=True)
    target = cache_root / f"{master.sha256}.mp4"
    target.write_bytes(b"data")

    loop_thread = threading.get_ident()
    saw = {}

    def recording_sha256(path):
        saw["thread"] = threading.get_ident()
        return master.sha256

    monkeypatch.setattr(cp, "_sha256", recording_sha256)
    result = await cp._cached_source_master("acct:page", master, "job-1")
    assert result == target
    assert saw["thread"] != loop_thread, "cache-hit hash must not run on the event loop"




def test_capabilities_include_only_completed_page_source_import_identities(lab):
    client, _, _ = lab
    cp.atomic_save(cp._jobs_path(), {
        **cp._empty_jobs(),
        "jobs": {
            "completed-import": {
                "pageId": PAGE_ID,
                "sourceKind": "page_source_import",
                "status": "completed",
                "sourceUrl": "https://cdn.example.com/imported-master.mp4",
            },
            "pending-import": {
                "pageId": PAGE_ID,
                "sourceKind": "page_source_import",
                "status": "running",
                "sourceUrl": "https://cdn.example.com/pending-master.mp4",
            },
        },
    })
    headers_for_page = {"X-RT-Page-Id": PAGE_ID, "Authorization": f"Bearer {TOKEN}"}
    capabilities = client.get(
        "/api/control-plane/v1/capabilities", headers=headers_for_page,
    ).json()["capabilities"]
    assert capabilities[0]["sourceIdentities"] == [
        "https://cdn.example.com/imported-master.mp4",
        "https://www.youtube.com/watch?v=vt5im2TRAKw",
    ]

    store = cp._load_jobs()
    store["jobs"]["completed-import"]["status"] = "failed"
    cp.atomic_save(cp._jobs_path(), store)
    capabilities = client.get(
        "/api/control-plane/v1/capabilities", headers=headers_for_page,
    ).json()["capabilities"]
    assert capabilities[0]["sourceIdentities"] == [
        "https://www.youtube.com/watch?v=vt5im2TRAKw",
    ]


def test_sourced_paths_never_probe_the_ai_video_resolver(lab, monkeypatch):
    client, _, started = lab

    def unexpected_generation_probe(_publication):
        raise AssertionError("sourced_video must not probe the ai_video resolver")

    monkeypatch.setattr(cp, "resolve_generation_recipe", unexpected_generation_probe)
    monkeypatch.setattr(cp, "resolve_generation_recipe_with_reason", unexpected_generation_probe)
    capabilities = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID, "Authorization": f"Bearer {TOKEN}"},
    )
    assert capabilities.status_code == 200
    assert capabilities.json()["capabilities"] == [{
        "recipeId": "pov-dirt-bike:master",
        "engine": "sourced_video",
        "sourceIdentities": ["https://www.youtube.com/watch?v=vt5im2TRAKw"],
        "recipeVersion": "dossier-feedfacefeedface",
        "maxQuantity": 10,
        "sourceIdentities": [SOURCE_IDENTITY],
    }]

    created = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("source-job-engine-dispatch"),
    )
    assert created.status_code == 200
    assert len(started) == 1


def test_failed_source_job_releases_unrendered_cut_windows(lab):
    client, _, _ = lab
    first = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2),
        headers=headers("source-job-release-first"),
    )
    assert first.status_code == 200
    first_id = first.json()["jobId"]
    first_job = cp._load_jobs()["jobs"][first_id]
    first_slots = {cut["slotId"] for cut in first_job["sourceCuts"]}
    recipe = cp._dossier_source_recipe(publication())
    assert first_slots <= cp._source_dna_unavailable_slots(
        cp._load_jobs(), recipe, first_job["recipeVersion"],
    )
    cp._update_job(first_id, status="failed", error="source_dna_master_unavailable")
    assert not first_slots & cp._source_dna_unavailable_slots(
        cp._load_jobs(), recipe, first_job["recipeVersion"],
    )

    retry_payload = publication(recipe_version="dossier-released000000")
    assert client.post(
        "/api/control-plane/v1/recipes", json=retry_payload,
        headers=headers("source-register-release-retry"),
    ).status_code == 200
    retry = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2, retry_payload),
        headers=headers("source-job-release-retry"),
    )
    assert retry.status_code == 200
    retry_job = cp._load_jobs()["jobs"][retry.json()["jobId"]]
    assert len(retry_job["sourceCuts"]) == 2


def test_later_recipe_cuts_new_time_frames_not_completed_ones(lab):
    client, _, _ = lab
    first = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2),
        headers=headers("source-job-completed-first"),
    )
    assert first.status_code == 200
    cp._update_job(first.json()["jobId"], status="completed")

    next_payload = publication(recipe_version="dossier-completed00000")
    assert client.post(
        "/api/control-plane/v1/recipes", json=next_payload,
        headers=headers("source-register-completed-next"),
    ).status_code == 200
    following = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2, next_payload),
        headers=headers("source-job-completed-next"),
    )
    assert following.status_code == 200
    following_job = cp._load_jobs()["jobs"][following.json()["jobId"]]
    first_job = cp._load_jobs()["jobs"][first.json()["jobId"]]
    assert not _overlapping(first_job["sourceCuts"], following_job["sourceCuts"])


def test_active_source_job_reserves_windows_across_recipe_revisions(lab):
    client, _, _ = lab
    first = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2),
        headers=headers("source-job-active-first"),
    )
    assert first.status_code == 200

    next_payload = publication(recipe_version="dossier-active00000000")
    assert client.post(
        "/api/control-plane/v1/recipes", json=next_payload,
        headers=headers("source-register-active-next"),
    ).status_code == 200
    following = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2, next_payload),
        headers=headers("source-job-active-next"),
    )
    assert following.status_code == 200
    following_job = cp._load_jobs()["jobs"][following.json()["jobId"]]
    first_job = cp._load_jobs()["jobs"][first.json()["jobId"]]
    assert not _overlapping(first_job["sourceCuts"], following_job["sourceCuts"])


def test_capability_counts_down_to_zero_only_when_every_window_is_cut(lab, monkeypatch):
    # Every whole-second start x length is cut once first, then the half and
    # quarter-second starts; no window is ever cut twice. Capacity reaches 0
    # only then, and job creation answers 409 source_windows_exhausted. A
    # 12 s master keeps the drain short; the planner is the same at any length.
    from dataclasses import replace
    client, _, _ = lab
    resolve = cp._dossier_source_recipe

    def short_master(payload):
        recipe = resolve(payload)
        return replace(recipe, masters=tuple(
            replace(master, duration_ms=12_000) for master in recipe.masters
        ))

    monkeypatch.setattr(cp, "_dossier_source_recipe", short_master)

    def capability():
        response = client.get(
            "/api/control-plane/v1/capabilities",
            headers={"X-RT-Page-Id": PAGE_ID, "Authorization": f"Bearer {TOKEN}"},
        )
        assert response.status_code == 200
        return response.json()["capabilities"]

    every = set()
    for length in range(5_000, 9_001, 500):
        for offset in (0, 250, 500, 750):
            every |= {(start, length) for start in range(offset, 12_000 - length + 1, 1_000)}
        every.add((12_000 - length, length))
    frames: list[tuple[int, int]] = []
    round_number = 0
    while (quantity := capability()[0]["maxQuantity"]) > 0:
        round_number += 1
        assert round_number < 600, "capacity never reached 0"
        created = client.post(
            "/api/control-plane/v1/jobs", json=job_body(quantity),
            headers=headers(f"source-capacity-{round_number}"),
        )
        assert created.status_code == 200, created.text
        job = cp._load_jobs()["jobs"][created.json()["jobId"]]
        assert len(job["sourceCuts"]) == quantity
        planned = [(cut["startMs"], cut["durationMs"]) for cut in job["sourceCuts"]]
        assert not set(planned) & set(frames), "a window was cut twice"
        frames.extend(planned)
        cp._update_job(created.json()["jobId"], status="completed")

    assert set(frames) == every
    assert len(frames) == len(every)
    assert capability() == [{
        "recipeId": "pov-dirt-bike:master",
        "engine": "sourced_video",
        "sourceIdentities": ["https://www.youtube.com/watch?v=vt5im2TRAKw"],
        "recipeVersion": "dossier-feedfacefeedface",
        "maxQuantity": 0,
        "zeroReason": "source_windows_exhausted",
        "sourceIdentities": [SOURCE_IDENTITY],
    }]
    stopped = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("source-capacity-exhausted"),
    )
    assert stopped.status_code == 409
    assert stopped.json() == {"detail": "source_windows_exhausted"}
    assert stopped.headers.get("X-RT-Zero-Reason") == "source_windows_exhausted"


@pytest.mark.asyncio
async def test_runner_cuts_real_window_changes_speed_and_records_original_lineage(
    lab, monkeypatch,
):
    client, tmp_path, _ = lab
    payload = publication(
        clip_speed=2.0,
        cut_duration_ms=6_000,
        recipe_version="dossier-speed0000000",
    )
    assert client.post(
        "/api/control-plane/v1/recipes", json=payload,
        headers=headers("source-register-speed"),
    ).status_code == 200
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1, payload),
        headers=headers("source-job-speed"),
    )
    _pin_cuts_to_first_frame(response.json()["jobId"])
    source = tmp_path / "master.mp4"
    _write_av_test_clip(source, duration=15.0)

    async def cached_source(*_):
        return source

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    await cp._run_dossier_source(response.json()["jobId"])
    job = cp._load_jobs()["jobs"][response.json()["jobId"]]
    assert job["status"] == "completed", job.get("error")
    output = Path(job["artifactRoot"]) / job["clips"][0]["path"]
    planned_duration = job["sourceCuts"][0]["durationMs"]
    assert _probe_duration(output) == pytest.approx(planned_duration / 2_000, abs=0.15)
    lineage = job["clips"][0]["source"]
    assert lineage["master"]["sha256"] == MASTER_SHA
    assert lineage["cutWindow"] == {
        "libraryStartMs": 0,
        "libraryEndMs": planned_duration,
        "durationMs": planned_duration,
        "originalStartMs": 120_000,
        "originalEndMs": 120_000 + planned_duration,
    }
    assert lineage["pageId"] == PAGE_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("duration_ms,ok", [(6_000, True), (6_250, False), (9_500, False)])
async def test_runner_renders_a_six_second_recut_and_refuses_a_forged_one(
    lab, monkeypatch, duration_ms, ok,
):
    client, tmp_path, _ = lab
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers(f"source-job-sixsec-{duration_ms}"),
    )
    job_id = response.json()["jobId"]
    store = cp._load_jobs()
    store["jobs"][job_id]["sourceCuts"] = [{
        "slotId": f"{MASTER_SHA}:0:{duration_ms}",
        "masterSha256": MASTER_SHA,
        "libraryStartMs": 0,
        "startMs": 0,
        "durationMs": duration_ms,
    }]
    cp.atomic_save(cp._jobs_path(), store)
    source = tmp_path / "master.mp4"
    _write_av_test_clip(source)

    async def cached_source(*_):
        return source

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    if ok:
        assert job["status"] == "completed", job.get("error")
        output = Path(job["artifactRoot"]) / job["clips"][0]["path"]
        assert _probe_duration(output) == pytest.approx(6.0, abs=0.15)
        assert job["clips"][0]["source"]["cutWindow"]["durationMs"] == 6_000
    else:
        assert job["status"] == "failed"
        assert "source_recipe_cut_invalid" in (job.get("error") or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("clip_speed,duration_ms", [
    (1.0, 5_000), (1.0, 7_500), (1.0, 9_000), (1.5, 7_500), (0.75, 6_500),
])
async def test_real_renders_at_the_length_bounds_probe_inside_the_worker_bounds(
    lab, monkeypatch, clip_speed, duration_ms,
):
    # The Worker's visual admission refuses a sourced clip whose probed
    # duration is outside 5000-9000 ms. Render the shortest, a half-second
    # and the longest allowed cut (and the delivered extremes at a saved
    # speed) through the real executor and probe them the way the scanner does.
    import time
    from services.visual_admission import _probe
    client, tmp_path, _ = lab
    payload = publication(
        clip_speed=clip_speed, cut_duration_ms=7_000,
        recipe_version=f"dossier-bounds{int(clip_speed * 100):03d}{duration_ms:05d}",
    )
    assert duration_ms in source_cut_durations(cp._dossier_source_recipe(payload))
    assert client.post(
        "/api/control-plane/v1/recipes", json=payload,
        headers=headers(f"source-register-bounds-{clip_speed}-{duration_ms}"),
    ).status_code == 200
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1, payload),
        headers=headers(f"source-job-bounds-{clip_speed}-{duration_ms}"),
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["jobId"]
    store = cp._load_jobs()
    store["jobs"][job_id]["sourceCuts"] = [{
        "slotId": f"{MASTER_SHA}:0:{duration_ms}",
        "masterSha256": MASTER_SHA,
        "libraryStartMs": 0,
        "startMs": 0,
        "durationMs": duration_ms,
    }]
    cp.atomic_save(cp._jobs_path(), store)
    source = tmp_path / "master.mp4"
    _write_av_test_clip(source, duration=12.0)

    async def cached_source(*_):
        return source

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    output = Path(job["artifactRoot"]) / job["clips"][0]["path"]
    *_, probed = _probe(output, time.monotonic() + 30)
    assert 5_000 <= round(probed * 1_000) <= 9_000, probed
    assert round(probed * 1_000) == pytest.approx(duration_ms / clip_speed, abs=40)


@pytest.mark.asyncio
async def test_sub_second_starts_render_and_never_decode_the_same_frames(lab, monkeypatch):
    # Sub-second starts (a quarter, a half and three quarters of a second in)
    # pass the executor's planner check, render through the exact ffmpeg
    # seek, and give different bytes from the whole-second cut of the same
    # length.
    client, tmp_path, _ = lab
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("source-job-subsecond"),
    )
    job_id = response.json()["jobId"]
    store = cp._load_jobs()
    store["jobs"][job_id]["sourceCuts"] = [{
        "slotId": f"{MASTER_SHA}:{start}:6000",
        "masterSha256": MASTER_SHA,
        "libraryStartMs": start,
        "startMs": start,
        "durationMs": 6_000,
    } for start in (0, 250, 500, 750)]
    cp.atomic_save(cp._jobs_path(), store)
    source = tmp_path / "master.mp4"
    _write_av_test_clip(source)

    async def cached_source(*_):
        return source

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert len({clip["sha256"] for clip in job["clips"]}) == 4
    assert [clip["source"]["cutWindow"]["libraryStartMs"] for clip in job["clips"]] == [0, 250, 500, 750]
    for clip in job["clips"]:
        assert _probe_duration(Path(job["artifactRoot"]) / clip["path"]) == pytest.approx(6.0, abs=0.15)


@pytest.mark.asyncio
async def test_queued_source_runner_accepts_unrelated_registry_digest_drift_and_passes_exact_treatment(
    lab, monkeypatch,
):
    client, tmp_path, _ = lab
    source = tmp_path / "master.mp4"
    source.write_bytes(b"master")
    crop = {"zoom": 1.5, "focusX": 0.2, "focusY": 0.8}
    payload = publication(
        clip_speed=0.75, clip_crop=crop, cut_duration_ms=8_000,
        recipe_version="dossier-crop00000000",
    )
    assert client.post(
        "/api/control-plane/v1/recipes", json=payload,
        headers=headers("source-register-crop"),
    ).status_code == 200
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1, payload),
        headers=headers("source-job-crop"),
    )
    job_id = response.json()["jobId"]
    assert cp._load_jobs()["jobs"][job_id]["status"] == "queued"
    _pin_cuts_to_first_frame(job_id)
    # Model a persisted queued job from before per-profile hashes were stored.
    # Its explicit source pins still authorize this work after unrelated
    # registry contents change.
    store = cp._load_jobs()
    selected_profile_hash = store["jobs"][job_id].pop("engineProfileHash")
    store["jobs"][job_id]["engineRegistryHash"] = "0" * 64
    cp.atomic_save(cp._jobs_path(), store)
    assert cp._load_jobs()["jobs"][job_id].get("engineProfileHash") is None
    assert selected_profile_hash.startswith("sha256:")
    calls = []

    async def cached_source(*_):
        return source

    async def render(src, dst, correction, **kwargs):
        calls.append((src, dst, correction, kwargs))
        Path(dst).write_bytes(b"derived")

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    monkeypatch.setattr(cp, "run_color_correct", render)
    # These tests stub the cut encode with a non-video file; Layer B transforms
    # (now on for every sourced page) would re-probe it, and Layer B also
    # probes the delivered duration. Neutralize both here.
    monkeypatch.setattr(cp, "apply_transforms", lambda *a, **k: None)
    monkeypatch.setattr(cp, "probe_source_output_duration_seconds", lambda path: 8.0)
    await cp._run_dossier_source(job_id)
    assert calls[0][3] == {
        "scale": None,
        "encode_args": cp.delivery_encode_args("tiktok_delivery_v1"),
        "playback_speed": 0.75,
        "clip_crop": crop,
        "clip_crop_size": (1080, 1920),
        "clip_start_ms": 0,
        "clip_duration_ms": cp._load_jobs()["jobs"][job_id]["sourceCuts"][0]["durationMs"],
    }
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed"
    assert Path(job["artifactRoot"]) in Path(calls[0][1]).parents
    receipt = job["clips"][0]["sourceTreatment"]
    assert receipt["sourceSha256"] == job["clips"][0]["sha256"]
    assert receipt["generationJobId"] == job_id
    assert receipt["visualTreatment"]["clipSpeed"] == pytest.approx(0.75)
    assert receipt["visualTreatment"]["clipCrop"] == crop


@pytest.mark.asyncio
async def test_runner_enforces_neutral_vertical_delivery_without_custom_crop(
    lab, monkeypatch,
):
    client, tmp_path, _ = lab
    source = tmp_path / "master.mp4"
    source.write_bytes(b"master")
    payload = publication(
        clip_crop=None, recipe_version="dossier-neutralcrop0000",
    )
    spec = json.loads(payload["recipeSpecCanonical"])
    spec["renderTreatment"]["clipCrop"] = None
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    payload["recipeSpecCanonical"] = canonical
    payload["recipeSpecHash"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    registered = client.post(
        "/api/control-plane/v1/recipes", json=payload,
        headers=headers("source-register-neutral-crop"),
    )
    assert registered.status_code == 200, registered.json()
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1, payload),
        headers=headers("source-job-neutral-crop"),
    )
    calls = []

    async def cached_source(*_):
        return source

    async def render(src, dst, correction, **kwargs):
        calls.append((src, dst, correction, kwargs))
        Path(dst).write_bytes(b"derived")

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    monkeypatch.setattr(cp, "run_color_correct", render)
    # These tests stub the cut encode with a non-video file; Layer B transforms
    # (now on for every sourced page) would re-probe it, and Layer B also
    # probes the delivered duration. Neutralize both here.
    monkeypatch.setattr(cp, "apply_transforms", lambda *a, **k: None)
    monkeypatch.setattr(cp, "probe_source_output_duration_seconds", lambda path: 8.0)
    await cp._run_dossier_source(response.json()["jobId"])
    checked = cp._load_jobs()["jobs"][response.json()["jobId"]]
    assert checked["status"] == "completed", checked.get("error")
    assert calls[0][3]["clip_crop"] == {
        "zoom": 1.0, "focusX": 0.5, "focusY": 0.5,
    }
    assert calls[0][3]["encode_args"] == cp.delivery_encode_args(
        "tiktok_delivery_v1",
    )
    assert calls[0][3]["clip_crop_size"] == (1080, 1920)


def test_source_media_origin_is_explicit_pinned_https(monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_CONTROL_PLANE_ORIGIN", "https://control.example")
    assert cp._source_media_origin() == "https://control.example"
    for invalid in ("", "http://control.example", "https://user@control.example", "https://control.example/path"):
        monkeypatch.setenv("CONTENT_LAB_CONTROL_PLANE_ORIGIN", invalid)
        with pytest.raises(RuntimeError, match="source_dna_control_plane_origin_unavailable"):
            cp._source_media_origin()


def test_failed_source_job_status_exposes_only_the_bounded_terminal_error(lab):
    client, _, _ = lab
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1), headers=headers("source-job-failure"),
    )
    job_id = response.json()["jobId"]

    cp._update_job(job_id, status="running", error="must-not-leak-before-terminal")
    running = client.get(
        f"/api/control-plane/v1/jobs/{job_id}", headers=headers("source-status-running"),
    )
    assert running.status_code == 200
    assert "error" not in running.json()

    exact_error = "source_dna_master_hash_mismatch:" + ("x" * 400)
    cp._update_job(job_id, status="failed", error=exact_error)
    failed = client.get(
        f"/api/control-plane/v1/jobs/{job_id}", headers=headers("source-status-failed"),
    )
    assert failed.status_code == 200
    assert failed.json()["error"] == exact_error[:300]
    assert len(failed.json()["error"]) == 300


def test_og_source_skips_first_minute_but_preserves_immutable_offsets():
    from dataclasses import replace
    recipe = resolve_source_recipe(publication(cut_duration_ms=7_000))
    original = replace(recipe.masters[0], source_offset_ms=0)
    whole = replace(recipe, masters=(original,))
    cuts = plan_source_cuts(whole, 100, set())
    assert all(cut.master.source_offset_ms + cut.start_ms >= 60_000 for cut in cuts)
    _, served = _drain(whole)
    assert min(int(slot.split(":")[1]) for slot in served) == 60_000
    offset = replace(recipe, masters=(replace(original, source_offset_ms=60_000),))
    _, served = _drain(offset)
    assert min(int(slot.split(":")[1]) for slot in served) == 0
    short = replace(whole, masters=(replace(original, duration_ms=60_000),))
    assert plan_source_cuts(short, 1, set()) == []


@pytest.mark.parametrize("authority", [
    "ShipStream source-manifest.v1 exact page master",
    (
        "ShipStream source-manifest.v1 page-bound historical posted cut; "
        "original source unavailable"
    ),
])
def test_page_bound_shipstream_master_can_use_first_frame(authority):
    from dataclasses import replace
    recipe = resolve_source_recipe(publication(cut_duration_ms=7_000))
    original = recipe.masters[0]
    page_master = replace(
        original,
        duration_ms=7_500,
        source_offset_ms=0,
        provenance={**original.provenance, "authority": authority},
    )
    page_recipe = replace(recipe, masters=(page_master,))
    cuts = plan_source_cuts(page_recipe, 2, set())
    assert len(cuts) == 1, "two cuts of a 7.5 s master would overlap"
    assert cuts[0].start_ms + cuts[0].duration_ms <= 7_500
    _, served = _drain(page_recipe)
    assert min(int(slot.split(":")[1]) for slot in served) == 0


def test_used_page_master_is_recut_at_shifted_points_not_declared_exhausted():
    from services.control_plane_sources import source_window_exclusions
    # Production 2026-09-18: bambisrevenge11 and tender.acres each have one
    # 90-second page master and read capability_capacity_exhausted once its
    # 9-second grid was cut. Every whole-second start and allowed length of the
    # same footage is now a distinct time frame before the page is exhausted.
    from dataclasses import replace
    recipe = resolve_source_recipe(publication(cut_duration_ms=7_000))
    original = recipe.masters[0]
    page_master = replace(
        original, duration_ms=90_000, source_offset_ms=0,
        provenance={**original.provenance, "authority": "ShipStream source-manifest.v1 exact page master"},
    )
    recipe = replace(recipe, masters=(page_master,))
    first = plan_source_cuts(recipe, 100, set())
    served = {cut.slot_id for cut in first}
    recut = plan_source_cuts(recipe, 100, served)
    assert recut, "a used page master must still offer re-cuts"
    assert not {cut.slot_id for cut in recut} & served
    assert all(cut.start_ms + cut.duration_ms <= 90_000 for cut in recut)
    assert all(5_000 <= cut.duration_ms <= 9_000 for cut in recut)
    for i, left in enumerate(recut):
        for right in recut[i + 1:]:
            assert not (left.start_ms < right.start_ms + right.duration_ms
                        and right.start_ms < left.start_ms + left.duration_ms), (left, right)
    _, total = _drain(recipe)
    # Whole-second starts, plus one ending on the last frame for half-second lengths.
    assert len(total) == sum(
        (90_000 - length) // 1_000 + 1 + (length % 1_000 != 0)
        for length in range(5_000, 9_001, 500)
    )
    # Every whole-second window cut: the planner moves inside the second
    # (half, then quarter seconds) instead of stopping, never repeating one.
    following = plan_source_cuts(recipe, 100, total)
    assert following and not {cut.slot_id for cut in following} & total
    assert all(cut.start_ms % 1_000 for cut in following)
    # Another page's window still blocks a re-cut, exactly like a first cut.
    everything = source_window_exclusions([{
        "sourceIdentity": page_master.provenance["sourceUrl"], "startMs": 0, "endMs": 90_000,
    }])
    assert plan_source_cuts(recipe, 100, served, everything) == []


def _page_master_recipe(duration_ms):
    from dataclasses import replace
    recipe = resolve_source_recipe(publication(cut_duration_ms=7_000))
    original = recipe.masters[0]
    master = replace(
        original, duration_ms=duration_ms, source_offset_ms=0,
        provenance={**original.provenance, "authority": "ShipStream source-manifest.v1 exact page master"},
    )
    return replace(recipe, masters=(master,)), master


def _drain(recipe, served=frozenset(), exclusions=None):
    """Plan until no never-cut whole-second window is left.

    The planner then moves to sub-second starts, so "drained" means every
    whole-second window is cut.
    """
    served = set(served)
    batches = []
    for _ in range(5_000):
        batch = plan_source_cuts(recipe, 100, served, exclusions)
        fresh = [
            cut for cut in batch
            if cut.slot_id not in served
            and (cut.start_ms % 1_000 == 0 or cut.start_ms == cut.master.duration_ms - cut.duration_ms)
        ]
        if not fresh:
            return batches, served
        batches.append(fresh)
        served |= {cut.slot_id for cut in fresh}
    raise AssertionError("fresh windows never ran out")


def test_short_page_master_is_recut_into_six_second_clips():
    # Production 2026-09-18: healing.in.the.hills has one 7.5-second page
    # master, so the grid gave it exactly one cut and it read
    # capability_capacity_exhausted. Operator: "do 6-second clips of the same
    # footage". Every fitting time frame, including 0-6 s and 1.5-7.5 s, is
    # cut once, one per job (they overlap); then come sub-second starts.
    recipe, master = _page_master_recipe(7_500)
    batches, served = _drain(recipe)
    cuts = [cut for batch in batches for cut in batch]
    assert sorted((cut.start_ms, cut.duration_ms) for cut in cuts) == [
        (0, 5_000), (0, 5_500), (0, 6_000), (0, 6_500), (0, 7_000), (0, 7_500),
        (500, 7_000), (1_000, 5_000), (1_000, 5_500), (1_000, 6_000),
        (1_000, 6_500), (1_500, 6_000), (2_000, 5_000), (2_000, 5_500),
        (2_500, 5_000),
    ]
    assert all(len(batch) == 1 for batch in batches)
    assert f"{master.sha256}:0:6000" in served and f"{master.sha256}:1500:6000" in served
    following = plan_source_cuts(recipe, 100, served)
    assert len(following) == 1 and following[0].slot_id not in served
    assert following[0].start_ms % 1_000


def test_long_footage_yields_every_length_including_the_six_second_grid():
    recipe, master = _page_master_recipe(90_000)
    batches, served = _drain(recipe)
    cuts = [cut for batch in batches for cut in batch]
    assert {cut.duration_ms for cut in cuts} == set(range(5_000, 9_001, 500))
    six = {cut.start_ms for cut in cuts if cut.duration_ms == 6_000}
    assert set(range(0, 84_001, 6_000)) <= six, "the old 6-second pass is a subset"
    assert len(cuts) == len({cut.slot_id for cut in cuts})
    assert not any(cut.slot_id in served for cut in plan_source_cuts(recipe, 100, served))


def test_executor_accepts_exactly_the_cuts_the_planner_emits():
    from services.control_plane_sources import source_cut_is_planned
    recipe, master = _page_master_recipe(7_500)
    batches, _ = _drain(recipe)
    for cut in (cut for batch in batches for cut in batch):
        assert source_cut_is_planned(recipe, master, cut.start_ms, cut.duration_ms, cut.slot_id), cut
    sha = master.sha256
    assert source_cut_is_planned(recipe, master, 1_500, 6_000, f"{sha}:1500:6000"), "ends on the last frame"
    assert not source_cut_is_planned(recipe, master, 0, 4_500, f"{sha}:0:4500"), "length outside the allowed range"
    assert not source_cut_is_planned(recipe, master, 100, 6_000, f"{sha}:100:6000"), "off the quarter-second grid"
    assert not source_cut_is_planned(recipe, master, 2_000, 6_000, f"{sha}:2000:6000"), "past the last frame"
    assert not source_cut_is_planned(recipe, master, 0, 5_000, f"{sha}:0"), "grid id with a non-planned length"
    assert not source_cut_is_planned(recipe, master, 0, 6_000, "other:0:6000")


def test_shared_source_exclusions_skip_other_page_windows_and_reencoding():
    from dataclasses import replace
    from services.control_plane_sources import source_window_exclusions
    recipe = resolve_source_recipe(publication(cut_duration_ms=7_000))
    master = replace(recipe.masters[0], sha256='f'*64, source_offset_ms=0)
    recipe = replace(recipe, masters=(master,))
    excluded = source_window_exclusions([{'sourceIdentity':'https://youtu.be/vt5im2TRAKw?si=x','startMs':60_000,'endMs':80_000}])
    _, served = _drain(recipe, exclusions=excluded)
    assert min(int(slot.split(':')[1]) for slot in served) == 80_000
    # Half-open touching intervals do not block a valid next slot.
    excluded = source_window_exclusions([{'sourceIdentity':master.provenance['sourceUrl'],'startMs':0,'endMs':63_000}])
    _, served = _drain(recipe, exclusions=excluded)
    assert min(int(slot.split(':')[1]) for slot in served) == 63_000
    # Unknown legacy timing blocks the whole source, not a guessed short window.
    excluded = source_window_exclusions([{'sourceIdentity':master.provenance['sourceUrl'],'startMs':0,'endMs':9_007_199_254_740_991}])
    assert plan_source_cuts(recipe, 1, set(), excluded) == []


def test_shared_folder_exclusions_bind_to_the_exact_master_file():
    from dataclasses import replace
    from services.control_plane_sources import source_window_exclusions
    recipe = resolve_source_recipe(publication(cut_duration_ms=7_000))
    folder_url = 'https://drive.google.com/drive/folders/library'
    first = replace(recipe.masters[0], sha256='a'*64, source_offset_ms=0,
                    provenance={'sourceUrl': folder_url})
    second = replace(recipe.masters[0], sha256='b'*64, source_offset_ms=0,
                     provenance={'sourceUrl': folder_url})
    recipe = replace(recipe, masters=(first, second))
    excluded = source_window_exclusions([{
        'sourceIdentity': folder_url,
        'masterSha256': first.sha256,
        'startMs': 0,
        'endMs': 9_007_199_254_740_991,
    }])
    cuts = plan_source_cuts(recipe, 1, set(), excluded)
    assert cuts[0].master.sha256 == second.sha256


@pytest.mark.parametrize('excluded', [[{'sourceIdentity':'bad','startMs':0,'endMs':1}],
    [{'sourceIdentity':'https://example.com/v','startMs':True,'endMs':2}],
    [{'sourceIdentity':'https://example.com/v','startMs':2,'endMs':1}], [{}]*2001])
def test_source_exclusions_fail_closed(excluded):
    from services.control_plane_sources import source_window_exclusions
    with pytest.raises(ValueError): source_window_exclusions(excluded)


def test_job_creation_consumes_global_source_exclusions(lab):
    client, _, _ = lab
    body = job_body(1)
    body['constraints']['sourceWindowExclusions'] = [{'sourceIdentity':'https://youtu.be/vt5im2TRAKw','startMs':120_000,'endMs':140_000}]
    response=client.post('/api/control-plane/v1/jobs',json=body,headers=headers('source-with-exclusions'))
    assert response.status_code == 200, response.text
    job=cp._load_jobs()['jobs'][response.json()['jobId']]
    # Original 120-140 s is library 0-20 s on this master (offset 120 s).
    assert job['sourceCuts'][0]['startMs'] >= 20_000


def test_source_identity_matches_worker_query_and_youtube_rules():
    from services.control_plane_sources import canonical_source_identity
    assert canonical_source_identity('https://youtube.com/watch?v=abcdefghijk&v=12345678901') == 'https://www.youtube.com/watch?v=abcdefghijk'
    assert canonical_source_identity('https://example.com/v?x=~*&utm_source=no') == 'https://example.com/v?x=%7E*'
    assert canonical_source_identity('https://example.com:443/v?b=2&a=1#fragment') == 'https://example.com/v?a=1&b=2'


def test_source_capability_suppresses_unknown_original_identity(lab, monkeypatch):
    from dataclasses import replace
    client, _, _ = lab
    resolve = cp._dossier_source_recipe
    def malformed(publication):
        recipe = resolve(publication)
        return replace(recipe, masters=tuple(replace(master, provenance={}) for master in recipe.masters))
    monkeypatch.setattr(cp, "_dossier_source_recipe", malformed)
    response = client.get("/api/control-plane/v1/capabilities", headers={"X-RT-Page-Id": PAGE_ID, "Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.json()["capabilities"] == []

@pytest.mark.asyncio
async def test_source_cancellation_releases_windows_and_removes_own_root(lab, monkeypatch):
    client, tmp_path, _ = lab
    response = client.post("/api/control-plane/v1/jobs", json=job_body(1), headers=headers("source-cancel"))
    assert response.status_code == 200
    job_id = response.json()["jobId"]
    started = asyncio.Event(); hold = asyncio.Event()
    async def stalled_source(*args, **kwargs):
        started.set(); await hold.wait()
    monkeypatch.setattr(cp, "_cached_source_master", stalled_source)
    task = asyncio.create_task(cp._run_dossier_source(job_id))
    await asyncio.wait_for(started.wait(), 5)
    saved = cp._load_jobs()["jobs"][job_id]
    root = Path(saved["artifactRoot"])
    assert root.exists()
    recipe = cp._dossier_source_recipe(cp.load_registered_recipe(PAGE_ID, saved["recipeId"], saved["engine"], saved["recipeVersion"]))
    slots = {cut["slotId"] for cut in saved["sourceCuts"]}
    assert slots & cp._source_dna_unavailable_slots(
        cp._load_jobs(), recipe, saved["recipeVersion"],
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    saved = cp._load_jobs()["jobs"][job_id]
    assert saved["status"] == "failed" and saved["error"] == "generation_cancelled" and saved["completedAt"]
    assert not root.exists()
    assert not (slots & cp._source_dna_unavailable_slots(
        cp._load_jobs(), recipe, saved["recipeVersion"],
    ))


@pytest.mark.parametrize("speed", [0.5, 0.8, 1.0, 1.2, 2.0])
def test_source_duration_respects_delivery_range_after_saved_speed(speed):
    recipe = resolve_source_recipe(publication(clip_speed=speed, cut_duration_ms=7_000))
    assert recipe is not None
    cuts = plan_source_cuts(recipe, 20, set())
    assert cuts
    for cut in cuts:
        # Operator 2026-09-30: 5-9 s cuts in 0.5 s steps; the Worker admits 5-9 s
        # delivered clips. Speeds no 5-9 s window can deliver within 5-9 s
        # (0.5x, 2x) keep the earlier 6-11 s vocabulary unchanged.
        if 5_000 * speed <= 9_000 and 5_000 <= 9_000 * speed:
            assert 5_000 <= cut.duration_ms <= 9_000
            assert 5_000 <= cut.duration_ms / speed <= 9_000
        else:
            assert 6_000 <= cut.duration_ms / speed <= 11_000
        assert source_cut_is_planned(recipe, cut.master, cut.start_ms, cut.duration_ms, cut.slot_id)


@pytest.mark.parametrize("speed", [0.5, 2.0])
def test_pov_club_refuses_speeds_without_a_worker_admissible_window(speed):
    recipe = resolve_source_recipe(publication(clip_speed=speed, cut_duration_ms=7_000))
    assert recipe is not None
    club_recipe = replace(recipe, format_slug="pov-club")
    assert source_cut_durations(club_recipe) == ()
    assert plan_source_cuts(club_recipe, 1, set()) == []
    master = club_recipe.masters[0]
    assert not source_cut_is_planned(
        club_recipe, master, 120_000, 12_000,
        f"{master.sha256}:120000:12000",
    )


def test_pov_club_unsupported_speed_is_refused_before_job_or_window_admission(lab, monkeypatch):
    client, _, started = lab
    payload = publication(
        clip_speed=2.0,
        recipe_version="dossier-pov-club-unsupported-speed",
    )
    registered = client.post(
        "/api/control-plane/v1/recipes", json=payload,
        headers=headers("source-register-pov-club-speed"),
    )
    assert registered.status_code == 200
    original_resolver = cp._dossier_source_recipe
    monkeypatch.setattr(
        cp, "_dossier_source_recipe",
        lambda publication: replace(
            original_resolver(publication), format_slug="pov-club",
        ),
    )
    monkeypatch.setattr(
        cp, "_source_dna_cut_ledger",
        lambda *_args: (_ for _ in ()).throw(AssertionError("windows were reserved")),
    )
    response = client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(1, payload),
        headers=headers("source-job-pov-club-unsupported-speed"),
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "source_speed_duration_unsupported"}
    assert cp._load_jobs()["jobs"] == {}
    assert started == []


@pytest.mark.parametrize("speed", [0.6, 0.8, 1.0, 1.5, 1.8])
def test_pov_club_supported_speeds_stay_inside_worker_source_and_output_bounds(speed):
    recipe = resolve_source_recipe(publication(clip_speed=speed, cut_duration_ms=7_000))
    assert recipe is not None
    club_recipe = replace(recipe, format_slug="pov-club")
    cuts = plan_source_cuts(club_recipe, 10, set())
    assert cuts
    for cut in cuts:
        assert 5_000 <= cut.duration_ms <= 9_000
        assert 5_000 <= cut.duration_ms / speed <= 9_000


@pytest.mark.parametrize("speed", [0.5, 1.0, 2.0])
def test_pov_club_rejects_legacy_grid_ids_without_breaking_older_formats(speed):
    recipe = resolve_source_recipe(publication(clip_speed=speed, cut_duration_ms=7_000))
    assert recipe is not None
    master = recipe.masters[0]
    start_ms = 120_000
    duration_ms = planned_source_cut_duration(recipe, master, start_ms)
    legacy_id = f"{master.sha256}:{start_ms}"
    assert source_cut_is_planned(recipe, master, start_ms, duration_ms, legacy_id)
    club_recipe = replace(recipe, format_slug="pov-club")
    assert not source_cut_is_planned(club_recipe, master, start_ms, duration_ms, legacy_id)
    for cut in plan_source_cuts(club_recipe, 1, set()):
        assert source_cut_is_planned(
            club_recipe, cut.master, cut.start_ms, cut.duration_ms, cut.slot_id,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("measured_seconds", [None, 4.99, 9.01, 5.0, 9.0])
async def test_pov_club_measures_output_before_admitting_manifest(lab, monkeypatch, measured_seconds):
    client, tmp_path, _ = lab
    payload = publication()
    assert client.post(
        "/api/control-plane/v1/recipes", json=payload,
        headers=headers("club-measured-register"),
    ).status_code == 200
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1, payload),
        headers=headers("club-measured-job"),
    )
    job_id = response.json()["jobId"]
    resolver = cp._dossier_source_recipe
    monkeypatch.setattr(cp, "_dossier_source_recipe", lambda p: replace(resolver(p), format_slug="pov-club"))
    source = tmp_path / "master.mp4"
    source.write_bytes(b"master")

    async def cached_source(*_):
        return source

    async def render(_src, dst, *_args, **_kwargs):
        Path(dst).write_bytes(b"derived")

    probed = []
    def probe(path):
        assert Path(path).read_bytes() == b"derived"
        probed.append(path)
        return measured_seconds

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    monkeypatch.setattr(cp, "run_color_correct", render)
    # These tests stub the cut encode with a non-video file; Layer B transforms
    # (now on for every sourced page) would re-probe it. Neutralize them here.
    monkeypatch.setattr(cp, "apply_transforms", lambda *a, **k: None)
    monkeypatch.setattr(cp, "probe_source_output_duration_seconds", probe)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert len(probed) == 1
    if measured_seconds in (5.0, 9.0):
        assert job["status"] == "completed"
        assert len(job["clips"]) == 1
    else:
        assert job["status"] == "failed"
        assert job["error"] == "source_output_duration_unsupported"
        assert not job.get("clips")


def _framed_publication(frame, fit, recipe_version):
    payload = publication(
        clip_crop={"zoom": 1.5, "focusX": 0.2, "focusY": 0.7}, recipe_version=recipe_version,
    )
    spec = json.loads(payload["recipeSpecCanonical"])
    spec["renderTreatment"]["frame"] = frame
    if fit is not None:
        spec["renderTreatment"]["frameFit"] = fit
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    payload["recipeSpecCanonical"] = canonical
    payload["recipeSpecHash"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize("frame,fit,resolved", [
    ("16:9", "fit", "fit"), ("1:1", "fill", "fill"), ("4:3", None, "fit"), ("3:4", "fill", "fill"),
])
async def test_runner_cuts_a_framed_page_into_its_band(lab, monkeypatch, frame, fit, resolved):
    client, tmp_path, _ = lab
    source = tmp_path / "master.mp4"
    source.write_bytes(b"master")
    payload = _framed_publication(frame, fit, "dossier-framed00000000")
    assert client.post(
        "/api/control-plane/v1/recipes", json=payload, headers=headers("source-register-framed"),
    ).status_code == 200
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1, payload), headers=headers("source-job-framed"),
    )
    job_id = response.json()["jobId"]
    _pin_cuts_to_first_frame(job_id)
    calls, probes = [], []

    async def cached_source(*_):
        return source

    async def render(src, dst, correction, **kwargs):
        calls.append(kwargs)
        Path(dst).write_bytes(b"derived")

    async def probe(path):
        probes.append(path)
        return (1920, 1080)

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    monkeypatch.setattr(cp, "run_color_correct", render)
    # These tests stub the cut encode with a non-video file; Layer B transforms
    # (now on for every sourced page) would re-probe it. Neutralize them here.
    monkeypatch.setattr(cp, "apply_transforms", lambda *a, **k: None)
    monkeypatch.setattr(cp, "probe_display_size", probe)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    expected = {
        "scale": None,
        "encode_args": cp.delivery_encode_args("tiktok_delivery_v1"),
        "playback_speed": 1.0,
        "clip_crop": {"zoom": 1.5, "focusX": 0.2, "focusY": 0.7},
        "clip_crop_size": (1080, 1920),
        "clip_start_ms": 0,
        "clip_duration_ms": job["sourceCuts"][0]["durationMs"],
        "page_frame": frame,
        "frame_fit": resolved,
    }
    if fit is None:
        expected.pop("page_frame")
        expected.pop("frame_fit")
    if fit is not None and resolved == "fit":
        # fit contains the source window, so the master's upright size is probed.
        expected["source_size"] = (1920, 1080)
    assert calls == [expected]
    assert probes == ([source] if fit is not None and resolved == "fit" else [])
    # Receipts are unchanged: visualTreatment is exactly grade, speed and crop;
    # the recipe context records the frame (and a saved fit) the cut used.
    receipt = job["clips"][0]["sourceTreatment"]
    assert set(receipt["visualTreatment"]) == {"filters", "clipSpeed", "clipCrop"}
    assert receipt["sourceRecipeTreatment"]["frame"] == frame
    assert receipt["sourceRecipeTreatment"].get("frameFit") == fit
    assert "frame" not in job["clips"][0] and "frameFit" not in job["clips"][0]


@pytest.mark.asyncio
async def test_runner_passes_no_frame_arguments_for_a_vertical_page(lab, monkeypatch):
    client, tmp_path, _ = lab
    source = tmp_path / "master.mp4"
    source.write_bytes(b"master")
    payload = _framed_publication("9:16", None, "dossier-vertical0000000")
    assert client.post(
        "/api/control-plane/v1/recipes", json=payload, headers=headers("source-register-vertical"),
    ).status_code == 200
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1, payload), headers=headers("source-job-vertical"),
    )
    calls = []

    async def cached_source(*_):
        return source

    async def render(src, dst, correction, **kwargs):
        calls.append(kwargs)
        Path(dst).write_bytes(b"derived")

    async def probe(_path):
        raise AssertionError("a 9:16 page never probes its master")

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    monkeypatch.setattr(cp, "run_color_correct", render)
    # These tests stub the cut encode with a non-video file; Layer B transforms
    # (now on for every sourced page) would re-probe it, and Layer B also
    # probes the delivered duration. Neutralize both here.
    monkeypatch.setattr(cp, "apply_transforms", lambda *a, **k: None)
    monkeypatch.setattr(cp, "probe_source_output_duration_seconds", lambda path: 8.0)
    monkeypatch.setattr(cp, "probe_display_size", probe)
    await cp._run_dossier_source(response.json()["jobId"])
    job = cp._load_jobs()["jobs"][response.json()["jobId"]]
    assert job["status"] == "completed", job.get("error")
    assert set(calls[0]) == {"scale", "encode_args", "playback_speed", "clip_crop", "clip_crop_size",
                             "clip_start_ms", "clip_duration_ms"}


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["timeout", "reply_too_large", "reply_invalid", "size_invalid", "ffprobe_failed"])
async def test_runner_cuts_fill_when_the_master_display_size_is_unprovable(lab, monkeypatch, caplog, reason):
    """No new refusal: fit falls back to fill, which needs no size, and the job completes."""
    client, tmp_path, _ = lab
    source = tmp_path / "master.mp4"
    source.write_bytes(b"master")
    payload = _framed_publication("16:9", "fit", "dossier-framedfallback0")
    assert client.post(
        "/api/control-plane/v1/recipes", json=payload, headers=headers("source-register-fallback"),
    ).status_code == 200
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1, payload), headers=headers("source-job-fallback"),
    )
    job_id = response.json()["jobId"]
    calls = []

    async def cached_source(*_):
        return source

    async def render(src, dst, correction, **kwargs):
        calls.append(kwargs)
        Path(dst).write_bytes(b"derived")

    async def probe(_path):
        raise cp.FrameGeometryUnavailable(reason)

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    monkeypatch.setattr(cp, "run_color_correct", render)
    # These tests stub the cut encode with a non-video file; Layer B transforms
    # (now on for every sourced page) would re-probe it, and Layer B also
    # probes the delivered duration. Neutralize both here.
    monkeypatch.setattr(cp, "apply_transforms", lambda *a, **k: None)
    monkeypatch.setattr(cp, "probe_source_output_duration_seconds", lambda path: 8.0)
    monkeypatch.setattr(cp, "probe_display_size", probe)
    with caplog.at_level("WARNING", logger="control_plane"):
        await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert calls[0]["page_frame"] == "16:9" and calls[0]["frame_fit"] == "fill"
    assert "source_size" not in calls[0]
    assert job["clips"][0]["frameCut"] == {"frame": "16:9", "frameFit": "fill"}
    assert f"cutting master.mp4 fill, its display size is unavailable ({reason})" in caplog.text


@pytest.mark.asyncio
async def test_a_clip_varying_page_cuts_each_window_at_its_own_speed_and_records_it(
    lab, monkeypatch,
):
    # services/clip_variation.py: the varied speed and crop are what the bytes
    # carry AND what the receipt says, so the final render takes them as they are.
    client, tmp_path, _ = lab
    monkeypatch.setattr(cp, "varies_clips", lambda page_id: page_id == PAGE_ID)
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2),
        headers=headers("source-job-varied"),
    )
    # Two early windows a short synthetic master covers.
    store = cp._load_jobs()
    for index, cut in enumerate(store["jobs"][response.json()["jobId"]]["sourceCuts"]):
        start = index * 15_000
        cut.update({"slotId": f"{cut['masterSha256']}:{start}:{cut['durationMs']}",
                    "libraryStartMs": start, "startMs": start})
    cp.atomic_save(cp._jobs_path(), store)
    source = tmp_path / "master.mp4"
    _write_av_test_clip(source, duration=40.0)

    async def cached_source(*_):
        return source

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    await cp._run_dossier_source(response.json()["jobId"])
    job = cp._load_jobs()["jobs"][response.json()["jobId"]]
    assert job["status"] == "completed", job.get("error")
    looks = []
    for clip, cut in zip(job["clips"], job["sourceCuts"]):
        visual = clip["sourceTreatment"]["visualTreatment"]
        assert visual["clipSpeed"] == clip["clipSpeed"] != 1.0
        assert visual["clipCrop"] == clip["clipCrop"]
        output = Path(job["artifactRoot"]) / clip["path"]
        assert _probe_duration(output) == pytest.approx(cut["durationMs"] / 1000 / clip["clipSpeed"], abs=0.2)
        looks.append((visual["clipSpeed"], tuple(sorted(visual["clipCrop"].items()))))
        # The transforms ride on the manifest, never in the receipt.
        assert set(clip["clipTransforms"]) == {"mirror", "motion", "reverse", "loop_swap"}
        assert "clipTransforms" not in clip["sourceTreatment"]
        # The trial similarity record never blocks; the second cut is compared with the first.
        assert clip["similarity"]["mode"] == "trial"
        assert len(clip["similarity"]["fingerprint"]) == 8
        # The grade moved inside its bands, recorded in the receipt.
        for name, band in GRADE_BANDS.items():
            assert abs(visual["filters"][name] - (1.0 if name != "warmth" else 0.0)) <= band
    assert looks[0] != looks[1]
    assert job["clips"][1]["similarity"]["nearestClip"] == f"{response.json()['jobId']}/0"


@pytest.mark.asyncio
async def test_a_non_varying_sourced_page_gets_transforms_and_an_unchanged_receipt(
    lab, monkeypatch,
):
    # Layer B is wider than Layer A: a non-BTL sourced source-video page gets
    # mirror/motion/reverse on the manifest, while its cut keeps the recipe's
    # one speed/crop/filters. The receipt schema and visualTreatment stay
    # identical to today; sourceSha256 follows the (possibly transformed)
    # delivered bytes.
    client, tmp_path, _ = lab
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("source-job-nonvarying-transforms"),
    )
    job_id = response.json()["jobId"]
    _pin_cuts_to_first_frame(job_id)
    source = tmp_path / "master.mp4"
    _write_av_test_clip(source, duration=40.0)

    async def cached_source(*_):
        return source

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    clip = job["clips"][0]
    # Layer B rides on the manifest, never in the receipt.
    assert set(clip["clipTransforms"]) == {"mirror", "motion", "reverse", "loop_swap"}
    # Layer A is untouched: the recipe's one look is what the bytes carry and
    # what the receipt says.
    receipt = clip["sourceTreatment"]
    assert set(receipt["visualTreatment"]) == {"filters", "clipSpeed", "clipCrop"}
    assert receipt["visualTreatment"]["clipSpeed"] == 1.0
    assert receipt["visualTreatment"]["clipCrop"] == {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5}
    assert clip["clipSpeed"] == 1.0
    assert clip["clipCrop"] == {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5}
    assert "clipTransforms" not in receipt
    # The similarity TRIAL is a Layer A (clip-varying) instrument only.
    assert "similarity" not in clip


@pytest.mark.asyncio
async def test_a_denied_sourced_page_gets_no_transforms(lab, monkeypatch):
    # A page on TRANSFORM_DENY_PAGES keeps its clips exactly as delivered: no
    # manifest transforms and no per-cut look.
    client, tmp_path, _ = lab
    monkeypatch.setattr(cp, "transforms_enabled", lambda page_id, slug: False)
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("source-job-denied-transforms"),
    )
    job_id = response.json()["jobId"]
    _pin_cuts_to_first_frame(job_id)
    source = tmp_path / "master.mp4"
    _write_av_test_clip(source, duration=40.0)

    async def cached_source(*_):
        return source

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    clip = job["clips"][0]
    assert "clipTransforms" not in clip
    assert "similarity" not in clip
    assert clip["clipSpeed"] == 1.0
    receipt = clip["sourceTreatment"]
    assert set(receipt["visualTreatment"]) == {"filters", "clipSpeed", "clipCrop"}


@pytest.mark.asyncio
async def test_transforms_all_off_makes_a_non_varying_page_match_main(lab, monkeypatch):
    import services.clip_transforms as ct

    # TRANSFORMS_ALL off is the kill switch: a non-vary_look sourced page draws
    # no Layer B transforms, so its clips are main's clips (no clipTransforms,
    # the recipe's one speed/crop/filters). A switch change takes effect on the
    # Railway service restart after the variable change (do it in a Lab gap).
    client, tmp_path, _ = lab
    monkeypatch.setattr(ct, "TRANSFORMS_ALL", False)
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("source-job-transforms-all-off"),
    )
    job_id = response.json()["jobId"]
    _pin_cuts_to_first_frame(job_id)
    source = tmp_path / "master.mp4"
    _write_av_test_clip(source, duration=40.0)

    async def cached_source(*_):
        return source

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    clip = job["clips"][0]
    assert "clipTransforms" not in clip
    assert "similarity" not in clip
    assert clip["clipSpeed"] == 1.0
    receipt = clip["sourceTreatment"]
    assert set(receipt["visualTreatment"]) == {"filters", "clipSpeed", "clipCrop"}
    assert receipt["visualTreatment"]["clipSpeed"] == 1.0


@pytest.mark.asyncio
async def test_a_layer_b_only_page_logs_its_drawn_transforms(lab, monkeypatch, caplog):
    from services.clip_transforms import CutTransforms

    # Releases grep Railway logs to prove Layer B applies (an 1182-line release
    # check found zero transform lines). Every cut on a transforms-enabled page
    # logs ONE INFO line keyed content_lab.source_cut_transforms with the drawn
    # set and its slot id; applied=True only when apply_transforms really
    # re-encoded the cut. Log-only: the manifest and receipt stay as they were.
    client, tmp_path, _ = lab
    monkeypatch.setattr(cp, "varies_clips", lambda page_id: False)
    monkeypatch.setattr(cp, "cut_transforms", lambda *a, **k: CutTransforms(mirror=True))
    applied = []
    monkeypatch.setattr(cp, "apply_transforms",
                        lambda path, transforms, encode_args: applied.append(path))
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2),
        headers=headers("source-job-transform-log"),
    )
    job_id = response.json()["jobId"]
    _pin_cuts_to_first_frame(job_id)
    source = tmp_path / "master.mp4"
    _write_av_test_clip(source, duration=40.0)

    async def cached_source(*_):
        return source

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    with caplog.at_level("INFO", logger="control_plane"):
        await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    lines = [record for record in caplog.records
             if record.message.split()[0] == "content_lab.source_cut_transforms"]
    assert len(lines) == 2, [record.message for record in caplog.records]
    logged_slots = set()
    for record in lines:
        fields = dict(token.split("=", 1) for token in record.message.split()[1:])
        assert fields["job_id"] == job_id
        assert fields["page_id"] == PAGE_ID
        assert fields["format_slug"] == "pov-dirt-bike"
        assert fields["cut_slot_id"].startswith(f"{MASTER_SHA}:0:")
        assert fields["mirror"] == "True"
        assert fields["motion"] == "None"
        assert fields["reverse"] == "False"
        assert fields["loop_swap"] == "False"
        assert fields["layer"] == "B"
        assert fields["applied"] == "True"
        logged_slots.add(fields["cut_slot_id"])
    # One line per cut, each naming its own slot id <master sha>:<start>:<duration>.
    assert logged_slots == {f"{cut['masterSha256']}:{cut['startMs']}:{cut['durationMs']}"
                            for cut in job["sourceCuts"]}
    assert len(applied) == 2
    # Log-only change: the manifest still carries the drawn set, the receipt not.
    for clip in job["clips"]:
        assert clip["clipTransforms"] == {"mirror": True, "motion": None,
                                          "reverse": False, "loop_swap": False}
        assert "clipTransforms" not in clip["sourceTreatment"]


@pytest.mark.asyncio
async def test_transforms_all_off_logs_one_reason_line_and_no_per_cut_lines(
    lab, monkeypatch, caplog,
):
    import services.clip_transforms as ct

    # With the kill-switch off, Layer B is off for the whole job: ONE line per
    # job names the reason (not one per cut), and no cut logs a transform line
    # with applied=True. A log sweep can tell "off by design" from "on but
    # nothing drawn".
    client, tmp_path, _ = lab
    monkeypatch.setattr(ct, "TRANSFORMS_ALL", False)
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2),
        headers=headers("source-job-transform-log-off"),
    )
    job_id = response.json()["jobId"]
    _pin_cuts_to_first_frame(job_id)
    source = tmp_path / "master.mp4"
    _write_av_test_clip(source, duration=40.0)

    async def cached_source(*_):
        return source

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    with caplog.at_level("INFO", logger="control_plane"):
        await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    keys = [record.message.split()[0] for record in caplog.records]
    # Two cuts, but the off job explains itself exactly once.
    assert keys.count("content_lab.source_cut_transforms_off") == 1
    assert "content_lab.source_cut_transforms" not in keys
    off = next(record for record in caplog.records
               if record.message.split()[0] == "content_lab.source_cut_transforms_off")
    fields = dict(token.split("=", 1) for token in off.message.split()[1:])
    assert fields == {"job_id": job_id, "page_id": PAGE_ID,
                      "format_slug": "pov-dirt-bike",
                      "reason": "transforms_all_off"}
    assert len(job["clips"]) == 2
    for clip in job["clips"]:
        assert "clipTransforms" not in clip


@pytest.mark.asyncio
async def test_a_transformed_sourced_cut_outside_the_format_bounds_is_rejected(lab, monkeypatch):
    from services.clip_transforms import CutTransforms

    # Layer B now probes the delivered duration whenever transforms were
    # applied, not only for pov-club / Layer A. A non-vary_look sourced cut
    # that lands outside the Worker's 5-10 s window must fail the job here.
    client, tmp_path, _ = lab
    monkeypatch.setattr(cp, "varies_clips", lambda page_id: False)
    monkeypatch.setattr(cp, "cut_transforms", lambda *a, **k: CutTransforms(mirror=True))
    # Keep the transform encode out of this test: the probe is what is pinned.
    monkeypatch.setattr(cp, "apply_transforms", lambda path, transforms, encode_args: None)
    monkeypatch.setattr(cp, "probe_source_output_duration_seconds", lambda path: 4.5)
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("source-job-transform-under-five"),
    )
    job_id = response.json()["jobId"]
    _pin_cuts_to_first_frame(job_id)
    source = tmp_path / "master.mp4"
    _write_av_test_clip(source, duration=40.0)

    async def cached_source(*_):
        return source

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "failed", job.get("error")
    assert "source_output_duration_unsupported" in job["error"]


@pytest.mark.asyncio
async def test_a_layer_b_only_page_delivers_an_unreadable_probe(lab, monkeypatch, caplog):
    from services.clip_transforms import CutTransforms

    # MED-1 (lead review): for a Layer-B-only page (not vary_look and not
    # pov-club), a probe that cannot read the delivered file must NOT fail the
    # job. It logs a warning keyed content_lab.layer_b_probe_unreadable (with
    # job and page), bumps the counter, and delivers the already-encoded cut.
    # main's strict behaviour stays only where main had it: vary_look and
    # pov-club still raise on None (pinned separately).
    client, tmp_path, _ = lab
    monkeypatch.setattr(cp, "varies_clips", lambda page_id: False)
    monkeypatch.setattr(cp, "cut_transforms", lambda *a, **k: CutTransforms(mirror=True))
    monkeypatch.setattr(cp, "apply_transforms", lambda path, transforms, encode_args: None)
    monkeypatch.setattr(cp, "probe_source_output_duration_seconds", lambda path: None)
    cp._layer_b_probe_unreadable_count = 0
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("source-job-layer-b-probe-unreadable"),
    )
    job_id = response.json()["jobId"]
    _pin_cuts_to_first_frame(job_id)
    source = tmp_path / "master.mp4"
    _write_av_test_clip(source, duration=40.0)

    async def cached_source(*_):
        return source

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    with caplog.at_level("WARNING", logger="control_plane"):
        await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    clip = job["clips"][0]
    assert set(clip["clipTransforms"]) == {"mirror", "motion", "reverse", "loop_swap"}
    assert cp._layer_b_probe_unreadable_count == 1
    assert any("content_lab.layer_b_probe_unreadable" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_transforms_all_off_cut_bytes_match_main(lab, monkeypatch):
    import services.clip_transforms as ct

    # DS r- MED-1 (byte-vs-base): with the TRANSFORMS_ALL kill switch off, a
    # non-varying sourced page must deliver the exact bytes (sha256) and
    # manifest that Lab main 60db4247 delivers for the same job. The pre-change
    # (main) path is reproduced by forcing transforms_enabled False — the only
    # head gate main lacks for a non-varying page, which makes head behaviorally
    # identical to main here. TRANSFORMS_ALL off reaches that same gate through
    # the real function. Both runs use the real cut encode, so the sha pins real
    # delivered bytes rather than a stub.
    client, tmp_path, _ = lab

    async def run_job(use_kill_switch):
        if use_kill_switch:
            monkeypatch.setattr(ct, "TRANSFORMS_ALL", False)
            monkeypatch.setattr(cp, "transforms_enabled", ct.transforms_enabled)
        else:
            monkeypatch.setattr(cp, "transforms_enabled", lambda page_id, slug: False)
        response = client.post(
            "/api/control-plane/v1/jobs", json=job_body(1),
            headers=headers("source-job-byte-base"),
        )
        job_id = response.json()["jobId"]
        _pin_cuts_to_first_frame(job_id)
        source = tmp_path / "master.mp4"
        _write_av_test_clip(source, duration=40.0)

        async def cached_source(*_):
            return source

        monkeypatch.setattr(cp, "_cached_source_master", cached_source)
        await cp._run_dossier_source(job_id)
        job = cp._load_jobs()["jobs"][job_id]
        assert job["status"] == "completed", job.get("error")
        return job["clips"][0]

    main_clip = await run_job(False)
    head_clip = await run_job(True)
    assert main_clip["sha256"] == head_clip["sha256"]
    assert main_clip["bytes"] == head_clip["bytes"]
    assert main_clip["name"] == head_clip["name"]
    assert main_clip["path"] == head_clip["path"]
    assert main_clip["clipSpeed"] == head_clip["clipSpeed"] == 1.0
    assert main_clip["clipCrop"] == head_clip["clipCrop"]
    assert "clipTransforms" not in main_clip and "clipTransforms" not in head_clip
    assert "similarity" not in main_clip and "similarity" not in head_clip


@pytest.mark.asyncio
async def test_a_denied_sourced_page_gets_zero_transforms_and_no_extra_encode(lab, monkeypatch):
    import services.clip_transforms as ct

    # TRANSFORM_DENY_PAGES: a denied page keeps its clips exactly as delivered —
    # zero transforms on the manifest and no extra transform encode pass.
    client, tmp_path, _ = lab
    monkeypatch.setattr(ct, "TRANSFORM_DENY_PAGES", frozenset({PAGE_ID}))
    encoded = []
    monkeypatch.setattr(cp, "apply_transforms", lambda path, transforms, encode_args: encoded.append(transforms))
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("source-job-denied-zero-encode"),
    )
    job_id = response.json()["jobId"]
    _pin_cuts_to_first_frame(job_id)
    source = tmp_path / "master.mp4"
    _write_av_test_clip(source, duration=40.0)

    async def cached_source(*_):
        return source

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    clip = job["clips"][0]
    assert "clipTransforms" not in clip
    assert "similarity" not in clip
    assert encoded == [], "a denied page must never run the transform encode pass"
    assert clip["clipSpeed"] == 1.0


@pytest.mark.asyncio
async def test_a_loop_swapped_cut_is_delivered_at_or_under_nine_seconds(
    lab, monkeypatch,
):
    # routers/control_plane.py:3837 is the ONLY production enforcement of the
    # <= 9.0 s cap for a loop-swapped cut. The unit test L2 drives
    # services/clip_variation.py directly; this test drives the real call site
    # (real cut + real loop_swap re-encode) with loop_swap forced, and asserts
    # the delivered clip lands at or under 9.0 s. The cut's own seed draws a
    # speed that would exceed 9.0 s without the cap (8500 ms / 0.92 = 9.24 s),
    # so mutating line 3837 to `None` turns this test RED.
    from services.clip_transforms import CutTransforms

    client, tmp_path, _ = lab
    monkeypatch.setattr(cp, "varies_clips", lambda page_id: page_id == PAGE_ID)
    # Force the loop_swap transform so the cap path is exercised on every cut.
    monkeypatch.setattr(cp, "cut_transforms", lambda *a, **k: CutTransforms(loop_swap=True))
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("source-job-loop-swap-cap"),
    )
    job_id = response.json()["jobId"]
    store = cp._load_jobs()
    store["jobs"][job_id]["sourceCuts"] = [{
        "slotId": f"{MASTER_SHA}:0:8500",
        "masterSha256": MASTER_SHA,
        "libraryStartMs": 0,
        "startMs": 0,
        "durationMs": 8500,
    }]
    cp.atomic_save(cp._jobs_path(), store)
    source = tmp_path / "master.mp4"
    _write_av_test_clip(source, duration=40.0)

    async def cached_source(*_):
        return source

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    clip = job["clips"][0]
    assert clip["clipTransforms"]["loop_swap"] is True
    output = Path(job["artifactRoot"]) / clip["path"]
    delivered = _probe_duration(output)
    assert delivered <= 9.0, (clip["clipSpeed"], delivered)


@pytest.mark.asyncio
async def test_a_loop_swapped_cut_over_nine_seconds_is_rejected(lab, monkeypatch):
    # The post-encode check enforces 9.0 s (not 10.0) for loop-swapped cuts:
    # a cut that lands over 9.0 s must fail the job, even if the speed cap is
    # bypassed. Force a 0.92 speed on an 8500 ms window (8500/0.92 = 9.24 s);
    # the pre-loop-swap head allowed 10.0 s and would ship this silently.
    from services.clip_transforms import CutTransforms

    client, tmp_path, _ = lab
    monkeypatch.setattr(cp, "varies_clips", lambda page_id: page_id == PAGE_ID)
    monkeypatch.setattr(cp, "cut_transforms", lambda *a, **k: CutTransforms(loop_swap=True))
    # Ignore the call-site speed cap; deliver a speed that overshoots 9.0 s.
    monkeypatch.setattr(cp, "cut_variant", lambda *a, **k: (0.92, {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5}))
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("source-job-loop-swap-over-nine"),
    )
    job_id = response.json()["jobId"]
    store = cp._load_jobs()
    store["jobs"][job_id]["sourceCuts"] = [{
        "slotId": f"{MASTER_SHA}:0:8500",
        "masterSha256": MASTER_SHA,
        "libraryStartMs": 0,
        "startMs": 0,
        "durationMs": 8500,
    }]
    cp.atomic_save(cp._jobs_path(), store)
    source = tmp_path / "master.mp4"
    _write_av_test_clip(source, duration=40.0)

    async def cached_source(*_):
        return source

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "failed", job.get("error")
    assert "source_output_duration_unsupported" in job["error"]


def test_the_latest_page_cut_seeds_the_next_jobs_look(monkeypatch):
    jobs = {
        "old": {"pageId": PAGE_ID, "status": "completed", "sourceKind": "dossier_source_dna",
                "completedAt": "2026-10-21T10:00:00+00:00",
                "clips": [{"clipSpeed": 0.8, "clipCrop": {"zoom": 1.5, "focusX": 0.5, "focusY": 0.5}}]},
        "new": {"pageId": PAGE_ID, "status": "completed", "sourceKind": "dossier_source_dna",
                "completedAt": "2026-10-22T10:00:00+00:00",
                "clips": [{"clipSpeed": 0.9, "clipCrop": {"zoom": 1.6, "focusX": 0.48, "focusY": 0.52}}]},
        "other-page": {"pageId": "acct:other", "status": "completed", "sourceKind": "dossier_source_dna",
                       "completedAt": "2026-10-22T11:00:00+00:00",
                       "clips": [{"clipSpeed": 1.1, "clipCrop": {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5}}]},
    }
    monkeypatch.setattr(cp, "_load_jobs", lambda: {"jobs": jobs})
    assert cp._latest_page_cut_look(PAGE_ID, "running") == (0.9, {"zoom": 1.6, "focusX": 0.48, "focusY": 0.52})
    assert cp._latest_page_cut_look("acct:none", "running") is None


@pytest.mark.asyncio
async def test_a_varied_job_whose_history_read_fails_ends_failed_not_running(lab, monkeypatch):
    client, tmp_path, _ = lab
    monkeypatch.setattr(cp, "varies_clips", lambda page_id: page_id == PAGE_ID)
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1),
        headers=headers("source-job-varied-history-fails"),
    )

    def broken(*_args, **_kwargs):
        raise OSError("job store unreadable")

    monkeypatch.setattr(cp.clip_similarity, "recent_fingerprints", broken)
    await cp._run_dossier_source(response.json()["jobId"])
    job = cp._load_jobs()["jobs"][response.json()["jobId"]]
    assert job["status"] == "failed"
    assert "job store unreadable" in job["error"]


@pytest.mark.parametrize("backup_cuts,status", [((1,), "completed"), ((0, 1), "failed")])
@pytest.mark.asyncio
async def test_a_cut_on_a_backup_whose_file_is_missing_is_skipped_and_the_job_goes_on(
    lab, monkeypatch, backup_cuts, status,
):
    from dataclasses import replace as dc_replace
    client, tmp_path, _ = lab
    backup_sha = "d" * 64
    original_recipe = cp._dossier_source_recipe
    primary_sha = None

    def pooled(publication):
        nonlocal primary_sha
        recipe = original_recipe(publication)
        primary = recipe.masters[0]
        primary_sha = primary.sha256
        backup = dc_replace(primary, source_id="zz-backup", sha256=backup_sha, role="backup",
                            storage_key="source-dna/backup/" + backup_sha + ".mp4")
        return dc_replace(recipe, masters=(primary, backup))

    monkeypatch.setattr(cp, "_dossier_source_recipe", pooled)
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2),
        headers=headers(f"source-job-backup-missing-{status}"),
    )
    job_id = response.json()["jobId"]
    store = cp._load_jobs()
    for index, cut in enumerate(store["jobs"][job_id]["sourceCuts"]):
        start = index * 15_000
        sha = backup_sha if index in backup_cuts else primary_sha
        cut.update({"masterSha256": sha, "slotId": f"{sha}:{start}:{cut['durationMs']}",
                    "libraryStartMs": start, "startMs": start})
    cp.atomic_save(cp._jobs_path(), store)
    source = tmp_path / "master.mp4"
    _write_av_test_clip(source, duration=40.0)

    async def cached_source(page_id, master, job):
        if master.sha256 == backup_sha:
            raise RuntimeError("source_dna_master_unavailable")
        return source

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == status, job.get("error")
    if status == "completed":
        assert len(job["clips"]) == 1
    else:
        assert job["error"] == "source_dna_master_unavailable"


def test_resolve_refuses_a_library_that_still_holds_a_retired_master(caplog):
    """Rule #1: a stale publication naming a library that still holds a retired
    master fails with a named, logged reason instead of resolving the retired
    footage again. The library is a single-primary historical record, so it
    parses; the executor is what refuses it."""
    import services.source_dna_registry as registry

    retired_page = "tt-retired-dirt-bike"
    retired_lib = "pov-dirt-bike-retired-v1"
    retired_sha = "c434bf9678fbaa20b9b081c68260cca75eb3dd109ddc1cb82df556ec59ae5bd5"
    manifest = {
        "schema": "content-lab.source-dna-library.v2",
        "libraryId": retired_lib,
        "format": "pov-dirt-bike",
        "pageId": retired_page,
        "masters": [{
            "sourceId": "youtube-retired-120-300",
            "sha256": retired_sha,
            "bytes": 414027449,
            "filename": "retired_master.mp4",
            "mimeType": "video/mp4",
            "storageKey": f"source-dna/pov-dirt-bike/{retired_page}/{retired_sha}.mp4",
            "durationMs": 180000,
            "sourceOffsetMs": 120000,
            "provenance": {
                "sourceUrl": "https://www.youtube.com/watch?v=vt5im2TRAKw",
                "acquiredAt": "2026-07-29T17:37:41Z",
                "authority": "test fixture: retired footage",
            },
        }],
    }
    (registry.MANIFEST_DIR / f"{retired_lib}.json").write_bytes(
        (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )

    intent, revision = master_pages(
        retired_page, handle="retired.dirt.bike",
        content_niche="POV - Dirtbike", content_engine="sourced_video",
        vault_url="https://shipstream.test/vault/retired.dirt.bike",
    )
    spec = {
        "schema": "dossier.recipe-spec.v4",
        "masterPages": intent,
        "masterPagesHash": revision,
        "production": {
            "catalogVersion": "sha256:" + "f" * 64,
            "providerId": None, "modelId": None, "promptModuleId": None, "referenceSetId": None,
            "sourceLibraryId": retired_lib,
            "variationValues": {},
            "controls": {"cutDurationMs": 6_000},
        },
        "renderTreatment": {
            "stylePreset": None, "filters": {}, "captionStyle": {},
            "clipSpeed": 1.0, "clipCrop": {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5},
        },
        "captionDiscipline": {
            "captionSet": "dirt-bike", "register": ["relatable_meme"], "slingshotShare": None,
        },
        "demand": {"formatMix": {"pov-dirt-bike": 1.0}},
    }
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    payload = {
        "schema": recipes.REQUEST_SCHEMA,
        "pageId": retired_page,
        "lane": recipes.LANE,
        "recipeId": "pov-dirt-bike:master",
        "engine": "sourced_video",
        "recipeVersion": "dossier-retired000000",
        "dossierRevision": "rev-retired-source",
        "recipeSpecHash": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
        "recipeSpecCanonical": canonical,
    }
    with caplog.at_level(logging.WARNING, logger="content_lab.control_plane_sources"):
        assert resolve_source_recipe(payload) is None
    assert any("source_master_retired" in record.message for record in caplog.records)
