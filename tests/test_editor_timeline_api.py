import copy
import json

import pytest

import services.abn_assets as abn_assets
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


def test_editor_timeline_api_imports_complex_episode_without_dropping_metadata(
    sync_client, monkeypatch, tmp_path
):
    """End-to-end fidelity guard: a realistic episode carrying every effect type
    (shot effects, transitionSec crossfade, kenBurns, a per-shot ducking envelope,
    a music-bed ducking envelope, segment boundaries + segmentId tags, source-type
    tags) must survive the ABN -> Editor Bay import with nothing silently dropped.

    Pins the import path the prior fixes (516296bc opacity, 9907784b/937d1f79
    keyframe clamps) touched against a complex input it had never been tested with.
    """
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)

    imported = sync_client.post(
        "/api/agenticnews/editor-timelines/api_complex/import-abn",
        json={
            "sourceEpisodeId": "ep_cx",
            "timeline": {
                "episodeId": "ep_cx",
                "title": "Complex",
                "totalSec": 10.0,
                # music bed ships its own ducking envelope (absolute gains)
                "musicBed": {
                    "src": "/agenticnews-assets/bed.mp3",
                    "keyframes": [
                        {
                            "property": "volume",
                            "points": [
                                {"t": 0.0, "value": 0.6, "interp": "linear"},
                                {"t": 5.0, "value": 0.22, "interp": "linear"},
                            ],
                        }
                    ],
                },
                "segments": [
                    {
                        "segmentId": "segA",
                        "durationSec": 5.0,
                        "shots": [
                            {
                                "id": "shot0",
                                "type": "webscroll",
                                "src": "/agenticnews-assets/scroll.mp4",
                                "startSec": 0,
                                "durationSec": 5,
                                "kenBurns": {
                                    "startScale": 1.0,
                                    "endScale": 1.2,
                                    "startX": 0.4,
                                    "endX": 0.6,
                                },
                                "effects": [
                                    {"id": "fx1", "type": "fadeIn", "params": {"duration": 0.5}}
                                ],
                                "transitionSec": 0.4,
                                "keyframes": [
                                    {
                                        "property": "opacity",
                                        "points": [
                                            {"t": 0, "value": 0.0, "interp": "linear"},
                                            {"t": 1.0, "value": 1.0, "interp": "linear"},
                                        ],
                                    }
                                ],
                            }
                        ],
                        "audio": {"vo": {"src": "/agenticnews-assets/vo0.wav", "duration": 5.0}},
                    },
                    {
                        "segmentId": "segB",
                        "durationSec": 5.0,
                        "shots": [
                            {
                                "id": "shot1",
                                "type": "remotion",
                                "src": "/agenticnews-assets/term.mp4",
                                "startSec": 0,
                                "durationSec": 5,
                                # factory-attached ducking envelope on a sibling field
                                "duckingKeyframes": [
                                    {
                                        "property": "volume",
                                        "points": [
                                            {"t": 0, "value": 1.0, "interp": "linear"},
                                            {"t": 2.0, "value": 0.3, "interp": "linear"},
                                        ],
                                    }
                                ],
                            }
                        ],
                        "audio": {"vo": {"src": "/agenticnews-assets/vo1.wav", "duration": 5.0}},
                    },
                ],
            },
        },
    )
    assert imported.status_code == 201
    project = imported.json()
    clips = list(project["clips"].values())

    def clip_by_kind(kind):
        return next(c for c in clips if c["kind"] == kind)

    # Segment boundaries: segA at cursor 0, segB offset by segA's 5s duration.
    scroll = clip_by_kind("webscroll")
    term = clip_by_kind("remotion")
    assert scroll["start"] == 0.0
    assert term["start"] == 5.0
    # segmentId tags survive on every clip (markers of which segment owns the clip).
    assert scroll["metadata"]["segmentId"] == "segA"
    assert term["metadata"]["segmentId"] == "segB"

    # Source-type vocabulary survives onto the assets (OpenShot layer routing).
    sources = {a.get("source") for a in project["assets"].values() if a.get("source")}
    assert {"webscroll", "remotion"} <= sources

    # Shot effects + synthesized transitionSec crossfade both reach the clip.
    scroll_fx = {e["type"] for e in scroll["effects"]}
    assert "fadeIn" in scroll_fx
    assert "crossfade" in scroll_fx

    # kenBurns (scale/x) merged with the shot's explicit opacity envelope — none dropped.
    scroll_kf = {k["property"] for k in scroll["keyframes"]}
    assert {"scale", "x", "opacity"} <= scroll_kf

    # Per-shot ducking envelope on a sibling field promoted to a real volume track.
    assert any(k["property"] == "volume" for k in term["keyframes"])

    # Music-bed ducking envelope promoted; flat 0.22 gain neutralized to 1.0 so the
    # envelope's absolute gains pass through (no double-attenuation).
    bed = clip_by_kind("music_bed")
    assert any(k["property"] == "volume" for k in bed["keyframes"])
    assert bed["volume"] == 1.0

    # Raw shot preserved under metadata.shot for lossless re-import.
    assert scroll["metadata"]["shot"]["id"] == "shot0"


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


def test_editor_timeline_openshot_export_emits_path_for_missing_asset_file(
    sync_client, monkeypatch, tmp_path
):
    """Edge case the export contract must pin: an asset whose /agenticnews-assets/
    URL resolves to a file that does NOT exist on disk. The export does not crash
    and does not silently blank the path — it resolves the URL to a concrete
    <ASSETS_DIR>/<name> path so OpenShot's reader (and the asset-health surface)
    can see the gap. Missing-file detection is asset-health's job, not export's."""
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    timeline_dir = tmp_path / "editor_timelines"
    timeline_dir.mkdir()
    timeline_path = timeline_dir / "ep_missing_asset.json"
    timeline_path.write_text(json.dumps({
        "schema": "editor-timeline/v1",
        "projectId": "ep_missing_asset",
        "sourceEpisodeId": "ep_missing_asset",
        "title": "Missing Asset",
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "revision": 0,
        "assets": {
            "a_missing": {"id": "a_missing", "type": "image", "src": "/agenticnews-assets/gone.png"}
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
                "effects": [],
                "keyframes": [],
                "metadata": {},
            }
        },
        "markers": {},
        "notes": {},
    }))
    assert not (tmp_path / "gone.png").exists()

    response = sync_client.get("/api/agenticnews/editor-timelines/ep_missing_asset/openshot")

    assert response.status_code == 200
    clip = response.json()["timeline"]["clips"][0]
    # URL resolved to a concrete (still-missing) path, not silently blanked.
    assert clip["reader"]["path"] == str(tmp_path / "gone.png")
    assert clip["reader"]["type"] == "QtImageReader"


