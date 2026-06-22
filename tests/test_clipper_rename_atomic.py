"""The clipper job-rename endpoint persists job_meta.json atomically.

Regression guard for the fsync gap: the rename used to call
`write_text(json.dumps(...))`, which can leave job_meta.json truncated if the
process crashes mid-write. It now routes through services.json_store.atomic_save
(tmp + fsync + os.replace).
"""

import json

from routers import clipper


def _job_dir(project: str, job_id: str):
    d = clipper._get_clipper_dir(project) / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_rename_writes_label_and_preserves_existing_meta(sync_client, isolated_projects_root):
    job_dir = _job_dir("rename-proj", "job-abc")
    # Pre-existing meta with an unrelated key must survive the rename.
    (job_dir / "job_meta.json").write_text(
        json.dumps({"created_at": "t0"}), encoding="utf-8"
    )

    resp = sync_client.patch(
        "/api/clipper/jobs/job-abc/rename",
        params={"project": "rename-proj"},
        json={"label": "  My Clip  "},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "label": "My Clip"}

    meta = json.loads((job_dir / "job_meta.json").read_text(encoding="utf-8"))
    assert meta["label"] == "My Clip"
    assert meta["created_at"] == "t0"  # untouched


def test_rename_uses_atomic_save(sync_client, isolated_projects_root, monkeypatch):
    """The endpoint must go through atomic_save, not a raw write_text."""
    _job_dir("rename-proj2", "job-xyz")
    calls = {}

    real = clipper.atomic_save

    def spy(path, data, **kwargs):
        calls["path"] = path
        calls["data"] = data
        return real(path, data, **kwargs)

    monkeypatch.setattr(clipper, "atomic_save", spy)

    resp = sync_client.patch(
        "/api/clipper/jobs/job-xyz/rename",
        params={"project": "rename-proj2"},
        json={"label": "Renamed"},
    )
    assert resp.status_code == 200
    assert calls["data"]["label"] == "Renamed"
    assert str(calls["path"]).endswith("job_meta.json")


def test_rename_rejects_empty_label(sync_client, isolated_projects_root):
    _job_dir("rename-proj3", "job-1")
    resp = sync_client.patch(
        "/api/clipper/jobs/job-1/rename",
        params={"project": "rename-proj3"},
        json={"label": "   "},
    )
    assert resp.status_code == 400
