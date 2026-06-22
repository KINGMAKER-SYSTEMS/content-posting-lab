import json

import pytest

from routers import agenticnews as agn


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