def test_editor_timeline_openshot_export_handles_empty_src_with_dummy_reader(
    sync_client, monkeypatch, tmp_path
):
    """A title/lower-third asset carries src="" (text is rendered, no file). The
    export must NOT invent a path — it falls back to an empty-path DummyReader so
    OpenShot treats it as a generated layer rather than a missing file."""
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    timeline_dir = tmp_path / "editor_timelines"
    timeline_dir.mkdir()
    timeline_path = timeline_dir / "ep_empty_src.json"
    timeline_path.write_text(json.dumps({
        "schema": "editor-timeline/v1",
        "projectId": "ep_empty_src",
        "sourceEpisodeId": "ep_empty_src",
        "title": "Empty Src",
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "revision": 0,
        "assets": {
            "lower": {
                "id": "lower",
                "type": "title",
                "src": "",
                "metadata": {"text": "Generated title"},
            }
        },
        "tracks": {"titles_1": {"id": "titles_1", "kind": "title", "name": "Titles", "index": 30}},
        "clips": {
            "lower_clip": {
                "id": "lower_clip",
                "assetId": "lower",
                "trackId": "titles_1",
                "kind": "lower_third",
                "start": 0,
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
    }))

    response = sync_client.get("/api/agenticnews/editor-timelines/ep_empty_src/openshot")

    assert response.status_code == 200
    clip = response.json()["timeline"]["clips"][0]
    assert clip["reader"]["path"] == ""
    assert clip["reader"]["type"] == "DummyReader"


def test_openshot_bridge_resolve_asset_src_url_edge_cases():
    """Unit-pin _resolve_asset_src so the export contract's path resolution can't
    regress: agenticnews-assets URLs join under asset_root, but only when a root is
    present; empty and non-prefixed srcs pass through untouched (never None, never
    raises)."""
    from pathlib import Path

    from services import openshot_bridge

    root = Path("/assets")
    # URL + root -> joined filesystem path
    assert openshot_bridge._resolve_asset_src(
        "/agenticnews-assets/card.png", asset_root=root
    ) == str(root / "card.png")
    # URL + NO root -> unresolvable, passes through as the URL (no crash, no None)
    assert openshot_bridge._resolve_asset_src(
        "/agenticnews-assets/card.png", asset_root=None
    ) == "/agenticnews-assets/card.png"
    # empty src -> empty (drives the DummyReader fallback)
    assert openshot_bridge._resolve_asset_src("", asset_root=root) == ""
    # already-absolute non-URL path -> untouched
    assert openshot_bridge._resolve_asset_src("/var/x.png", asset_root=root) == "/var/x.png"


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


def test_editor_timeline_command_rejects_invalid_payload_with_400(sync_client, monkeypatch, tmp_path):
    """An unsupported op / bad payload surfaces editor_timeline.CommandValidationError
    as a 400 with the validation reason, not an opaque 500."""
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    created = sync_client.post(
        "/api/agenticnews/editor-timelines",
        json={"projectId": "bad_op_proj", "title": "Bad Op"},
    )
    assert created.status_code == 201

    response = sync_client.post(
        "/api/agenticnews/editor-timelines/bad_op_proj/commands",
        json={"op": "clip.teleport", "actor": "agent", "expectedRevision": 0, "payload": {}},
    )

    assert response.status_code == 400
    assert "unsupported command op: clip.teleport" in response.json()["detail"]


def test_editor_timeline_command_rejects_non_integer_revision_with_400(sync_client, monkeypatch, tmp_path):
    """A non-integer expectedRevision triggers an int() ValueError inside apply_command;
    the router must map it to a clean 400, not leak a 500."""
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    created = sync_client.post(
        "/api/agenticnews/editor-timelines",
        json={"projectId": "bad_rev_proj", "title": "Bad Revision"},
    )
    assert created.status_code == 201

    response = sync_client.post(
        "/api/agenticnews/editor-timelines/bad_rev_proj/commands",
        json={
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": "not-a-number",
            "payload": {"assetId": "a1", "type": "image", "src": "/x.png"},
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "expectedRevision must be an integer"


def test_editor_timeline_command_missing_project_returns_404(sync_client, monkeypatch, tmp_path):
    """A command against a project that was never created hits store.load()'s
    FileNotFoundError and must come back as a 404, not a 500."""
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)

    response = sync_client.post(
        "/api/agenticnews/editor-timelines/never_created/commands",
        json={
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "image", "src": "/x.png"},
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "editor timeline not found"


def test_editor_timeline_revert_last_requires_expected_revision(sync_client, monkeypatch, tmp_path):
    """revert-last without expectedRevision is rejected up front with a 400."""
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    created = sync_client.post(
        "/api/agenticnews/editor-timelines",
        json={"projectId": "undo_no_rev", "title": "No Rev"},
    )
    assert created.status_code == 201

    response = sync_client.post(
        "/api/agenticnews/editor-timelines/undo_no_rev/commands/revert-last",
        json={"actor": "human"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "expectedRevision is required"


def test_editor_timeline_revert_last_rejects_non_integer_revision(sync_client, monkeypatch, tmp_path):
    """A non-integer expectedRevision on revert-last fails the int() cast and maps to 400."""
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    created = sync_client.post(
        "/api/agenticnews/editor-timelines",
        json={"projectId": "undo_bad_rev", "title": "Bad Rev"},
    )
    assert created.status_code == 201

    response = sync_client.post(
        "/api/agenticnews/editor-timelines/undo_bad_rev/commands/revert-last",
        json={"actor": "human", "expectedRevision": "abc"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "expectedRevision must be an integer"


def test_editor_timeline_revert_last_with_no_commands_returns_400(sync_client, monkeypatch, tmp_path):
    """Reverting a freshly-created project (empty command log) raises
    CommandValidationError, surfaced as a 400 with the reason."""
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    created = sync_client.post(
        "/api/agenticnews/editor-timelines",
        json={"projectId": "undo_empty", "title": "Empty Log"},
    )
    assert created.status_code == 201

    response = sync_client.post(
        "/api/agenticnews/editor-timelines/undo_empty/commands/revert-last",
        json={"actor": "human", "expectedRevision": 0},
    )

    assert response.status_code == 400
    assert "no command to revert" in response.json()["detail"]


def test_editor_timeline_revert_last_missing_project_returns_404(sync_client, monkeypatch, tmp_path):
    """revert-last against a non-existent project must be a 404, not a 500."""
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)

    response = sync_client.post(
        "/api/agenticnews/editor-timelines/never_created/commands/revert-last",
        json={"actor": "human", "expectedRevision": 0},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "editor timeline not found"


def test_editor_timeline_api_rejects_demo_timeline_ids(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)

    response = sync_client.get("/api/agenticnews/editor-timelines/demo_edit")

    assert response.status_code == 400
    assert "demo editor timelines are disabled" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Direct unit coverage for the render-cache sanitization helpers
# (routers/agenticnews.py lines 1138-1328). These run on every editor project
# load; a corruption here can make renders use stale/incorrect artifacts.
# ---------------------------------------------------------------------------


def _present(tmp_path, name: str) -> str:
    p = tmp_path / name
    p.write_bytes(b"x")
    return str(p)


def test_strip_source_reference_render_cache_drops_abn_source_backend():
    project = {"renderCache": {"video": {"backend": "abn-source", "video": "/x.mp4"}}}
    out, changed = agenticnews_router._strip_source_reference_render_cache(project)
    assert changed is True
    assert "video" not in out["renderCache"]
    # original is not mutated (deepcopy)
    assert project["renderCache"]["video"]["backend"] == "abn-source"


def test_strip_source_reference_render_cache_drops_episode_mp4_path():
    project = {"renderCache": {"video": {"backend": "openshot", "video": "/a/ep_episode.mp4"}}}
    out, changed = agenticnews_router._strip_source_reference_render_cache(project)
    assert changed is True
    assert "video" not in out["renderCache"]


def test_strip_source_reference_render_cache_drops_window_cache_by_start():
    project = {"renderCache": {"video": {"backend": "openshot", "video": "/a.mp4", "start": 1.5}}}
    out, changed = agenticnews_router._strip_source_reference_render_cache(project)
    assert changed is True
    assert "video" not in out["renderCache"]


def test_strip_source_reference_render_cache_drops_short_duration_against_clips():
    project = {
        "clips": {"c1": {"start": 0, "duration": 10}},
        "renderCache": {"video": {"backend": "openshot", "video": "/a.mp4", "duration": 4.0}},
    }
    out, changed = agenticnews_router._strip_source_reference_render_cache(project)
    assert changed is True
    assert "video" not in out["renderCache"]


def test_strip_source_reference_render_cache_keeps_full_length_cache():
    project = {
        "clips": {"c1": {"start": 0, "duration": 10}},
        "renderCache": {"video": {"backend": "openshot", "video": "/a.mp4", "duration": 10.0}},
    }
    out, changed = agenticnews_router._strip_source_reference_render_cache(project)
    assert changed is False
    assert out is project


def test_prune_missing_render_cache_removes_missing_video_window_frame(tmp_path):
    project = {
        "renderCache": {
            "video": {"video": str(tmp_path / "gone.mp4")},
            "windows": {"w": {"video": str(tmp_path / "gone-w.mp4")}},
            "frames": {"f": {"frame": str(tmp_path / "gone.png")}},
        }
    }
    out, changed = agenticnews_router._prune_missing_render_cache(project)
    assert changed is True
    # every entry was missing -> whole renderCache collapses
    assert "renderCache" not in out


def test_prune_missing_render_cache_keeps_present_files(tmp_path):
    project = {
        "renderCache": {
            "video": {"video": _present(tmp_path, "ok.mp4")},
            "windows": {"w": {"video": str(tmp_path / "gone-w.mp4")}},
        }
    }
    out, changed = agenticnews_router._prune_missing_render_cache(project)
    assert changed is True
    assert out["renderCache"]["video"]["video"].endswith("ok.mp4")
    assert "windows" not in out["renderCache"]


def test_prune_missing_render_cache_noop_when_all_present(tmp_path):
    project = {"renderCache": {"video": {"video": _present(tmp_path, "ok.mp4")}}}
    out, changed = agenticnews_router._prune_missing_render_cache(project)
    assert changed is False
    assert out is project


def test_prune_missing_render_cache_ignores_non_dict_cache():
    project = {"renderCache": "corrupt"}
    out, changed = agenticnews_router._prune_missing_render_cache(project)
    assert changed is False
    assert out is project


def test_prune_stale_revision_render_cache_drops_mismatched_entries(tmp_path):
    project = {
        "revision": 4,
        "renderCache": {
            "video": {"video": _present(tmp_path, "v.mp4"), "revision": 4},
            "windows": {"w": {"video": _present(tmp_path, "w.mp4"), "revision": 3}},
            "frames": {"f": {"frame": _present(tmp_path, "f.png"), "revision": 3}},
        },
    }
    out, changed = agenticnews_router._prune_stale_revision_render_cache(project)
    assert changed is True
    assert out["renderCache"]["video"]["revision"] == 4
    assert "windows" not in out["renderCache"]
    assert "frames" not in out["renderCache"]


def test_prune_stale_revision_render_cache_keeps_matching_revision(tmp_path):
    project = {
        "revision": 2,
        "renderCache": {"video": {"video": _present(tmp_path, "v.mp4"), "revision": 2}},
    }
    out, changed = agenticnews_router._prune_stale_revision_render_cache(project)
    assert changed is False
    assert out is project


def test_stamp_legacy_render_cache_revisions_stamps_unversioned_entries():
    project = {
        "revision": 7,
        "renderCache": {
            "video": {"video": "/v.mp4"},
            "windows": {"w": {"video": "/w.mp4"}},
            "frames": {"f": {"frame": "/f.png"}},
        },
    }
    out, changed = agenticnews_router._stamp_legacy_render_cache_revisions(project)
    assert changed is True
    assert out["renderCache"]["video"]["revision"] == 7
    assert out["renderCache"]["windows"]["w"]["revision"] == 7
    assert out["renderCache"]["frames"]["f"]["revision"] == 7


def test_stamp_legacy_render_cache_revisions_noop_when_already_stamped():
    project = {"revision": 1, "renderCache": {"video": {"video": "/v.mp4", "revision": 1}}}
    out, changed = agenticnews_router._stamp_legacy_render_cache_revisions(project)
    assert changed is False
    assert out is project


def test_render_cache_revision_mismatch_branches():
    # missing revision key -> treated as compatible (legacy entries)
    assert agenticnews_router._render_cache_revision_mismatch({}, 3) is False
    assert agenticnews_router._render_cache_revision_mismatch({"revision": 3}, 3) is False
    assert agenticnews_router._render_cache_revision_mismatch({"revision": 2}, 3) is True
    # unparseable revision -> treated as a mismatch (drop it)
    assert agenticnews_router._render_cache_revision_mismatch({"revision": "nope"}, 3) is True


def test_render_cache_path_exists_url_and_local(tmp_path, monkeypatch):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    assert agenticnews_router._render_cache_path_exists("") is False
    assert agenticnews_router._render_cache_path_exists("https://cdn/x.mp4") is True
    assert agenticnews_router._render_cache_path_exists("http://cdn/x.mp4") is True
    present = _present(tmp_path, "local.mp4")
    assert agenticnews_router._render_cache_path_exists(present) is True
    assert agenticnews_router._render_cache_path_exists(str(tmp_path / "missing.mp4")) is False
    # /agenticnews-assets/ prefix resolves under db.ASSETS_DIR
    (tmp_path / "card.png").write_bytes(b"c")
    assert agenticnews_router._render_cache_path_exists("/agenticnews-assets/card.png") is True
    assert agenticnews_router._render_cache_path_exists("/agenticnews-assets/nope.png") is False


def test_sanitize_render_cache_composes_all_stages(tmp_path, monkeypatch):
    """End-to-end at the function level: an abn-source full-video reference is
    stripped, a missing window is pruned, a present-but-unstamped frame gets the
    current revision stamped on. One pass, one combined 'changed' flag."""
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    project = {
        "revision": 5,
        "clips": {},
        "renderCache": {
            "video": {"backend": "abn-source", "video": "/agenticnews-assets/ep_episode.mp4"},
            "windows": {"w": {"video": str(tmp_path / "gone-w.mp4"), "revision": 5}},
            "frames": {"f": {"frame": _present(tmp_path, "f.png")}},
        },
    }
    out, changed = agenticnews_router._sanitize_render_cache(project)
    assert changed is True
    assert "video" not in out["renderCache"]
    assert "windows" not in out["renderCache"]
    assert out["renderCache"]["frames"]["f"]["revision"] == 5
    # original untouched
    assert project["renderCache"]["video"]["backend"] == "abn-source"


def test_sanitize_render_cache_clean_cache_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    project = {
        "revision": 1,
        "clips": {"c": {"start": 0, "duration": 2}},
        "renderCache": {
            "video": {"backend": "openshot", "video": _present(tmp_path, "v.mp4"), "duration": 2.0, "revision": 1}
        },
    }
    out, changed = agenticnews_router._sanitize_render_cache(project)
    assert changed is False
    assert out["renderCache"]["video"]["video"].endswith("v.mp4")


# ---------------------------------------------------------------------------
# _timeline_file_for_episode() — schema-first / flat-fallback contract with
# migration edge cases (restored by commit c161442c). A regression here would
# silently drop un-migrated episodes. The four straight branches are pinned in
# tests/test_editor_source_materialization.py; these cover the migration states
# the resolver must survive: flat-as-symlink (the real post-migration shape),
# the legacy `ep<N>` id form, and the schema-wins-over-stale-symlink transition.
# ---------------------------------------------------------------------------


@pytest.fixture
def assets_dir(tmp_path, monkeypatch):
    """Repoint BOTH captured ASSETS_DIR bindings at a temp dir.

    `_timeline_file_for_episode` reads `db.ASSETS_DIR` directly AND calls
    `abn_assets.episode_singleton_path`, which uses `abn_assets.ASSETS_DIR`
    (captured by value at import). Patch both or the resolver straddles dirs.
    """
    assets = tmp_path / "assets"
    assets.mkdir()
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", assets)
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", assets)
    return assets


def test_timeline_resolver_returns_flat_symlink_into_schema(assets_dir):
    """Post-migration shape: the flat legacy name is a SYMLINK pointing at the
    schema timeline.json, but no standalone schema file exists at the canonical
    path. The resolver must return the flat (symlink) path and it must read
    through to the real content — i.e. the back-compat link is honored, not
    dropped, for an episode the migration linked but didn't fully re-home."""
    ep = "ep_a1b2c3"
    real = assets_dir / ep / "renders" / "timeline_real.json"
    real.parent.mkdir(parents=True)
    real.write_text('{"segments": [{"id": "s0"}]}')

    flat = assets_dir / f"{ep}_timeline.json"
    flat.symlink_to(real)

    # canonical schema path itself is absent on disk
    assert not abn_assets.episode_singleton_path(ep, "timeline").exists()

    resolved = agenticnews_router._timeline_file_for_episode(ep)
    assert resolved == flat
    assert json.loads(resolved.read_text())["segments"][0]["id"] == "s0"


def test_timeline_resolver_schema_wins_over_stale_flat_symlink(assets_dir):
    """A fully-migrated episode: the schema timeline.json exists AND a flat
    symlink still lingers (pointing at stale content). Schema-first means the
    resolver returns the real schema path, never the legacy symlink — proving
    the un-migrated -> migrated transition can't resurrect stale data."""
    ep = "ep_d4e5f6"
    schema = abn_assets.asset_path(ep, "timeline")
    schema.write_text('{"segments": [{"id": "FRESH"}]}')

    stale = assets_dir / "stale.json"
    stale.write_text('{"segments": [{"id": "STALE"}]}')
    flat = assets_dir / f"{ep}_timeline.json"
    flat.symlink_to(stale)

    resolved = agenticnews_router._timeline_file_for_episode(ep)
    assert resolved == schema
    assert json.loads(resolved.read_text())["segments"][0]["id"] == "FRESH"


def test_timeline_resolver_handles_legacy_ep_n_id_schema_first(assets_dir):
    """The legacy `ep<N>` id form (e.g. `ep2`) is a valid episode id via the
    second regex branch in abn_assets._validate_ep, so the gateway scopes it and
    schema-first applies — the flat name is ignored when schema is present."""
    ep = "ep2"
    schema = abn_assets.asset_path(ep, "timeline")
    schema.write_text('{"segments": [{"id": "s0"}]}')
    (assets_dir / f"{ep}_timeline.json").write_text('{"segments": [{"id": "STALE"}]}')

    assert agenticnews_router._timeline_file_for_episode(ep) == schema


def test_timeline_resolver_legacy_ep_n_id_flat_fallback(assets_dir):
    """A legacy `ep<N>` episode with ONLY the flat file (never migrated to the
    schema dir) must still resolve to the flat path — the un-migrated read path
    a regression would silently drop."""
    ep = "ep2"
    flat = assets_dir / f"{ep}_timeline.json"
    flat.write_text('{"segments": [{"id": "s0"}]}')
    assert not abn_assets.episode_singleton_path(ep, "timeline").exists()

    assert agenticnews_router._timeline_file_for_episode(ep) == flat


def test_timeline_resolver_flat_fallback_is_exists_gated(assets_dir):
    """The flat fallback fires on `flat.exists()`. With NO flat file and NO
    schema file, the resolver returns the canonical schema path (so a downstream
    .exists() check fails on the path the factory WOULD write, never a phantom
    flat name) — the empty-disk migration state."""
    ep = "ep_c0ffee"
    assert not (assets_dir / f"{ep}_timeline.json").exists()

    resolved = agenticnews_router._timeline_file_for_episode(ep)
    assert resolved == abn_assets.episode_singleton_path(ep, "timeline")
    assert not resolved.exists()


# ---------------------------------------------------------------------------
# ffmpeg subprocess failure handling in source materialization.
#
# `_extract_audio_window` / `_extract_video_window` shell out to ffmpeg with
# `check=True` and no error handling. A missing ffmpeg binary, a corrupt /
# audio-less source, or a slow encode would otherwise leak a raw
# FileNotFoundError / CalledProcessError / TimeoutExpired straight up through
# `_plan_editor_source_materialization(materialize=True)`. These tests pin that
# all three are translated into the single, catchable
# `EditorSourceExtractionError` carrying ffmpeg's stderr.
# ---------------------------------------------------------------------------

import subprocess


def _materialization_timeline():
    return {
        "segments": [
            {
                "durationSec": 5.0,
                "audio": {"vo": {"src": "/agenticnews-assets/seg0_vo.wav"}},
                "shots": [],
            }
        ]
    }


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError("ffmpeg"),
        subprocess.CalledProcessError(
            returncode=1, cmd=["ffmpeg"], stderr="seg0_vo.wav: Output file does not contain any stream"
        ),
        subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=180),
    ],
    ids=["ffmpeg_missing", "nonzero_exit", "timeout"],
)
def test_extract_audio_window_translates_subprocess_failures(monkeypatch, tmp_path, exc):
    def boom(*_a, **_k):
        raise exc

    monkeypatch.setattr(agenticnews_router.subprocess, "run", boom)

    with pytest.raises(agenticnews_router.EditorSourceExtractionError) as caught:
        agenticnews_router._extract_audio_window(
            tmp_path / "episode.mp4", tmp_path / "out" / "seg0_vo.wav", start=0.0, duration=5.0
        )
    # The clean error names the target so a failure is diagnosable.
    assert "seg0_vo.wav" in str(caught.value)
    # The original subprocess exception is preserved on the chain.
    assert caught.value.__cause__ is exc


def test_extract_video_window_translates_subprocess_failures(monkeypatch, tmp_path):
    err = subprocess.CalledProcessError(returncode=1, cmd=["ffmpeg"], stderr="corrupt input")

    def boom(*_a, **_k):
        raise err

    monkeypatch.setattr(agenticnews_router.subprocess, "run", boom)

    with pytest.raises(agenticnews_router.EditorSourceExtractionError) as caught:
        agenticnews_router._extract_video_window(
            tmp_path / "episode.mp4", tmp_path / "out" / "seg0_demo.mp4", start=0.0, duration=5.0
        )
    assert "corrupt input" in str(caught.value)
    assert caught.value.__cause__ is err


def test_materialization_surfaces_clean_error_on_ffmpeg_failure(assets_dir, monkeypatch):
    """When ffmpeg fails mid-materialization the caller gets one clean,
    catchable EditorSourceExtractionError instead of a raw subprocess exception."""
    monkeypatch.setattr(
        agenticnews_router, "EDITOR_ALLOW_FLATTENED_SOURCE_MATERIALIZATION", True
    )
    # Flattened episode render present so the extract path is reached.
    (assets_dir / "vid1_episode.mp4").write_bytes(b"EPISODE")

    def boom(*_a, **_k):
        raise subprocess.CalledProcessError(
            returncode=1, cmd=["ffmpeg"], stderr="no audio stream"
        )

    monkeypatch.setattr(agenticnews_router.subprocess, "run", boom)

    with pytest.raises(agenticnews_router.EditorSourceExtractionError):
        agenticnews_router._plan_editor_source_materialization(
            "vid1", _materialization_timeline(), materialize=True
        )

    # Planning (materialize=False) never shells out, so it stays unaffected.
    plan = agenticnews_router._plan_editor_source_materialization(
        "vid1", _materialization_timeline(), materialize=False
    )
    assert any(e["type"] == "audio" for e in plan)


# ============ EDITOR BAY review-notes endpoints (/editor/{ep_id}/notes) ============
# Regression coverage for the review scratchpad notes persisted to review_notes.json.
# These are distinct from the editor-timeline command system above: they round-trip
# operator review notes through _load_review_notes / _save_review_notes (atomic write).
# _REVIEW_NOTES is bound at import time, so tests must repoint it (not db.ASSETS_DIR).


def _patch_review_notes(monkeypatch, tmp_path):
    """Repoint the module-level notes file at a temp path and stub out the DB
    lookup so the endpoints never touch the real SQLite videos table (no video
    found -> rid falls back to the ep_id, which is the normal scratchpad path)."""
    notes_file = tmp_path / "review_notes.json"
    monkeypatch.setattr(agenticnews_router, "_REVIEW_NOTES", notes_file)

    async def _no_video(_ep_id):
        return None

    monkeypatch.setattr(agenticnews_router, "_find_video", _no_video)
    return notes_file


def test_editor_save_note_appends_and_persists(sync_client, monkeypatch, tmp_path):
    notes_file = _patch_review_notes(monkeypatch, tmp_path)

    resp = sync_client.post(
        "/api/agenticnews/editor/ep_notes/notes",
        json={"t": 1.5, "kind": "pin", "track": "vo", "text": "tighten pacing"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["epId"] == "ep_notes"
    assert body["count"] == 1
    # An id is auto-assigned when none is supplied.
    assert body["note"]["id"]

    saved = json.loads(notes_file.read_text())
    assert saved["ep_notes"][0]["text"] == "tighten pacing"
    assert saved["ep_notes"][0]["id"] == body["note"]["id"]


def test_editor_save_note_upserts_by_id(sync_client, monkeypatch, tmp_path):
    notes_file = _patch_review_notes(monkeypatch, tmp_path)

    first = sync_client.post(
        "/api/agenticnews/editor/ep_up/notes",
        json={"id": "n1", "t": 0.0, "kind": "frame", "text": "original"},
    )
    assert first.status_code == 200
    assert first.json()["count"] == 1

    # Same id -> replace in place, not append.
    updated = sync_client.post(
        "/api/agenticnews/editor/ep_up/notes",
        json={"id": "n1", "t": 0.0, "kind": "frame", "text": "revised"},
    )
    assert updated.status_code == 200
    assert updated.json()["count"] == 1

    saved = json.loads(notes_file.read_text())
    assert len(saved["ep_up"]) == 1
    assert saved["ep_up"][0]["text"] == "revised"


def test_editor_delete_note_removes_matching_and_reports_count(sync_client, monkeypatch, tmp_path):
    notes_file = _patch_review_notes(monkeypatch, tmp_path)

    sync_client.post(
        "/api/agenticnews/editor/ep_del/notes",
        json={"id": "keep", "text": "keep me"},
    )
    sync_client.post(
        "/api/agenticnews/editor/ep_del/notes",
        json={"id": "drop", "text": "drop me"},
    )

    resp = sync_client.delete("/api/agenticnews/editor/ep_del/notes/drop")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "epId": "ep_del", "removed": 1}

    saved = json.loads(notes_file.read_text())
    assert [n["id"] for n in saved["ep_del"]] == ["keep"]


def test_editor_delete_unknown_note_is_noop(sync_client, monkeypatch, tmp_path):
    _patch_review_notes(monkeypatch, tmp_path)
    sync_client.post(
        "/api/agenticnews/editor/ep_noop/notes",
        json={"id": "n1", "text": "only note"},
    )

    resp = sync_client.delete("/api/agenticnews/editor/ep_noop/notes/does_not_exist")

    assert resp.status_code == 200
    assert resp.json()["removed"] == 0


def test_editor_save_note_recovers_from_corrupt_notes_file(sync_client, monkeypatch, tmp_path):
    """A truncated/garbage review_notes.json must not 500 the save path: the loader
    swallows the parse error and treats the store as empty, so the new note still lands."""
    notes_file = _patch_review_notes(monkeypatch, tmp_path)
    notes_file.write_text("{ this is not valid json")

    resp = sync_client.post(
        "/api/agenticnews/editor/ep_corrupt/notes",
        json={"text": "after corruption"},
    )

    assert resp.status_code == 200
    assert resp.json()["count"] == 1
    saved = json.loads(notes_file.read_text())
    assert saved["ep_corrupt"][0]["text"] == "after corruption"


def test_editor_save_note_surfaces_write_failure(sync_client, monkeypatch, tmp_path):
    """If the atomic write fails (e.g. read-only volume), the error propagates rather
    than silently dropping the operator's note. TestClient re-raises unhandled server
    exceptions, so the OSError surfaces here instead of being swallowed."""
    _patch_review_notes(monkeypatch, tmp_path)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(agenticnews_router, "atomic_save", boom)

    with pytest.raises(OSError, match="disk full"):
        sync_client.post(
            "/api/agenticnews/editor/ep_writefail/notes",
            json={"text": "never persisted"},
        )


# ---------------------------------------------------------------------------
# Target-directory creation failures (disk-full / no-perms / concurrent races).
#
# `_run_ffmpeg_extract` mkdir's the target's parent before shelling out. A
# disk-full (OSError/ENOSPC), a read-only mount, or a concurrent worker that
# planted a *file* where the dir should be (FileExistsError) must NOT leak a raw
# OSError past the extractor — it should be wrapped in the same catchable
# EditorSourceExtractionError as the ffmpeg failures so callers handle one type.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mkdir_exc",
    [
        OSError(28, "No space left on device"),  # disk full during mkdir
        PermissionError(13, "Permission denied"),  # read-only / unwritable mount
    ],
    ids=["disk_full", "no_perms"],
)
def test_extract_audio_window_wraps_mkdir_failures(monkeypatch, tmp_path, mkdir_exc):
    def boom_mkdir(*_a, **_k):
        raise mkdir_exc

    monkeypatch.setattr(agenticnews_router.Path, "mkdir", boom_mkdir)
    # subprocess.run must never be reached when the directory can't be made.
    monkeypatch.setattr(
        agenticnews_router.subprocess,
        "run",
        lambda *_a, **_k: pytest.fail("ffmpeg ran despite mkdir failure"),
    )

    with pytest.raises(agenticnews_router.EditorSourceExtractionError) as caught:
        agenticnews_router._extract_audio_window(
            tmp_path / "episode.mp4", tmp_path / "out" / "seg0_vo.wav", start=0.0, duration=5.0
        )
    assert "seg0_vo.wav" in str(caught.value)
    assert caught.value.__cause__ is mkdir_exc


def test_extract_video_window_wraps_concurrent_dir_race(monkeypatch, tmp_path):
    """A concurrent worker planted a *file* where the target dir should go;
    mkdir(parents=True, exist_ok=True) raises FileExistsError, which must be
    translated rather than leaked as a raw OSError."""
    # Real on-disk race: parent path exists as a file, not a directory.
    clash = tmp_path / "out"
    clash.write_bytes(b"not a dir")

    monkeypatch.setattr(
        agenticnews_router.subprocess,
        "run",
        lambda *_a, **_k: pytest.fail("ffmpeg ran despite mkdir failure"),
    )

    with pytest.raises(agenticnews_router.EditorSourceExtractionError) as caught:
        agenticnews_router._extract_video_window(
            clash / "seg0_demo.mp4", clash / "seg0_demo.mp4", start=0.0, duration=5.0
        )
    assert isinstance(caught.value.__cause__, OSError)


def test_extract_audio_window_wraps_disk_full_during_encode(monkeypatch, tmp_path):
    """Disk fills *during* the ffmpeg encode: ffmpeg exits non-zero with an
    ENOSPC stderr. That surfaces as a clean error carrying ffmpeg's message."""
    err = subprocess.CalledProcessError(
        returncode=1, cmd=["ffmpeg"], stderr="av_interleaved_write_frame(): No space left on device"
    )

    def boom(*_a, **_k):
        raise err

    monkeypatch.setattr(agenticnews_router.subprocess, "run", boom)

    with pytest.raises(agenticnews_router.EditorSourceExtractionError) as caught:
        agenticnews_router._extract_audio_window(
            tmp_path / "episode.mp4", tmp_path / "out" / "seg0_vo.wav", start=0.0, duration=5.0
        )
    assert "No space left on device" in str(caught.value)
    assert caught.value.__cause__ is err


def test_editor_timeline_api_track_create_and_validation(sync_client, monkeypatch, tmp_path):
    """track.create over HTTP adds a new track to the project (beyond the five
    default tracks) and a clip can then be created on it; a track.create missing
    trackId is rejected by command validation as a 400. Covers the apply_command
    track.create branch and _track_from_payload, which had no API-level test."""
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)

    created = sync_client.post(
        "/api/agenticnews/editor-timelines",
        json={"projectId": "track_proj", "title": "Track Project"},
    )
    assert created.status_code == 201
    # New projects ship the five default tracks.
    assert set(created.json()["tracks"]) == {
        "video_1", "graphics_1", "titles_1", "audio_1", "music_1"
    }

    # track.create adds a brand-new track with the payload's fields applied.
    new_track = sync_client.post(
        "/api/agenticnews/editor-timelines/track_proj/commands",
        json={
            "op": "track.create",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {
                "trackId": "overlay_1",
                "kind": "graphics",
                "name": "Overlay",
                "index": 25,
                "locked": True,
            },
        },
    )
    assert new_track.status_code == 200
    body = new_track.json()
    assert body["revision"] == 1
    track = body["tracks"]["overlay_1"]
    assert track == {
        "id": "overlay_1",
        "kind": "graphics",
        "name": "Overlay",
        "index": 25,
        "locked": True,
    }

    # The new track is real enough to hang a clip on (clip.create validates trackId).
    asset = sync_client.post(
        "/api/agenticnews/editor-timelines/track_proj/commands",
        json={
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {"assetId": "a1", "type": "image", "src": "/agenticnews-assets/card.png"},
        },
    )
    assert asset.status_code == 200
    clip = sync_client.post(
        "/api/agenticnews/editor-timelines/track_proj/commands",
        json={
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "overlay_1",
                "start": 0,
                "duration": 1,
            },
        },
    )
    assert clip.status_code == 200
    assert clip.json()["clips"]["c1"]["trackId"] == "overlay_1"

    # A track.create with no trackId/id is rejected by command validation (400).
    bad = sync_client.post(
        "/api/agenticnews/editor-timelines/track_proj/commands",
        json={
            "op": "track.create",
            "actor": "agent",
            "expectedRevision": 3,
            "payload": {"kind": "graphics", "name": "Nameless"},
        },
    )
    assert bad.status_code == 400
    assert "trackId is required" in bad.json()["detail"]


def test_editor_timeline_track_create_defaults_via_command_core():
    """Unit-pin _track_from_payload / apply_command track.create defaults: a payload
    with only an `id` gets kind=video, name=id, index=0, locked=False, and the track
    lands in project['tracks'] with the revision bumped."""
    from services import editor_timeline

    project = editor_timeline.new_project("p")
    result = editor_timeline.apply_command(
        project,
        {
            "op": "track.create",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"id": "bare_1"},
        },
    )
    assert result["revision"] == 1
    assert result["tracks"]["bare_1"] == {
        "id": "bare_1",
        "kind": "video",
        "name": "bare_1",
        "index": 0,
        "locked": False,
    }

# ---------------------------------------------------------------------------
# Direct unit coverage for clip.effect.* commands and _validated_effect
# (services/editor_timeline.py lines 1136-1158 + 1298-1327). These power the
# ABN editor-bay brightness/saturation/fade effects on clips; the add/update/
# delete ops and their validation were previously untested.
# ---------------------------------------------------------------------------


def _project_with_clip():
    """Build a project carrying a single video clip via the public command path."""
    from services import editor_timeline

    project = editor_timeline.new_project("p")
    project = editor_timeline.apply_command(
        project,
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"id": "a1", "type": "video", "src": "/x.mp4"},
        },
    )
    track_id = next(iter(project["tracks"]))
    project = editor_timeline.apply_command(
        project,
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": track_id,
                "start": 0.0,
                "duration": 5.0,
            },
        },
    )
    return editor_timeline, project


