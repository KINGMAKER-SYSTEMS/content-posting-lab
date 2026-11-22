"""Files of jobs that compaction moved out of the store leave the volume; everything still readable stays."""
import json

import pytest

from services import archived_media_retention as retention

JOB, OTHER, TRUCK, IMPORT = "cpl-" + "1" * 16, "cpl-" + "2" * 16, "cpl-" + "3" * 16, "cpl-" + "4" * 16


def make(tmp_path):
    root = tmp_path / "control_plane_generated"
    root.mkdir()
    return root, tmp_path / "control_plane_jobs.json"


def job_dir(root, job_id, *, group="dossier-1", files=("renders/c0.mp4", "thumbnails/0000.jpg")):
    directory = root / "acct:page" / group / job_id
    for rel in files:
        (directory / rel).parent.mkdir(parents=True, exist_ok=True)
        (directory / rel).write_bytes(b"x" * 100)
    return directory


def archive(store_path, *records, month="202610", tail=""):
    with open(f"{store_path}.archive-{month}.jsonl", "a") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
        handle.write(tail)


def record(job_id, directory, **fields):
    return {"jobId": job_id, "status": "completed", "sourceKind": "generated", "artifactRoot": str(directory), **fields}


def live(jobs=None, candidates=()):
    return {"jobs": dict(jobs or {}), "archiveIndex": {"version": 1, "truckCandidateJobs": list(candidates)}}


def job_id(n):
    return "cpl-" + f"{n:016x}"


def sweep(store, root, store_path, **kwargs):
    return retention.ArchivedMediaRetention().sweep(store, root, store_path, **kwargs)


def test_archived_job_files_are_deleted_and_the_empty_job_directory_removed(tmp_path):
    root, store_path = make(tmp_path)
    directory = job_dir(root, JOB)
    archive(store_path, record(JOB, directory))
    summary = sweep(live(), root, store_path)
    assert summary == {"dirs": 1, "files": 2, "bytes_freed": 200, "failed": 0, "held": 0}
    assert not directory.exists() and directory.parent.is_dir()


def test_dry_run_counts_and_deletes_nothing(tmp_path):
    root, store_path = make(tmp_path)
    directory = job_dir(root, JOB)
    archive(store_path, record(JOB, directory))
    assert sweep(live(), root, store_path, dry_run=True)["bytes_freed"] == 200
    assert (directory / "renders/c0.mp4").is_file()


def test_a_job_still_in_the_live_store_is_never_touched(tmp_path):
    root, store_path = make(tmp_path)
    directory = job_dir(root, JOB)
    archive(store_path, record(JOB, directory))  # crash between archive append and store commit
    elsewhere = root / "acct:page" / "moved" / JOB      # so only the job id, not a held path, protects it
    summary = sweep(live({JOB: record(JOB, elsewhere)}), root, store_path)
    assert summary["files"] == 0 and summary["held"] == 0
    assert (directory / "renders/c0.mp4").is_file()


def test_a_directory_no_archive_record_names_is_never_touched(tmp_path):
    root, store_path = make(tmp_path)
    unknown = job_dir(root, OTHER)
    archive(store_path, record(JOB, job_dir(root, JOB)))
    sweep(live(), root, store_path)
    assert (unknown / "renders/c0.mp4").is_file()


def test_truck_candidates_and_live_recovery_masters_hold_their_directory(tmp_path):
    root, store_path = make(tmp_path)
    truck, master = job_dir(root, TRUCK), job_dir(root, JOB)
    archive(store_path, record(TRUCK, truck), record(JOB, master))
    store = live({OTHER: {"jobId": OTHER, "status": "running", "artifactRoot": str(root / "acct:page" / "g" / OTHER),
                          "recoveryMasters": [{"artifactRoot": str(master)}]}},
                 [{"jobId": TRUCK, "artifactRoot": str(truck)}])
    summary = sweep(store, root, store_path)
    assert summary["files"] == 0 and summary["held"] == 2
    assert (truck / "renders/c0.mp4").is_file() and (master / "renders/c0.mp4").is_file()


def test_a_directory_containing_or_inside_a_live_pointer_is_held(tmp_path):
    root, store_path = make(tmp_path)
    contains, inside = job_dir(root, job_id(1)), job_dir(root, job_id(2), group="g2")
    archive(store_path, record(job_id(1), contains), record(job_id(2), inside))
    store = live({job_id(3): {"artifactRoot": str(contains / "renders")},       # a live pointer inside the directory
                  job_id(4): {"artifactRoot": str(inside.parent)}})              # a live pointer that contains it
    summary = sweep(store, root, store_path)
    assert summary["files"] == 0 and summary["held"] == 2
    assert (contains / "renders/c0.mp4").is_file() and (inside / "renders/c0.mp4").is_file()


