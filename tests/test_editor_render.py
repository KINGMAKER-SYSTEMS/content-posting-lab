import shutil
import subprocess
from pathlib import Path

import pytest

from services import editor_timeline as timeline
from services import editor_render


pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe required for layered render verification",
)


def _solid_png(path: Path, color: str, size: str = "32x32") -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={size}:d=0.1",
            "-frames:v",
            "1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def _probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def _sample_rgb(path: Path, x: int, y: int) -> tuple[int, int, int]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            f"crop=1:1:{x}:{y}",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        capture_output=True,
    )
    return tuple(result.stdout[:3])


def _project_with_card(project_id: str, asset_path: Path, *, x: float = 0.0) -> dict:
    project = timeline.new_project(project_id, width=96, height=64, fps=12)
    project["assets"]["card"] = {
        "id": "card",
        "type": "image",
        "src": str(asset_path),
        "metadata": {},
    }
    project["clips"]["card_clip"] = {
        "id": "card_clip",
        "assetId": "card",
        "trackId": "graphics_1",
        "kind": "artifact",
        "start": 0.0,
        "duration": 1.0,
        "sourceStart": 0.0,
        "enabled": True,
        "muted": False,
        "volume": 1.0,
        "transform": {"x": x, "y": 0.0, "scale": 1.0, "opacity": 1.0},
        "effects": [],
        "keyframes": [],
        "metadata": {},
    }
    return project


def test_backend_detection_prefers_openshot_but_reports_local_blocker():
    capabilities = editor_render.detect_render_backends()

    assert "openshot" in capabilities
    assert capabilities["openshot"]["preferred"] is True
    assert capabilities["ffmpeg"]["available"] is True
    if not capabilities["openshot"]["available"]:
        assert "Python bindings not importable" in capabilities["openshot"]["reason"]


def test_ffmpeg_renderer_exports_layered_mp4_and_preview_frame(tmp_path):
    red = _solid_png(tmp_path / "red.png", "red")
    project = _project_with_card("render_fixture", red, x=0.0)
    renderer = editor_render.FFmpegLayeredRenderer(tmp_path / "renders")

    result = renderer.render(project, output_path=tmp_path / "renders" / "render_fixture.mp4")
    assert result["backend"] == "ffmpeg"
    assert Path(result["video"]).exists()
    assert _probe_duration(Path(result["video"])) >= 0.9

    frame = renderer.render_frame(
        project,
        at=0.5,
        output_path=tmp_path / "renders" / "render_fixture_frame.png",
    )
    assert Path(frame["frame"]).exists()
    assert _sample_rgb(Path(frame["frame"]), 10, 10)[0] > 180


def test_moving_clip_changes_preview_without_regenerating_asset(tmp_path):
    red = _solid_png(tmp_path / "red.png", "red")
    renderer = editor_render.FFmpegLayeredRenderer(tmp_path / "renders")

    left_project = _project_with_card("left", red, x=0.0)
    left_frame = renderer.render_frame(
        left_project, at=0.5, output_path=tmp_path / "renders" / "left.png"
    )
    right_project = _project_with_card("right", red, x=1.0)
    right_frame = renderer.render_frame(
        right_project, at=0.5, output_path=tmp_path / "renders" / "right.png"
    )

    assert left_project["assets"]["card"]["src"] == right_project["assets"]["card"]["src"]
    assert _sample_rgb(Path(left_frame["frame"]), 10, 10)[0] > 180
    assert _sample_rgb(Path(right_frame["frame"]), 10, 10)[0] < 30
    assert _sample_rgb(Path(right_frame["frame"]), 86, 10)[0] > 180


def test_replacing_one_asset_path_changes_rendered_frame_without_changing_clip(tmp_path):
    red = _solid_png(tmp_path / "red.png", "red")
    green = _solid_png(tmp_path / "green.png", "green")
    renderer = editor_render.FFmpegLayeredRenderer(tmp_path / "renders")

    project = _project_with_card("replace", red, x=0.0)
    red_frame = renderer.render_frame(
        project, at=0.5, output_path=tmp_path / "renders" / "red_frame.png"
    )
    project["assets"]["card"]["src"] = str(green)
    green_frame = renderer.render_frame(
        project, at=0.5, output_path=tmp_path / "renders" / "green_frame.png"
    )

    assert project["clips"]["card_clip"]["assetId"] == "card"
    assert _sample_rgb(Path(red_frame["frame"]), 10, 10)[0] > 180
    green_pixel = _sample_rgb(Path(green_frame["frame"]), 10, 10)
    assert green_pixel[1] > 80
    assert green_pixel[0] < 80
