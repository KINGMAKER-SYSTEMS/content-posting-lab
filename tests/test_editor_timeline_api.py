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
