import json
import subprocess
from pathlib import Path

from services import editor_timeline
from services import openshot_bridge

REPO_ROOT = Path(__file__).resolve().parent.parent


def _project(asset_path: Path | str) -> dict:
    project = editor_timeline.new_project("ep_real", width=1920, height=1080, fps=30)
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
        "start": 1.5,
        "duration": 2.0,
        "sourceStart": 0.25,
        "enabled": True,
        "muted": False,
        "volume": 1.0,
        "transform": {"x": 0.25, "y": 0.75, "scale": 0.8, "opacity": 0.9},
        "effects": [],
        "keyframes": [],
        "metadata": {},
    }
    return project


def test_editor_project_exports_openshot_timeline_json(tmp_path):
    project = _project(tmp_path / "card.png")

    exported = openshot_bridge.timeline_json(project)

    assert exported["type"] == "Timeline"
    assert exported["width"] == 1920
    assert exported["height"] == 1080
    assert exported["fps"] == {"num": 30, "den": 1}
    assert exported["duration"] == 3.5
    assert len(exported["clips"]) == 1

    clip = exported["clips"][0]
    assert clip["id"] == "card_clip"
    assert clip["position"] == 1.5
    assert clip["start"] == 0.25
    assert clip["end"] == 2.25
    assert clip["layer"] == 20
    assert clip["reader"]["type"] == "QtImageReader"
    assert clip["reader"]["path"].endswith("card.png")
    assert clip["scale_x"]["Points"][0]["co"]["Y"] == 0.8
    assert clip["location_x"]["Points"][0]["co"]["Y"] == -0.5
    assert clip["location_y"]["Points"][0]["co"]["Y"] == 0.5
    assert clip["alpha"]["Points"][0]["co"]["Y"] == 0.9


def test_openshot_export_resolves_agenticnews_asset_urls(tmp_path):
    project = _project("/agenticnews-assets/card.png")

    exported = openshot_bridge.timeline_json(project, asset_root=tmp_path)

    assert exported["clips"][0]["reader"]["path"] == str(tmp_path / "card.png")


def test_openshot_export_maps_editor_volume_to_openshot_percent(tmp_path):
    project = _project(tmp_path / "vo.wav")
    project["assets"]["card"]["type"] = "audio"
    project["clips"]["card_clip"]["trackId"] = "audio_1"
    project["clips"]["card_clip"]["kind"] = "voiceover"
    project["clips"]["card_clip"]["volume"] = 1.0

    exported = openshot_bridge.timeline_json(project)

    clip = exported["clips"][0]
    assert clip["volume"]["Points"][0]["co"]["Y"] == 100.0
    assert clip["has_audio"]["Points"][0]["co"]["Y"] == 1
    assert clip["has_video"]["Points"][0]["co"]["Y"] == 0


def test_openshot_export_preserves_quiet_music_bed_volume(tmp_path):
    project = _project(tmp_path / "bed.wav")
    project["assets"]["card"]["type"] = "audio"
    project["clips"]["card_clip"]["trackId"] = "music_1"
    project["clips"]["card_clip"]["kind"] = "music_bed"
    project["clips"]["card_clip"]["volume"] = 0.18

    exported = openshot_bridge.timeline_json(project)

    assert exported["clips"][0]["volume"]["Points"][0]["co"]["Y"] == 18.0


def test_openshot_export_duration_ignores_disabled_tail_clip(tmp_path):
    project = _project(tmp_path / "card.png")
    project["clips"]["disabled_tail"] = {
        **project["clips"]["card_clip"],
        "id": "disabled_tail",
        "start": 100.0,
        "duration": 50.0,
        "enabled": False,
    }

    exported = openshot_bridge.timeline_json(project)

    assert exported["duration"] == 3.5
    assert [clip["id"] for clip in exported["clips"]] == ["card_clip"]


