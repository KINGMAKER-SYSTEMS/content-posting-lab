import json
import shutil
import subprocess
import sys
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
           enabled: bool = True, keyframes: list | None = None) -> dict:
    return {
        "id": clip_id,
        "assetId": clip_id,
        "trackId": "graphics_1",
        "kind": "artifact",
        "start": start,
        "duration": duration,
        "sourceStart": source_start,
        "enabled": enabled,
        "keyframes": keyframes or [],
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


def test_visual_filter_crossfade_is_dropped_not_faked(tmp_path):
    """A `crossfade` effect on a VISUAL (image/video) clip has no ffmpeg-native
    equivalent in the overlay path, so _visual_filter must NOT emit a fade/xfade
    term for it (unlike fadeIn/fadeOut). The drop is intentional and silent in the
    filter string — _ffmpeg_unsupported_drops is what surfaces it as a warning.

    This pins the audio/visual inconsistency the ticket flagged: the mux path
    applies an `afade` for fades, but a crossfade on a visual layer must vanish
    from the graph rather than being mistaken for a fade (which would corrupt the
    clip's alpha). It also guards against a future edit accidentally wiring
    crossfade into the `fades` accumulator."""
    r = _renderer(tmp_path)
    clip = {"id": "cf", "assetId": "v", "duration": 2.0, "sourceStart": 0.0, "transform": {},
            "effects": [{"id": "fx", "type": "crossfade", "params": {"duration": 0.5}}]}
    out = r._visual_filter(2, clip, {"type": "video"}, "lbl", 1920, 1080)
    assert "fade=" not in out   # crossfade is NOT honored as a native fade
    assert "xfade" not in out   # nor faked as an ffmpeg xfade
    # but the drop is reported by the warning collector, never lost in silence
    drops = editor_render._ffmpeg_unsupported_drops(clip)
    assert {"clipId": "cf", "kind": "effect", "name": "crossfade"} in drops


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


def test_visual_filter_no_rotation_omits_rotate_term(tmp_path):
    """The common (unrotated) clip must NOT carry a `rotate` filter — it would be a
    wasted per-frame op and risk sub-pixel resampling on every overlay."""
    r = _renderer(tmp_path)
    clip = {"id": "c", "assetId": "i", "duration": 1.0, "sourceStart": 0.0, "transform": {}}
    out = r._visual_filter(1, clip, {"type": "image"}, "lbl", 1920, 1080)
    assert "rotate=" not in out


def test_visual_filter_static_rotation_emits_rotate_in_radians(tmp_path):
    """A flat `rotation` transform (degrees, per the editor schema / OpenShot bridge)
    must map to ffmpeg's `rotate` filter, which takes RADIANS — so the angle is
    converted via *PI/180, fills corners transparently (c=none), and uses a constant
    diagonal output box so no angle clips the corners."""
    r = _renderer(tmp_path)
    clip = {"id": "c", "assetId": "i", "duration": 1.0, "sourceStart": 0.0,
            "transform": {"rotation": 90.0}}
    out = r._visual_filter(1, clip, {"type": "image"}, "lbl", 1920, 1080)
    assert "rotate=a='90.0000*PI/180':c=none" in out
    assert "ow='hypot(iw,ih)':oh='hypot(iw,ih)'" in out


def test_visual_filter_rotation_keyframes_emit_time_expr_not_dropped(tmp_path):
    """The ticket: a `rotation` keyframe envelope is validated in the schema and
    translated by the OpenShot bridge, but the ffmpeg fallback used to silently drop
    it. It must now reproduce the envelope as a piecewise-linear `t`-expression fed to
    the `rotate` angle (still converted degrees -> radians), and NOT be reported as an
    unsupported drop."""
    r = _renderer(tmp_path)
    clip = {"id": "rot", "assetId": "i", "duration": 2.0, "sourceStart": 0.0,
            "transform": {},
            "keyframes": [{
                "property": "rotation",
                "points": [{"t": 0.0, "value": 0.0, "interp": "linear"},
                           {"t": 2.0, "value": 180.0, "interp": "linear"}],
            }]}
    out = r._visual_filter(1, clip, {"type": "image"}, "lbl", 1920, 1080)
    assert "rotate=a='(" in out          # angle is a t-expression, not a constant
    assert ")*PI/180':c=none" in out     # converted to radians
    assert "lt(t," in out or "if(" in out  # piecewise-linear interpolation over t
    # and crucially the envelope is NOT surfaced as a dropped keyframe anymore
    drops = editor_render._ffmpeg_unsupported_drops(clip)
    assert not any(d.get("name") == "rotation" for d in drops)


def test_rotation_keyframe_filtergraph_actually_renders(tmp_path):
    """End-to-end: a rotation ENVELOPE must produce a filtergraph ffmpeg accepts and
    a real output file. A string-only assert can't catch the trap that broke the first
    cut: `rotate` evaluates ow/oh once at init, so a time-varying rotw()/roth() output
    box is rejected — and hypot()'s comma in ow/oh must be quoted or the filtergraph
    parser splits the filter. This render is the proof the chain is valid."""
    card = _solid_png(tmp_path / "card.png", "white", size="16x16")
    project = _project_with_card("rot_anim", card, x=0.5)
    project["clips"]["card_clip"]["keyframes"] = [
        {"property": "rotation", "points": [
            {"t": 0.0, "value": 0.0, "interp": "linear"},
            {"t": 1.0, "value": 90.0, "interp": "linear"}]},
    ]
    renderer = editor_render.FFmpegLayeredRenderer(tmp_path / "renders")
    result = renderer.render(project, output_path=tmp_path / "renders" / "rot.mp4")
    out = Path(result["video"])
    assert out.exists() and out.stat().st_size > 0
    # the envelope was honored, not reported as an unsupported drop
    assert not any(
        w.get("name") == "rotation" for w in (result.get("warnings") or [])
    )


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


def test_build_video_command_all_clips_disabled_still_emits_one_input(tmp_path):
    """All clips disabled -> visual_clips and audio_clips are both empty. The
    command must STILL carry the always-present black lavfi base as input 0 (never
    0 inputs, which ffmpeg rejects), produce a mapped [v] stream, and map no audio.
    Pins the invariant the ticket flagged: the builder must never emit a 0-input
    argv even when every timeline clip is turned off."""
    red = _solid_png(tmp_path / "red.png", "red")
    project = _project_with_card("all_disabled", red, x=0.0)
    project["clips"]["card_clip"]["enabled"] = False
    r = _renderer(tmp_path)
    cmd, missing, warnings = r._build_video_command(
        project, tmp_path / "out.mp4", duration=1.0, window_start=0.0
    )
    assert missing == []
    # exactly one -i (the black base); the disabled card is never fed as an input.
    assert cmd.count("-i") == 1
    assert "color=c=black:s=96x64:r=12:d=1.000" in cmd
    assert cmd[cmd.index("-map") + 1] == "[v]"
    assert "[a]" not in cmd  # no audio clips -> no audio map


def test_build_video_command_empty_project_renders_black_not_zero_inputs(tmp_path):
    """A project with NO clips at all (empty visual + empty audio lists) must
    compile to a single-input black-base command whose only video filter is the
    format-conversion of the base layer — and render end-to-end rather than
    emitting a 0-input argv that ffmpeg would reject at subprocess time."""
    project = timeline.new_project("empty_proj", width=96, height=64, fps=12)
    project["clips"].clear()
    r = _renderer(tmp_path)
    cmd, missing, warnings = r._build_video_command(
        project, tmp_path / "out.mp4", duration=0.5, window_start=0.0
    )
    assert missing == [] and warnings == []
    assert cmd.count("-i") == 1  # the black base is the sole input, never zero
    filter_arg = cmd[cmd.index("-filter_complex") + 1]
    assert filter_arg == "[0:v]format=yuv420p[v]"  # only the base layer formatted
    assert "[a]" not in cmd
    # and it actually renders — proving the 1-input command is valid for ffmpeg.
    result = r.render(project, output_path=tmp_path / "empty.mp4", start=0, duration=0.5)
    assert Path(result["video"]).exists()


def test_build_video_command_tolerates_missing_assets_key(tmp_path):
    """A malformed project missing the top-level 'assets' key must NOT KeyError.
    `assets = project.get("assets") or {}` defaults it; the clip then resolves to
    type-unknown (no asset entry), is fed no -i input, and the builder degrades to
    the black-base command instead of crashing. Pins the defensive default."""
    project = {
        "projectId": "no_assets",
        "width": 96,
        "height": 64,
        "fps": 12,
        "tracks": {"graphics_1": {"id": "graphics_1", "index": 0}},
        "clips": {
            "c": {
                "id": "c",
                "assetId": "card",
                "trackId": "graphics_1",
                "kind": "artifact",
                "start": 0.0,
                "duration": 1.0,
                "sourceStart": 0.0,
                "enabled": True,
                "transform": {},
                "effects": [],
                "keyframes": [],
            }
        },
    }
    r = _renderer(tmp_path)
    cmd, missing, warnings = r._build_video_command(
        project, tmp_path / "out.mp4", duration=1.0, window_start=0.0
    )
    # no 'assets' map -> the clip has no resolvable type, contributes no input,
    # and the command falls back to the single black-base input without raising.
    assert cmd.count("-i") == 1
    assert missing == []
    assert cmd[cmd.index("-map") + 1] == "[v]"


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


def _opacity_keyframes(*points: tuple[float, float]) -> list[dict]:
    """An `opacity` keyframe track: each point is (t_seconds_from_clip_start, value)."""
    return [{
        "property": "opacity",
        "points": [{"t": t, "value": v, "interp": "linear"} for t, v in points],
    }]


def _alpha_xs(clip: dict, project: dict) -> list[float]:
    """Export `clip` to its OpenShot payload and return the keyframe X (frame
    number in source-reader space) of every `alpha` point. `alpha` is what an
    `opacity` keyframe track compiles to (see openshot_bridge._KEYFRAME_PROPERTY_MAP)."""
    payload = openshot_bridge.clip_json(project, clip)
    return [p["co"]["X"] for p in payload["alpha"]["Points"]]


def test_windowed_clip_keyframe_fires_at_same_source_frame_as_unwindowed():
    """Regression guard for the front_trim / sourceStart keyframe-X coupling.

    A clip that spans a render-window boundary gets BOTH its `sourceStart` bumped
    by `front_trim` (editor_render line ~913) and its keyframe `t` shifted down by
    the same `front_trim` (line ~918). OpenShot computes keyframe X as
    `(sourceStart + t) * fps + 1`, so the two adjustments must cancel and leave
    every surviving keyframe pinned to the SAME source frame it had before
    windowing. If one adjustment happens without the other, X drifts and the
    animation fires at the wrong frame. No prior test imported -> windowed ->
    exported with keyframes present; this locks the invariant in."""

    fps = 12
    # Clip spans timeline [1, 5); source starts at 10s. Keyframes at 1s and 3s
    # into the clip (both AFTER the 1s that the window will trim off the front).
    kfs = _opacity_keyframes((1.0, 0.0), (3.0, 1.0))
    project = _window_project(_wclip("a", start=1.0, duration=4.0, source_start=10.0, keyframes=kfs))

    before = _alpha_xs(project["clips"]["a"], project)
    # source frame for t=1: (10+1)*12+1 = 133 ; for t=3: (10+3)*12+1 = 157
    assert before == pytest.approx([133.0, 157.0])

    # Window [2, 6): front_trim = 2 - 1 = 1s. sourceStart -> 11, kf t -> {0, 2}.
    scoped = editor_render._render_scope_project(project, window_start=2.0, duration=4.0)
    windowed = next(iter(scoped["clips"].values()))
    assert windowed["sourceStart"] == pytest.approx(11.0)

    after = _alpha_xs(windowed, scoped)
    # Both keyframes survive the trim, so X must be IDENTICAL to the unwindowed
    # export -- the source frame each one points at has not moved.
    assert after == pytest.approx(before)


def test_trimmed_clip_keyframe_at_t0_offsets_by_source_start():
    """Direct guard on the sourceStart frame-number offset (openshot_bridge:496).

    A clip trimmed via clip.trim carries sourceStart > 0. Its keyframe at t=0 must
    NOT compile to X=1 (the source-reader origin); it must compile to
    (sourceStart + 0)*fps + 1 so the animation fires at the frame the trimmed clip
    actually starts reading from. A second keyframe at t>0 must offset by the same
    base plus its own t. Prior keyframe tests only reach this offset *through*
    _render_scope_project windowing; none asserts clip_json applies it on a clip
    that is merely source-trimmed with a t=0 keyframe."""

    fps = 12
    source_start = 4.0  # clip.trim left the front 4s of source off the timeline
    kfs = _opacity_keyframes((0.0, 0.0), (2.5, 1.0))
    project = _window_project(
        _wclip("a", start=0.0, duration=3.0, source_start=source_start, keyframes=kfs)
    )

    xs = _alpha_xs(project["clips"]["a"], project)
    # t=0 -> (4+0)*12+1 = 49 (NOT 1) ; t=2.5 -> (4+2.5)*12+1 = 79
    assert xs == pytest.approx([49.0, 79.0])
    assert xs[0] != pytest.approx(1.0)  # offset really applied, not source-origin


def test_windowed_clip_keyframe_before_window_clamps_to_window_start():
    """A keyframe in the trimmed-away front region clamps to the window's first
    source frame (t -> 0 after the shift), not past it. Asserts the clamp in
    _shift_keyframes and the sourceStart offset agree on the windowed origin."""

    fps = 12
    # Keyframe at t=0.5s sits inside the 1s that gets trimmed; t=3s survives.
    kfs = _opacity_keyframes((0.5, 0.0), (3.0, 1.0))
    project = _window_project(_wclip("a", start=1.0, duration=4.0, source_start=10.0, keyframes=kfs))

    scoped = editor_render._render_scope_project(project, window_start=2.0, duration=4.0)
    windowed = next(iter(scoped["clips"].values()))
    after = _alpha_xs(windowed, scoped)

    # window origin source frame = (11 + 0)*12 + 1 = 133 ; survivor t=2 -> (11+2)*12+1 = 157
    assert after == pytest.approx([133.0, 157.0])


def _volume_keyframes(*points: "tuple[float, float]") -> list[dict]:
    """A `volume` keyframe track: each point is (t_seconds_from_clip_start, gain)."""
    return [{
        "property": "volume",
        "points": [{"t": t, "value": v, "interp": "linear"} for t, v in points],
    }]


def test_volume_filter_envelope_overrides_flat_gain_even_when_not_neutralized():
    """Backend-parity safety net: even if a caller leaves a non-1.0 flat `volume`
    (the legacy 0.22 bed duck) ALONGSIDE an absolute volume envelope, _volume_filter
    must emit the envelope's absolute gains and IGNORE the flat base_volume — not
    scale by it (the documented 0.6 -> 0.6*0.22 double-attenuation bug). This mirrors
    openshot_bridge, where the envelope overwrites the flat volume key outright.

    editor_timeline now neutralizes the flat gain to 1.0 on both import and the
    post-import clip.keyframes command, but this pins the render-side defense so the
    two backends agree regardless of upstream neutralization."""
    clip = {"keyframes": _volume_keyframes((0.0, 0.6), (1.0, 0.22))}
    # Flat gain present (0.22) AND an envelope -> the envelope wins, unscaled.
    flt_with_stale = editor_render._volume_filter(clip, 0.22)
    flt_neutralized = editor_render._volume_filter(clip, 1.0)
    # Identical output: base_volume is ignored once an envelope exists.
    assert flt_with_stale == flt_neutralized
    assert "0.6000" in flt_with_stale
    # NOT a base-scaled form: the intro gain is plain 0.6, never (0.2200)*(...).
    assert "(0.2200)*(" not in flt_with_stale and ":eval=frame" in flt_with_stale


def test_windowed_volume_envelope_keeps_absolute_gains_through_window_shift():
    """A ducking volume envelope (absolute gains: 0.6 under intro, 0.22 under VO)
    must survive _render_scope_project windowing with its GAIN values unchanged --
    only the keyframe `t` may shift. _shot_keyframes promotes such an envelope onto
    the clip (editor_timeline ~450-475); if windowing ever mutated `value` the music
    bed would duck to the wrong level (0.6 -> 0.22 or vice versa) or go silent.

    This locks the invariant at the _shift_keyframes seam directly on a `volume`
    track -- prior windowing tests only covered `opacity`/`alpha`."""

    # Clip spans timeline [1, 9); the front 1s gets trimmed by the window below.
    # Envelope: full-level intro point, ducked-under-VO point, recovery point.
    kfs = _volume_keyframes((0.5, 0.6), (2.0, 0.22), (6.0, 0.6))
    project = _window_project(
        _wclip("bed", start=1.0, duration=8.0, source_start=10.0, keyframes=kfs)
    )

    # Window [2, 8): front_trim = 2 - 1 = 1s. Each point's `t` shifts down by 1
    # (clamped at 0); every gain `value` must be byte-for-byte identical.
    scoped = editor_render._render_scope_project(project, window_start=2.0, duration=6.0)
    windowed = next(iter(scoped["clips"].values()))

    vol_track = next(t for t in windowed["keyframes"] if t["property"] == "volume")
    shifted = [(p["t"], p["value"]) for p in vol_track["points"]]

    # t: 0.5 -> 0 (clamped), 2.0 -> 1.0, 6.0 -> 5.0 ; gains untouched.
    assert shifted == pytest.approx([(0.0, 0.6), (1.0, 0.22), (5.0, 0.6)])
    # The original project envelope is not mutated by windowing.
    orig = project["clips"]["bed"]["keyframes"][0]["points"]
    assert [(p["t"], p["value"]) for p in orig] == [(0.5, 0.6), (2.0, 0.22), (6.0, 0.6)]


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
        # scale envelopes stay OpenShot-only (an animated scale re-times
        # overlay_w/overlay_h mid-overlay) -> must still surface as a warning.
        {"property": "scale", "points": [
            {"t": 0.0, "value": 0.5, "interp": "linear"},
            {"t": 1.0, "value": 1.0, "interp": "linear"},
        ]},
    ]
    renderer = editor_render.FFmpegLayeredRenderer(tmp_path / "renders")
    result = renderer.render(project, output_path=tmp_path / "renders" / "warn_fixture.mp4")

    assert Path(result["video"]).exists()  # still renders
    names = {(w["kind"], w["name"]) for w in result["warnings"]}
    assert ("effect", "crossfade") in names
    assert ("effect", "brightness") in names
    assert ("keyframe", "scale") in names
    # frame previews carry the same warning contract. Preview INSIDE the crossfade's
    # [0, 0.3] window (at=0.1) — at a later frame the front trim correctly drops the
    # spent fade, so there'd be nothing to warn about.
    frame = renderer.render_frame(project, at=0.1, output_path=tmp_path / "renders" / "warn.png")
    assert any(w["name"] == "crossfade" for w in frame["warnings"])