def _effect_cmd(op, expected, payload):
    return {"op": op, "actor": "agent", "expectedRevision": expected, "payload": payload}


def test_clip_effect_add_appends_validated_effect():
    et, project = _project_with_clip()
    out = et.apply_command(
        project,
        _effect_cmd(
            "clip.effect.add",
            project["revision"],
            {
                "clipId": "c1",
                "effect": {"id": "fx1", "type": "brightness", "params": {"value": 0.5}},
            },
        ),
    )
    assert out["clips"]["c1"]["effects"] == [
        {"id": "fx1", "type": "brightness", "params": {"value": 0.5}}
    ]
    assert out["revision"] == project["revision"] + 1


def test_clip_effect_add_accepts_inline_payload_shape():
    """The effect can be supplied inline on the payload (no nested `effect` key)."""
    et, project = _project_with_clip()
    out = et.apply_command(
        project,
        _effect_cmd(
            "clip.effect.add",
            project["revision"],
            {"clipId": "c1", "id": "fx_inline", "type": "fadeIn", "params": {"duration": 0.5}},
        ),
    )
    assert out["clips"]["c1"]["effects"][0]["id"] == "fx_inline"


def test_clip_effect_add_rejects_duplicate_id():
    et, project = _project_with_clip()
    project = et.apply_command(
        project,
        _effect_cmd(
            "clip.effect.add",
            project["revision"],
            {"clipId": "c1", "effect": {"id": "dup", "type": "saturation", "params": {"value": 1.0}}},
        ),
    )
    with pytest.raises(et.CommandValidationError, match="effect already exists: dup"):
        et.apply_command(
            project,
            _effect_cmd(
                "clip.effect.add",
                project["revision"],
                {"clipId": "c1", "effect": {"id": "dup", "type": "saturation", "params": {"value": 2.0}}},
            ),
        )


