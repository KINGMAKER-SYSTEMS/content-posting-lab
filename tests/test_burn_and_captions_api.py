import asyncio

import pytest

from project_manager import (
    get_project_burn_dir,
    get_project_caption_dir,
    get_project_clips_dir,
    get_project_video_dir,
)
from routers import burn as burn_router


def test_burn_overlay_rejects_path_traversal(sync_client):
    created = sync_client.post("/api/projects", json={"name": "Traversal Suite"})
    assert created.status_code == 201

    resp = sync_client.post(
        "/api/burn/overlay",
        json={
            "project": "traversal-suite",
            "batchId": "batch-y",
            "index": 0,
            "videoPath": "../../etc/passwd",
        },
    )
    assert resp.status_code == 400
    assert resp.json().get("error") == "Invalid path"


def test_legacy_burn_ws_route_removed(sync_client):
    """The legacy /api/burn/ws WebSocket endpoint was removed; nothing serves it."""
    routes = [getattr(r, "path", None) for r in sync_client.app.routes]
    assert "/api/burn/ws" not in routes

    # Attempting to open the WS connection must fail (no route to accept it).
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises((WebSocketDisconnect, Exception)):
        with sync_client.websocket_connect("/api/burn/ws"):
            pass


def test_burn_list_endpoints(sync_client):
    created = sync_client.post("/api/projects", json={"name": "Burn Suite"})
    assert created.status_code == 201

    video_dir = get_project_video_dir("burn-suite") / "provider-a"
    caption_dir = get_project_caption_dir("burn-suite") / "artist123"
    burn_dir = get_project_burn_dir("burn-suite") / "batch-x"

    video_dir.mkdir(parents=True, exist_ok=True)
    caption_dir.mkdir(parents=True, exist_ok=True)
    burn_dir.mkdir(parents=True, exist_ok=True)

    (video_dir / "clip.mp4").write_bytes(b"video")
    (caption_dir / "captions.csv").write_text(
        "video_id,video_url,caption,error\n"
        "123,https://tiktok.com/@artist/video/123,hello world,\n",
        encoding="utf-8",
    )
    (burn_dir / "burned_000.mp4").write_bytes(b"burned")

    videos_response = sync_client.get(
        "/api/burn/videos", params={"project": "burn-suite"}
    )
    assert videos_response.status_code == 200
    videos = videos_response.json()["videos"]
    assert len(videos) == 1
    assert videos[0]["name"] == "clip.mp4"

    captions_response = sync_client.get(
        "/api/burn/captions", params={"project": "burn-suite"}
    )
    assert captions_response.status_code == 200
    sources = captions_response.json()["sources"]
    assert len(sources) == 1
    assert sources[0]["username"] == "artist123"
    assert sources[0]["captions"][0]["text"] == "hello world"

    batches_response = sync_client.get(
        "/api/burn/batches", params={"project": "burn-suite"}
    )
    assert batches_response.status_code == 200
    batches = batches_response.json()["batches"]
    assert len(batches) == 1
    assert batches[0]["id"] == "batch-x"


def test_burn_batch_status_unknown_batch(sync_client):
    """Polling an unknown/not-yet-registered batch returns a safe empty shape,
    never a 500 — the frontend may poll before /overlay registers the batch."""
    resp = sync_client.get("/api/burn/batch-status/never-seen-batch")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "batchId": "never-seen-batch",
        "items": {},
        "total": 0,
        "done": 0,
        "ok": 0,
        "failed": 0,
    }


