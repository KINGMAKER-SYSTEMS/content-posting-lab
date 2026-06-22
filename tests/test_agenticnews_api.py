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
import services.abn_assets as abn_assets
from app import app


@pytest.fixture
def abn_db(tmp_path, monkeypatch):
    """Point the agenticnews SQLite singleton at an empty temp DB (no seed) and the
    asset writes at a temp dir, so the suite never touches the real volume state."""
    assets = tmp_path / "assets"
    assets.mkdir()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "abn_test.db")
    monkeypatch.setattr(db, "ASSETS_DIR", assets)
    # abn_assets captured ASSETS_DIR by value at import (`from ... import ASSETS_DIR`),
    # so the gateway must be repointed at the temp dir too or it writes to the real volume.
    monkeypatch.setattr(abn_assets, "ASSETS_DIR", assets)
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


def test_tools_tts_episode_name_routes_through_asset_gateway(client, monkeypatch):
    """An episode-scoped name ('ep_<hex>_sN') must land under {ep_id}/audio/ via the
    services/abn_assets.py gateway — NOT flat in the ASSETS_DIR root where the glob GC
    has eaten original VO. Pins the hard gate: asset writes go through the gateway."""
    import routers.agenticnews as r

    captured = {}

    async def fake_sh(cmd, timeout=300):
        # the gateway-resolved out path is embedded in the pocket-tts --output-path arg;
        # recover it and drop a file so the out.exists() guard passes
        out = db.ASSETS_DIR / "ep_648e806a" / "audio" / "s0_voice.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"RIFF")
        captured["wrote"] = out
        return 0, "ok"

    monkeypatch.setattr(r, "_sh", fake_sh)
    resp = client.post(
        "/api/agenticnews/tools/tts",
        json={"text": "voiceover", "name": "ep_648e806a_s0"},
    )
    assert resp.status_code == 200
    # URL carries the FULL episode subpath, not a colliding basename
    assert resp.json()["path"] == "/agenticnews-assets/ep_648e806a/audio/s0_voice.wav"
    assert captured["wrote"].exists()
    # nothing was written flat in the ASSETS_DIR root
    assert not (db.ASSETS_DIR / "ep_648e806a_s0.wav").exists()


def test_tools_tts_non_episode_name_falls_back_to_scratch(client, monkeypatch):
    """A bare/ad-hoc name (no ep_ prefix) has nowhere to be scoped, so it falls back to the
    REAPABLE _scratch/ dir — NOT the flat ASSETS_DIR root, where the glob GC can't tell an
    intermediate from a keeper and has eaten original VO. The gateway raises AssetPathError
    on the unscoped name and adhoc_scratch_path() routes it to _scratch/."""
    import routers.agenticnews as r

    captured = {}

    async def fake_sh(cmd, timeout=300):
        out = db.ASSETS_DIR / "_scratch" / "vo.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"RIFF")
        captured["wrote"] = out
        return 0, "ok"

    monkeypatch.setattr(r, "_sh", fake_sh)
    resp = client.post("/api/agenticnews/tools/tts", json={"text": "hi", "name": "vo"})
    assert resp.status_code == 200
    # URL carries the _scratch/ subpath so it resolves back to the same reapable file
    assert resp.json()["path"] == "/agenticnews-assets/_scratch/vo.wav"
    assert captured["wrote"].exists()
    # nothing was dumped flat in the ASSETS_DIR root (the glob-GC hazard)
    assert not (db.ASSETS_DIR / "vo.wav").exists()