def test_ffmpeg_strict_drops_refuses_render_instead_of_degrading(tmp_path, monkeypatch):
    """Hard gate: OpenShot is the sanctioned compiler, so a degraded ffmpeg render that
    dropped a video crossfade must never SILENTLY complete and risk being published.
    With EDITOR_RENDER_STRICT_DROPS=1 the fallback raises instead of emitting the
    artifact; without it the same project renders (warn-and-continue, the default)."""
    red = _solid_png(tmp_path / "red.png", "red")
    project = _project_with_card("strict_fixture", red, x=0.0)
    # A video crossfade the ffmpeg fallback genuinely cannot reproduce on a visual layer.
    project["clips"]["card_clip"]["effects"] = [
        {"id": "fx_cf", "type": "crossfade", "params": {"duration": 0.3}},
    ]
    out = tmp_path / "renders" / "strict_fixture.mp4"

    # Default (off): renders despite the drop, surfacing it as a warning.
    monkeypatch.delenv("EDITOR_RENDER_STRICT_DROPS", raising=False)
    renderer = editor_render.FFmpegLayeredRenderer(tmp_path / "renders")
    lenient = renderer.render(project, output_path=out)
    assert Path(lenient["video"]).exists()
    assert any(w["name"] == "crossfade" for w in lenient["warnings"])
    out.unlink()

    # Strict (on): refuse rather than ship a degraded artifact, and write nothing.
    monkeypatch.setenv("EDITOR_RENDER_STRICT_DROPS", "1")
    with pytest.raises(editor_render.RenderError) as exc:
        renderer.render(project, output_path=out)
    assert "crossfade" in str(exc.value)
    assert not out.exists()  # no degraded turd left behind


def test_ffmpeg_strict_drops_allows_fully_reproducible_render(tmp_path, monkeypatch):
    """Strict mode must NOT block a render the fallback can reproduce faithfully. A
    plain fadeIn (ffmpeg-native, zero warnings) still renders with the gate on."""
    monkeypatch.setenv("EDITOR_RENDER_STRICT_DROPS", "1")
    red = _solid_png(tmp_path / "red.png", "red", size="96x64")
    project = _project_with_card("strict_ok", red, x=0.0)
    project["clips"]["card_clip"]["effects"] = [
        {"id": "fx_in", "type": "fadeIn", "params": {"duration": 0.5}},
    ]
    renderer = editor_render.FFmpegLayeredRenderer(tmp_path / "renders")
    result = renderer.render(project, output_path=tmp_path / "renders" / "strict_ok.mp4")
    assert result["warnings"] == []
    assert Path(result["video"]).exists()


def test_ffmpeg_strict_drops_env_parsing():
    """The strict-mode toggle accepts the usual truthy spellings and stays off for
    empty/falsey values, so an accidental `EDITOR_RENDER_STRICT_DROPS=0` doesn't gate."""
    import os as _os

    saved = _os.environ.pop("EDITOR_RENDER_STRICT_DROPS", None)
    try:
        for val, expected in [("1", True), ("true", True), ("ON", True), ("yes", True),
                               ("0", False), ("false", False), ("", False)]:
            _os.environ["EDITOR_RENDER_STRICT_DROPS"] = val
            assert editor_render._ffmpeg_strict_drops() is expected, val
        _os.environ.pop("EDITOR_RENDER_STRICT_DROPS", None)
        assert editor_render._ffmpeg_strict_drops() is False  # unset -> off
    finally:
        if saved is not None:
            _os.environ["EDITOR_RENDER_STRICT_DROPS"] = saved
        else:
            _os.environ.pop("EDITOR_RENDER_STRICT_DROPS", None)


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


# --- keyframe-envelope support in the ffmpeg fallback (opacity + position) ---
# The ticket: the fallback silently lost crossfade/color/keyframe envelopes; the
# volume envelope was already wired into the mux path. These pin the remaining
# wins — opacity (alpha over time via geq) and clip-position (overlay x/y over
# time) keyframes — and confirm scale stays an explicit OpenShot-only warning.


def test_overlay_expr_builds_position_keyframe_time_expression(tmp_path):
    """An x/y keyframe envelope must compile to a piecewise-linear overlay
    position expression (the overlay filter accepts the timeline `t`), not a
    static fraction. A clip with no envelope keeps the cheap constant."""
    clip = {
        "transform": {"x": 0.5, "y": 0.5},
        "keyframes": [
            {"property": "x", "points": [
                {"t": 0.0, "value": 0.0}, {"t": 1.0, "value": 1.0}]},
        ],
    }
    x_expr, y_expr = editor_render._overlay_expr(clip)
    assert "if(lt(t," in x_expr and "(main_w-overlay_w)" in x_expr  # animated x
    assert "if(lt(t," not in y_expr and "0.500000" in y_expr        # static y


def test_visual_filter_opacity_keyframe_emits_geq_alpha_expression(tmp_path):
    """An opacity envelope drives the alpha plane over time via geq (the one
    primitive that takes a `T`-expression on alpha); without an envelope the
    cheaper colorchannelmixer constant is used."""
    r = _renderer(tmp_path)
    clip = {
        "duration": 2.0, "sourceStart": 0.0,
        "transform": {"opacity": 1.0, "scale": 1.0},
        "keyframes": [
            {"property": "opacity", "points": [
                {"t": 0.0, "value": 0.0}, {"t": 2.0, "value": 1.0}]},
        ],
    }
    out = r._visual_filter(1, clip, {"type": "image"}, "lbl", 96, 64)
    assert "geq=" in out and "a='255*max(0,min(1,if(lt(T," in out
    assert "colorchannelmixer" not in out  # envelope path, not the flat default


def test_position_keyframe_actually_moves_clip_in_fallback_render(tmp_path):
    """End-to-end: a card whose x animates 0 -> 1 over 1s must sit on the LEFT
    early and the RIGHT late in the real ffmpeg-fallback render. Proves the
    overlay x time-expression is honored, not silently flattened."""
    card = _solid_png(tmp_path / "card.png", "white", size="16x16")
    project = _project_with_card("pos_anim", card, x=0.0)
    project["clips"]["card_clip"]["keyframes"] = [
        {"property": "x", "points": [
            {"t": 0.0, "value": 0.0, "interp": "linear"},
            {"t": 1.0, "value": 1.0, "interp": "linear"},
        ]},
    ]
    renderer = editor_render.FFmpegLayeredRenderer(tmp_path / "renders")
    result = renderer.render(project, output_path=tmp_path / "renders" / "pos.mp4")
    assert result["warnings"] == []  # x keyframe is honored, not dropped

    video = Path(result["video"])
    early = _frame_png_at(video, tmp_path / "early.png", at=0.05)
    late = _frame_png_at(video, tmp_path / "late.png", at=0.85)
    # 96x64 frame, 16x16 card pinned to the top (y=0 -> rows 0..15), so sample
    # row 8. As x animates 0 -> 1 the card slides left -> right: col 10 sits under
    # the card early (near the left), col 80 under it late (near the right).
    assert _sample_rgb(early, 10, 8)[0] > _sample_rgb(early, 80, 8)[0]
    assert _sample_rgb(late, 80, 8)[0] > _sample_rgb(late, 10, 8)[0]


def test_opacity_keyframe_actually_ramps_alpha_in_fallback_render(tmp_path):
    """End-to-end: a white card whose opacity animates 0 -> 1 over its 1s must be
    near-invisible (dark base shows through) early and bright late. Proves the geq
    alpha envelope renders, not just compiles."""
    card = _solid_png(tmp_path / "card.png", "white", size="96x64")
    project = _project_with_card("op_anim", card, x=0.0)
    project["clips"]["card_clip"]["keyframes"] = [
        {"property": "opacity", "points": [
            {"t": 0.0, "value": 0.0, "interp": "linear"},
            {"t": 1.0, "value": 1.0, "interp": "linear"},
        ]},
    ]
    renderer = editor_render.FFmpegLayeredRenderer(tmp_path / "renders")
    result = renderer.render(project, output_path=tmp_path / "renders" / "op.mp4")
    assert result["warnings"] == []  # opacity keyframe is honored, not dropped

    video = Path(result["video"])
    early = _frame_png_at(video, tmp_path / "early.png", at=0.05)
    late = _frame_png_at(video, tmp_path / "late.png", at=0.85)
    assert _sample_rgb(early, 48, 32)[0] < _sample_rgb(late, 48, 32)[0]
    assert _sample_rgb(late, 48, 32)[0] > 150


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
    # choose_renderer wraps the chosen backend in a health-stamping proxy; the real
    # subprocess renderer is the proxied inner instance.
    assert renderer._inner.__class__.__name__ == "OpenShotSubprocessRenderer"


def test_backend_health_marks_ffmpeg_fallback_as_downgrade():
    capabilities = {
        "openshot": {"available": False, "preferred": True, "reason": "bindings missing"},
        "ffmpeg": {"available": True, "preferred": False, "reason": "available"},
    }
    health = editor_render._backend_health(capabilities, "ffmpeg")
    assert health["selected"] == "ffmpeg"
    assert health["preferred"] == "openshot"
    assert health["downgraded"] is True
    assert health["reason"] == "bindings missing"


def test_backend_health_no_downgrade_when_preferred_selected():
    capabilities = {
        "openshot": {"available": True, "preferred": True, "reason": "available"},
        "ffmpeg": {"available": True, "preferred": False, "reason": "available"},
    }
    health = editor_render._backend_health(capabilities, "openshot")
    assert health["downgraded"] is False
    assert health["reason"] == ""


def test_choose_renderer_stamps_backend_health_and_warnings_on_ffmpeg_fallback(monkeypatch, tmp_path):
    """When OpenShot is unavailable, the ffmpeg fallback render result must carry a
    backendHealth block (downgraded=True + reason) AND a warnings list, so the
    frontend can show WHY the episode silently dropped to ffmpeg and what it lost."""
    monkeypatch.setattr(
        editor_render,
        "detect_render_backends",
        lambda: {
            "openshot": {"available": False, "preferred": True, "reason": "Python bindings not importable"},
            "ffmpeg": {"available": True, "preferred": False, "reason": "available"},
        },
    )
    image = _solid_png(tmp_path / "still.png", "red")
    project = _project_with_card("health", image)

    renderer = editor_render.choose_renderer(tmp_path / "renders", asset_root=tmp_path)
    assert renderer.backend == "ffmpeg"
    result = renderer.render(project, start=0, duration=0.5)

    assert result["backendHealth"]["downgraded"] is True
    assert result["backendHealth"]["selected"] == "ffmpeg"
    assert result["backendHealth"]["preferred"] == "openshot"
    assert "Python bindings not importable" in result["backendHealth"]["reason"]
    assert "warnings" in result  # uniform contract regardless of backend


class _FakeInner:
    """Minimal renderer stand-in for proxy tests: exposes render/render_frame plus
    extra methods/attributes the proxy must forward through __getattr__."""

    backend = "fake"

    def __init__(self):
        self.output_dir = Path("/tmp/out")
        self.asset_root = Path("/tmp/assets")
        self._timeline = {"clips": []}

    def render(self, *a, **k):
        return {"ok": "render"}

    def render_frame(self, *a, **k):
        return {"ok": "frame"}

    def some_helper(self, x):
        return x * 2


def test_health_stamp_proxy_forwards_wrapped_methods_other_than_render():
    """__getattr__ fall-through must forward arbitrary methods (e.g. helpers the
    callers reach for) to the inner renderer, not just render/render_frame."""
    proxy = editor_render._HealthStampingRenderer(_FakeInner(), {"selected": "fake"})
    assert proxy.some_helper(21) == 42


def test_health_stamp_proxy_forwards_attributes_through_getattr():
    """output_dir/asset_root/_timeline live only on the inner renderer; the proxy
    must surface them via __getattr__ so existing call sites keep working."""
    inner = _FakeInner()
    proxy = editor_render._HealthStampingRenderer(inner, {})
    assert proxy.output_dir == inner.output_dir
    assert proxy.asset_root == inner.asset_root
    assert proxy._timeline == inner._timeline


def test_health_stamp_proxy_propagates_attributeerror_from_missing_attr():
    """When the inner renderer has no such attribute, getattr raises AttributeError;
    the proxy must let it propagate (not swallow it or recurse on _inner)."""
    proxy = editor_render._HealthStampingRenderer(_FakeInner(), {})
    with pytest.raises(AttributeError):
        _ = proxy.does_not_exist


def test_health_stamp_proxy_propagates_when_inner_getattr_itself_raises():
    """If the inner object's own __getattr__ raises a non-AttributeError, the proxy
    must surface that exact error rather than masking it as AttributeError."""

    class _AngryInner:
        def __getattr__(self, name):
            raise RuntimeError(f"boom:{name}")

    proxy = editor_render._HealthStampingRenderer(_AngryInner(), {})
    with pytest.raises(RuntimeError, match="boom:whatever"):
        _ = proxy.whatever


def _fake_which(present):
    """shutil.which stand-in: returns a fake path for binaries in ``present``."""

    def which(binary):
        return f"/usr/bin/{binary}" if binary in present else None

    return which


def test_detect_backends_reports_openshot_version_and_path_when_importable(monkeypatch):
    class FakeModule:
        @staticmethod
        def GetVersion():
            return "0.3.3"

    monkeypatch.setattr(
        editor_render,
        "_import_openshot",
        lambda: (FakeModule, "available", Path("/opt/openshot/python")),
    )
    monkeypatch.setattr(editor_render.shutil, "which", _fake_which({"ffmpeg", "ffprobe"}))

    caps = editor_render.detect_render_backends()

    assert caps["openshot"]["available"] is True
    assert caps["openshot"]["preferred"] is True
    assert caps["openshot"]["reason"] == "available"
    assert caps["openshot"]["version"] == "0.3.3"
    assert caps["openshot"]["pythonPath"] == "/opt/openshot/python"


def test_detect_backends_marks_openshot_unavailable_and_nulls_version(monkeypatch):
    monkeypatch.setattr(
        editor_render,
        "_import_openshot",
        lambda: (None, "Python bindings not importable in this environment", None),
    )
    monkeypatch.setattr(editor_render.shutil, "which", _fake_which({"ffmpeg", "ffprobe"}))

    caps = editor_render.detect_render_backends()

    assert caps["openshot"]["available"] is False
    assert caps["openshot"]["version"] is None
    assert caps["openshot"]["pythonPath"] is None
    assert "not importable" in caps["openshot"]["reason"]


def test_detect_backends_ffmpeg_needs_both_ffmpeg_and_ffprobe(monkeypatch):
    monkeypatch.setattr(editor_render, "_import_openshot", lambda: (None, "x", None))
    # ffmpeg present but ffprobe missing -> not usable.
    monkeypatch.setattr(editor_render.shutil, "which", _fake_which({"ffmpeg"}))

    caps = editor_render.detect_render_backends()

    assert caps["ffmpeg"]["available"] is False
    assert caps["ffmpeg"]["reason"] == "ffmpeg/ffprobe missing"


def test_detect_backends_reports_optional_mlt_and_blender(monkeypatch):
    monkeypatch.setattr(editor_render, "_import_openshot", lambda: (None, "x", None))
    monkeypatch.setattr(
        editor_render.shutil,
        "which",
        _fake_which({"ffmpeg", "ffprobe", "melt", "blender"}),
    )

    caps = editor_render.detect_render_backends()

    assert caps["ffmpeg"]["available"] is True
    assert caps["mlt"]["available"] is True
    assert caps["mlt"]["binary"] == "/usr/bin/melt"
    assert caps["blender"]["available"] is True
    assert caps["blender"]["binary"] == "/usr/bin/blender"
    # mlt/blender are detected but never preferred over openshot/ffmpeg.
    assert caps["mlt"]["reason"] == "available"
    assert caps["blender"]["reason"] == "available"


def test_choose_renderer_falls_back_to_ffmpeg_when_openshot_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(
        editor_render,
        "detect_render_backends",
        lambda: {
            "openshot": {"available": False, "preferred": True, "reason": "missing"},
            "ffmpeg": {"available": True, "preferred": False, "reason": "available"},
        },
    )

    renderer = editor_render.choose_renderer(tmp_path / "renders")

    assert renderer.backend == "ffmpeg"
    # choose_renderer wraps the concrete backend in _HealthStampingRenderer
    # (compiler-cascade health surfacing); assert through the proxied inner renderer.
    assert renderer._inner.__class__.__name__ == "FFmpegLayeredRenderer"


def test_choose_renderer_runs_openshot_in_process_inside_render_child(monkeypatch, tmp_path):
    monkeypatch.setattr(
        editor_render,
        "detect_render_backends",
        lambda: {
            "openshot": {"available": True, "preferred": True, "reason": "available"},
            "ffmpeg": {"available": True, "preferred": False, "reason": "available"},
        },
    )
    monkeypatch.setenv("EDITOR_RENDER_CHILD", "1")
    # OpenShotRenderer.__init__ re-imports the bindings; stub so the in-process
    # branch is exercised even on hosts without libopenshot installed.
    monkeypatch.setattr(
        editor_render, "_import_openshot", lambda: (object(), "available", None)
    )

    renderer = editor_render.choose_renderer(tmp_path / "renders")

    assert renderer.backend == "openshot"
    assert renderer.__class__.__name__ == "OpenShotRenderer"


def test_choose_renderer_raises_when_no_backend_available(monkeypatch, tmp_path):
    caps = {
        "openshot": {"available": False, "preferred": True, "reason": "missing"},
        "ffmpeg": {"available": False, "preferred": False, "reason": "ffmpeg/ffprobe missing"},
    }
    monkeypatch.setattr(editor_render, "detect_render_backends", lambda: caps)

    with pytest.raises(editor_render.RenderError) as excinfo:
        editor_render.choose_renderer(tmp_path / "renders")

    # The error embeds the capability report so ops can see *why* nothing ran.
    assert "no supported renderer available" in str(excinfo.value)
    assert "ffmpeg/ffprobe missing" in str(excinfo.value)


# --- ZMQ debug-logger port-bind livelock guard (editor_render.py L825-833) ---------
#
# libopenshot's ZmqLogger singleton binds TCP 5556 the first time a render runs.
# With multiple in-process renders (the test suite, prod-cycle gates) a later render
# contends on that port and blocks forever. `_import_openshot` must disable the logger
# on every import so the port is never bound. These tests inject a fake `openshot`
# module so the behaviour is verifiable on hosts without libopenshot installed.


