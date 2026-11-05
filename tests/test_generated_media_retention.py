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


def test_a_404_clip_is_skipped_for_an_hour_then_probed_again(tmp_path):
    root = tmp_path / "gen"
    directory, job = make_job(root, extra_files=())
    clock = [0.0]
    answers = [False, True]
    asked = []
    worker = retention.GeneratedMediaRetention(
        confirm=lambda sha, size: asked.append(sha) or answers.pop(0), monotonic=lambda: clock[0])
    store = {"jobs": {"j": job}}

    worker.sweep(store, root, NOW)  # the 404 answer lands in the negative cache
    assert retention.NEGATIVE_CACHE_SECONDS == 3600  # one hour, not six
    clock[0] = 3599
    worker.sweep(store, root, NOW)
    assert asked == [A] and directory.exists()  # inside the hour: not probed again
    clock[0] = 3601
    worker.sweep(store, root, NOW)
    assert asked == [A, A] and not directory.exists()


def test_an_errored_probe_is_never_cached_and_retries_next_pass(tmp_path):
    root = tmp_path / "gen"
    directory, job = make_job(root, extra_files=())
    asked = []
    worker = retention.GeneratedMediaRetention(
        confirm=lambda sha, size: asked.append(sha) or None, monotonic=lambda: 0.0)
    store = {"jobs": {"j": job}}

    worker.sweep(store, root, NOW)
    worker.sweep(store, root, NOW)

    assert asked == [A, A]  # unknown is not a 404: the next pass asks again
    assert files(directory) == ["renders/hailuo/c0.mp4", "thumbnails/0000.jpg"]


def test_the_negative_cache_is_bounded_and_drops_the_oldest(tmp_path, monkeypatch):
    monkeypatch.setattr(retention, "NEGATIVE_CACHE_MAX_ENTRIES", 5000)
    root = tmp_path / "gen"
    store = {"jobs": {}}
    for index in range(5001):
        _directory, job = make_job(root, name=f"cpl-{index:05d}", extra_files=(),
                                   clips=((f"{index:064x}", "renders/hailuo/c0.mp4"),))
        store["jobs"][f"job-{index}"] = job
    worker, asked = sweeper(False)

    worker.sweep(store, root, NOW, probe_limit=5001)

    assert len(asked) == 5001
    assert len(worker._negative_cache) == 5000
    assert f"{0:064x}" not in worker._negative_cache  # the oldest entry dropped
    assert f"{1:064x}" in worker._negative_cache


def test_one_run_is_bounded_newest_first_plus_one_old_slot(tmp_path):
    root = tmp_path / "gen"
    store = {"jobs": {}}
    for index in range(4):
        _directory, job = make_job(root, name=f"cpl-{index}", age=timedelta(hours=10 - index), extra_files=())
        store["jobs"][f"job-{index}"] = job
    worker, asked = sweeper(True)

    worker.sweep(store, root, NOW, probe_limit=2)

    assert len(asked) == 2
    # cap 2 = 1 newest (cpl-3, 7 h old) + 1 old slot (cpl-0, 10 h old)
    assert sorted(path.name for path in (root / "acct:page" / "dossier-1").iterdir()) == ["cpl-1", "cpl-2"]


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
    assert control_plane._media_retention_tick(last, 1000.0 + intervals["tight"] - 1) == last  # too soon
    pressure[0] = "tight"
    last = control_plane._media_retention_tick(last, 1000.0 + intervals["tight"])
    pressure[0] = "floor"
    control_plane._media_retention_tick(last, last + intervals["floor"])

    assert runs == ["normal", "tight", "floor"]


def test_every_pressure_waits_ten_minutes_between_passes():
    from routers import control_plane

    assert control_plane._MEDIA_RETENTION_INTERVAL_SECONDS == {"normal": 600, "tight": 600, "floor": 600}


def test_the_probe_limit_map_is_applied_per_pressure():
    assert retention.PROBE_LIMITS == {"normal": 100, "tight": 60, "floor": 30}


