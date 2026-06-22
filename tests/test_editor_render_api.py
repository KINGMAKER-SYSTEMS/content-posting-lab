import shutil
import subprocess
from pathlib import Path

import pytest

from routers import agenticnews as agenticnews_router


pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe required for render API verification",
)


def test_editor_render_api_reports_capabilities_and_renders_project(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)

    png = tmp_path / "card.png"
    _solid_png(png)
    created = sync_client.post(
        "/api/agenticnews/editor-timelines",
        json={"projectId": "render_api", "title": "Render API", "width": 32, "height": 32, "fps": 8},
    )
    assert created.status_code == 201
    for command in [
        {
            "op": "asset.import",
            "actor": "test",
            "expectedRevision": 0,
            "payload": {"assetId": "card", "type": "image", "src": str(png)},
        },
        {
            "op": "clip.create",
            "actor": "test",
            "expectedRevision": 1,
            "payload": {
                "clipId": "card_clip",
                "assetId": "card",
                "trackId": "graphics_1",
                "start": 0,
                "duration": 0.5,
            },
        },
    ]:
        response = sync_client.post(
            "/api/agenticnews/editor-timelines/render_api/commands", json=command
        )
        assert response.status_code == 200

    capabilities = sync_client.get("/api/agenticnews/editor-render/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["ffmpeg"]["available"] is True
    expected_backend = "openshot" if capabilities.json()["openshot"]["available"] else "ffmpeg"

    rendered = sync_client.post(
        "/api/agenticnews/editor-render/render_api/render",
        json={"start": 0, "duration": 0.25},
    )
    assert rendered.status_code == 200
    video = Path(rendered.json()["video"])
    assert video.exists()
    assert video.suffix == ".mp4"
    assert rendered.json()["backend"] == expected_backend
    assert rendered.json()["start"] == 0.0
    # End-to-end visibility: every render result carries a backendHealth block so the
    # frontend can tell a deliberate OpenShot compile from a silent ffmpeg downgrade,
    # plus a uniform warnings list. (See services/editor_render.choose_renderer.)
    health = rendered.json()["backendHealth"]
    assert health["selected"] == expected_backend
    assert health["preferred"] == "openshot"
    assert health["downgraded"] is (expected_backend != "openshot")
    assert "warnings" in rendered.json()

    frame = sync_client.post(
        "/api/agenticnews/editor-render/render_api/frame", json={"at": 0.25}
    )
    assert frame.status_code == 200
    assert Path(frame.json()["frame"]).exists()
    assert frame.json()["backend"] == expected_backend


def test_editor_render_api_rejects_start_only_window_render(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)

    created = sync_client.post(
        "/api/agenticnews/editor-timelines",
        json={"projectId": "render_window_guard", "title": "Render Window Guard"},
    )
    assert created.status_code == 201

    response = sync_client.post(
        "/api/agenticnews/editor-render/render_window_guard/render",
        json={"start": 1.0},
    )

    assert response.status_code == 400
    assert "partial render requests require duration" in response.json()["detail"]


def test_editor_render_api_blocks_cache_when_assets_are_missing(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)

    created = sync_client.post(
        "/api/agenticnews/editor-timelines",
        json={"projectId": "render_missing", "title": "Render Missing", "width": 32, "height": 32, "fps": 8},
    )
    assert created.status_code == 201
    for command in [
        {
            "op": "asset.import",
            "actor": "test",
            "expectedRevision": 0,
            "payload": {"assetId": "missing", "type": "image", "src": str(tmp_path / "missing.png")},
        },
        {
            "op": "clip.create",
            "actor": "test",
            "expectedRevision": 1,
            "payload": {
                "clipId": "missing_clip",
                "assetId": "missing",
                "trackId": "graphics_1",
                "start": 0,
                "duration": 0.5,
            },
        },
    ]:
        response = sync_client.post(
            "/api/agenticnews/editor-timelines/render_missing/commands", json=command
        )
        assert response.status_code == 200

    rendered = sync_client.post(
        "/api/agenticnews/editor-render/render_missing/render",
        json={"duration": 0.25},
    )

    assert rendered.status_code == 500
    assert "render blocked by missing assets" in rendered.json()["detail"]
    frame = sync_client.post(
        "/api/agenticnews/editor-render/render_missing/frame",
        json={"at": 0},
    )
    assert frame.status_code == 500
    assert "render blocked by missing assets" in frame.json()["detail"]
    loaded = sync_client.get("/api/agenticnews/editor-timelines/render_missing")
    assert "renderCache" not in loaded.json()


def test_editor_render_api_scopes_missing_assets_to_preview_window(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)

    png = tmp_path / "card.png"
    _solid_png(png)
    created = sync_client.post(
        "/api/agenticnews/editor-timelines",
        json={"projectId": "render_window_scope", "title": "Render Window Scope", "width": 32, "height": 32, "fps": 8},
    )
    assert created.status_code == 201
    for command in [
        {
            "op": "asset.import",
            "actor": "test",
            "expectedRevision": 0,
            "payload": {"assetId": "card", "type": "image", "src": str(png)},
        },
        {
            "op": "asset.import",
            "actor": "test",
            "expectedRevision": 1,
            "payload": {"assetId": "missing", "type": "image", "src": str(tmp_path / "missing.png")},
        },
        {
            "op": "clip.create",
            "actor": "test",
            "expectedRevision": 2,
            "payload": {
                "clipId": "card_clip",
                "assetId": "card",
                "trackId": "graphics_1",
                "start": 0,
                "duration": 0.5,
            },
        },
        {
            "op": "clip.create",
            "actor": "test",
            "expectedRevision": 3,
            "payload": {
                "clipId": "missing_clip",
                "assetId": "missing",
                "trackId": "graphics_1",
                "start": 10,
                "duration": 0.5,
            },
        },
    ]:
        response = sync_client.post(
            "/api/agenticnews/editor-timelines/render_window_scope/commands", json=command
        )
        assert response.status_code == 200

    preview = sync_client.post(
        "/api/agenticnews/editor-render/render_window_scope/render",
        json={"start": 0, "duration": 0.25},
    )
    assert preview.status_code == 200
    assert Path(preview.json()["video"]).exists()
    assert preview.json()["missingAssets"] == []

    frame = sync_client.post(
        "/api/agenticnews/editor-render/render_window_scope/frame",
        json={"at": 0.1},
    )
    assert frame.status_code == 200
    assert Path(frame.json()["frame"]).exists()
    assert frame.json()["missingAssets"] == []

    missing_window = sync_client.post(
        "/api/agenticnews/editor-render/render_window_scope/render",
        json={"start": 10, "duration": 0.25},
    )
    assert missing_window.status_code == 500
    assert "render blocked by missing assets" in missing_window.json()["detail"]


def test_editor_render_api_replaces_stale_window_cache_for_same_artifact(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)

    video = tmp_path / "editor_renders" / "render_cache_dedupe_0.00_1.00.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"fake-video")
    timeline_dir = tmp_path / "editor_timelines"
    timeline_dir.mkdir()
    project_path = timeline_dir / "render_cache_dedupe.json"
    project_path.write_text(
        """{
  "schema": "editor-timeline/v1",
  "projectId": "render_cache_dedupe",
  "sourceEpisodeId": "render_cache_dedupe",
  "title": "Render Cache Dedupe",
  "fps": 8,
  "width": 32,
  "height": 32,
  "revision": 0,
  "metadata": {"abnImportVersion": 2},
  "assets": {},
  "tracks": {},
  "clips": {},
  "markers": {},
  "notes": {},
  "commandLog": [],
  "renderCache": {
    "windows": {
      "0.00_1.01": {
        "backend": "openshot",
        "video": "%s",
        "start": 0,
        "duration": 1.01,
        "revision": 0
      }
    }
  }
}
"""
        % str(video)
    )

    class FakeRenderer:
        def render(self, project, *, output_path, start=0, duration=None):
            Path(output_path).write_bytes(b"fake-video")
            return {
                "backend": "openshot",
                "video": str(output_path),
                "start": start,
                "duration": 1.0,
                "missingAssets": [],
                "audioMuxed": True,
            }

    monkeypatch.setattr(
        agenticnews_router.editor_render,
        "choose_renderer",
        lambda *args, **kwargs: FakeRenderer(),
    )

    response = sync_client.post(
        "/api/agenticnews/editor-render/render_cache_dedupe/render",
        json={"start": 0, "duration": 1},
    )

    assert response.status_code == 200
    loaded = sync_client.get("/api/agenticnews/editor-timelines/render_cache_dedupe")
    windows = loaded.json()["renderCache"]["windows"]
    assert sorted(windows) == ["0.00_1.00"]
    assert windows["0.00_1.00"]["audioMuxed"] is True


def test_editor_render_frame_skips_cache_when_revision_changes_during_render(sync_client, monkeypatch, tmp_path):
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)

    png = tmp_path / "card.png"
    _solid_png(png)
    created = sync_client.post(
        "/api/agenticnews/editor-timelines",
        json={"projectId": "render_race", "title": "Render Race", "width": 32, "height": 32, "fps": 8},
    )
    assert created.status_code == 201
    for command in [
        {
            "op": "asset.import",
            "actor": "test",
            "expectedRevision": 0,
            "payload": {"assetId": "card", "type": "image", "src": str(png)},
        },
        {
            "op": "clip.create",
            "actor": "test",
            "expectedRevision": 1,
            "payload": {
                "clipId": "card_clip",
                "assetId": "card",
                "trackId": "graphics_1",
                "start": 0,
                "duration": 0.5,
            },
        },
    ]:
        response = sync_client.post(
            "/api/agenticnews/editor-timelines/render_race/commands", json=command
        )
        assert response.status_code == 200

    class RacingRenderer:
        def render_frame(self, project, *, at, output_path):
            Path(output_path).write_bytes(b"fake-frame")
            agenticnews_router._editor_timeline_store().apply_command(
                project["projectId"],
                {
                    "op": "clip.move",
                    "actor": "concurrent-editor",
                    "expectedRevision": project["revision"],
                    "payload": {"clipId": "card_clip", "start": 0.25},
                },
            )
            return {"backend": "fake", "frame": str(output_path), "at": at, "missingAssets": []}

    monkeypatch.setattr(
        agenticnews_router.editor_render,
        "choose_renderer",
        lambda *args, **kwargs: RacingRenderer(),
    )

    frame = sync_client.post(
        "/api/agenticnews/editor-render/render_race/frame",
        json={"at": 0},
    )

    assert frame.status_code == 200
    assert frame.json()["cacheSkipped"] is True
    assert frame.json()["currentRevision"] == 3
    loaded = sync_client.get("/api/agenticnews/editor-timelines/render_race")
    assert loaded.json()["revision"] == 3
    assert loaded.json()["clips"]["card_clip"]["start"] == 0.25
    assert "renderCache" not in loaded.json()


