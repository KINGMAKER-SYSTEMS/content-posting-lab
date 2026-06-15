import json

from routers import agenticnews as agenticnews_router


def test_editor_timeline_api_create_load_and_command(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)

    created = sync_client.post(
        "/api/agenticnews/editor-timelines",
        json={"projectId": "api_proj", "title": "API Project"},
    )
    assert created.status_code == 201
    assert created.json()["projectId"] == "api_proj"
    assert created.json()["revision"] == 0

    command = sync_client.post(
        "/api/agenticnews/editor-timelines/api_proj/commands",
        json={
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {
                "assetId": "a1",
                "type": "image",
                "src": "/agenticnews-assets/card.png",
            },
        },
    )
    assert command.status_code == 200
    assert command.json()["revision"] == 1

    loaded = sync_client.get("/api/agenticnews/editor-timelines/api_proj")
    assert loaded.status_code == 200
    payload = loaded.json()
    assert payload["projectId"] == "api_proj"
    assert payload["revision"] == 1
    assert payload["assets"]["a1"]["src"] == "/agenticnews-assets/card.png"

    stale = sync_client.post(
        "/api/agenticnews/editor-timelines/api_proj/commands",
        json={
            "op": "clip.create",
            "actor": "late-agent",
            "expectedRevision": 0,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "graphics_1",
                "start": 0,
                "duration": 1,
            },
        },
    )
    assert stale.status_code == 409


def test_editor_timeline_api_imports_abn_fixture(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)

    imported = sync_client.post(
        "/api/agenticnews/editor-timelines/api_import/import-abn",
        json={
            "sourceEpisodeId": "ep_api",
            "timeline": {
                "episodeId": "ep_api",
                "title": "Imported",
                "segments": [
                    {
                        "segmentId": "s0",
                        "durationSec": 2.0,
                        "shots": [
                            {
                                "src": "/agenticnews-assets/hook.png",
                                "startSec": 0,
                                "durationSec": 2,
                                "type": "artifact",
                            }
                        ],
                    }
                ],
            },
        },
    )
    assert imported.status_code == 201
    project = imported.json()
    assert project["projectId"] == "api_import"
    assert project["sourceEpisodeId"] == "ep_api"
    assert project["revision"] == 0
    assert len(project["clips"]) == 1