@pytest.mark.parametrize("pressure,limit", [("normal", 100), ("tight", 60), ("floor", 30)])
def test_a_pass_without_an_explicit_probe_limit_uses_its_pressures_limit(tmp_path, pressure, limit):
    root = tmp_path / "gen"
    clips = tuple((f"{index:064x}", f"renders/hailuo/{index}.mp4") for index in range(130))
    _directory, job = make_job(root, clips=clips, extra_files=())
    worker, asked = sweeper(False)  # 404 every time; nothing may be deleted

    worker.sweep({"jobs": {"j": job}}, root, NOW, pressure=pressure)

    assert len(asked) == limit
    assert len(list(root.rglob("*.mp4"))) == 130  # an unprobed clip is still kept


def test_a_forced_floor_pass_uses_the_floor_probe_limit(tmp_path, monkeypatch):
    from routers import control_plane
    root = tmp_path / "gen"
    clips = tuple((f"{index:064x}", f"renders/hailuo/{index}.mp4") for index in range(40))
    _directory, job = make_job(root, clips=clips, extra_files=())
    snapshot = type("Snapshot", (), {"data": {"jobs": {"j": job}}})()
    worker, asked = sweeper(False)
    monkeypatch.setattr(control_plane, "_read_jobs_snapshot_object", lambda: snapshot)
    monkeypatch.setattr(control_plane, "_generation_root", lambda: root)
    monkeypatch.setattr(control_plane, "_MEDIA_RETENTION", worker)

    summary = control_plane.run_generated_media_retention_once(NOW, pressure="floor")

    assert len(asked) == retention.PROBE_LIMITS["floor"]  # the remedy's forced pass
    assert summary["unconfirmed_bytes_kept"] == 40 * 101


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


def test_source_import_attempt_group_keeps_unconfirmed_current_clip_and_cleans_old_strays(tmp_path):
    root = tmp_path / 'gen'
    job_id = 'cpl-' + 'a' * 16
    attempt = 'b' * 32
    group = root / 'acct:page' / 'source-imports' / job_id
    old = group / ('c' * 32) / 'source' / 'old.mp4'
    current = group / attempt / 'source' / 'source.mp4'
    thumbnail = group / attempt / 'thumbnails' / '0000.jpg'
    for path, content in [(old, b'old'), (current, b'fresh'), (thumbnail, b'jpg')]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    stamp = (NOW - timedelta(days=3)).isoformat()
    job = {'jobId': job_id, 'pageId': 'acct:page', 'sourceKind': 'page_source_import',
           'sourceImportClaimVersion': 1, 'sourceImportAttemptId': attempt,
           'artifactRoot': str(group / attempt), 'status': 'completed', 'completedAt': stamp,
           'clips': [{'path': 'source/source.mp4', 'sha256': A, 'bytes': 5,
                      'thumbnail': {'path': 'thumbnails/0000.jpg'}}]}
    worker, asked = sweeper(False)
    worker.sweep({'jobs': {job_id: job}}, root, NOW)
    assert asked == [(A, 5)]
    assert not old.exists(), 'the old attempt is a delayed stray'
    assert current.read_bytes() == b'fresh' and thumbnail.read_bytes() == b'jpg'
    confirmed, _ = sweeper(True)
    confirmed.sweep({'jobs': {job_id: job}}, root, NOW)
    assert not current.exists() and not thumbnail.exists()


