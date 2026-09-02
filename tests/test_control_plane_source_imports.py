"""Page-scoped URL intake reuses the durable control-plane artifact contract."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import routers.control_plane as cp
from services.control_plane_source_imports import (
    SOURCE_IMPORT_SCHEMA,
    SOURCE_PROVENANCE_SCHEMA,
    SourceImportArtifact,
    SourceImportMedia,
)
from tests.master_pages_fixtures import bind_current_intent, master_pages
from services.master_pages_contract import intent_hash


TOKEN = "test-control-plane-token"
PAGE_ID = "acct:operator:night-walks"
SOURCE_URL = "https://cdn.example.com/source/night-walk.mp4"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "X-RT-Page-Id": PAGE_ID,
    "X-RT-Lane": "content-bucket-control-plane",
    "Idempotency-Key": "source:intake:night-walks:001",
}


@pytest.fixture
def lab(monkeypatch, tmp_path):
    intent, revision = master_pages(
        PAGE_ID,
        handle="night.walks",
        content_niche="POV — Night Core",
        content_engine="sourced_video",
        vault_url="https://shipstream.risingtidesviral.com/vault/night.walks",
    )
    intent["notionPageId"] = "notion-night-walks"
    revision = intent_hash(intent)
    bind_current_intent(monkeypatch, cp, intent, revision)
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv(
        "CONTENT_LAB_CONTROL_PLANE_ORIGIN", "https://content-lab.example.com",
    )
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    generation_root = tmp_path / "generated"
    monkeypatch.setattr(cp, "_generation_root", lambda: generation_root)
    monkeypatch.setattr(cp, "validate_source_url", lambda value: value)
    started: list[str] = []
    monkeypatch.setattr(cp, "_start_page_source_import", started.append)
    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    return TestClient(app), intent, revision, started


def _body(intent, revision, **overrides):
    body = {
        "schema": SOURCE_IMPORT_SCHEMA,
        "pageId": PAGE_ID,
        "format": "pov-night-core",
        "sourceUrl": SOURCE_URL,
        "masterPages": intent,
        "masterPagesHash": revision,
    }
    body.update(overrides)
    return body


def test_source_import_is_authenticated_exact_idempotent_and_durable(lab):
    client, intent, revision, started = lab
    response = client.post(
        "/api/control-plane/v1/source-imports",
        headers=HEADERS,
        json=_body(intent, revision),
    )
    assert response.status_code == 200
    result = response.json()
    assert result["schema"] == cp.RESPONSE_SCHEMA
    assert result["status"] == "queued"
    assert started == [result["jobId"]]

    job = cp._load_jobs()["jobs"][result["jobId"]]
    assert job["sourceKind"] == "page_source_import"
    assert job["pageId"] == PAGE_ID
    assert job["format"] == "pov-night-core"
    assert job["sourceUrl"] == SOURCE_URL
    assert job["masterPages"] == intent
    assert job["masterPagesHash"] == revision
    assert job["quantityRequested"] == 1

    retried = client.post(
        "/api/control-plane/v1/source-imports",
        headers=HEADERS,
        json=_body(intent, revision),
    )
    assert retried.status_code == 200
    assert retried.json() == result
    assert started == [result["jobId"]]

    conflict = client.post(
        "/api/control-plane/v1/source-imports",
        headers=HEADERS,
        json=_body(intent, revision, sourceUrl="https://cdn.example.com/other.mp4"),
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "idempotency key belongs to a different request"


def test_runtime_restart_resurrects_only_the_exact_idempotent_source_import(lab):
    client, intent, revision, started = lab
    created = client.post(
        "/api/control-plane/v1/source-imports",
        headers=HEADERS,
        json=_body(intent, revision),
    ).json()
    job_id = created["jobId"]
    store = cp._load_jobs()
    job = store["jobs"][job_id]
    old_token = job["token"]
    root = Path(job["artifactRoot"])
    partial = root / "source" / "original.part"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"partial")
    job["runtimeId"] = "previous-runtime"
    cp.atomic_save(cp._jobs_path(), store)

    retried = client.post(
        "/api/control-plane/v1/source-imports",
        headers=HEADERS,
        json=_body(intent, revision),
    )
    assert retried.status_code == 200
    assert retried.json() == {
        "schema": cp.RESPONSE_SCHEMA,
        "jobId": job_id,
        "status": "queued",
    }
    recovered = cp._load_jobs()["jobs"][job_id]
    assert recovered["runtimeId"] == cp._GENERATION_RUNTIME_ID
    assert recovered["status"] == "queued"
    assert recovered["progress"] == 0
    assert recovered["clips"] == []
    assert "error" not in recovered
    assert "completedAt" not in recovered
    assert recovered["token"] != old_token
    assert root.is_dir()
    assert not partial.exists()
    assert started == [job_id, job_id]

    recovered.update({
        "status": "failed",
        "error": "source video normalization failed",
        "completedAt": "2026-09-01T00:00:00+00:00",
    })
    store = cp._load_jobs()
    store["jobs"][job_id] = recovered
    cp.atomic_save(cp._jobs_path(), store)
    terminal = client.post(
        "/api/control-plane/v1/source-imports",
        headers=HEADERS,
        json=_body(intent, revision),
    )
    assert terminal.status_code == 200
    assert terminal.json()["status"] == "failed"
    assert started == [job_id, job_id]


def test_source_import_rejects_unscoped_stale_and_wrong_format_requests(lab, monkeypatch):
    client, intent, revision, _ = lab
    body = _body(intent, revision)
    assert client.post(
        "/api/control-plane/v1/source-imports", json=body,
    ).status_code == 401
    assert client.post(
        "/api/control-plane/v1/source-imports",
        headers={**HEADERS, "X-RT-Page-Id": "acct:other"},
        json=body,
    ).status_code == 400
    wrong_format = client.post(
        "/api/control-plane/v1/source-imports",
        headers=HEADERS,
        json=_body(intent, revision, format="pov-scenic"),
    )
    assert wrong_format.status_code == 409
    assert wrong_format.json()["detail"] == "source import format is not complete and commissioned for Master Pages"
    unknown = client.post(
        "/api/control-plane/v1/source-imports",
        headers=HEADERS,
        json={**body, "note": "untyped"},
    )
    assert unknown.status_code == 400
    monkeypatch.setattr(cp, "_current_master_pages_intent", lambda *_: None)
    stale = client.post(
        "/api/control-plane/v1/source-imports",
        headers={**HEADERS, "Idempotency-Key": "source:intake:night-walks:002"},
        json=body,
    )
    assert stale.status_code == 409
    assert not cp._jobs_path().exists()


def test_completed_import_uses_exact_bytes_probe_and_https_artifact(lab, monkeypatch):
    client, intent, revision, started = lab
    created = client.post(
        "/api/control-plane/v1/source-imports",
        headers=HEADERS,
        json=_body(intent, revision),
    ).json()
    job_id = created["jobId"]
    assert started == [job_id]
    exact_bytes = b"exact-source-video-bytes"
    exact_sha = hashlib.sha256(exact_bytes).hexdigest()
    media = SourceImportMedia(
        duration_ms=42_500,
        width=1080,
        height=1920,
        video_codec="h264",
        pixel_format="yuv420p",
        fps=30.0,
        audio_streams=0,
    )
    original_bytes = b"original-landscape-video"
    original_sha = hashlib.sha256(original_bytes).hexdigest()
    original_media = SourceImportMedia(
        duration_ms=42_500,
        width=3840,
        height=2160,
        video_codec="vp9",
        pixel_format="yuv420p",
        fps=60.0,
        audio_streams=1,
    )

    async def import_video(source_url: str, destination: Path):
        assert source_url == SOURCE_URL
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(exact_bytes)
        return SourceImportArtifact(
            destination,
            exact_sha,
            len(exact_bytes),
            media,
            original_sha,
            len(original_bytes),
            original_media,
        )

    async def thumbnail(job_root: Path, _video: Path, _index: int):
        target = job_root / "thumbnails" / "0000.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"jpeg")
        return cp._generated_manifest(job_root, target)

    monkeypatch.setattr(cp, "download_source_video", import_video)
    monkeypatch.setattr(cp, "_thumbnail_manifest", thumbnail)
    asyncio.run(cp._run_page_source_import(job_id))

    stored = cp._load_jobs()["jobs"][job_id]
    assert stored["status"] == "completed"
    assert stored["progress"] == 100
    assert len(stored["clips"]) == 1
    source = stored["clips"][0]["source"]
    assert source == {
        "schema": SOURCE_PROVENANCE_SCHEMA,
        "kind": "page_source_import",
        "pageId": PAGE_ID,
        "format": "pov-night-core",
        "sourceUrl": SOURCE_URL,
        "sha256": exact_sha,
        "bytes": len(exact_bytes),
        "mimeType": "video/mp4",
        "media": media.wire(),
        "original": {
            "sha256": original_sha,
            "bytes": len(original_bytes),
            "mimeType": "video/mp4",
            "media": original_media.wire(),
        },
        "masterPagesHash": revision,
        "contentNiche": "POV — Night Core",
        "contentEngine": "sourced_video",
        "vaultUrl": intent["vaultUrl"],
    }

    artifacts = client.get(
        f"/api/control-plane/v1/jobs/{job_id}/artifacts",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-RT-Page-Id": PAGE_ID,
        },
    )
    assert artifacts.status_code == 200
    artifact = artifacts.json()["artifacts"][0]
    assert artifact["sha256"] == exact_sha
    assert artifact["bytes"] == len(exact_bytes)
    assert artifact["source"] == source
    assert artifact["url"].startswith("https://content-lab.example.com/")
    assert artifact["thumbnail"]["url"].startswith("https://content-lab.example.com/")

    download_path = urlsplit(artifact["url"]).path + "?" + urlsplit(artifact["url"]).query
    downloaded = client.get(download_path)
    assert downloaded.status_code == 200
    assert downloaded.content == exact_bytes


def test_source_import_artifact_origin_ignores_forwarded_host(lab, monkeypatch):
    client, intent, revision, _ = lab
    created = client.post(
        "/api/control-plane/v1/source-imports",
        headers=HEADERS,
        json=_body(intent, revision),
    ).json()
    job = cp._load_jobs()["jobs"][created["jobId"]]
    job["status"] = "completed"
    job["clips"] = [{
        "path": "source/source.mp4",
        "sha256": "a" * 64,
        "bytes": 1,
        "source": {},
        "thumbnail": {"sha256": "b" * 64, "bytes": 1},
    }]
    cp._update_job(created["jobId"], status="completed", clips=job["clips"])
    response = client.get(
        f"/api/control-plane/v1/jobs/{created['jobId']}/artifacts",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-RT-Page-Id": PAGE_ID,
            "X-Forwarded-Host": "attacker.example",
            "X-Forwarded-Proto": "http",
        },
    )
    assert response.status_code == 200
    artifact = response.json()["artifacts"][0]
    assert artifact["url"].startswith("https://content-lab.example.com/")
    assert "attacker.example" not in artifact["url"]


def test_source_import_requires_allowlist_configuration(lab, monkeypatch):
    client, intent, revision, _ = lab

    def unavailable(_value):
        raise cp.SourceImportUnavailable("source import host allowlist is unavailable")

    monkeypatch.setattr(cp, "validate_source_url", unavailable)
    response = client.post(
        "/api/control-plane/v1/source-imports",
        headers=HEADERS,
        json=_body(intent, revision),
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "source import host allowlist is unavailable"


def test_source_host_resolution_has_a_bounded_timeout(monkeypatch):
    async def never_complete(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(cp.asyncio, "to_thread", never_complete)
    monkeypatch.setattr(cp, "SOURCE_URL_RESOLVE_SECONDS", 0.001)
    with pytest.raises(cp.SourceImportUnavailable, match="timed out"):
        asyncio.run(cp._validated_source_url(SOURCE_URL))


def test_source_import_requires_complete_commissioned_profile(lab, monkeypatch):
    client, intent, revision, _ = lab
    profiles, registry_hash = cp.load_engine_registry()
    profiles["pov-night-core"] = replace(
        profiles["pov-night-core"], execution_status="uncommissioned",
    )
    monkeypatch.setattr(cp, "load_engine_registry", lambda: (profiles, registry_hash))
    response = client.post(
        "/api/control-plane/v1/source-imports",
        headers=HEADERS,
        json=_body(intent, revision),
    )
    assert response.status_code == 409
    assert "complete and commissioned" in response.json()["detail"]


def test_source_import_enforces_one_active_job_per_page_and_global_capacity(lab):
    client, intent, revision, _ = lab
    first = client.post(
        "/api/control-plane/v1/source-imports",
        headers=HEADERS,
        json=_body(intent, revision),
    )
    assert first.status_code == 200
    duplicate_page = client.post(
        "/api/control-plane/v1/source-imports",
        headers={**HEADERS, "Idempotency-Key": "source:intake:night-walks:other"},
        json=_body(intent, revision, sourceUrl="https://cdn.example.com/other.mp4"),
    )
    assert duplicate_page.status_code == 409
    assert duplicate_page.json()["detail"] == "source import is already active for this page"

    current = cp._load_jobs()
    current["jobs"][first.json()["jobId"]]["pageId"] = "acct:other:one"
    current["jobs"]["cpl-other"] = {
        "jobId": "cpl-other",
        "pageId": "acct:other:two",
        "sourceKind": "page_source_import",
        "status": "running",
        "runtimeId": cp._GENERATION_RUNTIME_ID,
    }
    cp.atomic_save(cp._jobs_path(), current)
    capacity = client.post(
        "/api/control-plane/v1/source-imports",
        headers={**HEADERS, "Idempotency-Key": "source:intake:night-walks:capacity"},
        json=_body(intent, revision, sourceUrl="https://cdn.example.com/capacity.mp4"),
    )
    assert capacity.status_code == 409
    assert capacity.json()["detail"] == "source import capacity is currently full"


def test_failed_source_import_removes_its_partial_job_directory(lab, monkeypatch):
    client, intent, revision, _ = lab
    created = client.post(
        "/api/control-plane/v1/source-imports",
        headers=HEADERS,
        json=_body(intent, revision),
    ).json()
    job = cp._load_jobs()["jobs"][created["jobId"]]
    root = Path(job["artifactRoot"])
    partial = root / "source" / "original.part"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"partial")

    async def fail(_url, _destination):
        raise RuntimeError("download failed")

    monkeypatch.setattr(cp, "download_source_video", fail)
    asyncio.run(cp._run_page_source_import(created["jobId"]))
    stored = cp._load_jobs()["jobs"][created["jobId"]]
    assert stored["status"] == "failed"
    assert not root.exists()