def test_editor_render_api_surfaces_subprocess_render_error_as_500(sync_client, monkeypatch, tmp_path):
    """Router 1559-1560 / 1597-1598 — a RenderError raised by the renderer for a
    non-missing-asset reason (e.g. an OpenShot subprocess crash or ffmpeg timeout)
    surfaces as HTTP 500 with the error detail, on BOTH the render and frame
    endpoints, instead of escaping as an uncaught 500/empty body."""
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)

    created = sync_client.post(
        "/api/agenticnews/editor-timelines",
        json={"projectId": "render_subproc_err", "title": "Render Subproc Err", "width": 32, "height": 32, "fps": 8},
    )
    assert created.status_code == 201

    class ExplodingRenderer:
        def render(self, project, *, output_path, start=0, duration=None):
            raise agenticnews_router.editor_render.RenderError(
                "OpenShot subprocess failed (-6): libopenshot aborted"
            )

        def render_frame(self, project, *, at, output_path):
            raise agenticnews_router.editor_render.RenderError(
                "render command timed out: ffmpeg -y -i ... ..."
            )

    monkeypatch.setattr(
        agenticnews_router.editor_render,
        "choose_renderer",
        lambda *args, **kwargs: ExplodingRenderer(),
    )

    rendered = sync_client.post(
        "/api/agenticnews/editor-render/render_subproc_err/render",
        json={"duration": 0.25},
    )
    assert rendered.status_code == 500
    assert "OpenShot subprocess failed" in rendered.json()["detail"]

    frame = sync_client.post(
        "/api/agenticnews/editor-render/render_subproc_err/frame",
        json={"at": 0},
    )
    assert frame.status_code == 500
    assert "timed out" in frame.json()["detail"]

    # A failed render must not leave a render-cache entry behind.
    loaded = sync_client.get("/api/agenticnews/editor-timelines/render_subproc_err")
    assert "renderCache" not in loaded.json()


