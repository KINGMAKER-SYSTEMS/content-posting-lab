import json
import shutil
import subprocess
from pathlib import Path

import pytest

from services import editor_timeline as timeline
from services import editor_render
from services import openshot_bridge


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


def _frame_png_at(src: Path, dest: Path, *, at: float) -> Path:
    """Extract the first frame at or after `at` to a PNG (robust on this ffmpeg:
    mid-stream -ss piping drops the single frame, a file-target select does not)."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(src),
            "-vf",
            f"select='gte(t,{at})'",
            "-vframes",
            "1",
            str(dest),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return dest


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


def _window_project(*clips: dict) -> dict:
    """Minimal project dict for exercising the pure windowing helpers
    (_windowed_clips / _render_scope_project). No assets/ffmpeg needed."""
    return {
        "projectId": "win",
        "fps": 12,
        "tracks": {"graphics_1": {"id": "graphics_1", "index": 0}},
        "clips": {c["id"]: c for c in clips},
    }


def _wclip(clip_id: str, *, start: float, duration: float, source_start: float = 0.0,
           enabled: bool = True) -> dict:
    return {
        "id": clip_id,
        "assetId": clip_id,
        "trackId": "graphics_1",
        "kind": "artifact",
        "start": start,
        "duration": duration,
        "sourceStart": source_start,
        "enabled": enabled,
    }


def _renderer(tmp_path) -> "editor_render.FFmpegLayeredRenderer":
    return editor_render.FFmpegLayeredRenderer(tmp_path / "renders")


# --- direct unit tests for _visual_filter (no ffmpeg execution) ---
# _visual_filter builds the per-clip filter-graph segment. A typo here silently
# drops effects/keyframes or truncates the render, so assert the string directly
# rather than only through an end-to-end render.

def test_visual_filter_video_clip_emits_trim_branch(tmp_path):
    """A video asset must be source-trimmed (sourceStart..+duration) and PTS-reset
    so the overlay starts at the clip's in-point; image/title assets skip trim."""
    r = _renderer(tmp_path)
    clip = {"id": "c", "assetId": "v", "duration": 2.0, "sourceStart": 1.5, "transform": {}}
    out = r._visual_filter(3, clip, {"type": "video"}, "lbl", 1920, 1080)
    assert out.startswith("[3:v]trim=start=1.500:duration=2.000,setpts=PTS-STARTPTS,")
    assert out.endswith("null[lbl]")


def test_visual_filter_image_clip_skips_trim(tmp_path):
    """An image/title asset is a still fed via -loop; trimming it would be a no-op
    that ffmpeg rejects, so the filter must NOT contain a trim= segment."""
    r = _renderer(tmp_path)
    clip = {"id": "c", "assetId": "i", "duration": 1.0, "sourceStart": 0.0, "transform": {}}
    out = r._visual_filter(1, clip, {"type": "image"}, "lbl", 1920, 1080)
    assert "trim=" not in out
    assert out.startswith("[1:v]scale=")


def test_visual_filter_clamps_opacity_and_scale_into_string(tmp_path):
    """Out-of-range opacity clamps to [0,1] and scale to a >0 floor, landing in the
    colorchannelmixer/scale terms — an unclamped value produces an invalid filter
    that ffmpeg rejects and the whole render fails."""
    r = _renderer(tmp_path)
    clip = {"id": "c", "assetId": "i", "duration": 1.0, "sourceStart": 0.0,
            "transform": {"opacity": 5.0, "scale": -3.0}}
    out = r._visual_filter(1, clip, {"type": "image"}, "lbl", 1920, 1080)
    assert "colorchannelmixer=aa=1.0000" in out   # opacity 5.0 -> clamped to 1.0
    assert "scale=iw*0.0100:ih*0.0100" in out      # scale -3.0 -> clamped to 0.01 floor


def test_visual_filter_zero_duration_clip_omits_fades(tmp_path):
    """A zero-duration clip (e.g. a degenerate window slice) must NOT emit a
    fade=...:d=0 term — ffmpeg treats d<=0 as invalid and aborts the render. The
    `duration > 0` guard in _visual_filter is what prevents that; pin it."""
    r = _renderer(tmp_path)
    clip = {"id": "z", "assetId": "v", "duration": 0.0, "sourceStart": 0.0, "transform": {},
            "effects": [{"type": "fadeIn", "params": {"duration": 0.5}},
                        {"type": "fadeOut", "params": {"duration": 0.5}}]}
    out = r._visual_filter(2, clip, {"type": "video"}, "lbl", 1920, 1080)
    assert "fade=" not in out  # no fade term at all for a zero-length clip


# --- direct unit tests for _build_video_command (no ffmpeg execution) ---

def test_build_video_command_loops_image_over_black_base(tmp_path):
    """The command must open a black lavfi base layer at the project size/fps and
    feed each still image with `-loop 1 -t <dur>` (a still without -loop yields a
    single-frame input that truncates the overlay). Assert the constructed argv."""
    red = _solid_png(tmp_path / "red.png", "red")
    project = _project_with_card("cmd_img", red, x=0.0)
    r = _renderer(tmp_path)
    cmd, missing, warnings = r._build_video_command(
        project, tmp_path / "out.mp4", duration=1.0, window_start=0.0
    )
    assert missing == [] and warnings == []
    assert "lavfi" in cmd
    assert "color=c=black:s=96x64:r=12:d=1.000" in cmd
    # still image is looped for the clip duration
    loop_i = cmd.index("-loop")
    assert cmd[loop_i:loop_i + 5] == ["-loop", "1", "-t", "1.000", "-i"]
    # exactly one mapped video output, libx264 + yuv420p, no audio map
    assert cmd[cmd.index("-map") + 1] == "[v]"
    assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "libx264"
    assert "yuv420p" in cmd
    assert "[a]" not in cmd


def test_build_video_command_records_missing_asset_without_raising(tmp_path):
    """_build_video_command itself must NOT raise on a missing source — it records
    the gap in `missing_assets` and skips feeding a phantom -i input. render() is
    the layer that fails closed on a non-empty missing list; the builder stays pure
    so callers can inspect the gaps."""
    project = _project_with_card("cmd_missing", tmp_path / "gone.png", x=0.0)
    r = _renderer(tmp_path)
    cmd, missing, warnings = r._build_video_command(
        project, tmp_path / "out.mp4", duration=1.0, window_start=0.0
    )
    assert [m["assetId"] for m in missing] == ["card"]
    assert "gone.png" in missing[0]["src"]
    # the missing source was skipped, not fed as an input
    assert not any(str(tmp_path / "gone.png") == arg for arg in cmd)


