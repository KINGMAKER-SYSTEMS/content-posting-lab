"""Delivered-media age-out: succeeded post renders leave the volume once R2 holds them."""
import json
import logging
from collections import namedtuple

import httpx
import pytest

from services import post_render_jobs as jobs
from services.post_render import sha256
from tests.test_post_render import NOW
from tests.test_post_render_jobs import _iso_ms, _write_fake_render, service, submission

DAY = 24 * 60 * 60 * 1000
HOUR = 60 * 60 * 1000
WINDOW = jobs.DELIVERED_RETIRE_AFTER_MS["normal"]
Usage = namedtuple("Usage", "total used free")


def aged_worker(tmp_path, confirm, *, renderer=None):
    now = [NOW]
    kwargs = {"clock": lambda: now[0]}
    if renderer is not None:
        kwargs["renderer"] = renderer
    worker = service(tmp_path, **kwargs)
    worker.confirm_final = confirm
    return worker, now


def media_dir(worker, job_id):
    return worker.root / "attempts" / job_id


def test_confirmed_delivered_media_is_retired_and_keeps_its_tombstone(tmp_path):
    calls = []

    def confirm(final_sha, length):
        calls.append((final_sha, length))
        return True

    worker, now = aged_worker(tmp_path, confirm)
    job = worker.enqueue(submission(slot_id="slot:delivered"), "delivered-key")
    assert worker.run_one()
    receipt = json.loads(worker.artifact(job["id"], "receipt").read_text())
    now[0] += WINDOW + 1

    summary = worker._age_out_delivered()

    assert summary["retired"] == 1 and summary["bytes_freed"] > 0
    assert calls == [(receipt["final_sha256"], receipt["final_byte_length"])]
    assert not media_dir(worker, job["id"]).exists()
    status = worker.status(job["id"])
    assert status["retired_at_ms"] is not None
    assert status["final_sha256"] == receipt["final_sha256"]
    # Idempotency replay still answers from the tombstone.
    replay = service(tmp_path).enqueue(submission(slot_id="slot:delivered"), "delivered-key")
    assert replay["id"] == job["id"] and replay["state"] == "succeeded"


@pytest.mark.parametrize("answer", [False, None])
def test_media_the_control_plane_does_not_confirm_is_never_deleted(tmp_path, answer):
    worker, now = aged_worker(tmp_path, lambda _sha, _length: answer)
    job = worker.enqueue(submission(slot_id="slot:unconfirmed"), "unconfirmed-key")
    assert worker.run_one()
    now[0] += 30 * DAY

    summary = worker._age_out_delivered()

    assert summary == {"retired": 0, "unconfirmed": 1, "failed": 0, "bytes_freed": 0}
    assert media_dir(worker, job["id"]).exists()
    assert worker.status(job["id"])["retired_at_ms"] is None
    assert worker.artifact(job["id"], "final").exists()


def test_unconfirmed_job_is_rechecked_only_after_the_recheck_window(tmp_path):
    answers = [False, True]
    worker, now = aged_worker(tmp_path, lambda _sha, _length: answers.pop(0))
    job = worker.enqueue(submission(slot_id="slot:recheck"), "recheck-key")
    assert worker.run_one()
    now[0] += WINDOW + 1

    assert worker._age_out_delivered()["unconfirmed"] == 1
    assert worker._age_out_delivered() == {"retired": 0, "unconfirmed": 0, "failed": 0, "bytes_freed": 0}
    now[0] += jobs.AGE_OUT_RECHECK_MS["normal"]
    assert worker._age_out_delivered()["retired"] == 1
    assert not media_dir(worker, job["id"]).exists()


def test_recent_success_or_recent_slot_is_kept_even_when_confirmed(tmp_path):
    worker, now = aged_worker(tmp_path, lambda _sha, _length: True)
    window = WINDOW
    fresh = worker.enqueue(submission(slot_id="slot:fresh"), "fresh-key")
    assert worker.run_one()
    later_slot = worker.enqueue(
        submission(slot_id="slot:later", planned_at=_iso_ms(NOW + 2 * window)), "later-key")
    assert worker.run_one()
    now[0] += window + 1  # fresh is now old enough; later's planned slot is still ahead

    worker._age_out_delivered()

    assert worker.status(fresh["id"])["retired_at_ms"] is not None
    assert worker.status(later_slot["id"])["retired_at_ms"] is None
    assert media_dir(worker, later_slot["id"]).exists()
    now[0] += 2 * window
    worker._age_out_delivered()
    assert worker.status(later_slot["id"])["retired_at_ms"] is not None