def test_source_import_group_with_active_descendant_is_not_cleaned(tmp_path):
    root = tmp_path / 'gen'
    job_id = 'cpl-' + 'a' * 16
    attempt = 'b' * 32
    group = root / 'acct:page' / 'source-imports' / job_id
    stray = group / ('c' * 32) / 'old.bin'
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b'keep')
    current = group / attempt
    current.mkdir()
    stamp = (NOW - timedelta(days=3)).isoformat()
    terminal = {'jobId': job_id, 'pageId': 'acct:page', 'sourceKind': 'page_source_import',
                'sourceImportClaimVersion': 1, 'sourceImportAttemptId': attempt,
                'artifactRoot': str(current), 'status': 'failed', 'completedAt': stamp, 'clips': []}
    active = {'status': 'running', 'artifactRoot': str(current / 'still-active')}
    worker, _ = sweeper(True)
    worker.sweep({'jobs': {job_id: terminal, 'active': active}}, root, NOW)
    assert stray.read_bytes() == b'keep'


@pytest.mark.parametrize('malformed', ['boolean_version', 'wrong_attempt', 'parent_root'])
def test_malformed_source_attempt_metadata_never_broadens_retention_scope(tmp_path, malformed):
    root = tmp_path / 'gen'
    job_id = 'cpl-' + 'a' * 16
    attempt = 'b' * 32
    group = root / 'acct:page' / 'source-imports' / job_id
    current = group / attempt
    protected = current / 'source' / 'source.mp4'
    protected.parent.mkdir(parents=True)
    protected.write_bytes(b'keep')
    stamp = (NOW - timedelta(days=3)).isoformat()
    job = {'jobId': job_id, 'pageId': 'acct:page', 'sourceKind': 'page_source_import',
           'sourceImportClaimVersion': 1, 'sourceImportAttemptId': attempt,
           'artifactRoot': str(current), 'status': 'completed', 'completedAt': stamp, 'clips': []}
    if malformed == 'boolean_version':
        job['sourceImportClaimVersion'] = True
    elif malformed == 'wrong_attempt':
        job['sourceImportAttemptId'] = 'c' * 32
    else:
        job['artifactRoot'] = str(group)
    worker, _ = sweeper(True)
    worker.sweep({'jobs': {job_id: job}}, root, NOW)
    assert protected.read_bytes() == b'keep'

@pytest.mark.parametrize('retry_timing', ['before_plan', 'after_plan'])
def test_restartable_import_snapshot_never_deletes_a_new_retry(tmp_path, retry_timing):
    root = tmp_path / 'gen'
    job_id = 'cpl-' + 'a' * 16
    group = root / 'acct:page' / 'source-imports' / job_id
    old = group / ('b' * 32) / 'source' / 'old.mp4'
    fresh = group / ('c' * 32) / 'source' / 'fresh.mp4'
    old.parent.mkdir(parents=True)
    old.write_bytes(b'old')
    job = {'jobId': job_id, 'pageId': 'acct:page', 'sourceKind': 'page_source_import',
           'sourceImportClaimVersion': 1, 'sourceImportAttemptId': 'b' * 32,
           'artifactRoot': str(group / ('b' * 32)), 'status': 'failed',
           'error': 'source_import_runtime_restarted',
           'completedAt': (NOW - timedelta(days=3)).isoformat(), 'clips': []}
    worker, _ = sweeper(True)
    original_plan = worker.plan

    def create_retry():
        fresh.parent.mkdir(parents=True)
        fresh.write_bytes(b'fresh retry media')

    if retry_timing == 'before_plan':
        create_retry()
    else:
        def plan_then_retry(*args, **kwargs):
            plan = original_plan(*args, **kwargs)
            create_retry()
            return plan
        worker.plan = plan_then_retry
    worker.sweep({'jobs': {job_id: job}}, root, NOW)
    assert fresh.read_bytes() == b'fresh retry media'
    assert not old.exists()


def test_definitively_failed_import_cleans_old_attempts(tmp_path):
    root = tmp_path / 'gen'
    job_id = 'cpl-' + 'a' * 16
    group = root / 'acct:page' / 'source-imports' / job_id
    old = group / ('b' * 32) / 'old.mp4'
    current = group / ('c' * 32) / 'current.mp4'
    for path in (old, current):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'stray')
    job = {'jobId': job_id, 'pageId': 'acct:page', 'sourceKind': 'page_source_import',
           'sourceImportClaimVersion': 1, 'sourceImportAttemptId': 'c' * 32,
           'artifactRoot': str(group / ('c' * 32)), 'status': 'failed',
           'error': 'source_import_cancelled',
           'completedAt': (NOW - timedelta(days=3)).isoformat(), 'clips': []}
    worker, _ = sweeper(True)
    worker.sweep({'jobs': {job_id: job}}, root, NOW)
    assert not old.exists() and not current.exists()