def test_missing_assets_flags_agenticnews_url_when_episode_subdir_absent(tmp_path):
    """Per-episode asset schema (commit cb5c98f5) stores assets under
    /agenticnews-assets/<episode>/file. _missing_assets must resolve that URL
    against asset_root and report it missing when the subdir doesn't exist —
    otherwise render() ships a phantom -i input and ffmpeg fails late."""
    asset_root = tmp_path / "agenticnews_assets"
    asset_root.mkdir()
    # episode subdir "ep99" was never created — the URL points into a hole.
    project = _project_with_card("missing_subdir", tmp_path / "ignored.png")
    project["assets"]["card"]["src"] = "/agenticnews-assets/ep99/card.png"

    missing = editor_render._missing_assets(project, asset_root)

    assert [m["assetId"] for m in missing] == ["card"]
    # the recorded src is the fully-resolved per-episode path under asset_root,
    # not the raw URL — proves the subdir was joined before the existence check.
    assert missing[0]["src"] == str(asset_root / "ep99" / "card.png")
    assert missing[0]["clipId"] == "card_clip"


def test_missing_assets_accepts_agenticnews_url_when_episode_subdir_present(tmp_path):
    """The same URL resolves clean when the per-episode subdir holds the file:
    no false positive, so render() is not blocked on a present asset."""
    asset_root = tmp_path / "agenticnews_assets"
    episode_dir = asset_root / "ep99"
    episode_dir.mkdir(parents=True)
    _solid_png(episode_dir / "card.png", "blue")
    project = _project_with_card("present_subdir", tmp_path / "ignored.png")
    project["assets"]["card"]["src"] = "/agenticnews-assets/ep99/card.png"

    assert editor_render._missing_assets(project, asset_root) == []


def test_build_video_command_adds_audio_map_and_amix(tmp_path):
    """With an audio clip present the command must map a mixed [a] stream and encode
    it (aac, -shortest). A dropped audio map silently ships a silent render."""
    red = _solid_png(tmp_path / "red.png", "red")
    voice = tmp_path / "vo.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-ac", "1", "-ar", "48000", str(voice)],
        check=True, capture_output=True, text=True,
    )
    project = _project_with_card("cmd_audio", red, x=0.0)
    project["assets"]["vo"] = {"id": "vo", "type": "audio", "src": str(voice)}
    project["clips"]["vo_clip"] = {
        "id": "vo_clip", "assetId": "vo", "trackId": "audio_1", "kind": "voiceover",
        "start": 0.0, "duration": 1.0, "sourceStart": 0.0,
        "enabled": True, "muted": False, "volume": 1.0, "transform": {},
    }
    r = _renderer(tmp_path)
    cmd, missing, warnings = r._build_video_command(
        project, tmp_path / "out.mp4", duration=1.0, window_start=0.0
    )
    assert missing == []
    filter_arg = cmd[cmd.index("-filter_complex") + 1]
    assert "amix=inputs=1" in filter_arg
    # both [v] and [a] are mapped, audio encoded as aac with -shortest
    maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
    assert "[v]" in maps and "[a]" in maps
    assert "-c:a" in cmd and cmd[cmd.index("-c:a") + 1] == "aac"
    assert "-shortest" in cmd


def test_windowed_clips_trims_clip_overlapping_window_start():
    # clip spans [1, 5); window is [2, 6). Front 1s is before the window and must be cut.
    project = _window_project(_wclip("a", start=1.0, duration=4.0, source_start=10.0))
    out = editor_render._windowed_clips(project, window_start=2.0, duration=4.0)

    assert len(out) == 1
    clip = out[0]
    assert clip["start"] == pytest.approx(0.0)            # rebased to window origin
    assert clip["duration"] == pytest.approx(3.0)         # only [2,5) survives
    assert clip["sourceStart"] == pytest.approx(11.0)     # 10 + (2 - 1) skipped second


def test_windowed_clips_trims_clip_overlapping_window_end():
    # clip spans [1, 6); window is [0, 4). Tail past window_end must be cut, source untouched.
    project = _window_project(_wclip("a", start=1.0, duration=5.0, source_start=3.0))
    out = editor_render._windowed_clips(project, window_start=0.0, duration=4.0)

    assert len(out) == 1
    clip = out[0]
    assert clip["start"] == pytest.approx(1.0)            # clip starts 1s into the window
    assert clip["duration"] == pytest.approx(3.0)         # only [1,4) survives
    assert clip["sourceStart"] == pytest.approx(3.0)      # no front trim -> no source shift


def test_windowed_clips_drops_clips_fully_outside_window():
    project = _window_project(
        _wclip("before", start=0.0, duration=1.0),   # ends at window_start boundary
        _wclip("inside", start=2.0, duration=1.0),
        _wclip("after", start=5.0, duration=1.0),    # starts at window_end boundary
    )
    out = editor_render._windowed_clips(project, window_start=1.0, duration=4.0)

    assert [c["id"] for c in out] == ["inside"]          # [1,5): boundary-touching clips excluded


def test_windowed_clips_excludes_disabled_clips():
    project = _window_project(
        _wclip("on", start=0.0, duration=4.0),
        _wclip("off", start=0.0, duration=4.0, enabled=False),
    )
    out = editor_render._windowed_clips(project, window_start=0.0, duration=4.0)

    assert [c["id"] for c in out] == ["on"]


def test_render_scope_project_returns_original_when_no_windowing():
    project = _window_project(_wclip("a", start=0.0, duration=4.0))
    # window_start <= 0 and duration None -> identity (no copy, no scoping work)
    assert editor_render._render_scope_project(project, window_start=0.0, duration=None) is project


def test_render_scope_project_defaults_duration_to_remaining_timeline():
    # full timeline is [0, 10); scoping from 4 with no explicit duration -> [4, 10).
    project = _window_project(_wclip("a", start=0.0, duration=10.0))
    scoped = editor_render._render_scope_project(project, window_start=4.0, duration=None)

    assert scoped is not project                          # scoped without mutating the input
    assert project["clips"]["a"]["start"] == 0.0          # original untouched
    clip = next(iter(scoped["clips"].values()))
    assert clip["start"] == pytest.approx(0.0)
    assert clip["duration"] == pytest.approx(6.0)         # 10 - 4 remaining seconds


def test_ffmpeg_fallback_warns_on_dropped_effects_and_keyframes(tmp_path):
    """The ffmpeg fallback can't composite crossfade/color effects or keyframe
    envelopes (OpenShot-only). It must SURFACE that as a structured warning, not
    drop them in silence — that silent drop was the bug this slice fixes."""
    red = _solid_png(tmp_path / "red.png", "red")
    project = _project_with_card("warn_fixture", red, x=0.0)
    project["clips"]["card_clip"]["effects"] = [
        {"id": "fx_cf", "type": "crossfade", "params": {"duration": 0.3}},
        {"id": "fx_br", "type": "brightness", "params": {"value": 0.2}},
    ]
    project["clips"]["card_clip"]["keyframes"] = [
        {"property": "opacity", "points": [
            {"t": 0.0, "value": 0.0, "interp": "linear"},
            {"t": 1.0, "value": 1.0, "interp": "linear"},
        ]},
    ]
    renderer = editor_render.FFmpegLayeredRenderer(tmp_path / "renders")
    result = renderer.render(project, output_path=tmp_path / "renders" / "warn_fixture.mp4")

    assert Path(result["video"]).exists()  # still renders
    names = {(w["kind"], w["name"]) for w in result["warnings"]}
    assert ("effect", "crossfade") in names
    assert ("effect", "brightness") in names
    assert ("keyframe", "opacity") in names
    # frame previews carry the same warning contract
    frame = renderer.render_frame(project, at=0.5, output_path=tmp_path / "renders" / "warn.png")
    assert any(w["name"] == "crossfade" for w in frame["warnings"])


