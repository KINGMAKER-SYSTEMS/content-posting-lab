"""Error-path tests for the TikTok upload router (routers/upload.py).

These cover the user-facing 4xx cases that must NOT degrade into 500s or silent
failures:

  - submit for an account with no saved cookies (unlinked)        → 400
  - submit for an account whose cookies are expired               → 400
  - check_cookie for an account with no saved config              → 200 + "missing"
  - poll / cancel a job id that does not exist                    → 404
  - cancel a job that is not cancellable (already uploading)      → 409
  - trigger_login does not 500 and never spawns a real browser

Cookie state lives in `services.upload.COOKIE_DIR` (defaults to "." — the process
CWD). We point it at a tmp dir per-test so the suite neither reads real
`TK_cookies_*.json` from the repo root nor leaves any behind.
"""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

import routers.upload as upload_router
import services.upload as upload_svc


_FAKE_UPLOADER = SimpleNamespace(upload_tiktok=lambda **kwargs: None)


@pytest.fixture
def cookie_dir(tmp_path, monkeypatch):
    """Isolate cookie storage to a tmp dir; also clear the in-memory job store so
    job-id 404/409 assertions are deterministic regardless of test order."""
    d = tmp_path / "cookies"
    d.mkdir()
    monkeypatch.setattr(upload_svc, "COOKIE_DIR", str(d))
    # The job store is relative to CWD; isolate the locked read/write path here.
    monkeypatch.chdir(tmp_path)
    return d


def _write_cookie(cookie_dir, account, *, expired=False):
    exp = time.time() - 3600 if expired else time.time() + 3600
    path = cookie_dir / f"TK_cookies_{account}.json"
    path.write_text(json.dumps([{"name": "sessionid", "value": "x", "expiry": exp}]))
    return path


# ── submit_upload error paths ─────────────────────────────────────────────────


def test_submit_unlinked_account_returns_400(sync_client, cookie_dir):
    """No cookie file for the account → 400 with an actionable message, not a 500."""
    r = sync_client.post(
        "/api/upload/submit",
        json={"video_path": "/tmp/x.mp4", "account_name": "ghost"},
    )
    assert r.status_code == 400
    assert "ghost" in r.json()["detail"]
    assert "login" in r.json()["detail"].lower()


def test_submit_expired_cookie_returns_400(sync_client, cookie_dir):
    _write_cookie(cookie_dir, "stale", expired=True)
    r = sync_client.post(
        "/api/upload/submit",
        json={"video_path": "/tmp/x.mp4", "account_name": "stale"},
    )
    assert r.status_code == 400
    assert "expired" in r.json()["detail"].lower()


def test_submit_valid_cookie_queues_job(sync_client, cookie_dir, monkeypatch):
    """Happy-path guard so the 400s above are proven to be the cookie-gate, not a
    blanket failure. Stub the background queue processor so no upload is attempted."""

    async def _noop():
        return None

    monkeypatch.setattr(upload_router, "process_queue", _noop)
    monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: _FAKE_UPLOADER)
    _write_cookie(cookie_dir, "linked")
    r = sync_client.post(
        "/api/upload/submit",
        json={"video_path": "/tmp/x.mp4", "account_name": "linked"},
    )
    assert r.status_code == 200
    assert r.json()["job"]["account_name"] == "linked"
    assert r.json()["job"]["status"] == "queued"


