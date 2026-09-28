import hashlib
import logging
from datetime import datetime, timedelta, timezone

import pytest

from services import visual_admission as gate


class _Clock(datetime):
    current = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.current


def _fixture(monkeypatch, tmp_path, *, clip_count=1):
    from routers import control_plane as cp

    clips = []
    for index in range(clip_count):
        path = tmp_path / f"clip-{index}.mp4"
        path.write_bytes(f"fixture-{index}".encode())
        clips.append({
            "path": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        })
    job_id = "cpl-abcdef0123456789"
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    monkeypatch.setattr(cp, "datetime", _Clock)
    cp.atomic_save(cp._jobs_path(), {"jobs": {job_id: {
        "pageId": "acct:test",
        "artifactRoot": str(tmp_path),
        "clips": clips,
    }}})
    return cp, job_id, clips


def _record(cp, job_id, clips, *, index, attempt, error, at):
    _Clock.current = at
    clip = clips[index]
    item_key = cp._visual_sweep_item_key(index, clip)
    sweep_id = f"sweep-{index}-{attempt}"
    with cp.lock_for(cp._jobs_path()):
        store = cp._load_jobs()
        store["jobs"][job_id]["visualAdmissionSweep"] = {
            "id": sweep_id,
            "runtime": cp._VISUAL_RUNTIME,
            "running": True,
            "itemKey": item_key,
            "outputIndex": index,
        }
        cp._save_jobs(store)
    cp._record_visual_sweep_failure(job_id, sweep_id, error)
    return item_key


def _all_providers_down():
    return gate.VisionUnavailable(
        "vision_unavailable_all_providers",
        model={
            "name": "gpt-4o-mini",
            "provider": "openai",
            "fallback": True,
            "fallbackReason": "vision_service_unavailable",
            "fallbackError": "vision_service_unavailable",
        },
    )


def test_both_providers_failing_for_ten_minutes_never_terminalizes_output(
    monkeypatch, tmp_path,
):
    cp, job_id, clips = _fixture(monkeypatch, tmp_path)
    base = _Clock.current

    for attempt in range(6):
        _record(
            cp,
            job_id,
            clips,
            index=0,
            attempt=attempt,
            error=_all_providers_down(),
            at=base + timedelta(minutes=attempt * 2),
        )

    job = cp._load_jobs()["jobs"][job_id]
    item_key = cp._visual_sweep_item_key(0, clips[0])
    assert job["visualAdmissionFailures"][item_key]["count"] == 6
    assert job.get("visualAdmission", {}).get("0") is None


def test_provider_failures_do_not_consume_terminal_retry_budget_even_after_six_hours(
    monkeypatch, tmp_path, caplog,
):
    """Mutant pin: classifying provider errors as terminal-counting must fail."""
    cp, job_id, clips = _fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(cp, "_VISUAL_SWEEP_MAX_RETRIES", 2)
    base = _Clock.current

    with caplog.at_level(logging.ERROR):
        for attempt in range(3):
            _record(
                cp,
                job_id,
                clips,
                index=0,
                attempt=attempt,
                error=_all_providers_down(),
                at=base + timedelta(hours=4 * attempt),
            )

    job = cp._load_jobs()["jobs"][job_id]
    item_key = cp._visual_sweep_item_key(0, clips[0])
    assert job["visualAdmissionFailures"][item_key]["count"] == 3
    assert job["visualAdmissionFailures"][item_key]["terminalFailureCount"] == 0
    assert job.get("visualAdmission", {}).get("0") is None
    assert "visual_admission_persistent_failure" in caplog.text