def test_burn_batch_status_aggregates_mixed_items(sync_client):
    """batch-status returns the per-item dict plus the done/ok/failed counts the
    Burn.tsx poll loop reads. Seed _burn_jobs directly (real burns need ffmpeg)."""
    import routers.burn as burn

    batch_id = "status-suite-batch"
    burn._burn_jobs[batch_id] = {
        0: {"status": "done", "index": 0, "ok": True, "file": f"{batch_id}/burned_000.mp4"},
        1: {"status": "error", "index": 1, "ok": False, "error": "boom"},
        2: {"status": "burning"},
    }
    try:
        resp = sync_client.get(f"/api/burn/batch-status/{batch_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["batchId"] == batch_id
        assert body["total"] == 3
        # done counts both "done" and "error" terminal states
        assert body["done"] == 2
        assert body["ok"] == 1
        assert body["failed"] == 1
        # items are keyed by index (JSON stringifies int keys) — Burn.tsx reads finalResults[String(i)]
        assert body["items"]["0"]["file"] == f"{batch_id}/burned_000.mp4"
        assert body["items"]["1"]["error"] == "boom"
    finally:
        burn._burn_jobs.pop(batch_id, None)


def test_caption_export_endpoint(sync_client):
    created = sync_client.post("/api/projects", json={"name": "Caption Suite"})
    assert created.status_code == 201

    username = "creator1"
    csv_dir = get_project_caption_dir("caption-suite") / username
    csv_dir.mkdir(parents=True, exist_ok=True)
    csv_path = csv_dir / "captions.csv"
    csv_path.write_text(
        "video_id,video_url,caption,error\n"
        "1,https://tiktok.com/@creator1/video/1,my caption,\n",
        encoding="utf-8",
    )

    response = sync_client.get(
        f"/api/captions/export/{username}", params={"project": "caption-suite"}
    )
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "my caption" in response.text

    missing = sync_client.get(
        "/api/captions/export/missing-user", params={"project": "caption-suite"}
    )
    assert missing.status_code == 404


def test_burn_folder_rename_happy_path(sync_client):
    created = sync_client.post("/api/projects", json={"name": "Rename Happy"})
    assert created.status_code == 201

    video_dir = get_project_video_dir("rename-happy") / "oldname"
    video_dir.mkdir(parents=True, exist_ok=True)
    (video_dir / "clip.mp4").write_bytes(b"video")

    resp = sync_client.patch(
        "/api/burn/folders/rename",
        params={"project": "rename-happy"},
        json={"folder": "oldname", "new_name": "new name"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["old_folder"] == "oldname"
    # "new name" sanitizes to "new-name"
    assert body["new_folder"] == "new-name"

    # Disk state: old dir gone, new dir contains the file
    assert not (get_project_video_dir("rename-happy") / "oldname").exists()
    assert (get_project_video_dir("rename-happy") / "new-name" / "clip.mp4").exists()

    # videos list reflects the rename
    videos = sync_client.get(
        "/api/burn/videos", params={"project": "rename-happy"}
    ).json()["videos"]
    folders = {v["folder"] for v in videos}
    assert "new-name" in folders
    assert "oldname" not in folders
    # `created` field is now populated
    assert all("created" in v and isinstance(v["created"], int) for v in videos)


def test_burn_folder_rename_collision(sync_client):
    created = sync_client.post("/api/projects", json={"name": "Rename Collide"})
    assert created.status_code == 201

    root = get_project_video_dir("rename-collide")
    (root / "alpha").mkdir(parents=True, exist_ok=True)
    (root / "beta").mkdir(parents=True, exist_ok=True)
    (root / "alpha" / "a.mp4").write_bytes(b"a")
    (root / "beta" / "b.mp4").write_bytes(b"b")

    resp = sync_client.patch(
        "/api/burn/folders/rename",
        params={"project": "rename-collide"},
        json={"folder": "alpha", "new_name": "beta"},
    )
    assert resp.status_code == 409
    # Both dirs still present — no partial state
    assert (root / "alpha").exists()
    assert (root / "beta").exists()


def test_burn_folder_rename_rejects_virtual_and_root(sync_client):
    created = sync_client.post("/api/projects", json={"name": "Rename Virt"})
    assert created.status_code == 201

    # Virtual run_* folder
    resp = sync_client.patch(
        "/api/burn/folders/rename",
        params={"project": "rename-virt"},
        json={"folder": "run_abc123", "new_name": "something"},
    )
    assert resp.status_code == 400

    # Root
    resp = sync_client.patch(
        "/api/burn/folders/rename",
        params={"project": "rename-virt"},
        json={"folder": "(root)", "new_name": "something"},
    )
    assert resp.status_code == 400

    # Clips root
    resp = sync_client.patch(
        "/api/burn/folders/rename",
        params={"project": "rename-virt"},
        json={"folder": "clips", "new_name": "something"},
    )
    assert resp.status_code == 400

    # Path traversal in new_name
    (get_project_video_dir("rename-virt") / "foo").mkdir(parents=True, exist_ok=True)
    resp = sync_client.patch(
        "/api/burn/folders/rename",
        params={"project": "rename-virt"},
        json={"folder": "foo", "new_name": "../evil"},
    )
    assert resp.status_code == 400
    assert (get_project_video_dir("rename-virt") / "foo").exists()


def test_burn_video_raises_clearly_when_ffmpeg_missing(monkeypatch, tmp_path):
    """_burn_video must fail gracefully with a clear message (not a raw
    FileNotFoundError) when ffmpeg is not on PATH."""
    monkeypatch.setattr(burn_router.shutil, "which", lambda _name: None)

    src = tmp_path / "in.mp4"
    src.write_bytes(b"video")
    out = tmp_path / "out.mp4"

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(
            burn_router._burn_video(
                str(src), None, str(out), {"brightness": 50}
            )
        )
    assert "ffmpeg not available" in str(exc.value)


def test_burn_background_records_error_when_ffmpeg_missing(monkeypatch, tmp_path):
    """A burn job with a missing ffmpeg is recorded as status=error in
    _burn_jobs rather than crashing the background task."""
    monkeypatch.setattr(burn_router.shutil, "which", lambda _name: None)

    src = tmp_path / "in.mp4"
    src.write_bytes(b"video")
    out = tmp_path / "out.mp4"
    batch_id = "ffmpeg-missing-batch"
    burn_router._burn_jobs.pop(batch_id, None)

    asyncio.run(
        burn_router._burn_background(
            batch_id, 0, str(src), None, str(out), {"brightness": 50}, "in.mp4"
        )
    )

    item = burn_router._burn_jobs[batch_id][0]
    assert item["status"] == "error"
    assert item["ok"] is False
    assert "ffmpeg not available" in item["error"]
    burn_router._burn_jobs.pop(batch_id, None)


def test_burn_overlay_missing_video_returns_404(sync_client):
    """Burning against a project whose video directory / file doesn't exist
    returns 404 instead of crashing."""
    created = sync_client.post("/api/projects", json={"name": "Missing Video"})
    assert created.status_code == 201

    resp = sync_client.post(
        "/api/burn/overlay",
        json={
            "project": "missing-video",
            "batchId": "batch-missing",
            "index": 0,
            "videoPath": "nope/ghost.mp4",
        },
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["error"].lower()


def test_burn_folder_rename_clips_preserves_prefix(sync_client):
    created = sync_client.post("/api/projects", json={"name": "Rename Clips"})
    assert created.status_code == 201

    clips_dir = get_project_clips_dir("rename-clips") / "job_xyz"
    clips_dir.mkdir(parents=True, exist_ok=True)
    (clips_dir / "clip_001.mp4").write_bytes(b"clip")

    resp = sync_client.patch(
        "/api/burn/folders/rename",
        params={"project": "rename-clips"},
        json={"folder": "clips/job_xyz", "new_name": "rooftop-shoot"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["new_folder"] == "clips/rooftop-shoot"

    assert not (get_project_clips_dir("rename-clips") / "job_xyz").exists()
    assert (get_project_clips_dir("rename-clips") / "rooftop-shoot" / "clip_001.mp4").exists()


# ── Caption WebSocket pipeline (_run_pipeline) ────────────────────────────
#
# _run_pipeline imports its scraper deps lazily INSIDE the function
# (`from scraper.frame_extractor import ...`), so patches must land on the
# source modules, not on routers.captions. We capture the broadcast stream
# instead of opening a real WebSocket and assert the event/error shapes.

from routers import captions as captions_router  # noqa: E402


def _run_caption_pipeline(monkeypatch, *, list_videos, get_thumbnail, extract_caption,
                          analyze_mood, project="quick-test", max_videos=20):
    """Drive _run_pipeline with stubbed scraper deps; return captured broadcasts.

    Each scraper dep is an async-capable callable (or one that raises). Returns
    the list of (event, data) tuples broadcast for the job.
    """
    import scraper.frame_extractor as fe
    import scraper.caption_extractor as ce
    import scraper.sentiment_analyzer as sa

    monkeypatch.setattr(fe, "list_profile_videos", list_videos)
    monkeypatch.setattr(fe, "get_thumbnail", get_thumbnail)
    monkeypatch.setattr(ce, "extract_caption", extract_caption)
    monkeypatch.setattr(sa, "analyze_mood", analyze_mood)

    events: list[tuple[str, dict]] = []

    async def fake_broadcast(job_id, event, data):
        events.append((event, data))

    monkeypatch.setattr(captions_router, "_broadcast", fake_broadcast)

    asyncio.run(
        captions_router._run_pipeline(
            "job-test-0", "@creator", max_videos, "latest", project
        )
    )
    return events


def _ev(events, name):
    return [d for e, d in events if e == name]


def test_caption_pipeline_happy_path(monkeypatch, sync_client):
    """Full pipeline: list -> download -> OCR -> mood -> CSV -> all_complete."""
    sync_client.post("/api/projects", json={"name": "Pipe Happy"})

    async def list_videos(url, n, sort):
        return [
            "https://www.tiktok.com/@creator/video/111",
            "https://www.tiktok.com/@creator/video/222",
        ]

    async def get_thumbnail(url, dest):
        dest.write_bytes(b"\xff\xd8jpegbytes")
        return dest

    async def extract_caption(frame_bytes, _max_retries=4):
        return "a real caption"

    async def analyze_mood(caption, _max_retries=3):
        return "hype"

    events = _run_caption_pipeline(
        monkeypatch, project="pipe-happy",
        list_videos=list_videos, get_thumbnail=get_thumbnail,
        extract_caption=extract_caption, analyze_mood=analyze_mood,
    )

    assert _ev(events, "urls_collected")[0]["count"] == 2
    assert len(_ev(events, "frame_ready")) == 2
    ocr_done = _ev(events, "ocr_done")
    assert len(ocr_done) == 2
    assert all(d["caption"] == "a real caption" and d["mood"] == "hype" for d in ocr_done)

    complete = _ev(events, "all_complete")[0]
    assert len(complete["results"]) == 2
    assert all(r["error"] is None for r in complete["results"])

    # CSV written to the project-scoped caption dir
    csv_path = get_project_caption_dir("pipe-happy") / "creator" / "captions.csv"
    assert csv_path.exists()
    assert "a real caption" in csv_path.read_text(encoding="utf-8")


def test_caption_pipeline_empty_profile(monkeypatch, sync_client):
    """Zero videos short-circuits to all_complete with empty results, no crash."""
    sync_client.post("/api/projects", json={"name": "Pipe Empty"})

    async def list_videos(url, n, sort):
        return []

    async def unreached(*a, **k):  # pragma: no cover
        raise AssertionError("should not be called for an empty profile")

    events = _run_caption_pipeline(
        monkeypatch, project="pipe-empty",
        list_videos=list_videos, get_thumbnail=unreached,
        extract_caption=unreached, analyze_mood=unreached,
    )

    assert _ev(events, "urls_collected")[0]["count"] == 0
    complete = _ev(events, "all_complete")[0]
    assert complete["results"] == []
    assert complete["csv"] is None


def test_caption_pipeline_thumbnail_timeout(monkeypatch, sync_client):
    """A thumbnail download exceeding the 45s budget records a timeout error,
    emits frame_error, and the row carries through OCR as a skip (not a crash)."""
    sync_client.post("/api/projects", json={"name": "Pipe Timeout"})

    async def list_videos(url, n, sort):
        return ["https://www.tiktok.com/@creator/video/999"]

    async def get_thumbnail(url, dest):
        raise asyncio.TimeoutError()

    async def extract_caption(frame_bytes, _max_retries=4):  # pragma: no cover
        raise AssertionError("OCR must be skipped for a failed thumbnail")

    async def analyze_mood(caption, _max_retries=3):  # pragma: no cover
        raise AssertionError("mood must be skipped for a failed thumbnail")

    events = _run_caption_pipeline(
        monkeypatch, project="pipe-timeout",
        list_videos=list_videos, get_thumbnail=get_thumbnail,
        extract_caption=extract_caption, analyze_mood=analyze_mood,
    )

    frame_errs = _ev(events, "frame_error")
    assert len(frame_errs) == 1
    assert "timed out" in frame_errs[0]["error"].lower()
    # OCR emits ocr_done immediately for the errored row, carrying the error
    ocr_done = _ev(events, "ocr_done")[0]
    assert ocr_done["caption"] == ""
    assert "timed out" in ocr_done["error"].lower()
    assert _ev(events, "all_complete")[0]["results"][0]["error"]


def test_caption_pipeline_thumbnail_network_failure(monkeypatch, sync_client):
    """A non-timeout download exception (network/yt-dlp crash) is captured per-row
    and surfaced as frame_error without aborting the batch."""
    sync_client.post("/api/projects", json={"name": "Pipe Net"})

    async def list_videos(url, n, sort):
        return ["https://www.tiktok.com/@creator/video/1", "https://www.tiktok.com/@creator/video/2"]

    async def get_thumbnail(url, dest):
        if url.endswith("/1"):
            raise RuntimeError("connection reset by peer")
        dest.write_bytes(b"\xff\xd8ok")
        return dest

    async def extract_caption(frame_bytes, _max_retries=4):
        return "caption two"

    async def analyze_mood(caption, _max_retries=3):
        return "chill"

    events = _run_caption_pipeline(
        monkeypatch, project="pipe-net",
        list_videos=list_videos, get_thumbnail=get_thumbnail,
        extract_caption=extract_caption, analyze_mood=analyze_mood,
    )

    frame_errs = _ev(events, "frame_error")
    assert len(frame_errs) == 1
    assert "connection reset" in frame_errs[0]["error"]
    # The healthy second video still completes
    complete = _ev(events, "all_complete")[0]
    errors = [r["error"] for r in complete["results"]]
    captions = [r["caption"] for r in complete["results"]]
    assert "connection reset by peer" in errors
    assert "caption two" in captions


def test_caption_pipeline_ocr_timeout(monkeypatch, sync_client):
    """A successful thumbnail but a stalled OCR call records a caption-extraction
    timeout on the row and emits ocr_done with an empty caption."""
    sync_client.post("/api/projects", json={"name": "Pipe Ocr"})

    async def list_videos(url, n, sort):
        return ["https://www.tiktok.com/@creator/video/5"]

    async def get_thumbnail(url, dest):
        dest.write_bytes(b"\xff\xd8jpeg")
        return dest

    async def extract_caption(frame_bytes, _max_retries=4):
        raise asyncio.TimeoutError()

    async def analyze_mood(caption, _max_retries=3):  # pragma: no cover
        raise AssertionError("mood must not run when OCR times out")

    events = _run_caption_pipeline(
        monkeypatch, project="pipe-ocr",
        list_videos=list_videos, get_thumbnail=get_thumbnail,
        extract_caption=extract_caption, analyze_mood=analyze_mood,
    )

    ocr_done = _ev(events, "ocr_done")[0]
    assert ocr_done["caption"] == ""
    assert "timed out" in ocr_done["error"].lower()
    assert _ev(events, "all_complete")[0]["results"][0]["error"]


def test_caption_pipeline_mood_failure_falls_back_to_chill(monkeypatch, sync_client):
    """If mood analysis fails, the caption is still kept and mood defaults to
    'chill' — a mood error must never lose a good caption."""
    sync_client.post("/api/projects", json={"name": "Pipe Mood"})

    async def list_videos(url, n, sort):
        return ["https://www.tiktok.com/@creator/video/7"]

    async def get_thumbnail(url, dest):
        dest.write_bytes(b"\xff\xd8jpeg")
        return dest

    async def extract_caption(frame_bytes, _max_retries=4):
        return "good caption survives"

    async def analyze_mood(caption, _max_retries=3):
        raise RuntimeError("mood model 500")

    events = _run_caption_pipeline(
        monkeypatch, project="pipe-mood",
        list_videos=list_videos, get_thumbnail=get_thumbnail,
        extract_caption=extract_caption, analyze_mood=analyze_mood,
    )

    ocr_done = _ev(events, "ocr_done")[0]
    assert ocr_done["caption"] == "good caption survives"
    assert ocr_done["mood"] == "chill"
    assert ocr_done["error"] is None


def test_caption_pipeline_playwright_crash_emits_top_level_error(monkeypatch, sync_client):
    """A failure in the video-listing phase (e.g. Playwright/yt-dlp crash) aborts
    the pipeline cleanly with a single top-level `error` event — not an unhandled
    exception that kills the background task silently."""
    sync_client.post("/api/projects", json={"name": "Pipe Crash"})

    async def list_videos(url, n, sort):
        raise RuntimeError("Playwright browser closed unexpectedly")

    async def unreached(*a, **k):  # pragma: no cover
        raise AssertionError("nothing past listing should run")

    events = _run_caption_pipeline(
        monkeypatch, project="pipe-crash",
        list_videos=list_videos, get_thumbnail=unreached,
        extract_caption=unreached, analyze_mood=unreached,
    )

    errs = _ev(events, "error")
    assert len(errs) == 1
    assert "Playwright" in errs[0]["error"]
    assert not _ev(events, "all_complete")


# ── WebSocket endpoint (/ws/{job_id}) ─────────────────────────────────────
#
# The tests above drive _run_pipeline directly. These exercise the actual
# `@router.websocket("/ws/{job_id}")` handler: accept -> register client ->
# receive `start` -> dispatch _run_pipeline -> disconnect cleanup. We stub
# _run_pipeline so the handler is tested in isolation from the scraper deps.


def test_caption_ws_start_dispatches_pipeline(monkeypatch, sync_client):
    """Connecting registers the client, and a `start` action dispatches
    _run_pipeline with the parsed/clamped args. The stubbed pipeline broadcasts
    a sentinel back through the real socket, proving the round trip works."""
    captured: dict = {}

    async def fake_pipeline(job_id, profile_url, max_videos, sort, project):
        captured.update(
            job_id=job_id, profile_url=profile_url,
            max_videos=max_videos, sort=sort, project=project,
        )
        # Client is registered by the time the pipeline runs.
        assert captions_router._ws_clients.get(job_id), "client not registered"
        await captions_router._broadcast(job_id, "status", {"text": "dispatched"})

    monkeypatch.setattr(captions_router, "_run_pipeline", fake_pipeline)

    with sync_client.websocket_connect("/api/captions/ws/job-ws-1") as ws:
        ws.send_json({
            "action": "start",
            "profile_url": "@creator",
            "max_videos": 7,
            "sort": "popular",
            "project": "ws-proj",
        })
        msg = ws.receive_json()

    assert msg == {"event": "status", "text": "dispatched"}
    assert captured == {
        "job_id": "job-ws-1", "profile_url": "@creator",
        "max_videos": 7, "sort": "popular", "project": "ws-proj",
    }


def test_caption_ws_clamps_max_videos(monkeypatch, sync_client):
    """max_videos is clamped to [1, 50] and sort/project default sensibly."""
    seen: list[dict] = []

    async def fake_pipeline(job_id, profile_url, max_videos, sort, project):
        seen.append({"max": max_videos, "sort": sort, "project": project})
        await captions_router._broadcast(job_id, "ack", {})

    monkeypatch.setattr(captions_router, "_run_pipeline", fake_pipeline)

    with sync_client.websocket_connect("/api/captions/ws/job-ws-2") as ws:
        ws.send_json({"action": "start", "profile_url": "@a", "max_videos": 9999})
        ws.receive_json()
        ws.send_json({"action": "start", "profile_url": "@b", "max_videos": 0})
        ws.receive_json()

    assert seen[0]["max"] == 50  # over-cap clamped down
    assert seen[1]["max"] == 1   # under-floor clamped up
    # Defaults applied when omitted
    assert seen[0]["sort"] == "latest"
    assert seen[0]["project"] is None


def test_caption_ws_ignores_unknown_actions(monkeypatch, sync_client):
    """A non-`start` message is a no-op — the handler keeps listening and never
    dispatches the pipeline."""
    async def fake_pipeline(*a, **k):  # pragma: no cover
        raise AssertionError("pipeline must not run for unknown actions")

    monkeypatch.setattr(captions_router, "_run_pipeline", fake_pipeline)

    with sync_client.websocket_connect("/api/captions/ws/job-ws-3") as ws:
        ws.send_json({"action": "ping"})
        # Follow with a real start to prove the socket is still alive.
        captured = {}

        async def real_pipeline(job_id, *a, **k):
            captured["ran"] = True
            await captions_router._broadcast(job_id, "ack", {})

        monkeypatch.setattr(captions_router, "_run_pipeline", real_pipeline)
        ws.send_json({"action": "start", "profile_url": "@c"})
        msg = ws.receive_json()

    assert msg["event"] == "ack"
    assert captured.get("ran") is True


def test_caption_ws_disconnect_unregisters_client(monkeypatch, sync_client):
    """Closing the socket removes the client from _ws_clients (no leak)."""
    job_id = "job-ws-4"
    with sync_client.websocket_connect(f"/api/captions/ws/{job_id}"):
        # Inside the context the client is registered.
        assert len(captions_router._ws_clients.get(job_id, [])) == 1
    # After the context exits the finally-block removes it.
    assert captions_router._ws_clients.get(job_id, []) == []


# ── /history and /rename-batch endpoints ──────────────────────────────────


def test_caption_history_lists_batches(sync_client):
    sync_client.post("/api/projects", json={"name": "Hist Suite"})

    cdir = get_project_caption_dir("hist-suite") / "artistA"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "captions.csv").write_text(
        "video_id,video_url,caption,mood,error\n"
        "1,https://tiktok.com/@a/video/1,first cap,hype,\n"
        "2,https://tiktok.com/@a/video/2,second cap,chill,\n",
        encoding="utf-8",
    )

    resp = sync_client.get("/api/captions/history", params={"project": "hist-suite"})
    assert resp.status_code == 200
    batches = resp.json()["batches"]
    assert len(batches) == 1
    b = batches[0]
    assert b["username"] == "artistA"
    assert b["caption_count"] == 2
    assert b["captions"][0]["mood"] == "hype"
    assert "first cap" in b["sample"]


def test_caption_history_empty_project(sync_client):
    sync_client.post("/api/projects", json={"name": "Hist Empty"})
    resp = sync_client.get("/api/captions/history", params={"project": "hist-empty"})
    assert resp.status_code == 200
    assert resp.json()["batches"] == []


def test_caption_rename_batch_happy_path(sync_client):
    sync_client.post("/api/projects", json={"name": "Ren Batch"})
    cdir = get_project_caption_dir("ren-batch") / "oldartist"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "captions.csv").write_text("video_id,caption\n1,x\n", encoding="utf-8")

    resp = sync_client.post(
        "/api/captions/rename-batch",
        json={"project": "ren-batch", "old_name": "oldartist", "new_name": "new artist"},
    )
    assert resp.status_code == 200
    assert resp.json()["renamed"] is True
    assert not (get_project_caption_dir("ren-batch") / "oldartist").exists()
    assert (get_project_caption_dir("ren-batch") / "new artist" / "captions.csv").exists()


def test_caption_rename_batch_validation_and_collisions(sync_client):
    sync_client.post("/api/projects", json={"name": "Ren Guard"})
    base = get_project_caption_dir("ren-guard")
    (base / "alpha").mkdir(parents=True, exist_ok=True)
    (base / "beta").mkdir(parents=True, exist_ok=True)

    # Missing fields -> 400
    r = sync_client.post(
        "/api/captions/rename-batch",
        json={"project": "ren-guard", "old_name": "", "new_name": "x"},
    )
    assert r.status_code == 400

    # Path-traversal new_name sanitizes to empty -> 400
    r = sync_client.post(
        "/api/captions/rename-batch",
        json={"project": "ren-guard", "old_name": "alpha", "new_name": "../"},
    )
    assert r.status_code == 400
    assert (base / "alpha").exists()

    # Unknown old_name -> 404
    r = sync_client.post(
        "/api/captions/rename-batch",
        json={"project": "ren-guard", "old_name": "ghost", "new_name": "fresh"},
    )
    assert r.status_code == 404

    # Collision with existing batch -> 409
    r = sync_client.post(
        "/api/captions/rename-batch",
        json={"project": "ren-guard", "old_name": "alpha", "new_name": "beta"},
    )
    assert r.status_code == 409
    assert (base / "alpha").exists() and (base / "beta").exists()
