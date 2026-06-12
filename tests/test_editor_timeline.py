import pytest

from services import editor_timeline as timeline


def test_imports_abn_timeline_as_atomic_assets_clips_and_tracks(tmp_path):
    abn_timeline = {
        "episodeId": "ep_fixture",
        "title": "Fixture Episode",
        "fps": 30,
        "width": 1920,
        "height": 1080,
        "totalSec": 8.0,
        "segments": [
            {
                "segmentId": "s0",
                "title": "Hook",
                "durationSec": 5.0,
                "shots": [
                    {
                        "id": "shot_a",
                        "src": "/agenticnews-assets/card_a.png",
                        "startSec": 1.0,
                        "durationSec": 2.0,
                        "type": "artifact",
                    }
                ],
                "audio": {"vo": {"src": "/agenticnews-assets/vo_s0.wav", "duration": 5.0}},
                "lowerThirds": [
                    {"startSec": 0.5, "durationSec": 2.5, "headline": "Hook lower"}
                ],
            },
            {
                "segmentId": "s1",
                "title": "Proof",
                "durationSec": 3.0,
                "shots": [
                    {
                        "src": "/agenticnews-assets/demo.mp4",
                        "startSec": 0.25,
                        "durationSec": 1.5,
                        "type": "demo",
                    }
                ],
                "audio": {"vo": {"src": "/agenticnews-assets/vo_s1.wav", "duration": 3.0}},
            },
        ],
    }

    project = timeline.project_from_abn_timeline(
        "proj_ep_fixture", abn_timeline, source_episode_id="ep_fixture"
    )

    assert project["projectId"] == "proj_ep_fixture"
    assert project["revision"] == 0
    assert set(project["tracks"]) >= {"video_1", "graphics_1", "audio_1", "titles_1"}
    assert any(a["src"] == "/agenticnews-assets/card_a.png" for a in project["assets"].values())
    assert any(c["start"] == 1.0 and c["duration"] == 2.0 for c in project["clips"].values())
    assert any(c["start"] == 5.25 and c["duration"] == 1.5 for c in project["clips"].values())
    assert any(c["kind"] == "lower_third" for c in project["clips"].values())

    store = timeline.TimelineStore(tmp_path)
    saved = store.save(project)
    assert saved == project
    loaded = store.load("proj_ep_fixture")
    assert loaded["projectId"] == "proj_ep_fixture"


def test_commands_mutate_only_through_revision_checked_command_log(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_cmd")
    store.save(project)

    project = store.apply_command(
        "proj_cmd",
        {
            "op": "asset.import",
            "actor": "test-agent",
            "expectedRevision": 0,
            "payload": {
                "assetId": "asset_card",
                "type": "image",
                "src": "/agenticnews-assets/card.png",
            },
        },
    )
    project = store.apply_command(
        "proj_cmd",
        {
            "op": "clip.create",
            "actor": "test-agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "clip_card",
                "assetId": "asset_card",
                "trackId": "graphics_1",
                "start": 12.0,
                "duration": 3.0,
            },
        },
    )
    project = store.apply_command(
        "proj_cmd",
        {
            "op": "clip.move",
            "actor": "human",
            "expectedRevision": 2,
            "payload": {"clipId": "clip_card", "start": 12.5},
        },
    )

    assert project["revision"] == 3
    assert project["clips"]["clip_card"]["start"] == 12.5
    assert [c["op"] for c in project["commandLog"]] == [
        "asset.import",
        "clip.create",
        "clip.move",
    ]
    assert project["commandLog"][2]["actor"] == "human"
    assert project["commandLog"][2]["revision"] == 3

    with pytest.raises(timeline.RevisionConflict):
        store.apply_command(
            "proj_cmd",
            {
                "op": "clip.move",
                "actor": "late-agent",
                "expectedRevision": 2,
                "payload": {"clipId": "clip_card", "start": 13.0},
            },
        )


def test_replay_rebuilds_state_and_revert_appends_inverse_command(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_replay")
    store.save(project)
    for command in [
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
        },
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 1.0,
                "duration": 2.0,
            },
        },
        {
            "op": "clip.trim",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "start": 1.25, "duration": 1.5},
        },
    ]:
        project = store.apply_command("proj_replay", command)

    rebuilt = timeline.replay_project(project)
    assert rebuilt["clips"] == project["clips"]
    assert rebuilt["assets"] == project["assets"]
    assert rebuilt["revision"] == project["revision"]

    reverted = store.revert_last_command("proj_replay", actor="human")
    assert reverted["revision"] == 4
    assert reverted["clips"]["c1"]["start"] == 1.0
    assert reverted["clips"]["c1"]["duration"] == 2.0
    assert reverted["commandLog"][-1]["op"] == "clip.update"


def test_split_marker_note_and_property_commands(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_atomic")
    store.save(project)
    commands = [
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
        },
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 10.0,
                "duration": 6.0,
                "sourceStart": 2.0,
            },
        },
        {
            "op": "clip.split",
            "actor": "human",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "at": 12.5, "newClipId": "c1_b"},
        },
        {
            "op": "clip.opacity",
            "actor": "human",
            "expectedRevision": 3,
            "payload": {"clipId": "c1_b", "opacity": 0.4},
        },
        {
            "op": "marker.add",
            "actor": "agent",
            "expectedRevision": 4,
            "payload": {"markerId": "m1", "time": 12.5, "label": "split review"},
        },
        {
            "op": "note.add",
            "actor": "agent",
            "expectedRevision": 5,
            "payload": {
                "noteId": "n1",
                "target": {"clipId": "c1_b", "time": 12.5},
                "text": "Check the post-split beat.",
                "suggestedCommand": {
                    "op": "clip.move",
                    "payload": {"clipId": "c1_b", "start": 12.75},
                },
            },
        },
    ]
    for command in commands:
        project = store.apply_command("proj_atomic", command)

    assert project["clips"]["c1"]["start"] == 10.0
    assert project["clips"]["c1"]["duration"] == 2.5
    assert project["clips"]["c1_b"]["start"] == 12.5
    assert project["clips"]["c1_b"]["duration"] == 3.5
    assert project["clips"]["c1_b"]["sourceStart"] == 4.5
    assert project["clips"]["c1_b"]["transform"]["opacity"] == 0.4
    assert project["markers"]["m1"]["label"] == "split review"
    assert project["notes"]["n1"]["suggestedCommand"]["op"] == "clip.move"


def test_invalid_commands_are_rejected_without_mutating_state(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    store.save(timeline.new_project("proj_invalid"))

    with pytest.raises(timeline.CommandValidationError):
        store.apply_command(
            "proj_invalid",
            {
                "op": "clip.create",
                "actor": "agent",
                "expectedRevision": 0,
                "payload": {
                    "clipId": "c_missing_asset",
                    "assetId": "missing",
                    "trackId": "video_1",
                    "start": 0,
                    "duration": 1,
                },
            },
        )

    project = store.load("proj_invalid")
    assert project["revision"] == 0
    assert project["clips"] == {}
    assert project["commandLog"] == []
