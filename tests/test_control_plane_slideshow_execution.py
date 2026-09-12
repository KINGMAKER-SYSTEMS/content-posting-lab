"""Closed contracts for hash-bound Syzygy/R2 slideshow execution."""

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.control_plane as cp
import routers.control_plane_recipes as recipes
import services.dossier_ingredients as ingredients_module
from services.control_plane_slideshows import (
    SyzygyLibrary,
    SyzygyObject,
    plan_slideshows,
    resolve_slideshow_recipe,
)
from services.dossier_ingredients import (
    build_dossier_ingredient_catalog,
    catalog_selection_version,
)
from tests.master_pages_fixtures import bind_current_intent, master_pages


TOKEN = "test-control-plane-token"
PAGE_ID = "tt-holy-fumble"
HANDLE = "holy.fumble"
SUBJECT = "holy-fumble"
LANE = "meme"
FORMAT = "meme-slideshow"
ENGINE = "sourced_slideshow"
NICHE = "meme"
LIBRARY_ID = f"syzygy:{LANE}:{SUBJECT}"
RECIPE_ID = f"{FORMAT}:master"
RENDERER_REVISION = "d" * 64


def _library(count: int = 5) -> SyzygyLibrary:
    objects = tuple(sorted(
        (
            SyzygyObject(
                key=f"{LANE}/{SUBJECT}/photos/img-{index:02d}.jpg",
                bytes=1_000 + index,
                url=f"https://r2.example/{LANE}/{SUBJECT}/photos/img-{index:02d}.jpg",
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
        library_id=LIBRARY_ID,
        lane=LANE,
        subject=SUBJECT,
        objects=objects,
        snapshot_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def publication(
    monkeypatch,
    *,
    clip_speed=1.0,
    clip_crop=None,
    recipe_version="dossier-slideshow0000",
    schema="dossier.recipe-spec.v4",
    library=None,
):
    library = library if library is not None else _library()
    monkeypatch.setattr(
        ingredients_module,
        "load_syzygy_library",
        lambda *_args, **_kwargs: library,
    )
    intent, revision = master_pages(
        PAGE_ID,
        handle=HANDLE,
        content_niche=NICHE,
        content_engine=ENGINE,
        vault_url=f"https://shipstream.risingtidesviral.com/vault/{HANDLE}",
    )
    catalog = build_dossier_ingredient_catalog(PAGE_ID, intent, revision)
    production = {
        "catalogVersion": "",
        "providerId": None,
        "modelId": None,
        "promptModuleId": None,
        "referenceSetId": None,
        "sourceLibraryId": LIBRARY_ID,
        "variationValues": {},
        "controls": {},
    }
    version = catalog_selection_version(catalog, FORMAT, production)
    assert version is not None
    production["catalogVersion"] = version
    recipe_spec = {
        "schema": schema,
        "masterPages": intent,
        "masterPagesHash": revision,
        "production": production,
        "renderTreatment": {
            "stylePreset": None,
            "filters": {},
            "captionStyle": {},
            "clipSpeed": clip_speed,
            "clipCrop": clip_crop or {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5},
        },
        "demand": {"formatMix": {FORMAT: 1.0}},
    }
    if schema == "dossier.recipe-spec.v4":
        recipe_spec["captionDiscipline"] = {
            "captionSet": "meme",
            "register": ["relatable_meme"],
            "slingshotShare": None,
        }
    canonical = json.dumps(recipe_spec, sort_keys=True, separators=(",", ":"))
    return {
        "schema": recipes.REQUEST_SCHEMA,
        "pageId": PAGE_ID,
        "lane": recipes.LANE,
        "recipeId": RECIPE_ID,
        "engine": ENGINE,
        "recipeVersion": recipe_version,
        "dossierRevision": "rev-slideshow",
        "recipeSpecHash": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
        "recipeSpecCanonical": canonical,
    }


def headers(idempotency="slideshow-job-0001"):
    return {
        "Authorization": f"Bearer {TOKEN}",
        "X-RT-Lane": recipes.LANE,
        "X-RT-Page-Id": PAGE_ID,
        "Idempotency-Key": idempotency,
    }


def job_body(quantity=2, payload=None):
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


@pytest.fixture
def lab(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("CONTENT_LAB_RECIPE_ROOT", str(tmp_path / "recipes"))
    monkeypatch.setenv("CONTENT_LAB_GENERATION_MODE", "ready")
    monkeypatch.setenv("SYZYGY_API_URL", "https://syzygy.example")
    monkeypatch.setenv("SYZYGY_API_KEY", "syz_test_key")
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    monkeypatch.setattr(cp, "_generation_root", lambda: tmp_path / "generated")
    library = _library()
    monkeypatch.setattr(
        cp, "load_recipe_library", lambda *_args, **_kwargs: library,
    )
    monkeypatch.setattr(
        cp, "load_syzygy_library", lambda *_args, **_kwargs: library,
    )
    intent, revision = master_pages(
        PAGE_ID,
        handle=HANDLE,
        content_niche=NICHE,
        content_engine=ENGINE,
        vault_url=f"https://shipstream.risingtidesviral.com/vault/{HANDLE}",
    )
    bind_current_intent(monkeypatch, cp, intent, revision)
    started = []
    monkeypatch.setattr(cp, "_start_syzygy_slideshow", started.append)

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
    payload = publication(monkeypatch)
    registered = client.post(
        "/api/control-plane/v1/recipes",
        json=payload,
        headers=headers("slideshow-register-0001"),
    )
    assert registered.status_code == 200, registered.json()
    return client, tmp_path, started, library, payload


def test_slideshow_recipe_resolves_only_a_full_pinned_publication(monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_GENERATION_MODE", "ready")
    monkeypatch.setenv("SYZYGY_API_URL", "https://syzygy.example")
    monkeypatch.setenv("SYZYGY_API_KEY", "syz_test_key")
    payload = publication(monkeypatch)
    resolved = resolve_slideshow_recipe(payload)
    assert resolved is not None
    assert resolved.recipe_id == RECIPE_ID
    assert resolved.library_id == LIBRARY_ID
    assert resolved.engine == ENGINE
    assert resolved.encode_preset == "tiktok_delivery_v1"
    assert (resolved.output_width, resolved.output_height) == (1080, 1920)
    assert [template.slug for template in resolved.templates] == [
        "digital-postcard-hook", "clean-slideshow", "mirror-split",
    ]

    spec = json.loads(payload["recipeSpecCanonical"])
    spec["production"]["sourceLibraryId"] = "syzygy:meme:another-page"
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    payload["recipeSpecCanonical"] = canonical
    payload["recipeSpecHash"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    assert resolve_slideshow_recipe(payload) is None
    assert resolve_slideshow_recipe({**payload, "engine": "ai_video"}) is None


def test_slideshow_recipe_requires_the_runtime(monkeypatch):
    monkeypatch.delenv("CONTENT_LAB_GENERATION_MODE", raising=False)
    monkeypatch.delenv("SYZYGY_API_URL", raising=False)
    monkeypatch.delenv("SYZYGY_API_KEY", raising=False)
    payload = publication(monkeypatch)
    assert resolve_slideshow_recipe(payload) is None
    assert resolve_slideshow_recipe(payload, require_runtime=False) is not None


def test_slideshow_planner_is_deterministic_fresh_and_respects_reservations(
    monkeypatch,
):
    recipe = resolve_slideshow_recipe(
        publication(monkeypatch), require_runtime=False,
    )
    assert recipe is not None
    library = _library()
    first = plan_slideshows(recipe, library, 3, set(), "run-a")
    repeat = plan_slideshows(recipe, library, 3, set(), "run-a")
    assert first == repeat
    assert len(first) == 3
    assert len({plan["signature"] for plan in first}) == 3
    reserved = {first[0]["signature"]}
    second = plan_slideshows(recipe, library, 3, reserved, "run-a")
    assert reserved.isdisjoint({plan["signature"] for plan in second})
    for plan in first:
        template_counts = {
            "digital-postcard-hook": 5, "clean-slideshow": 5, "mirror-split": 5,
        }
        assert len(plan["mediaKeys"]) == template_counts[plan["templateSlug"]]
        assert set(plan["mediaKeys"]) <= {item.key for item in library.objects}
        assert plan["librarySnapshotHash"] == library.snapshot_hash


def test_slideshow_planner_returns_nothing_when_fresh_plans_are_exhausted(
    monkeypatch,
):
    recipe = resolve_slideshow_recipe(
        publication(monkeypatch), require_runtime=False,
    )
    assert recipe is not None
    library = _library()
    every = plan_slideshows(recipe, library, 10_000, set(), "run-a")
    exhausted = plan_slideshows(
        recipe, library, 1, {plan["signature"] for plan in every}, "run-a",
    )
    assert exhausted == []
    undersized = _library(count=4)
    assert plan_slideshows(recipe, undersized, 1, set(), "run-a") == []


def test_capability_and_jobs_bind_master_hash_and_reserve_unique_plans(lab):
    client, _, started, library, payload = lab
    capabilities = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID},
    ).json()["capabilities"]
    row = next(item for item in capabilities if item["recipeId"] == RECIPE_ID)
    assert row["engine"] == ENGINE
    assert row["maxQuantity"] > 0

    first = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2, payload),
        headers=headers("slideshow-job-first"),
    )
    assert first.status_code == 200, first.json()
    first_job = cp._load_jobs()["jobs"][first.json()["jobId"]]
    assert first_job["sourceKind"] == "syzygy_slideshow"
    assert first_job["sourceLibraryId"] == LIBRARY_ID
    assert first_job["librarySnapshotHash"] == library.snapshot_hash
    assert len(first_job["slideshowPlan"]) == 2
    bytes_by_key = {item.key: item.bytes for item in library.objects}
    for plan in first_job["slideshowPlan"]:
        assert [item["objectKey"] for item in plan["media"]] == plan["mediaKeys"]
        assert all(
            item["bytes"] == bytes_by_key[item["objectKey"]]
            for item in plan["media"]
        )
        expected = hashlib.sha256(json.dumps(
            {"templateSlug": plan["templateSlug"], "mediaKeys": plan["mediaKeys"]},
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        assert plan["signature"] == expected

    second = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2, payload),
        headers=headers("slideshow-job-second"),
    )
    assert second.status_code == 200
    second_job = cp._load_jobs()["jobs"][second.json()["jobId"]]
    first_signatures = {plan["signature"] for plan in first_job["slideshowPlan"]}
    second_signatures = {plan["signature"] for plan in second_job["slideshowPlan"]}
    assert first_signatures.isdisjoint(second_signatures)
    assert len(started) == 2


def test_slideshow_admission_returns_409_when_fresh_inventory_is_exhausted(lab):
    client, _, _, _, payload = lab
    created = 0
    for index in range(6):
        response = client.post(
            "/api/control-plane/v1/jobs", json=job_body(10, payload),
            headers=headers(f"slideshow-fill-{index:04d}"),
        )
        assert response.status_code == 200, response.json()
        cp._update_job(response.json()["jobId"], status="completed")
        created += 10
    assert created == 60
    exhausted = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1, payload),
        headers=headers("slideshow-overflow-0001"),
    )
    assert exhausted.status_code == 409
    assert exhausted.json()["detail"] == "slideshow_inventory_exhausted"
    capabilities = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID},
    ).json()["capabilities"]
    row = next(item for item in capabilities if item["recipeId"] == RECIPE_ID)
    assert row["maxQuantity"] == 0