def test_editor_timeline_api_auto_imports_real_episode_timeline(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    timeline_path = tmp_path / "ep_real_timeline.json"
    timeline_path.write_text(json.dumps({
        "episodeId": "ep_real",
        "title": "Real Episode",
        "totalSec": 4.0,
        "musicBed": "/agenticnews-assets/bed.mp3",
        "segments": [
            {
                "segmentId": "s0",
                "durationSec": 4.0,
                "shots": [
                    {
                        "id": "shot0",
                        "type": "artifact",
                        "src": "/agenticnews-assets/ep_real_s0_card.png",
                        "startSec": 0,
                        "durationSec": 4,
                    }
                ],
                "audio": {"vo": {"src": "/agenticnews-assets/ep_real_s0.wav", "duration": 4.0}},
                "lowerThirds": [
                    {
                        "headline": "Real lower third",
                        "sourceUrl": "https://example.com/source",
                        "startSec": 0.5,
                        "durationSec": 2.0,
                    }
                ],
            }
        ],
    }))

    imported = sync_client.get("/api/agenticnews/editor-timelines/ep_real")

    assert imported.status_code == 200
    project = imported.json()
    assert project["projectId"] == "ep_real"
    assert project["sourceEpisodeId"] == "ep_real"
    assert project["title"] == "Real Episode"
    assert "video" not in project.get("renderCache", {})
    assert any(asset["src"] == "/agenticnews-assets/ep_real_s0_card.png" for asset in project["assets"].values())
    assert any(clip["kind"] == "voiceover" for clip in project["clips"].values())
    assert any(clip["kind"] == "music_bed" for clip in project["clips"].values())
    title_assets = [asset for asset in project["assets"].values() if asset["type"] == "title"]
    assert title_assets
    assert title_assets[0]["src"] == ""
    assert not (tmp_path / "editor_title_assets").exists()
    assert (tmp_path / "editor_timelines" / "ep_real.json").exists()


def test_editor_timeline_api_blocks_flattened_source_materialization_by_default(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    monkeypatch.setattr(agenticnews_router, "EDITOR_ALLOW_FLATTENED_SOURCE_MATERIALIZATION", False)
    (tmp_path / "ep_real_episode.mp4").write_bytes(b"not-a-real-video")
    (tmp_path / "ep_real_s1_card.png").write_bytes(b"card")
    timeline_path = tmp_path / "ep_real_timeline.json"
    timeline_path.write_text(json.dumps({
        "episodeId": "ep_real",
        "title": "Real Episode",
        "totalSec": 4.0,
        "segments": [
            {
                "segmentId": "ep_real_s0",
                "durationSec": 2.0,
                "shots": [
                    {
                        "id": "shot0",
                        "type": "artifact",
                        "src": "/agenticnews-assets/ep_real_s1_src.png",
                        "startSec": 0,
                        "durationSec": 1,
                    },
                    {
                        "id": "shot1",
                        "type": "broll",
                        "src": "/agenticnews-assets/ep_real_s1_demo.mp4",
                        "startSec": 1,
                        "durationSec": 1,
                    },
                ],
                "audio": {"vo": {"src": "/agenticnews-assets/ep_real_s0.wav", "duration": 2.0}},
            }
        ],
    }))

    response = sync_client.get("/api/agenticnews/editor-timelines/ep_real")

    assert response.status_code == 200
    project = response.json()
    assert "materializedAssets" not in project.get("metadata", {})
    assert not (tmp_path / "ep_real_s1_src.png").exists()
    assert not (tmp_path / "ep_real_s0.wav").exists()
    assert not (tmp_path / "ep_real_s1_demo.mp4").exists()


def test_editor_timeline_asset_health_reports_missing_sources_without_mutating(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    monkeypatch.setattr(agenticnews_router, "EDITOR_ALLOW_FLATTENED_SOURCE_MATERIALIZATION", False)
    (tmp_path / "ep_real_episode.mp4").write_bytes(b"not-a-real-video")
    (tmp_path / "ep_real_s1_card.png").write_bytes(b"card")
    timeline_path = tmp_path / "ep_real_timeline.json"
    timeline_path.write_text(json.dumps({
        "episodeId": "ep_real",
        "title": "Real Episode",
        "totalSec": 4.0,
        "segments": [
            {
                "segmentId": "ep_real_s0",
                "durationSec": 2.0,
                "shots": [
                    {
                        "id": "shot0",
                        "type": "artifact",
                        "src": "/agenticnews-assets/ep_real_s1_src.png",
                        "startSec": 0,
                        "durationSec": 1,
                    },
                    {
                        "id": "shot1",
                        "type": "broll",
                        "src": "/agenticnews-assets/ep_real_s1_demo.mp4",
                        "startSec": 1,
                        "durationSec": 1,
                    },
                ],
                "audio": {"vo": {"src": "/agenticnews-assets/ep_real_s0.wav", "duration": 2.0}},
            }
        ],
    }))

    response = sync_client.get("/api/agenticnews/editor-timelines/ep_real/asset-health")

    assert response.status_code == 200
    health = response.json()
    assert health["ok"] is False
    assert health["renderable"] is False
    assert health["importedInMemory"] is True
    assert health["wouldMutateOnLoad"] is True
    assert health["assetCount"] == 3
    assert health["clipCount"] == 3
    assert {item["type"] for item in health["missingFiles"]} == {"audio", "image", "video"}
    assert len(health["uniqueMissingFiles"]) == 3
    assert {item["type"] for item in health["blockedMaterializations"]} == {"audio", "video"}
    assert all(
        item["provenance"] == "would_derive_from_flattened_episode"
        for item in health["blockedMaterializations"]
    )
    assert health["copyCandidates"] == [{
        "type": "image",
        "path": str(tmp_path / "ep_real_s1_src.png"),
        "source": str(tmp_path / "ep_real_s1_card.png"),
        "provenance": "copied_from_existing_layer_parent",
        "status": "available",
        "action": "copy",
    }]
    assert not (tmp_path / "ep_real_s1_src.png").exists()
    assert not (tmp_path / "ep_real_s0.wav").exists()
    assert not (tmp_path / "ep_real_s1_demo.mp4").exists()
    assert not (tmp_path / "editor_timelines" / "ep_real.json").exists()


def test_editor_timeline_asset_health_loads_saved_timeline_read_only(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    timeline_dir = tmp_path / "editor_timelines"
    timeline_dir.mkdir()
    timeline_path = timeline_dir / "saved_proj.json"
    timeline_path.write_text(json.dumps({
        "schema": "editor-timeline/v1",
        "projectId": "saved_proj",
        "sourceEpisodeId": "saved_proj",
        "title": "Saved Project",
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "revision": 5,
        "assets": {
            "a_missing": {
                "id": "a_missing",
                "type": "image",
                "src": "/agenticnews-assets/missing.png",
            }
        },
        "tracks": {"graphics_1": {"id": "graphics_1", "kind": "image", "name": "Graphics", "index": 1}},
        "clips": {
            "c_missing": {
                "id": "c_missing",
                "assetId": "a_missing",
                "trackId": "graphics_1",
                "kind": "image",
                "start": 0,
                "duration": 1,
                "sourceStart": 0,
                "enabled": True,
                "muted": False,
                "volume": 1,
                "transform": {"x": 0.5, "y": 0.5, "scale": 1, "opacity": 1},
            }
        },
        "markers": {},
        "notes": {},
        "renderCache": {"video": {"backend": "stale", "video": "/tmp/missing.mp4"}},
    }))
    before = timeline_path.read_text()

    response = sync_client.get("/api/agenticnews/editor-timelines/saved_proj/asset-health")

    assert response.status_code == 200
    health = response.json()
    assert health["projectId"] == "saved_proj"
    assert health["revision"] == 5
    assert health["importedInMemory"] is False
    assert health["wouldMutateOnLoad"] is True
    assert "would sanitize render cache" in health["wouldMutateOnLoadReasons"]
    assert health["missingFiles"][0]["assetId"] == "a_missing"
    assert health["missingFiles"][0]["enabledClipIds"] == ["c_missing"]
    assert timeline_path.read_text() == before


def test_editor_timeline_asset_health_does_not_mutate_absent_render_cache(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    (tmp_path / "card.png").write_bytes(b"card")
    timeline_dir = tmp_path / "editor_timelines"
    timeline_dir.mkdir()
    timeline_path = timeline_dir / "saved_clean.json"
    timeline_path.write_text(json.dumps({
        "schema": "editor-timeline/v1",
        "projectId": "saved_clean",
        "sourceEpisodeId": "saved_clean",
        "title": "Saved Clean Project",
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "revision": 0,
        "metadata": {"abnImportVersion": agenticnews_router.editor_timeline.ABN_IMPORT_VERSION},
        "assets": {
            "a_card": {
                "id": "a_card",
                "type": "image",
                "src": "/agenticnews-assets/card.png",
            }
        },
        "tracks": {"graphics_1": {"id": "graphics_1", "kind": "image", "name": "Graphics", "index": 1}},
        "clips": {
            "c_card": {
                "id": "c_card",
                "assetId": "a_card",
                "trackId": "graphics_1",
                "kind": "image",
                "start": 0,
                "duration": 1,
                "sourceStart": 0,
                "enabled": True,
                "muted": False,
                "volume": 1,
                "transform": {"x": 0.5, "y": 0.5, "scale": 1, "opacity": 1},
            }
        },
        "markers": {},
        "notes": {},
        "commandLog": [],
    }))
    before = timeline_path.read_text()

    response = sync_client.get("/api/agenticnews/editor-timelines/saved_clean/asset-health")

    assert response.status_code == 200
    health = response.json()
    assert health["ok"] is True
    assert health["renderable"] is True
    assert health["wouldMutateOnLoad"] is False
    assert health["wouldMutateOnLoadReasons"] == []
    assert timeline_path.read_text() == before


def test_editor_timeline_asset_health_dedupes_shared_copy_candidates(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    (tmp_path / "ep_real_episode.mp4").write_bytes(b"not-a-real-video")
    (tmp_path / "ep_real_s1_card.png").write_bytes(b"card")
    timeline_path = tmp_path / "ep_real_timeline.json"
    timeline_path.write_text(json.dumps({
        "episodeId": "ep_real",
        "title": "Real Episode",
        "totalSec": 2.0,
        "segments": [
            {
                "segmentId": "ep_real_s1",
                "durationSec": 2.0,
                "shots": [
                    {
                        "id": "shot0",
                        "type": "artifact",
                        "src": "/agenticnews-assets/ep_real_s1_src.png",
                        "startSec": 0,
                        "durationSec": 1,
                    },
                    {
                        "id": "shot1",
                        "type": "artifact",
                        "src": "/agenticnews-assets/ep_real_s1_src.png",
                        "startSec": 1,
                        "durationSec": 1,
                    },
                ],
            }
        ],
    }))

    response = sync_client.get("/api/agenticnews/editor-timelines/ep_real/asset-health")

    assert response.status_code == 200
    health = response.json()
    assert len(health["copyCandidates"]) == 1
    assert health["copyCandidates"][0]["path"] == str(tmp_path / "ep_real_s1_src.png")
    assert len(health["materializationPlan"]) == 1
    assert not (tmp_path / "ep_real_s1_src.png").exists()


def test_editor_timeline_api_exports_openshot_contract(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    timeline_path = tmp_path / "ep_openshot_timeline.json"
    timeline_path.write_text(json.dumps({
        "episodeId": "ep_openshot",
        "title": "OpenShot Contract",
        "totalSec": 2.0,
        "segments": [
            {
                "segmentId": "s0",
                "durationSec": 2.0,
                "shots": [
                    {
                        "id": "shot0",
                        "type": "artifact",
                        "src": "/agenticnews-assets/card.png",
                        "startSec": 0,
                        "durationSec": 2,
                    }
                ],
            }
        ],
    }))

    imported = sync_client.get("/api/agenticnews/editor-timelines/ep_openshot")
    assert imported.status_code == 200
    project = imported.json()
    clip_id = next(iter(project["clips"]))

    command = sync_client.post(
        "/api/agenticnews/editor-timelines/ep_openshot/commands",
        json={
            "id": "cmd_nudge",
            "op": "clip.transform",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"clipId": clip_id, "transform": {"x": 0.6, "y": 0.4, "scale": 0.9}},
        },
    )
    assert command.status_code == 200

    response = sync_client.get("/api/agenticnews/editor-timelines/ep_openshot/openshot")

    assert response.status_code == 200
    payload = response.json()
    assert payload["engineAvailable"] in {True, False}
    assert payload["timeline"]["type"] == "Timeline"
    assert payload["timeline"]["clips"][0]["reader"]["path"] == str(tmp_path / "card.png")
    assert payload["updateActions"][0]["type"] == "update"
    assert payload["updateActions"][0]["key"] == ["clips", {"id": clip_id}]
    assert payload["updateActions"][0]["transaction"] == "cmd_nudge"


def test_editor_timeline_api_strips_stale_source_video_cache(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    timeline_dir = tmp_path / "editor_timelines"
    timeline_dir.mkdir()
    timeline_path = timeline_dir / "ep_real.json"
    timeline_path.write_text(json.dumps({
        "schema": "editor-timeline/v1",
        "projectId": "ep_real",
        "sourceEpisodeId": "ep_real",
        "title": "Real Episode",
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "revision": 2,
        "assets": {},
        "tracks": {},
        "clips": {},
        "markers": {},
        "notes": {},
        "renderCache": {
            "video": {
                "backend": "abn-source",
                "video": "/agenticnews-assets/ep_real_episode.mp4",
                "duration": 12.0,
            }
        },
    }))

    response = sync_client.get("/api/agenticnews/editor-timelines/ep_real")

    assert response.status_code == 200
    assert "video" not in response.json().get("renderCache", {})
    saved = json.loads(timeline_path.read_text())
    assert "video" not in saved.get("renderCache", {})


def test_editor_timeline_api_prunes_missing_render_cache_files(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    timeline_dir = tmp_path / "editor_timelines"
    timeline_dir.mkdir()
    timeline_path = timeline_dir / "ep_missing_cache.json"
    timeline_path.write_text(json.dumps({
        "schema": "editor-timeline/v1",
        "projectId": "ep_missing_cache",
        "sourceEpisodeId": "ep_missing_cache",
        "title": "Missing Cache",
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "revision": 0,
        "assets": {},
        "tracks": {},
        "clips": {},
        "markers": {},
        "notes": {},
        "renderCache": {
            "video": {"backend": "openshot", "video": str(tmp_path / "gone.mp4"), "duration": 12.0},
            "windows": {
                "1.00_2.00": {"backend": "openshot", "video": str(tmp_path / "gone-window.mp4"), "start": 1.0, "duration": 2.0}
            },
            "frames": {
                "1.00": {"backend": "openshot", "frame": str(tmp_path / "gone.png"), "at": 1.0}
            },
        },
    }))

    response = sync_client.get("/api/agenticnews/editor-timelines/ep_missing_cache")

    assert response.status_code == 200
    assert "renderCache" not in response.json()
    saved = json.loads(timeline_path.read_text())
    assert "renderCache" not in saved


def test_editor_timeline_api_prunes_revision_mismatched_render_cache(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    timeline_dir = tmp_path / "editor_timelines"
    timeline_dir.mkdir()
    render_dir = tmp_path / "editor_renders"
    render_dir.mkdir()
    fresh_video = render_dir / "fresh.mp4"
    stale_window = render_dir / "stale-window.mp4"
    stale_frame = render_dir / "stale.png"
    for path in (fresh_video, stale_window, stale_frame):
        path.write_bytes(b"cache")
    timeline_path = timeline_dir / "ep_stale_cache.json"
    timeline_path.write_text(json.dumps({
        "schema": "editor-timeline/v1",
        "projectId": "ep_stale_cache",
        "sourceEpisodeId": "ep_stale_cache",
        "title": "Stale Cache",
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "revision": 4,
        "assets": {},
        "tracks": {},
        "clips": {},
        "markers": {},
        "notes": {},
        "renderCache": {
            "video": {"backend": "openshot", "video": str(fresh_video), "duration": 12.0, "revision": 4},
            "windows": {
                "1.00_2.00": {"backend": "openshot", "video": str(stale_window), "start": 1.0, "duration": 2.0, "revision": 3}
            },
            "frames": {
                "1.00": {"backend": "openshot", "frame": str(stale_frame), "at": 1.0, "revision": 3}
            },
        },
    }))

    response = sync_client.get("/api/agenticnews/editor-timelines/ep_stale_cache")

    assert response.status_code == 200
    render_cache = response.json()["renderCache"]
    assert render_cache["video"]["video"] == str(fresh_video)
    assert "windows" not in render_cache
    assert "frames" not in render_cache
    saved = json.loads(timeline_path.read_text())
    assert saved["renderCache"] == render_cache


def test_editor_timeline_api_stamps_legacy_render_cache_revisions(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    timeline_dir = tmp_path / "editor_timelines"
    timeline_dir.mkdir()
    render_dir = tmp_path / "editor_renders"
    render_dir.mkdir()
    full_video = render_dir / "legacy-full.mp4"
    window_video = render_dir / "legacy-window.mp4"
    frame = render_dir / "legacy-frame.png"
    for path in (full_video, window_video, frame):
        path.write_bytes(b"cache")
    timeline_path = timeline_dir / "ep_legacy_cache.json"
    timeline_path.write_text(json.dumps({
        "schema": "editor-timeline/v1",
        "projectId": "ep_legacy_cache",
        "sourceEpisodeId": "ep_legacy_cache",
        "title": "Legacy Cache",
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "revision": 7,
        "assets": {},
        "tracks": {},
        "clips": {},
        "markers": {},
        "notes": {},
        "renderCache": {
            "video": {"backend": "openshot", "video": str(full_video), "duration": 12.0},
            "windows": {
                "1.00_2.00": {"backend": "openshot", "video": str(window_video), "start": 1.0, "duration": 2.0}
            },
            "frames": {
                "1.00": {"backend": "openshot", "frame": str(frame), "at": 1.0}
            },
        },
    }))

    response = sync_client.get("/api/agenticnews/editor-timelines/ep_legacy_cache")

    assert response.status_code == 200
    render_cache = response.json()["renderCache"]
    assert render_cache["video"]["revision"] == 7
    assert render_cache["windows"]["1.00_2.00"]["revision"] == 7
    assert render_cache["frames"]["1.00"]["revision"] == 7
    saved = json.loads(timeline_path.read_text())
    assert saved["renderCache"] == render_cache


def test_editor_timeline_api_does_not_materialize_title_assets_on_load(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    timeline_dir = tmp_path / "editor_timelines"
    timeline_dir.mkdir()
    cached_video = tmp_path / "cached-before-title-materialization.mp4"
    cached_video.write_bytes(b"cache")
    timeline_path = timeline_dir / "ep_title_cache.json"
    timeline_path.write_text(json.dumps({
        "schema": "editor-timeline/v1",
        "projectId": "ep_title_cache",
        "sourceEpisodeId": "ep_title_cache",
        "title": "Title Cache",
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "revision": 2,
        "metadata": {},
        "assets": {
            "lower": {
                "id": "lower",
                "type": "title",
                "src": "",
                "metadata": {"text": "Real title", "sourceUrl": "https://example.com"},
            }
        },
        "tracks": {"titles_1": {"id": "titles_1", "kind": "title", "name": "Titles", "index": 30}},
        "clips": {
            "lower_clip": {
                "id": "lower_clip",
                "assetId": "lower",
                "trackId": "titles_1",
                "kind": "lower_third",
                "start": 1,
                "duration": 2,
                "sourceStart": 0,
                "enabled": True,
                "muted": False,
                "volume": 1,
                "transform": {"x": 0.5, "y": 0.5, "scale": 1, "opacity": 1},
                "effects": [],
                "keyframes": [],
                "metadata": {},
            }
        },
        "markers": {},
        "notes": {},
        "renderCache": {
            "video": {"backend": "openshot", "video": str(cached_video), "duration": 10, "revision": 2}
        },
    }))

    response = sync_client.get("/api/agenticnews/editor-timelines/ep_title_cache")

    assert response.status_code == 200
    project = response.json()
    assert project["renderCache"]["video"]["video"] == str(cached_video)
    assert "titleAssetVersion" not in project["metadata"]
    assert project["assets"]["lower"]["src"] == ""
    assert not (tmp_path / "editor_title_assets").exists()
    saved = json.loads(timeline_path.read_text())
    assert saved["renderCache"]["video"]["video"] == str(cached_video)
    assert saved["assets"]["lower"]["src"] == ""


def test_editor_timeline_command_invalidates_render_cache_for_output_changes(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    timeline_dir = tmp_path / "editor_timelines"
    timeline_dir.mkdir()
    timeline_path = timeline_dir / "ep_cached_edit.json"
    cached_video = tmp_path / "cached.mp4"
    cached_video.write_bytes(b"cached")
    timeline_path.write_text(json.dumps({
        "schema": "editor-timeline/v1",
        "projectId": "ep_cached_edit",
        "sourceEpisodeId": "ep_cached_edit",
        "title": "Cached Edit",
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "revision": 0,
        "assets": {"card": {"id": "card", "type": "image", "src": str(tmp_path / "card.png"), "metadata": {}}},
        "tracks": {"graphics_1": {"id": "graphics_1", "kind": "graphics", "name": "Graphics", "index": 20}},
        "clips": {
            "card_clip": {
                "id": "card_clip",
                "assetId": "card",
                "trackId": "graphics_1",
                "kind": "artifact",
                "start": 0,
                "duration": 1,
                "sourceStart": 0,
                "enabled": True,
                "muted": False,
                "volume": 1,
                "transform": {"x": 0.5, "y": 0.5, "scale": 1, "opacity": 1},
                "effects": [],
                "keyframes": [],
                "metadata": {},
            }
        },
        "markers": {},
        "notes": {},
        "renderCache": {
            "video": {"backend": "openshot", "video": str(cached_video), "duration": 1.0}
        },
    }))

    response = sync_client.post(
        "/api/agenticnews/editor-timelines/ep_cached_edit/commands",
        json={
            "op": "clip.move",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"clipId": "card_clip", "start": 0.5},
        },
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 1
    assert "renderCache" not in response.json()
    saved = json.loads(timeline_path.read_text())
    assert "renderCache" not in saved


def test_editor_timeline_api_clip_visibility_toggles_flip_flags_and_drop_render_cache(
    sync_client, monkeypatch, tmp_path
):
    """clip.hide / clip.mute over HTTP flip the persisted enabled/muted flags and,
    because they change the rendered output, invalidate the render cache."""
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    timeline_dir = tmp_path / "editor_timelines"
    timeline_dir.mkdir()
    timeline_path = timeline_dir / "ep_toggle_edit.json"
    cached_video = tmp_path / "cached.mp4"
    cached_video.write_bytes(b"cached")
    timeline_path.write_text(json.dumps({
        "schema": "editor-timeline/v1",
        "projectId": "ep_toggle_edit",
        "sourceEpisodeId": "ep_toggle_edit",
        "title": "Toggle Edit",
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "revision": 0,
        "assets": {"card": {"id": "card", "type": "image", "src": str(tmp_path / "card.png"), "metadata": {}}},
        "tracks": {"graphics_1": {"id": "graphics_1", "kind": "graphics", "name": "Graphics", "index": 20}},
        "clips": {
            "card_clip": {
                "id": "card_clip",
                "assetId": "card",
                "trackId": "graphics_1",
                "kind": "artifact",
                "start": 0,
                "duration": 1,
                "sourceStart": 0,
                "enabled": True,
                "muted": False,
                "volume": 1,
                "transform": {"x": 0.5, "y": 0.5, "scale": 1, "opacity": 1},
                "effects": [],
                "keyframes": [],
                "metadata": {},
            }
        },
        "markers": {},
        "notes": {},
        "renderCache": {
            "video": {"backend": "openshot", "video": str(cached_video), "duration": 1.0}
        },
    }))

    hidden = sync_client.post(
        "/api/agenticnews/editor-timelines/ep_toggle_edit/commands",
        json={
            "op": "clip.hide",
            "actor": "human",
            "expectedRevision": 0,
            "payload": {"clipId": "card_clip"},
        },
    )
    assert hidden.status_code == 200
    assert hidden.json()["clips"]["card_clip"]["enabled"] is False
    assert "renderCache" not in hidden.json()

    muted = sync_client.post(
        "/api/agenticnews/editor-timelines/ep_toggle_edit/commands",
        json={
            "op": "clip.mute",
            "actor": "human",
            "expectedRevision": 1,
            "payload": {"clipId": "card_clip"},
        },
    )
    assert muted.status_code == 200
    assert muted.json()["clips"]["card_clip"]["muted"] is True

    saved = json.loads(timeline_path.read_text())
    assert saved["clips"]["card_clip"]["enabled"] is False
    assert saved["clips"]["card_clip"]["muted"] is True
    assert "renderCache" not in saved


def test_editor_timeline_non_output_command_refreshes_render_cache_revision(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    timeline_dir = tmp_path / "editor_timelines"
    timeline_dir.mkdir()
    timeline_path = timeline_dir / "ep_cached_note.json"
    cached_video = tmp_path / "cached.mp4"
    cached_window = tmp_path / "cached_window.mp4"
    cached_frame = tmp_path / "cached_frame.png"
    cached_video.write_bytes(b"cached")
    cached_window.write_bytes(b"cached window")
    cached_frame.write_bytes(b"cached frame")
    timeline_path.write_text(json.dumps({
        "schema": "editor-timeline/v1",
        "projectId": "ep_cached_note",
        "sourceEpisodeId": "ep_cached_note",
        "title": "Cached Note",
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "revision": 0,
        "assets": {},
        "tracks": {},
        "clips": {},
        "markers": {},
        "notes": {},
        "renderCache": {
            "video": {"backend": "openshot", "video": str(cached_video), "duration": 2.0, "revision": 0},
            "windows": {
                "0.50_1.00": {
                    "backend": "openshot",
                    "video": str(cached_window),
                    "start": 0.5,
                    "duration": 1.0,
                    "revision": 0,
                }
            },
            "frames": {
                "0.50": {"backend": "openshot", "frame": str(cached_frame), "at": 0.5, "revision": 0}
            },
        },
    }))

    response = sync_client.post(
        "/api/agenticnews/editor-timelines/ep_cached_note/commands",
        json={
            "op": "note.add",
            "actor": "human",
            "expectedRevision": 0,
            "payload": {"noteId": "n1", "target": {"time": 0.5}, "text": "Review timing"},
        },
    )

    assert response.status_code == 200
    project = response.json()
    assert project["revision"] == 1
    assert project["renderCache"]["video"]["revision"] == 1
    assert project["renderCache"]["windows"]["0.50_1.00"]["revision"] == 1
    assert project["renderCache"]["frames"]["0.50"]["revision"] == 1

    loaded = sync_client.get("/api/agenticnews/editor-timelines/ep_cached_note")
    assert loaded.status_code == 200
    assert loaded.json()["renderCache"]["video"]["video"] == str(cached_video)
    assert loaded.json()["renderCache"]["windows"]["0.50_1.00"]["video"] == str(cached_window)
    assert loaded.json()["renderCache"]["frames"]["0.50"]["frame"] == str(cached_frame)
    assert loaded.json()["renderCache"]["video"]["revision"] == 1


def test_editor_timeline_revert_last_rejects_stale_revision(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    timeline_dir = tmp_path / "editor_timelines"
    timeline_dir.mkdir()
    timeline_path = timeline_dir / "ep_undo_stale.json"
    timeline_path.write_text(json.dumps({
        "schema": "editor-timeline/v1",
        "projectId": "ep_undo_stale",
        "sourceEpisodeId": "ep_undo_stale",
        "title": "Undo Stale",
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "revision": 1,
        "assets": {"card": {"id": "card", "type": "image", "src": str(tmp_path / "card.png"), "metadata": {}}},
        "tracks": {"graphics_1": {"id": "graphics_1", "kind": "graphics", "name": "Graphics", "index": 20}},
        "clips": {
            "card_clip": {
                "id": "card_clip",
                "assetId": "card",
                "trackId": "graphics_1",
                "kind": "artifact",
                "start": 0.5,
                "duration": 1,
                "sourceStart": 0,
                "enabled": True,
                "muted": False,
                "volume": 1,
                "transform": {"x": 0.5, "y": 0.5, "scale": 1, "opacity": 1},
                "effects": [],
                "keyframes": [],
                "metadata": {},
            }
        },
        "markers": {},
        "notes": {},
        "commandLog": [
            {
                "id": "cmd_move",
                "op": "clip.move",
                "actor": "human",
                "expectedRevision": 0,
                "revision": 1,
                "payload": {"clipId": "card_clip", "start": 0.5},
                "before": {"clip": {"id": "card_clip", "assetId": "card", "trackId": "graphics_1", "kind": "artifact", "start": 0, "duration": 1, "sourceStart": 0, "enabled": True, "muted": False, "volume": 1, "transform": {"x": 0.5, "y": 0.5, "scale": 1, "opacity": 1}, "effects": [], "keyframes": [], "metadata": {}}},
                "after": {},
                "ts": 1,
            }
        ],
    }))

    response = sync_client.post(
        "/api/agenticnews/editor-timelines/ep_undo_stale/commands/revert-last",
        json={"actor": "human", "expectedRevision": 0},
    )

    assert response.status_code == 409
    assert "expected revision 0, current revision 1" in response.json()["detail"]


def test_editor_timeline_revert_last_output_change_clears_render_cache(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    timeline_dir = tmp_path / "editor_timelines"
    timeline_dir.mkdir()
    cached_video = tmp_path / "cached.mp4"
    cached_video.write_bytes(b"cached")
    timeline_path = timeline_dir / "ep_undo_cache.json"
    original_clip = {
        "id": "card_clip",
        "assetId": "card",
        "trackId": "graphics_1",
        "kind": "artifact",
        "start": 0,
        "duration": 1,
        "sourceStart": 0,
        "enabled": True,
        "muted": False,
        "volume": 1,
        "transform": {"x": 0.5, "y": 0.5, "scale": 1, "opacity": 1},
        "effects": [],
        "keyframes": [],
        "metadata": {},
    }
    moved_clip = {**original_clip, "start": 0.5}
    timeline_path.write_text(json.dumps({
        "schema": "editor-timeline/v1",
        "projectId": "ep_undo_cache",
        "sourceEpisodeId": "ep_undo_cache",
        "title": "Undo Cache",
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "revision": 1,
        "assets": {"card": {"id": "card", "type": "image", "src": str(tmp_path / "card.png"), "metadata": {}}},
        "tracks": {"graphics_1": {"id": "graphics_1", "kind": "graphics", "name": "Graphics", "index": 20}},
        "clips": {"card_clip": moved_clip},
        "markers": {},
        "notes": {},
        "renderCache": {"video": {"backend": "openshot", "video": str(cached_video), "duration": 1.0, "revision": 1}},
        "commandLog": [
            {
                "id": "cmd_move",
                "op": "clip.move",
                "actor": "human",
                "expectedRevision": 0,
                "revision": 1,
                "payload": {"clipId": "card_clip", "start": 0.5},
                "before": {"clip": original_clip},
                "after": {"clip": moved_clip},
                "ts": 1,
            }
        ],
    }))

    response = sync_client.post(
        "/api/agenticnews/editor-timelines/ep_undo_cache/commands/revert-last",
        json={"actor": "human", "expectedRevision": 1},
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 2
    assert response.json()["clips"]["card_clip"]["start"] == 0
    assert "renderCache" not in response.json()
    saved = json.loads(timeline_path.read_text())
    assert "renderCache" not in saved


def test_editor_timeline_revert_last_note_preserves_render_cache(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    timeline_dir = tmp_path / "editor_timelines"
    timeline_dir.mkdir()
    cached_video = tmp_path / "cached.mp4"
    cached_video.write_bytes(b"cached")
    timeline_path = timeline_dir / "ep_undo_note_cache.json"
    timeline_path.write_text(json.dumps({
        "schema": "editor-timeline/v1",
        "projectId": "ep_undo_note_cache",
        "sourceEpisodeId": "ep_undo_note_cache",
        "title": "Undo Note Cache",
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "revision": 1,
        "assets": {},
        "tracks": {},
        "clips": {},
        "markers": {},
        "notes": {"n1": {"id": "n1", "target": {"time": 1}, "text": "Review", "suggestedCommand": None, "metadata": {}}},
        "renderCache": {"video": {"backend": "openshot", "video": str(cached_video), "duration": 1.0, "revision": 1}},
        "commandLog": [
            {
                "id": "cmd_note",
                "op": "note.add",
                "actor": "human",
                "expectedRevision": 0,
                "revision": 1,
                "payload": {"noteId": "n1", "target": {"time": 1}, "text": "Review"},
                "before": {},
                "after": {"note": {"id": "n1", "target": {"time": 1}, "text": "Review", "suggestedCommand": None, "metadata": {}}},
                "ts": 1,
            }
        ],
    }))

    response = sync_client.post(
        "/api/agenticnews/editor-timelines/ep_undo_note_cache/commands/revert-last",
        json={"actor": "human", "expectedRevision": 1},
    )

    assert response.status_code == 200
    assert "n1" not in response.json()["notes"]
    assert response.json()["renderCache"]["video"]["video"] == str(cached_video)
    saved = json.loads(timeline_path.read_text())
    assert saved["renderCache"]["video"]["video"] == str(cached_video)


def test_editor_timeline_api_rejects_demo_timeline_ids(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)

    response = sync_client.get("/api/agenticnews/editor-timelines/demo_edit")

    assert response.status_code == 400
    assert "demo editor timelines are disabled" in response.json()["detail"]