# --- round 2: no starvation, growing back-off, errors and malformed ids back off, eviction order ---

GOOD = "f" * 64


def _clocked(confirm):
    clock = [0.0]
    return retention.GeneratedMediaRetention(confirm=confirm, monotonic=lambda: clock[0]), clock


def test_old_never_admitted_clips_cannot_starve_a_newer_admitted_clip_at_floor(tmp_path):
    root = tmp_path / "gen"
    store = {"jobs": {}}
    for index in range(200):  # 200 old clips the Worker never copied: 404 forever
        _d, job = make_job(root, name=f"bad{index:03d}", age=timedelta(hours=100 - index * 0.01),
                           clips=((f"{index:064x}", "renders/c.mp4"),), extra_files=())
        store["jobs"][f"bad{index:03d}"] = job
    good_dir, good_job = make_job(root, name="good", age=timedelta(hours=3),
                                  clips=((GOOD, "renders/c.mp4"),), extra_files=())
    store["jobs"]["good"] = good_job
    asked = []
    worker, clock = _clocked(lambda sha, size: asked.append(sha) or (sha == GOOD))

    deleted_at = None
    for pass_number in range(12):  # a floor pass every 600 s: 30 probes each
        worker.sweep(store, root, NOW, pressure="floor")
        if not (good_dir / "renders/c.mp4").exists():
            deleted_at = pass_number
            break
        clock[0] += 600

    assert deleted_at == 0  # the newest job is probed first, however many old 404s exist
    assert len(asked) == 30 and asked.index(GOOD) == 0
    assert len(list((root).rglob("c.mp4"))) == 200  # every 404 clip is still kept


def test_a_404_backs_off_one_two_four_hours_up_to_a_day(tmp_path):
    root = tmp_path / "gen"
    _d, job = make_job(root, extra_files=())
    asked = []
    worker, clock = _clocked(lambda sha, size: asked.append(sha) or False)
    store = {"jobs": {"j": job}}
    waits = []
    for _ in range(8):
        worker.sweep(store, root, NOW)
        waits.append(worker._negative_cache[A] - clock[0])
        clock[0] = worker._negative_cache[A] - 1
        worker.sweep(store, root, NOW)  # one second early: not probed
        clock[0] += 1
    assert waits == [3600, 7200, 14400, 28800, 57600, 86400, 86400, 86400]
    assert len(asked) == 8
    worker.sweep(store, root, NOW)
    assert len(asked) == 9


def test_a_wrong_length_200_counts_as_not_confirmed_and_backs_off(tmp_path, monkeypatch):
    root = tmp_path / "gen"
    directory, job = make_job(root, extra_files=())
    monkeypatch.setenv("CONTENT_LAB_CONTROL_PLANE_ORIGIN", "https://cp.example")
    monkeypatch.setenv("CONTROL_PLANE_SERVICE_ID", "id")
    monkeypatch.setenv("CONTROL_PLANE_SERVICE_SECRET", "secret")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 99))
    worker, clock = _clocked(lambda sha, size: retention.confirm_admitted_clip(sha, size, transport=transport))

    worker.sweep({"jobs": {"j": job}}, root, NOW)

    assert files(directory) == ["renders/hailuo/c0.mp4", "thumbnails/0000.jpg"]  # never deleted
    assert worker._negative_cache[A] == 3600


