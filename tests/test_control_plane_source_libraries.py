"""Source-library commissioning stays byte-exact and capability-atomic."""

import hashlib
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import routers.control_plane as cp
import routers.control_plane_recipes as recipes
import routers.control_plane_source_libraries as source_router
import services.control_plane_source_libraries as source_libraries
import services.control_plane_sources as source_executors
from scripts.commission_source_library import CommissioningError, verify_local_source
from tests.master_pages_fixtures import master_pages


TOKEN = "test-control-plane-token"
LIBRARY_ID = "pov-dirt-bike-8791-20260828"
PAGE_ID = "tt-dirt-bike"


def _manifest(clips: list[bytes]) -> bytes:
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
        "libraryId": LIBRARY_ID,
        "format": "pov-dirt-bike",
        "authority": {
            "system": "8791",
            "clipBankPath": "out/clip_bank.json",
            "clipBankSha256": "a" * 64,
            "selection": "operator-approved and text-scan-passed exact bytes",
        },
        "clips": rows,
    }, sort_keys=True, separators=(",", ":")).encode()


def _publication(recipe_version="dossier-d1d1d1d1d1d1d1d1"):
    intent, revision = master_pages(
        PAGE_ID, handle="dirt.bike", content_niche="POV - Dirtbike",
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
        "pageId": PAGE_ID,
        "lane": recipes.LANE,
        "recipeId": "pov-dirt-bike:master",
        "engine": "sourced_video",
        "recipeVersion": recipe_version,
        "dossierRevision": "rev-dirt-bike",
        "recipeSpecHash": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
        "recipeSpecCanonical": canonical,
    }


def _headers(manifest_sha: str | None = None):
    headers = {"Authorization": f"Bearer {TOKEN}", "X-RT-Lane": recipes.LANE}
    if manifest_sha is not None:
        headers["X-Source-Manifest-Sha256"] = "sha256:" + manifest_sha
    return headers


@pytest.fixture
def lab(monkeypatch, tmp_path):
    bodies = [b"first-approved-source", b"second-approved-source"]
    manifest_dir = tmp_path / "manifest"
    manifest_dir.mkdir()
    raw = _manifest(bodies)
    manifest_path = manifest_dir / f"{LIBRARY_ID}.json"
    manifest_path.write_bytes(raw)
    manifest_sha = hashlib.sha256(raw).hexdigest()
    projects = tmp_path / "projects"
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir()
    (recipes_dir / f"{LIBRARY_ID}.json").write_text(json.dumps({
        "registered": True,
        "project": LIBRARY_ID,
        "maxQuantity": 10,
        "pages": [],
    }))
    catalog = tmp_path / "source-executors.json"
    catalog.write_text(json.dumps({
        "schema": source_executors.CATALOG_SCHEMA,
        "executors": {
            "pov-dirt-bike:master": {
                "format": "pov-dirt-bike",
                "engine": "sourced_video",
                "contentNiche": "POV - Dirtbike",
                "baseRecipeId": LIBRARY_ID,
                "baseRecipeVersion": "v" + manifest_sha[:12],
                "maxQuantity": 10,
            },
        },
    }))
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("CONTENT_LAB_RECIPE_ROOT", str(tmp_path / "publications"))
    monkeypatch.setattr(source_libraries, "MANIFEST_DIR", manifest_dir)
    monkeypatch.setattr(source_libraries, "PROJECTS_DIR", projects)
    monkeypatch.setattr(cp, "PROJECTS_DIR", projects)
    monkeypatch.setattr(source_executors, "CATALOG_PATH", catalog)

    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    app.include_router(recipes.router, prefix="/api/control-plane")
    app.include_router(source_router.router, prefix="/api/control-plane")
    client = TestClient(app)
    publication_headers = {
        "Authorization": f"Bearer {TOKEN}",
        "X-RT-Lane": recipes.LANE,
        "X-RT-Page-Id": PAGE_ID,
        "Idempotency-Key": "register-dirt-bike-source",
    }
    assert client.post(
        "/api/control-plane/v1/recipes",
        json=_publication(),
        headers=publication_headers,
    ).status_code == 200
    manifest = source_libraries.load_source_library_manifest(LIBRARY_ID)
    return client, tmp_path, manifest, bodies


def _upload(client, manifest, clip, body, **header_overrides):
    headers = _headers(manifest.sha256)
    headers.update(header_overrides)
    return client.put(
        f"/api/control-plane/v1/source-libraries/{LIBRARY_ID}/clips/{clip.sha256}",
        content=body,
        headers=headers,
    )