@pytest.mark.parametrize("existing", [False, True])
def test_empty_video_path_refused_without_poisoning_queue(
    sync_client, cookie_dir, monkeypatch, existing,
):
    _write_cookie(cookie_dir, "linked")
    monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: _FAKE_UPLOADER)
    calls = []
    scheduled = []

    async def queue():
        scheduled.append(True)

    def provider(job):
        calls.append(job["video_path"])
        return "Completed"

    monkeypatch.setattr(upload_router, "process_queue", queue)
    monkeypatch.setattr(upload_svc, "_run_upload_sync", provider)
    stored = cookie_dir.parent / "upload_jobs.json"
    if existing:
        old = upload_svc.submit_job(video_path="/tmp/old.mp4", account_name="other")
        upload_svc._update_job(old["job_id"], {"status": "completed"})
    before = stored.read_bytes() if existing else None
    response = sync_client.post("/api/upload/submit", json={
        "video_path": "", "account_name": "linked",
    })
    assert response.status_code == 400
    assert (stored.read_bytes() if stored.exists() else None) == before
    assert scheduled == []
    assert calls == []

    response = sync_client.post("/api/upload/submit", json={
        "video_path": "/tmp/valid.mp4", "account_name": "linked",
    })
    assert response.status_code == 200
    # The launch stub leaves processing to this explicit drain, so completion
    # does not depend on the background task winning a scheduling race.
    asyncio.run(upload_svc.process_queue())
    assert calls == ["/tmp/valid.mp4"]
    assert upload_svc.get_job(response.json()["job"]["job_id"])["status"] == "completed"


def test_direct_empty_video_path_refused_before_save(cookie_dir, monkeypatch):
    monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: _FAKE_UPLOADER)
    with pytest.raises(ValueError, match="invalid upload job"):
        upload_svc.submit_job(video_path="", account_name="linked")
    assert not (cookie_dir.parent / "upload_jobs.json").exists()


def test_submit_without_provider_refuses_before_job_creation(sync_client, cookie_dir, monkeypatch):
    _write_cookie(cookie_dir, "linked")
    monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: None)
    r = sync_client.post("/api/upload/submit", json={"video_path": "/tmp/x.mp4", "account_name": "linked"})
    assert r.status_code == 503
    assert "provider runtime" in r.json()["detail"]
    assert not (cookie_dir.parent / "upload_jobs.json").exists()


def test_broken_provider_import_refuses_before_job_creation(sync_client, cookie_dir, monkeypatch):
    _write_cookie(cookie_dir, "linked")

    def _broken_import(name):
        raise RuntimeError("missing browser dependency")

    monkeypatch.setattr(upload_svc.importlib, "import_module", _broken_import)
    r = sync_client.post("/api/upload/submit", json={"video_path": "/tmp/x.mp4", "account_name": "linked"})
    assert r.status_code == 503
    assert not (cookie_dir.parent / "upload_jobs.json").exists()


def test_hosted_ephemeral_store_refuses_before_job_creation(sync_client, cookie_dir, monkeypatch):
    _write_cookie(cookie_dir, "linked")
    monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: _FAKE_UPLOADER)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(cookie_dir))
    r = sync_client.post("/api/upload/submit", json={"video_path": "/tmp/x.mp4", "account_name": "linked"})
    assert r.status_code == 503
    assert "durable upload reconciliation" in r.json()["detail"]
    assert not (cookie_dir.parent / "upload_jobs.json").exists()


def test_hosted_volume_store_alone_does_not_enable_upload(sync_client, cookie_dir, monkeypatch):
    _write_cookie(cookie_dir, "linked")
    monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: _FAKE_UPLOADER)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(cookie_dir))
    store_dir = cookie_dir / "_upload"
    store_dir.mkdir()
    store = store_dir / "upload_jobs.json"
    monkeypatch.setattr(upload_svc, "UPLOAD_JOBS_FILE", str(store))

    r = sync_client.post("/api/upload/submit", json={"video_path": "/tmp/x.mp4", "account_name": "linked"})
    assert r.status_code == 503
    assert "durable upload reconciliation" in r.json()["detail"]
    assert not store.exists()


