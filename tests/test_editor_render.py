import json
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


def _mean_volume(path: Path, *, start: float = 0, duration: float = 1.0) -> float:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-ss",
            str(start),
            "-t",
            str(duration),
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-vn",
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    for line in result.stderr.splitlines():
        if "mean_volume:" in line:
            return float(line.rsplit(" ", 2)[-2])
    raise AssertionError(result.stderr)


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


def test_choose_renderer_isolates_openshot_in_subprocess(monkeypatch, tmp_path):
    monkeypatch.setattr(
        editor_render,
        "detect_render_backends",
        lambda: {
            "openshot": {"available": True, "preferred": True, "reason": "available"},
            "ffmpeg": {"available": True, "preferred": False, "reason": "available"},
        },
    )

    renderer = editor_render.choose_renderer(tmp_path / "renders")

    assert renderer.backend == "openshot"
    assert renderer.__class__.__name__ == "OpenShotSubprocessRenderer"


def test_openshot_subprocess_renderer_salvages_result_from_native_child_exit(monkeypatch, tmp_path):
    output = tmp_path / "renders" / "window.mp4"
    output.parent.mkdir()
    output.write_bytes(b"rendered")

    class Completed:
        returncode = -11
        stdout = json.dumps({
            "backend": "openshot",
            "video": str(output),
            "start": 0,
            "duration": 1,
            "missingAssets": [],
        })
        stderr = "native child exited after render"

    monkeypatch.setattr(editor_render.subprocess, "run", lambda *args, **kwargs: Completed())

    renderer = editor_render.OpenShotSubprocessRenderer(tmp_path / "renders")
    result = renderer.render({"projectId": "p"}, output_path=output, start=0, duration=1)

    assert result["backend"] == "openshot"
    assert result["video"] == str(output)
    assert result["subprocessExitCode"] == -11


def test_open_shot_audio_mux_keeps_later_timeline_audio_clips(tmp_path):
    video = tmp_path / "silent_video.mp4"
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=96x64:r=12:d=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    for path, frequency in ((first, 440), (second, 880)):
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:duration=0.8",
                "-ac",
                "1",
                "-ar",
                "48000",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    project = timeline.new_project("audio_mux", width=96, height=64, fps=12)
    for asset_id, path in (("first", first), ("second", second)):
        project["assets"][asset_id] = {"id": asset_id, "type": "audio", "src": str(path)}
    project["clips"]["first"] = {
        "id": "first",
        "assetId": "first",
        "trackId": "audio_1",
        "kind": "voiceover",
        "start": 0,
        "duration": 0.8,
        "sourceStart": 0,
        "enabled": True,
        "muted": False,
        "volume": 1,
        "transform": {},
    }
    project["clips"]["second"] = {
        **project["clips"]["first"],
        "id": "second",
        "assetId": "second",
        "start": 1.0,
    }

    assert editor_render._mux_timeline_audio(project, video, duration=2.0, asset_root=None) is True

    assert _mean_volume(video, start=0.1, duration=0.4) > -30
    assert _mean_volume(video, start=1.1, duration=0.4) > -30


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


def test_openshot_renders_real_two_layer_timeline_to_valid_mp4(tmp_path):
    """End-to-end through the REAL OpenShot backend (not the ffmpeg fallback, not a
    mocked subprocess): image card + audio clip -> valid mp4 with both streams and
    audible audio. Skips where libopenshot isn't installed (e.g. CI), so it pins the
    prod render path on boxes that have it without breaking those that don't."""
    if not editor_render._import_openshot()[0]:
        pytest.skip("libopenshot Python bindings not importable here")

    red = _solid_png(tmp_path / "red.png", "red", size="96x64")
    voice = tmp_path / "vo.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-ac", "2", "-ar", "48000", str(voice)],
        check=True, capture_output=True, text=True,
    )
    project = _project_with_card("openshot_e2e", red, x=0.0)
    project["assets"]["vo"] = {"id": "vo", "type": "audio", "src": str(voice)}
    project["clips"]["vo_clip"] = {
        "id": "vo_clip", "assetId": "vo", "trackId": "audio_1", "kind": "voiceover",
        "start": 0.0, "duration": 1.0, "sourceStart": 0.0,
        "enabled": True, "muted": False, "volume": 1.0, "transform": {},
    }

    # in-process OpenShotRenderer (the subprocess wrapper just shells out to this)
    output = tmp_path / "renders" / "openshot_e2e.mp4"
    result = editor_render.OpenShotRenderer(tmp_path / "renders").render(
        project, output_path=output,
    )

    assert result["backend"] == "openshot"
    assert result["missingAssets"] == []
    assert result["audioMuxed"] is True
    assert Path(result["video"]).exists()
    assert _probe_duration(output) >= 0.9
    assert _mean_volume(output, start=0.1, duration=0.6) > -30  # audio actually present