def test_clip_effect_update_replaces_matching_effect():
    et, project = _project_with_clip()
    project = et.apply_command(
        project,
        _effect_cmd(
            "clip.effect.add",
            project["revision"],
            {"clipId": "c1", "effect": {"id": "fx1", "type": "brightness", "params": {"value": 0.2}}},
        ),
    )
    out = et.apply_command(
        project,
        _effect_cmd(
            "clip.effect.update",
            project["revision"],
            {"clipId": "c1", "effect": {"id": "fx1", "type": "brightness", "params": {"value": -0.4}}},
        ),
    )
    assert out["clips"]["c1"]["effects"] == [
        {"id": "fx1", "type": "brightness", "params": {"value": -0.4}}
    ]


def test_clip_effect_update_rejects_missing_effect():
    et, project = _project_with_clip()
    with pytest.raises(et.CommandValidationError, match="effect does not exist: ghost"):
        et.apply_command(
            project,
            _effect_cmd(
                "clip.effect.update",
                project["revision"],
                {"clipId": "c1", "effect": {"id": "ghost", "type": "brightness", "params": {"value": 0.1}}},
            ),
        )


def test_clip_effect_delete_removes_effect_by_id():
    et, project = _project_with_clip()
    for fid in ("fx1", "fx2"):
        project = et.apply_command(
            project,
            _effect_cmd(
                "clip.effect.add",
                project["revision"],
                {"clipId": "c1", "effect": {"id": fid, "type": "saturation", "params": {"value": 1.0}}},
            ),
        )
    out = et.apply_command(
        project,
        _effect_cmd("clip.effect.delete", project["revision"], {"clipId": "c1", "effectId": "fx1"}),
    )
    assert [e["id"] for e in out["clips"]["c1"]["effects"]] == ["fx2"]


