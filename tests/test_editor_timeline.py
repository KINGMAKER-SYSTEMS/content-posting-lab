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


def test_save_fsyncs_before_replace_so_a_crash_cannot_lose_edits(tmp_path, monkeypatch):
    """save() must flush+fsync the tmp file before the atomic rename.

    Without fsync, the rename can hit disk while the data is still in the OS
    page cache, so a CPU crash leaves a renamed-but-empty/truncated project
    file. atomic_save (the proven telegram/json_store path) fsyncs first; this
    guards that editor_timeline routes through it and never regresses to a raw
    write_text()+replace.
    """
    import os

    fsynced: list[int] = []
    real_fsync = os.fsync

    def spy_fsync(fd: int) -> None:
        fsynced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr("services.json_store.os.fsync", spy_fsync)

    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_durable", title="Durable")
    store.save(project)

    assert fsynced, "save() did not fsync the tmp file before the atomic rename"
    # round-trips cleanly and leaves no stray tmp file behind
    assert store.load("proj_durable")["title"] == "Durable"
    assert not list(tmp_path.glob("*.tmp"))


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


def test_music_bed_imports_pre_ducked_under_vo():
    """The music bed must import ducked (0.22, the factory convention) so a render has
    music UNDER the VO, not competing with it at full volume. VO stays at 1.0."""
    project = timeline.project_from_abn_timeline("p", {
        "episodeId": "e", "totalSec": 4.0, "musicBed": "/agenticnews-assets/bed.mp3",
        "segments": [{"segmentId": "s0", "durationSec": 4.0, "shots": [],
                      "audio": {"vo": {"src": "/agenticnews-assets/vo.wav", "duration": 4.0}}}],
    })
    bed = next(c for c in project["clips"].values() if c["kind"] == "music_bed")
    vo = next(c for c in project["clips"].values() if c["kind"] == "voiceover")
    assert bed["volume"] == 0.22
    assert vo["volume"] == 1.0


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


def test_split_preserves_keyframes_and_effects_on_both_halves(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    project = timeline.new_project("proj_split_kf")
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
            },
        },
        {
            # opacity ramp across the whole clip: 0 at t=0, 1 at t=2 (the split
            # point), 0.5 at t=5 (clip-local seconds).
            "op": "clip.keyframes",
            "actor": "human",
            "expectedRevision": 2,
            "payload": {
                "clipId": "c1",
                "keyframes": [
                    {
                        "property": "opacity",
                        "points": [
                            {"t": 0.0, "value": 0.0, "interp": "linear"},
                            {"t": 2.0, "value": 1.0, "interp": "linear"},
                            {"t": 5.0, "value": 0.5, "interp": "linear"},
                        ],
                    }
                ],
            },
        },
        {
            "op": "clip.effect.add",
            "actor": "human",
            "expectedRevision": 3,
            "payload": {"clipId": "c1", "effect": {"id": "fx_in", "type": "fadeIn", "params": {"duration": 0.5}}},
        },
        {
            "op": "clip.effect.add",
            "actor": "human",
            "expectedRevision": 4,
            "payload": {"clipId": "c1", "effect": {"id": "fx_out", "type": "fadeOut", "params": {"duration": 0.5}}},
        },
        {
            "op": "clip.effect.add",
            "actor": "human",
            "expectedRevision": 5,
            "payload": {"clipId": "c1", "effect": {"id": "fx_bri", "type": "brightness", "params": {"value": 0.2}}},
        },
        {
            # split 2s into the clip (timeline t=12.0)
            "op": "clip.split",
            "actor": "human",
            "expectedRevision": 6,
            "payload": {"clipId": "c1", "at": 12.0, "newClipId": "c1_b"},
        },
    ]
    for command in commands:
        project = store.apply_command("proj_split_kf", command)

    head = project["clips"]["c1"]
    tail = project["clips"]["c1_b"]

    # Head keeps only the points up to the split (t <= 2), unshifted.
    head_pts = head["keyframes"][0]["points"]
    assert [(p["t"], p["value"]) for p in head_pts] == [(0.0, 0.0), (2.0, 1.0)]

    # Tail rebases to its own t=0 at the split: t=2 -> 0, t=5 -> 3.
    tail_pts = tail["keyframes"][0]["points"]
    assert [(p["t"], p["value"]) for p in tail_pts] == [(0.0, 1.0), (3.0, 0.5)]

    # fadeIn stays at the head, fadeOut moves to the tail, brightness is whole-clip.
    assert {e["id"] for e in head["effects"]} == {"fx_in", "fx_bri"}
    assert {e["id"] for e in tail["effects"]} == {"fx_out", "fx_bri"}

    # Replay reproduces the same partitioned envelopes deterministically.
    rebuilt = timeline.replay_project(project)
    assert rebuilt["clips"]["c1"]["keyframes"] == head["keyframes"]
    assert rebuilt["clips"]["c1_b"]["keyframes"] == tail["keyframes"]
    assert rebuilt["clips"]["c1"]["effects"] == head["effects"]
    assert rebuilt["clips"]["c1_b"]["effects"] == tail["effects"]


