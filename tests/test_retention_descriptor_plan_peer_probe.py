"""Nonshipping read-only plan probe; synthetic files, no cleanup or R2 call."""
from datetime import timedelta

import pytest

from routers import control_plane as cp
from tests.test_generated_media_retention import A, B, NOW, make_job, sweeper


def _snapshot(directory):
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


def _persist(monkeypatch, tmp_path, jobs):
    store_path = tmp_path / "jobs.json"
    monkeypatch.setattr(cp, "_jobs_path", lambda: store_path)
    cp.atomic_save(store_path, {"jobs": jobs, "byIdempotency": {}})
    return store_path, cp._load_jobs()


@pytest.mark.parametrize("shape", ["missing_path", "null_path", "outside_path"])
def test_malformed_descriptor_plan_holds_unidentified_files_and_keeps_confirmed_peer(
    tmp_path, monkeypatch, shape,
):
    root = tmp_path / "generated"
    bad_dir, bad = make_job(root, name="bad", age=timedelta(days=3), extra_files=())
    good_dir, good = make_job(root, name="good", age=timedelta(hours=3),
                              clips=((B, "renders/good.mp4"),), extra_files=())
    bad["clips"] = [{
        "missing_path": {},
        "null_path": {"path": None, "sha256": A, "bytes": 100},
        "outside_path": {"path": "../outside.mp4", "sha256": A, "bytes": 100},
    }[shape]]
    path, stored = _persist(monkeypatch, tmp_path, {"bad": bad, "good": good})
    private_before = path.read_bytes()
    bad_before, good_before = _snapshot(bad_dir), _snapshot(good_dir)
    worker, asked = sweeper(lambda digest: digest == B)

    plan = worker.plan(stored, root, NOW)

    assert _snapshot(bad_dir) == bad_before and _snapshot(good_dir) == good_before
    assert path.read_bytes() == private_before, "planning must not rewrite persisted job metadata"
    assert asked == [(B, 100)], "only the valid peer has identifiable confirmable media"
    assert set(plan.clip_files) == {good_dir / "renders/good.mp4", good_dir / "thumbnails/0000.jpg"}
    selected = [item for item in plan.clip_files + plan.stray_files
                if bad_dir == item or bad_dir in item.parents]
    assert selected == [], "malformed clip descriptors must hold unidentified job files, not select them as strays"


def test_empty_clip_list_legacy_plan_can_select_expired_stray(tmp_path, monkeypatch):
    root = tmp_path / "generated"
    directory, job = make_job(root, age=timedelta(days=3), clips=(),
                              extra_files=("scratch/old.bin",))
    path, stored = _persist(monkeypatch, tmp_path, {"legacy": job})
    before, private_before = _snapshot(directory), path.read_bytes()
    worker, asked = sweeper(False)
    plan = worker.plan(stored, root, NOW)
    assert asked == [] and plan.clip_files == []
    assert plan.stray_files == [directory / "scratch/old.bin"]
    assert _snapshot(directory) == before and path.read_bytes() == private_before


def test_known_unconfirmed_manifest_plan_keeps_identified_clip_and_thumbnail(tmp_path, monkeypatch):
    root = tmp_path / "generated"
    directory, job = make_job(root, age=timedelta(days=3), extra_files=())
    path, stored = _persist(monkeypatch, tmp_path, {"known": job})
    before, private_before = _snapshot(directory), path.read_bytes()
    worker, asked = sweeper(False)
    plan = worker.plan(stored, root, NOW)
    assert asked == [(A, 100)]
    assert plan.clip_files == plan.stray_files == []
    assert plan.unconfirmed_bytes == 101
    assert _snapshot(directory) == before and path.read_bytes() == private_before