def _install_fake_openshot(monkeypatch, *, with_logger=True, raise_on_enable=False):
    """Make `import openshot` resolve to a tracking double; return the double."""
    import importlib.machinery
    import types

    enable_calls: list[bool] = []

    fake = types.ModuleType("openshot")
    fake.__file__ = "/fake/openshot/__init__.py"
    # importlib.util.find_spec consults __spec__ for a module already in sys.modules.
    fake.__spec__ = importlib.machinery.ModuleSpec("openshot", loader=None, origin=fake.__file__)
    fake.enable_calls = enable_calls  # type: ignore[attr-defined]

    if with_logger:
        class _Logger:
            def Enable(self, flag):
                if raise_on_enable:
                    raise RuntimeError("logger API absent on this build")
                enable_calls.append(flag)

        _instance = _Logger()

        class _ZmqLogger:
            @staticmethod
            def Instance():
                return _instance

        fake.ZmqLogger = _ZmqLogger  # type: ignore[attr-defined]

    # `_import_openshot` uses importlib.util.find_spec; a module in sys.modules
    # is found and returned without touching sys.path or the real bindings.
    monkeypatch.setitem(sys.modules, "openshot", fake)
    # Keep candidate path probing inert so no real install gets onto sys.path.
    monkeypatch.setattr(editor_render, "_openshot_python_candidates", lambda: [])
    return fake


def test_import_openshot_disables_zmq_logger_to_prevent_port_bind(monkeypatch):
    monkeypatch.setattr(editor_render, "_zmq_logger_disabled", False)
    fake = _install_fake_openshot(monkeypatch)

    module, reason, _path = editor_render._import_openshot()

    assert module is fake
    assert reason == "available"
    # The logger was disabled exactly once with False — the 5556 bind never happens.
    assert fake.enable_calls == [False]


def test_import_openshot_disables_logger_once_across_parallel_renders(monkeypatch):
    # The ZMQ logger is a process-wide singleton: a single Enable(False) kills the
    # 5556 bind for the whole process. Re-touching the singleton on every import
    # is exactly the contention that hung parallel renders, so N imports must
    # produce exactly ONE disable, not N.
    monkeypatch.setattr(editor_render, "_zmq_logger_disabled", False)
    fake = _install_fake_openshot(monkeypatch)

    for _ in range(5):
        module, reason, _ = editor_render._import_openshot()
        assert module is fake
        assert reason == "available"

    assert fake.enable_calls == [False]


def test_import_openshot_survives_builds_without_zmq_logger(monkeypatch):
    # Older libopenshot builds lack ZmqLogger entirely. Import must still succeed
    # (the bind never happens there anyway) and never raise.
    monkeypatch.setattr(editor_render, "_zmq_logger_disabled", False)
    fake = _install_fake_openshot(monkeypatch, with_logger=False)
    assert not hasattr(fake, "ZmqLogger")

    module, reason, _ = editor_render._import_openshot()

    assert module is fake
    assert reason == "available"


def test_import_openshot_swallows_zmq_logger_enable_errors(monkeypatch):
    # If Enable() throws on some build, the import must not blow up the render.
    monkeypatch.setattr(editor_render, "_zmq_logger_disabled", False)
    fake = _install_fake_openshot(monkeypatch, raise_on_enable=True)

    module, reason, _ = editor_render._import_openshot()

    assert module is fake
    assert reason == "available"
    assert fake.enable_calls == []  # the throwing call recorded nothing


def test_import_openshot_degrades_when_find_spec_raises(monkeypatch):
    # A half-loaded native-extension install (the "dylibs wiped, bindings present"
    # OpenShot-runtime-fragility failure) can leave `openshot` in sys.modules with
    # `__spec__ = None`. importlib.util.find_spec("openshot") then RAISES
    # ValueError("openshot.__spec__ is None") instead of returning None. That must
    # NOT escape _import_openshot (it would crash choose_renderer and skip the
    # ffmpeg fallback entirely); it must degrade to "not importable" so the
    # renderer falls back. Pin that contract.
    import types

    broken = types.ModuleType("openshot")
    broken.__spec__ = None  # the trap importlib.util.find_spec trips over
    monkeypatch.setitem(sys.modules, "openshot", broken)
    monkeypatch.setattr(editor_render, "_openshot_python_candidates", lambda: [])

    module, reason, path = editor_render._import_openshot()

    assert module is None
    assert path is None
    assert "not importable" in reason


def test_find_spec_exception_surfaces_as_ffmpeg_fallback_through_choose_renderer(
    monkeypatch, tmp_path
):
    # End-to-end companion to the unit test above: when importlib.util.find_spec
    # itself RAISES (not the __spec__-is-None ValueError, but any native-extension
    # discovery blow-up), the exception must be caught in _import_openshot and the
    # WHOLE stack must degrade to ffmpeg — detect_render_backends reports openshot
    # unavailable with the exception text as its reason, and choose_renderer returns
    # the ffmpeg backend stamped as a downgrade carrying that reason. Without the
    # try/except at the find_spec call this raises out of choose_renderer and there
    # is no render at all. Pin the surfaced path, not just the helper return tuple.
    monkeypatch.setattr(editor_render, "_openshot_python_candidates", lambda: [])

    def _boom(name):
        raise OSError("libopenshot.dylib: image not found")

    monkeypatch.setattr(editor_render.importlib.util, "find_spec", _boom)

    caps = editor_render.detect_render_backends()
    assert caps["openshot"]["available"] is False
    assert "image not found" in caps["openshot"]["reason"]

    renderer = editor_render.choose_renderer(tmp_path / "renders")

    assert renderer.backend == "ffmpeg"
    assert renderer._inner.__class__.__name__ == "FFmpegLayeredRenderer"
    health = renderer._health
    assert health["downgraded"] is True
    assert health["selected"] == "ffmpeg"
    assert health["preferred"] == "openshot"
    assert "image not found" in health["reason"]


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


def test_openshot_subprocess_does_not_salvage_truncated_zero_byte_artifact(monkeypatch, tmp_path):
    """editor_render.OpenShotSubprocessRenderer._run_child salvage branch (385-390):
    a non-zero child exit that leaves a *present but zero-byte* artifact behind
    (process killed mid-write) is NOT a salvageable render. The salvage gate
    (`_render_result_artifact_exists`) must reject the truncated turd so the
    caller raises a hard RenderError instead of shipping a corrupt 0-byte mp4
    as if it were a finished clip."""
    output = tmp_path / "renders" / "window.mp4"
    output.parent.mkdir()
    output.write_bytes(b"")  # truncated: file exists, but is empty/corrupt

    class Completed:
        returncode = -9  # SIGKILL mid-write
        stdout = json.dumps({
            "backend": "openshot",
            "video": str(output),
            "start": 0,
            "duration": 1,
            "missingAssets": [],
        })
        stderr = "child killed after creating but before filling output"

    monkeypatch.setattr(editor_render.subprocess, "run", lambda *a, **k: Completed())

    renderer = editor_render.OpenShotSubprocessRenderer(tmp_path / "renders")
    with pytest.raises(editor_render.RenderError) as excinfo:
        renderer.render({"projectId": "p"}, output_path=output, start=0, duration=1)
    assert "OpenShot subprocess failed (-9)" in str(excinfo.value)
    assert "child killed" in str(excinfo.value)


def test_render_result_artifact_exists_rejects_empty_and_missing(tmp_path):
    """The salvage gate distinguishes a usable artifact (present, non-empty) from
    a corrupt one (missing, or present-but-zero-byte)."""
    good = tmp_path / "good.mp4"
    good.write_bytes(b"rendered")
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")

    assert editor_render._render_result_artifact_exists({"video": str(good)}) is True
    assert editor_render._render_result_artifact_exists({"frame": str(good)}) is True
    assert editor_render._render_result_artifact_exists({"video": str(empty)}) is False
    assert editor_render._render_result_artifact_exists({"video": str(tmp_path / "nope.mp4")}) is False
    assert editor_render._render_result_artifact_exists({}) is False


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


def test_audio_fade_in_window_treats_crossfade_as_fadein():
    """An audio crossfade is a start-anchored fade-in (openshot_bridge maps both
    fadeIn and crossfade to an `in` Fade), so _audio_fade_in_window must surface a
    crossfade's duration even when the clip carries no explicit fadeIn. The longest
    of the two wins when both are present; no fade -> None."""
    assert editor_render._audio_fade_in_window({"effects": []}) is None
    crossfade_only = {
        "effects": [{"id": "xf", "type": "crossfade", "params": {"duration": 0.7}}]
    }
    assert editor_render._audio_fade_in_window(crossfade_only) == pytest.approx(0.7)
    both = {
        "effects": [
            {"id": "fi", "type": "fadeIn", "params": {"duration": 0.3}},
            {"id": "xf", "type": "crossfade", "params": {"duration": 0.7}},
        ]
    }
    assert editor_render._audio_fade_in_window(both) == pytest.approx(0.7)


def test_build_video_command_audio_crossfade_emits_afade_and_is_not_warned(tmp_path):
    """An audio clip's crossfade must reproduce as an `afade=t=in` in the ffmpeg
    fallback (parity with OpenShot's `in` Fade) AND must NOT be reported as a
    dropped effect, because the fallback now honors it. Pre-fix the afade chain
    only looked at fadeIn, so audio crossfades vanished silently."""
    red = _solid_png(tmp_path / "red.png", "red")
    voice = tmp_path / "vo.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-ac", "1", "-ar", "48000", str(voice)],
        check=True, capture_output=True, text=True,
    )
    project = _project_with_card("cmd_xfade", red, x=0.0)
    project["assets"]["vo"] = {"id": "vo", "type": "audio", "src": str(voice)}
    project["clips"]["vo_clip"] = {
        "id": "vo_clip", "assetId": "vo", "trackId": "audio_1", "kind": "voiceover",
        "start": 0.0, "duration": 1.0, "sourceStart": 0.0,
        "enabled": True, "muted": False, "volume": 1.0, "transform": {},
        "effects": [{"id": "xf", "type": "crossfade", "params": {"duration": 0.5}}],
    }
    r = _renderer(tmp_path)
    cmd, missing, warnings = r._build_video_command(
        project, tmp_path / "out.mp4", duration=1.0, window_start=0.0
    )
    assert missing == []
    filter_arg = cmd[cmd.index("-filter_complex") + 1]
    assert "afade=t=in:st=0:d=0.500" in filter_arg
    # the audio crossfade is reproduced, so it is NOT a dropped-effect warning
    assert not any(w["name"] == "crossfade" for w in warnings)


def test_open_shot_audio_mux_applies_audio_crossfade_as_fadein(tmp_path):
    """The OpenShot mux path must honor an audio crossfade as a fade-in, exactly
    like fadeIn. Pre-fix only fadeIn rode through, so an interclip ducking crossfade
    on audio was silently lost vs OpenShot. The ramped head is audibly quieter than
    the steady body."""
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
    project = timeline.new_project("audio_xfade", width=96, height=64, fps=12)
    project["assets"]["tone"] = {"id": "tone", "type": "audio", "src": str(tone)}
    project["clips"]["tone"] = {
        "id": "tone", "assetId": "tone", "trackId": "audio_1", "kind": "music",
        "start": 0, "duration": 2.0, "sourceStart": 0, "enabled": True,
        "muted": False, "volume": 1, "transform": {},
        "effects": [{"id": "xf", "type": "crossfade", "params": {"duration": 1.0}}],
    }

    assert editor_render._mux_timeline_audio(project, video, duration=2.0, asset_root=None) is True

    head = _mean_volume(video, start=0.0, duration=0.25)
    body = _mean_volume(video, start=1.2, duration=0.4)
    assert head < body - 6  # crossfade ramp keeps the head well below steady level


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


def test_open_shot_audio_mux_raises_on_audio_asset_symlink_target_deleted(tmp_path, monkeypatch):
    """Concurrent asset cleanup race: an audio clip's src is a SYMLINK whose target
    is unlinked between project validation (_missing_assets) and the mux's own guard
    (editor_render.py:923). A broken symlink is the realistic shape of a mid-render
    GC — the link node survives, the data is gone. Path.exists() follows the link, so
    it correctly returns False for a dangling target, and the guard must fail closed
    with the missing-asset error BEFORE ffmpeg is invoked (a phantom -i would make
    ffmpeg fail late with an opaque filtergraph error instead). subprocess.run is
    monkeypatched to a tripwire so reaching ffmpeg is itself a test failure — proving
    the guard short-circuits the whole graph build. Also pins that the source mp4 is
    left byte-identical (no .audio-mux temp swapped in)."""
    video = tmp_path / "silent_video.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=96x64:r=12:d=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
        check=True, capture_output=True, text=True,
    )
    original = video.read_bytes()

    # Asset exists at validation time, then its symlink target is GC'd mid-render.
    real_audio = tmp_path / "tone.wav"
    real_audio.write_bytes(b"")  # only .exists() is checked before ffmpeg
    link = tmp_path / "tone-link.wav"
    link.symlink_to(real_audio)
    assert link.exists()  # validation would have passed: link resolves
    real_audio.unlink()   # concurrent cleanup deletes the target
    assert link.is_symlink() and not link.exists()  # dangling: link node, no data

    project = timeline.new_project("audio_mux_dangling", width=96, height=64, fps=12)
    project["assets"]["tone"] = {"id": "tone", "type": "audio", "src": str(link)}
    project["clips"]["tone"] = {
        "id": "tone", "assetId": "tone", "trackId": "audio_1", "kind": "music",
        "start": 0.0, "duration": 1.0, "sourceStart": 0.0, "enabled": True,
        "muted": False, "volume": 1.0, "transform": {},
    }

    def _tripwire(*args, **kwargs):
        raise AssertionError("ffmpeg must NOT run when an audio asset symlink is dangling")

    monkeypatch.setattr(editor_render.subprocess, "run", _tripwire)

    with pytest.raises(editor_render.RenderError) as excinfo:
        editor_render._mux_timeline_audio(project, video, duration=2.0, asset_root=None)
    assert "audio mux blocked by missing asset" in str(excinfo.value)
    assert "tone-link.wav" in str(excinfo.value)
    # fail-closed: source untouched, no temp swap.
    assert video.read_bytes() == original
    assert not video.with_name("silent_video.audio-mux.mp4").exists()


def test_open_shot_audio_mux_raises_render_error_on_corrupt_audio_real_ffmpeg(tmp_path):
    """End-to-end fail-closed proof against REAL ffmpeg (not a mocked subprocess):
    an audio asset that exists on disk but is undecodable garbage passes the
    .exists() guard, then makes the filter_complex/atrim graph fail at decode time.
    The non-zero returncode branch (editor_render.py:855) must surface ffmpeg's
    stderr as RenderError, leave the original mp4 byte-identical, and never swap in
    the .audio-mux temp. This is the edge case the mocked timeout/returncode tests
    can't cover: that the live ffmpeg invocation actually exits non-zero here."""
    video = tmp_path / "silent_video.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=96x64:r=12:d=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
        check=True, capture_output=True, text=True,
    )
    original = video.read_bytes()

    corrupt = tmp_path / "corrupt.wav"  # exists, but not real audio
    corrupt.write_bytes(b"this is not audio data at all, only garbage bytes")
    project = timeline.new_project("audio_mux_corrupt", width=96, height=64, fps=12)
    project["assets"]["bad"] = {"id": "bad", "type": "audio", "src": str(corrupt)}
    project["clips"]["bad"] = {
        "id": "bad", "assetId": "bad", "trackId": "audio_1", "kind": "voiceover",
        "start": 0.0, "duration": 1.0, "sourceStart": 0.0, "enabled": True,
        "muted": False, "volume": 1.0, "transform": {},
    }

    with pytest.raises(editor_render.RenderError):
        editor_render._mux_timeline_audio(project, video, duration=2.0, asset_root=None)
    # source untouched + no temp swap left behind on the real-ffmpeg failure path.
    assert video.read_bytes() == original
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


def test_volume_envelope_is_absolute_and_not_scaled_by_flat_gain():
    """A volume envelope's points are ABSOLUTE gains and must pass through the
    ffmpeg filter unscaled — exactly as openshot_bridge OVERWRITES the flat volume
    key with the envelope. If editor_render multiplied the envelope by the clip's
    flat `volume` (the documented double-attenuation bug) the two render backends
    would diverge whenever a bed kept a non-1.0 flat gain beside an envelope.

    editor_timeline neutralizes the flat gain to 1.0 on import, but this pins the
    contract directly so the carryover is robust even if a caller forgets to.

    Concrete carryover: intro gain 0.6, ducked 0.22. The OpenShot path animates
    volume to 60.0% / 22.0%; the ffmpeg expr must encode 0.6 / 0.22 verbatim, NOT
    0.6*flat / 0.22*flat."""
    ducked = {
        "volume": 0.22,  # legacy flat gain left in place ALONGSIDE the envelope
        "keyframes": [
            {
                "property": "volume",
                "points": [
                    {"t": 0.0, "value": 0.6},
                    {"t": 1.0, "value": 0.22},
                ],
            }
        ],
    }
    # Pass the clip's real flat gain (0.22) as base_volume — mirrors the call site
    # `_volume_filter(clip, float(clip.get("volume")))`.
    expr = editor_render._volume_filter(ducked, 0.22)
    # The absolute envelope values appear verbatim; the flat 0.22 must NOT prefix
    # the expression as a scale factor (that would be `(0.2200)*(...)`).
    assert "0.6000" in expr and "0.2200" in expr
    assert "(0.2200)*(" not in expr, "flat gain double-attenuated the envelope"

    # Backend parity: the OpenShot bridge maps the same envelope to its absolute
    # percent gains (0.6 -> 60.0, 0.22 -> 22.0), independent of the flat volume.
    overrides = openshot_bridge._keyframe_overrides(ducked["keyframes"], fps=30)
    ys = [round(p["co"]["Y"], 4) for p in overrides["volume"]["Points"]]
    assert ys == [60.0, 22.0]


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