def test_unknown_sweep_exceptions_default_to_transient(monkeypatch, tmp_path):
    cp, job_id, clips = _fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(cp, "_VISUAL_SWEEP_MAX_RETRIES", 2)
    base = _Clock.current

    for attempt in range(3):
        _record(
            cp,
            job_id,
            clips,
            index=0,
            attempt=attempt,
            error=RuntimeError("new_scanner_failure"),
            at=base + timedelta(minutes=attempt),
        )

    job = cp._load_jobs()["jobs"][job_id]
    item_key = cp._visual_sweep_item_key(0, clips[0])
    failure = job["visualAdmissionFailures"][item_key]
    assert failure["count"] == 3
    assert failure["terminalFailureCount"] == 0
    assert failure["failureClass"] == "transient"
    assert job.get("visualAdmission", {}).get("0") is None


def test_only_corrupt_output_terminalizes_after_retry_cap(monkeypatch, tmp_path):
    cp, job_id, clips = _fixture(monkeypatch, tmp_path, clip_count=2)
    monkeypatch.setattr(cp, "_VISUAL_SWEEP_MAX_RETRIES", 2)
    base = _Clock.current

    for attempt in range(3):
        _record(
            cp,
            job_id,
            clips,
            index=0,
            attempt=attempt,
            error=_all_providers_down(),
            at=base + timedelta(minutes=attempt),
        )
        _record(
            cp,
            job_id,
            clips,
            index=1,
            attempt=attempt,
            error=RuntimeError("incomplete_decode"),
            at=base + timedelta(minutes=attempt),
        )

    job = cp._load_jobs()["jobs"][job_id]
    assert job.get("visualAdmission", {}).get("0") is None
    assert job["visualAdmission"]["1"]["reason"] == "scan_failure_retry_exhausted"
    provider_key = cp._visual_sweep_item_key(0, clips[0])
    corrupt_key = cp._visual_sweep_item_key(1, clips[1])
    assert job["visualAdmissionFailures"][provider_key]["terminalFailureCount"] == 0
    assert job["visualAdmissionFailures"][corrupt_key]["terminalFailureCount"] == 3


def test_visual_sweep_failure_backoff_is_exponential_and_capped(monkeypatch, tmp_path):
    cp, _, _ = _fixture(monkeypatch, tmp_path)
    failed_at = _Clock.current

    assert cp._visual_sweep_backed_off({
        "scanFailedAt": failed_at.isoformat(),
        "scanFailures": 4,
    }, failed_at + timedelta(seconds=239))
    assert not cp._visual_sweep_backed_off({
        "scanFailedAt": failed_at.isoformat(),
        "scanFailures": 4,
    }, failed_at + timedelta(seconds=240))
    assert cp._visual_sweep_backoff_seconds(100) == 30 * 60


def test_output_recovers_normally_after_provider_outage(monkeypatch, tmp_path):
    cp, job_id, clips = _fixture(monkeypatch, tmp_path)
    base = _Clock.current
    for attempt in range(6):
        _record(
            cp,
            job_id,
            clips,
            index=0,
            attempt=attempt,
            error=_all_providers_down(),
            at=base + timedelta(minutes=attempt * 2),
        )

    decision = gate.pending_decision(
        page_id="acct:test",
        job_id=job_id,
        index=0,
        sha256=clips[0]["sha256"],
        byte_count=clips[0]["bytes"],
    )
    decision.update(verdict="clean", reason="full_frame_ocr_and_vision_clean")
    monkeypatch.setattr(gate, "scan_artifact", lambda *args, **kwargs: decision)
    sweep_id = "sweep-recovered"
    with cp.lock_for(cp._jobs_path()):
        store = cp._load_jobs()
        store["jobs"][job_id]["visualAdmissionSweep"] = {
            "id": sweep_id,
            "runtime": cp._VISUAL_RUNTIME,
            "running": True,
        }
        cp._save_jobs(store)

    cp._finish_visual_sweep(job_id, sweep_id)

    job = cp._load_jobs()["jobs"][job_id]
    item_key = cp._visual_sweep_item_key(0, clips[0])
    assert job["visualAdmission"]["0"]["verdict"] == "clean"
    assert item_key not in job.get("visualAdmissionFailures", {})
