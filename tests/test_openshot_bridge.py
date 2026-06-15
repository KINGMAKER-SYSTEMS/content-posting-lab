import json
from pathlib import Path

from services import editor_timeline
from services import openshot_bridge


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
    assert [p["co"]["X"] for p in points] == [1.0, 31.0, 61.0]  # t*30fps + 1
    assert [round(p["co"]["Y"], 2) for p in points] == [100.0, 22.0, 100.0]  # 0..1 -> 0..100
    assert points[0]["interpolation"] == openshot_bridge.LINEAR
    assert points[1]["interpolation"] == openshot_bridge.CONSTANT


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