def test_ffmpeg_fallback_applies_native_fadein_without_warning(tmp_path):
    """fadeIn/fadeOut are the one effect ffmpeg reproduces natively (the `fade`
    filter). A 0.5s fadeIn on a 1s red card must (a) NOT warn and (b) actually
    ramp alpha — the start of the clip is near-black (faded out) and ramps up to
    full red by the end."""
    red = _solid_png(tmp_path / "red.png", "red", size="96x64")
    project = _project_with_card("fade_fixture", red, x=0.0)
    project["clips"]["card_clip"]["effects"] = [
        {"id": "fx_in", "type": "fadeIn", "params": {"duration": 0.5}},
    ]
    renderer = editor_render.FFmpegLayeredRenderer(tmp_path / "renders")
    result = renderer.render(project, output_path=tmp_path / "renders" / "fade_fixture.mp4")
    assert result["warnings"] == []  # fadeIn is honored natively, not dropped

    video = Path(result["video"])
    early = _frame_png_at(video, tmp_path / "early.png", at=0.0)
    late = _frame_png_at(video, tmp_path / "late.png", at=0.9)
    early_red = _sample_rgb(early, 10, 10)[0]
    late_red = _sample_rgb(late, 10, 10)[0]
    assert early_red < late_red  # red ramps up from black as the fade-in progresses
    assert late_red > 150


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


def test_open_shot_audio_mux_applies_audio_fadein(tmp_path):
    """The OpenShot mux path must honor fadeIn/fadeOut effects the same way the
    ffmpeg-layered fallback does. Without the fade the head of the clip is at
    full level; with it the ramped head is audibly quieter than the body.
    """
    video = tmp_path / "silent_video.mp4"
    tone = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=96x64:r=12:d=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2.0",
         "-ac", "1", "-ar", "48000", str(tone)],
        check=True, capture_output=True, text=True,
    )
    project = timeline.new_project("audio_fade", width=96, height=64, fps=12)
    project["assets"]["tone"] = {"id": "tone", "type": "audio", "src": str(tone)}
    project["clips"]["tone"] = {
        "id": "tone", "assetId": "tone", "trackId": "audio_1", "kind": "music",
        "start": 0, "duration": 2.0, "sourceStart": 0, "enabled": True,
        "muted": False, "volume": 1, "transform": {},
        "effects": [{"id": "fx_in", "type": "fadeIn", "params": {"duration": 1.0}}],
    }

    assert editor_render._mux_timeline_audio(project, video, duration=2.0, asset_root=None) is True

    head = _mean_volume(video, start=0.0, duration=0.25)
    body = _mean_volume(video, start=1.2, duration=0.4)
    assert head < body - 6  # fadeIn ramp keeps the head well below steady level


def test_open_shot_audio_mux_raises_on_missing_audio_asset(tmp_path):
    """The mux path must fail closed when an audio clip's src is absent (line 749):
    it builds a -filter_complex over the audio inputs and a phantom -i would make
    ffmpeg fail late with an opaque error. The missing-asset guard fires BEFORE the
    subprocess, so this raises deterministically without invoking ffmpeg. Pins the
    fail-closed contract guarding fadeIn/fadeOut and the amix filter graph."""
    video = tmp_path / "silent_video.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=96x64:r=12:d=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
        check=True, capture_output=True, text=True,
    )
    ghost = tmp_path / "ghost.wav"  # never created on disk
    project = timeline.new_project("audio_mux_missing", width=96, height=64, fps=12)
    project["assets"]["ghost"] = {"id": "ghost", "type": "audio", "src": str(ghost)}
    project["clips"]["ghost"] = {
        "id": "ghost", "assetId": "ghost", "trackId": "audio_1", "kind": "voiceover",
        "start": 0.0, "duration": 1.0, "sourceStart": 0.0, "enabled": True,
        "muted": False, "volume": 1.0, "transform": {},
        "effects": [{"id": "fx_in", "type": "fadeIn", "params": {"duration": 0.5}}],
    }

    with pytest.raises(editor_render.RenderError) as excinfo:
        editor_render._mux_timeline_audio(project, video, duration=2.0, asset_root=None)
    assert "audio mux blocked by missing asset" in str(excinfo.value)
    assert "ghost.wav" in str(excinfo.value)
    # fail-closed: the video must be left untouched (no .audio-mux temp swapped in).
    assert not video.with_name("silent_video.audio-mux.mp4").exists()


def test_volume_filter_builds_keyframe_expression_when_envelope_present():
    """A `volume` keyframe track must compile to a time-varying ffmpeg expression
    (eval=frame), not the flat `volume=` that silently flattened ducking envelopes.
    No keyframes -> the cheap flat filter."""
    flat = {"volume": 0.8, "keyframes": []}
    assert editor_render._volume_filter(flat, 0.8) == "volume=0.8000"

    ducked = {
        "volume": 1.0,
        "keyframes": [
            {
                "property": "volume",
                "points": [
                    {"t": 0.0, "value": 1.0},
                    {"t": 1.0, "value": 0.2},
                    {"t": 2.0, "value": 1.0},
                ],
            }
        ],
    }
    expr = editor_render._volume_filter(ducked, 1.0)
    assert expr.startswith("volume='") and ":eval=frame" in expr
    assert "(t-0.0000)" in expr and "(t-1.0000)" in expr  # piecewise segments


def test_open_shot_audio_mux_honors_volume_ducking_keyframes(tmp_path):
    """The mux path must apply a keyframed volume envelope, not flat volume. A
    music-bed ducking curve (loud head, then ducked under VO) must leave the
    ducked body audibly quieter than the head — the flat `volume=` path could
    only ever pin the whole clip to one level, so this would fail without the
    keyframe-aware filter."""
    video = tmp_path / "silent_video.mp4"
    tone = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=96x64:r=12:d=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3.0",
         "-ac", "1", "-ar", "48000", str(tone)],
        check=True, capture_output=True, text=True,
    )
    project = timeline.new_project("audio_duck", width=96, height=64, fps=12)
    project["assets"]["tone"] = {"id": "tone", "type": "audio", "src": str(tone)}
    project["clips"]["tone"] = {
        "id": "tone", "assetId": "tone", "trackId": "audio_1", "kind": "music",
        "start": 0, "duration": 3.0, "sourceStart": 0, "enabled": True,
        "muted": False, "volume": 1, "transform": {},
        "keyframes": [
            {
                "property": "volume",
                "points": [
                    {"t": 0.0, "value": 1.0},
                    {"t": 0.5, "value": 1.0},
                    {"t": 0.8, "value": 0.05},
                    {"t": 3.0, "value": 0.05},
                ],
            }
        ],
    }

    assert editor_render._mux_timeline_audio(project, video, duration=3.0, asset_root=None) is True

    head = _mean_volume(video, start=0.0, duration=0.4)  # full level
    body = _mean_volume(video, start=1.3, duration=1.0)  # ducked under VO
    assert body < head - 10  # ducked body well below the loud head


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


