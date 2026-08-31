"""Closed contracts for version-bound dossier approved-library execution."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.control_plane as cp
import routers.control_plane_recipes as recipes
from services.content_engine_registry import REGISTRY_PATH
from services.control_plane_sources import resolve_source_recipe
from tests.master_pages_fixtures import bind_current_intent, master_pages


TOKEN = "test-control-plane-token"
PAGE_ID = "tt-dirt-bike"
BASE_RECIPE = {
    "recipeId": "pov-dirt-bike-8791-20260828",
    "engine": "content_lab",
    "recipeVersion": "v32169e75b485",
    "maxQuantity": 20,
}


def publication(
    recipe_id="pov-dirt-bike:master",
    format_slug="pov-dirt-bike",
    *,
    content_niche="POV - Dirtbike",
    clip_speed=1.25,
    include_clip_speed=True,
    clip_crop=None,
    recipe_version="dossier-feedfacefeedface",
    dossier_revision="rev-dirt-bike",
):
    intent, revision = master_pages(
        PAGE_ID, handle="dirt.bike", content_niche=content_niche,
        content_engine="sourced_video", vault_url="https://drive.example/dirt-bike",
    )
    render_treatment = {
        "stylePreset": "warm-coffee",
        "filters": {"brightness": 1.05, "warmth": 0.1},
        "captionStyle": {},
    }
    if include_clip_speed:
        render_treatment["clipSpeed"] = clip_speed
    if clip_crop is not None:
        render_treatment["clipCrop"] = clip_crop
    canonical = json.dumps({
        "schema": "dossier.recipe-spec.v2",
        "masterPages": intent,
        "masterPagesHash": revision,
        "renderTreatment": render_treatment,
        "demand": {"formatMix": {format_slug: 1.0}},
    }, sort_keys=True, separators=(",", ":"))
    return {
        "schema": recipes.REQUEST_SCHEMA,
        "pageId": PAGE_ID,
        "lane": recipes.LANE,
        "recipeId": recipe_id,
        "engine": "sourced_video",
        "recipeVersion": recipe_version,
        "dossierRevision": dossier_revision,
        "recipeSpecHash": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
        "recipeSpecCanonical": canonical,
    }


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


def _write_av_test_clip(path: Path, duration: float = 2.4) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required for the real clip-speed render contract")
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x568:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000",
            "-t", str(duration), "-shortest",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _probe_durations(path: Path) -> tuple[float, dict[str, float]]:
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe is required for the real clip-speed render contract")
    completed = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=codec_type,duration",
            "-of", "json", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    probe = json.loads(completed.stdout)
    streams = {
        stream["codec_type"]: float(stream["duration"])
        for stream in probe["streams"]
        if stream.get("codec_type") in {"video", "audio"}
        and stream.get("duration") is not None
    }
    return float(probe["format"]["duration"]), streams


def _probe_video_size(path: Path) -> tuple[int, int]:
    completed = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "json", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    stream = json.loads(completed.stdout)["streams"][0]
    return int(stream["width"]), int(stream["height"])


@pytest.fixture
def lab(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("CONTENT_LAB_RECIPE_ROOT", str(tmp_path / "recipe-publications"))
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    monkeypatch.setattr(cp, "_generation_root", lambda: tmp_path / "generated")
    intent, revision = master_pages(
        PAGE_ID, handle="dirt.bike", content_niche="POV - Dirtbike",
        content_engine="sourced_video", vault_url="https://drive.example/dirt-bike",
    )
    bind_current_intent(monkeypatch, cp, intent, revision)
    projects = tmp_path / "projects"
    videos = projects / BASE_RECIPE["recipeId"] / "videos"
    videos.mkdir(parents=True)
    for index in range(3):
        (videos / f"clip-{index}.mp4").write_bytes(f"approved-source-{index}".encode())
    monkeypatch.setattr(cp, "PROJECTS_DIR", projects)
    base = dict(BASE_RECIPE)
    monkeypatch.setattr(cp, "_registered_recipes", lambda: [dict(base)])
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
    response = client.post(
        "/api/control-plane/v1/recipes",
        json=publication(),
        headers=headers("source-register-0001"),
    )
    assert response.status_code == 200
    return client, tmp_path, base, started


@pytest.mark.parametrize(
    ("clip_speed", "recipe_version"),
    [
        (0.5, "dossier-0505050505050505"),
        (1.0, "dossier-1010101010101010"),
        (2.0, "dossier-2020202020202020"),
    ],
)
@pytest.mark.asyncio
async def test_source_runner_changes_real_video_and_audio_duration_with_provenance(
    lab, clip_speed, recipe_version,
):
    client, tmp_path, _, _ = lab
    source = (
        tmp_path / "projects" / BASE_RECIPE["recipeId"] / "videos" / "clip-0.mp4"
    )
    _write_av_test_clip(source)
    source_duration, source_streams = _probe_durations(source)
    assert set(source_streams) == {"video", "audio"}

    payload = publication(
        clip_speed=clip_speed,
        recipe_version=recipe_version,
        dossier_revision=f"rev-speed-{clip_speed}",
    )
    registered = client.post(
        "/api/control-plane/v1/recipes",
        json=payload,
        headers=headers(f"source-register-{recipe_version}"),
    )
    assert registered.status_code == 200
    response = client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(1, payload),
        headers=headers(f"source-job-{recipe_version}"),
    )
    assert response.status_code == 200
    job_id = response.json()["jobId"]
    selected = cp._load_jobs()["jobs"][job_id]["sourceClips"][0]

    await cp._run_dossier_source(job_id)

    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed"
    assert len(job["clips"]) == 1
    clip = job["clips"][0]
    output = Path(job["artifactRoot"]) / clip["path"]
    output_duration, output_streams = _probe_durations(output)
    expected_duration = source_duration / clip_speed
    assert output_duration == pytest.approx(expected_duration, abs=0.12)
    assert set(output_streams) == {"video", "audio"}
    assert output_streams["video"] == pytest.approx(expected_duration, abs=0.12)
    assert output_streams["audio"] == pytest.approx(expected_duration, abs=0.12)
    assert clip["clipSpeed"] == pytest.approx(clip_speed)
    assert clip["bytes"] == output.stat().st_size > 0
    assert clip["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert clip["sha256"] != selected["sha256"]
    assert clip["source"] == {
        "recipeId": BASE_RECIPE["recipeId"],
        "recipeVersion": BASE_RECIPE["recipeVersion"],
        "path": selected["path"],
        "sha256": selected["sha256"],
        "bytes": selected["bytes"],
        "pageId": PAGE_ID,
        "masterPagesHash": master_pages(
            PAGE_ID, handle="dirt.bike", content_niche="POV - Dirtbike",
            content_engine="sourced_video", vault_url="https://drive.example/dirt-bike",
        )[1],
        "contentNiche": "POV - Dirtbike",
        "contentEngine": "sourced_video",
        "vaultUrl": "https://drive.example/dirt-bike",
    }


@pytest.mark.asyncio
async def test_source_runner_defaults_legacy_clip_speed_omission_to_real_time(lab):
    client, tmp_path, _, _ = lab
    source = (
        tmp_path / "projects" / BASE_RECIPE["recipeId"] / "videos" / "clip-0.mp4"
    )
    _write_av_test_clip(source)
    source_duration, _ = _probe_durations(source)
    payload = publication(
        include_clip_speed=False,
        recipe_version="dossier-deaddeaddeaddead",
        dossier_revision="rev-speed-legacy",
    )
    assert client.post(
        "/api/control-plane/v1/recipes",
        json=payload,
        headers=headers("source-register-speed-legacy"),
    ).status_code == 200
    response = client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(1, payload),
        headers=headers("source-job-speed-legacy"),
    )
    assert response.status_code == 200
    job_id = response.json()["jobId"]

    await cp._run_dossier_source(job_id)

    job = cp._load_jobs()["jobs"][job_id]
    clip = job["clips"][0]
    output = Path(job["artifactRoot"]) / clip["path"]
    output_duration, _ = _probe_durations(output)
    assert clip["clipSpeed"] == pytest.approx(1.0)
    assert output_duration == pytest.approx(source_duration, abs=0.12)


@pytest.mark.asyncio
async def test_source_runner_renders_crop_and_records_exact_transform(lab):
    client, tmp_path, _, _ = lab
    source = (
        tmp_path / "projects" / BASE_RECIPE["recipeId"] / "videos" / "clip-0.mp4"
    )
    _write_av_test_clip(source)
    crop = {"zoom": 1.75, "focusX": 0.2, "focusY": 0.8}
    payload = publication(
        clip_crop=crop,
        recipe_version="dossier-cropcropcrop01",
        dossier_revision="rev-crop-1",
    )
    assert client.post(
        "/api/control-plane/v1/recipes", json=payload,
        headers=headers("source-register-crop"),
    ).status_code == 200
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(1, payload),
        headers=headers("source-job-crop"),
    )
    assert response.status_code == 200
    job_id = response.json()["jobId"]

    await cp._run_dossier_source(job_id)

    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed"
    clip = job["clips"][0]
    output = Path(job["artifactRoot"]) / clip["path"]
    assert _probe_video_size(output) == (1080, 1920)
    assert clip["clipCrop"] == crop


@pytest.mark.parametrize(
    "invalid",
    [0.49, 2.01, True, "fast", float("nan"), float("inf")],
)
def test_source_recipe_fails_closed_for_invalid_clip_speed(invalid):
    assert resolve_source_recipe(
        publication(clip_speed=invalid),
        base_recipe_lookup=lambda _: dict(BASE_RECIPE),
    ) is None


@pytest.mark.parametrize(
    "invalid",
    [
        {"zoom": 0.99, "focusX": 0.5, "focusY": 0.5},
        {"zoom": 3.01, "focusX": 0.5, "focusY": 0.5},
        {"zoom": 1.0, "focusX": -0.01, "focusY": 0.5},
        {"zoom": 1.0, "focusX": 0.5, "focusY": 1.01},
        {"zoom": 1.0, "focusX": 0.5},
    ],
)
def test_source_recipe_fails_closed_for_invalid_clip_crop(invalid):
    assert resolve_source_recipe(
        publication(clip_crop=invalid),
        base_recipe_lookup=lambda _: dict(BASE_RECIPE),
    ) is None


def test_source_recipe_requires_exact_mapping_typed_format_and_live_base_version():
    payload = publication()
    resolved = resolve_source_recipe(
        payload,
        base_recipe_lookup=lambda _: dict(BASE_RECIPE),
    )
    assert resolved is not None
    assert resolved.base_recipe_id == "pov-dirt-bike-8791-20260828"
    assert resolved.base_recipe_version == "v32169e75b485"
    assert resolved.served_ledger_key.endswith(":v32169e75b485")

    drifted = {**BASE_RECIPE, "recipeVersion": "v-drifted"}
    assert resolve_source_recipe(payload, base_recipe_lookup=lambda _: drifted) is None
    assert resolve_source_recipe(
        publication("coffee-tok:master", "coffee-tok"),
        base_recipe_lookup=lambda _: dict(BASE_RECIPE),
    ) is None


@pytest.mark.parametrize(
    (
        "recipe_id", "format_slug", "content_niche",
        "base_recipe_id", "base_recipe_version",
    ),
    [
        (
            "pov-dirt-bike:master",
            "pov-dirt-bike",
            "POV - Dirtbike",
            "pov-dirt-bike-8791-20260828",
            "v32169e75b485",
        ),
    ],
)
def test_new_source_executors_require_the_exact_approved_library_version(
    recipe_id, format_slug, content_niche, base_recipe_id, base_recipe_version,
):
    payload = publication(recipe_id, format_slug, content_niche=content_niche)
    base = {
        "recipeId": base_recipe_id,
        "engine": "content_lab",
        "recipeVersion": base_recipe_version,
        "maxQuantity": 20,
    }
    resolved = resolve_source_recipe(
        payload,
        base_recipe_lookup=lambda requested: dict(base)
        if requested == base_recipe_id
        else None,
    )
    assert resolved is not None
    assert resolved.base_recipe_id == base_recipe_id
    assert resolved.base_recipe_version == base_recipe_version

    drifted = {**base, "recipeVersion": "v-drifted"}
    assert resolve_source_recipe(
        payload,
        base_recipe_lookup=lambda _: drifted,
    ) is None
    assert resolve_source_recipe(
        payload,
        base_recipe_lookup=lambda _: None,
    ) is None


def test_night_core_rejects_the_visually_invalid_truck_library():
    payload = publication(
        "pov-night-core:master",
        "pov-night-core",
        content_niche="POV — Night Core",
    )
    mislabeled_truck_library = {
        "recipeId": "between-the-lines-nightcore-pov",
        "engine": "content_lab",
        "recipeVersion": "v886fe1b646f3",
        "maxQuantity": 20,
    }
    assert resolve_source_recipe(
        payload,
        base_recipe_lookup=lambda _: mislabeled_truck_library,
    ) is None


@pytest.mark.parametrize(
    ("recipe_id", "format_slug", "content_niche", "base_recipe_id", "version"),
    [
        (
            "coffee-tok:master", "coffee-tok", "Coffee",
            "brewpilled-coffee", "v62173591b07a",
        ),
        (
            "pov-night-core:master", "pov-night-core", "POV — Night Core",
            "between-the-lines-nightcore-pov", "v886fe1b646f3",
        ),
        (
            "pov-scenic:master", "pov-scenic", "POV — Scenic",
            "between-the-lines-nightcore-pov", "v886fe1b646f3",
        ),
    ],
)
def test_uncommissioned_source_families_remain_unavailable(
    recipe_id, format_slug, content_niche, base_recipe_id, version,
):
    payload = publication(recipe_id, format_slug, content_niche=content_niche)
    base = {
        "recipeId": base_recipe_id,
        "engine": "content_lab",
        "recipeVersion": version,
        "maxQuantity": 20,
    }
    assert resolve_source_recipe(payload, base_recipe_lookup=lambda _: base) is None


def test_capability_and_job_bind_exact_source_version_and_select_without_replacement(lab):
    client, _, _, started = lab
    capabilities = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID},
    ).json()["capabilities"]
    expected = publication()
    assert {
        "recipeId": expected["recipeId"],
        "engine": expected["engine"],
        "recipeVersion": expected["recipeVersion"],
        "maxQuantity": 10,
    } in capabilities

    response = client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(2),
        headers=headers("source-job-0001"),
    )
    assert response.status_code == 200
    job_id = response.json()["jobId"]
    assert started == [job_id]
    job = cp._load_jobs()["jobs"][job_id]
    assert job["sourceKind"] == "dossier_approved_library"
    assert job["baseRecipeId"] == BASE_RECIPE["recipeId"]
    assert job["baseRecipeVersion"] == BASE_RECIPE["recipeVersion"]
    assert job["engineRegistryHash"] == hashlib.sha256(
        REGISTRY_PATH.read_bytes(),
    ).hexdigest()
    assert job["sourceManifestHash"] == (
        "32169e75b485f89b20e79f8a78df968aff490cb256adbbd358cbc13f9e35106e"
    )
    assert job["materialSource"] == "source_library"
    assert job["assetType"] == "video/mp4"
    assert len(job["sourceClips"]) == 2
    assert all(clip["sha256"] for clip in job["sourceClips"])

    exhausted = client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(2),
        headers=headers("source-job-0002"),
    )
    assert exhausted.status_code == 409
    assert exhausted.json()["detail"] == "insufficient_inventory"


@pytest.mark.asyncio
async def test_source_runner_always_treats_into_isolated_root_with_provenance(lab, monkeypatch):
    client, tmp_path, _, _ = lab
    response = client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(2),
        headers=headers("source-job-0003"),
    )
    job_id = response.json()["jobId"]
    calls = []

    async def fake_color_correct(
        source, destination, correction, scale=None, playback_speed=1.0,
        clip_crop=None,
    ):
        calls.append((source, destination, correction, playback_speed, clip_crop))
        shutil.copyfile(source, destination)

    monkeypatch.setattr(cp, "run_color_correct", fake_color_correct)
    await cp._run_dossier_source(job_id)

    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed"
    assert len(calls) == 2
    assert all(call[3] == pytest.approx(1.25) for call in calls)
    assert len(job["clips"]) == 2
    root = (tmp_path / "generated").resolve()
    for clip in job["clips"]:
        output = Path(job["artifactRoot"]) / clip["path"]
        assert root in output.resolve().parents
        assert clip["source"]["recipeId"] == BASE_RECIPE["recipeId"]
        assert clip["source"]["recipeVersion"] == BASE_RECIPE["recipeVersion"]
        assert clip["source"]["sha256"]
        assert clip["sha256"]

    response = client.get(
        f"/api/control-plane/v1/jobs/{job_id}/artifacts",
        headers={"Authorization": f"Bearer {TOKEN}", "X-RT-Page-Id": PAGE_ID},
    )
    assert response.status_code == 200
    artifacts = response.json()["artifacts"]
    assert len(artifacts) == 2
    for artifact, clip in zip(artifacts, job["clips"], strict=True):
        assert artifact["sha256"] == clip["sha256"]
        assert artifact["bytes"] == clip["bytes"]
        assert artifact["source"] == clip["source"]


@pytest.mark.asyncio
async def test_source_runner_fails_closed_when_selected_library_bytes_change(lab, monkeypatch):
    client, tmp_path, _, _ = lab
    response = client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(1),
        headers=headers("source-job-mutated"),
    )
    job_id = response.json()["jobId"]
    selected = cp._load_jobs()["jobs"][job_id]["sourceClips"][0]
    source = (
        tmp_path / "projects" / BASE_RECIPE["recipeId"] / "videos" / selected["path"]
    )
    source.write_bytes(b"changed-after-hash-pinned-selection")

    calls = []

    async def fake_color_correct(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(cp, "run_color_correct", fake_color_correct)
    await cp._run_dossier_source(job_id)

    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "failed"
    assert job["error"] == "source_recipe_artifact_changed"
    assert job["clips"] == []
    assert calls == []


def test_base_version_drift_withdraws_capability_and_refuses_job(lab):
    client, _, base, _ = lab
    base["recipeVersion"] = "v-drifted"
    capabilities = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID},
    ).json()["capabilities"]
    assert all(entry["recipeId"] != "coffee-tok:master" for entry in capabilities)
    response = client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(1),
        headers=headers("source-job-drift"),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "recipe_executor_unavailable"


def test_source_manifest_refuses_a_symlink_that_escapes_the_approved_library(lab):
    _, tmp_path, _, _ = lab
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"not-approved-library-bytes")
    videos = tmp_path / "projects" / BASE_RECIPE["recipeId"] / "videos"
    (videos / "escape.mp4").symlink_to(outside)
    assert cp._clip_manifest(BASE_RECIPE["recipeId"], "escape.mp4") is None