def test_corrupt_job_store_refuses_with_recovery_steps(sync_client, cookie_dir, monkeypatch, caplog):
    _write_cookie(cookie_dir, "linked")
    monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: _FAKE_UPLOADER)
    stored = cookie_dir.parent / "upload_jobs.json"
    stored.write_text("{broken")
    r = sync_client.post("/api/upload/submit", json={"video_path": "/tmp/x.mp4", "account_name": "linked"})
    assert r.status_code == 503
    for text in (str(stored.resolve()), "Inspect the file", "fix it", "restore it from a backup", "then retry"):
        assert text in r.json()["detail"]
        assert text in caplog.text
    assert stored.read_text() == "{broken"

@pytest.mark.parametrize("existing", [None, {"jobs": []}, "not-a-list"])
def test_non_list_job_store_refuses_with_recovery_steps(sync_client, cookie_dir, monkeypatch, existing, caplog):
    _write_cookie(cookie_dir, "linked")
    monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: _FAKE_UPLOADER)
    stored = cookie_dir.parent / "upload_jobs.json"
    original = json.dumps(existing)
    stored.write_text(original)

    r = sync_client.post("/api/upload/submit", json={"video_path": "/tmp/x.mp4", "account_name": "linked"})
    assert r.status_code == 503
    for text in (str(stored.resolve()), "Inspect the file", "fix it", "restore it from a backup", "then retry"):
        assert text in r.json()["detail"]
        assert text in caplog.text
    assert stored.read_text() == original


def test_bad_stored_row_is_quarantined_and_good_job_processes(cookie_dir, monkeypatch, caplog):
    monkeypatch.setattr(upload_svc, "require_upload_runtime", lambda: None)
    good = upload_svc.submit_job(video_path="/tmp/good.mp4", account_name="linked")
    bad = {**good, "job_id": "bad-row", "video_path": ""}
    stored = cookie_dir.parent / "upload_jobs.json"
    stored.write_text(json.dumps([good, bad]))
    original = stored.read_bytes()
    calls = []
    monkeypatch.setattr(upload_svc, "_run_upload_sync", lambda job: calls.append(job["job_id"]) or "Completed")

    try:
        listed_jobs = upload_svc.get_all_jobs()
    except upload_svc.UploadUnavailable:
        listed_jobs = []
    assert [job["job_id"] for job in listed_jobs] == [good["job_id"]]
    assert stored.read_bytes() == original
    asyncio.run(upload_svc._process_single_job(good["job_id"]))

    assert calls == [good["job_id"]]
    assert upload_svc.get_job(good["job_id"])["status"] == "completed"
    saved = json.loads(stored.read_text())
    assert len(saved) == 2
    assert saved[0]["job_id"] == good["job_id"]
    assert saved[0]["status"] == "completed"
    assert saved[1] == bad
    quarantine = stored.with_name(stored.name + ".quarantine.jsonl")
    records = [json.loads(line) for line in quarantine.read_text().splitlines()]
    assert records == [{"row_index": 1, "reason": "video_path must be a non-empty string", "job": bad}]
    assert "Quarantined upload job row 1: video_path must be a non-empty string" in caplog.text


def test_quarantined_unresolved_row_blocks_same_account_video_retry(
    sync_client, cookie_dir, monkeypatch,
):
    _write_cookie(cookie_dir, "linked")
    monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: _FAKE_UPLOADER)
    calls = []
    monkeypatch.setattr(upload_svc, "_run_upload_sync", lambda job: calls.append(job) or "Completed")

    async def unexpected_queue_run():
        pytest.fail("blocked duplicate scheduled the upload queue")
    monkeypatch.setattr(upload_router, "process_queue", unexpected_queue_run)

    stored = cookie_dir.parent / "upload_jobs.json"
    unresolved = {
        "job_id": "",
        "status": "uploading",
        "video_path": "/tmp/uncertain.mp4",
        "account_name": "linked",
    }
    stored.write_text(json.dumps([unresolved]))
    original = stored.read_bytes()

    # The admission read quarantines the malformed row, but keeps it active so
    # its readable identity still blocks a duplicate provider attempt.
    assert upload_svc.get_all_jobs() == []
    assert stored.read_bytes() == original
    response = sync_client.post("/api/upload/submit", json={
        "video_path": "/tmp/other/../uncertain.mp4", "account_name": "linked",
    })

    assert response.status_code == 409
    assert "original attempt requires reconciliation" in response.json()["detail"]
    assert "unknown" in response.json()["detail"]
    assert stored.read_bytes() == original
    assert calls == []
    quarantine = stored.with_name(stored.name + ".quarantine.jsonl")
    assert len(quarantine.read_text().splitlines()) == 1


