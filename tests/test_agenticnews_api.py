"""Integration tests for the AgenticBuilderNews / editor-bay router.

`routers/agenticnews.py` is the editor-bay + content-flywheel API and shipped with
zero integration tests. These exercise the critical data-mutating endpoints and
their side effects: video create/patch/move state transitions, the job
claim/lock lifecycle, chat-message persistence, and the tools/* validation +
error-handling contract (without requiring pocket-tts / magick / ffmpeg on PATH).

The router's data layer is a module-level SQLite singleton (`services.agenticnews`),
so each test runs against a fresh temp DB via the `abn_db` fixture.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import services.agenticnews as db
from app import app


@pytest.fixture
def abn_db(tmp_path, monkeypatch):
    """Point the agenticnews SQLite singleton at an empty temp DB (no seed) and the
    asset writes at a temp dir, so the suite never touches the real volume state."""
    assets = tmp_path / "assets"
    assets.mkdir()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "abn_test.db")
    monkeypatch.setattr(db, "ASSETS_DIR", assets)
    monkeypatch.setattr(db, "_conn", None)  # force a fresh connection on the temp path
    db._init_sync()
    yield db
    if db._conn is not None:
        db._conn.close()
    monkeypatch.setattr(db, "_conn", None)


@pytest.fixture
def client(abn_db):
    # NOTE: TestClient is intentionally NOT used as a context manager. Entering the
    # context runs the app lifespan, which seeds the DB AND spawns the autonomous
    # factory loop (`abn_factory.start_factory`) — that loop would mutate our temp
    # board mid-test. The router only needs the schema, which `abn_db` already set up.
    return TestClient(app)


# ---------------------------------------------------------------- videos / board
def test_create_video_returns_defaults_and_id(client):
    r = client.post("/api/agenticnews/videos", json={"title": "Test Episode"})
    assert r.status_code == 200
    v = r.json()
    assert v["id"].startswith("v_")
    assert v["title"] == "Test Episode"
    assert v["stage"] == "idea"           # default stage
    assert v["lane"] == "week"            # default lane
    assert v["artifacts"] == {}


def test_create_video_does_not_validate_stage(client):
    """POST /videos has NO stage validation — it stores whatever is sent. The board
    GET still lists it, but a bogus stage is silently absent from /stats buckets.
    Documents the actual (lenient) contract so a future tightening is intentional."""
    r = client.post(
        "/api/agenticnews/videos",
        json={"title": "Bad Stage", "stage": "not_a_real_stage"},
    )
    assert r.status_code == 200
    assert r.json()["stage"] == "not_a_real_stage"
    # the bogus card is still retrievable on the board listing
    listed = client.get("/api/agenticnews/videos?stage=not_a_real_stage").json()
    assert any(v["title"] == "Bad Stage" for v in listed["videos"])


def test_patch_video_missing_returns_404(client):
    r = client.patch("/api/agenticnews/videos/does_not_exist", json={"stage": "ready"})
    assert r.status_code == 404


def test_patch_video_merges_artifacts(client):
    vid = client.post("/api/agenticnews/videos", json={"title": "Merge"}).json()["id"]
    client.patch(
        f"/api/agenticnews/videos/{vid}", json={"artifacts": {"script": True}}
    )
    r = client.patch(
        f"/api/agenticnews/videos/{vid}", json={"artifacts": {"vo": True}}
    )
    assert r.status_code == 200
    arts = r.json()["artifacts"]
    assert arts == {"script": True, "vo": True}   # merged, not overwritten


def test_video_stage_transitions_idea_to_scripting(client):
    """idea -> ready -> scripting through the validated /move endpoint."""
    vid = client.post("/api/agenticnews/videos", json={"title": "Flow"}).json()["id"]
    for stage in ("ready", "scripting"):
        r = client.post(f"/api/agenticnews/videos/{vid}/move", json={"stage": stage})
        assert r.status_code == 200
        assert r.json()["stage"] == stage


def test_move_video_rejects_invalid_stage(client):
    vid = client.post("/api/agenticnews/videos", json={"title": "Flow"}).json()["id"]
    r = client.post(
        f"/api/agenticnews/videos/{vid}/move", json={"stage": "bogus"}
    )
    assert r.status_code == 400


def test_move_video_missing_returns_404(client):
    r = client.post("/api/agenticnews/videos/nope/move", json={"stage": "ready"})
    assert r.status_code == 404


def test_delete_video_is_idempotent(client):
    vid = client.post("/api/agenticnews/videos", json={"title": "Doomed"}).json()["id"]
    assert client.delete(f"/api/agenticnews/videos/{vid}").json() == {"ok": True}
    # deleting again still returns ok (DELETE is unconditional)
    assert client.delete(f"/api/agenticnews/videos/{vid}").json() == {"ok": True}
    assert client.patch(
        f"/api/agenticnews/videos/{vid}", json={"stage": "ready"}
    ).status_code == 404


# ----------------------------------------------------------------------- jobs
def test_job_claim_locks_linked_video(client):
    vid = client.post("/api/agenticnews/videos", json={"title": "Job target"}).json()["id"]
    jid = client.post(
        "/api/agenticnews/jobs", json={"video_id": vid, "job_type": "render"}
    ).json()["id"]

    r = client.post(f"/api/agenticnews/jobs/{jid}/claim", json={"agent_id": "a1"})
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "running"
    assert j["agent_id"] == "a1"
    # the linked video card is now locked to that agent
    v = next(
        x for x in client.get("/api/agenticnews/videos").json()["videos"]
        if x["id"] == vid
    )
    assert v["locked_by"] == "a1"


def test_job_second_claim_steals_lock(client):
    """No optimistic-lock guard exists: a second claim succeeds and re-locks the
    video to the new agent. This pins the current (steal-on-claim) behavior so a
    future concurrency fix is a deliberate, test-driven change rather than a silent
    regression."""
    jid = client.post(
        "/api/agenticnews/jobs", json={"job_type": "render"}
    ).json()["id"]
    first = client.post(f"/api/agenticnews/jobs/{jid}/claim", json={"agent_id": "a1"})
    second = client.post(f"/api/agenticnews/jobs/{jid}/claim", json={"agent_id": "a2"})
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["agent_id"] == "a2"   # second claim is NOT rejected


def test_job_claim_missing_returns_404(client):
    r = client.post("/api/agenticnews/jobs/ghost/claim", json={"agent_id": "a1"})
    assert r.status_code == 404


def test_job_complete_unlocks_video_and_applies_stage(client):
    vid = client.post("/api/agenticnews/videos", json={"title": "Completing"}).json()["id"]
    jid = client.post(
        "/api/agenticnews/jobs", json={"video_id": vid, "job_type": "render"}
    ).json()["id"]
    client.post(f"/api/agenticnews/jobs/{jid}/claim", json={"agent_id": "a1"})

    r = client.post(
        f"/api/agenticnews/jobs/{jid}/complete",
        json={"result": {"ok": True}, "stage": "review"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "done"
    v = next(
        x for x in client.get("/api/agenticnews/videos").json()["videos"]
        if x["id"] == vid
    )
    assert v["locked_by"] is None      # lock released on completion
    assert v["stage"] == "review"      # stage advanced from job result


def test_job_fail_releases_lock(client):
    vid = client.post("/api/agenticnews/videos", json={"title": "Failing"}).json()["id"]
    jid = client.post(
        "/api/agenticnews/jobs", json={"video_id": vid, "job_type": "render"}
    ).json()["id"]
    client.post(f"/api/agenticnews/jobs/{jid}/claim", json={"agent_id": "a1"})

    r = client.post(f"/api/agenticnews/jobs/{jid}/fail", json={"error": "boom"})
    assert r.status_code == 200
    assert r.json()["status"] == "failed"
    v = next(
        x for x in client.get("/api/agenticnews/videos").json()["videos"]
        if x["id"] == vid
    )
    assert v["locked_by"] is None


# ----------------------------------------------------------------------- chat
def test_chat_roundtrip_persists_and_drains(client):
    client.post("/api/agenticnews/chat", json={"text": "hello from operator"})
    # /chat/inbox drains user->claude messages exactly once
    inbox = client.get("/api/agenticnews/chat/inbox").json()["messages"]
    assert inbox == ["hello from operator"]
    # draining is destructive: a second poll is empty
    assert client.get("/api/agenticnews/chat/inbox").json()["messages"] == []

    client.post("/api/agenticnews/chat/reply", json={"text": "ack from claude"})
    poll = client.get("/api/agenticnews/chat/poll").json()["messages"]
    assert poll == ["ack from claude"]

    # history is non-destructive and ordered oldest->newest
    hist = client.get("/api/agenticnews/chat/history").json()["messages"]
    texts = [(m["who"], m["text"]) for m in hist]
    assert ("user", "hello from operator") in texts
    assert ("claude", "ack from claude") in texts


# ------------------------------------------------------------------- tools/*
def test_tools_tts_rejects_empty_text(client):
    r = client.post("/api/agenticnews/tools/tts", json={"text": "   "})
    assert r.status_code == 400


def test_tools_assemble_requires_card_and_vo(client):
    r = client.post("/api/agenticnews/tools/assemble", json={"name": "clip"})
    assert r.status_code == 400


def test_tools_tts_surfaces_render_failure(client, monkeypatch):
    """When the underlying pocket-tts shell-out fails (nonzero / no output file),
    the endpoint must surface a 500 rather than claim success. We stub the shell
    runner so the test needs no binaries and stays hermetic."""
    import routers.agenticnews as r

    async def fake_sh(cmd, timeout=300):
        return 1, "pocket-tts: command not found"

    monkeypatch.setattr(r, "_sh", fake_sh)
    resp = client.post(
        "/api/agenticnews/tools/tts", json={"text": "render me"}
    )
    assert resp.status_code == 500
    assert "tts failed" in resp.json()["detail"]


def test_tools_cards_surfaces_render_failure(client, monkeypatch):
    import routers.agenticnews as r

    async def fake_sh(cmd, timeout=60):
        return 1, "magick: not installed"

    monkeypatch.setattr(r, "_sh", fake_sh)
    resp = client.post(
        "/api/agenticnews/tools/cards", json={"title": "HELLO"}
    )
    assert resp.status_code == 500
    assert "card failed" in resp.json()["detail"]


def test_tools_tts_success_attaches_artifact_to_video(client, monkeypatch):
    """On a successful render the VO path is auto-attached to the linked card so the
    caller can't desync it. Stub the shell-out AND drop a real output file so the
    `out.exists()` guard passes."""
    import routers.agenticnews as r

    vid = client.post("/api/agenticnews/videos", json={"title": "VO target"}).json()["id"]

    async def fake_sh(cmd, timeout=300):
        # the command writes to ASSETS_DIR/<name>.wav; create it so the guard passes
        out = db.ASSETS_DIR / f"{vid}.wav"
        out.write_bytes(b"RIFF")
        return 0, "ok"

    monkeypatch.setattr(r, "_sh", fake_sh)
    resp = client.post(
        "/api/agenticnews/tools/tts", json={"text": "voiceover", "video_id": vid}
    )
    assert resp.status_code == 200
    assert resp.json()["path"] == f"/agenticnews-assets/{vid}.wav"
    v = next(
        x for x in client.get("/api/agenticnews/videos").json()["videos"]
        if x["id"] == vid
    )
    assert v["artifacts"]["vo"] is True
    assert v["artifacts"]["vo_path"] == f"/agenticnews-assets/{vid}.wav"