def test_ffmpeg_renderer_skips_text_only_lower_third_without_crashing(tmp_path):
    """A lower-third clip has an empty src (its headline lives in metadata). The empty
    src resolves to Path('')=='.' which 'exists' as a directory, so it slipped the
    missing-asset gate and was fed to ffmpeg as a phantom -i input -> crash. The renderer
    must skip text-only placeholders (parity with OpenShot's DummyReader) and still
    render the real card."""
    red = _solid_png(tmp_path / "red.png", "red")
    project = _project_with_card("lt_fixture", red, x=0.0)
    project["assets"]["lt"] = {"id": "lt", "type": "title", "src": "",
                               "metadata": {"text": "BREAKING NEWS"}}
    project["clips"]["lt_clip"] = {
        "id": "lt_clip", "assetId": "lt", "trackId": "titles_1", "kind": "lower_third",
        "start": 0.0, "duration": 1.0, "sourceStart": 0.0,
        "enabled": True, "muted": False, "volume": 1.0,
        "transform": {"x": 0.5, "y": 0.8, "scale": 1.0, "opacity": 1.0},
    }
    renderer = editor_render.FFmpegLayeredRenderer(tmp_path / "renders")
    result = renderer.render(project, output_path=tmp_path / "renders" / "lt_fixture.mp4")
    assert Path(result["video"]).exists()                 # render succeeded, no phantom-input crash
    assert all(m["assetId"] != "lt" for m in result["missingAssets"])  # not reported missing


def test_ffmpeg_renderer_raises_render_error_on_unreadable_visual_src(tmp_path):
    """_resolve_src points at a file that does not exist on disk. The build step
    must record it in missingAssets and render() must raise RenderError (the
    production fail-closed contract) rather than feed a phantom -i input to ffmpeg."""
    project = _project_with_card("missing_visual", tmp_path / "does_not_exist.png", x=0.0)
    renderer = editor_render.FFmpegLayeredRenderer(tmp_path / "renders")
    with pytest.raises(editor_render.RenderError) as excinfo:
        renderer.render(project, output_path=tmp_path / "renders" / "missing_visual.mp4")
    assert "missing assets" in str(excinfo.value)
    assert "does_not_exist.png" in str(excinfo.value)


def test_ffmpeg_renderer_raises_render_error_on_missing_audio_asset(tmp_path):
    """An audio clip whose src file is absent is a missing asset too — the audio
    branch of the build loop must funnel it to the same missingAssets gate so the
    render fails closed instead of silently dropping the voiceover."""
    red = _solid_png(tmp_path / "red.png", "red")
    project = _project_with_card("missing_audio", red, x=0.0)
    project["assets"]["vo"] = {"id": "vo", "type": "audio", "src": str(tmp_path / "ghost.wav")}
    project["clips"]["vo_clip"] = {
        "id": "vo_clip", "assetId": "vo", "trackId": "audio_1", "kind": "voiceover",
        "start": 0.0, "duration": 1.0, "sourceStart": 0.0,
        "enabled": True, "muted": False, "volume": 1.0, "transform": {},
    }
    renderer = editor_render.FFmpegLayeredRenderer(tmp_path / "renders")
    with pytest.raises(editor_render.RenderError) as excinfo:
        renderer.render(project, output_path=tmp_path / "renders" / "missing_audio.mp4")
    assert "ghost.wav" in str(excinfo.value)


def test_ffmpeg_renderer_tolerates_malformed_transform_and_opacity(tmp_path):
    """Transforms arrive as untrusted frontend JSON. A poisoned opacity/scale/x/y
    (non-numeric, NaN, out-of-range) previously raised a bare ValueError out of
    _visual_filter/_overlay_expr that escaped render() as an uncaught 500. The
    renderer must coerce to sane defaults and still produce a valid mp4."""
    red = _solid_png(tmp_path / "red.png", "red")
    project = _project_with_card("bad_transform", red, x=0.0)
    project["clips"]["card_clip"]["transform"] = {
        "x": "left", "y": None, "scale": "huge", "opacity": "opaque",
    }
    renderer = editor_render.FFmpegLayeredRenderer(tmp_path / "renders")
    result = renderer.render(project, output_path=tmp_path / "renders" / "bad_transform.mp4")
    assert Path(result["video"]).exists()
    assert _probe_duration(Path(result["video"])) >= 0.9


def test_ffmpeg_renderer_mixes_overlapping_audio_clips_without_crashing(tmp_path):
    """Two audio clips whose timelines overlap must both survive the amix filter
    (line 450). Overlapping ranges are the common case (music bed under VO); the
    mix must stay audible across the overlap window rather than dropping a stream."""
    red = _solid_png(tmp_path / "red.png", "red")
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    for path, frequency in ((first, 440), (second, 880)):
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             f"sine=frequency={frequency}:duration=1", "-ac", "1", "-ar", "48000", str(path)],
            check=True, capture_output=True, text=True,
        )
    project = _project_with_card("overlap_audio", red, x=0.0)
    project["assets"]["bed"] = {"id": "bed", "type": "audio", "src": str(first)}
    project["assets"]["vo"] = {"id": "vo", "type": "audio", "src": str(second)}
    project["clips"]["bed_clip"] = {
        "id": "bed_clip", "assetId": "bed", "trackId": "audio_1", "kind": "music",
        "start": 0.0, "duration": 1.0, "sourceStart": 0.0,
        "enabled": True, "muted": False, "volume": 1.0, "transform": {},
    }
    project["clips"]["vo_clip"] = {
        **project["clips"]["bed_clip"],
        "id": "vo_clip", "assetId": "vo", "trackId": "audio_2", "kind": "voiceover",
        "start": 0.5,  # overlaps bed_clip's [0,1] window
    }
    renderer = editor_render.FFmpegLayeredRenderer(tmp_path / "renders")
    output = tmp_path / "renders" / "overlap_audio.mp4"
    result = renderer.render(project, output_path=output)
    assert Path(result["video"]).exists()
    assert result["missingAssets"] == []
    assert _mean_volume(output, start=0.6, duration=0.3) > -40  # both streams audible in overlap


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


