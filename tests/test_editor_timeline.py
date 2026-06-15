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
        "musicBed": "/agenticnews-assets/bed.mp3",
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
    assert any(c["kind"] == "music_bed" and c["trackId"] == "music_1" for c in project["clips"].values())

    store = timeline.TimelineStore(tmp_path)
    saved = store.save(project)
    assert saved == project
    loaded = store.load("proj_ep_fixture")
    assert loaded["projectId"] == "proj_ep_fixture"


def test_import_preserves_production_source_for_openshot():
    """A shot whose `type` is a known production source (broll/remotion/webscroll/css)
    carries that onto the asset as `source`, so the OpenShot bridge keeps layers
    distinguishable. Unknown types (artifact) get no source and fall back to media kind."""
    from services import openshot_bridge

    abn_timeline = {
        "episodeId": "ep_src",
        "segments": [{
            "segmentId": "s0",
            "durationSec": 4.0,
            "shots": [
                {"id": "b", "src": "/agenticnews-assets/ui.mp4", "startSec": 0.0, "durationSec": 2.0, "type": "broll"},
                {"id": "a", "src": "/agenticnews-assets/card.png", "startSec": 2.0, "durationSec": 2.0, "type": "artifact"},
            ],
        }],
    }
    project = timeline.project_from_abn_timeline("proj_ep_src", abn_timeline, source_episode_id="ep_src")

    broll = next(a for a in project["assets"].values() if a["src"] == "/agenticnews-assets/ui.mp4")
    artifact = next(a for a in project["assets"].values() if a["src"] == "/agenticnews-assets/card.png")
    assert broll["source"] == "broll"
    assert "source" not in artifact
    # the bridge resolves the source tag to the right libopenshot media kind
    assert openshot_bridge._asset_type(broll) == "video"


def test_import_infers_missing_shot_durations_from_next_boundary():
    project = timeline.project_from_abn_timeline(
        "proj_boundaries",
        {
            "episodeId": "ep_boundaries",
            "segments": [
                {
                    "segmentId": "s0",
                    "durationSec": 6.0,
                    "shots": [
                        {
                            "id": "a",
                            "src": "/agenticnews-assets/a.png",
                            "startSec": 1.0,
                            "type": "artifact",
                        },
                        {
                            "id": "b",
                            "src": "/agenticnews-assets/b.png",
                            "startSec": 3.5,
                            "type": "artifact",
                        },
                    ],
                }
            ],
        },
    )

    clips = {clip["metadata"]["shot"]["id"]: clip for clip in project["clips"].values()}

    assert clips["a"]["duration"] == 2.5
    assert clips["b"]["duration"] == 2.5
    assert project["metadata"]["abnImportVersion"] == timeline.ABN_IMPORT_VERSION


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


def test_revert_last_command_restores_transform_exactly_and_replays(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_undo_transform")
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
                "start": 0,
                "duration": 2,
            },
        },
        {
            "op": "clip.transform",
            "actor": "human",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "transform": {"rotation": 12, "x": 0.25}},
        },
    ]:
        project = store.apply_command("proj_undo_transform", command)

    assert project["clips"]["c1"]["transform"]["rotation"] == 12

    reverted = store.revert_last_command("proj_undo_transform", actor="human", expected_revision=3)

    assert reverted["revision"] == 4
    assert reverted["commandLog"][-1]["op"] == "clip.update"
    assert "rotation" not in reverted["clips"]["c1"]["transform"]
    assert reverted["clips"]["c1"]["transform"] == {
        "x": 0.5,
        "y": 0.5,
        "scale": 1.0,
        "opacity": 1.0,
    }
    rebuilt = timeline.replay_project(reverted)
    assert rebuilt["clips"] == reverted["clips"]
    assert rebuilt["revision"] == reverted["revision"]


def test_revert_last_split_restores_original_clip_and_removes_split_clip(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_undo_split")
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
                "start": 10,
                "duration": 6,
                "sourceStart": 2,
            },
        },
        {
            "op": "clip.split",
            "actor": "human",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "at": 12.5, "newClipId": "c1_b"},
        },
    ]:
        project = store.apply_command("proj_undo_split", command)

    assert set(project["clips"]) == {"c1", "c1_b"}

    reverted = store.revert_last_command("proj_undo_split", actor="human", expected_revision=3)

    assert reverted["revision"] == 4
    assert reverted["commandLog"][-1]["op"] == "clip.unsplit"
    assert set(reverted["clips"]) == {"c1"}
    assert reverted["clips"]["c1"]["start"] == 10
    assert reverted["clips"]["c1"]["duration"] == 6
    assert reverted["clips"]["c1"]["sourceStart"] == 2
    rebuilt = timeline.replay_project(reverted)
    assert rebuilt["clips"] == reverted["clips"]