def test_failed_slideshow_job_releases_its_plan_signatures(lab):
    client, _, _, _, payload = lab
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(3, payload),
        headers=headers("slideshow-release-0001"),
    )
    assert response.status_code == 200
    job_id = response.json()["jobId"]
    job = cp._load_jobs()["jobs"][job_id]
    recipe = resolve_slideshow_recipe(payload)
    signatures = {plan["signature"] for plan in job["slideshowPlan"]}
    store = cp._load_jobs()
    assert signatures <= cp._slideshow_unavailable_signatures(
        store, recipe, PAGE_ID,
    )
    cp._update_job(job_id, status="failed", error="syzygy_render_failed")
    assert signatures.isdisjoint(cp._slideshow_unavailable_signatures(
        cp._load_jobs(), recipe, PAGE_ID,
    ))
    cp._update_job(job_id, status="completed")
    assert signatures <= cp._slideshow_unavailable_signatures(
        cp._load_jobs(), recipe, PAGE_ID,
    )


def _fake_syzygy_boundary(monkeypatch, library, *, mutate=False):
    observations = {
        item.key: {
            "objectKey": item.key,
            "bytes": item.bytes,
            "etag": f'"etag-{item.key.rsplit("/", 1)[-1]}"',
            "sha256": hashlib.sha256(item.key.encode()).hexdigest(),
        }
        for item in library.objects
    }
    calls = {"observe": 0, "submit": [], "download": []}

    async def fake_revision():
        return RENDERER_REVISION

    async def fake_observe(_library, keys):
        calls["observe"] += 1
        result = []
        for key in keys:
            row = dict(observations[key])
            if mutate and calls["observe"] % 2 == 0:
                row["sha256"] = "0" * 64
            result.append(row)
        return result

    async def fake_submit(plan, *, page_id, recipe_version):
        calls["submit"].append((plan, page_id, recipe_version))
        return f"render-job-{len(calls['submit']):04d}", "draft"

    async def fake_poll(render_job_id):
        return f"/render-file/{render_job_id}.mp4"

    async def fake_download(play_url, destination):
        calls["download"].append(play_url)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"raw-render")

    async def fake_render(src, dst, correction, **kwargs):
        # A real render differs per plan; derive bytes from the artifact name
        # so the duplicate-output guard is not tripped by the fake itself.
        Path(dst).write_bytes(b"derived:" + Path(dst).name.encode())

    monkeypatch.setattr(cp, "syzygy_revision", fake_revision)
    monkeypatch.setattr(cp, "observe_library_media", fake_observe)
    monkeypatch.setattr(cp, "submit_syzygy_render", fake_submit)
    monkeypatch.setattr(cp, "poll_syzygy_render", fake_poll)
    monkeypatch.setattr(cp, "download_syzygy_artifact", fake_download)
    monkeypatch.setattr(cp, "run_color_correct", fake_render)
    return calls


