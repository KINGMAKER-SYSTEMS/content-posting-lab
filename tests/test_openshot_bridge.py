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