BAD_STORES = {
    "index missing": {"jobs": {}},
    "index null": {"jobs": {}, "archiveIndex": None},
    "index version missing": {"jobs": {}, "archiveIndex": {"truckCandidateJobs": []}},
    "index version 2": {"jobs": {}, "archiveIndex": {"version": 2, "truckCandidateJobs": []}},
    "candidates missing": {"jobs": {}, "archiveIndex": {"version": 1}},
    "candidates not a list": {"jobs": {}, "archiveIndex": {"version": 1, "truckCandidateJobs": "x"}},
    "candidate not a dict": live(candidates=["x"]),
    "candidate without a root": live(candidates=[{"jobId": TRUCK}]),
    "live pointer not a string": live({OTHER: {"artifactRoot": 7}}),
    "live pointer with a NUL": live({OTHER: {"artifactRoot": "/tmp/a\x00b"}}),
    "recovery masters not a list": live({OTHER: {"recoveryMasters": "x"}}),
    "recovery master not a dict": live({OTHER: {"recoveryMasters": ["x"]}}),
    "job not a dict": live({OTHER: "x"}),
    "jobs not a dict": {"jobs": [], "archiveIndex": {"version": 1, "truckCandidateJobs": []}},
    "store not a dict": None,
}


@pytest.mark.parametrize("name", sorted(BAD_STORES))
def test_unknown_history_or_a_malformed_pointer_holds_everything(tmp_path, name):
    root, store_path = make(tmp_path)
    directory = job_dir(root, JOB)
    archive(store_path, record(JOB, directory))
    assert sweep(BAD_STORES[name], root, store_path) == {"dirs": 0, "files": 0, "bytes_freed": 0, "failed": 0, "held": 0}
    assert (directory / "renders/c0.mp4").is_file()


def test_only_the_jobs_own_directory_qualifies(tmp_path):
    root, store_path = make(tmp_path)
    outside = tmp_path / "elsewhere" / "g" / job_id(1)
    outside.mkdir(parents=True)
    (outside / "keep.mp4").write_bytes(b"x")
    shared = job_dir(root, job_id(20))                   # a directory named after ANOTHER job
    group = shared.parent                                # a whole group
    real = job_dir(root, job_id(4), group="real")
    link = root / "acct:page" / "alias"
    link.mkdir()
    (link / job_id(4)).symlink_to(real, target_is_directory=True)   # reached through a symlink
    dotted = job_dir(root, job_id(5), group="dots")
    shallow = root / "acct:page" / job_id(9)              # named after the job, one level too high
    deep = root / "acct:page" / "g" / "extra" / "more" / job_id(10)   # and two levels too low
    for wrong in (shallow, deep):
        wrong.mkdir(parents=True)
        (wrong / "keep.mp4").write_bytes(b"x")
    archive(store_path,
            record(job_id(1), outside), record(job_id(2), shared), record(job_id(3), group),
            record(job_id(4), link / job_id(4)), record(job_id(5), str(dotted.parent / ".." / "dots" / job_id(5))),
            record(job_id(6), "relative/g/" + job_id(6)), record("not-a-job-id", shared),
            {"jobId": job_id(7)}, record(job_id(8), 5), record(job_id(9), shallow), record(job_id(10), deep))
    assert sweep(live(), root, store_path) == {"dirs": 0, "files": 0, "bytes_freed": 0, "failed": 0, "held": 0}
    for kept in (outside / "keep.mp4", shared / "renders/c0.mp4", real / "renders/c0.mp4", dotted / "renders/c0.mp4",
                 shallow / "keep.mp4", deep / "keep.mp4"):
        assert kept.is_file()


def test_a_directory_that_cannot_be_emptied_does_not_starve_later_passes(tmp_path):
    root, store_path = make(tmp_path)
    keep = tmp_path / "keep.mp4"
    keep.write_bytes(b"k")
    stuck = [job_dir(root, job_id(n), files=()) for n in (1, 2)]
    for directory in stuck:
        directory.mkdir(parents=True)
        (directory / "link.mp4").symlink_to(keep)
    free = job_dir(root, job_id(3))
    archive(store_path, *[record(job_id(n + 1), d) for n, d in enumerate(stuck + [free])])
    sweeper = retention.ArchivedMediaRetention()
    assert sweeper.sweep(live(), root, store_path, dir_limit=2)["files"] == 0
    assert sweeper.sweep(live(), root, store_path, dir_limit=2) == {"dirs": 1, "files": 2, "bytes_freed": 200, "failed": 0, "held": 0}
    assert not free.exists() and all((d / "link.mp4").is_symlink() for d in stuck)