def test_clip_hide_show_mute_unmute_toggle_enabled_and_muted_flags(tmp_path):
    """The four UI visibility/audio toggles flip exactly the `enabled`/`muted`
    booleans and leave every other clip field untouched."""
    store = timeline.TimelineStore(tmp_path)
    project = _seed_project_with_clip(store, "proj_toggles")

    # Fresh clips start visible and audible.
    assert project["clips"]["c1"]["enabled"] is True
    assert project["clips"]["c1"]["muted"] is False

    for revision, (op, expected_enabled, expected_muted) in enumerate(
        [
            ("clip.hide", False, False),
            ("clip.mute", False, True),
            ("clip.show", True, True),
            ("clip.unmute", True, False),
        ],
        start=2,
    ):
        project = store.apply_command(
            "proj_toggles",
            {"op": op, "actor": "human", "expectedRevision": revision, "payload": {"clipId": "c1"}},
        )
        assert project["clips"]["c1"]["enabled"] is expected_enabled, op
        assert project["clips"]["c1"]["muted"] is expected_muted, op

    # No collateral damage to the rest of the clip.
    assert project["clips"]["c1"]["start"] == 0
    assert project["clips"]["c1"]["duration"] == 4
    assert project["clips"]["c1"]["volume"] == 1.0


def test_revert_last_clip_mute_restores_prior_muted_flag_and_replays(tmp_path):
    """clip.mute is revertible: undo emits a clip.update inverse that restores the
    pre-mute `muted` flag, and replaying the log reproduces the reverted state."""
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_undo_mute")

    project = store.apply_command(
        "proj_undo_mute",
        {"op": "clip.mute", "actor": "human", "expectedRevision": 2, "payload": {"clipId": "c1"}},
    )
    assert project["clips"]["c1"]["muted"] is True

    reverted = store.revert_last_command("proj_undo_mute", actor="human", expected_revision=3)

    assert reverted["revision"] == 4
    assert reverted["commandLog"][-1]["op"] == "clip.update"
    assert reverted["clips"]["c1"]["muted"] is False
    rebuilt = timeline.replay_project(reverted)
    assert rebuilt["clips"] == reverted["clips"]
    assert rebuilt["revision"] == reverted["revision"]


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


def _seed_project_with_clip(store, project_id):
    """Create a project with one imported asset and clip `c1` on video_1 (revision 2)."""
    store.save(timeline.new_project(project_id))
    store.apply_command(
        project_id,
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
        },
    )
    return store.apply_command(
        project_id,
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 0,
                "duration": 4,
            },
        },
    )


@pytest.mark.parametrize(
    "op,payload",
    [
        ("clip.move", {"clipId": "ghost", "start": 1.0}),
        ("clip.split", {"clipId": "ghost", "at": 1.0}),
        ("clip.trim", {"clipId": "ghost", "duration": 1.0}),
        ("clip.mute", {"clipId": "ghost"}),
        ("clip.transform", {"clipId": "ghost", "transform": {"x": 0.4}}),
    ],
)
def test_clip_operations_on_missing_clip_are_rejected_without_mutating_state(tmp_path, op, payload):
    """Every clip-targeted op funnels through _require_clip; a missing clipId must
    raise CommandValidationError and leave the project untouched."""
    store = timeline.TimelineStore(tmp_path)
    project = _seed_project_with_clip(store, "proj_missing_clip")

    with pytest.raises(timeline.CommandValidationError, match="clip does not exist: ghost"):
        store.apply_command(
            "proj_missing_clip",
            {"op": op, "actor": "agent", "expectedRevision": 2, "payload": payload},
        )

    loaded = store.load("proj_missing_clip")
    assert loaded["revision"] == project["revision"]
    assert set(loaded["clips"]) == {"c1"}