def test_piecewise_expr_honors_bezier_and_constant_interp_per_segment():
    """The ffmpeg fallback's piecewise builder must emit a DIFFERENT segment shape
    per interp, not silently linearize everything (the bug: bezier ducking curves
    dropped to linear in mux renders). bezier -> smoothstep (3s^2-2s^3), constant ->
    a held step, linear -> a slope ramp. A 2-tuple point stays linear for old callers."""
    pts = [(0.0, 0.0, "bezier"), (1.0, 1.0, "linear"), (2.0, 1.0, "constant")]
    expr = editor_render._piecewise_linear_expr(pts)
    # segment 0->1 carries the END point's interp (linear) -> a slope ramp
    assert "0.0000+(1.000000)*(t-0.0000)" in expr
    # segment 1->2 carries 'constant' -> holds the previous value, no ramp term
    assert "if(lt(t,2.0000),1.0000," in expr
    # a leading bezier point shapes the segment INTO the next point that owns it:
    bez = editor_render._piecewise_linear_expr([(0.0, 0.0, "linear"), (1.0, 1.0, "bezier")])
    assert "3*" in bez and "-2*" in bez  # smoothstep, not a bare linear slope
    assert "(1.000000)*(t-0.0000)" not in bez  # NOT the linear ramp form
    # back-compat: 2-tuples (no interp) still produce the linear ramp
    lin = editor_render._piecewise_linear_expr([(0.0, 0.0), (1.0, 1.0)])
    assert "0.0000+(1.000000)*(t-0.0000)" in lin and "3*" not in lin


def test_mux_renders_bezier_ducking_smoother_than_constant_step(tmp_path):
    """End-to-end ticket guard: a music-bed volume envelope whose midpoint uses
    `interp: bezier` must render through the actual ffmpeg mux subprocess as a
    SMOOTH curve, not the silent-linear drop and not a hard step. We compare the
    mid-transition loudness of a bezier duck against a `constant` duck on identical
    points: the constant step holds the loud value until t1, while the bezier eases
    down before it, so at the transition window the bezier render is measurably
    quieter. This proves interp survives the bridge<->mux seam into a real render."""
    tone = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=4.0",
         "-ac", "1", "-ar", "48000", str(tone)],
        check=True, capture_output=True, text=True,
    )

    def _render(interp: str) -> Path:
        video = tmp_path / f"silent_{interp}.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=96x64:r=12:d=4",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
            check=True, capture_output=True, text=True,
        )
        project = timeline.new_project(f"duck_{interp}", width=96, height=64, fps=12)
        project["assets"]["tone"] = {"id": "tone", "type": "audio", "src": str(tone)}
        project["clips"]["tone"] = {
            "id": "tone", "assetId": "tone", "trackId": "audio_1", "kind": "music",
            "start": 0, "duration": 4.0, "sourceStart": 0, "enabled": True,
            "muted": False, "volume": 1, "transform": {},
            "keyframes": [
                {"property": "volume", "points": [
                    {"t": 0.0, "value": 1.0, "interp": "linear"},
                    # midpoint owns the t0->t1 transition; bezier eases, constant steps.
                    {"t": 2.0, "value": 0.02, "interp": interp},
                    {"t": 4.0, "value": 0.02, "interp": "linear"},
                ]},
            ],
        }
        assert editor_render._mux_timeline_audio(project, video, duration=4.0, asset_root=None) is True
        return video

    bezier_v = _render("bezier")
    constant_v = _render("constant")

    # In the first half of the transition (t in [0.2,1.6]) the constant step is
    # still pinned at full loud, while the bezier curve has already begun easing
    # down. The bezier render is therefore audibly quieter across that window.
    window = dict(start=0.2, duration=1.4)
    assert _mean_volume(bezier_v, **window) < _mean_volume(constant_v, **window) - 3


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


def test_split_then_window_keeps_volume_envelope_aligned(tmp_path):
    """The two keyframe-rebasing passes must compose: clip.split rebases the tail
    envelope to its new t=0 (timeline lines 525-526), then _windowed_clips front-trims
    and shifts it again (render line 918). A volume ducking envelope on a bed clip must
    survive both and stay sample-accurate, not double-shift or get dropped."""
    project = timeline.new_project("split_win", width=96, height=64, fps=12)
    project["assets"]["bed"] = {"id": "bed", "type": "audio", "src": "/x/bed.mp3"}
    project["clips"]["bed_clip"] = {
        "id": "bed_clip", "assetId": "bed", "trackId": "music_1", "kind": "music_bed",
        "start": 0.0, "duration": 10.0, "sourceStart": 0.0,
        "enabled": True, "muted": False, "volume": 1.0,
        "transform": {"x": 0.5, "y": 0.5, "scale": 1.0, "opacity": 1.0}, "effects": [],
        "keyframes": [{"property": "volume", "points": [
            {"t": 0.0, "value": 0.6, "interp": "linear"},
            {"t": 4.0, "value": 0.18, "interp": "linear"},  # duck under VO
            {"t": 8.0, "value": 0.6, "interp": "linear"},   # back up
        ]}],
    }

    # Split at timeline t=4.0: tail clip is rebased so its envelope starts at t=0.
    split = timeline.apply_command(project, {
        "op": "clip.split", "actor": "t", "expectedRevision": 0,
        "payload": {"clipId": "bed_clip", "at": 4.0, "newClipId": "bed_clip_b"},
    })
    tail = split["clips"]["bed_clip_b"]
    tail_pts = tail["keyframes"][0]["points"]
    # Tail rebased: t=4 -> 0 (the duck floor), t=8 -> 4 (back up).
    assert [p["t"] for p in tail_pts] == [0.0, 4.0]
    assert tail_pts[0]["value"] == 0.18

    # Now render-window the tail starting 1s in (window timeline t=5..9 -> front_trim 1s
    # off the tail, which itself starts at timeline t=4). _windowed_clips must shift the
    # already-rebased tail envelope down by another 1s.
    windowed = editor_render._windowed_clips(split, window_start=5.0, duration=4.0)
    wbed = next(c for c in windowed if c["id"] == "bed_clip_b")
    wpts = wbed["keyframes"][0]["points"]
    assert wpts[0]["t"] == pytest.approx(0.0)  # 0.0 - 1.0 clamped to 0
    assert wpts[1]["t"] == pytest.approx(3.0)  # 4.0 - 1.0
    assert wpts[0]["value"] == 0.18 and wpts[1]["value"] == 0.6
    # The ffmpeg mux path reads this same envelope; it must build a keyframe expression,
    # not the flat fallback, proving the envelope survived both passes intact.
    assert "if(" in editor_render._volume_filter(wbed, 1.0)


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

    # A front trim larger than the fade wipes the start-anchored fades entirely: they
    # re-fit to <= 0 and are DROPPED, not kept as `duration: 0.0` no-op fades that
    # OpenShot's Fade silently ignores (the ticket's 0s-boundary contract breach).
    gone = editor_render._windowed_clips(project, window_start=2.0, duration=2.0)
    gone_effects = {e["type"]: e for e in gone[0]["effects"]}
    assert "fadeIn" not in gone_effects
    assert "crossfade" not in gone_effects

    # Original project clip effects must be untouched (no aliasing of nested params).
    orig = project["clips"]["card_clip"]["effects"]
    assert orig[0]["params"]["duration"] == pytest.approx(1.0)
    assert orig[1]["params"]["duration"] == pytest.approx(1.0)


def test_windowed_crossfade_clip_trimmed_on_both_ends_refits_and_exports():
    """A clip that STARTS BEFORE the window AND EXTENDS BEYOND it is trimmed on BOTH
    ends within a single windowed render (front_trim > 0 AND back-truncated). The
    ticket's exact case: window start=2.0, duration=3.0 over a clip spanning t=1..7.

    front_trim = 2 - 1 = 1.0s shortens the start-anchored crossfade; the clip is also
    truncated at the window end (t=5), so windowed_duration = 3.0 < the original 6.0.
    Both adjustments must apply at once: the crossfade re-fits to (orig - front_trim)
    then clamps to the window, sourceStart shifts by front_trim, start lands at 0
    (relative to the window), and the whole thing exports to a single OpenShot in-Fade.
    No prior test exercises crossfade + trim-on-BOTH-ends in one window."""
    project = timeline.new_project("xf_both_ends", width=64, height=48, fps=10)
    project["assets"]["a1"] = {"id": "a1", "type": "video", "src": "/x.mp4", "metadata": {}}
    project["clips"]["c1"] = {
        "id": "c1", "assetId": "a1", "trackId": "video_1", "kind": "video",
        "start": 1.0, "duration": 6.0, "sourceStart": 4.0,  # clip spans timeline t=1..7
        "enabled": True, "muted": False, "volume": 1.0,
        "transform": {"x": 0.5, "y": 0.5, "scale": 1.0, "opacity": 1.0},
        "effects": [{"id": "xf", "type": "crossfade", "params": {"duration": 2.0}}],
        "keyframes": [], "metadata": {},
    }

    # Window timeline t=2..5: front_trim = 2 - 1 = 1.0s, windowed_duration = 3.0s.
    windowed = editor_render._windowed_clips(project, window_start=2.0, duration=3.0)
    assert len(windowed) == 1
    wclip = windowed[0]

    # Trimmed on both ends: start is window-relative (overlap_start 2.0 - window 2.0 = 0),
    # duration is the in-window overlap (min(7,5) - max(1,2) = 3.0), sourceStart advances
    # by the front_trim so the same source frame plays.
    assert wclip["start"] == pytest.approx(0.0)
    assert wclip["duration"] == pytest.approx(3.0)
    assert wclip["sourceStart"] == pytest.approx(5.0)  # 4.0 + 1.0 front_trim

    # The start-anchored crossfade is shortened by the front_trim (2.0 - 1.0 = 1.0),
    # which is <= the 3.0 window so the back-trim clamp leaves it at 1.0 (proves the
    # front-trim path fired even though the clip is ALSO back-truncated).
    xf = next(e for e in wclip["effects"] if e["id"] == "xf")
    assert xf["params"]["duration"] == pytest.approx(1.0)

    # The doubly-trimmed clip still exports to exactly one OpenShot in-Fade carrying the
    # re-fit duration -- timeline state and compiled video agree on the fade.
    clip_json = openshot_bridge.clip_json(project, wclip)
    fades = [e for e in clip_json["effects"] if e["type"] == "Fade"]
    assert len(fades) == 1
    assert fades[0]["fade"] == "in"
    assert fades[0]["duration"]["Points"][0]["co"]["Y"] == pytest.approx(1.0)

    # Original clip effect untouched (no nested-param aliasing).
    assert project["clips"]["c1"]["effects"][0]["params"]["duration"] == pytest.approx(2.0)


def test_split_clip_crossfade_refits_through_render_window_into_openshot_fade():
    """End-to-end carry-through: a clip with a start-anchored crossfade is SPLIT, then
    the surviving-crossfade HEAD is windowed during render (front-trimmed), then exported
    to OpenShot. The crossfade must re-fit to the front-trim AND land as a single OpenShot
    Fade-`in` effect whose duration keyframe carries the re-fit seconds. This is the
    split -> windowed-render -> OpenShot-export path the ticket flags as untested; a
    regression here means the timeline state and the compiled video disagree on the fade.
    """
    project = timeline.new_project("xf_carry", width=64, height=48, fps=10)
    project["assets"]["a1"] = {"id": "a1", "type": "video", "src": "/x.mp4", "metadata": {}}
    project["clips"]["c1"] = {
        "id": "c1", "assetId": "a1", "trackId": "video_1", "kind": "video",
        "start": 0.0, "duration": 10.0, "sourceStart": 0.0,
        "enabled": True, "muted": False, "volume": 1.0,
        "transform": {"x": 0.5, "y": 0.5, "scale": 1.0, "opacity": 1.0},
        "effects": [{"id": "xf", "type": "crossfade", "params": {"duration": 3.0}}],
        "keyframes": [], "metadata": {},
    }

    # Split at t=8 -> head [0,8) keeps the 3s start-anchored crossfade at full duration.
    # The tail's 8s-trimmed front fully eats that start-anchored crossfade (3.0 - 8.0 < 0),
    # so the tail DROPS it (matching editor_render._refit_effects) rather than carrying a
    # `duration: 0.0` no-op fade that OpenShot's Fade silently ignores — keeping the
    # timeline state and the compiled video in agreement at the 0s boundary.
    split = timeline.apply_command(
        project,
        {
            "op": "clip.split", "actor": "human", "expectedRevision": 0,
            "payload": {"clipId": "c1", "at": 8.0, "newClipId": "c1_tail"},
        },
    )
    assert [e["type"] for e in split["clips"]["c1"]["effects"]] == ["crossfade"]
    assert split["clips"]["c1_tail"]["effects"] == []

    # Render-window the head with a 2s front trim (window t=2..6). The start-anchored
    # crossfade must shrink by 2s: 3.0 - 2.0 = 1.0s, clamped to the 4s window.
    scoped = editor_render._render_scope_project(split, window_start=2.0, duration=4.0)
    head_scoped = scoped["clips"]["c1"]
    xf = next(e for e in head_scoped["effects"] if e["id"] == "xf")
    assert xf["params"]["duration"] == pytest.approx(1.0)

    # Export the windowed head to OpenShot: ONE Fade-`in` effect carrying the re-fit
    # duration as a single keyframe Point (no duplicate, correct direction + value).
    clip_json = openshot_bridge.clip_json(scoped, head_scoped)
    fades = [e for e in clip_json["effects"] if e["type"] == "Fade"]
    assert len(fades) == 1
    assert fades[0]["fade"] == "in"  # crossfade -> OpenShot `in` Fade
    assert fades[0]["duration"]["Points"][0]["co"]["Y"] == pytest.approx(1.0)


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