def test_command_log_exports_openshot_apply_json_diff(tmp_path):
    project = _project(tmp_path / "card.png")
    command = {
        "id": "cmd_transform",
        "op": "clip.transform",
        "actor": "test",
        "expectedRevision": 0,
        "payload": {
            "clipId": "card_clip",
            "transform": {"x": 0.5, "y": 0.5, "scale": 1.0, "opacity": 1.0},
        },
    }
    project = editor_timeline.apply_command(project, command)

    actions = openshot_bridge.flattened_update_actions(project)

    assert actions == [
        {
            "type": "update",
            "key": ["clips", {"id": "card_clip"}],
            "value": actions[0]["value"],
            "old_values": actions[0]["old_values"],
            "transaction": "cmd_transform",
        }
    ]
    assert actions[0]["value"]["location_x"]["Points"][0]["co"]["Y"] == 0.0
    assert actions[0]["value"]["location_y"]["Points"][0]["co"]["Y"] == 0.0
    assert actions[0]["value"]["scale_x"]["Points"][0]["co"]["Y"] == 1.0
    assert actions[0]["old_values"]["scale_x"]["Points"][0]["co"]["Y"] == 0.8

    diff = json.loads(openshot_bridge.json_diff(project))
    assert diff[0]["type"] == "update"
    assert diff[0]["key"] == ["clips", {"id": "card_clip"}]