def test_clip_op_without_clip_id_is_rejected(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_no_clip_id")

    with pytest.raises(timeline.CommandValidationError, match="clipId is required"):
        store.apply_command(
            "proj_no_clip_id",
            {"op": "clip.move", "actor": "agent", "expectedRevision": 2, "payload": {"start": 1.0}},
        )

    assert store.load("proj_no_clip_id")["revision"] == 2


def test_apply_command_rejects_stale_revision_without_mutating(tmp_path):
    """The RevisionConflict guard in apply_command fires when expectedRevision is behind
    the live project, and the rejected command never touches state."""
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_stale")  # advances to revision 2

    with pytest.raises(timeline.RevisionConflict, match="expected revision 0, current revision 2"):
        store.apply_command(
            "proj_stale",
            {
                "op": "clip.move",
                "actor": "stale-agent",
                "expectedRevision": 0,
                "payload": {"clipId": "c1", "start": 2.0},
            },
        )

    loaded = store.load("proj_stale")
    assert loaded["revision"] == 2
    assert loaded["clips"]["c1"]["start"] == 0


def test_apply_command_requires_expected_revision(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    store.save(timeline.new_project("proj_no_rev"))

    with pytest.raises(timeline.CommandValidationError, match="expectedRevision is required"):
        store.apply_command(
            "proj_no_rev",
            {"op": "asset.import", "actor": "agent", "payload": {"assetId": "a1", "type": "image", "src": "/a.png"}},
        )


def test_revert_last_command_on_empty_log_is_rejected(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    store.save(timeline.new_project("proj_empty_log"))

    with pytest.raises(timeline.CommandValidationError, match="no command to revert"):
        store.revert_last_command("proj_empty_log", actor="human")


def test_revert_last_command_rejects_stale_expected_revision(tmp_path):
    """revert_last_command has its own RevisionConflict guard separate from apply_command."""
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_revert_stale")  # revision 2

    with pytest.raises(timeline.RevisionConflict, match="expected revision 99, current revision 2"):
        store.revert_last_command("proj_revert_stale", actor="human", expected_revision=99)

    # nothing reverted: clip.create is still the last log entry
    assert store.load("proj_revert_stale")["commandLog"][-1]["op"] == "clip.create"


def test_revert_of_unrevertable_op_is_rejected(tmp_path):
    """asset.import has no inverse; reverting it must raise rather than corrupt state."""
    store = timeline.TimelineStore(tmp_path)
    store.save(timeline.new_project("proj_unrevertable"))
    store.apply_command(
        "proj_unrevertable",
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "image", "src": "/a.png"},
        },
    )

    with pytest.raises(timeline.CommandValidationError, match="cannot revert command asset.import"):
        store.revert_last_command("proj_unrevertable", actor="human")

    assert store.load("proj_unrevertable")["revision"] == 1


def test_unsplit_requires_original_clip_and_created_clip_id(tmp_path):
    """The early structural guards in the clip.unsplit branch reject malformed payloads
    before any _clip_values_match() comparison runs."""
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_unsplit_payload")  # revision 2

    with pytest.raises(timeline.CommandValidationError, match="requires original clip"):
        store.apply_command(
            "proj_unsplit_payload",
            {
                "op": "clip.unsplit",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {"createdClipId": "c1_b"},
            },
        )

    with pytest.raises(timeline.CommandValidationError, match="requires createdClipId"):
        store.apply_command(
            "proj_unsplit_payload",
            {
                "op": "clip.unsplit",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {"clip": {"id": "c1"}},
            },
        )

    assert store.load("proj_unsplit_payload")["revision"] == 2


def test_unsplit_reverts_command_id_must_point_at_a_split(tmp_path):
    """A clip.unsplit whose revertsCommandId targets a non-split entry is rejected before
    mutating clips. The generic _validate_reverts_command_id guard fires first because a
    clip.move's server inverse is a clip.update, not a clip.unsplit."""
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_unsplit_nonsplit")
    project = store.apply_command(
        "proj_unsplit_nonsplit",
        {
            "id": "cmd_move",
            "op": "clip.move",
            "actor": "human",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "start": 1.0},
        },
    )

    with pytest.raises(timeline.CommandValidationError, match="op does not match inverse command"):
        store.apply_command(
            "proj_unsplit_nonsplit",
            {
                "op": "clip.unsplit",
                "actor": "agent",
                "expectedRevision": project["revision"],
                "revertsCommandId": "cmd_move",
                "payload": {
                    "clipId": "c1",
                    "createdClipId": "c1_b",
                    "clip": {"id": "c1"},
                },
            },
        )

    assert store.load("proj_unsplit_nonsplit")["revision"] == project["revision"]


