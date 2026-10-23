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
import builtins
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
    (tmp_path / "upload_jobs.json").write_text("[]")
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
    before = stored.read_bytes()
    response = sync_client.post("/api/upload/submit", json={
        "video_path": "", "account_name": "linked",
    })
    assert response.status_code == 400
    assert stored.read_bytes() == before
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
    assert upload_svc.get_job(response.json()["job"]["job_id"])["status"] == "held"


def test_direct_empty_video_path_refused_before_save(cookie_dir, monkeypatch):
    monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: _FAKE_UPLOADER)
    with pytest.raises(ValueError, match="invalid upload job"):
        upload_svc.submit_job(video_path="", account_name="linked")
    assert (cookie_dir.parent / "upload_jobs.json").read_text() == "[]"


def test_bare_completed_preserves_receipt_and_blocks_same_account_queue(cookie_dir, monkeypatch):
    monkeypatch.setattr(upload_svc, "require_upload_runtime", lambda: None)
    first = upload_svc.submit_job(video_path="/tmp/first.mp4", account_name="linked")
    second = upload_svc.submit_job(video_path="/tmp/second.mp4", account_name="linked")
    calls = []
    monkeypatch.setattr(upload_svc, "_run_upload_sync", lambda job: calls.append(job["job_id"]) or "Completed")
    asyncio.run(upload_svc.process_queue())

    assert calls == [first["job_id"]]
    saved = upload_svc.get_job(first["job_id"])
    assert saved["status"] == "held"
    assert saved["completed_at"] is None
    assert saved["provider_result"] == "Completed"
    assert saved["provider_result_at"]
    assert "reconciliation" in saved["error"]
    assert upload_svc.get_job(second["job_id"])["status"] == "queued"
    assert upload_svc.get_queue_stats()["uploading"] == 0
    # Read from durable JSON again as a fresh process would; held is a valid
    # record, not quarantine corruption or an active browser call.
    upload_svc._quarantined_job_rows.clear()
    assert [row["status"] for row in upload_svc._read_stored_jobs()] == ["held", "queued"]
    assert asyncio.run(upload_svc._process_single_job(second["job_id"])) is False
    with pytest.raises(upload_svc.UploadReconciliationRequired):
        upload_svc.submit_job(video_path="/tmp/first.mp4", account_name="linked")
    # Another account can still make progress without replaying the first Post.
    unrelated = upload_svc.submit_job(video_path="/tmp/other.mp4", account_name="unrelated")
    asyncio.run(upload_svc.process_queue())
    assert calls == [first["job_id"], unrelated["job_id"]]


def test_legacy_completed_without_canonical_receipt_cannot_be_replayed(cookie_dir, monkeypatch):
    monkeypatch.setattr(upload_svc, "require_upload_runtime", lambda: None)
    original = upload_svc.submit_job(video_path="/tmp/legacy.mp4", account_name="linked")
    upload_svc._update_job(original["job_id"], {"status": "completed", "completed_at": "2026-10-01T00:00:00"})
    with pytest.raises(upload_svc.UploadReconciliationRequired, match=original["job_id"]):
        upload_svc.submit_job(video_path="/tmp/legacy.mp4", account_name="linked")
    assert len(upload_svc.get_all_jobs()) == 1


@pytest.mark.parametrize("video_url", [
    "https://tiktok.com.evil.test/@linked/video/1234567890123456789",
    "https://www.tiktok.com/@linked/video/9999999999999999999",
    "http://www.tiktok.com/@linked/video/1234567890123456789",
    "https://www.tiktok.com/@linked/video/1234567890123456789?share=1",
])
def test_completed_with_noncanonical_receipt_still_requires_reconciliation(
    cookie_dir, monkeypatch, video_url,
):
    monkeypatch.setattr(upload_svc, "require_upload_runtime", lambda: None)
    original = upload_svc.submit_job(video_path="/tmp/legacy.mp4", account_name="linked")
    upload_svc._update_job(original["job_id"], {
        "status": "completed", "video_id": "1234567890123456789", "video_url": video_url,
    })
    with pytest.raises(upload_svc.UploadReconciliationRequired):
        upload_svc.submit_job(video_path="/tmp/legacy.mp4", account_name="linked")