def test_clip_effect_delete_rejects_missing_effect():
    et, project = _project_with_clip()
    with pytest.raises(et.CommandValidationError, match="effect does not exist: nope"):
        et.apply_command(
            project,
            _effect_cmd("clip.effect.delete", project["revision"], {"clipId": "c1", "effectId": "nope"}),
        )


def test_validated_effect_rejects_unsupported_type():
    from services import editor_timeline

    with pytest.raises(editor_timeline.CommandValidationError, match="unsupported effect type: blur"):
        editor_timeline._validated_effect({"id": "x", "type": "blur", "params": {}})


def test_validated_effect_rejects_unknown_param():
    from services import editor_timeline

    with pytest.raises(editor_timeline.CommandValidationError, match="unsupported effect params"):
        editor_timeline._validated_effect(
            {"id": "x", "type": "brightness", "params": {"value": 0.5, "gamma": 1.0}}
        )


def test_validated_effect_rejects_missing_required_param():
    from services import editor_timeline

    with pytest.raises(editor_timeline.CommandValidationError, match="effect param required: value"):
        editor_timeline._validated_effect({"id": "x", "type": "brightness", "params": {}})


def test_validated_effect_rejects_out_of_bounds_param():
    from services import editor_timeline

    # brightness is bounded to [-1.0, 1.0]; 2.0 is out of range.
    with pytest.raises(editor_timeline.CommandValidationError, match="must be between"):
        editor_timeline._validated_effect(
            {"id": "x", "type": "brightness", "params": {"value": 2.0}}
        )


