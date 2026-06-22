"""`_persist_job` writes job state durably (fsync before replace).

Regression guard for the fsync gap: the writer used to call
`tmp.write_text(json.dumps(...))` then `tmp.replace(path)`, omitting the
`os.fsync` that services.json_store.atomic_save uses. A crash between the
write completing and the rename could leave a truncated/partial file and lose
the first job save after a process crash.

`_persist_job` now delegates the durable write to
``services.json_store.atomic_save`` instead of reimplementing tmp+fsync+replace
inline, and ``_load_jobs_from_disk`` reads via ``atomic_load``. These tests pin
that durability still holds end-to-end.
"""

import json

from routers import clipper
from services import json_store


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
    """fsync must run on the tmp fd before the rename.

    The durable write is now delegated to ``json_store.atomic_save``, so the
    fsync/replace happen through ``json_store.os`` — spy there.
    """
    monkeypatch.setattr(clipper, "PROJECTS_DIR", tmp_path)

    events = []

    real_fsync = json_store.os.fsync
    real_replace = json_store.os.replace

    def spy_fsync(fd):
        events.append("fsync")
        return real_fsync(fd)

    def spy_replace(src, dst):
        events.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(json_store.os, "fsync", spy_fsync)
    monkeypatch.setattr(json_store.os, "replace", spy_replace)

    clipper._persist_job({"_project": "p", "job_id": "j", "status": "done"})

    assert events == ["fsync", "replace"], events


def test_persist_then_load_roundtrips(monkeypatch, tmp_path):
    """End-to-end: a persisted job rehydrates via _load_jobs_from_disk (atomic_load)."""
    monkeypatch.setattr(clipper, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(clipper, "_JOBS_LOADED", False)
    monkeypatch.setattr(clipper, "_batch_jobs", {})

    clipper._persist_job(
        {"_project": "proj-x", "job_id": "job-x", "status": "done", "progress": 100}
    )

    clipper._load_jobs_from_disk()

    assert "job-x" in clipper._batch_jobs
    loaded = clipper._batch_jobs["job-x"]
    assert loaded["status"] == "done"
    assert loaded["progress"] == 100
    # project is re-derived from the path on load
    assert loaded["_project"] == "proj-x"


def test_load_skips_corrupt_state_via_atomic_load(monkeypatch, tmp_path):
    """A truncated _state.json must not crash the load (atomic_load -> default {})."""
    monkeypatch.setattr(clipper, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(clipper, "_JOBS_LOADED", False)
    monkeypatch.setattr(clipper, "_batch_jobs", {})

    bad = tmp_path / "proj-bad" / "clips" / "job-bad" / "_state.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{ this is not json", encoding="utf-8")

    # Must not raise; corrupt file yields {} which has no job_id -> skipped.
    clipper._load_jobs_from_disk()
    assert clipper._batch_jobs == {}


def test_persist_job_missing_ids_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(clipper, "PROJECTS_DIR", tmp_path)
    # No project / no job_id -> nothing written, never raises.
    clipper._persist_job({"status": "running"})
    assert list(tmp_path.rglob("_state.json")) == []