@pytest.mark.asyncio
async def test_runner_completes_with_strict_syzygy_provenance(lab, monkeypatch):
    client, tmp_path, _, library, payload = lab
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(2, payload),
        headers=headers("slideshow-runner-0001"),
    )
    assert response.status_code == 200
    job_id = response.json()["jobId"]
    calls = _fake_syzygy_boundary(monkeypatch, library)
    await cp._run_syzygy_slideshow(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert len(job["clips"]) == 2
    plans = {plan["signature"]: plan for plan in job["slideshowPlan"]}
    spec = json.loads(payload["recipeSpecCanonical"])
    for index, clip in enumerate(job["clips"]):
        source = clip["source"]
        plan = plans[source["planSignature"]]
        assert source["schema"] == "content-lab.syzygy-slideshow-source.v1"
        assert source["kind"] == "syzygy_slideshow"
        assert source["recipeId"] == RECIPE_ID
        assert source["recipeVersion"] == payload["recipeVersion"]
        assert source["pageId"] == PAGE_ID
        assert source["masterPagesHash"] == spec["masterPagesHash"]
        assert source["contentNiche"] == NICHE
        assert source["contentEngine"] == ENGINE
        assert source["vaultUrl"] == spec["masterPages"]["vaultUrl"]
        assert source["sourceLibraryId"] == LIBRARY_ID
        assert source["librarySnapshotHash"] == plan["librarySnapshotHash"]
        assert source["templateSlug"] == plan["templateSlug"]
        assert source["rendererRevision"] == RENDERER_REVISION
        assert source["renderJobId"] == f"render-job-{index + 1:04d}"
        assert [row["objectKey"] for row in source["media"]] == plan["mediaKeys"]
        assert all(
            row["bytes"] > 0 and len(row["sha256"]) == 64
            for row in source["media"]
        )
        assert clip["clipSpeed"] == 1.0
        assert clip["clipCrop"] == {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5}
        treatment = clip["sourceTreatment"]
        assert treatment["sourceSha256"] == clip["sha256"]
        assert treatment["generationJobId"] == job_id
        assert treatment["recipeSpecHash"] == payload["recipeSpecHash"]
        assert treatment["visualTreatment"]["clipSpeed"] == 1.0
        assert treatment["visualTreatment"]["clipCrop"] == {
            "zoom": 1.0, "focusX": 0.5, "focusY": 0.5,
        }
        assert clip["thumbnail"]["sha256"]
        output = Path(job["artifactRoot"]) / clip["path"]
        assert output.read_bytes() == b"derived:" + output.name.encode()
    assert calls["observe"] == 4  # before + after for each of two plans
    assert len(calls["submit"]) == 2
    assert len(calls["download"]) == 2
    artifacts = client.get(
        f"/api/control-plane/v1/jobs/{job_id}/artifacts",
        headers={"Authorization": f"Bearer {TOKEN}", "X-RT-Page-Id": PAGE_ID},
    )
    assert artifacts.status_code == 200
    assert [artifact["sourceTreatment"] for artifact in artifacts.json()["artifacts"]] == [
        clip["sourceTreatment"] for clip in job["clips"]
    ]


@pytest.mark.asyncio
async def test_runner_passes_exact_speed_crop_and_encode_to_render(lab, monkeypatch):
    client, tmp_path, _, library, _ = lab
    crop = {"zoom": 1.5, "focusX": 0.2, "focusY": 0.8}
    payload = publication(
        monkeypatch,
        clip_speed=0.75,
        clip_crop=crop,
        recipe_version="dossier-slidecrop000",
    )
    registered = client.post(
        "/api/control-plane/v1/recipes", json=payload,
        headers=headers("slideshow-register-crop"),
    )
    assert registered.status_code == 200, registered.json()
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1, payload),
        headers=headers("slideshow-job-crop"),
    )
    assert response.status_code == 200
    calls = _fake_syzygy_boundary(monkeypatch, library)
    render_calls = []

    async def recording_render(src, dst, correction, **kwargs):
        render_calls.append((src, dst, correction, kwargs))
        Path(dst).write_bytes(b"derived")

    monkeypatch.setattr(cp, "run_color_correct", recording_render)
    await cp._run_syzygy_slideshow(response.json()["jobId"])
    job = cp._load_jobs()["jobs"][response.json()["jobId"]]
    assert job["status"] == "completed", job.get("error")
    assert render_calls[0][3] == {
        "scale": None,
        "encode_args": cp.delivery_encode_args("tiktok_delivery_v1"),
        "playback_speed": 0.75,
        "clip_crop": crop,
        "clip_crop_size": (1080, 1920),
    }
    assert Path(job["artifactRoot"]) in Path(render_calls[0][1]).parents