def test_validated_effect_requires_id():
    from services import editor_timeline

    with pytest.raises(editor_timeline.CommandValidationError, match="effect id is required"):
        editor_timeline._validated_effect({"type": "brightness", "params": {"value": 0.5}})


# ---------------------------------------------------------------------------
# replay_project + CommandValidationError seams (from ticket cb9-13).
# ---------------------------------------------------------------------------


def _project_with_clip_replay():
    """A fresh project carrying one asset + one clip, ready for split/mutate."""
    from services import editor_timeline

    project = editor_timeline.new_project("replay_proj")
    project = editor_timeline.apply_command(
        project,
        {
            "id": "cmd_asset",
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/agenticnews-assets/clip.mp4"},
        },
    )
    project = editor_timeline.apply_command(
        project,
        {
            "id": "cmd_clip",
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 0,
                "duration": 4,
            },
        },
    )
    return project


def test_replay_project_reconstructs_state_from_command_log():
    """replay_project replays the command log onto a fresh base and must reproduce
    the same clips/revision as the live project (timeline persistence guard)."""
    from services import editor_timeline

    project = _project_with_clip_replay()
    # A few more output-changing edits so the log is non-trivial.
    project = editor_timeline.apply_command(
        project,
        {
            "id": "cmd_move",
            "op": "clip.move",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "start": 1.0},
        },
    )
    project = editor_timeline.apply_command(
        project,
        {
            "id": "cmd_split",
            "op": "clip.split",
            "actor": "agent",
            "expectedRevision": 3,
            "payload": {"clipId": "c1", "at": 2.0, "newClipId": "c2"},
        },
    )

    replayed = editor_timeline.replay_project(project)

    assert replayed["revision"] == project["revision"]
    assert set(replayed["clips"]) == set(project["clips"]) == {"c1", "c2"}
    assert replayed["clips"]["c1"]["start"] == project["clips"]["c1"]["start"]
    assert replayed["clips"]["c2"]["start"] == project["clips"]["c2"]["start"]
    # The replayed log preserves the original command ids (not freshly minted).
    assert [e["id"] for e in replayed["commandLog"]] == [
        e["id"] for e in project["commandLog"]
    ]