def test_deleted_job_store_after_claim_refuses_restart_and_replay(cookie_dir, monkeypatch):
    monkeypatch.setattr(upload_svc, "require_upload_runtime", lambda: None)
    original = upload_svc.submit_job(video_path="/tmp/claim.mp4", account_name="linked")
    upload_svc._update_job(original["job_id"], {"status": "held", "provider_result": "Completed"})
    store = cookie_dir.parent / "upload_jobs.json"
    store.unlink()
    upload_svc._quarantined_job_rows.clear()  # simulate a fresh process
    calls = []
    monkeypatch.setattr(upload_svc, "_run_upload_sync", lambda job: calls.append(job))

    with pytest.raises(upload_svc.UploadUnavailable, match="job store.*missing"):
        upload_svc.submit_job(video_path="/tmp/claim.mp4", account_name="linked")
    with pytest.raises(upload_svc.UploadUnavailable, match="job store.*missing"):
        asyncio.run(upload_svc.process_queue())
    assert calls == []
    assert not store.exists()


def test_missing_job_store_requires_explicit_empty_provisioning(sync_client, cookie_dir, monkeypatch):
    monkeypatch.setattr(upload_svc, "require_upload_runtime", lambda: None)
    store = cookie_dir.parent / "upload_jobs.json"
    store.unlink()
    with pytest.raises(upload_svc.UploadUnavailable, match="explicitly provision an empty"):
        upload_svc.submit_job(video_path="/tmp/new.mp4", account_name="linked")
    for route in ("/api/upload/jobs", "/api/upload/jobs/unknown"):
        response = sync_client.get(route)
        assert response.status_code == 503
        assert "job store" in response.json()["detail"]
    store.write_text("[]")
    assert upload_svc.submit_job(video_path="/tmp/new.mp4", account_name="linked")["status"] == "queued"


@pytest.mark.parametrize("invalid_status", [None, "unknown", []])
def test_quarantined_same_account_attempt_blocks_different_video(
    cookie_dir, monkeypatch, invalid_status,
):
    monkeypatch.setattr(upload_svc, "require_upload_runtime", lambda: None)
    good = upload_svc.submit_job(video_path="/tmp/new.mp4", account_name="linked")
    bad = {
        "job_id": "old-unresolved", "status": invalid_status,
        "video_path": "/tmp/prior.mp4", "account_name": "linked",
    }
    store = cookie_dir.parent / "upload_jobs.json"
    store.write_text(json.dumps([bad, good]))
    calls = []
    monkeypatch.setattr(upload_svc, "_run_upload_sync", lambda job: calls.append(job))
    assert asyncio.run(upload_svc._process_single_job(good["job_id"])) is False
    with pytest.raises(upload_svc.UploadReconciliationRequired, match="old-unresolved"):
        upload_svc.submit_job(video_path="/tmp/different.mp4", account_name="linked")
    assert calls == []
    assert upload_svc.get_job(good["job_id"])["status"] == "queued"
    assert json.loads(store.read_text()) == [bad, good]