def test_quarantine_write_failure_keeps_valid_job_available(cookie_dir, monkeypatch, caplog):
    monkeypatch.setattr(upload_svc, "require_upload_runtime", lambda: None)
    good = upload_svc.submit_job(video_path="/tmp/good.mp4", account_name="linked")
    bad = {**good, "job_id": "bad-row", "video_path": ""}
    stored = cookie_dir.parent / "upload_jobs.json"
    stored.write_text(json.dumps([good, bad]))
    original = stored.read_bytes()

    def fail_quarantine(_rows):
        raise OSError("disk full")

    monkeypatch.setattr(upload_svc, "_quarantine_stored_jobs", fail_quarantine)
    calls = []
    monkeypatch.setattr(upload_svc, "_run_upload_sync", lambda job: calls.append(job["job_id"]) or "Completed")

    try:
        listed_jobs = upload_svc.get_all_jobs()
    except OSError:
        listed_jobs = []
    assert [job["job_id"] for job in listed_jobs] == [good["job_id"]]
    assert stored.read_bytes() == original
    assert str(stored.with_name(stored.name + ".quarantine.jsonl").resolve()) in caplog.text
    assert "row 1: video_path must be a non-empty string" in caplog.text

    asyncio.run(upload_svc._process_single_job(good["job_id"]))
    assert calls == [good["job_id"]]
    persisted = json.loads(stored.read_text())
    assert persisted[0]["job_id"] == good["job_id"]
    assert persisted[0]["status"] == "completed"
    assert persisted[1] == bad


@pytest.mark.parametrize("status", ["queued", "uploading", "failed"])
def test_unresolved_original_upload_refuses_same_account_video_without_second_job(
    sync_client, cookie_dir, monkeypatch, status,
):
    _write_cookie(cookie_dir, "linked")
    monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: _FAKE_UPLOADER)

    async def unexpected_queue_run():
        pytest.fail("blocked duplicate scheduled the upload queue")

    monkeypatch.setattr(upload_router, "process_queue", unexpected_queue_run)
    original = upload_svc.submit_job(video_path="/tmp/clip.mp4", account_name="linked")
    if status != "queued":
        upload_svc._update_job(original["job_id"], {"status": status})
    stored = cookie_dir.parent / "upload_jobs.json"
    before = stored.read_bytes()

    response = sync_client.post("/api/upload/submit", json={
        "video_path": "/tmp/other/../clip.mp4", "account_name": "linked",
    })
    assert response.status_code == 409
    assert "original attempt requires reconciliation" in response.json()["detail"]
    assert original["job_id"] in response.json()["detail"]
    assert stored.read_bytes() == before
    assert len(upload_svc._read_stored_jobs()) == 1


def test_upload_claim_cannot_be_cancelled_or_resubmitted_during_browser_call(
    cookie_dir, monkeypatch,
):
    monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: _FAKE_UPLOADER)
    original = upload_svc.submit_job(video_path="/tmp/clip.mp4", account_name="linked")
    started = threading.Event()
    release = threading.Event()
    calls = []

    def fake_upload(job):
        calls.append(job["job_id"])
        started.set()
        assert release.wait(timeout=5)
        return "Completed"

    monkeypatch.setattr(upload_svc, "_run_upload_sync", fake_upload)

    async def exercise():
        task = asyncio.create_task(upload_svc._process_single_job(original["job_id"]))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            assert upload_svc.cancel_job(original["job_id"])["status"] == "uploading"
            with pytest.raises(upload_svc.UploadReconciliationRequired):
                upload_svc.submit_job(video_path="/tmp/clip.mp4", account_name="linked")
            assert len(upload_svc._read_stored_jobs()) == 1
        finally:
            release.set()
            await task

    asyncio.run(exercise())
    assert calls == [original["job_id"]]
    assert upload_svc.get_job(original["job_id"])["status"] == "completed"