def test_direct_unsplit_requires_matching_recorded_split_inverse(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_unsplit_guard")
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
                "start": 10,
                "duration": 6,
                "sourceStart": 2,
            },
        },
        {
            "op": "clip.split",
            "actor": "human",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "at": 12.5, "newClipId": "c1_b"},
        },
    ]:
        project = store.apply_command("proj_unsplit_guard", command)

    split_command = project["commandLog"][-1]
    original = split_command["before"]["clip"]
    unsplit_payload = {
        "clipId": "c1",
        "createdClipId": "c1_b",
        "clip": original,
    }
    with pytest.raises(timeline.CommandValidationError, match="requires revertsCommandId"):
        store.apply_command(
            "proj_unsplit_guard",
            {
                "op": "clip.unsplit",
                "actor": "agent",
                "expectedRevision": 3,
                "payload": unsplit_payload,
            },
        )

    with pytest.raises(timeline.CommandValidationError, match="does not reference a command"):
        store.apply_command(
            "proj_unsplit_guard",
            {
                "op": "clip.unsplit",
                "actor": "agent",
                "expectedRevision": 3,
                "revertsCommandId": "cmd_missing",
                "payload": unsplit_payload,
            },
        )

    tampered_original = {**original, "duration": 9}
    with pytest.raises(timeline.CommandValidationError, match="payload does not match inverse"):
        store.apply_command(
            "proj_unsplit_guard",
            {
                "op": "clip.unsplit",
                "actor": "agent",
                "expectedRevision": 3,
                "revertsCommandId": split_command["id"],
                "payload": {**unsplit_payload, "clip": tampered_original},
            },
        )

    project = store.apply_command(
        "proj_unsplit_guard",
        {
            "op": "clip.move",
            "actor": "agent",
            "expectedRevision": 3,
            "payload": {"clipId": "c1_b", "start": 13.0},
        },
    )
    with pytest.raises(timeline.CommandValidationError, match="current created clip no longer matches"):
        store.apply_command(
            "proj_unsplit_guard",
            {
                "op": "clip.unsplit",
                "actor": "agent",
                "expectedRevision": project["revision"],
                "revertsCommandId": split_command["id"],
                "payload": unsplit_payload,
            },
        )


def test_reverts_command_id_must_match_server_inverse(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_revert_guard")
    store.save(project)
    for command in [
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "image", "src": "/a.png"},
        },
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "graphics_1",
                "start": 0,
                "duration": 2,
            },
        },
        {
            "id": "cmd_move",
            "op": "clip.move",
            "actor": "human",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "start": 1},
        },
    ]:
        project = store.apply_command("proj_revert_guard", command)

    with pytest.raises(timeline.CommandValidationError, match="payload does not match inverse"):
        store.apply_command(
            "proj_revert_guard",
            {
                "op": "clip.update",
                "actor": "agent",
                "expectedRevision": 3,
                "revertsCommandId": "cmd_move",
                "payload": {
                    "clipId": "c1",
                    "patch": {"start": 99},
                },
            },
        )

    reverted = store.revert_last_command("proj_revert_guard", actor="human", expected_revision=3)

    assert reverted["commandLog"][-1]["revertsCommandId"] == "cmd_move"
    assert reverted["clips"]["c1"]["start"] == 0

    with pytest.raises(timeline.CommandValidationError, match="already reverted"):
        store.apply_command(
            "proj_revert_guard",
            {
                "op": "clip.update",
                "actor": "agent",
                "expectedRevision": 4,
                "revertsCommandId": "cmd_move",
                "payload": {
                    "clipId": "c1",
                    "patch": {"start": 0},
                },
            },
        )


def test_apply_command_deep_copies_payload_boundaries():
    command = {
        "op": "asset.import",
        "actor": "agent",
        "expectedRevision": 0,
        "payload": {
            "assetId": "a1",
            "type": "image",
            "src": "/a.png",
            "metadata": {"label": "original"},
        },
    }
    project = timeline.apply_command(timeline.new_project("proj_clone_boundary"), command)

    command["payload"]["metadata"]["label"] = "mutated"

    assert project["assets"]["a1"]["metadata"]["label"] == "original"
    assert project["commandLog"][0]["payload"]["metadata"]["label"] == "original"


def test_command_ids_must_be_unique(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_unique_command_ids")
    store.save(project)
    project = store.apply_command(
        "proj_unique_command_ids",
        {
            "id": "cmd_duplicate",
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "image", "src": "/a.png"},
        },
    )

    with pytest.raises(timeline.CommandValidationError, match="command id already exists"):
        store.apply_command(
            "proj_unique_command_ids",
            {
                "id": "cmd_duplicate",
                "op": "asset.import",
                "actor": "agent",
                "expectedRevision": 1,
                "payload": {"assetId": "a2", "type": "image", "src": "/b.png"},
            },
        )

    loaded = store.load("proj_unique_command_ids")
    assert loaded["revision"] == project["revision"]
    assert list(loaded["assets"]) == ["a1"]


