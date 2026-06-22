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

import json
import time

import pytest

import routers.upload as upload_router
import services.upload as upload_svc


@pytest.fixture
def cookie_dir(tmp_path, monkeypatch):
    """Isolate cookie storage to a tmp dir; also clear the in-memory job store so
    job-id 404/409 assertions are deterministic regardless of test order."""
    d = tmp_path / "cookies"
    d.mkdir()
    monkeypatch.setattr(upload_svc, "COOKIE_DIR", str(d))
    # upload_jobs.json is loaded/saved relative to CWD via atomic_load/atomic_save;
    # run the whole test from the tmp dir so we never touch a real queue file.
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
    _write_cookie(cookie_dir, "linked")
    r = sync_client.post(
        "/api/upload/submit",
        json={"video_path": "/tmp/x.mp4", "account_name": "linked"},
    )
    assert r.status_code == 200
    assert r.json()["job"]["account_name"] == "linked"
    assert r.json()["job"]["status"] == "queued"


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
    r = sync_client.post("/api/upload/login/whoever")
    assert r.status_code == 500
    assert "Failed to start login" in r.json()["detail"]