@pytest.mark.parametrize("fail_at", [1, 3])
def test_lock_open_failure_returns_storage_unavailable_without_mutation(
    sync_client, cookie_dir, monkeypatch, fail_at,
):
    _write_cookie(cookie_dir, "linked")
    monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: _FAKE_UPLOADER)
    upload_svc.submit_job(video_path="/tmp/existing.mp4", account_name="linked")
    stored = cookie_dir.parent / "upload_jobs.json"
    before = stored.read_bytes()
    lock_opens = 0
    calls = []
    scheduled = []

    def fail_lock_open(path, *args, **kwargs):
        nonlocal lock_opens
        if str(path) == f"{upload_svc.UPLOAD_JOBS_FILE}.lock":
            lock_opens += 1
            if lock_opens == fail_at:
                raise PermissionError("lock access denied")
        return builtins.open(path, *args, **kwargs)

    async def queue():
        scheduled.append(True)

    monkeypatch.setattr(upload_svc, "open", fail_lock_open, raising=False)
    monkeypatch.setattr(upload_svc, "_run_upload_sync", lambda job: calls.append(job["job_id"]) or "Completed")
    monkeypatch.setattr(upload_router, "process_queue", queue)
    monkeypatch.setattr(sync_client._transport, "raise_server_exceptions", False)
    response = sync_client.post("/api/upload/submit", json={"video_path": "/tmp/new.mp4", "account_name": "linked"})
    assert response.status_code == 503
    assert "job storage" in response.json()["detail"]
    assert "unavailable" in response.json()["detail"]
    assert stored.read_bytes() == before
    assert scheduled == []
    assert calls == []
    lock_opens = fail_at - 1
    with pytest.raises(upload_svc.UploadUnavailable, match="job storage"):
        with upload_svc._job_store_write_lock():
            pytest.fail("unavailable lock must not enter the critical section")


def test_submit_without_provider_refuses_before_job_creation(sync_client, cookie_dir, monkeypatch):
    _write_cookie(cookie_dir, "linked")
    monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: None)
    r = sync_client.post("/api/upload/submit", json={"video_path": "/tmp/x.mp4", "account_name": "linked"})
    assert r.status_code == 503
    assert "provider runtime" in r.json()["detail"]
    assert (cookie_dir.parent / "upload_jobs.json").read_text() == "[]"


def test_broken_provider_import_refuses_before_job_creation(sync_client, cookie_dir, monkeypatch):
    _write_cookie(cookie_dir, "linked")

    def _broken_import(name):
        raise RuntimeError("missing browser dependency")

    monkeypatch.setattr(upload_svc.importlib, "import_module", _broken_import)
    r = sync_client.post("/api/upload/submit", json={"video_path": "/tmp/x.mp4", "account_name": "linked"})
    assert r.status_code == 503
    assert (cookie_dir.parent / "upload_jobs.json").read_text() == "[]"


def test_hosted_ephemeral_store_refuses_before_job_creation(sync_client, cookie_dir, monkeypatch):
    _write_cookie(cookie_dir, "linked")
    monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: _FAKE_UPLOADER)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(cookie_dir))
    r = sync_client.post("/api/upload/submit", json={"video_path": "/tmp/x.mp4", "account_name": "linked"})
    assert r.status_code == 503
    assert "durable upload reconciliation" in r.json()["detail"]
    assert (cookie_dir.parent / "upload_jobs.json").read_text() == "[]"


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
    good = upload_svc.submit_job(video_path="/tmp/good.mp4", account_name="unrelated")
    bad = {**good, "job_id": "bad-row", "video_path": "", "account_name": "linked"}
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
    assert upload_svc.get_job(good["job_id"])["status"] == "held"
    saved = json.loads(stored.read_text())
    assert len(saved) == 2
    assert saved[0]["job_id"] == good["job_id"]
    assert saved[0]["status"] == "held"
    assert saved[1] == bad
    quarantine = stored.with_name(stored.name + ".quarantine.jsonl")
    records = [json.loads(line) for line in quarantine.read_text().splitlines()]
    assert records == [{"row_index": 1, "reason": "video_path must be a non-empty string", "job": bad}]
    assert "Quarantined upload job row 1: video_path must be a non-empty string" in caplog.text


@pytest.mark.parametrize("status", ["queued", "uploading", "failed", None, "unknown", []])
def test_quarantined_unresolved_row_blocks_same_account_video_retry(
    sync_client, cookie_dir, monkeypatch, status,
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
        "status": status,
        "video_path": "/tmp/uncertain.mp4",
        "account_name": "linked",
    }
    stored.write_text(json.dumps([unresolved]))
    original = stored.read_bytes()

    # The admission read quarantines the malformed row, but keeps it active so
    # its readable identity still blocks a duplicate provider attempt.
    assert upload_svc.get_all_jobs() == []
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