def test_open_shot_audio_mux_truncates_stderr_tail_to_1200_chars(tmp_path, monkeypatch):
    """services/editor_render.py:1032 — a verbose ffmpeg failure must not dump its
    entire stderr into the RenderError; only the last 1200 chars survive. A real
    ffmpeg can emit megabytes of filtergraph spew on failure, so the tail slice is
    the bound that keeps the error (and the logs/UI carrying it) sane."""
    project = _single_audio_clip_project(tmp_path)
    video = tmp_path / "silent_video.mp4"
    video.write_bytes(b"original-bytes")

    monkeypatch.setattr(editor_render.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    head = "X" * 5000          # noise that must be dropped
    tail = "REAL_TAIL_" * 100  # 1000 chars; the meaningful end of the message
    full_stderr = head + tail

    class _Verbose:
        returncode = 1
        stderr = full_stderr
        stdout = ""

    monkeypatch.setattr(editor_render.subprocess, "run", lambda *a, **k: _Verbose())

    with pytest.raises(editor_render.RenderError) as excinfo:
        editor_render._mux_timeline_audio(project, video, duration=1.0, asset_root=None)
    msg = str(excinfo.value)
    assert len(msg) == 1200                       # exactly the tail slice, not the whole 6000
    assert msg == full_stderr[-1200:]             # and it is the END of the spew (the real error)
    assert "REAL_TAIL_" in msg                    # the meaningful tail survived
    assert video.read_bytes() == b"original-bytes"


def test_open_shot_audio_mux_falls_back_to_stdout_when_stderr_empty(tmp_path, monkeypatch):
    """services/editor_render.py:1032 — when ffmpeg fails with an empty stderr the
    error must surface the stdout tail instead, so a non-zero exit never yields a
    blank, un-actionable RenderError."""
    project = _single_audio_clip_project(tmp_path)
    video = tmp_path / "silent_video.mp4"
    video.write_bytes(b"original-bytes")

    monkeypatch.setattr(editor_render.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    class _StdoutOnly:
        returncode = 1
        stderr = ""
        stdout = "diagnostic on stdout: muxer refused the stream"

    monkeypatch.setattr(editor_render.subprocess, "run", lambda *a, **k: _StdoutOnly())

    with pytest.raises(editor_render.RenderError) as excinfo:
        editor_render._mux_timeline_audio(project, video, duration=1.0, asset_root=None)
    assert "muxer refused the stream" in str(excinfo.value)


def test_open_shot_audio_mux_cleans_up_partial_temp_on_failure(tmp_path, monkeypatch):
    """services/editor_render.py — a mux that fails mid-operation must not leave its
    `.audio-mux` partial file behind. Simulate ffmpeg writing a half-baked temp
    output and then exiting non-zero; the finally-cleanup must remove the turd while
    leaving the real source untouched."""
    project = _single_audio_clip_project(tmp_path)
    video = tmp_path / "silent_video.mp4"
    video.write_bytes(b"original-bytes")
    temp_output = video.with_name(f"{video.stem}.audio-mux{video.suffix}")

    monkeypatch.setattr(editor_render.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    class _FailedAfterPartialWrite:
        returncode = 1
        stderr = "ffmpeg: write failed near EOF"
        stdout = ""

    def _run(*a, **k):
        # ffmpeg writes a partial temp file, then fails (the real failure mode).
        temp_output.write_bytes(b"half-muxed-garbage")
        return _FailedAfterPartialWrite()

    monkeypatch.setattr(editor_render.subprocess, "run", _run)

    with pytest.raises(editor_render.RenderError):
        editor_render._mux_timeline_audio(project, video, duration=1.0, asset_root=None)
    assert not temp_output.exists()               # partial turd cleaned up
    assert video.read_bytes() == b"original-bytes"  # source never clobbered


def test_open_shot_audio_mux_cleans_up_partial_temp_on_timeout(tmp_path, monkeypatch):
    """services/editor_render.py — same cleanup guarantee when the mux times out:
    a partial `.audio-mux` file from a killed ffmpeg must not survive the
    TimeoutExpired -> RenderError translation."""
    project = _single_audio_clip_project(tmp_path)
    video = tmp_path / "silent_video.mp4"
    video.write_bytes(b"original-bytes")
    temp_output = video.with_name(f"{video.stem}.audio-mux{video.suffix}")

    monkeypatch.setattr(editor_render.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    def _timeout(*a, **k):
        temp_output.write_bytes(b"partial-before-kill")
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=900)

    monkeypatch.setattr(editor_render.subprocess, "run", _timeout)

    with pytest.raises(editor_render.RenderError) as excinfo:
        editor_render._mux_timeline_audio(project, video, duration=1.0, asset_root=None)
    assert "timed out" in str(excinfo.value)
    assert not temp_output.exists()
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


def test_openshot_render_frame_window_shift_preserves_keyframe_source_frames(tmp_path, monkeypatch):
    """The ticket's core gap: OpenShotRenderer.render_frame uses a hard-coded 250ms
    window via _render_scope_project, which front-trims and so runs BOTH the
    keyframe-t shift (down) and the sourceStart bump (up). Those two must CANCEL in
    OpenShot frame space (X = (sourceStart + t)*fps + 1), so a keyframe lands at the
    SAME source frame whether the preview window starts at the clip's head or mid-clip.
    If they don't cancel, a preview frame shows opacity/volume/position envelopes
    firing at the wrong time vs the full OpenShot render. No prior test drove the real
    render_frame entrypoint (it patches _timeline directly to capture the JSON the
    method actually feeds libopenshot)."""
    fake = _fake_openshot()
    monkeypatch.setattr(editor_render, "_import_openshot", lambda: (fake, "available", None))
    renderer = editor_render.OpenShotRenderer(tmp_path / "renders")

    captured: dict[str, dict] = {}

    class _CapturedFrame:
        def Save(self, *_a, **_k):
            pass

    class _CapturingTimeline:
        def GetFrame(self, _n):
            return _CapturedFrame()

        def Close(self):
            pass

    def _capture_timeline(render_project):
        # This is exactly what the real _timeline serializes into libopenshot.
        captured["clip"] = editor_render.openshot_bridge.timeline_json(render_project)["clips"][0]
        return _CapturingTimeline()

    monkeypatch.setattr(renderer, "_timeline", _capture_timeline)

    # Clip spans timeline t=4..7, fps=30, sourceStart=1.0, with volume + opacity +
    # x envelopes. A volume point at t=1.0 sits at absolute timeline t=5.0.
    project = _full_envelope_project(source_start=1.0)
    bed = tmp_path / "bed.wav"
    bed.write_bytes(b"")  # only .exists() is checked by render_frame's missing-asset gate
    project["assets"]["bed"]["src"] = str(bed)
    project["clips"]["c1"]["keyframes"].append(
        {"property": "opacity", "points": [
            {"t": 0.0, "value": 0.0, "interp": "linear"},
            {"t": 1.0, "value": 1.0, "interp": "linear"},
        ]}
    )

    # Source frame X each volume keyframe lands at when previewing from the clip head
    # (no front-trim): X = (1.0 + t)*30 + 1 for t in [0,1,3].
    head = editor_render.openshot_bridge.timeline_json(project)["clips"][0]
    head_vol_x = [p["co"]["X"] for p in head["volume"]["Points"]]
    assert head_vol_x == [31.0, 61.0, 121.0]

    # Preview a frame at t=5.0 -> render_frame windows t=5..5.25, front_trim=1.0s.
    renderer.render_frame(project, at=5.0, output_path=tmp_path / "preview.png")
    clip = captured["clip"]

    # The shared first volume point (t=1.0 -> abs t=5.0) is the window origin and must
    # land at the IDENTICAL source frame it had un-windowed (61.0): proof the t-shift
    # and sourceStart-bump cancel. Points before the window clamp to the origin frame.
    win_vol_x = [p["co"]["X"] for p in clip["volume"]["Points"]]
    assert win_vol_x == [61.0, 61.0, 121.0]
    assert win_vol_x[1] == head_vol_x[1]            # the in-window point is frame-stable
    # Volume Y (ducking depth) rides along unchanged: 1.0,0.2,1.0 -> 100,20,100.
    assert [round(p["co"]["Y"], 2) for p in clip["volume"]["Points"]] == [100.0, 20.0, 100.0]

    # Opacity fade: its t=1.0 reveal also sits at abs t=5.0, so it must fire at the
    # window-origin frame 61.0 (not slip late), with Y still clamped 0..1.
    op = clip["alpha"]["Points"]
    assert [p["co"]["X"] for p in op] == [61.0, 61.0]   # both <= origin clamp to 61
    assert [p["co"]["Y"] for p in op] == [0.0, 1.0]

    # Position (location_x) envelope shifts in lockstep: x t=0/3 -> frames 61/121.
    loc_x = [p["co"]["X"] for p in clip["location_x"]["Points"]]
    assert loc_x == [61.0, 121.0]

    # The caller's project is never mutated by the windowing (deep-copy scope).
    assert project["clips"]["c1"]["sourceStart"] == pytest.approx(1.0)
    assert project["clips"]["c1"]["keyframes"][0]["points"][0]["t"] == pytest.approx(0.0)


def _ffmpeg_envelope_project(tmp_path, *, source_start: float = 0.0) -> dict:
    """The ffmpeg fallback animates volume on AUDIO assets and opacity/x on VISUAL
    assets (see _FFMPEG_KEYFRAME_PROPERTIES). Use two clips sharing the SAME timeline
    span (t=4..7) and sourceStart so a single window front-trims both identically: a
    video carrying opacity + x, and an audio bed carrying the volume envelope. Real
    media on disk so the missing-asset gate stays quiet."""
    clip_video = tmp_path / "clip.mp4"
    bed_audio = tmp_path / "bed.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64:r=30:d=8",
         "-frames:v", "240", str(clip_video)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
         "-t", "8", str(bed_audio)],
        check=True, capture_output=True, text=True,
    )
    project = timeline.new_project("ffmpeg_kf", width=1920, height=1080, fps=30)
    project["assets"]["v"] = {"id": "v", "type": "video", "src": str(clip_video), "metadata": {}}
    project["assets"]["bed"] = {"id": "bed", "type": "audio", "src": str(bed_audio), "metadata": {}}
    span = {"start": 4.0, "duration": 3.0, "sourceStart": source_start,
            "enabled": True, "muted": False, "volume": 1.0, "effects": [], "metadata": {}}
    project["clips"]["c1"] = {
        **span, "id": "c1", "assetId": "v", "trackId": "graphics_1", "kind": "artifact",
        "transform": {"x": 0.5, "y": 0.5, "scale": 1.0, "opacity": 1.0},
        "keyframes": [
            {"property": "opacity", "points": [
                {"t": 0.0, "value": 0.0, "interp": "linear"},
                {"t": 1.0, "value": 1.0, "interp": "linear"},
            ]},
            {"property": "x", "points": [
                {"t": 0.0, "value": 0.0}, {"t": 3.0, "value": 1.0},
            ]},
        ],
    }
    project["clips"]["c2"] = {
        **span, "id": "c2", "assetId": "bed", "trackId": "music_1", "kind": "music_bed",
        "transform": {"x": 0.5, "y": 0.5, "scale": 1.0, "opacity": 1.0},
        "keyframes": [
            {"property": "volume", "points": [
                {"t": 0.0, "value": 1.0, "interp": "linear"},
                {"t": 1.0, "value": 0.2, "interp": "constant"},
                {"t": 3.0, "value": 1.0, "interp": "bezier"},
            ]},
        ],
    }
    return project


def _kf_segment_thresholds(expr: str) -> list[float]:
    """Pull the ordered `lt(<var>,<T>)` segment thresholds out of a
    _piecewise_linear_expr string. The smallest threshold is the OUTERMOST if (it
    appears first in the string), so left-to-right order is ascending segment time —
    the filter-time at which each keyframe point fires."""
    import re

    return [float(m) for m in re.findall(r"lt\([tT],([0-9.]+)\)", expr)]


def test_ffmpeg_render_frame_window_shift_preserves_keyframe_source_frames(tmp_path):
    """Parallel to the OpenShot frame-window test for the FFmpegLayeredRenderer path.

    OpenShotRenderer.render_frame windows a 250ms slice and proves the keyframe-t
    shift (down) and sourceStart bump (up) CANCEL in OpenShot frame space. The ffmpeg
    fallback windows the SAME way (FFmpegLayeredRenderer.render -> _build_video_command
    -> _windowed_clips), but lands its keyframes via filter-time `t`/`T` after
    atrim/trim + (a)setpts reset PTS to 0. So the analogous invariant is: a keyframe
    that sits at the window ORIGIN must fire at filter-time 0 whether the preview window
    starts at the clip head or mid-clip — otherwise volume/opacity/x animate at the
    wrong moment on the fallback vs OpenShot (front-trim misalignment the ticket names).

    Drive the real command builder (no ffmpeg run) at two window starts and compare the
    `lt(t,T)` / `lt(T,T)` segment thresholds of the volume, opacity, and x envelopes.
    """
    import re

    renderer = editor_render.FFmpegLayeredRenderer(tmp_path / "renders")
    # Clip spans timeline t=4..7, sourceStart=1.0. Volume points sit at clip-relative
    # t=0,1,3 -> absolute timeline t=4,5,7.
    project = _ffmpeg_envelope_project(tmp_path, source_start=1.0)

    def _exprs(window_start: float, duration: float):
        cmd, missing, _ = renderer._build_video_command(
            project, tmp_path / "renders" / "out.mp4",
            duration=duration, window_start=window_start,
        )
        assert missing == []
        fc = cmd[cmd.index("-filter_complex") + 1]
        # one volume= (audio), one geq alpha (opacity), one overlay x (position)
        vol = re.search(r"volume='([^']+)':eval=frame", fc).group(1)
        alpha = re.search(r"a='255\*max\(0,min\(1,(.+?)\)\)'", fc).group(1)
        x_overlay = re.search(r"overlay=x='([^']+)'", fc).group(1)
        return vol, alpha, x_overlay

    # HEAD: window the whole clip from its start (front_trim=0). Keyframe t passes
    # through untouched: volume fires at filter-time 0,1,3.
    head_vol, head_alpha, head_x = _exprs(window_start=4.0, duration=3.0)
    assert _kf_segment_thresholds(head_vol) == [0.0, 1.0, 3.0]
    assert _kf_segment_thresholds(head_alpha) == [0.0, 1.0]
    assert _kf_segment_thresholds(head_x) == [0.0, 3.0]

    # MID-CLIP: preview from absolute t=5.0 -> front_trim=1.0s. _windowed_clips shifts
    # every keyframe t down by 1.0 (clamped at 0) AND the visual trim/audio atrim bump
    # sourceStart, but PTS reset makes filter-time start at 0. So the keyframe that sat
    # at absolute t=5.0 (clip-relative 1.0) becomes the window ORIGIN at filter-time 0.
    mid_vol, mid_alpha, mid_x = _exprs(window_start=5.0, duration=2.0)
    # volume t's 0,1,3 -> shifted 0,0,2 (first two clamp to the origin frame 0).
    assert _kf_segment_thresholds(mid_vol) == [0.0, 0.0, 2.0]
    # opacity reveal t=1.0 (abs 5.0) is now the origin -> fires at filter-time 0, not
    # late: thresholds 0,1 -> 0,0. This is the front-trim misalignment guard.
    assert _kf_segment_thresholds(mid_alpha) == [0.0, 0.0]
    # x pan t's 0,3 -> 0,2, shifting in lockstep with volume/opacity.
    assert _kf_segment_thresholds(mid_x) == [0.0, 2.0]

    # The in-window keyframe is frame-stable across both builds: the volume point at
    # absolute t=7.0 (the bezier return) sits 3.0s after the head origin and 2.0s after
    # the mid origin, i.e. exactly front_trim earlier — the t-shift, nothing dropped.
    assert _kf_segment_thresholds(head_vol)[-1] - _kf_segment_thresholds(mid_vol)[-1] == pytest.approx(1.0)

    # render_frame is the real entrypoint and uses a fixed 250ms window; its built
    # command must carry the same shifted-origin opacity envelope (proving render_frame,
    # not just _build_video_command, threads the windowing). Capture the command _run
    # would receive instead of invoking ffmpeg.
    captured: dict[str, list] = {}
    original_run = editor_render._run
    original_probe = editor_render._probe_duration
    try:
        editor_render._run = lambda cmd: captured.setdefault("cmds", []).append(cmd)
        editor_render._probe_duration = lambda *a, **k: 0.25  # no real mp4 to probe
        renderer.render_frame(project, at=5.0, output_path=tmp_path / "renders" / "preview.png")
    finally:
        editor_render._run = original_run
        editor_render._probe_duration = original_probe
    # first _run is the layered render building the preview slice (has -filter_complex);
    # the second is the single-frame extract.
    render_cmd = next(c for c in captured["cmds"] if "-filter_complex" in c)
    rc_fc = render_cmd[render_cmd.index("-filter_complex") + 1]
    rc_alpha = re.search(r"a='255\*max\(0,min\(1,(.+?)\)\)'", rc_fc).group(1)
    assert _kf_segment_thresholds(rc_alpha)[0] == 0.0  # origin reveal fires immediately

    # The caller's project is never mutated by the windowing (deep-copy scope), exactly
    # as the OpenShot parallel asserts.
    assert project["clips"]["c1"]["sourceStart"] == pytest.approx(1.0)
    assert project["clips"]["c1"]["keyframes"][0]["points"][0]["t"] == pytest.approx(0.0)


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


# ---------------------------------------------------------------------------
# ABN-import -> ffmpeg-fallback seam. project_from_abn_timeline produces the
# project the renderers consume. The fallback can't composite crossfades or
# keyframe envelopes (OpenShot-only); on an ABN-imported timeline it must still
# (a) build a command without crashing on the text-only lower third (no media
# src -> skip, not a phantom -i input) and (b) SURFACE the un-renderable
# crossfade/keyframe as structured warnings. Both blind spots the ticket names.
# ---------------------------------------------------------------------------


def test_ffmpeg_fallback_on_abn_imported_timeline_skips_text_layer_and_warns(tmp_path):
    # real media files so the broll/bed/vo assets resolve (not missing-asset noise)
    broll = _solid_png(tmp_path / "ui.png", "blue", size="96x64")
    vo = tmp_path / "vo.wav"
    bedf = tmp_path / "bed.wav"
    for audio in (vo, bedf):
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
             "-t", "4", str(audio)],
            check=True, capture_output=True, text=True,
        )

    abn_timeline = {
        "episodeId": "ep_render_seam",
        "fps": 12,
        "width": 96,
        "height": 64,
        "totalSec": 4.0,
        "musicBed": str(bedf),
        "segments": [
            {
                "segmentId": "s0",
                "durationSec": 4.0,
                "shots": [
                    {"id": "b", "src": str(broll), "startSec": 0.0,
                     "durationSec": 2.0, "type": "still"},
                ],
                "audio": {"vo": {"src": str(vo), "duration": 4.0}},
                # text-only lower third: headline lives in metadata, no media src.
                "lowerThirds": [{"startSec": 0.5, "durationSec": 2.5, "headline": "Hook"}],
            }
        ],
    }
    project = timeline.project_from_abn_timeline("proj_render_seam", abn_timeline)

    # Attach the OpenShot-only constructs the fallback must warn about: a crossfade
    # transition on the still, and a ducking volume envelope on the imported bed.
    still = next(c for c in project["clips"].values() if c["kind"] == "still")
    still["effects"] = [{"id": "fx_cf", "type": "crossfade", "params": {"duration": 0.3}}]
    bed_clip = next(c for c in project["clips"].values() if c["kind"] == "music_bed")
    bed_clip["keyframes"] = [
        {"property": "volume", "points": [
            {"t": 0.0, "value": 0.6, "interp": "linear"},
            {"t": 1.0, "value": 0.22, "interp": "constant"},
        ]},
    ]

    renderer = editor_render.FFmpegLayeredRenderer(tmp_path / "renders", asset_root=tmp_path)
    cmd, missing, warnings = renderer._build_video_command(
        project, tmp_path / "renders" / "seam.mp4", duration=4.0
    )

    # The text-only lower third (empty src) is skipped, not reported missing or
    # fed as a phantom -i input that would crash the command.
    assert missing == []
    names = {(w["kind"], w["name"]) for w in warnings}
    assert ("effect", "crossfade") in names            # crossfade can't be faked
    # the ducking volume envelope is now reproduced as a piecewise-linear ffmpeg
    # volume expression (no longer a silent drop), so it must NOT warn AND the
    # built command must carry the time-varying expression.
    assert ("keyframe", "volume") not in names
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "eval=frame" in fc and "if(lt(t," in fc
    # the command is buildable and routes the resolved media through -i inputs
    assert str(broll) in cmd and str(bedf) in cmd and str(vo) in cmd


# ---------------------------------------------------------------------------
# RenderError exception paths not otherwise pinned: the ffmpeg-layered `_run`
# helper (timeout + non-zero exit, lines 990-993) and the OpenShot subprocess
# wrapper's child-failure branches (lines 247, 249). All mocked at the stdlib
# boundary so they are deterministic and need no real ffmpeg/libopenshot.
# ---------------------------------------------------------------------------


def test_run_raises_render_error_on_ffmpeg_timeout(monkeypatch):
    """editor_render._run:990-991 — a TimeoutExpired from the layered ffmpeg
    render subprocess is re-raised as RenderError carrying the command head, not
    allowed to escape raw as a 500."""

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=60)

    monkeypatch.setattr(editor_render.subprocess, "run", _timeout)

    with pytest.raises(editor_render.RenderError) as excinfo:
        editor_render._run(["ffmpeg", "-y", "-i", "in.mp4", "out.mp4"])
    assert "timed out" in str(excinfo.value)


def test_run_raises_render_error_on_nonzero_exit(monkeypatch):
    """editor_render._run:992-993 — a non-zero ffmpeg exit surfaces its stderr
    tail as a RenderError."""

    class _Failed:
        returncode = 1
        stderr = "ffmpeg: layered render boom"
        stdout = ""

    monkeypatch.setattr(editor_render.subprocess, "run", lambda *a, **k: _Failed())

    with pytest.raises(editor_render.RenderError) as excinfo:
        editor_render._run(["ffmpeg", "-y", "out.mp4"])
    assert "layered render boom" in str(excinfo.value)


def test_openshot_subprocess_raises_render_error_when_child_fails_without_artifact(monkeypatch, tmp_path):
    """editor_render.OpenShotSubprocessRenderer._run_child:246-247 — a non-zero
    child exit with NO salvageable artifact on disk is a hard RenderError carrying
    the exit code and stderr tail (the salvage branch at 240-245 is skipped because
    the output path never gets written)."""

    class _Completed:
        returncode = -6  # SIGABRT from a native libopenshot crash
        stdout = ""
        stderr = "libopenshot aborted before writing output"

    monkeypatch.setattr(editor_render.subprocess, "run", lambda *a, **k: _Completed())

    renderer = editor_render.OpenShotSubprocessRenderer(tmp_path / "renders")
    with pytest.raises(editor_render.RenderError) as excinfo:
        renderer.render({"projectId": "p"}, output_path=tmp_path / "renders" / "never.mp4", start=0, duration=1)
    assert "OpenShot subprocess failed (-6)" in str(excinfo.value)
    assert "libopenshot aborted" in str(excinfo.value)


