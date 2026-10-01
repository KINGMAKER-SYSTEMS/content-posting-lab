"""Finished generation media leaves the volume once R2 holds each clip; strays after 48 h."""
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from services import generated_media_retention as retention

NOW = datetime(2026, 10, 1, 1, 0, tzinfo=timezone.utc)
A, B = "a" * 64, "b" * 64


def make_job(root, *, name="cpl-1", group="dossier-1", status="completed", age=timedelta(hours=3),
             clips=((A, "renders/hailuo/c0.mp4"),), extra_files=("renders/hailuo/raw.mp4",), **fields):
    directory = root / "acct:page" / group / name
    records = []
    for index, (sha, rel) in enumerate(clips):
        (directory / rel).parent.mkdir(parents=True, exist_ok=True)
        (directory / rel).write_bytes(b"c" * 100)
        thumb = f"thumbnails/{index:04d}.jpg"
        (directory / thumb).parent.mkdir(parents=True, exist_ok=True)
        (directory / thumb).write_bytes(b"t")
        records.append({"path": rel, "sha256": sha, "bytes": 100, "thumbnail": {"path": thumb}})
    for rel in extra_files:
        (directory / rel).parent.mkdir(parents=True, exist_ok=True)
        (directory / rel).write_bytes(b"s" * 50)
    stamp = (NOW - age).isoformat()
    job = {"status": status, "artifactRoot": str(directory), "createdAt": stamp, "completedAt": stamp,
           "sourceKind": "generated", "clips": records, **fields}
    return directory, job


def sweeper(answers):
    asked = []

    def confirm(sha, size):
        asked.append((sha, size))
        return answers(sha) if callable(answers) else answers

    return retention.GeneratedMediaRetention(confirm=confirm, monotonic=lambda: 0.0), asked


def files(directory):
    return sorted(str(path.relative_to(directory)) for path in directory.rglob("*") if path.is_file())


def test_a_clip_r2_holds_is_deleted_and_the_job_record_is_untouched(tmp_path, caplog):
    root = tmp_path / "gen"
    directory, job = make_job(root)
    store = {"jobs": {"j": job}}
    before = json.dumps(store, sort_keys=True)
    worker, asked = sweeper(True)
    caplog.set_level(logging.INFO, logger="content_lab.generated_media_retention")

    summary = worker.sweep(store, root, NOW)

    assert asked == [(A, 100)]
    assert files(directory) == ["renders/hailuo/raw.mp4"]  # the stray waits for 48 h
    assert summary["clip_files"] == 2 and summary["bytes_freed"] == 101
    assert json.dumps(store, sort_keys=True) == before
    assert "clip_files=2" in caplog.text and "free_bytes=" in caplog.text


@pytest.mark.parametrize("answer", [False, None])
def test_a_clip_the_worker_has_not_copied_is_never_deleted_at_any_age(tmp_path, answer):
    root = tmp_path / "gen"
    directory, job = make_job(root, age=timedelta(days=60))
    worker, _ = sweeper(answer)

    summary = worker.sweep({"jobs": {"j": job}}, root, NOW, pressure="floor")

    assert files(directory) == ["renders/hailuo/c0.mp4", "thumbnails/0000.jpg"]  # only the stray went
    assert summary["unconfirmed_bytes_kept"] == 101


def test_strays_go_48_hours_after_the_job_finished_and_sooner_under_pressure(tmp_path):
    root = tmp_path / "gen"
    young, young_job = make_job(root, name="young", age=timedelta(hours=30))
    old, old_job = make_job(root, name="old", age=timedelta(hours=49))
    store = {"jobs": {"young": young_job, "old": old_job}}
    worker, _ = sweeper(False)

    worker.sweep(store, root, NOW)
    assert "renders/hailuo/raw.mp4" in files(young)
    assert "renders/hailuo/raw.mp4" not in files(old)

    worker.sweep(store, root, NOW, pressure="tight")  # 24 h under 25% free
    assert "renders/hailuo/raw.mp4" not in files(young)


def test_a_fully_cleared_job_directory_is_removed_but_never_its_parents(tmp_path):
    root = tmp_path / "gen"
    directory, job = make_job(root, age=timedelta(days=3))
    worker, _ = sweeper(True)

    worker.sweep({"jobs": {"j": job}}, root, NOW)

    assert not directory.exists()
    assert directory.parent.is_dir() and (root / "acct:page").is_dir()