def test_unknown_account_attempt_holds_same_video_and_allows_unrelated_target(
    cookie_dir, monkeypatch,
):
    monkeypatch.setattr(upload_svc, "require_upload_runtime", lambda: None)
    same = upload_svc.submit_job(video_path="/tmp/clip.mp4", account_name="linked")
    other = upload_svc.submit_job(video_path="/tmp/other.mp4", account_name="unrelated")
    stored = cookie_dir.parent / "upload_jobs.json"
    unknown = {"job_id": "prior", "status": "uploading", "video_path": "/tmp/clip.mp4"}
    stored.write_text(json.dumps([same, other, unknown]))
    original = json.loads(stored.read_text())
    calls = []
    monkeypatch.setattr(upload_svc, "_run_upload_sync", lambda job: calls.append(job["job_id"]) or "Completed")

    with pytest.raises(upload_svc.UploadReconciliationRequired):
        upload_svc.submit_job(video_path="/tmp/other/../clip.mp4", account_name="different")
    assert json.loads(stored.read_text()) == original

    asyncio.run(upload_svc.process_queue())
    assert calls == [other["job_id"]]
    assert upload_svc.get_job(same["job_id"])["status"] == "queued"
    assert upload_svc.get_job(other["job_id"])["status"] == "held"
    assert unknown in json.loads(stored.read_text())
    quarantine = stored.with_name(stored.name + ".quarantine.jsonl")
    assert any(json.loads(line)["job"] == unknown for line in quarantine.read_text().splitlines())


def test_unknown_account_and_video_hold_all_targets_until_reconciled(cookie_dir, monkeypatch):
    monkeypatch.setattr(upload_svc, "require_upload_runtime", lambda: None)
    queued = upload_svc.submit_job(video_path="/tmp/good.mp4", account_name="unrelated")
    stored = cookie_dir.parent / "upload_jobs.json"
    unknown = {"job_id": "prior", "status": "uploading"}
    stored.write_text(json.dumps([queued, unknown]))
    calls = []
    monkeypatch.setattr(upload_svc, "_run_upload_sync", lambda job: calls.append(job["job_id"]) or "Completed")

    with pytest.raises(upload_svc.UploadReconciliationRequired):
        upload_svc.submit_job(video_path="/tmp/new.mp4", account_name="different")
    asyncio.run(upload_svc.process_queue())
    assert calls == []
    assert upload_svc.get_job(queued["job_id"])["status"] == "queued"
    assert unknown in json.loads(stored.read_text())


@pytest.mark.parametrize("unknown", [None, ["lost upload"], {
    "job_id": "prior", "status": "uploading", "video_path": "/tmp/bad\x00path.mp4",
}])
def test_unidentifiable_quarantined_attempt_never_reposts(
    cookie_dir, monkeypatch, unknown,
):
    monkeypatch.setattr(upload_svc, "require_upload_runtime", lambda: None)
    queued = upload_svc.submit_job(video_path="/tmp/good.mp4", account_name="linked")
    stored = cookie_dir.parent / "upload_jobs.json"
    stored.write_text(json.dumps([queued, unknown]))
    original = json.loads(stored.read_text())
    calls = []
    monkeypatch.setattr(upload_svc, "_run_upload_sync", lambda job: calls.append(job["job_id"]) or "Completed")

    with pytest.raises(upload_svc.UploadReconciliationRequired):
        upload_svc.submit_job(video_path="/tmp/new.mp4", account_name="other")
    asyncio.run(upload_svc.process_queue())
    assert calls == []
    assert upload_svc.get_job(queued["job_id"])["status"] == "queued"
    assert json.loads(stored.read_text()) == original
    quarantine = stored.with_name(stored.name + ".quarantine.jsonl")
    assert any(json.loads(line)["job"] == unknown for line in quarantine.read_text().splitlines())