@pytest.mark.asyncio
async def test_queued_slideshow_job_fails_before_media_work_when_strategy_changes(
    lab, monkeypatch,
):
    client, _, _, _, payload = lab
    queued = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1, payload),
        headers=headers("slideshow-strategy-0001"),
    )
    assert queued.status_code == 200
    prior = job_body(1, payload)["masterPages"]
    changed = {**prior, "contentNiche": "TRUCK", "contentEngine": "ai_video"}
    bind_current_intent(monkeypatch, cp, changed, recipes.intent_hash(changed))

    async def fail_submit(*_args, **_kwargs):
        raise AssertionError("stale strategy reached the Syzygy boundary")

    monkeypatch.setattr(cp, "submit_syzygy_render", fail_submit)
    await cp._run_syzygy_slideshow(queued.json()["jobId"])
    stored = cp._load_jobs()["jobs"][queued.json()["jobId"]]
    assert stored["status"] == "failed"
    assert stored["error"] == "master_pages_strategy_changed"


@pytest.mark.asyncio
async def test_runner_rejects_source_mutation_during_render(lab, monkeypatch):
    client, _, _, library, payload = lab
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1, payload),
        headers=headers("slideshow-mutation-0001"),
    )
    assert response.status_code == 200
    _fake_syzygy_boundary(monkeypatch, library, mutate=True)
    await cp._run_syzygy_slideshow(response.json()["jobId"])
    stored = cp._load_jobs()["jobs"][response.json()["jobId"]]
    assert stored["status"] == "failed"
    assert stored["error"] == "slideshow_source_mutated_during_render"