def test_openshot_subprocess_rejects_nonfinite_volume_keyframe_before_spawn(monkeypatch, tmp_path):
    """editor_render.OpenShotSubprocessRenderer._run_child — a non-finite
    (NaN/Infinity) volume/ducking keyframe value must fail the JSON round-trip
    LOUDLY in the parent before the child is spawned. Otherwise json.dumps emits
    a bare `NaN`/`Infinity` token, the child's lenient json.loads swallows it,
    OpenShot flattens the ducking envelope to garbage, and the child exits 0 — a
    corrupt-audio render the parent never detects. We assert (1) it raises, and
    (2) subprocess.run is NEVER reached (no silent-success window)."""

    spawned = {"called": False}

    def _boom_if_spawned(*_a, **_k):
        spawned["called"] = True
        raise AssertionError("subprocess.run must not be called for a corrupt payload")

    monkeypatch.setattr(editor_render.subprocess, "run", _boom_if_spawned)

    project = {
        "projectId": "p",
        "clips": {
            "music": {
                "id": "music",
                "assetId": "bed",
                "volume": 1.0,
                # A ducking envelope corrupted to a non-finite gain.
                "keyframes": [
                    {"property": "volume", "points": [
                        {"t": 0.0, "value": 1.0},
                        {"t": 2.0, "value": float("inf")},
                    ]},
                ],
            },
        },
    }

    renderer = editor_render.OpenShotSubprocessRenderer(tmp_path / "renders")
    with pytest.raises(editor_render.RenderError) as excinfo:
        renderer.render(project, output_path=tmp_path / "renders" / "duck.mp4", start=0, duration=3)
    assert "non-finite" in str(excinfo.value)
    assert spawned["called"] is False


def test_openshot_subprocess_allows_finite_volume_keyframe_round_trip(monkeypatch, tmp_path):
    """The guard is precise: a well-formed (finite) volume/ducking envelope must
    pass cleanly through the JSON boundary and reach the child unchanged."""

    captured = {}

    class _Completed:
        returncode = 0
        stdout = json.dumps({"video": "ok.mp4", "warnings": []})
        stderr = ""

    def _capture(*_a, **kwargs):
        captured["input"] = kwargs.get("input")
        return _Completed()

    monkeypatch.setattr(editor_render.subprocess, "run", _capture)

    project = {
        "projectId": "p",
        "clips": {
            "music": {
                "id": "music", "assetId": "bed", "volume": 1.0,
                "keyframes": [
                    {"property": "volume", "points": [
                        {"t": 0.0, "value": 1.0},
                        {"t": 2.0, "value": 0.2},  # duck under VO
                        {"t": 4.0, "value": 1.0},
                    ]},
                ],
            },
        },
    }

    renderer = editor_render.OpenShotSubprocessRenderer(tmp_path / "renders")
    result = renderer.render(project, output_path=tmp_path / "renders" / "duck.mp4", start=0, duration=5)
    assert result["video"] == "ok.mp4"
    # The exact ducking envelope must survive the serialization the child reads.
    sent = json.loads(captured["input"])
    pts = sent["project"]["clips"]["music"]["keyframes"][0]["points"]
    assert [p["value"] for p in pts] == [1.0, 0.2, 1.0]


def test_openshot_subprocess_raises_render_error_when_child_emits_no_result(monkeypatch, tmp_path):
    """editor_render.OpenShotSubprocessRenderer._run_child:248-249 — a clean exit
    (returncode 0) whose stdout carries no parseable JSON render result is a
    RenderError, not a silent None return."""

    class _Completed:
        returncode = 0
        stdout = "warming up...\nnot json at all\n"
        stderr = ""

    monkeypatch.setattr(editor_render.subprocess, "run", lambda *a, **k: _Completed())

    renderer = editor_render.OpenShotSubprocessRenderer(tmp_path / "renders")
    with pytest.raises(editor_render.RenderError) as excinfo:
        renderer.render({"projectId": "p"}, output_path=tmp_path / "renders" / "empty.mp4", start=0, duration=1)
    assert "produced no render result" in str(excinfo.value)


# ---------------------------------------------------------------------------
# OpenShot audio-mux gating. The external ffmpeg mix (editor_render.py L334) must
# run ONLY when OpenShot wrote SILENT video (ffmpeg present -> mix_audio_externally).
# When ffmpeg is absent OpenShot already wrote its own in-engine AAC stream, and
# unconditionally calling _mux_timeline_audio would raise "ffmpeg is required",
# throwing away a perfectly valid OpenShot render. These pin both branches with a
# FAKE openshot module so they run with no libopenshot and no ffmpeg dependency.
# ---------------------------------------------------------------------------


class _FakeWriter:
    def __init__(self, _path):
        self.audio_enabled = None

    def SetVideoOptions(self, *a, **k):
        pass

    def SetAudioOptions(self, has_audio, *a, **k):
        self.audio_enabled = has_audio

    def Open(self):
        pass

    def WriteFrame(self, _frame):
        pass

    def Close(self):
        pass


class _FakeFraction:
    def __init__(self, num, den):
        self.num, self.den = num, den


def _fake_openshot():
    import types

    mod = types.SimpleNamespace()
    mod.FFmpegWriter = _FakeWriter
    mod.Fraction = _FakeFraction
    return mod


def _audio_project(tmp_path):
    tone = tmp_path / "tone.wav"
    tone.write_bytes(b"")  # only existence is checked before any (skipped) mux
    project = timeline.new_project("mux_gate", width=96, height=64, fps=12)
    project["assets"]["tone"] = {"id": "tone", "type": "audio", "src": str(tone)}
    project["clips"]["tone"] = {
        "id": "tone", "assetId": "tone", "trackId": "audio_1", "kind": "voiceover",
        "start": 0.0, "duration": 1.0, "sourceStart": 0.0, "enabled": True,
        "muted": False, "volume": 1.0, "transform": {},
    }
    return project


def _build_fake_openshot_renderer(tmp_path, monkeypatch):
    """An OpenShotRenderer whose native bits are faked: the writer is a no-op,
    _timeline returns a stub, and _probe_duration is stubbed — so render() exercises
    the real audio-mux gating logic with no libopenshot and no ffmpeg."""
    fake = _fake_openshot()
    monkeypatch.setattr(editor_render, "_import_openshot", lambda: (fake, "available", None))
    renderer = editor_render.OpenShotRenderer(tmp_path / "renders")

    class _StubTimeline:
        def GetFrame(self, _n):
            return object()

        def Close(self):
            pass

    monkeypatch.setattr(renderer, "_timeline", lambda _p: _StubTimeline())
    monkeypatch.setattr(editor_render, "_probe_duration", lambda *_a, **_k: 1.0)
    return renderer


def test_openshot_render_skips_external_mux_when_ffmpeg_absent(tmp_path, monkeypatch):
    """The gap the ticket flags: with audio present but ffmpeg ABSENT, OpenShot
    writes its own in-engine AAC stream (SetAudioOptions enabled). render() must NOT
    then call the ffmpeg mux — which would raise 'ffmpeg is required' and discard a
    valid render. Assert audioMuxed is False and _mux_timeline_audio is never hit."""
    monkeypatch.setattr(editor_render.shutil, "which", lambda name: None)  # no ffmpeg/ffprobe

    def _boom(*a, **k):
        raise AssertionError("_mux_timeline_audio must NOT run when ffmpeg is absent")

    monkeypatch.setattr(editor_render, "_mux_timeline_audio", _boom)

    renderer = _build_fake_openshot_renderer(tmp_path, monkeypatch)
    project = _audio_project(tmp_path)
    output = tmp_path / "renders" / "out.mp4"

    result = renderer.render(project, output_path=output)

    assert result["audioMuxed"] is False  # in-engine audio, no external mux
    assert result["backend"] == "openshot"


def test_openshot_render_muxes_externally_when_ffmpeg_present(tmp_path, monkeypatch):
    """The other branch: ffmpeg present -> OpenShot wrote SILENT video, so render()
    DOES call the deterministic external mix. Stub the mux to confirm it ran and its
    True return flows out as audioMuxed."""
    monkeypatch.setattr(editor_render.shutil, "which", lambda name: "/usr/bin/" + name)

    calls: list[float] = []

    def _spy(project, output, *, duration, asset_root):
        calls.append(duration)
        return True

    monkeypatch.setattr(editor_render, "_mux_timeline_audio", _spy)

    renderer = _build_fake_openshot_renderer(tmp_path, monkeypatch)
    project = _audio_project(tmp_path)
    output = tmp_path / "renders" / "out.mp4"

    result = renderer.render(project, output_path=output)

    assert calls, "external mux must run when ffmpeg is present (silent OpenShot video)"
    assert result["audioMuxed"] is True


def test_openshot_render_silent_project_never_muxes(tmp_path, monkeypatch):
    """A project with no audio clips must never touch the mux path regardless of
    ffmpeg presence — audioMuxed stays False and _mux_timeline_audio is not called."""
    monkeypatch.setattr(editor_render.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        editor_render, "_mux_timeline_audio",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no audio -> no mux")),
    )

    renderer = _build_fake_openshot_renderer(tmp_path, monkeypatch)
    red = _solid_png(tmp_path / "red.png", "red", size="96x64")
    project = _project_with_card("silent", red, x=0.0)
    output = tmp_path / "renders" / "silent.mp4"

    result = renderer.render(project, output_path=output)

    assert result["audioMuxed"] is False


# ---------------------------------------------------------------------------
# OpenShotRenderer.render() exception cleanup (editor_render.py:424-434). When
# writer.Open()/WriteFrame() throws (crashed libopenshot), the except block calls
# writer.Close() in a blind try/except and the finally calls timeline.Close() also
# blindly. These paths are never hit by the happy-path fakes above: a failed Open()
# followed by a double Close(), and timeline.Close() after an early failure. A
# regression here either masks the real error behind a cleanup exception or leaks
# the native Timeline handle. Pin both with a writer/timeline that can be told to
# blow up at each stage.
# ---------------------------------------------------------------------------


class _ExplodingWriter(_FakeWriter):
    """A writer whose Open()/WriteFrame()/Close() can be armed to raise, and that
    counts Close() calls so the double-close path can be asserted."""

    open_error: Exception | None = None
    write_error: Exception | None = None
    close_error: Exception | None = None

    def __init__(self, _path):
        super().__init__(_path)
        self.close_calls = 0

    def Open(self):
        if self.open_error is not None:
            raise self.open_error

    def WriteFrame(self, _frame):
        if self.write_error is not None:
            raise self.write_error

    def Close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def _build_exploding_renderer(tmp_path, monkeypatch):
    """OpenShotRenderer wired with _ExplodingWriter + a Timeline stub that records
    whether Close() ran and can be armed to raise on Close()."""
    fake = _fake_openshot()
    fake.FFmpegWriter = _ExplodingWriter
    monkeypatch.setattr(editor_render, "_import_openshot", lambda: (fake, "available", None))
    renderer = editor_render.OpenShotRenderer(tmp_path / "renders")

    class _StubTimeline:
        closed = 0
        close_error: Exception | None = None

        def GetFrame(self, _n):
            return object()

        def Close(self):
            type(self).closed += 1
            if self.close_error is not None:
                raise self.close_error

    timeline_stub = _StubTimeline()
    monkeypatch.setattr(renderer, "_timeline", lambda _p: timeline_stub)
    monkeypatch.setattr(editor_render, "_probe_duration", lambda *_a, **_k: 1.0)
    return renderer, timeline_stub


def test_openshot_render_wraps_writer_open_failure_as_render_error(tmp_path, monkeypatch):
    """writer.Open() failure (crashed libopenshot at startup) must surface as a
    RenderError carrying the original cause, not leak the raw native exception."""
    monkeypatch.setattr(editor_render.shutil, "which", lambda name: None)
    renderer, _timeline_stub = _build_exploding_renderer(tmp_path, monkeypatch)
    monkeypatch.setattr(_ExplodingWriter, "open_error", RuntimeError("libopenshot Open crashed"), raising=False)

    project = _audio_project(tmp_path)
    with pytest.raises(editor_render.RenderError) as excinfo:
        renderer.render(project, output_path=tmp_path / "renders" / "out.mp4")

    assert "OpenShot render failed" in str(excinfo.value)
    # the original libopenshot exception is preserved as the cause (raise ... from exc)
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "Open crashed" in str(excinfo.value.__cause__)


def test_openshot_render_double_close_after_open_failure_does_not_mask_error(tmp_path, monkeypatch):
    """After Open() fails, the except block calls writer.Close() in a blind
    try/except (line 426). On an already-failed/half-open writer that Close() can
    itself throw — it must be swallowed so the ORIGINAL Open() error is what
    propagates, never the secondary cleanup error."""
    monkeypatch.setattr(editor_render.shutil, "which", lambda name: None)
    renderer, _timeline_stub = _build_exploding_renderer(tmp_path, monkeypatch)
    monkeypatch.setattr(_ExplodingWriter, "open_error", RuntimeError("primary Open failure"), raising=False)
    monkeypatch.setattr(_ExplodingWriter, "close_error", RuntimeError("secondary Close blew up"), raising=False)

    project = _audio_project(tmp_path)
    with pytest.raises(editor_render.RenderError) as excinfo:
        renderer.render(project, output_path=tmp_path / "renders" / "out.mp4")

    # the wrapped cause is the PRIMARY Open failure, not the cleanup Close failure
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "primary Open failure" in str(excinfo.value.__cause__)
    assert "secondary Close" not in str(excinfo.value)


def test_openshot_render_closes_timeline_after_early_failure(tmp_path, monkeypatch):
    """The finally block must close the native Timeline even when Open() failed
    before any frame was written — otherwise the libopenshot handle leaks on every
    crashed render."""
    monkeypatch.setattr(editor_render.shutil, "which", lambda name: None)
    renderer, timeline_stub = _build_exploding_renderer(tmp_path, monkeypatch)
    type(timeline_stub).closed = 0
    monkeypatch.setattr(_ExplodingWriter, "open_error", RuntimeError("Open failed early"), raising=False)

    project = _audio_project(tmp_path)
    with pytest.raises(editor_render.RenderError):
        renderer.render(project, output_path=tmp_path / "renders" / "out.mp4")

    assert type(timeline_stub).closed == 1  # timeline.Close() ran in the finally


def test_openshot_render_swallows_timeline_close_failure_keeps_original_error(tmp_path, monkeypatch):
    """timeline.Close() raising in the finally (line 432) must be swallowed by its
    blind try/except so it never overrides the render error already in flight."""
    monkeypatch.setattr(editor_render.shutil, "which", lambda name: None)
    renderer, timeline_stub = _build_exploding_renderer(tmp_path, monkeypatch)
    monkeypatch.setattr(_ExplodingWriter, "write_error", RuntimeError("WriteFrame crashed"), raising=False)
    monkeypatch.setattr(type(timeline_stub), "close_error", RuntimeError("Timeline Close crashed"), raising=False)

    project = _audio_project(tmp_path)
    with pytest.raises(editor_render.RenderError) as excinfo:
        renderer.render(project, output_path=tmp_path / "renders" / "out.mp4")

    # the in-flight WriteFrame error wins; the finally's Close failure is swallowed
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "WriteFrame crashed" in str(excinfo.value.__cause__)
    assert "Timeline Close" not in str(excinfo.value)


def test_refit_effects_drops_start_fade_eaten_by_front_trim():
    """A start-anchored fade whose ramp is fully consumed by the front trim must be
    DROPPED, not kept as a meaningless `duration: 0.0` fade. A 0.5s crossfade with a
    0.6s front trim (split at the 0.6s mark) re-fits to -0.1 -> the fade no longer
    exists on the tail; emitting `duration: 0.0` would breach the OpenShot contract
    (Fade silently ignores a 0s fade) and desync the timeline from the rendered video."""
    effects = [{"id": "xf", "type": "crossfade", "params": {"duration": 0.5}}]
    refit = editor_render._refit_effects(effects, front_trim=0.6, windowed_duration=2.0)
    assert refit == []


def test_refit_effects_keeps_partially_trimmed_start_fade():
    """A start-anchored fade only partially eaten by the front trim survives with its
    remaining duration (regression guard so the zero-drop doesn't over-eat)."""
    effects = [{"id": "fi", "type": "fadeIn", "params": {"duration": 0.5}}]
    refit = editor_render._refit_effects(effects, front_trim=0.2, windowed_duration=2.0)
    assert len(refit) == 1
    assert refit[0]["params"]["duration"] == pytest.approx(0.3)


def test_refit_effects_drops_fade_on_zero_length_window():
    """Any fade clamped to a zero-length window (windowed_duration == 0) is dropped."""
    effects = [
        {"id": "fo", "type": "fadeOut", "params": {"duration": 0.4}},
        {"id": "fi", "type": "fadeIn", "params": {"duration": 0.4}},
    ]
    refit = editor_render._refit_effects(effects, front_trim=0.0, windowed_duration=0.0)
    assert refit == []


def test_split_at_crossfade_boundary_leaves_no_dead_fade_on_tail():
    """End-to-end: split a clip carrying a 0.5s crossfade at the 0.6s mark. The tail's
    front is trimmed by 0.6s, fully eating the start-anchored crossfade. The tail must
    NOT carry a `duration: 0.0` crossfade that openshot_bridge would export as a valid
    but non-rendering fade -- it must carry no crossfade at all."""
    project = timeline.new_project("split-fade", fps=30, width=1920, height=1080)
    project["assets"]["a"] = {
        "id": "a", "type": "video", "path": "a.mp4", "duration": 10.0,
    }
    track_id = next(iter(project["tracks"]))
    project["clips"]["c"] = {
        "id": "c", "trackId": track_id, "assetId": "a",
        "start": 0.0, "duration": 2.0, "sourceStart": 0.0,
        "transform": {}, "keyframes": [],
        "effects": [{"id": "xf", "type": "crossfade", "params": {"duration": 0.5}}],
    }

    split = timeline.apply_command(
        project,
        {
            "op": "clip.split", "actor": "human", "expectedRevision": 0,
            "payload": {"clipId": "c", "at": 0.6, "newClipId": "c2"},
        },
    )

    tail = split["clips"]["c2"]
    assert all(e["type"] != "crossfade" for e in tail.get("effects") or []), (
        "tail must not carry a crossfade consumed by the split front trim"
    )
    # The head keeps the start-anchored crossfade whole (it owns the original start).
    head = split["clips"]["c"]
    head_xf = [e for e in head.get("effects") or [] if e["type"] == "crossfade"]
    assert len(head_xf) == 1 and head_xf[0]["params"]["duration"] == pytest.approx(0.5)

    # The bridge must never emit a 0-second fade for the tail's compiled effects.
    for effect in tail.get("effects") or []:
        out = openshot_bridge.effect_json(effect, fps=30)
        if "duration" in out:
            assert out["duration"]["Points"][0]["co"]["Y"] > 0.0