def test_windowed_clip_shifts_keyframe_times_when_front_trimmed():
    """A clip windowed from a later start has its front trimmed; keyframe `t`
    (seconds relative to the clip start) must move down by the same trim amount,
    clamped at 0, so the animation fires at the right time during partial renders."""
    project = timeline.new_project("kf_window", width=96, height=64, fps=12)
    project["clips"]["card_clip"] = {
        "id": "card_clip",
        "assetId": "card",
        "trackId": "graphics_1",
        "start": 5.0,
        "duration": 4.0,  # clip spans timeline t=5..9
        "sourceStart": 0.0,
        "enabled": True,
        "keyframes": [
            {
                "property": "opacity",
                "points": [
                    {"t": 0.5, "value": 0.0, "interp": "linear"},  # before window -> clamps to 0
                    {"t": 3.0, "value": 1.0, "interp": "linear"},  # at timeline t=8
                ],
            }
        ],
    }

    # Window timeline t=7..9 -> front_trim = 7 - 5 = 2.0s.
    windowed = editor_render._windowed_clips(project, window_start=7.0, duration=2.0)

    assert len(windowed) == 1
    clip = windowed[0]
    assert clip["start"] == pytest.approx(0.0)  # clip begins at window start
    assert clip["sourceStart"] == pytest.approx(2.0)
    points = clip["keyframes"][0]["points"]
    assert points[0]["t"] == pytest.approx(0.0)  # 0.5 - 2.0 clamped to 0
    assert points[1]["t"] == pytest.approx(1.0)  # 3.0 - 2.0
    assert points[0]["value"] == 0.0 and points[1]["value"] == 1.0

    # Original project clip must be untouched (no aliasing of nested keyframes).
    original = project["clips"]["card_clip"]["keyframes"][0]["points"]
    assert original[0]["t"] == pytest.approx(0.5)
    assert original[1]["t"] == pytest.approx(3.0)


def test_opacity_keyframe_envelope_actually_animates_through_openshot(tmp_path):
    """End-to-end: a clip with an opacity envelope (0 -> 1 over its duration) must RENDER
    the fade through OpenShot, not just translate to correct JSON. Sample an early frame
    (low opacity -> dark bg shows through) vs a late frame (high opacity -> card visible).
    Pins the keyframe-envelope render contract; skips where libopenshot isn't installed."""
    if not editor_render._import_openshot()[0]:
        pytest.skip("libopenshot Python bindings not importable here")

    white = _solid_png(tmp_path / "white.png", "white", size="64x48")
    project = timeline.new_project("kf_render", width=64, height=48, fps=12)
    project["assets"]["c"] = {"id": "c", "type": "image", "src": str(white)}
    project["clips"]["c1"] = {
        "id": "c1", "assetId": "c", "trackId": "graphics_1", "kind": "artifact",
        "start": 0.0, "duration": 2.0, "sourceStart": 0.0,
        "enabled": True, "muted": False, "volume": 1.0,
        "transform": {"x": 0.5, "y": 0.5, "scale": 3.0, "opacity": 1.0}, "effects": [],
        "keyframes": [{"property": "opacity", "points": [
            {"t": 0.0, "value": 0.0, "interp": "linear"},
            {"t": 2.0, "value": 1.0, "interp": "linear"},
        ]}],
        "metadata": {},
    }
    project.update({"sampleRate": 48000, "channels": 2, "channelLayout": 3})

    renderer = editor_render.OpenShotRenderer(tmp_path)

    def _luma(at: float) -> int:
        frame = renderer.render_frame(project, at=at, output_path=tmp_path / f"f{at}.png")
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(frame["frame"]), "-vf", "crop=1:1:32:24",
             "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
            check=True, capture_output=True,
        )
        return out.stdout[0] if out.stdout else -1

    early = _luma(0.2)   # low opacity -> mostly black background
    late = _luma(1.8)    # high opacity -> white card visible
    assert late > early + 30, (early, late)  # the fade-in actually rendered

def test_windowed_clip_refits_fade_effect_durations_to_window():
    """Fade effects assume the original clip length. A windowed (shortened) clip must
    have its fade durations re-fit, or a fadeOut longer than the window silences the
    whole clip, and a front-trim eats a start-anchored fadeIn. Fixes both renderers,
    which read this same windowed effects array (OpenShot) / its duration param
    (ffmpeg via _fade_window)."""
    project = timeline.new_project("fade_window", width=96, height=64, fps=12)
    project["clips"]["card_clip"] = {
        "id": "card_clip",
        "assetId": "card",
        "trackId": "graphics_1",
        "start": 0.0,
        "duration": 5.0,  # clip spans timeline t=0..5
        "sourceStart": 0.0,
        "enabled": True,
        "effects": [
            {"type": "fadeIn", "params": {"duration": 1.0}},
            {"type": "fadeOut", "params": {"duration": 1.0}},
            # crossfade is start-anchored like fadeIn (both -> OpenShot `in` Fade), so a
            # front trim must eat its ramp too, or the crossfade overshoots the window.
            {"type": "crossfade", "params": {"duration": 1.0}},
            {"type": "colorFilter", "params": {"hue": 30}},  # no duration -> untouched
        ],
    }

    # Window timeline t=0..2 -> back-trim only, no front trim.
    windowed = editor_render._windowed_clips(project, window_start=0.0, duration=2.0)
    assert len(windowed) == 1
    effects = {e["type"]: e for e in windowed[0]["effects"]}
    # fadeOut must be clamped to the 2s window so it no longer overruns it. (Before
    # the fix it stayed at 1.0 -> with a tighter window it would silence the clip.)
    assert effects["fadeOut"]["params"]["duration"] == pytest.approx(1.0)
    assert effects["colorFilter"]["params"] == {"hue": 30}

    # Window timeline t=0.5..1.0 -> windowed_duration=0.5, so a 1s fade can't fit.
    tiny = editor_render._windowed_clips(project, window_start=0.5, duration=0.5)
    tiny_effects = {e["type"]: e for e in tiny[0]["effects"]}
    # front_trim=0.5 eats half the 1s fadeIn -> 0.5, then clamped to the 0.5 window.
    assert tiny_effects["fadeIn"]["params"]["duration"] == pytest.approx(0.5)
    # crossfade is start-anchored too: front_trim eats half -> 0.5, clamped to window.
    assert tiny_effects["crossfade"]["params"]["duration"] == pytest.approx(0.5)
    # fadeOut clamped to the 0.5 window.
    assert tiny_effects["fadeOut"]["params"]["duration"] == pytest.approx(0.5)

    # A front trim larger than the fade wipes it entirely (clamped at 0).
    gone = editor_render._windowed_clips(project, window_start=2.0, duration=2.0)
    gone_effects = {e["type"]: e for e in gone[0]["effects"]}
    assert gone_effects["fadeIn"]["params"]["duration"] == pytest.approx(0.0)
    assert gone_effects["crossfade"]["params"]["duration"] == pytest.approx(0.0)

    # Original project clip effects must be untouched (no aliasing of nested params).
    orig = project["clips"]["card_clip"]["effects"]
    assert orig[0]["params"]["duration"] == pytest.approx(1.0)
    assert orig[1]["params"]["duration"] == pytest.approx(1.0)
def _kf_clip(clip_id: str, *, start: float, duration: float, source_start: float,
             tracks: list[dict]) -> dict:
    """A windowing fixture clip that carries keyframe tracks."""
    clip = _wclip(clip_id, start=start, duration=duration, source_start=source_start)
    clip["keyframes"] = tracks
    return clip