def test_replay_project_carries_reverts_command_id():
    """A revert command in the log must replay through the revertsCommandId branch
    without tripping the 'already reverted' / 'does not reference' validators."""
    from services import editor_timeline

    project = _project_with_clip_replay()
    project = editor_timeline.apply_command(
        project,
        {
            "id": "cmd_move",
            "op": "clip.move",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "start": 1.5},
        },
    )
    # Revert the move via the store-style inverse so the log has a revertsCommandId.
    inverse = editor_timeline._inverse_command(
        project["commandLog"][-1], actor="agent", expected_revision=project["revision"]
    )
    project = editor_timeline.apply_command(project, inverse)
    assert project["clips"]["c1"]["start"] == 0.0

    replayed = editor_timeline.replay_project(project)
    assert replayed["clips"]["c1"]["start"] == 0.0
    assert any(e.get("revertsCommandId") for e in replayed["commandLog"])


def test_validate_reverts_command_id_unknown_target():
    from services import editor_timeline

    project = _project_with_clip_replay()
    with pytest.raises(editor_timeline.CommandValidationError, match="does not reference a command"):
        editor_timeline.apply_command(
            project,
            {
                "op": "clip.move",
                "actor": "agent",
                "expectedRevision": 2,
                "revertsCommandId": "no_such_cmd",
                "payload": {"clipId": "c1", "start": 0},
            },
        )


def test_validate_reverts_command_id_already_reverted():
    from services import editor_timeline

    project = _project_with_clip_replay()
    project = editor_timeline.apply_command(
        project,
        {
            "id": "cmd_move",
            "op": "clip.move",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "start": 1.5},
        },
    )
    inverse = editor_timeline._inverse_command(
        project["commandLog"][-1], actor="agent", expected_revision=project["revision"]
    )
    project = editor_timeline.apply_command(project, inverse)

    # Re-issuing the same revert must be rejected: cmd_move was already reverted.
    inverse2 = dict(inverse)
    inverse2["expectedRevision"] = project["revision"]
    inverse2.pop("id", None)
    with pytest.raises(editor_timeline.CommandValidationError, match="already reverted"):
        editor_timeline.apply_command(project, inverse2)


def test_validate_reverts_command_id_payload_mismatch():
    """revertsCommandId pointing at a real command, but with a payload that is NOT
    the true inverse, is rejected."""
    from services import editor_timeline

    project = _project_with_clip_replay()
    project = editor_timeline.apply_command(
        project,
        {
            "id": "cmd_move",
            "op": "clip.move",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "start": 1.5},
        },
    )
    # The true inverse of a clip.move is a clip.update restore; reusing that op but
    # with a bogus payload trips the payload-mismatch validator.
    with pytest.raises(editor_timeline.CommandValidationError, match="payload does not match"):
        editor_timeline.apply_command(
            project,
            {
                "op": "clip.update",
                "actor": "agent",
                "expectedRevision": 3,
                "revertsCommandId": "cmd_move",
                "payload": {"clipId": "c1", "patch": {"start": 99.0}},
            },
        )


def test_validate_reverts_command_id_op_mismatch():
    """revertsCommandId points at a real command, but the supplied op is not the
    inverse op the command would produce."""
    from services import editor_timeline

    project = _project_with_clip_replay()
    project = editor_timeline.apply_command(
        project,
        {
            "id": "cmd_move",
            "op": "clip.move",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "start": 1.5},
        },
    )
    # Inverse of clip.move is clip.update; offering clip.move is an op mismatch.
    with pytest.raises(editor_timeline.CommandValidationError, match="op does not match inverse command"):
        editor_timeline.apply_command(
            project,
            {
                "op": "clip.move",
                "actor": "agent",
                "expectedRevision": 3,
                "revertsCommandId": "cmd_move",
                "payload": {"clipId": "c1", "start": 0},
            },
        )


def test_clip_unsplit_requires_original_and_created_ids():
    from services import editor_timeline

    project = _project_with_clip_replay()
    with pytest.raises(editor_timeline.CommandValidationError, match="requires original clip"):
        editor_timeline.apply_command(
            project,
            {
                "op": "clip.unsplit",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {"createdClipId": "c2"},
            },
        )
    with pytest.raises(editor_timeline.CommandValidationError, match="requires createdClipId"):
        editor_timeline.apply_command(
            project,
            {
                "op": "clip.unsplit",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {"clip": {"id": "c1"}},
            },
        )