# ---------------------------------------------------------------------------
# _parse_child_render_result — malformed / contaminated subprocess stdout.
# Pure-string parsing; no ffmpeg needed (but module skipif keeps it harmless).
# ---------------------------------------------------------------------------


def test_parse_child_render_result_picks_last_dict_line():
    stdout = (
        "loading project...\n"
        '{"video": "/tmp/a.mp4"}\n'
        '{"video": "/tmp/final.mp4"}\n'
    )
    assert editor_render._parse_child_render_result(stdout) == {
        "video": "/tmp/final.mp4"
    }


def test_parse_child_render_result_only_invalid_json():
    # stderr/logging contamination with zero parseable JSON -> None.
    stdout = "Traceback (most recent call last):\n  File x\nValueError: boom\n"
    assert editor_render._parse_child_render_result(stdout) is None


def test_parse_child_render_result_mixed_valid_invalid_spanning_lines():
    # The valid dict is buried above noisy non-JSON trailing lines; reverse
    # iteration must skip the junk and still find it.
    stdout = (
        '{"frame": "/tmp/f.png"}\n'
        "WARN: deprecation notice\n"
        "not json at all\n"
    )
    assert editor_render._parse_child_render_result(stdout) == {
        "frame": "/tmp/f.png"
    }


def test_parse_child_render_result_valid_but_non_dict_json():
    # Valid JSON arrays/primitives are not render results -> None.
    for payload in ("[1, 2, 3]", '"a string"', "42", "true", "null"):
        assert editor_render._parse_child_render_result(payload + "\n") is None


def test_parse_child_render_result_non_dict_then_dict():
    # A non-dict valid-JSON line below a real dict: dict still wins.
    stdout = '{"video": "/tmp/v.mp4"}\n[1, 2, 3]\n'
    assert editor_render._parse_child_render_result(stdout) == {
        "video": "/tmp/v.mp4"
    }


def test_parse_child_render_result_empty_and_none():
    assert editor_render._parse_child_render_result("") is None
    assert editor_render._parse_child_render_result(None) is None


# ---------------------------------------------------------------------------
# ffmpeg-layered failure modes not previously pinned (ticket): a vanished
# ffmpeg binary at exec time, a generic OSError at exec time, an
# unprobeable/corrupt output artifact (ffmpeg exits 0 but the container has no
# parseable duration), an ffprobe failure, and an end-to-end render that must
# surface a deep timeout as RenderError. All mocked at the stdlib boundary.
# ---------------------------------------------------------------------------


def test_run_raises_render_error_when_ffmpeg_binary_missing(monkeypatch):
    """ffmpeg detected at __init__ can vanish before the render runs; the exec
    FileNotFoundError must come back as RenderError, not a raw OSError."""

    def _missing(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "ffmpeg")

    monkeypatch.setattr(editor_render.subprocess, "run", _missing)
    with pytest.raises(editor_render.RenderError) as excinfo:
        editor_render._run(["ffmpeg", "-i", "in.mp4", "out.mp4"])
    assert "could not be executed" in str(excinfo.value)


def test_run_raises_render_error_on_generic_oserror(monkeypatch):
    """A generic OSError at exec time (e.g. ETXTBSY / Exec format error) is
    wrapped as RenderError rather than escaping raw."""

    def _oserror(*args, **kwargs):
        raise OSError("Exec format error")

    monkeypatch.setattr(editor_render.subprocess, "run", _oserror)
    with pytest.raises(editor_render.RenderError):
        editor_render._run(["ffmpeg", "out.mp4"])


def test_probe_duration_raises_render_error_on_corrupt_artifact(monkeypatch, tmp_path):
    """ffmpeg can exit 0 yet leave an artifact with no parseable duration
    (`N/A` stdout). float() of that must become RenderError, not a crash."""

    class _Result:
        returncode = 0
        stderr = ""
        stdout = "N/A\n"

    monkeypatch.setattr(editor_render.subprocess, "run", lambda *a, **k: _Result())
    with pytest.raises(editor_render.RenderError) as excinfo:
        editor_render._probe_duration("ffprobe", tmp_path / "out.mp4")
    assert "unprobeable" in str(excinfo.value)


def test_probe_duration_raises_render_error_on_empty_stdout(monkeypatch, tmp_path):
    """Empty ffprobe stdout (truncated / zero-byte output) -> RenderError."""

    class _Result:
        returncode = 0
        stderr = ""
        stdout = ""

    monkeypatch.setattr(editor_render.subprocess, "run", lambda *a, **k: _Result())
    with pytest.raises(editor_render.RenderError):
        editor_render._probe_duration("ffprobe", tmp_path / "out.mp4")


def test_probe_duration_propagates_ffprobe_failure(monkeypatch, tmp_path):
    """Non-zero ffprobe exit (unreadable file) reports its stderr as RenderError."""

    class _Result:
        returncode = 1
        stderr = "moov atom not found"
        stdout = ""

    monkeypatch.setattr(editor_render.subprocess, "run", lambda *a, **k: _Result())
    with pytest.raises(editor_render.RenderError) as excinfo:
        editor_render._probe_duration("ffprobe", tmp_path / "out.mp4")
    assert "moov atom" in str(excinfo.value)


def test_full_render_surfaces_timeout_as_render_error(monkeypatch, tmp_path):
    """End-to-end: a timeout deep in _run during FFmpegLayeredRenderer.render
    propagates out as RenderError (the renderer never leaks TimeoutExpired)."""
    renderer = editor_render.FFmpegLayeredRenderer(tmp_path / "renders")
    img = _solid_png(tmp_path / "a.png", "red")
    project = _project_with_card("timeout", img)

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=60)

    monkeypatch.setattr(editor_render.subprocess, "run", _timeout)
    with pytest.raises(editor_render.RenderError) as excinfo:
        renderer.render(project, output_path=tmp_path / "out.mp4")
    assert "timed out" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Transition translation-gap guard. ABN crossfades flow:
#   editor_timeline.EFFECT_TYPES        (validation gate; what an import may carry)
#   -> openshot_bridge._EFFECT_CLASS_MAP / _FADE_DIRECTION_MAP  (-> libopenshot Fade)
#   -> editor_render._FFMPEG_NATIVE_EFFECTS  (what the fallback can reproduce; the
#      rest must surface as a structured drop, never vanish silently)
# These four dicts are hand-maintained and independent. If a future transition
# type is added to the validation gate but missed in the bridge maps, effect_json
# emits a bare class name with NO `fade`/`duration` and the fallback doesn't even
# flag it as a drop -- a fidelity loss with zero warning (exactly the ticket's
# "any transition type outside that set fails silently"). These guards make that
# desync fail loudly here instead of silently in a render.
# ---------------------------------------------------------------------------


def test_every_fadelike_effect_type_has_bridge_class_and_direction():
    """Every fade/crossfade transition the validation gate accepts must have BOTH
    a bridge class (_EFFECT_CLASS_MAP) and a fade direction (_FADE_DIRECTION_MAP),
    or openshot_bridge.effect_json silently emits a directionless, durationless
    Fade -- a hard cut where the factory choreographed a dissolve."""
    fadelike = {"fadeIn", "fadeOut", "crossfade"}
    # The fade-like vocabulary must actually be a subset of what the import gate
    # validates -- otherwise this guard is checking against a stale literal.
    assert fadelike <= set(timeline.EFFECT_TYPES)
    for effect_type in fadelike:
        assert effect_type in openshot_bridge._EFFECT_CLASS_MAP, effect_type
        assert effect_type in openshot_bridge._FADE_DIRECTION_MAP, effect_type


def test_every_validated_effect_type_translates_through_bridge():
    """Every effect type the ABN import gate can carry must produce a non-empty
    OpenShot translation (a known class name, not the raw passthrough), so an
    import never reaches the compiler as an untranslated effect."""
    for effect_type, spec in timeline.EFFECT_TYPES.items():
        params = {name: 0.5 for name in spec}
        out = openshot_bridge.effect_json(
            {"id": f"fx_{effect_type}", "type": effect_type, "params": params},
            fps=30,
        )
        # A mapped class never equals the raw editor type (Fade/Brightness/...);
        # the raw-type passthrough branch is the silent-failure signature.
        assert out["type"] != effect_type, (
            f"{effect_type} fell through openshot_bridge untranslated -- "
            "add it to _EFFECT_CLASS_MAP"
        )


def test_crossfade_translates_to_a_real_in_fade_with_duration():
    """The concrete carry-through: a factory crossfade lands as an OpenShot `in`
    Fade carrying its duration -- not a bare directionless effect."""
    out = openshot_bridge.effect_json(
        {"id": "xf", "type": "crossfade", "params": {"duration": 0.5}}, fps=30
    )
    assert out["type"] == "Fade"
    assert out["fade"] == "in"
    assert out["duration"]["Points"][0]["co"]["Y"] == pytest.approx(0.5)


def test_ffmpeg_fallback_native_effects_are_all_real_validated_types():
    """The fallback's native-effect allowlists must reference only effect types
    the import gate actually defines. A typo'd or stale entry would silently let a
    genuinely-unreproducible effect skip its drop warning."""
    assert editor_render._FFMPEG_NATIVE_EFFECTS <= set(timeline.EFFECT_TYPES)
    assert editor_render._FFMPEG_NATIVE_AUDIO_EFFECTS <= set(timeline.EFFECT_TYPES)


# ---------------------------------------------------------------------------
# Full Editor Bay -> OpenShot compile pipeline, exercised on ONE real ABN shot
# carrying EVERY effect type + an inter-shot transition + a kenBurns block + an
# explicit keyframe envelope. Prior tests pin the bridge maps in isolation and
# the ffmpeg-FALLBACK seam; none drives a single imported shot that wears the
# whole effect vocabulary through project_from_abn_timeline -> openshot clip_json
# and asserts NOTHING is silently dropped on the OpenShot (sanctioned-compiler)
# path. This is the fidelity guard the prod-cycle 'openshot' sweep flagged:
# "volume/ducking/transitions/effects not carried" through the timeline import.
# ---------------------------------------------------------------------------


def test_all_abn_effect_types_round_trip_through_openshot_bridge():
    """Import one ABN shot wearing every EFFECT_TYPE + transitionSec + kenBurns +
    an explicit keyframe envelope, compile it to OpenShot, and assert each one
    survives. A silent drop anywhere in editor_timeline.project_from_abn_timeline
    or openshot_bridge.clip_json fails this loudly instead of in a render."""

    abn_timeline = {
        "episodeId": "ep_all_fx",
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "totalSec": 6.0,
        "segments": [
            {
                "segmentId": "s0",
                "durationSec": 6.0,
                "shots": [
                    {
                        "id": "hero",
                        "src": "/agenticnews-assets/ep_all_fx/hero.mp4",
                        "type": "remotion",
                        "startSec": 0.0,
                        "durationSec": 6.0,
                        "clipStartSec": 0.0,
                        # inter-shot dissolve the factory choreographs at the boundary
                        "transitionSec": 0.4,
                        # every color/fade effect the import gate accepts
                        "effects": [
                            {"id": "fx_in", "type": "fadeIn", "params": {"duration": 0.5}},
                            {"id": "fx_out", "type": "fadeOut", "params": {"duration": 0.5}},
                            {"id": "fx_br", "type": "brightness", "params": {"value": 0.2}},
                            {"id": "fx_sat", "type": "saturation", "params": {"value": 1.4}},
                        ],
                        # ken-Burns push-in + pan -> scale/x keyframe tracks
                        "kenBurns": {
                            "startScale": 1.0,
                            "endScale": 1.2,
                            "startX": 0.4,
                            "endX": 0.6,
                        },
                        # an explicit envelope on a NON-kenBurns property (opacity)
                        "keyframes": [
                            {"property": "opacity", "points": [
                                {"t": 0.0, "value": 0.0, "interp": "linear"},
                                {"t": 1.0, "value": 1.0, "interp": "linear"},
                            ]},
                        ],
                    }
                ],
            }
        ],
    }

    project = timeline.project_from_abn_timeline("proj_all_fx", abn_timeline)
    clip = next(c for c in project["clips"].values() if c["kind"] == "remotion")

    # 1) IMPORT carried every effect onto the clip: the 4 explicit ones PLUS the
    # synthesized crossfade from transitionSec (no explicit crossfade existed).
    imported_effects = {e["type"] for e in clip["effects"]}
    assert imported_effects == {"fadeIn", "fadeOut", "brightness", "saturation", "crossfade"}
    # transitionSec became a real-duration crossfade, not a 0s/None placeholder.
    xf = next(e for e in clip["effects"] if e["type"] == "crossfade")
    assert xf["params"]["duration"] == pytest.approx(0.4)

    # 2) IMPORT carried both kenBurns axes (scale, x) AND the explicit opacity
    # envelope as keyframe tracks (none clobbered the others).
    assert {k["property"] for k in clip["keyframes"]} == {"scale", "x", "opacity"}

    # 3) COMPILE: every imported effect translates to a known OpenShot class --
    # no raw-type passthrough (the silent-failure signature). Brightness/Saturation
    # map to their own classes; the three fade-likes all become a Fade with a
    # direction.
    compiled = openshot_bridge.clip_json(project, clip)
    compiled_classes = sorted(e["type"] for e in compiled["effects"])
    assert compiled_classes == ["Brightness", "Fade", "Fade", "Fade", "Saturation"]
    # None fell through as the raw editor type (fadeIn/brightness/... are the
    # silent-failure signature -- a bare class name with no fade/duration).
    raw_editor_types = set(timeline.EFFECT_TYPES)
    assert not (set(compiled_classes) & raw_editor_types)
    fades = [e for e in compiled["effects"] if e["type"] == "Fade"]
    assert sorted(f["fade"] for f in fades) == ["in", "in", "out"]  # 2x in (fadeIn+crossfade) + fadeOut
    for fade in fades:
        assert fade["duration"]["Points"][0]["co"]["Y"] > 0.0  # never a 0s hard-cut fade
    bright = next(e for e in compiled["effects"] if e["type"] == "Brightness")
    assert bright["brightness"]["Points"][0]["co"]["Y"] == pytest.approx(0.2)
    sat = next(e for e in compiled["effects"] if e["type"] == "Saturation")
    assert sat["saturation"]["Points"][0]["co"]["Y"] == pytest.approx(1.4)

    # 4) COMPILE: every keyframe envelope reaches OpenShot as a multi-point curve,
    # not a flattened single-point default. scale -> scale_x/scale_y, x ->
    # location_x, opacity -> alpha. A silent drop would leave the flat 1-point seed.
    assert len(compiled["scale_x"]["Points"]) == 2
    assert len(compiled["scale_y"]["Points"]) == 2
    assert len(compiled["location_x"]["Points"]) == 2
    assert len(compiled["alpha"]["Points"]) == 2
    # the scale curve actually pushes in 1.0 -> 1.2 (values carried, not zeroed)
    scale_ys = [p["co"]["Y"] for p in compiled["scale_x"]["Points"]]
    assert scale_ys == pytest.approx([1.0, 1.2])


def test_abn_music_bed_ducking_envelope_round_trips_to_openshot_volume_curve():
    """A factory-attached ducking envelope on the music bed must survive import and
    compile to a multi-point OpenShot `volume` curve with its ABSOLUTE gains intact
    (not double-attenuated by the flat 0.22 seed). This is the 'volume/ducking not
    carried' half of the sweep finding, on the OpenShot path."""

    abn_timeline = {
        "episodeId": "ep_duck",
        "fps": 30,
        "totalSec": 6.0,
        "musicBed": {"src": "/agenticnews-assets/ep_duck/bed.wav"},
        # absolute-gain ducking envelope: 0.6 under the intro, 0.22 under VO.
        "musicBedKeyframes": [
            {"property": "volume", "points": [
                {"t": 0.0, "value": 0.6, "interp": "linear"},
                {"t": 2.0, "value": 0.22, "interp": "linear"},
            ]},
        ],
        "segments": [{"segmentId": "s0", "durationSec": 6.0, "shots": []}],
    }

    project = timeline.project_from_abn_timeline("proj_duck", abn_timeline)
    bed = next(c for c in project["clips"].values() if c["kind"] == "music_bed")

    # The flat seed is neutralized to 1.0 so the envelope's absolute gains pass
    # through OpenShot un-scaled (0.6 stays 0.6, not 0.6 * 0.22).
    assert bed["volume"] == pytest.approx(1.0)
    vol_track = next(t for t in bed["keyframes"] if t["property"] == "volume")
    assert [p["value"] for p in vol_track["points"]] == pytest.approx([0.6, 0.22])

    compiled = openshot_bridge.clip_json(project, bed)
    # OpenShot volume is the editor gain * 100; the curve keeps both points.
    vol_ys = [p["co"]["Y"] for p in compiled["volume"]["Points"]]
    assert len(vol_ys) == 2
    assert vol_ys == pytest.approx([60.0, 22.0])


# ---------------------------------------------------------------------------
# OpenShotRenderer._timeline serialization seam. Every other fake-openshot test
# (_build_fake_openshot_renderer) STUBS _timeline, so the real method --
#   json.dumps(openshot_bridge.timeline_json(project)) -> Timeline.SetJson
# -- is exercised by NOTHING on a box without libopenshot bindings (i.e. CI).
# That method is exactly where a translation gap bites: a keyframe envelope that
# won't json-serialize, a multi-clip crossfade transition whose two Fades land on
# the wrong layers, an effect that drops on the way into SetJson. These drive the
# REAL _timeline with a fake openshot module whose Timeline captures the JSON
# string handed to SetJson, on a COMPLEX multi-clip overlapping-crossfade project
# carrying volume/opacity/scale keyframe envelopes -- the exact "complex effects
# and keyframes" the render-path ticket flags. No bindings needed -> never skips.
# ---------------------------------------------------------------------------


class _CapturingTimelineModule:
    """A fake `openshot` module whose Timeline records the SetJson payload.

    Lets OpenShotRenderer._timeline run for real (it constructs Timeline(...),
    calls SetJson(json) and Open()) while capturing the serialized timeline JSON
    -- the precise bytes that would reach libopenshot -- without any bindings."""

    def __init__(self) -> None:
        captured: dict[str, str] = {}
        self.captured = captured

        class _Fraction:
            def __init__(self, num, den):
                self.num, self.den = num, den

        class _Timeline:
            def __init__(self, *_a, **_k):
                pass

            def SetJson(self, payload):
                captured["json"] = payload

            def Open(self):
                pass

            def Close(self):
                pass

        self.Fraction = _Fraction
        self.Timeline = _Timeline


