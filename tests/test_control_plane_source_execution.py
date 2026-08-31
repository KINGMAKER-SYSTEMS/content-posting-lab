"""Closed contracts for page-scoped immutable-master recut execution."""

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
from services.control_plane_sources import CUT_SLOT_STEP_MS, resolve_source_recipe
from services.dossier_ingredients import build_dossier_ingredient_catalog
from tests.master_pages_fixtures import bind_current_intent, master_pages


TOKEN = "test-control-plane-token"
PAGE_ID = "tt-chase-miles-4l"
LIBRARY_ID = "pov-dirt-bike-chase-miles-4l-v1"
MASTER_SHA = "c434bf9678fbaa20b9b081c68260cca75eb3dd109ddc1cb82df556ec59ae5bd5"


def publication(
    *,
    clip_speed=1.0,
    clip_crop=None,
    cut_duration_ms=6_000,
    source_library_id=LIBRARY_ID,
    recipe_version="dossier-feedfacefeedface",
):
    intent, revision = master_pages(
        PAGE_ID,
        handle="chase.miles.4l",
        content_niche="POV - Dirtbike",
        content_engine="sourced_video",
        vault_url="https://shipstream.risingtidesviral.com/vault/chase.miles.4l",
    )
    catalog = build_dossier_ingredient_catalog(PAGE_ID, intent, revision)
    render_treatment = {
        "stylePreset": None,
        "filters": {},
        "captionStyle": {},
        "clipSpeed": clip_speed,
        "clipCrop": clip_crop or {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5},
    }
    canonical = json.dumps({
        "schema": "dossier.recipe-spec.v3",
        "masterPages": intent,
        "masterPagesHash": revision,
        "production": {
            "catalogVersion": catalog["catalogVersion"],
            "providerId": None,
            "modelId": None,
            "promptModuleId": None,
            "referenceSetId": None,
            "sourceLibraryId": source_library_id,
            "variationValues": {},
            "controls": {"cutDurationMs": cut_duration_ms},
        },
        "renderTreatment": render_treatment,
        "demand": {"formatMix": {"pov-dirt-bike": 1.0}},
    }, sort_keys=True, separators=(",", ":"))
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
        vault_url="https://shipstream.risingtidesviral.com/vault/chase.miles.4l",
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

    assert resolve_source_recipe(publication(source_library_id="not-registered")) is None
    assert resolve_source_recipe(publication(cut_duration_ms=6_500)) is None
    legacy = publication()
    spec = json.loads(legacy["recipeSpecCanonical"])
    spec.pop("production")
    spec["schema"] = "dossier.recipe-spec.v2"
    legacy["recipeSpecCanonical"] = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    assert resolve_source_recipe(legacy) is None


def test_capability_and_jobs_bind_master_hash_and_unique_windows(lab):
    client, _, started = lab
    capabilities = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID},
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
    assert [cut["startMs"] for cut in first_job["sourceCuts"]] == [0, CUT_SLOT_STEP_MS]

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
    assert [cut["startMs"] for cut in second_job["sourceCuts"]] == [
        CUT_SLOT_STEP_MS * 2, CUT_SLOT_STEP_MS * 3,
    ]
    assert len(started) == 2


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
    source = tmp_path / "master.mp4"
    _write_av_test_clip(source)

    async def cached_source(*_):
        return source

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    await cp._run_dossier_source(response.json()["jobId"])
    job = cp._load_jobs()["jobs"][response.json()["jobId"]]
    assert job["status"] == "completed"
    output = Path(job["artifactRoot"]) / job["clips"][0]["path"]
    assert _probe_duration(output) == pytest.approx(3.0, abs=0.15)
    lineage = job["clips"][0]["source"]
    assert lineage["master"]["sha256"] == MASTER_SHA
    assert lineage["cutWindow"] == {
        "libraryStartMs": 0,
        "libraryEndMs": 6_000,
        "durationMs": 6_000,
        "originalStartMs": 120_000,
        "originalEndMs": 126_000,
    }
    assert lineage["pageId"] == PAGE_ID


@pytest.mark.asyncio
async def test_runner_passes_exact_cut_speed_and_crop_to_isolated_render(lab, monkeypatch):
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
    calls = []

    async def cached_source(*_):
        return source

    async def render(src, dst, correction, **kwargs):
        calls.append((src, dst, correction, kwargs))
        Path(dst).write_bytes(b"derived")

    monkeypatch.setattr(cp, "_cached_source_master", cached_source)
    monkeypatch.setattr(cp, "run_color_correct", render)
    await cp._run_dossier_source(response.json()["jobId"])
    assert calls[0][3] == {
        "scale": None,
        "playback_speed": 0.75,
        "clip_crop": crop,
        "clip_start_ms": 0,
        "clip_duration_ms": 8_000,
    }
    job = cp._load_jobs()["jobs"][response.json()["jobId"]]
    assert job["status"] == "completed"
    assert Path(job["artifactRoot"]) in Path(calls[0][1]).parents


def test_source_media_origin_is_explicit_pinned_https(monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_CONTROL_PLANE_ORIGIN", "https://control.example")
    assert cp._source_media_origin() == "https://control.example"
    for invalid in ("", "http://control.example", "https://user@control.example", "https://control.example/path"):
        monkeypatch.setenv("CONTENT_LAB_CONTROL_PLANE_ORIGIN", invalid)
        with pytest.raises(RuntimeError, match="source_dna_control_plane_origin_unavailable"):
            cp._source_media_origin()