def test_an_erroring_clip_backs_off_after_the_first_retry_and_cannot_pin_the_budget(tmp_path):
    root = tmp_path / "gen"
    store = {"jobs": {}}
    for index in range(30):  # 30 older clips whose probe keeps erroring
        _d, job = make_job(root, name=f"err{index:02d}", age=timedelta(hours=100 - index * 0.01),
                           clips=((f"{index:064x}", "renders/c.mp4"),), extra_files=())
        store["jobs"][f"err{index:02d}"] = job
    good_dir, good_job = make_job(root, name="good", age=timedelta(hours=3),
                                  clips=((GOOD, "renders/c.mp4"),), extra_files=())
    store["jobs"]["good"] = good_job
    worker, clock = _clocked(lambda sha, size: True if sha == GOOD else None)

    worker.sweep(store, root, NOW, pressure="floor")  # pass 0: the newest clip goes first, errors fill the rest
    assert not (good_dir / "renders/c.mp4").exists()
    assert worker._negative_cache[f"{0:064x}"] == 0  # first error: retry at once
    clock[0] += 600
    worker.sweep(store, root, NOW, pressure="floor")  # pass 1: the errors are probed again
    assert worker._negative_cache[f"{0:064x}"] == 600 + 600  # second error: 10 min back-off
    waits = []
    for _ in range(4):
        clock[0] = worker._negative_cache[f"{0:064x}"]
        worker.sweep(store, root, NOW, pressure="floor")
        waits.append(worker._negative_cache[f"{0:064x}"] - clock[0])
    assert waits == [1200, 2400, 3600, 3600]  # capped at an hour


def test_a_malformed_clip_record_is_held_a_day_and_spends_no_probe(tmp_path):
    root = tmp_path / "gen"
    store = {"jobs": {}}
    _d, short = make_job(root, name="short", age=timedelta(hours=50), clips=(("abc", "renders/c.mp4"),),
                         extra_files=())
    _d, bad_size = make_job(root, name="size", age=timedelta(hours=49), clips=((B, "renders/c.mp4"),),
                            extra_files=())
    bad_size["clips"][0]["bytes"] = "100"
    good_dir, good_job = make_job(root, name="good", age=timedelta(hours=3),
                                  clips=((GOOD, "renders/c.mp4"),), extra_files=())
    store["jobs"] = {"short": short, "size": bad_size, "good": good_job}
    asked = []
    worker, clock = _clocked(lambda sha, size: asked.append(sha) or True)

    worker.sweep(store, root, NOW, probe_limit=1)  # one probe is enough: malformed ones cost none

    assert asked == [GOOD] and not (good_dir / "renders/c.mp4").exists()
    held = sorted(worker._negative_cache.values())
    assert held == [86400, 86400]
    assert len(list(root.rglob("c.mp4"))) == 2  # the malformed clips are kept
    clock[0] = 86399
    worker.sweep(store, root, NOW, probe_limit=1)
    assert asked == [GOOD]


def test_eviction_drops_the_entry_that_expires_first_not_the_one_inserted_first(monkeypatch):
    monkeypatch.setattr(retention, "NEGATIVE_CACHE_MAX_ENTRIES", 3)
    worker, clock = _clocked(lambda sha, size: False)
    for when, key in ((0, "k1"), (10, "k2"), (20, "k3")):
        clock[0] = when
        worker._record_failure(key, "missing")
    clock[0] = 4000
    worker._record_failure("k1", "missing")  # refreshed: now expires at 4000 + 7200, still first in the dict
    clock[0] = 4010
    worker._record_failure("k4", "missing")  # over the bound: k2 (expires 3610) goes, not k1

    assert sorted(worker._negative_cache) == ["k1", "k3", "k4"]
    assert sorted(worker._probe_meta) == ["k1", "k3", "k4"]
    assert worker._negative_cache["k1"] == 11200