def test_jobs_that_are_not_succeeded_are_never_offered_for_confirmation(tmp_path):
    calls = []
    worker, now = aged_worker(tmp_path, lambda sha, length: calls.append(sha) or True)
    queued = worker.enqueue(submission(slot_id="slot:still-queued"), "queued-key")
    now[0] += 30 * DAY

    worker._age_out_delivered()

    assert calls == []
    assert worker.status(queued["id"])["state"] == "queued"


def test_shared_final_is_retired_without_taking_the_owners_hash_authority(tmp_path):
    def same_bytes(_source, output, request, *, clock_ms):
        _write_fake_render(output, request, clock_ms=clock_ms, final=b"identical final bytes")

    worker, now = aged_worker(tmp_path, lambda _sha, _length: True, renderer=same_bytes)
    legacy = worker.enqueue(submission(slot_id="slot:legacy"), "legacy-key")
    assert worker.run_one()
    # A row finished before final_sha256 was recorded: its hash column is empty.
    with worker._db() as db:
        db.execute("UPDATE jobs SET final_sha256=NULL,qa_frame_sha256=NULL,final_byte_length=NULL,"
                   "output_authority_json=NULL WHERE id=?", (legacy["id"],))
    owner = worker.enqueue(submission(slot_id="slot:owner"), "owner-key")
    assert worker.run_one()
    shared = sha256(b"identical final bytes")
    assert worker.status(owner["id"])["final_sha256"] == shared
    with pytest.raises(jobs.RenderJobError, match="duplicate_output"):
        worker.acknowledge(legacy["id"])  # the endpoint path cannot free this one
    now[0] += WINDOW + 1

    summary = worker._age_out_delivered()

    assert summary["retired"] == 2 and summary["failed"] == 0
    assert not media_dir(worker, legacy["id"]).exists()
    assert worker.status(owner["id"])["final_sha256"] == shared
    assert worker._row(legacy["id"])["final_sha256"] is None
    # The legacy tombstone still proves exactly which final it produced.
    assert worker._verify_output(worker._row(legacy["id"]))["final_sha256"] == shared


def test_one_run_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "AGE_OUT_BATCH", 2)
    worker, now = aged_worker(tmp_path, lambda _sha, _length: True)
    ids = []
    for index in range(3):
        ids.append(worker.enqueue(submission(slot_id=f"slot:bounded-{index}"), f"bounded-key-{index}")["id"])
        assert worker.run_one()
    now[0] += WINDOW + 1

    assert worker._age_out_delivered()["retired"] == 2
    assert sum(media_dir(worker, job_id).exists() for job_id in ids) == 1
    assert worker._age_out_delivered()["retired"] == 1


def pinned_volume(monkeypatch, *, free, total=256 * 1024 ** 3):
    usage = {"free": free}
    monkeypatch.setattr(jobs.shutil, "disk_usage", lambda _path: Usage(total, total - usage["free"], usage["free"]))
    return usage


def test_age_out_runs_on_its_interval_and_logs_volume(tmp_path, monkeypatch, caplog):
    runs = []
    worker, now = aged_worker(tmp_path, lambda _sha, _length: True)
    monkeypatch.setattr(worker, "_age_out_delivered", lambda pressure: runs.append((now[0], pressure)) or {})
    gib = 1024 ** 3
    pinned_volume(monkeypatch, free=156 * gib)
    caplog.set_level(logging.INFO, logger="content_lab.post_render_jobs")

    worker._maybe_age_out()
    worker._maybe_age_out()
    now[0] += jobs.AGE_OUT_INTERVAL_MS["normal"]
    worker._maybe_age_out()

    assert runs == [(NOW, "normal"), (NOW + jobs.AGE_OUT_INTERVAL_MS["normal"], "normal")]
    assert f"used_bytes={100 * gib} free_bytes={156 * gib}" in caplog.text and "pressure=normal" in caplog.text


def test_under_25_percent_free_the_window_tightens_by_itself(tmp_path, monkeypatch, caplog):
    worker, now = aged_worker(tmp_path, lambda _sha, _length: True)
    job = worker.enqueue(submission(slot_id="slot:tight"), "tight-key")
    assert worker.run_one()
    now[0] += jobs.DELIVERED_RETIRE_AFTER_MS["tight"] + 1  # 24 h: too young for the normal 72 h window
    total = 256 * 1024 ** 3
    volume = pinned_volume(monkeypatch, free=int(total * 0.30), total=total)

    assert worker._maybe_age_out()["retired"] == 0
    assert media_dir(worker, job["id"]).exists()

    volume["free"] = int(total * 0.20)
    now[0] += jobs.AGE_OUT_INTERVAL_MS["normal"]
    summary = worker._maybe_age_out()

    assert summary["pressure"] == "tight" and summary["retired"] == 1
    assert not media_dir(worker, job["id"]).exists()
    assert "acted on volume pressure=tight" in caplog.text
    # Under pressure the next pass comes sooner.
    now[0] += jobs.AGE_OUT_INTERVAL_MS["tight"]
    assert worker._maybe_age_out() is not None