@pytest.mark.asyncio
async def test_runner_rejects_duplicate_output_bytes_from_an_earlier_job(
    lab, monkeypatch,
):
    client, tmp_path, _, library, payload = lab
    first = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1, payload),
        headers=headers("slideshow-duplicate-first"),
    )
    assert first.status_code == 200
    first_id = first.json()["jobId"]
    duplicate_sha = hashlib.sha256(b"derived:slideshow-0000.mp4").hexdigest()
    cp._update_job(
        first_id,
        status="completed",
        clips=[{
            "path": "treated/slideshow-0000.mp4",
            "sha256": duplicate_sha,
            "bytes": 7,
            "source": {"planSignature": "a" * 64},
        }],
    )
    second = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1, payload),
        headers=headers("slideshow-duplicate-second"),
    )
    assert second.status_code == 200
    _fake_syzygy_boundary(monkeypatch, library)
    await cp._run_syzygy_slideshow(second.json()["jobId"])
    stored = cp._load_jobs()["jobs"][second.json()["jobId"]]
    assert stored["status"] == "failed"
    assert stored["error"] == "duplicate_slideshow_artifact"


@pytest.mark.asyncio
async def test_runner_rejects_a_plan_whose_library_object_changed(
    lab, monkeypatch,
):
    client, _, _, library, payload = lab
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1, payload),
        headers=headers("slideshow-drift-0001"),
    )
    assert response.status_code == 200
    job_id = response.json()["jobId"]
    changed = _library()
    drifted = tuple(
        SyzygyObject(item.key, item.bytes + 1, item.url)
        if index == 0 else item
        for index, item in enumerate(changed.objects)
    )
    object.__setattr__(changed, "objects", drifted)
    monkeypatch.setattr(
        cp, "load_recipe_library", lambda *_args, **_kwargs: changed,
    )
    _fake_syzygy_boundary(monkeypatch, changed)

    async def fail_submit(*_args, **_kwargs):
        raise AssertionError("a drifted library object reached the renderer")

    monkeypatch.setattr(cp, "submit_syzygy_render", fail_submit)
    await cp._run_syzygy_slideshow(job_id)
    stored = cp._load_jobs()["jobs"][job_id]
    assert stored["status"] == "failed"
    assert stored["error"] == "slideshow_library_object_changed"