def test_render_scope_project_carries_shifted_keyframes_into_scoped_clips():
    """The commit's render path goes through _render_scope_project, not
    _windowed_clips directly. A front-trimmed clip's keyframe `t` must arrive
    shifted inside scoped["clips"] (what the renderer actually feeds OpenShot),
    and sourceStart must shift in lockstep — the windowing<->keyframe interaction
    the ticket flags as under-tested."""
    project = _window_project(
        _kf_clip(
            "a",
            start=2.0,
            duration=6.0,            # clip spans timeline t=2..8
            source_start=4.0,
            tracks=[{"property": "opacity", "points": [
                {"t": 1.0, "value": 0.0},   # before window -> clamps to 0
                {"t": 4.0, "value": 1.0},   # 4 - 3 = 1.0 after shift
            ]}],
        )
    )
    # Window timeline t=5..8 -> front_trim = 5 - 2 = 3.0s.
    scoped = editor_render._render_scope_project(project, window_start=5.0, duration=3.0)

    assert scoped is not project
    clip = scoped["clips"]["a"]
    assert clip["start"] == pytest.approx(0.0)
    assert clip["sourceStart"] == pytest.approx(7.0)        # 4 + 3 front-trim
    points = clip["keyframes"][0]["points"]
    assert points[0]["t"] == pytest.approx(0.0)             # 1.0 - 3.0 clamped
    assert points[1]["t"] == pytest.approx(1.0)             # 4.0 - 3.0
    # Source project keyframes untouched (deep copy, no aliasing).
    assert project["clips"]["a"]["keyframes"][0]["points"][0]["t"] == pytest.approx(1.0)


def test_windowed_clips_keyframe_exactly_on_window_start_lands_at_zero():
    """Boundary case: a keyframe whose `t` equals the front-trim amount lands
    exactly at 0 (the new window origin) — not negative, not clamped away from a
    real value. The reveal that was scheduled at the cut must fire at frame 0."""
    project = _window_project(
        _kf_clip(
            "a",
            start=1.0,
            duration=5.0,            # clip spans t=1..6
            source_start=0.0,
            tracks=[{"property": "scale", "points": [
                {"t": 2.0, "value": 1.0},   # at window start exactly -> t == front_trim
                {"t": 4.0, "value": 1.5},
            ]}],
        )
    )
    # Window t=3..6 -> front_trim = 3 - 1 = 2.0s, equal to the first point's t.
    windowed = editor_render._windowed_clips(project, window_start=3.0, duration=3.0)

    points = windowed[0]["keyframes"][0]["points"]
    assert points[0]["t"] == pytest.approx(0.0)             # 2.0 - 2.0, lands on origin
    assert points[1]["t"] == pytest.approx(2.0)             # 4.0 - 2.0


def test_windowed_clips_shifts_every_keyframe_track_independently():
    """Front-trim must shift ALL keyframe tracks on a clip (opacity, scale, x, y),
    not just the first. A clip with multiple animated properties must keep every
    envelope aligned after windowing."""
    project = _window_project(
        _kf_clip(
            "a",
            start=0.0,
            duration=10.0,
            source_start=0.0,
            tracks=[
                {"property": "opacity", "points": [{"t": 5.0, "value": 1.0}]},
                {"property": "scale", "points": [{"t": 6.0, "value": 2.0}]},
                {"property": "x", "points": [{"t": 7.0, "value": 0.3}]},
            ],
        )
    )
    # Window t=4..10 -> front_trim = 4.0s applied to every track.
    windowed = editor_render._windowed_clips(project, window_start=4.0, duration=6.0)
    tracks = {t["property"]: t["points"][0]["t"] for t in windowed[0]["keyframes"]}

    assert tracks["opacity"] == pytest.approx(1.0)          # 5 - 4
    assert tracks["scale"] == pytest.approx(2.0)            # 6 - 4
    assert tracks["x"] == pytest.approx(3.0)                # 7 - 4


def test_windowed_clips_leading_clip_no_front_trim_leaves_keyframes_untouched():
    """A clip that starts at/after the window origin has no front-trim, so its
    keyframes must pass through identically (the shift branch is skipped) and the
    original keyframe list object is reused, not needlessly deep-copied."""
    original_tracks = [{"property": "opacity", "points": [
        {"t": 0.5, "value": 0.0}, {"t": 2.0, "value": 1.0},
    ]}]
    project = _window_project(
        _kf_clip("a", start=2.0, duration=4.0, source_start=1.5, tracks=original_tracks)
    )
    # Window t=0..8: clip starts 2s in, no front-trim (window_start=0 <= clip start).
    windowed = editor_render._windowed_clips(project, window_start=0.0, duration=8.0)

    clip = windowed[0]
    assert clip["start"] == pytest.approx(2.0)              # rebased onto window origin (==0)
    assert clip["sourceStart"] == pytest.approx(1.5)        # untouched, no front-trim
    # No shift performed -> the keyframe list passes through by reference.
    assert clip["keyframes"] is original_tracks
    assert clip["keyframes"][0]["points"][0]["t"] == pytest.approx(0.5)


def _single_audio_clip_project(tmp_path):
    """A minimal one-audio-clip project whose asset exists on disk, so the
    _mux_timeline_audio guards (ffmpeg present, clip present, asset exists) all
    pass and execution reaches the subprocess.run call."""
    tone = tmp_path / "tone.wav"
    tone.write_bytes(b"")  # only .exists() is checked before the (mocked) ffmpeg run
    project = timeline.new_project("mux_errpath", width=96, height=64, fps=12)
    project["assets"]["tone"] = {"id": "tone", "type": "audio", "src": str(tone)}
    project["clips"]["tone"] = {
        "id": "tone", "assetId": "tone", "trackId": "audio_1", "kind": "music",
        "start": 0, "duration": 1.0, "sourceStart": 0, "enabled": True,
        "muted": False, "volume": 1, "transform": {},
    }
    return project


# ---------------------------------------------------------------------------
# Render-path keyframe-translation round-trip (libopenshot-FREE).
#
# OpenShotRenderer._timeline feeds openshot_bridge.timeline_json(project) into
# libopenshot — so the keyframe envelope -> OpenShot keyframe JSON translation is
# on the production render path, but the only render-layer coverage of it
# (test_opacity_keyframe_envelope_actually_animates_through_openshot) SKIPS where
# the bindings are absent. These tests drive a full multi-property envelope through
# the exact bridge entrypoint the render path uses (reached via the same
# editor_render.openshot_bridge reference), so the translation is pinned on EVERY
# box, bindings or not. They also exercise the render-layer windowing
# (_render_scope_project) -> bridge JSON hand-off, the round-trip the ticket flags.
# ---------------------------------------------------------------------------


