"""Files of jobs that compaction moved out of the store leave the volume; everything still readable stays."""
import json

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


def sweep(store, root, store_path, **kwargs):
    return retention.ArchivedMediaRetention().sweep(store, root, store_path, **kwargs)


def test_archived_job_files_are_deleted_and_the_empty_job_directory_removed(tmp_path):
    root, store_path = make(tmp_path)
    directory = job_dir(root, JOB)
    archive(store_path, record(JOB, directory))
    summary = sweep({"jobs": {}}, root, store_path)
    assert summary == {"dirs": 1, "files": 2, "bytes_freed": 200, "failed": 0, "held": 0}
    assert not directory.exists() and directory.parent.is_dir()


def test_dry_run_counts_and_deletes_nothing(tmp_path):
    root, store_path = make(tmp_path)
    directory = job_dir(root, JOB)
    archive(store_path, record(JOB, directory))
    assert sweep({"jobs": {}}, root, store_path, dry_run=True)["bytes_freed"] == 200
    assert (directory / "renders/c0.mp4").is_file()


def test_a_job_still_in_the_live_store_is_never_touched(tmp_path):
    root, store_path = make(tmp_path)
    directory = job_dir(root, JOB)
    archive(store_path, record(JOB, directory))  # crash between archive append and store commit
    store = {"jobs": {JOB: record(JOB, directory)}}
    assert sweep(store, root, store_path)["files"] == 0
    assert (directory / "renders/c0.mp4").is_file()


def test_a_directory_no_archive_record_names_is_never_touched(tmp_path):
    root, store_path = make(tmp_path)
    unknown = job_dir(root, OTHER)
    archive(store_path, record(JOB, job_dir(root, JOB)))
    sweep({"jobs": {}}, root, store_path)
    assert (unknown / "renders/c0.mp4").is_file()


def test_truck_candidates_and_live_recovery_masters_hold_their_directory(tmp_path):
    root, store_path = make(tmp_path)
    truck, master = job_dir(root, TRUCK), job_dir(root, JOB)
    archive(store_path, record(TRUCK, truck), record(JOB, master))
    store = {
        "jobs": {OTHER: {"jobId": OTHER, "status": "running", "artifactRoot": str(root / "acct:page" / "g" / OTHER),
                         "recoveryMasters": [{"artifactRoot": str(master)}]}},
        "archiveIndex": {"version": 1, "truckCandidateJobs": [{"jobId": TRUCK, "artifactRoot": str(truck)}]},
    }
    summary = sweep(store, root, store_path)
    assert summary["files"] == 0 and summary["held"] == 2
    assert (truck / "renders/c0.mp4").is_file() and (master / "renders/c0.mp4").is_file()


def test_unreadable_index_or_malformed_live_pointer_holds_everything(tmp_path):
    root, store_path = make(tmp_path)
    directory = job_dir(root, JOB)
    archive(store_path, record(JOB, directory))
    for store in ({"jobs": {}, "archiveIndex": None},
                  {"jobs": {}, "archiveIndex": {"truckCandidateJobs": "x"}},
                  {"jobs": {OTHER: {"artifactRoot": 7}}},
                  {"jobs": {OTHER: {"recoveryMasters": "x"}}},
                  {"jobs": {OTHER: "x"}}, {"jobs": []}, None):
        assert sweep(store, root, store_path)["files"] == 0
    assert (directory / "renders/c0.mp4").is_file()


def test_only_the_jobs_own_directory_qualifies(tmp_path):
    root, store_path = make(tmp_path)
    outside = tmp_path / "elsewhere" / "g" / JOB
    outside.mkdir(parents=True)
    (outside / "keep.mp4").write_bytes(b"x")
    shared = job_dir(root, OTHER)                      # record points at another job's directory
    group = root / "acct:page" / "dossier-1"            # record points at the whole group
    real = job_dir(root, TRUCK, group="real")
    link = root / "acct:page" / "alias"
    link.mkdir()
    (link / JOB).symlink_to(real, target_is_directory=True)  # record reaches a directory through a symlink
    archive(store_path, record(JOB, outside), record(JOB, shared), record(JOB, group),
            record(JOB, link / JOB), record(JOB, "relative/g/" + JOB), record("not-a-job-id", shared),
            {"jobId": JOB}, record(JOB, 5))
    assert sweep({"jobs": {}}, root, store_path)["files"] == 0
    assert (outside / "keep.mp4").is_file() and (shared / "renders/c0.mp4").is_file()
    assert (real / "renders/c0.mp4").is_file()