def test_the_cache_survives_threads_hammering_it_at_the_bound(monkeypatch):
    import threading
    monkeypatch.setattr(retention, "NEGATIVE_CACHE_MAX_ENTRIES", 50)
    worker, _clock = _clocked(lambda sha, size: False)
    errors = []

    def hammer(offset):
        try:
            for index in range(500):
                worker._record_failure(f"{offset}-{index}", "missing")
        except Exception as exc:  # pragma: no cover - the failure being tested
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(worker._negative_cache) == 50 and set(worker._probe_meta) == set(worker._negative_cache)


def _bad_jobs(root, count, *, oldest_hours=500):
    jobs = {}
    for index in range(count):  # old clips the Worker never copied: 404 forever
        _d, job = make_job(root, name=f"bad{index:05d}", age=timedelta(hours=oldest_hours - index * 0.001),
                           clips=((f"{index:064x}", "renders/c.mp4"),), extra_files=())
        jobs[f"bad{index:05d}"] = job
    return jobs


@pytest.mark.parametrize("bad, bound", [(200, 200_000), (1000, 200_000), (180, 50)])
def test_the_newest_admitted_clip_is_freed_on_the_first_floor_pass_whatever_the_bad_population(
        tmp_path, monkeypatch, bad, bound):
    # bound 50 + cap 30 + 100 = 180 distinct failing shas: far past the cache bound
    monkeypatch.setattr(retention, "NEGATIVE_CACHE_MAX_ENTRIES", bound)
    root = tmp_path / "gen"
    store = {"jobs": _bad_jobs(root, bad)}
    good_dir, good_job = make_job(root, name="good", age=timedelta(hours=3),
                                  clips=((GOOD, "renders/c.mp4"),), extra_files=())
    store["jobs"]["good"] = good_job
    worker, clock = _clocked(lambda sha, size: sha == GOOD)

    for _pass in range(2):
        worker.sweep(store, root, NOW, pressure="floor")
        if not (good_dir / "renders/c.mp4").exists():
            break
        clock[0] += 600

    assert _pass == 0 and not (good_dir / "renders/c.mp4").exists()
    assert len(list(root.rglob("c.mp4"))) == bad  # every 404 clip is kept


def test_a_fresh_admitted_clip_every_pass_is_freed_the_same_pass_past_the_cache_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(retention, "NEGATIVE_CACHE_MAX_ENTRIES", 50)
    root = tmp_path / "gen"
    store = {"jobs": _bad_jobs(root, 400)}
    worker, clock = _clocked(lambda sha, size: sha.startswith("f"))
    for number in range(12):
        sha = "f" * 63 + f"{number:x}"
        directory, job = make_job(root, name=f"new{number:02d}", age=timedelta(hours=3) - timedelta(minutes=number),
                                  clips=((sha, "renders/c.mp4"),), extra_files=())
        store["jobs"][f"new{number:02d}"] = job
        worker.sweep(store, root, NOW, pressure="floor")
        assert not (directory / "renders/c.mp4").exists(), number
        clock[0] += 600
    assert len(worker._negative_cache) <= 50


def test_a_restart_with_empty_state_still_frees_the_newest_clip_first(tmp_path):
    root = tmp_path / "gen"
    store = {"jobs": _bad_jobs(root, 1000)}
    for number in range(3):  # every pass is a brand-new process: no back-off, no probe history
        sha = "f" * 63 + str(number)
        directory, job = make_job(root, name=f"new{number}", age=timedelta(hours=3), clips=((sha, "renders/c.mp4"),),
                                  extra_files=())
        store["jobs"][f"new{number}"] = job
        worker, _clock = _clocked(lambda s, size: s.startswith("f"))
        worker.sweep(store, root, NOW, pressure="floor")
        assert not (directory / "renders/c.mp4").exists(), number