@pytest.mark.parametrize(
    "command",
    [
        {
            "op": "clip.keyframes",
            "actor": "test",
            "expectedRevision": 0,
            "payload": {
                "clipId": "fx_clip",
                "keyframes": [
                    {
                        "property": "opacity",
                        "points": [
                            {"t": 0.0, "value": 0.0, "interp": "linear"},
                            {"t": 0.5, "value": 1.0, "interp": "linear"},
                        ],
                    }
                ],
            },
        },
        {
            "op": "clip.effect.add",
            "actor": "test",
            "expectedRevision": 0,
            "payload": {
                "clipId": "fx_clip",
                "effect": {"id": "fx_fade", "type": "fadeIn", "params": {"duration": 0.25}},
            },
        },
        {
            "op": "clip.effect.update",
            "actor": "test",
            "expectedRevision": 0,
            "payload": {
                "clipId": "fx_clip",
                "effect": {"id": "fx_seed", "type": "fadeIn", "params": {"duration": 0.4}},
            },
        },
        {
            "op": "clip.effect.delete",
            "actor": "test",
            "expectedRevision": 0,
            "payload": {"clipId": "fx_clip", "effectId": "fx_seed"},
        },
    ],
    ids=["keyframes", "effect.add", "effect.update", "effect.delete"],
)
def test_editor_command_invalidates_render_cache_for_keyframe_and_effect_ops(
    sync_client, monkeypatch, tmp_path, command
):
    """A clip.keyframes / clip.effect.* command changes how the clip compiles
    (services/editor_timeline._mutate_clip), so it must drop the render cache or
    the API will keep serving a stale render. Regression guard for
    routers/agenticnews._command_invalidates_render_cache()."""
    monkeypatch.setattr(agenticnews_router.db, "ASSETS_DIR", tmp_path)

    video = tmp_path / "editor_renders" / "fx_invalidate_0.00_1.00.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"fake-video")
    timeline_dir = tmp_path / "editor_timelines"
    timeline_dir.mkdir()
    project_path = timeline_dir / "fx_invalidate.json"
    project_path.write_text(
        """{
  "schema": "editor-timeline/v1",
  "projectId": "fx_invalidate",
  "sourceEpisodeId": "fx_invalidate",
  "title": "FX Invalidate",
  "fps": 8,
  "width": 32,
  "height": 32,
  "revision": 0,
  "metadata": {"abnImportVersion": 2},
  "assets": {"card": {"id": "card", "type": "image", "src": "card.png"}},
  "tracks": {"graphics_1": {"id": "graphics_1", "kind": "graphics", "index": 0}},
  "clips": {
    "fx_clip": {
      "id": "fx_clip",
      "assetId": "card",
      "trackId": "graphics_1",
      "start": 0,
      "duration": 1.0,
      "keyframes": [],
      "effects": [{"id": "fx_seed", "type": "fadeIn", "params": {"duration": 0.2}}]
    }
  },
  "markers": {},
  "notes": {},
  "commandLog": [],
  "renderCache": {
    "windows": {
      "0.00_1.00": {
        "backend": "openshot",
        "video": "%s",
        "start": 0,
        "duration": 1.0,
        "revision": 0
      }
    }
  }
}
"""
        % str(video)
    )

    seeded = sync_client.get("/api/agenticnews/editor-timelines/fx_invalidate")
    assert seeded.status_code == 200
    assert "renderCache" in seeded.json()

    response = sync_client.post(
        "/api/agenticnews/editor-timelines/fx_invalidate/commands", json=command
    )
    assert response.status_code == 200

    loaded = sync_client.get("/api/agenticnews/editor-timelines/fx_invalidate")
    assert "renderCache" not in loaded.json()


def _solid_png(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=32x32:d=0.1",
            "-frames:v",
            "1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