def test_tools_assemble_episode_name_routes_through_asset_gateway(client, monkeypatch):
    """An episode-scoped name must land under {ep_id}/renders/ via the gateway, with the
    URL carrying the full subpath so it resolves back to the same file."""
    import routers.agenticnews as r

    async def fake_sh(cmd, timeout=300):
        out = db.ASSETS_DIR / "ep_648e806a" / "renders" / "s0_assembled.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00\x00\x00\x18ftyp")
        return 0, "ok"

    monkeypatch.setattr(r, "_sh", fake_sh)
    resp = client.post(
        "/api/agenticnews/tools/assemble",
        json={
            "name": "ep_648e806a_s0",
            "card_path": "/agenticnews-assets/ep_648e806a/css/s0_card.png",
            "vo_path": "/agenticnews-assets/ep_648e806a/audio/s0_voice.wav",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["path"] == "/agenticnews-assets/ep_648e806a/renders/s0_assembled.mp4"
    assert not (db.ASSETS_DIR / "ep_648e806a_s0_assembled.mp4").exists()


def test_tools_cards_non_episode_name_falls_back_to_scratch(client, monkeypatch):
    """tool_cards must catch the gateway's AssetPathError for a non-episode name and fall
    back to the REAPABLE _scratch/ dir (``_scratch/{name}_card.png``) — NOT the flat
    ASSETS_DIR root. A bare 'card' has no ep_ prefix, so split_slug raises and adhoc_scratch_path
    routes it to _scratch/ where the GC can reap it by location instead of guessing from the name."""
    import routers.agenticnews as r

    async def fake_sh(cmd, timeout=60):
        out = db.ASSETS_DIR / "_scratch" / "card_card.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x89PNG")
        return 0, "ok"

    monkeypatch.setattr(r, "_sh", fake_sh)
    resp = client.post("/api/agenticnews/tools/cards", json={"title": "HELLO"})
    assert resp.status_code == 200
    assert resp.json()["path"] == "/agenticnews-assets/_scratch/card_card.png"
    # nothing dumped flat in the root
    assert not (db.ASSETS_DIR / "card_card.png").exists()


def test_tools_cards_episode_name_routes_through_asset_gateway(client, monkeypatch):
    """An episode-scoped card name lands under {ep_id}/css/ via the gateway (the success
    side of the line-293 catch), URL carrying the full subpath."""
    import routers.agenticnews as r

    async def fake_sh(cmd, timeout=60):
        out = db.ASSETS_DIR / "ep_648e806a" / "css" / "s0_card.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x89PNG")
        return 0, "ok"

    monkeypatch.setattr(r, "_sh", fake_sh)
    resp = client.post(
        "/api/agenticnews/tools/cards",
        json={"title": "HELLO", "name": "ep_648e806a_s0"},
    )
    assert resp.status_code == 200
    assert resp.json()["path"] == "/agenticnews-assets/ep_648e806a/css/s0_card.png"
    assert not (db.ASSETS_DIR / "ep_648e806a_s0_card.png").exists()