def test_clip_keyframe_envelope_translates_to_multipoint_openshot_keyframes(tmp_path):
    """A volume-ducking envelope on the clip becomes a multi-Point OpenShot
    keyframe (frame X = t*fps+1, Y scaled to 0..100), overriding the flat default."""
    project = _project(tmp_path / "bed.wav")
    project["assets"]["card"]["type"] = "audio"
    project["clips"]["card_clip"]["trackId"] = "music_1"
    project["clips"]["card_clip"]["kind"] = "music_bed"
    project["clips"]["card_clip"]["keyframes"] = [
        {
            "property": "volume",
            "points": [
                {"t": 0.0, "value": 1.0, "interp": "linear"},
                {"t": 1.0, "value": 0.22, "interp": "constant"},
                {"t": 2.0, "value": 1.0, "interp": "linear"},
            ],
        }
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    points = clip["volume"]["Points"]
    # X = (sourceStart 0.25 + t) * 30fps + 1 — trimmed clips offset by sourceStart
    assert [p["co"]["X"] for p in points] == [8.5, 38.5, 68.5]
    assert [round(p["co"]["Y"], 2) for p in points] == [100.0, 22.0, 100.0]  # 0..1 -> 0..100
    assert points[0]["interpolation"] == openshot_bridge.LINEAR
    assert points[1]["interpolation"] == openshot_bridge.CONSTANT


def test_keyframe_X_offsets_by_sourceStart_for_trimmed_clips(tmp_path):
    """A keyframe at editor t maps to source frame (sourceStart + t)*fps + 1.
    A clip trimmed to sourceStart=5.0 must fire its t=1.0 keyframe at frame 181,
    not 31, so the envelope lands at the right source frame."""
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["sourceStart"] = 5.0
    project["clips"]["card_clip"]["keyframes"] = [
        {"property": "opacity", "points": [{"t": 0.0, "value": 0.0}, {"t": 1.0, "value": 1.0}]},
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    # (5.0 + 0.0)*30 + 1 = 151 ; (5.0 + 1.0)*30 + 1 = 181
    assert [p["co"]["X"] for p in clip["alpha"]["Points"]] == [151.0, 181.0]


def test_keyframe_X_with_no_trim_keeps_plain_t_times_fps(tmp_path):
    """sourceStart=0 (untrimmed) keeps the original t*fps + 1 mapping."""
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["sourceStart"] = 0.0
    project["clips"]["card_clip"]["keyframes"] = [
        {"property": "opacity", "points": [{"t": 0.0, "value": 0.0}, {"t": 1.0, "value": 1.0}]},
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    assert [p["co"]["X"] for p in clip["alpha"]["Points"]] == [1.0, 31.0]


def test_clip_keyframe_opacity_and_scale_map_to_alpha_and_both_scale_axes(tmp_path):
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["keyframes"] = [
        {"property": "opacity", "points": [{"t": 0.0, "value": 0.0}, {"t": 0.5, "value": 1.0}]},
        {"property": "scale", "points": [{"t": 0.0, "value": 1.0}, {"t": 0.5, "value": 1.5}]},
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    assert [p["co"]["Y"] for p in clip["alpha"]["Points"]] == [0.0, 1.0]
    # scale animates BOTH axes from one editor track
    assert [p["co"]["Y"] for p in clip["scale_x"]["Points"]] == [1.0, 1.5]
    assert clip["scale_y"]["Points"] == clip["scale_x"]["Points"]


def test_empty_keyframes_keep_the_flat_default_keyframe(tmp_path):
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["keyframes"] = []

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    assert clip["scale_x"]["Points"] == [{"co": {"X": 1.0, "Y": 0.8}, "interpolation": openshot_bridge.CONSTANT}]


def test_clip_keyframe_position_and_rotation_envelopes_and_bezier_interp(tmp_path):
    """x/y position envelopes go through the same 0..1 -> -1..1 _location transform
    as the flat default, rotation passes through, and a `bezier` interp point maps
    to OpenShot's BEZIER code (0). These props + interp had no envelope coverage —
    a regression in the per-property transform or interp map would only bite at
    render time."""
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["keyframes"] = [
        {"property": "x", "points": [{"t": 0.0, "value": 0.0}, {"t": 1.0, "value": 1.0}]},
        {"property": "y", "points": [{"t": 0.0, "value": 0.5, "interp": "bezier"}]},
        {"property": "rotation", "points": [{"t": 0.0, "value": 0.0}, {"t": 1.0, "value": 90.0}]},
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    # x: 0.0 -> -1.0 (left edge), 1.0 -> 1.0 (right edge); frames are t*30 + 1
    assert [p["co"]["X"] for p in clip["location_x"]["Points"]] == [1.0, 31.0]
    assert [p["co"]["Y"] for p in clip["location_x"]["Points"]] == [-1.0, 1.0]
    # y: 0.5 -> 0.0 (center), single bezier point
    assert clip["location_y"]["Points"][0]["co"]["Y"] == 0.0
    assert clip["location_y"]["Points"][0]["interpolation"] == openshot_bridge._INTERPOLATION_MAP["bezier"]
    # rotation passes through untransformed
    assert [p["co"]["Y"] for p in clip["rotation"]["Points"]] == [0.0, 90.0]


def test_clip_effects_translate_to_openshot_effect_objects(tmp_path):
    """A clip's editor effects become libopenshot Effect JSON objects on the clip:
    fades map to the Fade class with a direction + keyframed duration; color filters
    map to their own effect class. Empty effects stay an empty list."""
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["effects"] = [
        {"id": "fx1", "type": "fadeIn", "params": {"duration": 0.5}},
        {"id": "fx2", "type": "brightness", "params": {"value": -0.3}},
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    assert [e["id"] for e in clip["effects"]] == ["fx1", "fx2"]
    fade, bright = clip["effects"]
    assert fade["type"] == "Fade"
    assert fade["fade"] == "in"
    assert fade["duration"]["Points"][0]["co"]["Y"] == 0.5
    assert bright["type"] == "Brightness"
    assert bright["brightness"]["Points"][0]["co"]["Y"] == -0.3


def test_clip_with_no_effects_exports_empty_effects_list(tmp_path):
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["effects"] = []

    assert openshot_bridge.timeline_json(project)["clips"][0]["effects"] == []


def test_crossfade_transition_exports_as_inward_fade(tmp_path):
    """A crossfade transition carries into OpenShot as the Fade class with an
    inward direction and a keyframed duration — the clip-boundary compositing
    that the abn_factory music-bed ducking / cut-stitching depends on."""
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["effects"] = [
        {"id": "fxX", "type": "crossfade", "params": {"duration": 0.75}},
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    (xfade,) = clip["effects"]
    assert xfade["id"] == "fxX"
    assert xfade["type"] == "Fade"
    assert xfade["fade"] == "in"
    assert xfade["duration"]["Points"][0]["co"]["Y"] == 0.75


def test_saturation_effect_exports_as_saturation_class(tmp_path):
    """Saturation is the second color filter in the closed effect vocabulary; it
    maps to its own OpenShot Saturation class with a keyframed value."""
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["effects"] = [
        {"id": "sat1", "type": "saturation", "params": {"value": 1.4}},
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    (sat,) = clip["effects"]
    assert sat["type"] == "Saturation"
    assert sat["saturation"]["Points"][0]["co"]["Y"] == 1.4


def test_keyframe_position_and_rotation_envelopes_carry_their_transforms(tmp_path):
    """An x/y position envelope animates location_x/location_y through the same
    centered ((v-0.5)*2) transform the flat default uses, and a rotation envelope
    animates rotation 1:1 — so a panning/spinning clip round-trips into OpenShot."""
    project = _project(tmp_path / "card.png")
    project["clips"]["card_clip"]["keyframes"] = [
        {"property": "x", "points": [{"t": 0.0, "value": 0.0}, {"t": 1.0, "value": 1.0}]},
        {"property": "y", "points": [{"t": 0.0, "value": 0.5}, {"t": 1.0, "value": 0.25}]},
        {"property": "rotation", "points": [{"t": 0.0, "value": 0.0}, {"t": 1.0, "value": 90.0}]},
    ]

    clip = openshot_bridge.timeline_json(project)["clips"][0]

    # x: 0.0 -> -1.0, 1.0 -> 1.0  (centered then doubled)
    assert [p["co"]["Y"] for p in clip["location_x"]["Points"]] == [-1.0, 1.0]
    # y: 0.5 -> 0.0, 0.25 -> -0.5
    assert [p["co"]["Y"] for p in clip["location_y"]["Points"]] == [0.0, -0.5]
    # rotation passes through unchanged
    assert [p["co"]["Y"] for p in clip["rotation"]["Points"]] == [0.0, 90.0]
    # frame X = t*fps + 1 for every envelope
    assert [p["co"]["X"] for p in clip["location_x"]["Points"]] == [1.0, 31.0]


def test_effect_add_command_exports_update_action_with_effects(tmp_path):
    """clip.effect.add flows through the generic clip.* update path: the resulting
    OpenShot UpdateAction carries the full clip JSON with the new effects array."""
    project = _project(tmp_path / "card.png")
    project = editor_timeline.apply_command(
        project,
        {
            "id": "cmd_fx",
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"clipId": "card_clip", "effect": {"id": "fx1", "type": "fadeOut", "params": {"duration": 1.0}}},
        },
    )

    actions = openshot_bridge.flattened_update_actions(project)
    fx_action = next(a for a in actions if a["transaction"] == "cmd_fx")
    assert fx_action["type"] == "update"
    assert fx_action["key"] == ["clips", {"id": "card_clip"}]
    effects = fx_action["value"]["effects"]
    assert effects[0]["type"] == "Fade"
    assert effects[0]["fade"] == "out"


def test_unsplit_exports_update_and_delete_actions(tmp_path):
    project = _project(tmp_path / "card.png")
    project = editor_timeline.apply_command(
        project,
        {
            "id": "cmd_split",
            "op": "clip.split",
            "actor": "human",
            "expectedRevision": 0,
            "payload": {
                "clipId": "card_clip",
                "at": 2.5,
                "newClipId": "card_clip_b",
            },
        },
    )
    project = editor_timeline.apply_command(
        project,
        {
            "id": "cmd_unsplit",
            "op": "clip.unsplit",
            "actor": "human",
            "expectedRevision": 1,
            "revertsCommandId": "cmd_split",
            "payload": {
                "clipId": "card_clip",
                "createdClipId": "card_clip_b",
                "clip": project["commandLog"][0]["before"]["clip"],
            },
        },
    )

    actions = openshot_bridge.flattened_update_actions(project)
    unsplit_actions = [
        action for action in actions if action["transaction"] == "cmd_unsplit"
    ]

    assert [action["type"] for action in unsplit_actions] == ["update", "delete"]
    assert unsplit_actions[0]["key"] == ["clips", {"id": "card_clip"}]
    assert unsplit_actions[1]["key"] == ["clips", {"id": "card_clip_b"}]
    assert unsplit_actions[1]["old_values"]["id"] == "card_clip_b"


# ---------------------------------------------------------------------------
# Isolated regression coverage for update_action_from_command and
# update_actions_from_command_log — the command->UpdateAction mapping that the
# editor-render pipeline relies on. Previously only exercised transitively via
# flattened_update_actions.
# ---------------------------------------------------------------------------


def test_command_create_maps_to_insert_action(tmp_path):
    project = _project(tmp_path / "card.png")
    new_clip = {**project["clips"]["card_clip"], "id": "card_clip_new"}
    entry = {"id": "cmd_create", "op": "clip.create", "after": {"clip": new_clip}}

    action = openshot_bridge.update_action_from_command(project, entry)

    assert action["type"] == "insert"
    assert action["key"] == ["clips"]
    assert action["old_values"] is None
    assert action["transaction"] == "cmd_create"
    assert action["value"]["id"] == "card_clip_new"


def test_generic_clip_command_maps_to_update_action(tmp_path):
    project = _project(tmp_path / "card.png")
    after = {**project["clips"]["card_clip"], "duration": 4.0}
    entry = {
        "id": "cmd_resize",
        "op": "clip.resize",
        "before": {"clip": project["clips"]["card_clip"]},
        "after": {"clip": after},
    }

    action = openshot_bridge.update_action_from_command(project, entry)

    assert action["type"] == "update"
    assert action["key"] == ["clips", {"id": "card_clip"}]
    assert action["value"]["duration"] == 4.0
    assert action["old_values"]["duration"] == 2.0
    assert action["transaction"] == "cmd_resize"


def test_split_command_returns_unflattened_batch(tmp_path):
    project = _project(tmp_path / "card.png")
    left = {**project["clips"]["card_clip"], "duration": 1.0}
    right = {**project["clips"]["card_clip"], "id": "card_clip_b", "duration": 1.0}
    entry = {
        "id": "cmd_split",
        "op": "clip.split",
        "before": {"clip": project["clips"]["card_clip"]},
        "after": {"clip": left, "createdClip": right},
    }

    action = openshot_bridge.update_action_from_command(project, entry)

    # Split is a batch (not flattened here) — update existing + insert new.
    assert set(action) == {"batch"}
    assert [a["type"] for a in action["batch"]] == ["update", "insert"]
    assert action["batch"][0]["key"] == ["clips", {"id": "card_clip"}]
    assert action["batch"][1]["value"]["id"] == "card_clip_b"


def test_non_clip_commands_return_none(tmp_path):
    project = _project(tmp_path / "card.png")

    # Markers/notes and other non-timeline ops are intentionally ignored.
    assert openshot_bridge.update_action_from_command(
        project, {"id": "n1", "op": "note.add", "after": {"text": "hi"}}
    ) is None
    assert openshot_bridge.update_action_from_command(
        project, {"id": "m1", "op": "marker.add", "after": {}}
    ) is None
    # A clip op missing its clip payload yields nothing.
    assert openshot_bridge.update_action_from_command(
        project, {"id": "x1", "op": "clip.transform", "after": {}}
    ) is None
    # Empty entry doesn't raise.
    assert openshot_bridge.update_action_from_command(project, {}) is None


def test_command_log_keeps_split_batches_unflattened(tmp_path):
    project = _project(tmp_path / "card.png")
    project = editor_timeline.apply_command(
        project,
        {
            "id": "cmd_split",
            "op": "clip.split",
            "actor": "human",
            "expectedRevision": 0,
            "payload": {"clipId": "card_clip", "at": 2.5, "newClipId": "card_clip_b"},
        },
    )

    raw = openshot_bridge.update_actions_from_command_log(project)
    flat = openshot_bridge.flattened_update_actions(project)

    # The split surfaces as a single batch entry pre-flattening...
    assert len(raw) == 1
    assert "batch" in raw[0]
    # ...and as two discrete actions once flattened.
    assert len(flat) == 2
    assert [a["type"] for a in flat] == ["update", "insert"]


# ── repo-hygiene guard ────────────────────────────────────────────────────────
# openshot_bridge.py is imported at app load (routers/agenticnews.py). It was once
# untracked and nearly lost; a clean checkout that's missing it raises
# ModuleNotFoundError at startup. prod-cycle.js (lines 107/151) explicitly guards
# for this on every merge. These tests pin the invariant so it can't silently regress.

def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def test_openshot_bridge_is_git_tracked():
    """openshot_bridge.py must be committed — the app imports it at load time."""
    tracked = _git("ls-files", "--error-unmatch", "services/openshot_bridge.py")
    assert tracked.returncode == 0, (
        "services/openshot_bridge.py is NOT git-tracked — a clean checkout will "
        f"ModuleNotFoundError on import.\n{tracked.stderr}"
    )


def test_openshot_bridge_is_not_gitignored():
    """It must NOT be ignored — an ignored module silently drops from clean clones."""
    ignored = _git("check-ignore", "-v", "services/openshot_bridge.py")
    # git check-ignore exits 0 (and prints the matching rule) when a path IS ignored.
    assert ignored.returncode != 0, (
        "services/openshot_bridge.py matches a .gitignore rule:\n"
        f"{ignored.stdout}"
    )


def test_app_imported_python_modules_are_all_tracked():
    """Generalize the openshot_bridge lesson: every services/ + routers/ module the
    app can import at runtime must be tracked, so a clean checkout never raises
    ModuleNotFoundError. Catches the whole class of bug, not just one file."""
    candidates = sorted(
        p for d in ("services", "routers")
        for p in (REPO_ROOT / d).rglob("*.py")
        if "__pycache__" not in p.parts
    )
    rel = [str(p.relative_to(REPO_ROOT)) for p in candidates]
    assert rel, "no python modules found under services/ or routers/ — wrong repo root?"

    listed = _git("ls-files", "--", *rel)
    tracked = set(listed.stdout.splitlines())
    untracked = [p for p in rel if p not in tracked]
    assert not untracked, (
        "app-imported python modules are NOT git-tracked (clean checkout would "
        f"ModuleNotFoundError): {untracked}"
    )