def _crossfade_transition_project() -> dict:
    """Two overlapping graphics clips forming a crossfade transition (clip A fades
    OUT as clip B crossfades IN over the same 0.5s window), PLUS a ducking music bed
    carrying volume + a scale-pop + an opacity envelope. Every render dimension the
    ticket names is active across the timeline: a transition, and volume/opacity/
    scale keyframes."""
    project = timeline.new_project("xfade_tl", width=96, height=64, fps=12)
    project["assets"]["a"] = {"id": "a", "type": "image", "src": "/tmp/a.png", "metadata": {}}
    project["assets"]["b"] = {"id": "b", "type": "image", "src": "/tmp/b.png", "metadata": {}}
    project["assets"]["bed"] = {"id": "bed", "type": "audio", "src": "/tmp/bed.wav", "metadata": {}}
    # Clip A: graphics, t=0..2, fades out over its last 0.5s.
    project["clips"]["clip_a"] = {
        "id": "clip_a", "assetId": "a", "trackId": "graphics_1", "kind": "artifact",
        "start": 0.0, "duration": 2.0, "sourceStart": 0.0,
        "enabled": True, "muted": False, "volume": 1.0,
        "transform": {"x": 0.5, "y": 0.5, "scale": 1.0, "opacity": 1.0},
        "effects": [{"id": "fx_out", "type": "fadeOut", "params": {"duration": 0.5}}],
        "keyframes": [], "metadata": {},
    }
    # Clip B: graphics, t=1.5..3.5 (overlaps A's last 0.5s), crossfades in.
    project["clips"]["clip_b"] = {
        "id": "clip_b", "assetId": "b", "trackId": "graphics_1", "kind": "artifact",
        "start": 1.5, "duration": 2.0, "sourceStart": 0.0,
        "enabled": True, "muted": False, "volume": 1.0,
        "transform": {"x": 0.5, "y": 0.5, "scale": 1.0, "opacity": 1.0},
        "effects": [{"id": "fx_cf", "type": "crossfade", "params": {"duration": 0.5}}],
        "keyframes": [], "metadata": {},
    }
    # Music bed: audio, t=0..3.5, ducks under and animates scale+opacity (so the
    # serialized timeline carries all three envelope kinds at once).
    project["clips"]["bed_clip"] = {
        "id": "bed_clip", "assetId": "bed", "trackId": "music_1", "kind": "music_bed",
        "start": 0.0, "duration": 3.5, "sourceStart": 0.0,
        "enabled": True, "muted": False, "volume": 1.0,
        "transform": {"x": 0.5, "y": 0.5, "scale": 1.0, "opacity": 1.0},
        "effects": [],
        "keyframes": [
            {"property": "volume", "points": [
                {"t": 0.0, "value": 0.6, "interp": "linear"},
                {"t": 1.0, "value": 0.18, "interp": "constant"},
                {"t": 3.0, "value": 0.6, "interp": "bezier"},
            ]},
            {"property": "scale", "points": [
                {"t": 0.0, "value": 1.0, "interp": "linear"},
                {"t": 1.0, "value": 1.3, "interp": "linear"},
            ]},
            {"property": "opacity", "points": [
                {"t": 0.0, "value": 0.0, "interp": "linear"},
                {"t": 0.5, "value": 1.0, "interp": "linear"},
            ]},
        ],
        "metadata": {},
    }
    return project


def test_openshot_timeline_serializes_complex_crossfade_and_keyframes_to_setjson(tmp_path, monkeypatch):
    """The real OpenShotRenderer._timeline must serialize a complex multi-clip
    timeline (overlapping crossfade transition + volume/opacity/scale envelopes)
    into a well-formed Timeline JSON and hand it to SetJson. This is the prod
    SetJson hand-off no existing test reaches without libopenshot (they all stub
    _timeline). A non-finite keyframe, a non-serializable value, or a transition
    landing on the wrong layer would surface HERE, before the writer runs."""
    fake = _CapturingTimelineModule()
    monkeypatch.setattr(editor_render, "_import_openshot", lambda: (fake, "available", None))
    renderer = editor_render.OpenShotRenderer(tmp_path / "renders")

    project = _crossfade_transition_project()
    tl = renderer._timeline(project)
    assert tl is not None

    # SetJson got a real, parseable JSON string (the libopenshot contract). A
    # NaN/Infinity keyframe would make json.dumps emit a token json.loads rejects
    # under strict mode -- so parse strictly to pin finiteness at the seam.
    raw = fake.captured["json"]
    payload = json.loads(raw, parse_constant=lambda c: (_ for _ in ()).throw(
        AssertionError(f"non-finite token {c!r} reached SetJson")))

    # Top-level Timeline shape mirrors the project (the writer reads these).
    assert payload["type"] == "Timeline"
    assert payload["width"] == 96 and payload["height"] == 64
    assert payload["fps"] == {"num": 12, "den": 1}
    assert payload["duration"] == pytest.approx(3.5)  # bed clip ends last

    clips = {c["id"]: c for c in payload["clips"]}
    assert set(clips) == {"clip_a", "clip_b", "bed_clip"}

    # --- the crossfade TRANSITION: A fades OUT, B fades IN, over the overlap. ---
    (a_fade,) = clips["clip_a"]["effects"]
    (b_fade,) = clips["clip_b"]["effects"]
    assert a_fade["type"] == "Fade" and a_fade["fade"] == "out"
    assert b_fade["type"] == "Fade" and b_fade["fade"] == "in"   # crossfade -> in
    assert a_fade["duration"]["Points"][0]["co"]["Y"] == pytest.approx(0.5)
    assert b_fade["duration"]["Points"][0]["co"]["Y"] == pytest.approx(0.5)
    # The two transition clips share the graphics layer and overlap in time (the
    # defining property of a crossfade, not a hard cut).
    assert clips["clip_a"]["layer"] == clips["clip_b"]["layer"]
    a_end = clips["clip_a"]["position"] + clips["clip_a"]["duration"]
    assert clips["clip_b"]["position"] < a_end                    # overlap exists

    # --- all three keyframe envelopes survived onto the bed clip ---
    bed = clips["bed_clip"]
    # volume: 0..1 -> 0..100, interp linear/constant/bezier preserved, X = t*12+1.
    vol = bed["volume"]["Points"]
    assert [round(p["co"]["Y"], 2) for p in vol] == [60.0, 18.0, 60.0]
    assert [p["co"]["X"] for p in vol] == [1.0, 13.0, 37.0]
    assert [p["interpolation"] for p in vol] == [
        openshot_bridge.LINEAR, openshot_bridge.CONSTANT,
        openshot_bridge._INTERPOLATION_MAP["bezier"],
    ]
    # scale: one editor track drives BOTH axes (multi-point envelope, not flat).
    assert [p["co"]["Y"] for p in bed["scale_x"]["Points"]] == [1.0, 1.3]
    assert bed["scale_y"]["Points"] == bed["scale_x"]["Points"]
    assert len(bed["scale_x"]["Points"]) == 2
    # opacity envelope overrode the flat alpha default (2 points, clamped 0..1).
    assert [p["co"]["Y"] for p in bed["alpha"]["Points"]] == [0.0, 1.0]
    # the audio bed is tagged audio-only (has_audio=1, has_video=0) so OpenShot
    # mixes it rather than compositing a phantom video plane.
    assert bed["has_audio"]["Points"][0]["co"]["Y"] == 1
    assert bed["has_video"]["Points"][0]["co"]["Y"] == 0


def test_openshot_timeline_setjson_in_process_path_does_not_guard_nonfinite(tmp_path, monkeypatch):
    """Document where finiteness IS and ISN'T enforced. A poisoned (Infinity) volume
    keyframe serializes through the in-process _timeline as the bare JS `Infinity`
    literal -- the in-process SetJson path does NOT reject it. That guard lives on
    the SUBPROCESS path (OpenShotSubprocessRenderer._run_child uses allow_nan=False,
    pinned by test_openshot_subprocess_rejects_nonfinite_volume_keyframe_before_spawn),
    which is the sanctioned prod entrypoint via choose_renderer. This test pins the
    asymmetry so a future refactor that routes prod through the in-process path
    without re-adding the allow_nan guard fails loudly here instead of shipping a
    corrupted duck."""
    fake = _CapturingTimelineModule()
    monkeypatch.setattr(editor_render, "_import_openshot", lambda: (fake, "available", None))
    renderer = editor_render.OpenShotRenderer(tmp_path / "renders")

    project = _crossfade_transition_project()
    project["clips"]["bed_clip"]["keyframes"][0]["points"][1]["value"] = float("inf")

    tl = renderer._timeline(project)
    assert tl is not None
    raw = fake.captured["json"]
    # The in-process seam emits the non-finite value as the JS `Infinity` literal
    # (default json.dumps) -- it does NOT raise. A strict parser (parse_constant)
    # is what catches it; the subprocess boundary's allow_nan=False is the real gate.
    assert "Infinity" in raw
    with pytest.raises(AssertionError):
        json.loads(raw, parse_constant=lambda c: (_ for _ in ()).throw(
            AssertionError(f"non-finite token {c!r}")))


# ---------------------------------------------------------------------------
# Unit tests for _bake_fade_keyframes (pure-python, no render needed).
#
# Translates visual fade effects (fadeIn / crossfade / fadeOut) into an opacity
# keyframe ramp the OpenShot path can actually animate. Five code paths:
#   1. no fades              -> identity (same object)
#   2. fade_in only          -> 0->1 ramp at the head
#   3. fade_out only         -> 1->0 ramp at the tail
#   4. fade_in + fade_out    -> head ramp + tail ramp
#   5. fades exceeding clip  -> clamped to duration (no overlap / past-end points)
# ---------------------------------------------------------------------------


def _opacity_points(baked: dict) -> list[dict]:
    tracks = [t for t in (baked.get("keyframes") or []) if t.get("property") == "opacity"]
    assert len(tracks) == 1, "expected exactly one opacity track"
    return tracks[0]["points"]


def test_bake_fade_no_fades_is_identity():
    clip = {"id": "c1", "duration": 5.0, "effects": [{"type": "blur"}]}
    out = editor_render._bake_fade_keyframes(clip)
    # No visual fade present -> callers rely on the exact same object being returned.
    assert out is clip


def test_bake_fade_no_effects_at_all_is_identity():
    clip = {"id": "c1", "duration": 5.0}
    assert editor_render._bake_fade_keyframes(clip) is clip


def test_bake_fade_in_only():
    clip = {
        "id": "c1",
        "duration": 4.0,
        "effects": [{"type": "fadeIn", "params": {"duration": 1.0}}],
    }
    pts = _opacity_points(editor_render._bake_fade_keyframes(clip))
    assert pts == [
        {"t": 0.0, "value": 0.0, "interp": "linear"},
        {"t": 1.0, "value": 1.0, "interp": "linear"},
    ]


def test_bake_crossfade_counts_as_fade_in():
    clip = {
        "id": "c1",
        "duration": 4.0,
        "effects": [{"type": "crossfade", "params": {"duration": 0.5}}],
    }
    pts = _opacity_points(editor_render._bake_fade_keyframes(clip))
    assert pts[0] == {"t": 0.0, "value": 0.0, "interp": "linear"}
    assert pts[1] == {"t": 0.5, "value": 1.0, "interp": "linear"}


def test_bake_fade_out_only():
    clip = {
        "id": "c1",
        "duration": 4.0,
        "effects": [{"type": "fadeOut", "params": {"duration": 1.0}}],
    }
    pts = _opacity_points(editor_render._bake_fade_keyframes(clip))
    assert pts == [
        {"t": 3.0, "value": 1.0, "interp": "linear"},
        {"t": 4.0, "value": 0.0, "interp": "linear"},
    ]


def test_bake_fade_in_and_out():
    clip = {
        "id": "c1",
        "duration": 10.0,
        "effects": [
            {"type": "fadeIn", "params": {"duration": 2.0}},
            {"type": "fadeOut", "params": {"duration": 3.0}},
        ],
    }
    pts = _opacity_points(editor_render._bake_fade_keyframes(clip))
    assert pts == [
        {"t": 0.0, "value": 0.0, "interp": "linear"},
        {"t": 2.0, "value": 1.0, "interp": "linear"},
        {"t": 7.0, "value": 1.0, "interp": "linear"},
        {"t": 10.0, "value": 0.0, "interp": "linear"},
    ]


def test_bake_fade_durations_exceed_clip_are_clamped():
    # Both fades individually exceed the clip; each clamps to the full duration and
    # out_start is held at >= fade_in so the ramps can't cross or emit a point > duration.
    clip = {
        "id": "c1",
        "duration": 2.0,
        "effects": [
            {"type": "fadeIn", "params": {"duration": 5.0}},
            {"type": "fadeOut", "params": {"duration": 5.0}},
        ],
    }
    pts = _opacity_points(editor_render._bake_fade_keyframes(clip))
    times = [p["t"] for p in pts]
    assert all(0.0 <= t <= 2.0 for t in times)
    assert times == sorted(times)
    # fade_in clamped to full duration -> head ramp ends exactly at the clip end.
    assert pts[0] == {"t": 0.0, "value": 0.0, "interp": "linear"}
    assert pts[1] == {"t": 2.0, "value": 1.0, "interp": "linear"}


def test_bake_fade_replaces_existing_opacity_track():
    clip = {
        "id": "c1",
        "duration": 4.0,
        "effects": [{"type": "fadeIn", "params": {"duration": 1.0}}],
        "keyframes": [
            {"property": "opacity", "points": [{"t": 0.0, "value": 0.5}]},
            {"property": "scale", "points": [{"t": 0.0, "value": 1.0}]},
        ],
    }
    out = editor_render._bake_fade_keyframes(clip)
    # original clip is not mutated
    assert len(clip["keyframes"]) == 2
    props = [t["property"] for t in out["keyframes"]]
    assert props.count("opacity") == 1  # stale opacity track dropped, fade one added
    assert "scale" in props  # non-opacity tracks preserved
    assert _opacity_points(out)[0] == {"t": 0.0, "value": 0.0, "interp": "linear"}


def test_bake_fade_zero_duration_fade_is_identity():
    clip = {
        "id": "c1",
        "duration": 4.0,
        "effects": [{"type": "fadeIn", "params": {"duration": 0.0}}],
    }
    # A fade effect with zero duration produces no ramp -> identity.
    assert editor_render._bake_fade_keyframes(clip) is clip


# --- bezier keyframe parity: ffmpeg fallback vs OpenShot's cubic Bezier ---------

# Ground truth: libopenshot's default BEZIER interpolation for a 0->1 ramp over a
# 10-frame span (Keyframe.AddPoint(1,0,LINEAR); AddPoint(11,1,BEZIER); GetValue
# at frames 1..11). Captured from the bundled libopenshot binding so the test does
# NOT depend on the fragile native runtime importing in CI. This is the symmetric
# ease-in-out cubic Bezier with x-handles at 0.5, i.e. x(u)=1.5u-1.5u^2+u^3,
# y(u)=3u^2-2u^3 -- which is NOT smoothstep(3s^2-2s^3).
_OPENSHOT_BEZIER_RAMP = [
    0.0, 0.014912, 0.064076, 0.160667, 0.308193, 0.5,
    0.691807, 0.839333, 0.935924, 0.985088, 1.0,
]


def _eval_ffmpeg_time_expr(expr: str, t: float) -> float:
    """Evaluate the subset of ffmpeg expression syntax our keyframe generator
    emits -- nested `if(lt(...),a,b)` over arithmetic -- the way ffmpeg's
    eval=frame would. Lets us assert the rendered curve numerically without
    spawning ffmpeg."""
    py = expr.replace("if(", "IF(").replace("lt(", "LT(")
    return eval(  # noqa: S307 - generated, trusted arithmetic only
        py,
        {"__builtins__": {}},
        {"t": t, "IF": lambda c, a, b: a if c else b, "LT": lambda a, b: a < b},
    )


def test_bezier_envelope_matches_openshot_cubic_not_smoothstep():
    """An envelope point with `bezier` interp must render the SAME cubic Bezier
    curve in the ffmpeg fallback that openshot_bridge gets from libopenshot
    (BEZIER=0). The old smoothstep stand-in (3s^2-2s^3) diverges from libopenshot's
    default ease-in-out by up to ~0.056 mid-segment, so a bezier volume-ducking
    envelope rendered an audibly different curve in the two backends with nothing
    guarding parity. The Newton-refined expression must track the real curve to a
    small tolerance AND beat smoothstep's worst-case error decisively."""
    expr = editor_render._piecewise_linear_expr(
        [(0.0, 0.0), (10.0, 1.0, "bezier")], var="t"
    )

    errs = []
    smoothstep_errs = []
    for i, ref in enumerate(_OPENSHOT_BEZIER_RAMP):
        t = float(i)
        got = _eval_ffmpeg_time_expr(expr, t)
        errs.append(abs(got - ref))
        s = t / 10.0
        smoothstep_errs.append(abs((3 * s * s - 2 * s * s * s) - ref))

    # The fallback now tracks libopenshot's cubic Bezier closely.
    assert max(errs) < 0.005, f"bezier parity drift too large: {errs}"
    # Endpoints are exact (they are the keyframe values themselves).
    assert errs[0] == 0.0 and errs[-1] == 0.0
    # Regression guard: smoothstep was off by >0.05; we are an order better.
    assert max(smoothstep_errs) > 0.05  # documents the old bug magnitude
    assert max(errs) < max(smoothstep_errs) / 10


def test_bezier_envelope_is_distinct_from_linear_and_constant():
    """Sanity: the three interpolations produce genuinely different mid-segment
    values, so `bezier` is not silently collapsing to linear/constant in the
    ffmpeg fallback (the failure mode the parity bug masked)."""
    mid_t = 2.5  # quarter-ish into a 0..10 ramp where the curves separate
    lin = editor_render._piecewise_linear_expr([(0.0, 0.0), (10.0, 1.0, "linear")], var="t")
    bez = editor_render._piecewise_linear_expr([(0.0, 0.0), (10.0, 1.0, "bezier")], var="t")
    con = editor_render._piecewise_linear_expr([(0.0, 0.0), (10.0, 1.0, "constant")], var="t")

    v_lin = _eval_ffmpeg_time_expr(lin, mid_t)
    v_bez = _eval_ffmpeg_time_expr(bez, mid_t)
    v_con = _eval_ffmpeg_time_expr(con, mid_t)

    assert abs(v_lin - v_bez) > 0.05, "bezier collapsed to linear"
    assert v_con == 0.0, "constant must hold the start value before the end point"
    assert v_bez != v_lin and v_bez != v_con