def test_quarantine_write_failure_keeps_valid_job_available(cookie_dir, monkeypatch, caplog):
    monkeypatch.setattr(upload_svc, "require_upload_runtime", lambda: None)
    good = upload_svc.submit_job(video_path="/tmp/good.mp4", account_name="unrelated")
    bad = {**good, "job_id": "bad-row", "video_path": "", "account_name": "linked"}
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
    assert persisted[0]["status"] == "held"
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


@pytest.mark.parametrize("status", ["cancelled", "completed"])
@pytest.mark.parametrize("malformed", [False, True])
def test_only_valid_terminal_upload_allows_same_account_video_retry(
    sync_client, cookie_dir, monkeypatch, status, malformed,
):
    _write_cookie(cookie_dir, "linked")
    monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: _FAKE_UPLOADER)
    calls = []
    monkeypatch.setattr(upload_svc, "_run_upload_sync", lambda job: calls.append(job["job_id"]) or "Completed")

    async def leave_queued():
        pass

    monkeypatch.setattr(upload_router, "process_queue", leave_queued)
    original = upload_svc.submit_job(video_path="/tmp/clip.mp4", account_name="linked", description="old caption")
    if status == "cancelled":
        cancelled = sync_client.post(f"/api/upload/jobs/{original['job_id']}/cancel")
        assert cancelled.status_code == 200
    else:
        upload_svc._update_job(original["job_id"], {
            "status": "completed", "video_id": "1234567890123456789",
            "video_url": "https://www.tiktok.com/@linked/video/1234567890123456789",
        })
    original = upload_svc.get_job(original["job_id"])
    assert original["status"] == status
    stored = cookie_dir.parent / "upload_jobs.json"
    if malformed:
        original["job_id"] = ""
        stored.write_text(json.dumps([original]))
    calls.clear()

    response = sync_client.post("/api/upload/submit", json={
        "video_path": "/tmp/other/../clip.mp4", "account_name": "linked", "description": "fixed caption",
    })

    if malformed:
        assert response.status_code == 409
        assert "reconciliation" in response.json()["detail"]
        assert json.loads(stored.read_text()) == [original]
        assert calls == []
        return

    assert response.status_code == 200
    retry = response.json()["job"]
    assert retry["job_id"] != original["job_id"]
    assert retry["status"] == "queued"
    assert retry["description"] == "fixed caption"
    saved = json.loads(stored.read_text())
    assert len(saved) == 2
    assert original in saved
    assert calls == []
    asyncio.run(upload_svc._process_single_job(retry["job_id"]))
    assert calls == [retry["job_id"]]
    assert upload_svc.get_job(retry["job_id"])["status"] == "held"


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("other_status,other_path", [
    ("uploading", "/tmp/conflict.mp4"),
    ("completed", "/tmp/other-copy.mp4"),
    ("cancelled", "/tmp/other-copy.mp4"),
    ("queued", "/tmp/other-copy.mp4"),
    (None, "/tmp/other-copy.mp4"),
])
def test_conflicting_upload_id_blocks_every_copy_and_preserves_other_jobs(
    sync_client, cookie_dir, monkeypatch, caplog, reverse, other_status, other_path,
):
    _write_cookie(cookie_dir, "linked")
    monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: _FAKE_UPLOADER)
    disputed = upload_svc.submit_job(video_path="/tmp/conflict.mp4", account_name="linked")
    good = upload_svc.submit_job(video_path="/tmp/good.mp4", account_name="unrelated")
    other = {**disputed, "status": other_status, "video_path": other_path}
    copies = [other, disputed] if reverse else [disputed, other]
    stored = cookie_dir.parent / "upload_jobs.json"
    stored.write_text(json.dumps([*copies, good]))
    before = stored.read_bytes()
    calls = []
    monkeypatch.setattr(upload_svc, "_run_upload_sync", lambda job: calls.append(job["job_id"]) or "Completed")

    async def leave_queued():
        pass

    monkeypatch.setattr(upload_router, "process_queue", leave_queued)
    asyncio.run(upload_svc._process_single_job(disputed["job_id"]))
    assert calls == []
    assert stored.read_bytes() == before
    assert [(job["job_id"], job["status"]) for job in upload_svc.get_all_jobs()] == [(good["job_id"], "queued"), (disputed["job_id"], "held")]
    assert upload_svc.cancel_job(disputed["job_id"]) is None
    assert upload_svc._update_job(disputed["job_id"], {"status": "completed"}) is None

    for path in dict.fromkeys([disputed["video_path"], other_path]):
        response = sync_client.post("/api/upload/submit", json={"video_path": path, "account_name": "linked"})
        assert response.status_code == 409
        assert disputed["job_id"] in response.json()["detail"]
    assert calls == []
    assert stored.read_bytes() == before

    asyncio.run(upload_svc._process_single_job(good["job_id"]))
    assert calls == [good["job_id"]]
    assert upload_svc.get_job(good["job_id"])["status"] == "held"
    assert upload_svc.get_job(disputed["job_id"])["status"] == "held"
    saved = json.loads(stored.read_text())
    assert [row for row in saved if row["job_id"] == disputed["job_id"]] == copies
    quarantine = stored.with_name(stored.name + ".quarantine.jsonl")
    records = [json.loads(line) for line in quarantine.read_text().splitlines()]
    assert len(records) == 2
    assert [record["job"] for record in records] == copies
    assert all("duplicate stored rows" in record["reason"] for record in records)
    assert f"job_id {disputed['job_id']} has duplicate stored rows" in caplog.text