def test_time_running_out_inside_a_directory_stops_the_pass_and_the_next_one_finishes(tmp_path):
    root, store_path = make(tmp_path)
    directory = job_dir(root, JOB, files=tuple(f"renders/c{n}.mp4" for n in range(6)))
    archive(store_path, record(JOB, directory))
    clock = iter(range(1000))
    sweeper = retention.ArchivedMediaRetention(monotonic=lambda: next(clock))
    first = sweeper.sweep(live(), root, store_path, time_budget=7)
    assert 0 < first["files"] < 6 and directory.is_dir()
    assert sweeper.sweep(live(), root, store_path, time_budget=500)["files"] == 6 - first["files"]
    assert not directory.exists()


def test_source_import_attempt_directory_and_symlinks_inside_a_job(tmp_path):
    root, store_path = make(tmp_path)
    keep = tmp_path / "keep.mp4"
    keep.write_bytes(b"k")
    attempt = root / "acct:page" / "source-imports" / IMPORT / ("a" * 32)
    (attempt / "source").mkdir(parents=True)
    (attempt / "source" / "video.mp4").write_bytes(b"x" * 10)
    (attempt / "source" / "link.mp4").symlink_to(keep)
    archive(store_path, record(IMPORT, attempt, sourceKind="page_source_import"))
    summary = sweep(live(), root, store_path)
    assert summary["files"] == 1 and summary["bytes_freed"] == 10
    assert keep.read_bytes() == b"k" and (attempt / "source" / "link.mp4").is_symlink()


def test_dir_limit_bounds_a_pass_and_later_passes_finish(tmp_path):
    root, store_path = make(tmp_path)
    archive(store_path, *[record(job_id(n), job_dir(root, job_id(n))) for n in range(5)])
    sweeper = retention.ArchivedMediaRetention()
    assert sweeper.sweep(live(), root, store_path, dir_limit=2)["dirs"] == 2
    assert sweeper.sweep(live(), root, store_path, dir_limit=2)["dirs"] == 2
    assert sweeper.sweep(live(), root, store_path, dir_limit=2)["dirs"] == 1
    assert sweeper.sweep(live(), root, store_path, dir_limit=2)["dirs"] == 0


def test_expired_time_budget_deletes_nothing(tmp_path):
    root, store_path = make(tmp_path)
    directory = job_dir(root, JOB)
    archive(store_path, record(JOB, directory))
    assert sweep(live(), root, store_path, time_budget=0)["files"] == 0
    assert (directory / "renders/c0.mp4").is_file()


def test_archive_is_read_incrementally_and_a_torn_final_frame_waits(tmp_path):
    root, store_path = make(tmp_path)
    first, second = job_dir(root, JOB), job_dir(root, OTHER)
    whole = json.dumps(record(OTHER, second))
    archive(store_path, record(JOB, first), tail=whole[:40])       # second record still being written
    sweeper = retention.ArchivedMediaRetention()
    assert sweeper.sweep(live(), root, store_path)["dirs"] == 1
    assert (second / "renders/c0.mp4").is_file()
    archive(store_path, tail=whole[40:] + "\n")
    assert sweeper.sweep(live(), root, store_path)["dirs"] == 1
    assert not second.exists()


def test_loop_switches(tmp_path, monkeypatch):
    import routers.control_plane as cp
    root, store_path = make(tmp_path)
    directory = job_dir(root, JOB)
    monkeypatch.setattr(cp, "_jobs_path", lambda: store_path)
    monkeypatch.setattr(cp, "_generation_root", lambda: root)
    monkeypatch.setattr(cp, "_read_jobs_snapshot_object", lambda: type("S", (), {"data": live()})())
    monkeypatch.setattr(cp, "_ARCHIVED_MEDIA_RETENTION", retention.ArchivedMediaRetention())
    assert cp.run_archived_media_retention_once() == {}            # no archive file: nothing is even read
    archive(store_path, record(JOB, directory))
    monkeypatch.setenv("CONTENT_LAB_ARCHIVED_MEDIA_RETENTION", " OFF ")
    assert cp.run_archived_media_retention_once() == {}
    assert (directory / "renders/c0.mp4").is_file()
    monkeypatch.setenv("CONTENT_LAB_ARCHIVED_MEDIA_RETENTION", "Dry-Run")
    assert cp.run_archived_media_retention_once()["bytes_freed"] == 200
    assert (directory / "renders/c0.mp4").is_file()
    monkeypatch.delenv("CONTENT_LAB_ARCHIVED_MEDIA_RETENTION")
    monkeypatch.setattr(cp, "_generated_volume_pressure", lambda: "normal")
    monkeypatch.setattr(cp, "run_generated_media_retention_once", lambda **kw: {})
    monkeypatch.setenv("CONTENT_LAB_MEDIA_RETENTION", "off")           # the main switch stops this pass too
    cp._media_retention_tick(float("-inf"), 10_000.0)
    assert (directory / "renders/c0.mp4").is_file()
    monkeypatch.delenv("CONTENT_LAB_MEDIA_RETENTION")
    cp._media_retention_tick(float("-inf"), 10_000.0)                  # and the tick runs it when nothing is off
    assert not directory.exists()