def test_tools_assemble_non_episode_name_falls_back_to_scratch(client, monkeypatch):
    """tool_assemble must catch the gateway's AssetPathError for a non-episode name and fall
    back to the REAPABLE _scratch/ dir (``_scratch/{name}_assembled.mp4``) — NOT the flat
    ASSETS_DIR root. 'clip' has no ep_ prefix, so split_slug raises and adhoc_scratch_path
    routes it to _scratch/."""
    import routers.agenticnews as r

    async def fake_sh(cmd, timeout=300):
        out = db.ASSETS_DIR / "_scratch" / "clip_assembled.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00\x00\x00\x18ftyp")
        return 0, "ok"

    monkeypatch.setattr(r, "_sh", fake_sh)
    resp = client.post(
        "/api/agenticnews/tools/assemble",
        json={
            "name": "clip",
            "card_path": "/agenticnews-assets/_scratch/card_card.png",
            "vo_path": "/agenticnews-assets/_scratch/vo.wav",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["path"] == "/agenticnews-assets/_scratch/clip_assembled.mp4"
    assert not (db.ASSETS_DIR / "clip_assembled.mp4").exists()


def test_editor_load_reads_timeline_and_render_from_schema_path(client):
    """editor_load must read the timeline + render the factory ACTUALLY wrote — the schema
    paths {ep}/timeline.json and {ep}/renders/episode.mp4 (resolved via the abn_assets
    gateway) — NOT the flat {ep}_timeline.json / {ep}_episode.mp4 legacy names. This is the
    flat-path retirement: reads no longer depend on the migration's back-compat symlinks."""
    ep = "ep_648e806a"
    # seed a video whose id IS the episode id (the factory invariant)
    assert client.post("/api/agenticnews/videos",
                       json={"id": ep, "title": "Schema Read", "stage": "review"}).status_code == 200
    # write the timeline + render where the FACTORY writes them: the schema paths.
    tl = abn_assets.asset_path(ep, "timeline")
    tl.write_text('{"segments": [{"id": "s0", "durationSec": 4}], "totalSec": 4}')
    render = abn_assets.asset_path(ep, "episode")
    render.write_bytes(b"\x00\x00\x00\x18ftyp")
    # NOTHING is written at the flat legacy names — if the read used them it would 404/miss.
    assert not (db.ASSETS_DIR / f"{ep}_timeline.json").exists()
    assert not (db.ASSETS_DIR / f"{ep}_episode.mp4").exists()

    resp = client.get(f"/api/agenticnews/editor/{ep}")
    assert resp.status_code == 200
    body = resp.json()
    # the full on-disk timeline (with segments[]) came from the schema path
    assert body["timeline"].get("segments"), "timeline must be read from {ep}/timeline.json"
    assert body["timeline"]["segments"][0]["id"] == "s0"
    # the player URL points at the schema render, carrying the full subpath
    assert body["videoUrl"] == f"/agenticnews-assets/{ep}/renders/episode.mp4"


def test_editor_load_falls_back_to_flat_render_url_when_unmigrated(client):
    """An episode that hasn't been migrated to the schema yet (only a flat {ep}_episode.mp4
    exists) must still resolve — the read falls back to the flat legacy URL rather than
    pointing at a non-existent schema render."""
    ep = "ep_7a15b8c3"
    assert client.post("/api/agenticnews/videos",
                       json={"id": ep, "title": "Unmigrated", "stage": "review",
                             "timeline": {"segments": [{"id": "s0", "durationSec": 3}]}}
                       ).status_code == 200
    # no schema render — only the flat legacy file exists
    flat_render = db.ASSETS_DIR / f"{ep}_episode.mp4"
    flat_render.write_bytes(b"\x00\x00\x00\x18ftyp")
    assert not (abn_assets.episode_singleton_path(ep, "episode")).exists()  # schema render absent

    resp = client.get(f"/api/agenticnews/editor/{ep}")
    assert resp.status_code == 200
    assert resp.json()["videoUrl"] == f"/agenticnews-assets/{ep}_episode.mp4"


def test_episode_qa_closes_props_file_handle(client, monkeypatch, tmp_path):
    """Regression: episode_qa read the render-props json with a bare open() passed
    straight into json.load(), leaking the file descriptor on every call (the
    'too many open files' failure mode under load). Wrap the builtin open so we can
    assert every handle the endpoint opens for the props file is closed by the time
    the response comes back."""
    import builtins
    import routers.agenticnews as r

    # The QA endpoint now routes through the abn_assets gateway (no RAILWAY_VOLUME env
    # hand-built path). For a non-schema id like 'ep_qa_test' the gateway returns the flat
    # fallback under db.ASSETS_DIR (the abn_db temp dir) — write the props there.
    props = db.ASSETS_DIR / "ep_qa_test_timeline.json"
    props.write_text(
        '{"segments": [{"title": "Foo"}, {"title": "Bar"}, {"title": "Baz"}],'
        ' "musicBed": "bed.wav", "sfx": []}'
    )

    opened = []
    real_open = builtins.open

    def tracking_open(file, *a, **k):
        fh = real_open(file, *a, **k)
        if str(file) == str(props):
            opened.append(fh)
        return fh

    monkeypatch.setattr(r, "open", tracking_open, raising=False)

    resp = client.get("/api/agenticnews/episodes/ep_qa_test/qa")
    assert resp.status_code == 200
    assert resp.json()["props_found"] is True
    # the props file was actually read, and every handle opened for it is closed
    assert opened, "endpoint never opened the props file"
    assert all(fh.closed for fh in opened), "leaked an open file handle for props"


def test_episode_qa_reads_schema_paths_not_flat_legacy(client):
    """The ticket: episode_qa must route through the abn_assets gateway and grade the
    timeline the factory ACTUALLY wrote at the schema path {ep}/timeline.json — NOT the
    flat {ep}_timeline.json legacy name. Write the props ONLY at the schema path; if QA
    used the flat path it would miss them (props_found False)."""
    ep = "ep_648e806a"
    assert client.post("/api/agenticnews/videos",
                       json={"id": ep, "title": "QA schema", "stage": "review"}).status_code == 200
    # write the render-props where the FACTORY writes it: the gateway schema path.
    tl = abn_assets.asset_path(ep, "timeline")
    tl.write_text(
        '{"segments": [{"title": "Foo"}, {"title": "Bar"}, {"title": "Baz"}],'
        ' "musicBed": "bed.wav", "sfx": [{"at": 1}]}'
    )
    # NOTHING at the flat legacy name — if QA read the flat path it would miss the props.
    assert not (db.ASSETS_DIR / f"{ep}_timeline.json").exists()

    resp = client.get(f"/api/agenticnews/episodes/{ep}/qa")
    assert resp.status_code == 200
    body = resp.json()
    assert body["props_found"] is True, "QA did not read the schema timeline.json"
    assert body["checks"]["has_segments"] is True
    assert body["checks"]["music_bed"] is True
    assert body["checks"]["sfx"] is True


def test_tools_tts_success_attaches_artifact_to_video(client, monkeypatch):
    """On a successful render the VO path is auto-attached to the linked card so the
    caller can't desync it. Stub the shell-out AND drop a real output file so the
    `out.exists()` guard passes."""
    import routers.agenticnews as r

    vid = client.post("/api/agenticnews/videos", json={"title": "VO target"}).json()["id"]

    async def fake_sh(cmd, timeout=300):
        # a video id has no ep_ prefix, so the render lands in the reapable _scratch/ dir,
        # not flat in the root. Create it there so the out.exists() guard passes.
        out = db.ASSETS_DIR / "_scratch" / f"{vid}.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"RIFF")
        return 0, "ok"

    monkeypatch.setattr(r, "_sh", fake_sh)
    resp = client.post(
        "/api/agenticnews/tools/tts", json={"text": "voiceover", "video_id": vid}
    )
    assert resp.status_code == 200
    assert resp.json()["path"] == f"/agenticnews-assets/_scratch/{vid}.wav"
    v = next(
        x for x in client.get("/api/agenticnews/videos").json()["videos"]
        if x["id"] == vid
    )
    assert v["artifacts"]["vo"] is True
    assert v["artifacts"]["vo_path"] == f"/agenticnews-assets/_scratch/{vid}.wav"


# --------------------------------------------------------- tools/* more 400/500
def test_tools_cards_rejects_when_render_fails_without_video(client, monkeypatch):
    """/tools/cards 500 path when no video_id is attached: a non-zero magick exit
    surfaces as a 500 and nothing is auto-attached (no card to a phantom video)."""
    import routers.agenticnews as r

    async def fake_sh(cmd, timeout=60):
        return 127, "magick: not found"

    monkeypatch.setattr(r, "_sh", fake_sh)
    resp = client.post("/api/agenticnews/tools/cards", json={"title": "HI", "name": "loose"})
    assert resp.status_code == 500
    assert "card failed" in resp.json()["detail"]


def test_tools_assemble_rejects_missing_vo_only(client):
    """assemble needs BOTH card_path and vo_path — supplying only one is a 400."""
    r = client.post(
        "/api/agenticnews/tools/assemble",
        json={"name": "clip", "card_path": "/agenticnews-assets/x_card.png"},
    )
    assert r.status_code == 400
    assert "need card_path and vo_path" in r.json()["detail"]


# --------------------------------------------------------- publish-package 404
def test_publish_package_missing_episode_returns_404(client):
    r = client.get("/api/agenticnews/episodes/ep_nope/publish-package")
    assert r.status_code == 404
    assert "episode not found" in r.json()["detail"]


# --------------------------------------------------- editor-timelines create/import
def test_editor_timeline_create_requires_project_id(client):
    """POST /editor-timelines with no projectId is a 400 (line 1414)."""
    r = client.post("/api/agenticnews/editor-timelines", json={"title": "x"})
    assert r.status_code == 400
    assert "projectId is required" in r.json()["detail"]


def test_editor_timeline_create_rejects_demo_project(client):
    """Demo/sandbox project ids are disabled — a 'demo*' id is a 400 (line 584)."""
    r = client.post(
        "/api/agenticnews/editor-timelines", json={"projectId": "demo-playground"}
    )
    assert r.status_code == 400
    assert "demo editor timelines are disabled" in r.json()["detail"]


def test_editor_timeline_import_abn_requires_timeline_object(client):
    """import-abn needs a `timeline` dict in the body — a missing/non-dict value is 400 (line 1432)."""
    r = client.post(
        "/api/agenticnews/editor-timelines/ep_real1/import-abn",
        json={"sourceEpisodeId": "ep_real1"},
    )
    assert r.status_code == 400
    assert "timeline object is required" in r.json()["detail"]


# ------------------------------------------------------ editor-timelines commands
def _make_editor_project(client, project_id="ep_cmd1"):
    """Create a real (non-demo) editor timeline and return (id, revision)."""
    resp = client.post(
        "/api/agenticnews/editor-timelines", json={"projectId": project_id}
    )
    assert resp.status_code == 201, resp.text
    return project_id, int(resp.json()["revision"])


def test_editor_command_missing_project_returns_404(client):
    """Applying a command to a project that was never created is a 404 (line 1480)."""
    r = client.post(
        "/api/agenticnews/editor-timelines/ep_ghost/commands",
        json={"op": "marker.add", "expectedRevision": 0, "payload": {}},
    )
    assert r.status_code == 404
    assert "editor timeline not found" in r.json()["detail"]


def test_editor_command_revision_conflict_returns_409(client):
    """A command whose expectedRevision is stale is a 409 RevisionConflict (line 1482)."""
    pid, rev = _make_editor_project(client, "ep_conflict")
    r = client.post(
        f"/api/agenticnews/editor-timelines/{pid}/commands",
        json={"op": "marker.add", "expectedRevision": rev + 5, "payload": {}},
    )
    assert r.status_code == 409


def test_editor_command_missing_expected_revision_returns_400(client):
    """A command with no expectedRevision is a CommandValidationError -> 400 (line 1484)."""
    pid, _rev = _make_editor_project(client, "ep_norev")
    r = client.post(
        f"/api/agenticnews/editor-timelines/{pid}/commands",
        json={"op": "marker.add", "payload": {}},
    )
    assert r.status_code == 400


def test_editor_command_unsupported_op_returns_400(client):
    """An unrecognised op is a CommandValidationError -> 400 (line 1484)."""
    pid, rev = _make_editor_project(client, "ep_badop")
    r = client.post(
        f"/api/agenticnews/editor-timelines/{pid}/commands",
        json={"op": "not.a.real.op", "expectedRevision": rev, "payload": {}},
    )
    assert r.status_code == 400


def test_editor_command_rejects_demo_project(client):
    """Commands against a demo project id are rejected with a 400 before any store hit."""
    r = client.post(
        "/api/agenticnews/editor-timelines/demo-x/commands",
        json={"op": "marker.add", "expectedRevision": 0, "payload": {}},
    )
    assert r.status_code == 400
    assert "demo editor timelines are disabled" in r.json()["detail"]


# ----------------------------------------------- editor-timelines revert-last
def test_editor_revert_last_requires_expected_revision(client):
    """revert-last with no expectedRevision in the body is a 400 (line 1491)."""
    pid, _rev = _make_editor_project(client, "ep_revert1")
    r = client.post(
        f"/api/agenticnews/editor-timelines/{pid}/commands/revert-last", json={}
    )
    assert r.status_code == 400
    assert "expectedRevision is required" in r.json()["detail"]


def test_editor_revert_last_non_integer_expected_revision_returns_400(client):
    """A non-integer expectedRevision is coerced and fails as a 400 (line 1508)."""
    pid, _rev = _make_editor_project(client, "ep_revert2")
    r = client.post(
        f"/api/agenticnews/editor-timelines/{pid}/commands/revert-last",
        json={"expectedRevision": "not-a-number"},
    )
    assert r.status_code == 400


def test_editor_revert_last_missing_project_returns_404(client):
    """revert-last on a project that doesn't exist is a 404 (line 1503)."""
    r = client.post(
        "/api/agenticnews/editor-timelines/ep_no_such/commands/revert-last",
        json={"expectedRevision": 0},
    )
    assert r.status_code == 404


def test_editor_revert_last_nothing_to_revert_returns_400(client):
    """A fresh project has an empty command log; reverting is a CommandValidationError -> 400 (line 1506)."""
    pid, rev = _make_editor_project(client, "ep_revert3")
    r = client.post(
        f"/api/agenticnews/editor-timelines/{pid}/commands/revert-last",
        json={"expectedRevision": rev},
    )
    assert r.status_code == 400


# --------------------------------------------------------- editor-render 400
def test_editor_render_rejects_non_numeric_start(client):
    """start/duration must be numeric — a non-numeric value is a 400 (line 1538)."""
    pid, _rev = _make_editor_project(client, "ep_render1")
    r = client.post(
        f"/api/agenticnews/editor-render/{pid}/render",
        json={"start": "soon", "duration": 5},
    )
    assert r.status_code == 400
    assert "must be numeric" in r.json()["detail"]


# --------------------------------------------------- publish-package (no abn_youtube)
def test_publish_package_survives_missing_youtube_module(client):
    """services/abn_youtube.py does not exist yet (the whole publish path is dormant
    until OAuth creds + that module are built). GET /publish-package must still return
    200 with the dormant blockers, not 500 with a ModuleNotFoundError. Regression for
    `_publish_blockers` importing a missing module unguarded."""
    vid = client.post(
        "/api/agenticnews/videos", json={"title": "Publish me", "kind": "episode"}
    ).json()["id"]
    r = client.get(f"/api/agenticnews/episodes/{vid}/publish-package")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is False
    assert any("YouTube OAuth not configured" in b for b in body["blockers"])


def test_publish_endpoint_blocks_instead_of_500_when_youtube_missing(client):
    """POST /publish imports the same missing module — it must report blocked, not 500."""
    vid = client.post(
        "/api/agenticnews/videos", json={"title": "Publish me", "kind": "episode"}
    ).json()["id"]
    r = client.post(f"/api/agenticnews/episodes/{vid}/publish", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["blocked"] is True
    assert body["blockers"]


# ---------------------------------------------------------- _regen_card: atomic swap + traversal guard
#
# _regen_card (routers/agenticnews.py) regenerates ONE designed-card PNG in place from an
# OPERATOR-SUPPLIED src. The src is hostile input, so the function has two hard guards:
#   1. the resolved target must be an EXISTING, MANAGED card under the episode schema
#      ({ep_id}/css/…) — a traversal (../), a symlink, a bare name, or an off-schema path
#      must be rejected (return False, write nothing) BEFORE any PNG is generated.
#   2. the new PNG renders to a sibling .tmp_* stem, then os.replace's onto the live target —
#      an atomic swap, so a crashed/partial render can never leave a torn card in place of the
#      good one, and the .tmp_* scratch is cleaned up on failure.
# The happy path is the only thing the rest of the suite touches; these cover the guards.


@pytest.fixture
def regen_env(abn_db, tmp_path, monkeypatch):
    """A real managed card on disk under {ep_id}/css/ + factory repointed at the temp store.

    factory.ASSETS and factory._v2cards are import-time captures, so the abn_db fixture (which
    only repoints db/abn_assets ASSETS_DIR) is not enough — _regen_card resolves the write path
    via factory._resolve_asset (factory.ASSETS) and renders via factory._v2cards.
    """
    import routers.agenticnews as r
    import services.abn_factory as factory

    assets = abn_db.ASSETS_DIR
    monkeypatch.setattr(factory, "ASSETS", assets)

    css = assets / "ep_abc123" / "css"
    css.mkdir(parents=True)
    target = css / "s0_v1sc0_hook.png"
    target.write_bytes(b"ORIGINAL-CARD-BYTES")
    src = "/agenticnews-assets/ep_abc123/css/s0_v1sc0_hook.png"

    return r, factory, assets, target, src


def _stub_cards(monkeypatch, factory, *, writes=True, raises=False):
    """Install a fake _v2cards that records the tmp stem it was handed and (optionally) writes
    a tmp PNG / raises mid-render — so we can assert the atomic swap + cleanup behaviour without
    ImageMagick on PATH."""
    calls = {}

    class _FakeCards:
        BRAND_CYAN = "#0ff"
        BRAND_RED = "#f00"

        @staticmethod
        def hook_card(txt, stem, A, F, accent=None):
            calls["stem"] = stem
            out = A / f"{stem}_hook.png"
            if raises:
                raise RuntimeError("magick crashed mid-render")
            if writes:
                out.write_bytes(b"NEW-CARD-BYTES")
            return out

    monkeypatch.setattr(factory, "_v2cards", _FakeCards)
    return calls


def test_regen_card_rejects_path_traversal(regen_env, monkeypatch):
    """A `../`-laden src must never resolve to a write outside the schema — return False, no write."""
    r, factory, assets, _target, _src = regen_env
    # craft an escape that still ends in a recognized card kind so it passes the _CARD_KIND regex
    evil = "/agenticnews-assets/../../../../tmp/pwned_hook.png"
    sentinel = assets.parent / "pwned_hook.png"
    assert not sentinel.exists()
    assert r._regen_card(evil, {"text": "x"}) is False
    assert not sentinel.exists()  # nothing written outside the schema


def test_regen_card_rejects_backslash_traversal(regen_env):
    """A `..\\`-style src is just a bare (non-managed) name on POSIX — must be rejected too."""
    r, factory, assets, _target, _src = regen_env
    assert r._regen_card("..\\..\\evil_hook.png", {"text": "x"}) is False


def test_regen_card_rejects_bare_unmanaged_name(regen_env):
    """A bare name resolves under the store root but is NOT a managed/existing card → False."""
    r, *_ = regen_env
    assert r._regen_card("orphan_hook.png", {"text": "x"}) is False


def test_regen_card_rejects_symlink_target(regen_env):
    """Even a symlink that lives inside css/ must be refused — it could point anywhere."""
    r, factory, assets, target, _src = regen_env
    link = target.parent / "s9_link_hook.png"
    link.symlink_to(target)
    assert r._regen_card("/agenticnews-assets/ep_abc123/css/s9_link_hook.png", {"text": "x"}) is False
    assert target.read_bytes() == b"ORIGINAL-CARD-BYTES"  # link's target untouched


def test_regen_card_rejects_non_card_src(regen_env):
    """src whose name isn't a known card kind never even resolves a target → False."""
    r, *_ = regen_env
    assert r._regen_card("/agenticnews-assets/ep_abc123/css/s0_v1sc0.png", {"text": "x"}) is False


def test_regen_card_atomic_swap_replaces_in_place(regen_env, monkeypatch):
    """Happy path: the new PNG is rendered to a .tmp_* sibling, then os.replace'd onto the live
    target — the operator never sees a torn card, and no .tmp_* scratch is left behind."""
    r, factory, assets, target, src = regen_env
    calls = _stub_cards(monkeypatch, factory, writes=True)

    assert r._regen_card(src, {"text": "fresh hook"}) is True
    assert target.read_bytes() == b"NEW-CARD-BYTES"          # swapped to the new content
    assert calls["stem"].startswith(".tmp_")                  # rendered to a temp stem first
    leftover = list(target.parent.glob(".tmp_*"))
    assert leftover == []                                     # tmp consumed by the atomic replace


def test_regen_card_render_failure_leaves_original_and_cleans_tmp(regen_env, monkeypatch):
    """If the generator crashes mid-render, the live card is untouched and the partial .tmp_* file
    is removed — a failed swap can never torch the good card or litter scratch."""
    r, factory, assets, target, src = regen_env
    # writes=True so a partial tmp file lands on disk, then raises BEFORE os.replace
    calls = {}

    class _PartialThenCrash:
        BRAND_CYAN = "#0ff"
        BRAND_RED = "#f00"

        @staticmethod
        def hook_card(txt, stem, A, F, accent=None):
            calls["stem"] = stem
            (A / f"{stem}_hook.png").write_bytes(b"HALF-WRITTEN")
            raise RuntimeError("crash after partial write")

    monkeypatch.setattr(factory, "_v2cards", _PartialThenCrash)

    assert r._regen_card(src, {"text": "x"}) is False
    assert target.read_bytes() == b"ORIGINAL-CARD-BYTES"      # live card untouched on failure
    assert list(target.parent.glob(".tmp_*")) == []          # half-written tmp cleaned up


def test_regen_card_swap_unlink_failure_does_not_corrupt_target(regen_env, monkeypatch):
    """If os.replace itself fails (e.g. tmp write succeeded but the swap raises), _regen_card
    returns False and the original card is left intact rather than half-replaced."""
    r, factory, assets, target, src = regen_env
    _stub_cards(monkeypatch, factory, writes=True)

    import os as _os
    real_replace = _os.replace

    def _boom(a, b):
        raise OSError("rename failed across devices")

    monkeypatch.setattr(r.os, "replace", _boom)

    assert r._regen_card(src, {"text": "x"}) is False
    assert target.read_bytes() == b"ORIGINAL-CARD-BYTES"      # never partially swapped
    assert list(target.parent.glob(".tmp_*")) == []          # tmp scratch still cleaned up
    monkeypatch.setattr(r.os, "replace", real_replace)
