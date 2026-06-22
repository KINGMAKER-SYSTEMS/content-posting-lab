import json

import pytest

from routers import agenticnews as agn
from services import editor_timeline as timeline


def test_editor_apply_writes_timeline_atomically(sync_client, monkeypatch, tmp_path):
    """POST /editor/{ep}/apply must persist the edited timeline via the proven
    fsync+os.replace atomic_save path and leave no orphan .tmp file."""
    tl_file = tmp_path / "timeline.json"
    timeline = {
        "segments": [
            {"shots": [{"src": "card_a.png"}, {"src": "card_b.png"}]},
        ]
    }
    tl_file.write_text(json.dumps(timeline))

    async def fake_find_video(ep_id):
        return {"id": ep_id}

    monkeypatch.setattr(agn, "_find_video", fake_find_video)
    monkeypatch.setattr(agn, "_timeline_file_for_episode", lambda rid: tl_file)

    # Don't actually re-render; just let the background task no-op.
    async def fake_render(rid, timeline, force=False):
        return None

    monkeypatch.setattr(agn.factory, "_render_remotion", fake_render)

    resp = sync_client.post(
        "/api/agenticnews/editor/ep1/apply",
        json={"edits": [{"action": "delete", "src": "card_a.png"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["applied"]["deleted"] == 1

    # File is valid JSON with the edit applied (not half-written/corrupt).
    written = json.loads(tl_file.read_text())
    srcs = [s["src"] for s in written["segments"][0]["shots"]]
    assert srcs == ["card_b.png"]

    # No orphaned tmp files left behind in the directory.
    tmps = list(tmp_path.glob("*.tmp"))
    assert tmps == [], f"orphan tmp files: {tmps}"


def _seed_clip_project(store, project_id):
    """A project with one asset + clip so clip.* commands have a target."""
    store.save(timeline.new_project(project_id))
    store.apply_command(
        project_id,
        {
            "op": "asset.import",
            "actor": "test",
            "expectedRevision": 0,
            "payload": {"assetId": "a1", "type": "image", "src": "/x/card.png"},
        },
    )
    store.apply_command(
        project_id,
        {
            "op": "clip.create",
            "actor": "test",
            "expectedRevision": 1,
            "payload": {
                "clipId": "c1",
                "assetId": "a1",
                "trackId": "graphics_1",
                "start": 0.0,
                "duration": 4.0,
            },
        },
    )


def _move(rev, start, *, cmd_id=None):
    cmd = {
        "op": "clip.move",
        "actor": "test",
        "expectedRevision": rev,
        "payload": {"clipId": "c1", "start": start},
    }
    if cmd_id is not None:
        cmd["id"] = cmd_id
    return cmd


def test_partial_clip_batch_failure_leaves_disk_at_last_good_revision(tmp_path):
    """When command 3 of a 5-command clip.* batch fails, the on-disk timeline must
    hold exactly the commands that succeeded before it — the failing command must
    not partially mutate or corrupt the persisted file, and the surviving prefix
    must still be replayable.

    Each store.apply_command is its own atomic read-modify-write: the module-level
    apply_command deep-copies and raises *before* save() is reached, so a mid-batch
    failure can never write a half-applied revision."""
    store = timeline.TimelineStore(tmp_path)
    _seed_clip_project(store, "proj_batch")  # revision now 2 (import + create)

    batch = [
        _move(2, 1.0),                      # ok -> rev 3
        _move(3, 2.0),                      # ok -> rev 4
        _move(4, -5.0),                     # FAILS: negative start (command 3 of 5)
        _move(5, 3.0),                      # never reached
        _move(6, 4.0),                      # never reached
    ]

    applied = 0
    with pytest.raises(timeline.CommandValidationError):
        for cmd in batch:
            store.apply_command("proj_batch", cmd)
            applied += 1
    assert applied == 2, "should have applied exactly the two valid commands"

    # On-disk state reflects only the successful prefix, not the failing command.
    on_disk = store.load("proj_batch")
    assert on_disk["revision"] == 4
    assert on_disk["clips"]["c1"]["start"] == 2.0
    assert [c["op"] for c in on_disk["commandLog"]] == [
        "asset.import",
        "clip.create",
        "clip.move",
        "clip.move",
    ]

    # File is whole JSON and no orphan tmp was left by the aborted write.
    raw = json.loads(store.path_for("proj_batch").read_text())
    assert raw["revision"] == 4
    assert list(tmp_path.glob("*.tmp")) == []

    # The surviving prefix is internally consistent: replay rebuilds the same state.
    replayed = timeline.replay_project(on_disk)
    assert replayed["revision"] == 4
    assert replayed["clips"]["c1"]["start"] == 2.0


def test_failing_clip_command_does_not_advance_revision_for_a_retry(tmp_path):
    """A command that fails mid-batch leaves the revision untouched, so the next
    command in the batch (or a retry) targets the same expectedRevision and the
    optimistic-concurrency contract still holds — no off-by-one revision skew."""
    store = timeline.TimelineStore(tmp_path)
    _seed_clip_project(store, "proj_retry")  # revision 2

    # Command 3-of-5 fails (duplicate command id collides with command 1).
    store.apply_command("proj_retry", _move(2, 1.0, cmd_id="m1"))  # rev 3
    with pytest.raises(timeline.CommandValidationError):
        store.apply_command("proj_retry", _move(3, 2.0, cmd_id="m1"))  # dup id

    # Revision did NOT advance past the last good write.
    assert store.load("proj_retry")["revision"] == 3

    # A corrected retry against the still-current revision succeeds.
    fixed = store.apply_command("proj_retry", _move(3, 2.0, cmd_id="m2"))
    assert fixed["revision"] == 4
    assert fixed["clips"]["c1"]["start"] == 2.0
    assert list(tmp_path.glob("*.tmp")) == []