def test_a_pass_gives_a_fifth_of_the_cap_to_the_oldest_unprobed_clips_and_the_rest_to_the_newest(tmp_path):
    root = tmp_path / "gen"
    store = {"jobs": {}}
    for index in range(100):  # all R2-confirmed; index 0 is the oldest job
        _d, job = make_job(root, name=f"j{index:03d}", age=timedelta(hours=200 - index),
                           clips=((f"{index:064x}", "renders/c.mp4"),), extra_files=())
        store["jobs"][f"j{index:03d}"] = job
    worker, asked = sweeper(True)

    worker.sweep(store, root, NOW, pressure="floor")  # cap 30 = 24 newest + 6 oldest

    freed = sorted(int(sha, 16) for sha, _size in asked)
    assert freed == [0, 1, 2, 3, 4, 5] + list(range(76, 100))
    assert sorted(path.name for path in (root / "acct:page" / "dossier-1").iterdir()) == [
        f"j{index:03d}" for index in range(6, 76)]


def test_the_old_share_rotates_so_an_old_admitted_clip_frees_behind_newer_permanent_404s(tmp_path):
    root = tmp_path / "gen"
    store = {"jobs": _bad_jobs(root, 120, oldest_hours=400)}  # 400 h down to ~400 h: the OLD bad clips
    old_dir, old_job = make_job(root, name="oldgood", age=timedelta(hours=399.9), clips=((GOOD, "renders/c.mp4"),),
                                extra_files=())
    store["jobs"]["oldgood"] = old_job
    for index in range(300):  # 300 NEWER permanent 404s hog the newest share
        _d, job = make_job(root, name=f"new{index:04d}", age=timedelta(hours=100 - index * 0.01),
                           clips=((f"{index + 5000:064x}", "renders/c.mp4"),), extra_files=())
        store["jobs"][f"new{index:04d}"] = job
    worker, clock = _clocked(lambda sha, size: sha == GOOD)

    deleted_at = None
    for number in range(40):
        worker.sweep(store, root, NOW, pressure="floor")
        if not (old_dir / "renders/c.mp4").exists():
            deleted_at = number
            break
        clock[0] += 600

    # 6 old slots a pass walk the oldest never-probed shas first (101 bad ones are older than this clip)
    assert deleted_at == 16  # the old good clip is the 102nd oldest: 101 // 6


def test_a_pass_stops_probing_when_the_time_budget_is_spent(tmp_path):
    root = tmp_path / "gen"
    store = {"jobs": _bad_jobs(root, 10)}
    clock = [0.0]
    asked = []

    def slow(sha, size):
        asked.append(sha)
        clock[0] += 50  # each probe takes 50 s
        return False

    worker = retention.GeneratedMediaRetention(confirm=slow, monotonic=lambda: clock[0])

    plan = worker.plan(store, root, NOW, pressure="normal", time_budget=120)

    assert len(asked) == 3 and plan.probes == 3  # started at 0, 50, 100 s; at 150 s the budget is spent
    assert plan.unconfirmed_bytes == 10 * 101


def test_a_back_off_entry_is_dropped_when_its_clip_file_is_gone(tmp_path):
    root = tmp_path / "gen"
    directory, job = make_job(root, extra_files=())
    store = {"jobs": {"j": job}}
    worker, _clock = _clocked(lambda sha, size: False)
    worker.sweep(store, root, NOW)
    assert list(worker._negative_cache) == [A] and list(worker._probe_meta) == [A]

    for path in files(directory):
        (directory / path).unlink()
    worker.sweep(store, root, NOW)

    assert worker._negative_cache == {} and worker._probe_meta == {}


def test_the_cache_bound_is_a_far_away_safety_net():
    assert retention.NEGATIVE_CACHE_MAX_ENTRIES == 200_000


def test_the_lock_is_never_held_while_a_probe_is_in_flight(tmp_path):
    root = tmp_path / "gen"
    _d, job = make_job(root, extra_files=())
    held = []

    def probe(sha, size):
        free = worker._lock.acquire(blocking=False)
        held.append(not free)
        if free:
            worker._lock.release()
        return False

    worker = retention.GeneratedMediaRetention(confirm=probe, monotonic=lambda: 0.0)
    worker.sweep({"jobs": {"j": job}}, root, NOW)

    assert held == [False]

