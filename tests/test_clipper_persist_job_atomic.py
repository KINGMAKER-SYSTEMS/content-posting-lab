"""`_persist_job` writes job state durably (fsync before replace).

Regression guard for the fsync gap: the writer used to call
`tmp.write_text(json.dumps(...))` then `tmp.replace(path)`, omitting the
`os.fsync` that services.json_store.atomic_save uses. A crash between the
write completing and the rename could leave a truncated/partial file and lose
the first job save after a process crash. The writer now flushes + fsyncs the
tmp file before the atomic rename.
"""

import json

from routers import clipper


def test_persist_job_roundtrips(monkeypatch, tmp_path):
    monkeypatch.setattr(clipper, "PROJECTS_DIR", tmp_path)

    job = {
        "_project": "proj-1",
        "_runtime_ref": object(),  # transient, must not be serialized
        "job_id": "job-1",
        "status": "running",
        "progress": 42,
    }
    clipper._persist_job(job)

    path = clipper._job_state_path("proj-1", "job-1")
    assert path.exists()
    # No leftover tmp file.
    assert not path.with_suffix(".json.tmp").exists()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"job_id": "job-1", "status": "running", "progress": 42}
    assert "_project" not in data and "_runtime_ref" not in data


def test_persist_job_fsyncs_before_replace(monkeypatch, tmp_path):
    """fsync must run on the tmp fd before the rename, mirroring atomic_save."""
    monkeypatch.setattr(clipper, "PROJECTS_DIR", tmp_path)

    events = []

    real_fsync = clipper.os.fsync
    real_replace = clipper.os.replace

    def spy_fsync(fd):
        events.append("fsync")
        return real_fsync(fd)

    def spy_replace(src, dst):
        events.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(clipper.os, "fsync", spy_fsync)
    monkeypatch.setattr(clipper.os, "replace", spy_replace)

    clipper._persist_job({"_project": "p", "job_id": "j", "status": "done"})

    assert events == ["fsync", "replace"], events


def test_persist_job_missing_ids_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(clipper, "PROJECTS_DIR", tmp_path)
    # No project / no job_id -> nothing written, never raises.
    clipper._persist_job({"status": "running"})
    assert list(tmp_path.rglob("_state.json")) == []