def test_slideshow_paths_never_probe_the_ai_video_resolver(lab, monkeypatch):
    client, _, started, _, payload = lab

    def unexpected_generation_probe(_publication):
        raise AssertionError("sourced_slideshow must not probe the ai_video resolver")

    monkeypatch.setattr(cp, "resolve_generation_recipe", unexpected_generation_probe)
    capabilities = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID},
    )
    assert capabilities.status_code == 200
    created = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1, payload),
        headers=headers("slideshow-dispatch-0001"),
    )
    assert created.status_code == 200
    assert len(started) == 1


@pytest.mark.asyncio
async def test_cancelled_runner_fails_the_job_and_releases_its_reservation(
    lab, monkeypatch,
):
    client, _, _, library, payload = lab
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1, payload),
        headers=headers("slideshow-cancel-0001"),
    )
    assert response.status_code == 200
    job_id = response.json()["jobId"]
    _fake_syzygy_boundary(monkeypatch, library)

    async def cancelled_submit(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(cp, "submit_syzygy_render", cancelled_submit)
    with pytest.raises(asyncio.CancelledError):
        await cp._run_syzygy_slideshow(job_id)
    stored = cp._load_jobs()["jobs"][job_id]
    assert stored["status"] == "failed"
    assert stored["error"] == "slideshow_render_cancelled"
    recipe = resolve_slideshow_recipe(payload)
    assert cp._slideshow_unavailable_signatures(
        cp._load_jobs(), recipe, PAGE_ID,
    ) == set()
