"""Synthetic-only operator rotation of one job's signed URL credential."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

import routers.control_plane as cp
from scripts.rotate_job_download_token import main
from services.generation_recovery import store_lock
from services.job_token_rotation import RotationRefused, inspect_job, rotate_job
from services.json_store import atomic_save

TARGET = "cpl-" + "a" * 16
OTHER = "cpl-" + "b" * 16
OLD_TOKEN = "test-old-signed-url-token"
OTHER_TOKEN = "test-other-signed-url-token"


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    (artifact_root / "clip.mp4").write_bytes(b"fixture clip")
    (artifact_root / "thumb.jpg").write_bytes(b"fixture thumbnail")
    path = tmp_path / "control_plane_jobs.json"
    jobs = {
        TARGET: {
            "jobId": TARGET, "status": "completed", "pageId": "acct:fixture",
            "token": OLD_TOKEN, "artifactRoot": str(artifact_root),
            "clips": [{"path": "clip.mp4", "name": "clip.mp4",
                       "sha256": hashlib.sha256(b"fixture clip").hexdigest(),
                       "bytes": len(b"fixture clip"), "source": {},
                       "thumbnail": {"path": "thumb.jpg", "name": "thumb.jpg",
                                     "sha256": hashlib.sha256(b"fixture thumbnail").hexdigest(),
                                     "bytes": len(b"fixture thumbnail")}}],
        },
        OTHER: {
            "jobId": OTHER, "status": "completed", "pageId": "acct:other",
            "token": OTHER_TOKEN, "clips": [],
        },
    }
    atomic_save(path, {"version": 1, "jobs": jobs, "byIdempotency": {}, "served": {}})
    monkeypatch.setattr(cp, "_jobs_path", lambda: path)
    return path


def _read(path: Path):
    return json.loads(path.read_text())


def test_dry_run_and_apply_change_only_selected_token_without_secret_output(store, capsys):
    before = _read(store)
    before_bytes = store.read_bytes()
    assert main(["--store", str(store), "--job-id", TARGET, "--dry-run"]) == 0
    dry_output = capsys.readouterr().out
    dry = json.loads(dry_output)
    assert dry["status"] == "ready"
    assert dry["artifactCount"] == 1
    assert OLD_TOKEN not in dry_output and OTHER_TOKEN not in dry_output
    assert store.read_bytes() == before_bytes
    assert not Path(str(store) + ".lock").exists()

    assert main(["--store", str(store), "--job-id", TARGET, "--apply",
                 "--expected-job-sha256", dry["jobRevisionSha256"]]) == 0
    printed = capsys.readouterr().out
    after = _read(store)
    assert after["jobs"][OTHER] == before["jobs"][OTHER]
    assert after["jobs"][TARGET]["token"] != OLD_TOKEN
    assert after["jobs"][TARGET]["signedUrlTokenRotation"]["reason"] == "exposed_signed_url"
    selected_after = {key: value for key, value in after["jobs"][TARGET].items()
                      if key not in {"token", "signedUrlTokenRotation"}}
    selected_before = {key: value for key, value in before["jobs"][TARGET].items() if key != "token"}
    assert selected_after == selected_before
    assert OLD_TOKEN not in printed
    assert OTHER_TOKEN not in printed
    assert after["jobs"][TARGET]["token"] not in printed
    assert store.stat().st_mode & 0o077 == 0

    with pytest.raises(HTTPException) as refused:
        cp.job_download(TARGET, 0, token=OLD_TOKEN)
    assert refused.value.status_code == 403
    with pytest.raises(HTTPException) as refused_thumb:
        cp.job_thumbnail(TARGET, 0, token=OLD_TOKEN)
    assert refused_thumb.value.status_code == 403
    assert cp.job_download(TARGET, 0, token=after["jobs"][TARGET]["token"]).path.name == "clip.mp4"
    assert cp.job_thumbnail(TARGET, 0, token=after["jobs"][TARGET]["token"]).path.name == "thumb.jpg"


def test_absent_or_archived_job_is_a_safe_no_op(store):
    before = store.read_bytes()
    absent = "cpl-" + "c" * 16
    assert inspect_job(store, absent)["status"] == "not_found_or_archived"
    result = rotate_job(store, absent, expected_job_sha256="0" * 64)
    assert result["status"] == "not_found_or_archived"
    assert store.read_bytes() == before
    with store_lock(store):
        current = _read(store)
        current["jobs"].pop(TARGET)
        current["byIdempotency"]["archived-key"] = TARGET
        atomic_save(store, current)
    archived_bytes = store.read_bytes()
    assert inspect_job(store, TARGET)["status"] == "not_found_or_archived"
    assert rotate_job(store, TARGET, expected_job_sha256="0" * 64)["status"] == "not_found_or_archived"
    assert store.read_bytes() == archived_bytes


def test_changed_job_revision_refuses_without_restoring_old_token(store, capsys):
    old_revision = inspect_job(store, TARGET)["jobRevisionSha256"]
    with store_lock(store):
        current = _read(store)
        current["jobs"][TARGET]["statusNote"] = "other writer"
        atomic_save(store, current)
    after_other_writer = store.read_bytes()
    with pytest.raises(RotationRefused, match="job_changed_retry_dry_run"):
        rotate_job(store, TARGET, expected_job_sha256=old_revision)
    assert store.read_bytes() == after_other_writer
    assert main(["--store", str(store), "--job-id", TARGET, "--apply",
                 "--expected-job-sha256", old_revision]) == 2
    assert capsys.readouterr().out.strip() == '{"reason": "job_changed_retry_dry_run", "status": "refused"}'


def test_unrelated_writer_change_is_preserved_under_job_scoped_compare_and_set(store):
    revision = inspect_job(store, TARGET)["jobRevisionSha256"]
    with store_lock(store):
        current = _read(store)
        current["jobs"][OTHER]["statusNote"] = "unrelated writer"
        atomic_save(store, current)
    assert rotate_job(store, TARGET, expected_job_sha256=revision)["status"] == "rotated"
    assert _read(store)["jobs"][OTHER]["statusNote"] == "unrelated writer"


def test_rotation_waits_for_same_store_lock(store):
    revision = inspect_job(store, TARGET)["jobRevisionSha256"]
    started = threading.Event()
    finished = threading.Event()
    outcome = []

    def rotate():
        started.set()
        outcome.append(rotate_job(store, TARGET, expected_job_sha256=revision))
        finished.set()

    with store_lock(store):
        thread = threading.Thread(target=rotate)
        thread.start()
        assert started.wait(1)
        assert not finished.wait(0.1)
        assert _read(store)["jobs"][TARGET]["token"] == OLD_TOKEN
    thread.join(timeout=2)
    assert finished.is_set()
    assert outcome[0]["status"] == "rotated"


def test_active_or_corrupt_store_refuses_without_write(store):
    with store_lock(store):
        current = _read(store)
        current["jobs"][TARGET]["status"] = "running"
        atomic_save(store, current)
    before = store.read_bytes()
    assert inspect_job(store, TARGET)["status"] == "not_rotatable"
    assert rotate_job(store, TARGET, expected_job_sha256="0" * 64)["status"] == "not_rotatable"
    assert store.read_bytes() == before
    store.write_text("{broken")
    with pytest.raises(RotationRefused, match="store_unreadable"):
        inspect_job(store, TARGET)


def test_store_path_must_be_existing_regular_non_symlink(store):
    link = store.with_name("linked-jobs.json")
    link.symlink_to(store)
    with pytest.raises(RotationRefused, match="store_unavailable"):
        inspect_job(link, TARGET)
    with pytest.raises(RotationRefused, match="store_unavailable"):
        rotate_job(link, TARGET, expected_job_sha256="0" * 64)
    assert not Path(str(link) + ".lock").exists()


@pytest.mark.parametrize("clips", [1, {"unexpected": "shape"}, "bad", None])
def test_malformed_artifacts_refuse_with_bounded_secret_free_code(store, clips, capsys):
    current = _read(store)
    current["jobs"][TARGET]["clips"] = clips
    atomic_save(store, current)
    before = store.read_bytes()
    assert main(["--store", str(store), "--job-id", TARGET, "--dry-run"]) == 2
    assert capsys.readouterr().out.strip() == '{"reason": "job_artifacts_invalid", "status": "refused"}'
    assert store.read_bytes() == before


@pytest.mark.parametrize("audit", [7, "bad", [], None])
def test_malformed_existing_audit_refuses_without_resetting_count(store, audit, capsys):
    current = _read(store)
    current["jobs"][TARGET]["signedUrlTokenRotation"] = audit
    atomic_save(store, current)
    revision = inspect_job(store, TARGET)["jobRevisionSha256"]
    before = store.read_bytes()
    assert main(["--store", str(store), "--job-id", TARGET, "--apply",
                 "--expected-job-sha256", revision]) == 2
    assert capsys.readouterr().out.strip() == '{"reason": "rotation_audit_invalid", "status": "refused"}'
    assert store.read_bytes() == before


def test_shared_writable_directory_refuses_apply_without_store_change(store):
    revision = inspect_job(store, TARGET)["jobRevisionSha256"]
    before = store.read_bytes()
    original_mode = store.parent.stat().st_mode & 0o777
    try:
        store.parent.chmod(0o777)
        with pytest.raises(RotationRefused, match="store_directory_not_private"):
            rotate_job(store, TARGET, expected_job_sha256=revision)
    finally:
        store.parent.chmod(original_mode)
    assert store.read_bytes() == before


def test_preexisting_atomic_temp_file_refuses_without_truncation(store):
    revision = inspect_job(store, TARGET)["jobRevisionSha256"]
    temporary = store.with_suffix(f"{store.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text("existing file must survive")
    before = store.read_bytes()
    with pytest.raises(RotationRefused, match="store_temp_conflict"):
        rotate_job(store, TARGET, expected_job_sha256=revision)
    assert temporary.read_text() == "existing file must survive"
    assert store.read_bytes() == before


@pytest.mark.parametrize("broken_clip", [
    {},
    {"path": "clip.mp4"},
    {"path": "clip.mp4", "name": "clip.mp4", "bytes": 1,
     "sha256": "0" * 64, "source": {}, "thumbnail": {}},
])
def test_incomplete_clip_refuses_before_any_token_change(store, broken_clip):
    current = _read(store)
    current["jobs"][TARGET]["clips"] = [broken_clip]
    atomic_save(store, current)
    before = store.read_bytes()
    with pytest.raises(RotationRefused, match="job_artifacts_invalid"):
        inspect_job(store, TARGET)
    assert store.read_bytes() == before


@pytest.mark.parametrize("audit", [
    {"count": 1, "at": "not-a-date", "reason": "exposed_signed_url"},
    {"count": 1, "at": "2026-10-10T12:00:00", "reason": "exposed_signed_url"},
    {"count": 1, "at": "2026-10-10T12:00:00+00:00", "reason": "other"},
    {"count": 1, "at": "2026-10-10T12:00:00+00:00",
     "reason": "exposed_signed_url", "history": [{"at": "older"}]},
])
def test_malformed_existing_audit_fields_preserved_on_refusal(store, audit):
    current = _read(store)
    current["jobs"][TARGET]["signedUrlTokenRotation"] = audit
    atomic_save(store, current)
    revision = inspect_job(store, TARGET)["jobRevisionSha256"]
    before = store.read_bytes()
    with pytest.raises(RotationRefused, match="rotation_audit_invalid"):
        rotate_job(store, TARGET, expected_job_sha256=revision)
    assert store.read_bytes() == before