def _full_envelope_project(*, source_start: float = 0.0) -> dict:
    """A clip carrying every keyframable property at once (volume duck + scale
    pop + x/y pan + spin), so one assertion sweep covers the whole property map."""
    project = timeline.new_project("kf_roundtrip", width=1920, height=1080, fps=30)
    project["assets"]["bed"] = {"id": "bed", "type": "audio", "src": "/bed.wav", "metadata": {}}
    project["clips"]["c1"] = {
        "id": "c1", "assetId": "bed", "trackId": "music_1", "kind": "music_bed",
        "start": 4.0, "duration": 3.0, "sourceStart": source_start,
        "enabled": True, "muted": False, "volume": 1.0,
        "transform": {"x": 0.5, "y": 0.5, "scale": 1.0, "opacity": 1.0},
        "effects": [],
        "keyframes": [
            {"property": "volume", "points": [
                {"t": 0.0, "value": 1.0, "interp": "linear"},
                {"t": 1.0, "value": 0.2, "interp": "constant"},
                {"t": 3.0, "value": 1.0, "interp": "bezier"},
            ]},
            {"property": "scale", "points": [
                {"t": 0.0, "value": 1.0}, {"t": 1.5, "value": 1.4},
            ]},
            {"property": "x", "points": [{"t": 0.0, "value": 0.0}, {"t": 3.0, "value": 1.0}]},
            {"property": "y", "points": [{"t": 0.0, "value": 0.5}, {"t": 3.0, "value": 0.25}]},
            {"property": "rotation", "points": [{"t": 0.0, "value": 0.0}, {"t": 3.0, "value": 90.0}]},
        ],
        "metadata": {},
    }
    return project


# --- render-fidelity: a SINGLE clip carrying keyframes + effects + a non-default
# transform all at once must round-trip into the OpenShot timeline JSON that
# OpenShotRenderer.render feeds to Timeline.SetJson (editor_render.py L335). The
# existing bridge tests each exercise one dimension in isolation; the factory
# composites clips that animate, fade, AND are repositioned simultaneously, and
# that combination had no regression coverage. timeline_json() is the exact
# compiler contract, so assert it directly (no libopenshot needed -> never skips).

def _fidelity_project() -> dict:
    """A 96x64@12fps project whose one clip is trimmed (sourceStart=1.0),
    repositioned/scaled, fades in, AND animates opacity 0->1 — every render
    dimension active on the same clip."""
    project = timeline.new_project("fidelity", width=96, height=64, fps=12)
    project["assets"]["card"] = {"id": "card", "type": "image", "src": "/tmp/card.png", "metadata": {}}
    project["clips"]["card_clip"] = {
        "id": "card_clip", "assetId": "card", "trackId": "graphics_1", "kind": "artifact",
        "start": 0.0, "duration": 2.0, "sourceStart": 1.0,
        "enabled": True, "muted": False, "volume": 1.0,
        "transform": {"x": 0.25, "y": 0.75, "scale": 0.5, "opacity": 1.0},
        "effects": [{"id": "fx_in", "type": "fadeIn", "params": {"duration": 0.5}}],
        "keyframes": [{"property": "opacity", "points": [
            {"t": 0.0, "value": 0.0, "interp": "linear"},
            {"t": 2.0, "value": 1.0, "interp": "linear"},
        ]}],
        "metadata": {},
    }
    return project


def test_open_shot_audio_mux_raises_render_error_on_timeout(tmp_path, monkeypatch):
    """services/editor_render.py:808-809 — a TimeoutExpired from the ffmpeg mux
    subprocess is re-raised as RenderError, not allowed to escape raw. Mock
    subprocess.run so the timeout path is deterministic and ffmpeg-independent."""
    project = _single_audio_clip_project(tmp_path)
    video = tmp_path / "silent_video.mp4"
    video.write_bytes(b"")

    monkeypatch.setattr(editor_render.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=900)

    monkeypatch.setattr(editor_render.subprocess, "run", _timeout)

    with pytest.raises(editor_render.RenderError) as excinfo:
        editor_render._mux_timeline_audio(project, video, duration=1.0, asset_root=None)
    assert "timed out" in str(excinfo.value)
    # Original mp4 must be left intact (the temp replace is never reached).
    assert video.exists()