def test_one_unconfirmed_clip_keeps_only_itself(tmp_path):
    root = tmp_path / "gen"
    directory, job = make_job(root, clips=((A, "treated/0.mp4"), (B, "treated/1.mp4")), extra_files=())
    worker, _ = sweeper(lambda sha: sha == A)

    worker.sweep({"jobs": {"j": job}}, root, NOW)

    assert files(directory) == ["thumbnails/0001.jpg", "treated/1.mp4"]


def test_a_just_finished_job_settles_first(tmp_path):
    root = tmp_path / "gen"
    directory, job = make_job(root, age=timedelta(minutes=10))
    worker, asked = sweeper(True)

    worker.sweep({"jobs": {"j": job}}, root, NOW)

    assert asked == [] and len(files(directory)) == 3


@pytest.mark.parametrize("status", ["queued", "running", "paused", None])
def test_a_job_that_is_not_terminal_is_never_touched_however_old(tmp_path, status):
    root = tmp_path / "gen"
    directory, job = make_job(root, status=status, age=timedelta(days=30))
    worker, asked = sweeper(True)

    worker.sweep({"jobs": {"j": job}}, root, NOW, pressure="floor")

    assert asked == [] and len(files(directory)) == 3


def test_a_directory_an_active_job_points_at_is_protected(tmp_path):
    root = tmp_path / "gen"
    master, master_job = make_job(root, name="master", age=timedelta(days=30))
    shared, shared_job = make_job(root, name="shared", age=timedelta(days=30))
    recovery = {"status": "running", "sourceKind": "truck_master_recovery", "artifactRoot": str(shared),
                "recoveryMasters": [{"artifactRoot": str(master), "path": "renders/hailuo/c0.mp4"}]}
    worker, asked = sweeper(True)

    worker.sweep({"jobs": {"master": master_job, "shared": shared_job, "recovery": recovery}}, root, NOW)

    assert asked == [] and len(files(master)) == 3 and len(files(shared)) == 3


def test_the_truck_re_crop_pool_is_kept(tmp_path):
    root = tmp_path / "gen"
    truck, truck_job = make_job(root, name="truck", age=timedelta(days=30))
    truck_job["clips"][0]["source"] = {"contentNiche": "truck"}
    delivered, delivered_job = make_job(root, name="delivered", age=timedelta(days=30))
    delivered_job["clips"][0].update(source={"contentNiche": "TRUCK"}, delivery={})
    worker, _ = sweeper(True)

    worker.sweep({"jobs": {"truck": truck_job, "delivered": delivered_job}}, root, NOW)

    assert len(files(truck)) == 3 and not delivered.exists()


def test_only_files_inside_a_job_directory_inside_the_root_are_eligible(tmp_path):
    root = tmp_path / "gen"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep.mp4").write_bytes(b"k")
    directory, job = make_job(root, age=timedelta(hours=3), extra_files=())
    job["clips"][0]["path"] = "../../../../elsewhere/keep.mp4"
    page_level = {"status": "completed", "artifactRoot": str(root / "acct:page"), "completedAt": job["completedAt"],
                  "clips": [{"path": "dossier-1/cpl-1/renders/hailuo/c0.mp4", "sha256": B, "bytes": 100}]}
    worker, _ = sweeper(True)

    plan = worker.plan({"jobs": {"j": job, "page": page_level}}, root, NOW)

    assert (outside / "keep.mp4").exists()
    assert plan.clip_files == [directory / "thumbnails/0000.jpg"]
    assert plan.stray_files == []


def test_an_unconfirmed_clip_is_rechecked_only_after_the_backoff(tmp_path):
    root = tmp_path / "gen"
    directory, job = make_job(root, extra_files=())
    clock = [0.0]
    answers = [False, True]
    asked = []
    worker = retention.GeneratedMediaRetention(
        confirm=lambda sha, size: asked.append(sha) or answers.pop(0), monotonic=lambda: clock[0])
    store = {"jobs": {"j": job}}

    worker.sweep(store, root, NOW)
    worker.sweep(store, root, NOW)
    assert asked == [A] and directory.exists()
    clock[0] += retention.UNCONFIRMED_RECHECK_SECONDS + 1
    worker.sweep(store, root, NOW)
    assert asked == [A, A] and not directory.exists()


def test_one_run_is_bounded_oldest_first(tmp_path):
    root = tmp_path / "gen"
    store = {"jobs": {}}
    for index in range(4):
        _directory, job = make_job(root, name=f"cpl-{index}", age=timedelta(hours=10 - index), extra_files=())
        store["jobs"][f"job-{index}"] = job
    worker, asked = sweeper(True)

    worker.sweep(store, root, NOW, probe_limit=2)

    assert len(asked) == 2
    assert sorted(path.name for path in (root / "acct:page" / "dossier-1").iterdir()) == ["cpl-2", "cpl-3"]