def test_identical_upload_rows_are_held_before_provider_and_other_submission_runs(
    sync_client, cookie_dir, monkeypatch, caplog,
):
    _write_cookie(cookie_dir, "linked")
    monkeypatch.setattr(upload_svc.importlib, "import_module", lambda name: _FAKE_UPLOADER)
    repeated = upload_svc.submit_job(video_path="/tmp/repeated.mp4", account_name="linked")
    stored = cookie_dir.parent / "upload_jobs.json"
    stored.write_text(json.dumps([repeated, repeated]))
    before = stored.read_bytes()
    calls = []
    monkeypatch.setattr(upload_svc, "_run_upload_sync", lambda job: calls.append(job["job_id"]) or "Completed")

    async def leave_queued():
        pass

    monkeypatch.setattr(upload_router, "process_queue", leave_queued)
    asyncio.run(upload_svc._process_single_job(repeated["job_id"]))
    assert calls == []
    assert stored.read_bytes() == before
    held = sync_client.get(f"/api/upload/jobs/{repeated['job_id']}")
    assert held.status_code == 200
    assert held.json()["job"]["status"] == "held"
    assert "reconciliation" in held.json()["job"]["error"]
    listed = sync_client.get("/api/upload/jobs").json()["jobs"]
    assert [(job["job_id"], job["status"]) for job in listed] == [(repeated["job_id"], "held")]
    retry = sync_client.post("/api/upload/submit", json={"video_path": repeated["video_path"], "account_name": "linked"})
    assert retry.status_code == 409
    _write_cookie(cookie_dir, "unrelated")
    response = sync_client.post("/api/upload/submit", json={"video_path": "/tmp/good.mp4", "account_name": "unrelated"})
    assert response.status_code == 200
    good_id = response.json()["job"]["job_id"]
    asyncio.run(upload_svc.process_queue())
    assert calls == [good_id]
    assert upload_svc.get_job(good_id)["status"] == "held"
    assert upload_svc.get_job(repeated["job_id"])["status"] == "held"
    assert [row for row in json.loads(stored.read_text()) if row["job_id"] == repeated["job_id"]] == [repeated, repeated]
    records = [json.loads(line) for line in stored.with_name(stored.name + ".quarantine.jsonl").read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["job"] == repeated
    assert "duplicate stored rows" in records[0]["reason"]
    assert f"job_id {repeated['job_id']} has duplicate stored rows" in caplog.text


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
    assert upload_svc.get_job(original["job_id"])["status"] == "held"


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