@pytest.mark.parametrize(
    "transform,message",
    [
        ({"opacity": 1.5}, "transform.opacity must be between 0.0 and 1.0"),
        ({"opacity": -0.1}, "transform.opacity must be non-negative"),
        ({"scale": 0}, "transform.scale must be positive"),
        ({"scale": -2}, "transform.scale must be non-negative"),
    ],
)
def test_clip_transform_rejects_out_of_bounds_values(tmp_path, transform, message):
    """_validated_transform enforces opacity in [0,1] and scale > 0. Out-of-bounds
    payloads must be rejected, not clamped, and must not advance the revision."""
    store = timeline.TimelineStore(tmp_path)
    project = _seed_project_with_clip(store, "proj_transform_bounds")
    baseline = project["clips"]["c1"]["transform"]

    with pytest.raises(timeline.CommandValidationError, match=message):
        store.apply_command(
            "proj_transform_bounds",
            {
                "op": "clip.transform",
                "actor": "human",
                "expectedRevision": 2,
                "payload": {"clipId": "c1", "transform": transform},
            },
        )

    loaded = store.load("proj_transform_bounds")
    assert loaded["revision"] == 2
    assert loaded["clips"]["c1"]["transform"] == baseline


def test_clip_opacity_op_rejects_out_of_range_value(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_opacity_bounds")

    with pytest.raises(timeline.CommandValidationError, match="opacity must be between 0.0 and 1.0"):
        store.apply_command(
            "proj_opacity_bounds",
            {
                "op": "clip.opacity",
                "actor": "human",
                "expectedRevision": 2,
                "payload": {"clipId": "c1", "opacity": 2.0},
            },
        )

    assert store.load("proj_opacity_bounds")["revision"] == 2


def test_clip_volume_op_rejects_negative_value(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_volume_neg")

    with pytest.raises(timeline.CommandValidationError, match="volume must be non-negative"):
        store.apply_command(
            "proj_volume_neg",
            {
                "op": "clip.volume",
                "actor": "human",
                "expectedRevision": 2,
                "payload": {"clipId": "c1", "volume": -0.5},
            },
        )

    assert store.load("proj_volume_neg")["revision"] == 2


def test_split_outside_clip_bounds_is_rejected(tmp_path):
    """clip.split must reject a split point at or beyond the clip edges."""
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_split_bounds")  # c1 start=0 duration=4

    with pytest.raises(timeline.CommandValidationError, match="split point must be inside the clip"):
        store.apply_command(
            "proj_split_bounds",
            {
                "op": "clip.split",
                "actor": "human",
                "expectedRevision": 2,
                "payload": {"clipId": "c1", "at": 4.0},
            },
        )

    loaded = store.load("proj_split_bounds")
    assert loaded["revision"] == 2
    assert set(loaded["clips"]) == {"c1"}


def test_split_partitions_and_rebases_keyframes_and_effects(tmp_path):
    """clip.split must not blindly clone animation onto the tail half.

    A clip [0, 10) with a fadeIn (head-anchored), a fadeOut (tail-anchored) and a
    volume envelope is split at t=8.0. The head keeps fadeIn and only the points up
    to the split (t <= 8); the tail keeps fadeOut and rebases its points to its new
    t=0 (t >= 8 → t -= 8). Without this, the tail clip fires the parent's keyframes
    at the wrong absolute times and inherits a fadeIn it should not have.
    """
    store = timeline.TimelineStore(tmp_path)
    store.save(timeline.new_project("proj_split_anim"))
    store.apply_command(
        "proj_split_anim",
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "video", "src": "/a.mp4"},
        },
    )
    store.apply_command(
        "proj_split_anim",
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "video_1",
                "start": 0.0,
                "duration": 10.0,
            },
        },
    )
    store.apply_command(
        "proj_split_anim",
        {
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "effect": {"id": "fi", "type": "fadeIn", "params": {"duration": 1.0}}},
        },
    )
    store.apply_command(
        "proj_split_anim",
        {
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": 3,
            "payload": {"clipId": "c1", "effect": {"id": "fo", "type": "fadeOut", "params": {"duration": 1.0}}},
        },
    )
    store.apply_command(
        "proj_split_anim",
        {
            "op": "clip.keyframes",
            "actor": "agent",
            "expectedRevision": 4,
            "payload": {
                "clipId": "c1",
                "keyframes": [
                    {
                        "property": "opacity",
                        "points": [
                            {"t": 0.0, "value": 1.0, "interp": "linear"},
                            {"t": 7.5, "value": 0.5, "interp": "linear"},
                            {"t": 8.0, "value": 0.4, "interp": "linear"},
                            {"t": 9.5, "value": 0.0, "interp": "linear"},
                        ],
                    }
                ],
            },
        },
    )

    project = store.apply_command(
        "proj_split_anim",
        {
            "op": "clip.split",
            "actor": "human",
            "expectedRevision": 5,
            "payload": {"clipId": "c1", "at": 8.0, "newClipId": "c1_tail"},
        },
    )

    head = project["clips"]["c1"]
    tail = project["clips"]["c1_tail"]

    # Geometry split as before.
    assert head["duration"] == 8.0
    assert (tail["start"], tail["duration"]) == (8.0, 2.0)

    # Boundary-anchored effects: head keeps fadeIn only, tail keeps fadeOut only.
    assert [e["id"] for e in head["effects"]] == ["fi"]
    assert [e["id"] for e in tail["effects"]] == ["fo"]

    # Head envelope keeps only points up to the split (t <= 8); the t=9.5 point is gone.
    assert [p["t"] for p in head["keyframes"][0]["points"]] == [0.0, 7.5, 8.0]
    # Tail envelope keeps points at/after the split, rebased to its new t=0.
    assert [p["t"] for p in tail["keyframes"][0]["points"]] == [0.0, 1.5]
    assert tail["keyframes"][0]["points"][0]["value"] == 0.4  # the boundary point anchors the tail
    assert tail["keyframes"][0]["points"][1]["value"] == 0.0  # was t=9.5 on the parent