def test_a_failed_delete_is_counted_and_the_rest_continue(tmp_path, monkeypatch):
    root = tmp_path / "gen"
    first, first_job = make_job(root, name="a", extra_files=(), age=timedelta(hours=9))
    second, second_job = make_job(root, name="b", extra_files=())
    real = retention.Path.unlink

    def flaky(self, *args, **kwargs):
        if self == first / "renders/hailuo/c0.mp4":
            raise PermissionError("busy")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(retention.Path, "unlink", flaky)
    worker, _ = sweeper(True)
    summary = worker.sweep({"jobs": {"a": first_job, "b": second_job}}, root, NOW)

    assert summary["failed"] == 1 and summary["clip_files"] == 3
    assert (first / "renders/hailuo/c0.mp4").exists() and not second.exists()


def _clip_confirm(monkeypatch, handler, configured=True):
    for name, value in (("CONTENT_LAB_CONTROL_PLANE_ORIGIN", "https://control.example.test"),
                        ("CONTROL_PLANE_SERVICE_ID", "svc"), ("CONTROL_PLANE_SERVICE_SECRET", "secret")):
        if configured:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
    seen = []
    result = retention.confirm_admitted_clip("cd" * 32, 77, transport=httpx.MockTransport(
        lambda request: seen.append(request) or handler(request)))
    return result, seen


def test_clip_confirmation_asks_the_staging_video_door(monkeypatch):
    result, seen = _clip_confirm(monkeypatch, lambda _r: httpx.Response(200, content=b"x" * 77))
    assert result is True
    assert str(seen[0].url) == f"https://control.example.test/staging-video/{'cd' * 32}.mp4"
    assert seen[0].headers["cf-access-client-id"] == "svc"


@pytest.mark.parametrize("status,body,expected", [(200, b"x" * 76, False), (404, b"", False),
                                                  (503, b"", None), (302, b"", None)])
def test_clip_confirmation_is_true_only_for_the_exact_object(monkeypatch, status, body, expected):
    assert _clip_confirm(monkeypatch, lambda _r: httpx.Response(status, content=body))[0] is expected


def test_clip_confirmation_is_unknown_when_unconfigured_or_unreachable(monkeypatch):
    result, seen = _clip_confirm(monkeypatch, lambda _r: httpx.Response(200), configured=False)
    assert result is None and seen == []

    def down(_request):
        raise httpx.ConnectError("down")

    assert _clip_confirm(monkeypatch, down)[0] is None


def test_the_scheduler_tick_follows_the_pressure_interval(monkeypatch):
    from routers import control_plane
    pressure = ["normal"]
    runs = []
    monkeypatch.setattr(control_plane, "_generated_volume_pressure", lambda: pressure[0])
    monkeypatch.setattr(control_plane, "run_generated_media_retention_once",
                        lambda **kwargs: runs.append(kwargs["pressure"]))
    intervals = control_plane._MEDIA_RETENTION_INTERVAL_SECONDS

    last = control_plane._media_retention_tick(float("-inf"), 1000.0)
    assert control_plane._media_retention_tick(last, 1000.0 + intervals["tight"]) == last  # normal: too soon
    pressure[0] = "tight"
    last = control_plane._media_retention_tick(last, 1000.0 + intervals["tight"])
    pressure[0] = "floor"
    control_plane._media_retention_tick(last, last + intervals["floor"])

    assert runs == ["normal", "tight", "floor"]


def test_the_scheduled_pass_reads_the_live_snapshot_and_generation_root(tmp_path, monkeypatch):
    from routers import control_plane
    root = tmp_path / "gen"
    directory, job = make_job(root, age=timedelta(days=3))
    snapshot = type("Snapshot", (), {"data": {"jobs": {"j": job}}})()
    monkeypatch.setattr(control_plane, "_read_jobs_snapshot_object", lambda: snapshot)
    monkeypatch.setattr(control_plane, "_generation_root", lambda: root)
    monkeypatch.setattr(control_plane, "_MEDIA_RETENTION", sweeper(True)[0])

    summary = control_plane.run_generated_media_retention_once(NOW, pressure="normal")

    assert summary["clip_files"] == 2 and summary["stray_files"] == 1 and not directory.exists()