def test_revert_last_marker_and_note_add_delete_only_added_items(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_undo_notes")
    store.save(project)
    project = store.apply_command(
        "proj_undo_notes",
        {
            "op": "marker.add",
            "actor": "human",
            "expectedRevision": 0,
            "payload": {"markerId": "m1", "time": 1.2, "label": "Review"},
        },
    )
    project = store.apply_command(
        "proj_undo_notes",
        {
            "op": "note.add",
            "actor": "human",
            "expectedRevision": 1,
            "payload": {"noteId": "n1", "text": "Fix beat", "target": {"time": 1.2}},
        },
    )

    reverted_note = store.revert_last_command("proj_undo_notes", actor="human", expected_revision=2)
    assert reverted_note["commandLog"][-1]["op"] == "note.delete"
    assert "n1" not in reverted_note["notes"]
    assert "m1" in reverted_note["markers"]

    reverted_marker = store.revert_last_command("proj_undo_notes", actor="human", expected_revision=3)
    assert reverted_marker["commandLog"][-1]["op"] == "marker.delete"
    assert "m1" not in reverted_marker["markers"]
    rebuilt = timeline.replay_project(reverted_marker)
    assert rebuilt["markers"] == reverted_marker["markers"]
    assert rebuilt["notes"] == reverted_marker["notes"]


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
        {
            "op": "clip.update",
            "actor": "human",
            "expectedRevision": 6,
            "payload": {
                "clipId": "c1_b",
                "patch": {
                    "volume": 0.25,
                    "transform": {"x": 0.4, "y": 0.6, "scale": 0.5, "opacity": 0.8},
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
    assert project["clips"]["c1_b"]["transform"] == {"x": 0.4, "y": 0.6, "scale": 0.5, "opacity": 0.8}
    assert project["clips"]["c1_b"]["volume"] == 0.25
    assert project["markers"]["m1"]["label"] == "split review"
    assert project["notes"]["n1"]["suggestedCommand"]["op"] == "clip.move"


def test_clip_update_accepts_direct_patch_fields_for_agent_control(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_direct_update")
    store.save(project)
    project = store.apply_command(
        "proj_direct_update",
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
        },
    )
    project = store.apply_command(
        "proj_direct_update",
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 0,
                "duration": 2,
            },
        },
    )

    project = store.apply_command(
        "proj_direct_update",
        {
            "op": "clip.update",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {
                "clipId": "c1",
                "volume": 0.25,
                "transform": {"x": 0.4, "y": 0.6, "scale": 0.5, "opacity": 0.8},
            },
        },
    )

    assert project["clips"]["c1"]["volume"] == 0.25
    assert project["clips"]["c1"]["transform"] == {
        "x": 0.4,
        "y": 0.6,
        "scale": 0.5,
        "opacity": 0.8,
    }


def test_clip_update_rejects_invalid_transform_values(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_invalid_transform")
    store.save(project)
    project = store.apply_command(
        "proj_invalid_transform",
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
        },
    )
    project = store.apply_command(
        "proj_invalid_transform",
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 0,
                "duration": 2,
            },
        },
    )

    with pytest.raises(timeline.CommandValidationError, match="transform.x must be numeric"):
        store.apply_command(
            "proj_invalid_transform",
            {
                "op": "clip.update",
                "actor": "human",
                "expectedRevision": 2,
                "payload": {
                    "clipId": "c1",
                    "patch": {
                        "transform": {
                            "x": "bad",
                            "y": 0.5,
                            "scale": 1,
                            "opacity": 1,
                        },
                    },
                },
            },
        )

    loaded = store.load("proj_invalid_transform")
    assert loaded["revision"] == 2
    assert loaded["clips"]["c1"]["transform"] == project["clips"]["c1"]["transform"]


def test_clip_update_rejects_missing_track_id(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_invalid_track_update")
    store.save(project)
    project = store.apply_command(
        "proj_invalid_track_update",
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
        },
    )
    project = store.apply_command(
        "proj_invalid_track_update",
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 0,
                "duration": 2,
            },
        },
    )

    with pytest.raises(timeline.CommandValidationError, match="track does not exist"):
        store.apply_command(
            "proj_invalid_track_update",
            {
                "op": "clip.update",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {
                    "clipId": "c1",
                    "patch": {"trackId": "missing_track"},
                },
            },
        )

    loaded = store.load("proj_invalid_track_update")
    assert loaded["revision"] == 2
    assert loaded["clips"]["c1"]["trackId"] == project["clips"]["c1"]["trackId"]


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