def test_open_shot_audio_mux_raises_render_error_on_nonzero_returncode(tmp_path, monkeypatch):
    """services/editor_render.py:810-811 — a non-zero ffmpeg return code surfaces
    its stderr tail as a RenderError. Mock subprocess.run to fail without touching
    real ffmpeg, and confirm the failed temp output never clobbers the source."""
    project = _single_audio_clip_project(tmp_path)
    video = tmp_path / "silent_video.mp4"
    video.write_bytes(b"original-bytes")

    monkeypatch.setattr(editor_render.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    class _Failed:
        returncode = 1
        stderr = "ffmpeg: invalid filtergraph boom"
        stdout = ""

    monkeypatch.setattr(editor_render.subprocess, "run", lambda *a, **k: _Failed())

    with pytest.raises(editor_render.RenderError) as excinfo:
        editor_render._mux_timeline_audio(project, video, duration=1.0, asset_root=None)
    assert "invalid filtergraph boom" in str(excinfo.value)
    # temp_output.replace(output) is past the raise, so the source is untouched.
    assert video.read_bytes() == b"original-bytes"


def test_render_path_translates_full_keyframe_envelope_to_openshot_json():
    """Every keyframable property fans out to the right OpenShot clip key(s) with the
    documented value transform, monotonic frame X (t*fps + 1 with no trim), and
    interp map — reached through editor_render's own bridge reference, i.e. exactly
    what OpenShotRenderer._timeline serializes. Needs no libopenshot."""
    project = _full_envelope_project()
    clip = editor_render.openshot_bridge.timeline_json(project)["clips"][0]

    # volume: 0..1 -> 0..100, frame X = t*30 + 1, interp linear/constant/bezier preserved
    vol = clip["volume"]["Points"]
    assert [p["co"]["X"] for p in vol] == [1.0, 31.0, 91.0]
    assert [round(p["co"]["Y"], 2) for p in vol] == [100.0, 20.0, 100.0]
    assert [p["interpolation"] for p in vol] == [
        editor_render.openshot_bridge.LINEAR,
        editor_render.openshot_bridge.CONSTANT,
        editor_render.openshot_bridge._INTERPOLATION_MAP["bezier"],
    ]
    # scale: one editor track animates BOTH axes identically (>=0 floor transform)
    assert [p["co"]["Y"] for p in clip["scale_x"]["Points"]] == [1.0, 1.4]
    assert clip["scale_y"]["Points"] == clip["scale_x"]["Points"]
    # x/y: 0..1 -> centered -1..1 (same _location transform as the flat default)
    assert [p["co"]["Y"] for p in clip["location_x"]["Points"]] == [-1.0, 1.0]
    assert [p["co"]["Y"] for p in clip["location_y"]["Points"]] == [0.0, -0.5]
    # rotation: pass-through degrees
    assert [p["co"]["Y"] for p in clip["rotation"]["Points"]] == [0.0, 90.0]
    # an envelope present means the flat single-point default was overridden, not appended
    assert len(clip["scale_x"]["Points"]) == 2
    # frame X is strictly increasing across every multi-point envelope (monotonic time)
    for key in ("volume", "scale_x", "location_x", "location_y", "rotation"):
        xs = [p["co"]["X"] for p in clip[key]["Points"]]
        assert xs == sorted(xs) and len(set(xs)) == len(xs), key


def test_render_path_envelope_frame_X_offsets_by_source_start():
    """A trimmed clip (sourceStart>0) must offset every keyframe's frame X by
    sourceStart*fps so the envelope lands at the right SOURCE frame, not the right
    timeline frame — the trimmed-clip translation the ffmpeg fallback can't do at all."""
    project = _full_envelope_project(source_start=2.0)
    clip = editor_render.openshot_bridge.timeline_json(project)["clips"][0]

    # X = (sourceStart 2.0 + t) * 30fps + 1: volume points at t=0,1,3 -> 61,91,151
    assert [p["co"]["X"] for p in clip["volume"]["Points"]] == [61.0, 91.0, 151.0]
    # the offset is uniform across tracks (scale at t=0 -> same 61 origin)
    assert clip["scale_x"]["Points"][0]["co"]["X"] == 61.0


def test_render_scope_windowing_feeds_shifted_envelope_into_bridge_json():
    """The render path is _render_scope_project (front-trim shifts keyframe t and
    sourceStart) -> timeline_json. A clip windowed from a later start must arrive at
    the bridge with its envelope re-timed: the shifted t AND the bumped sourceStart
    must BOTH land in the OpenShot frame X. This is the windowing<->translation
    round-trip the ticket calls out as having no coverage."""
    project = _full_envelope_project(source_start=1.0)
    # clip spans timeline t=4..7; window t=5..7 -> front_trim = 1.0s.
    scoped = editor_render._render_scope_project(project, window_start=5.0, duration=2.0)
    clip = editor_render.openshot_bridge.timeline_json(scoped)["clips"][0]

    # sourceStart bumped 1.0 + 1.0 = 2.0; volume t's shifted down by 1.0 -> [0,0,2]
    # (first two clamp at 0). Frame X = (2.0 + shifted_t)*30 + 1 -> 61, 61, 121.
    assert [p["co"]["X"] for p in clip["volume"]["Points"]] == [61.0, 61.0, 121.0]
    # values ride along with their (re-timed) points unchanged
    assert [round(p["co"]["Y"], 2) for p in clip["volume"]["Points"]] == [100.0, 20.0, 100.0]
    # the source project envelope is untouched (scoping deep-copies, no aliasing)
    assert project["clips"]["c1"]["keyframes"][0]["points"][0]["t"] == pytest.approx(0.0)
    assert project["clips"]["c1"]["sourceStart"] == pytest.approx(1.0)


def test_keyframes_effects_and_transform_coexist_in_one_openshot_clip_json():
    """Round-trip fidelity for the combined case. The exported OpenShot clip must
    carry, on the SAME clip object: (1) the transform — centered location_x/y and
    the non-1.0 scale on both axes; (2) the fadeIn effect as a Fade(in) object;
    (3) the opacity keyframe ENVELOPE on `alpha`, which must OVERRIDE the flat
    transform-derived alpha (multi-point, source-frame-offset by sourceStart).
    A regression that lets any one dimension clobber another renders the clip
    wrong with no error — exactly the silent-corruption class this suite guards."""
    project = _fidelity_project()
    exported = openshot_bridge.timeline_json(project)
    (clip,) = exported["clips"]

    # (1) transform survived: centered x/y ((v-0.5)*2) and scale on both axes.
    assert clip["location_x"]["Points"][0]["co"]["Y"] == pytest.approx(-0.5)   # x=0.25 -> -0.5
    assert clip["location_y"]["Points"][0]["co"]["Y"] == pytest.approx(0.5)    # y=0.75 -> 0.5
    assert clip["scale_x"]["Points"][0]["co"]["Y"] == pytest.approx(0.5)
    assert clip["scale_y"]["Points"][0]["co"]["Y"] == pytest.approx(0.5)

    # (2) the fadeIn effect rode along as a Fade(in) object, not dropped.
    (effect,) = clip["effects"]
    assert effect["type"] == "Fade" and effect["fade"] == "in"
    assert effect["duration"]["Points"][0]["co"]["Y"] == pytest.approx(0.5)

    # (3) the opacity envelope OVERRODE the flat alpha: a real 2-point keyframe on
    # `alpha`, each X offset into source-reader space by sourceStart=1.0 (frame
    # (sourceStart + t)*fps + 1), Y clamped to 0..1. This is the dimension most
    # likely to be clobbered by the transform's flat alpha default.
    alpha_points = clip["alpha"]["Points"]
    assert len(alpha_points) == 2                                   # envelope, not the flat 1-pt default
    assert alpha_points[0]["co"]["X"] == pytest.approx(1.0 * 12 + 1.0)   # (1.0+0.0)*12+1 = 13
    assert alpha_points[0]["co"]["Y"] == pytest.approx(0.0)
    assert alpha_points[1]["co"]["X"] == pytest.approx(3.0 * 12 + 1.0)   # (1.0+2.0)*12+1 = 37
    assert alpha_points[1]["co"]["Y"] == pytest.approx(1.0)

    # source trim survived alongside everything else (start/end in reader space).
    assert clip["start"] == pytest.approx(1.0)
    assert clip["end"] == pytest.approx(3.0)


def test_combined_keyframes_effects_transform_render_to_valid_frame_through_openshot(tmp_path):
    """End-to-end on a box that has libopenshot: the SAME combined clip (keyframes
    + fadeIn + non-default transform) must render to a real frame through the prod
    OpenShot path, not just translate to correct JSON. Pins the full compiler path
    for the combined case; skips cleanly where the bindings are absent (CI)."""
    if not editor_render._import_openshot()[0]:
        pytest.skip("libopenshot Python bindings not importable here")

    white = _solid_png(tmp_path / "card.png", "white", size="96x64")
    project = _fidelity_project()
    project["assets"]["card"]["src"] = str(white)
    project.update({"sampleRate": 48000, "channels": 2, "channelLayout": 3})

    renderer = editor_render.OpenShotRenderer(tmp_path / "renders")
    output = tmp_path / "renders" / "fidelity.mp4"
    result = renderer.render(project, output_path=output)

    assert result["backend"] == "openshot"
    assert result["missingAssets"] == []
    assert Path(result["video"]).exists()
    assert _probe_duration(output) >= 1.8                # full 2s clip rendered

    # the opacity envelope (0 at head, 1 at tail) actually animated through the
    # combined clip: a late frame is brighter than an early one.
    early = _frame_png_at(output, tmp_path / "early.png", at=0.1)
    late = _frame_png_at(output, tmp_path / "late.png", at=1.9)
    # sample the clip's repositioned/scaled region near frame center.
    assert _sample_rgb(late, 48, 32)[0] >= _sample_rgb(early, 48, 32)[0]
