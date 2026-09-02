"""Page-scoped URL intake reuses the durable control-plane artifact contract."""

from __future__ import annotations

import asyncio
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
    assert wrong_format.json()["detail"] == "source import format does not match Master Pages"
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
    assert artifact["url"].startswith("https://testserver/")
    assert artifact["thumbnail"]["url"].startswith("https://testserver/")

    download_path = urlsplit(artifact["url"]).path + "?" + urlsplit(artifact["url"]).query
    downloaded = client.get(download_path)
    assert downloaded.status_code == 200
    assert downloaded.content == exact_bytes