def test_clip_unsplit_requires_revert_of_recorded_split():
    """clip.unsplit with no revertsCommandId, and one that points at a non-split
    command, are both rejected by _validate_unsplit_reverts_recorded_split."""
    from services import editor_timeline

    project = _project_with_clip_replay()
    # Missing revertsCommandId.
    with pytest.raises(editor_timeline.CommandValidationError, match="requires revertsCommandId"):
        editor_timeline.apply_command(
            project,
            {
                "op": "clip.unsplit",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {"clip": {"id": "c1"}, "createdClipId": "c2"},
            },
        )
    # revertsCommandId points at cmd_clip (a clip.create, which has no inverse) so
    # the generic reverts validator rejects it before the unsplit-specific seam.
    with pytest.raises(editor_timeline.CommandValidationError, match="cannot revert command clip.create"):
        editor_timeline.apply_command(
            project,
            {
                "op": "clip.unsplit",
                "actor": "agent",
                "expectedRevision": 2,
                "revertsCommandId": "cmd_clip",
                "payload": {"clip": {"id": "c1"}, "createdClipId": "c2"},
            },
        )


def test_clip_split_then_unsplit_round_trips_via_apply_command():
    """The happy-path unsplit (the inverse of a recorded split) restores the original
    clip and drops the created clip — exercising the full validation seam."""
    from services import editor_timeline

    project = _project_with_clip_replay()
    project = editor_timeline.apply_command(
        project,
        {
            "id": "cmd_split",
            "op": "clip.split",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "at": 2.0, "newClipId": "c2"},
        },
    )
    assert set(project["clips"]) == {"c1", "c2"}

    inverse = editor_timeline._inverse_command(
        project["commandLog"][-1], actor="agent", expected_revision=project["revision"]
    )
    assert inverse["op"] == "clip.unsplit"
    project = editor_timeline.apply_command(project, inverse)

    assert set(project["clips"]) == {"c1"}
    assert project["clips"]["c1"]["duration"] == 4


def test_clip_unsplit_inner_seam_rejects_mutated_split_output():
    """A recorded split exists and the unsplit payload IS the true inverse, but the
    project's current clips were mutated after the split — the inner
    _validate_unsplit_reverts_recorded_split seam rejects the stale unsplit."""
    from services import editor_timeline

    project = _project_with_clip_replay()
    project = editor_timeline.apply_command(
        project,
        {
            "id": "cmd_split",
            "op": "clip.split",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "at": 2.0, "newClipId": "c2"},
        },
    )
    inverse = editor_timeline._inverse_command(
        project["commandLog"][-1], actor="agent", expected_revision=project["revision"]
    )
    # Move the created clip after the split so the live state no longer matches the
    # split's recorded output, while the unsplit payload still equals the true inverse.
    project = editor_timeline.apply_command(
        project,
        {
            "op": "clip.move",
            "actor": "agent",
            "expectedRevision": project["revision"],
            "payload": {"clipId": "c2", "start": 3.5},
        },
    )
    bad = copy.deepcopy(inverse)
    bad["expectedRevision"] = project["revision"]
    with pytest.raises(
        editor_timeline.CommandValidationError,
        match="no longer matches split output",
    ):
        editor_timeline.apply_command(project, bad)


# --- editor title-asset materialization + PNG rendering -----------------------
# These exercise _materialize_editor_title_assets / _render_title_asset_png in
# routers/agenticnews.py. They render real PIL images with untrusted dimension
# and text inputs; a renderer crash escapes to a 500, so the blast radius is high
# and previously had zero direct coverage.

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _title_project(assets, *, project_id="proj", width=1920, height=1080):
    return {
        "projectId": project_id,
        "width": width,
        "height": height,
        "assets": assets,
    }


def test_materialize_title_assets_renders_valid_png(monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    project = _title_project(
        {
            "t1": {
                "id": "t1",
                "type": "title",
                "metadata": {
                    "text": "Breaking news headline",
                    "sourceUrl": "https://www.example.com/story/42",
                },
            }
        }
    )

    materialized, changed = agenticnews_router._materialize_editor_title_assets(project)

    assert changed is True
    assert materialized == [
        {
            "type": "title",
            "assetId": "t1",
            "path": str(tmp_path / "editor_title_assets" / "proj_t1.png"),
        }
    ]
    # The asset src is rewritten to the served URL.
    assert project["assets"]["t1"]["src"] == (
        "/agenticnews-assets/editor_title_assets/proj_t1.png"
    )
    # Version stamp recorded so re-runs are skippable.
    assert project["metadata"]["titleAssetVersion"] == (
        agenticnews_router.EDITOR_TITLE_ASSET_VERSION
    )
    # A real, openable PNG at the requested canvas size was written.
    out = tmp_path / "editor_title_assets" / "proj_t1.png"
    with Image.open(out) as img:
        img.verify()
    with Image.open(out) as img:
        assert img.size == (1920, 1080)
        assert img.mode == "RGBA"


def test_materialize_title_assets_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    project = _title_project(
        {"t1": {"id": "t1", "type": "title", "metadata": {"text": "Hi there"}}}
    )

    first, first_changed = agenticnews_router._materialize_editor_title_assets(project)
    second, second_changed = agenticnews_router._materialize_editor_title_assets(project)

    assert first_changed is True and first
    # Asset already exists and version is current -> nothing re-rendered.
    assert second_changed is False
    assert second == []


def test_materialize_title_assets_rerenders_on_stale_version(monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    project = _title_project(
        {"t1": {"id": "t1", "type": "title", "metadata": {"text": "Hi"}}}
    )
    agenticnews_router._materialize_editor_title_assets(project)

    # Simulate an asset rendered by an older title-asset version.
    project["metadata"]["titleAssetVersion"] = (
        agenticnews_router.EDITOR_TITLE_ASSET_VERSION - 1
    )
    materialized, changed = agenticnews_router._materialize_editor_title_assets(project)

    assert changed is True
    assert [m["assetId"] for m in materialized] == ["t1"]
    assert project["metadata"]["titleAssetVersion"] == (
        agenticnews_router.EDITOR_TITLE_ASSET_VERSION
    )


def test_materialize_title_assets_ignores_non_title_assets(monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    project = _title_project(
        {
            "v1": {"id": "v1", "type": "video", "src": "/x.mp4"},
            "a1": {"id": "a1", "type": "audio", "src": "/x.wav"},
        }
    )

    materialized, changed = agenticnews_router._materialize_editor_title_assets(project)

    assert materialized == []
    assert changed is False
    assert not (tmp_path / "editor_title_assets" / "proj_v1.png").exists()


def test_materialize_title_assets_sanitizes_unsafe_ids(monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)
    project = _title_project(
        {"../../etc": {"id": "../../etc", "type": "title", "metadata": {"text": "x"}}},
        project_id="proj/../id",
    )

    materialized, _ = agenticnews_router._materialize_editor_title_assets(project)

    out = tmp_path / "editor_title_assets" / "proj_id_etc.png"
    # Path is confined to the title dir; no traversal escaped.
    assert materialized[0]["path"] == str(out)
    assert out.exists()
    assert tmp_path in out.parents


@pytest.mark.parametrize("dims", [(2, 2), (1, 5000), (3840, 2160)])
def test_render_title_png_survives_extreme_dimensions(tmp_path, dims):
    width, height = dims
    out = tmp_path / "card.png"

    # Untrusted dimensions must not crash the renderer (would surface as a 500).
    agenticnews_router._render_title_asset_png(
        out, text="hello world", source_url="https://news.example.com", width=width, height=height
    )

    assert out.exists()
    with Image.open(out) as img:
        assert img.size == (width, height)


def test_render_title_png_wraps_and_truncates_long_text(tmp_path):
    out = tmp_path / "card.png"
    long_text = " ".join(["headline"] * 40)

    agenticnews_router._render_title_asset_png(
        out, text=long_text, source_url="", width=1920, height=1080
    )

    assert out.exists()
    with Image.open(out) as img:
        assert img.size == (1920, 1080)


def test_render_title_png_handles_empty_text(tmp_path):
    out = tmp_path / "card.png"

    # Empty/whitespace text must still produce a valid card, not raise.
    agenticnews_router._render_title_asset_png(
        out, text="   ", source_url="", width=1280, height=720
    )

    assert out.exists()
    with Image.open(out) as img:
        img.verify()