def test_below_the_floor_an_emergency_pass_runs_before_a_claim_is_refused(tmp_path, monkeypatch, caplog):
    def failing(source, output, request, **kwargs):
        raise jobs.RenderJobError("render_failed")

    worker, now = aged_worker(tmp_path, lambda _sha, _length: True)
    delivered = worker.enqueue(submission(slot_id="slot:emergency"), "emergency-key")
    assert worker.run_one()
    worker.renderer = failing
    broken = worker.enqueue(submission(slot_id="slot:broken"), "broken-key")
    assert worker.run_one()
    failed_attempt = worker.root / "attempts" / broken["id"]
    assert worker.status(broken["id"])["state"] == "failed" and failed_attempt.exists()
    now[0] += jobs.DELIVERED_RETIRE_AFTER_MS["floor"] + 1  # far inside the normal and tight windows
    floor = jobs.render_claim_floor_bytes(worker.worker_count)
    volume = pinned_volume(monkeypatch, free=floor - 1)
    freed = []
    real_retire = worker._retire_row

    def retire_and_free(row, **kwargs):
        done = real_retire(row, **kwargs)
        volume["free"] = floor + 1
        freed.append(row["id"])
        return done

    monkeypatch.setattr(worker, "_retire_row", retire_and_free)
    waiting = worker.enqueue(submission(slot_id="slot:waiting"), "waiting-key")

    assert worker._has_workspace_capacity(job_id=waiting["id"]) is True
    assert freed == [delivered["id"]]
    assert not media_dir(worker, delivered["id"]).exists()
    assert not any(failed_attempt.glob("*"))  # failed-attempt bytes went first, at any age
    assert "acted on volume pressure=floor" in caplog.text


def test_a_refused_claim_pulls_a_pass_forward_at_most_once_per_minute(tmp_path, monkeypatch, caplog):
    runs = []
    worker, now = aged_worker(tmp_path, lambda _sha, _length: True)
    monkeypatch.setattr(worker, "_age_out_delivered", lambda pressure: runs.append(pressure) or {
        "retired": 0, "unconfirmed": 0, "failed": 0, "bytes_freed": 0})
    pinned_volume(monkeypatch, free=1)

    assert worker._has_workspace_capacity() is False
    assert worker._has_workspace_capacity() is False
    now[0] += jobs.AGE_OUT_INTERVAL_MS["floor"]
    assert worker._has_workspace_capacity() is False

    assert runs == ["floor", "floor"]
    assert "post_render_workspace_capacity_insufficient" in caplog.text


SHA = "ab" * 32


def _confirm(monkeypatch, handler, *, configured=True):
    for name, value in (("CONTENT_LAB_CONTROL_PLANE_ORIGIN", "https://control.example.test"),
                        ("CONTROL_PLANE_SERVICE_ID", "svc-id"), ("CONTROL_PLANE_SERVICE_SECRET", "svc-secret")):
        if configured:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    return jobs.confirm_admitted_final(SHA, 1234, transport=httpx.MockTransport(wrapped)), seen


def test_confirmation_asks_the_prepared_video_door_for_one_byte(monkeypatch):
    result, seen = _confirm(monkeypatch, lambda _r: httpx.Response(206, headers={"Content-Range": "bytes 0-0/1234"}))
    assert result is True
    request = seen[0]
    assert str(request.url) == f"https://control.example.test/prepared-video/{SHA}.mp4"
    assert request.headers["range"] == "bytes=0-0"
    assert request.headers["cf-access-client-id"] == "svc-id"


@pytest.mark.parametrize("response,expected", [
    (httpx.Response(206, headers={"Content-Range": "bytes 0-0/999"}), False),  # different object size
    (httpx.Response(404), False),
    (httpx.Response(503), None),
    (httpx.Response(200), None),
    (httpx.Response(302, headers={"Location": "https://elsewhere.test/"}), None),
])
def test_confirmation_is_true_only_for_an_exact_partial_answer(monkeypatch, response, expected):
    assert _confirm(monkeypatch, lambda _r: response)[0] is expected


def test_confirmation_is_unknown_when_unconfigured_or_unreachable(monkeypatch):
    result, seen = _confirm(monkeypatch, lambda _r: httpx.Response(206), configured=False)
    assert result is None and seen == []

    def unreachable(_request):
        raise httpx.ConnectError("down")

    assert _confirm(monkeypatch, unreachable)[0] is None
