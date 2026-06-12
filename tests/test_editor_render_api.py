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

    rendered = sync_client.post("/api/agenticnews/editor-render/render_api/render")
    assert rendered.status_code == 200
    video = Path(rendered.json()["video"])
    assert video.exists()
    assert video.suffix == ".mp4"

    frame = sync_client.post(
        "/api/agenticnews/editor-render/render_api/frame", json={"at": 0.25}
    )
    assert frame.status_code == 200
    assert Path(frame.json()["frame"]).exists()


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