def test_source_import_attempt_directory_and_symlinks_inside_a_job(tmp_path):
    root, store_path = make(tmp_path)
    keep = tmp_path / "keep.mp4"
    keep.write_bytes(b"k")
    attempt = root / "acct:page" / "source-imports" / IMPORT / ("a" * 32)
    (attempt / "source").mkdir(parents=True)
    (attempt / "source" / "video.mp4").write_bytes(b"x" * 10)
    (attempt / "source" / "link.mp4").symlink_to(keep)
    archive(store_path, record(IMPORT, attempt, sourceKind="page_source_import"))
    summary = sweep({"jobs": {}}, root, store_path)
    assert summary["files"] == 1 and summary["bytes_freed"] == 10
    assert keep.read_bytes() == b"k" and (attempt / "source" / "link.mp4").is_symlink()


def test_dir_limit_bounds_a_pass_and_later_passes_finish(tmp_path):
    root, store_path = make(tmp_path)
    ids = ["cpl-" + f"{n:016x}" for n in range(5)]
    archive(store_path, *[record(job_id, job_dir(root, job_id)) for job_id in ids])
    sweeper = retention.ArchivedMediaRetention()
    assert sweeper.sweep({"jobs": {}}, root, store_path, dir_limit=2)["dirs"] == 2
    assert sweeper.sweep({"jobs": {}}, root, store_path, dir_limit=2)["dirs"] == 2
    assert sweeper.sweep({"jobs": {}}, root, store_path, dir_limit=2)["dirs"] == 1
    assert sweeper.sweep({"jobs": {}}, root, store_path, dir_limit=2)["dirs"] == 0


def test_expired_time_budget_deletes_nothing(tmp_path):
    root, store_path = make(tmp_path)
    directory = job_dir(root, JOB)
    archive(store_path, record(JOB, directory))
    assert sweep({"jobs": {}}, root, store_path, time_budget=0)["files"] == 0
    assert (directory / "renders/c0.mp4").is_file()


def test_archive_is_read_incrementally_and_a_torn_final_frame_waits(tmp_path):
    root, store_path = make(tmp_path)
    first, second = job_dir(root, JOB), job_dir(root, OTHER)
    whole = json.dumps(record(OTHER, second))
    archive(store_path, record(JOB, first), tail=whole[:40])       # second record still being written
    sweeper = retention.ArchivedMediaRetention()
    assert sweeper.sweep({"jobs": {}}, root, store_path)["dirs"] == 1
    assert (second / "renders/c0.mp4").is_file()
    archive(store_path, tail=whole[40:] + "\n")
    assert sweeper.sweep({"jobs": {}}, root, store_path)["dirs"] == 1
    assert not second.exists()


def test_loop_switches(tmp_path, monkeypatch):
    import routers.control_plane as cp
    root, store_path = make(tmp_path)
    directory = job_dir(root, JOB)
    monkeypatch.setattr(cp, "_jobs_path", lambda: store_path)
    monkeypatch.setattr(cp, "_generation_root", lambda: root)
    monkeypatch.setattr(cp, "_read_jobs_snapshot_object", lambda: type("S", (), {"data": {"jobs": {}}})())
    monkeypatch.setattr(cp, "_ARCHIVED_MEDIA_RETENTION", retention.ArchivedMediaRetention())
    assert cp.run_archived_media_retention_once() == {}            # no archive file: nothing is even read
    archive(store_path, record(JOB, directory))
    monkeypatch.setenv("CONTENT_LAB_ARCHIVED_MEDIA_RETENTION", " OFF ")
    assert cp.run_archived_media_retention_once() == {}
    assert (directory / "renders/c0.mp4").is_file()
    monkeypatch.delenv("CONTENT_LAB_ARCHIVED_MEDIA_RETENTION")
    assert cp.run_archived_media_retention_once()["files"] == 2