# ── check_cookie ──────────────────────────────────────────────────────────────


def test_check_cookie_missing_account_reports_missing(sync_client, cookie_dir):
    """Checking an account with no saved config is a normal query: 200 + 'missing',
    never a 404/500."""
    r = sync_client.get("/api/upload/cookies/nobody")
    assert r.status_code == 200
    assert r.json() == {"account": "nobody", "status": "missing"}


def test_check_cookie_valid_account(sync_client, cookie_dir):
    _write_cookie(cookie_dir, "ok")
    r = sync_client.get("/api/upload/cookies/ok")
    assert r.status_code == 200
    assert r.json()["status"] == "valid"


# ── job polling / cancel error paths ──────────────────────────────────────────


def test_poll_missing_job_returns_404(sync_client, cookie_dir):
    r = sync_client.get("/api/upload/jobs/does-not-exist")
    assert r.status_code == 404


def test_cancel_missing_job_returns_404(sync_client, cookie_dir):
    r = sync_client.post("/api/upload/jobs/does-not-exist/cancel")
    assert r.status_code == 404


def test_cancel_non_cancellable_job_returns_409(sync_client, cookie_dir):
    """A job already past 'queued' (e.g. uploading) cannot be cancelled → 409."""
    from unittest.mock import patch
    with patch.object(upload_svc.importlib, "import_module", return_value=_FAKE_UPLOADER):
        job = upload_svc.submit_job(video_path="/tmp/x.mp4", account_name="acct")
    upload_svc._update_job(job["job_id"], {"status": "uploading"})
    r = sync_client.post(f"/api/upload/jobs/{job['job_id']}/cancel")
    assert r.status_code == 409
    assert "uploading" in r.json()["detail"]


# ── trigger_login ─────────────────────────────────────────────────────────────


def test_trigger_login_does_not_spawn_real_browser(sync_client, cookie_dir, monkeypatch):
    """The login endpoint must return 200 'login_started' and surface a pid, without
    actually launching a Playwright browser during tests. We assert it routes through
    the subprocess path (stubbed) rather than 500-ing."""

    class _FakeProc:
        pid = 4242

    async def _fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(upload_router.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: _FAKE_UPLOADER)
    r = sync_client.post("/api/upload/login/whoever")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "login_started"
    assert body["account"] == "whoever"
    assert body["pid"] == 4242


def test_trigger_login_subprocess_failure_returns_500(sync_client, cookie_dir, monkeypatch):
    """If the subprocess can't even start, the endpoint reports a 500 with a message
    rather than letting a raw exception escape."""

    async def _boom(*args, **kwargs):
        raise OSError("no exe")

    monkeypatch.setattr(upload_router.asyncio, "create_subprocess_exec", _boom)
    monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: _FAKE_UPLOADER)
    r = sync_client.post("/api/upload/login/whoever")
    assert r.status_code == 500
    assert "Failed to start login" in r.json()["detail"]


@pytest.mark.parametrize("hosted", [False, True])
def test_login_unavailable_does_not_spawn(sync_client, cookie_dir, monkeypatch, hosted):
    if hosted:
        monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
        monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: _FAKE_UPLOADER)
    else:
        monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: None)

    async def _unexpected_spawn(*args, **kwargs):
        raise AssertionError("login subprocess must not spawn")

    monkeypatch.setattr(upload_router.asyncio, "create_subprocess_exec", _unexpected_spawn)
    r = sync_client.post("/api/upload/login/whoever")
    assert r.status_code == 503
    assert "unavailable" in r.json()["detail"]