def test_unsupported_op_is_rejected(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    store.save(timeline.new_project("proj_bad_op"))

    with pytest.raises(timeline.CommandValidationError, match="unsupported command op: clip.teleport"):
        store.apply_command(
            "proj_bad_op",
            {"op": "clip.teleport", "actor": "agent", "expectedRevision": 0, "payload": {}},
        )

    assert store.load("proj_bad_op")["revision"] == 0


def _project_with_bed_clip(store: timeline.TimelineStore, project_id: str) -> dict:
    store.save(timeline.new_project(project_id))
    store.apply_command(
        project_id,
        {
            "op": "asset.import",
            "actor": "agent",
            "expectedRevision": 0,
            "payload": {"assetId": "bed", "type": "audio", "src": "/agenticnews-assets/bed.mp3"},
        },
    )
    return store.apply_command(
        project_id,
        {
            "op": "clip.create",
            "actor": "agent",
            "expectedRevision": 1,
            "payload": {
                "clipId": "bed_clip",
                "assetId": "bed",
                "trackId": "music_1",
                "start": 0.0,
                "duration": 6.0,
            },
        },
    )


def test_clip_keyframes_command_sets_sorted_envelope_and_reverts(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    _project_with_bed_clip(store, "proj_keyframes")

    # Unsorted-on-purpose ducking envelope: full -> ducked under VO -> back up.
    project = store.apply_command(
        "proj_keyframes",
        {
            "op": "clip.keyframes",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {
                "clipId": "bed_clip",
                "keyframes": [
                    {
                        "property": "volume",
                        "points": [
                            {"t": 5.0, "value": 1.0, "interp": "linear"},
                            {"t": 0.0, "value": 1.0},
                            {"t": 1.0, "value": 0.22, "interp": "constant"},
                        ],
                    }
                ],
            },
        },
    )

    envelope = project["clips"]["bed_clip"]["keyframes"]
    assert [p["t"] for p in envelope[0]["points"]] == [0.0, 1.0, 5.0]  # sorted by time
    assert envelope[0]["points"][1]["interp"] == "constant"
    assert envelope[0]["points"][0]["interp"] == "linear"  # default applied

    reverted = store.revert_last_command("proj_keyframes", actor="human", expected_revision=3)
    assert reverted["clips"]["bed_clip"]["keyframes"] == []  # back to flat


def test_clip_effect_add_update_delete_and_revert(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_effects")  # clip c1 at revision 2

    # add a fadeIn effect
    project = store.apply_command(
        "proj_effects",
        {
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "effect": {"id": "fx1", "type": "fadeIn", "params": {"duration": 0.5}}},
        },
    )
    assert project["clips"]["c1"]["effects"] == [
        {"id": "fx1", "type": "fadeIn", "params": {"duration": 0.5}}
    ]

    # update it in place
    project = store.apply_command(
        "proj_effects",
        {
            "op": "clip.effect.update",
            "actor": "agent",
            "expectedRevision": 3,
            "payload": {"clipId": "c1", "effect": {"id": "fx1", "type": "fadeIn", "params": {"duration": 1.5}}},
        },
    )
    assert project["clips"]["c1"]["effects"][0]["params"]["duration"] == 1.5

    # add a second effect, then delete the first by id
    project = store.apply_command(
        "proj_effects",
        {
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": 4,
            "payload": {"clipId": "c1", "id": "fx2", "type": "brightness", "params": {"value": -0.3}},
        },
    )
    assert [e["id"] for e in project["clips"]["c1"]["effects"]] == ["fx1", "fx2"]

    project = store.apply_command(
        "proj_effects",
        {
            "op": "clip.effect.delete",
            "actor": "agent",
            "expectedRevision": 5,
            "payload": {"clipId": "c1", "effectId": "fx1"},
        },
    )
    assert [e["id"] for e in project["clips"]["c1"]["effects"]] == ["fx2"]

    # revert the delete: fx1 comes back (effects list restored exactly)
    reverted = store.revert_last_command("proj_effects", actor="human", expected_revision=6)
    assert reverted["commandLog"][-1]["op"] == "clip.update"
    assert [e["id"] for e in reverted["clips"]["c1"]["effects"]] == ["fx1", "fx2"]

    # replay rebuilds identical state from the command log
    rebuilt = timeline.replay_project(reverted)
    assert rebuilt["clips"] == reverted["clips"]
    assert rebuilt["revision"] == reverted["revision"]


def test_clip_effect_commands_reject_bad_type_params_and_duplicates(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_effects_bad")  # revision 2

    # unknown effect type
    with pytest.raises(timeline.CommandValidationError, match="unsupported effect type"):
        store.apply_command(
            "proj_effects_bad",
            {
                "op": "clip.effect.add",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {"clipId": "c1", "id": "fx", "type": "warp", "params": {}},
            },
        )

    # out-of-bounds param value
    with pytest.raises(timeline.CommandValidationError, match="effect.value must be between"):
        store.apply_command(
            "proj_effects_bad",
            {
                "op": "clip.effect.add",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {"clipId": "c1", "id": "fx", "type": "brightness", "params": {"value": 5}},
            },
        )

    # unknown param key
    with pytest.raises(timeline.CommandValidationError, match="unsupported effect params"):
        store.apply_command(
            "proj_effects_bad",
            {
                "op": "clip.effect.add",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {"clipId": "c1", "id": "fx", "type": "fadeIn", "params": {"duration": 1, "bogus": 2}},
            },
        )

    # updating / deleting an effect that doesn't exist
    with pytest.raises(timeline.CommandValidationError, match="effect does not exist"):
        store.apply_command(
            "proj_effects_bad",
            {
                "op": "clip.effect.update",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {"clipId": "c1", "id": "ghost", "type": "fadeIn", "params": {"duration": 1}},
            },
        )
    with pytest.raises(timeline.CommandValidationError, match="effect does not exist"):
        store.apply_command(
            "proj_effects_bad",
            {
                "op": "clip.effect.delete",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {"clipId": "c1", "effectId": "ghost"},
            },
        )

    # duplicate effect id on add
    project = store.apply_command(
        "proj_effects_bad",
        {
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "id": "fx1", "type": "fadeOut", "params": {"duration": 0.5}},
        },
    )
    with pytest.raises(timeline.CommandValidationError, match="effect already exists"):
        store.apply_command(
            "proj_effects_bad",
            {
                "op": "clip.effect.add",
                "actor": "agent",
                "expectedRevision": project["revision"],
                "payload": {"clipId": "c1", "id": "fx1", "type": "fadeIn", "params": {"duration": 0.5}},
            },
        )

    # the failed commands never advanced revision past the one good add
    assert store.load("proj_effects_bad")["revision"] == 3


def test_clip_keyframes_command_rejects_bad_property_and_interp(tmp_path):
    store = timeline.TimelineStore(tmp_path)
    _project_with_bed_clip(store, "proj_keyframes_bad")

    with pytest.raises(timeline.CommandValidationError, match="unsupported keyframe property"):
        store.apply_command(
            "proj_keyframes_bad",
            {
                "op": "clip.keyframes",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {
                    "clipId": "bed_clip",
                    "keyframes": [{"property": "warp", "points": [{"t": 0, "value": 1}]}],
                },
            },
        )

    with pytest.raises(timeline.CommandValidationError, match="unsupported keyframe interp"):
        store.apply_command(
            "proj_keyframes_bad",
            {
                "op": "clip.keyframes",
                "actor": "agent",
                "expectedRevision": 2,
                "payload": {
                    "clipId": "bed_clip",
                    "keyframes": [
                        {"property": "volume", "points": [{"t": 0, "value": 1, "interp": "warp"}]}
                    ],
                },
            },
        )

    assert store.load("proj_keyframes_bad")["revision"] == 2  # nothing committed


def test_crossfade_and_saturation_effects_validate_and_persist(tmp_path):
    """crossfade (transition) and saturation (color filter) are in the closed
    effect vocabulary but were untested. Confirm both pass validation, clamp
    params, and persist exactly — the editor side of the round-trip whose
    OpenShot translation lives in test_openshot_bridge."""
    store = timeline.TimelineStore(tmp_path)
    _seed_project_with_clip(store, "proj_xfade_sat")  # clip c1 at revision 2

    project = store.apply_command(
        "proj_xfade_sat",
        {
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": 2,
            "payload": {"clipId": "c1", "id": "xf", "type": "crossfade", "params": {"duration": 0.75}},
        },
    )
    project = store.apply_command(
        "proj_xfade_sat",
        {
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": 3,
            "payload": {"clipId": "c1", "id": "sat", "type": "saturation", "params": {"value": 1.4}},
        },
    )

    assert project["clips"]["c1"]["effects"] == [
        {"id": "xf", "type": "crossfade", "params": {"duration": 0.75}},
        {"id": "sat", "type": "saturation", "params": {"value": 1.4}},
    ]

    # saturation is bounded 0..4; out-of-range is rejected without advancing revision
    with pytest.raises(timeline.CommandValidationError, match="effect.value must be between"):
        store.apply_command(
            "proj_xfade_sat",
            {
                "op": "clip.effect.add",
                "actor": "agent",
                "expectedRevision": 4,
                "payload": {"clipId": "c1", "id": "sat2", "type": "saturation", "params": {"value": 9}},
            },
        )
    assert store.load("proj_xfade_sat")["revision"] == 4


def test_save_uses_atomic_store_and_fsyncs(tmp_path, monkeypatch):
    """TimelineStore.save must route through the shared atomic_save util (tmp+fsync+
    replace) — not a bare write — so a crash mid-write can't truncate a timeline config."""
    calls = {}

    def _spy(path, data, **kwargs):
        calls["path"] = path
        calls["data"] = data
        from services.json_store import atomic_save as real
        real(path, data, **kwargs)

    monkeypatch.setattr(timeline, "atomic_save", _spy)

    store = timeline.TimelineStore(tmp_path)
    project = {"projectId": "proj_atomic", "revision": 0, "clips": {}}
    store.save(project)

    # routed through the shared util...
    assert calls["path"] == store.path_for("proj_atomic")
    assert calls["data"]["projectId"] == "proj_atomic"
    # ...and the real atomic_save left no stray tmp file and round-trips.
    assert not list(tmp_path.glob("*.tmp"))
    assert store.load("proj_atomic") == project


def test_abn_import_ducking_envelope_and_shot_effect_round_trip_to_openshot(tmp_path):
    """The full ABN -> Editor Bay -> OpenShot fidelity path for a KEYFRAMED ducking
    envelope and a clip effect.

    test_music_bed_imports_pre_ducked_under_vo() only proves the bed lands at the flat
    0.22 gain on import. It does NOT prove that a *keyframed* volume envelope (the real
    sidechain duck: full -> under VO -> back up) survives all the way to the compiler,
    nor that a crossfade on a shot does. abn_factory's duck pass and clip-boundary
    crossfades depend on both reaching OpenShot, so this nails the round-trip end to end:
    import the timeline, attach the envelope + effect through the command core, and
    assert openshot_bridge emits a multi-point volume keyframe (0..100 scale, correct
    interpolations) and a Fade effect — not the flat single-point default that a silent
    discard would leave behind.
    """
    from services import openshot_bridge

    abn_timeline = {
        "episodeId": "ep_round_trip",
        "totalSec": 4.0,
        "musicBed": "/agenticnews-assets/bed.mp3",
        "segments": [
            {
                "segmentId": "s0",
                "durationSec": 4.0,
                "shots": [
                    {
                        "id": "card",
                        "src": "/agenticnews-assets/card.png",
                        "startSec": 0.0,
                        "durationSec": 2.0,
                        "type": "artifact",
                    }
                ],
                "audio": {"vo": {"src": "/agenticnews-assets/vo.wav", "duration": 4.0}},
            }
        ],
    }

    store = timeline.TimelineStore(tmp_path)
    project = timeline.project_from_abn_timeline("proj_round_trip", abn_timeline)
    store.save(project)

    bed_id = next(c["id"] for c in project["clips"].values() if c["kind"] == "music_bed")
    shot_id = next(c["id"] for c in project["clips"].values() if c["kind"] == "artifact")
    base_rev = project["revision"]

    # Replace the flat 0.22 gain with a real ducking envelope: full -> ducked under
    # the VO (constant-held floor) -> back up at the VO tail.
    project = store.apply_command(
        "proj_round_trip",
        {
            "op": "clip.keyframes",
            "actor": "agent",
            "expectedRevision": base_rev,
            "payload": {
                "clipId": bed_id,
                "keyframes": [
                    {
                        "property": "volume",
                        "points": [
                            {"t": 0.0, "value": 1.0, "interp": "linear"},
                            {"t": 1.0, "value": 0.22, "interp": "constant"},
                            {"t": 3.0, "value": 1.0, "interp": "linear"},
                        ],
                    }
                ],
            },
        },
    )
    # And a clip-boundary crossfade on the shot.
    project = store.apply_command(
        "proj_round_trip",
        {
            "op": "clip.effect.add",
            "actor": "agent",
            "expectedRevision": project["revision"],
            "payload": {
                "clipId": shot_id,
                "id": "xf",
                "type": "crossfade",
                "params": {"duration": 0.5},
            },
        },
    )

    bridge = openshot_bridge.timeline_json(project, asset_root=tmp_path)
    bed_oj = next(c for c in bridge["clips"] if c["id"] == bed_id)
    shot_oj = next(c for c in bridge["clips"] if c["id"] == shot_id)

    # The ducking envelope is a real multi-point keyframe, NOT the flat single-point
    # default — values on OpenShot's 0..100 scale, interpolations preserved per point.
    volume_points = bed_oj["volume"]["Points"]
    assert [round(p["co"]["Y"], 2) for p in volume_points] == [100.0, 22.0, 100.0]
    assert [p["interpolation"] for p in volume_points] == [
        openshot_bridge.LINEAR,
        openshot_bridge.CONSTANT,
        openshot_bridge.LINEAR,
    ]

    # The crossfade survives as an OpenShot Fade effect (not silently dropped).
    assert [e["type"] for e in shot_oj["effects"]] == ["Fade"]
    assert shot_oj["effects"][0]["id"] == "xf"
    assert shot_oj["effects"][0]["fade"] == "in"


def test_abn_import_does_not_yet_promote_shot_level_effects_or_keyframes():
    """Pin the known fidelity boundary so it can never regress to a SILENT discard.

    abn_factory shots can carry per-shot envelopes (e.g. `kenBurns`) and could carry an
    `effects` list. project_from_abn_timeline does NOT yet promote those onto the active
    clip.effects / clip.keyframes fields that openshot_bridge reads — so they would not
    reach the compiler on import alone (an editor command must attach them; see the
    round-trip test above). The raw shot is preserved losslessly under
    clip.metadata.shot, so the data is never destroyed — it is just not wired through.

    If a future change teaches the importer to carry shot effects/keyframes, this test
    will fail loudly and must be updated intentionally, rather than the promotion landing
    silently with no coverage.
    """
    abn_timeline = {
        "episodeId": "ep_shot_fx",
        "segments": [
            {
                "segmentId": "s0",
                "durationSec": 4.0,
                "shots": [
                    {
                        "id": "card",
                        "src": "/agenticnews-assets/card.png",
                        "startSec": 0.0,
                        "durationSec": 2.0,
                        "type": "artifact",
                        "effects": [
                            {"id": "xf", "type": "crossfade", "params": {"duration": 0.5}}
                        ],
                        "kenBurns": {"startScale": 1.0, "endScale": 1.1},
                    }
                ],
            }
        ],
    }

    project = timeline.project_from_abn_timeline("proj_shot_fx", abn_timeline)
    clip = next(c for c in project["clips"].values() if c["kind"] == "artifact")

    # Current behaviour: shot-level effects/keyframes are NOT promoted to active fields.
    assert clip["effects"] == []
    assert clip["keyframes"] == []
    # ...but the raw shot (and everything on it) is retained, so nothing is lost.
    assert clip["metadata"]["shot"]["effects"][0]["type"] == "crossfade"
    assert clip["metadata"]["shot"]["kenBurns"]["endScale"] == 1.1