def test_malformed_server_manifest_fails_closed(monkeypatch, tmp_path):
    manifest_dir = tmp_path / "manifest"
    manifest_dir.mkdir()
    (manifest_dir / f"{LIBRARY_ID}.json").write_text('{"schema":"wrong"}')
    monkeypatch.setattr(source_libraries, "MANIFEST_DIR", manifest_dir)
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    app = FastAPI()
    app.include_router(source_router.router, prefix="/api/control-plane")
    client = TestClient(app)
    response = client.get(
        f"/api/control-plane/v1/source-libraries/{LIBRARY_ID}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 503


def test_auth_lane_hash_and_body_fail_without_capability(lab):
    client, _, manifest, bodies = lab
    clip = manifest.clips[0]
    endpoint = f"/api/control-plane/v1/source-libraries/{LIBRARY_ID}"
    assert client.get(endpoint).status_code == 401
    assert _upload(
        client, manifest, clip, bodies[0], Authorization="Bearer wrong",
    ).status_code == 401
    assert _upload(
        client, manifest, clip, bodies[0], **{"X-RT-Lane": "wrong"},
    ).status_code == 400
    assert _upload(
        client,
        manifest,
        clip,
        bodies[0],
        **{"X-Source-Manifest-Sha256": "sha256:" + "0" * 64},
    ).status_code == 409
    assert _upload(client, manifest, clip, b"wrong").status_code == 409
    assert _upload(client, manifest, clip, b"x" * clip.bytes).status_code == 409
    capabilities = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID},
    ).json()["capabilities"]
    assert all(item["recipeId"] != "pov-dirt-bike:master" for item in capabilities)
    assert not (manifest and (Path(source_libraries.PROJECTS_DIR) / LIBRARY_ID / "prompts.json").exists())


def test_partial_finalize_refuses_then_exact_retry_is_idempotent_and_atomic(lab):
    client, tmp_path, manifest, bodies = lab
    clips_by_body = {
        hashlib.sha256(body).hexdigest(): body for body in bodies
    }
    first = manifest.clips[0]
    assert _upload(client, manifest, first, clips_by_body[first.sha256]).status_code == 200
    assert _upload(client, manifest, first, clips_by_body[first.sha256]).status_code == 200
    finalize_url = f"/api/control-plane/v1/source-libraries/{LIBRARY_ID}/finalize"
    assert client.post(finalize_url, headers=_headers(manifest.sha256)).status_code == 409
    prompts = tmp_path / "projects" / LIBRARY_ID / "prompts.json"
    assert not prompts.exists()
    for clip in manifest.clips[1:]:
        assert _upload(client, manifest, clip, clips_by_body[clip.sha256]).status_code == 200
    finalized = client.post(finalize_url, headers=_headers(manifest.sha256))
    assert finalized.status_code == 200
    assert finalized.json()["finalized"] is True
    assert finalized.json()["recipeVersion"] == "v" + manifest.sha256[:12]
    assert prompts.read_bytes() == manifest.raw
    video_dir = tmp_path / "projects" / LIBRARY_ID / "videos"
    assert sorted(path.name for path in video_dir.iterdir()) == [
        clip.stored_name for clip in manifest.clips
    ]
    assert client.post(finalize_url, headers=_headers(manifest.sha256)).status_code == 200

    capabilities = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID},
    ).json()["capabilities"]
    assert {
        "recipeId": "pov-dirt-bike:master",
        "engine": "sourced_video",
        "recipeVersion": _publication()["recipeVersion"],
        "maxQuantity": 10,
    } in capabilities
    assert all(item["recipeId"] != LIBRARY_ID for item in capabilities)


def test_divergent_preexisting_project_is_never_overwritten(lab):
    client, tmp_path, manifest, bodies = lab
    clip = manifest.clips[0]
    videos = tmp_path / "projects" / LIBRARY_ID / "videos"
    videos.mkdir(parents=True)
    divergent = videos / clip.stored_name
    divergent.write_bytes(b"preserve-me".ljust(clip.bytes, b"x"))
    before = divergent.read_bytes()
    response = _upload(client, manifest, clip, bodies[0])
    assert response.status_code == 409
    assert divergent.read_bytes() == before
    assert not (videos.parent / "prompts.json").exists()


def test_local_commissioner_rechecks_8791_clip_bank_and_exact_bytes(tmp_path):
    rail = tmp_path / "rail"
    clips = [b"approved-source"]
    raw = _manifest(clips)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(raw)
    manifest = json.loads(raw)
    clip = manifest["clips"][0]
    source = rail / clip["railPath"]
    source.parent.mkdir(parents=True)
    source.write_bytes(clips[0])
    clip_bank = {
        clip["sha256"]: {
            "format": "pov-dirt-bike",
            "eligible": True,
            "file": clip["railPath"],
            "qa_sweep": {"verdict": "pass", "swept_by": "operator:8791-review"},
            "text_scan": "pass",
        }
    }
    bank_path = rail / "out/clip_bank.json"
    bank_path.parent.mkdir(parents=True, exist_ok=True)
    bank_path.write_text(json.dumps(clip_bank, sort_keys=True))
    manifest["authority"]["clipBankSha256"] = hashlib.sha256(bank_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    clip_bank["f" * 64] = {"unrelated": "new row after selection"}
    bank_path.write_text(json.dumps(clip_bank, sort_keys=True))

    verified, _, current_bank_sha, paths = verify_local_source(manifest_path, rail)
    assert verified["libraryId"] == LIBRARY_ID
    assert current_bank_sha == hashlib.sha256(bank_path.read_bytes()).hexdigest()
    assert current_bank_sha != manifest["authority"]["clipBankSha256"]
    assert paths[0][1] == source
    source.write_bytes(b"changed")
    with pytest.raises(CommissioningError, match="bytes do not match"):
        verify_local_source(manifest_path, rail)
