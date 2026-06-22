"""Regression tests: router config-file writes must be crash-safe (fsync'd).

Several routers used to write JSON config/metadata via a raw ``write_text`` (or a
tmp+replace without ``fsync``). A process crash mid-write could leave corrupted
JSON — the exact P0 that hit the upload-job queue. These helpers now route every
write through ``services.json_store.atomic_save`` (tmp + flush + fsync + replace).

Each test asserts the helper actually invokes ``os.fsync`` (proving it goes
through the atomic path, not a bare write), leaves no ``*.tmp`` sidecar, and
produces a complete, re-loadable document. If anyone reverts a helper to a raw
``write_text``, the fsync assertion fails.
"""

import json

import pytest

from services import json_store


@pytest.fixture
def fsync_spy(monkeypatch):
    """Count os.fsync calls made through json_store during a save."""
    calls = {"n": 0}
    real_fsync = json_store.os.fsync

    def counting_fsync(fd):
        calls["n"] += 1
        return real_fsync(fd)

    monkeypatch.setattr(json_store.os, "fsync", counting_fsync)
    return calls


def test_burn_save_batch_meta_is_fsynced(tmp_path, fsync_spy):
    from routers import burn

    meta = {"batch_id": "proj-06151200-1", "items": [1, 2, 3]}
    burn._save_batch_meta(tmp_path, meta)

    out = tmp_path / "batch_meta.json"
    assert fsync_spy["n"] >= 1, "batch meta write was not fsync'd (raw write?)"
    assert json.loads(out.read_text(encoding="utf-8")) == meta
    assert not list(tmp_path.glob("*.tmp")), "tmp sidecar leaked"


def test_video_save_prompt_is_fsynced(tmp_path, fsync_spy, monkeypatch):
    from routers import video

    proj_dir = tmp_path / "proj"
    monkeypatch.setattr(video, "PROJECTS_DIR", tmp_path)

    video._save_prompt("proj", {"prompt": "a cat", "provider": "grok"})

    out = proj_dir / "prompts.json"
    assert fsync_spy["n"] >= 1, "prompt write was not fsync'd (raw write?)"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data[0]["prompt"] == "a cat"
    assert not list(proj_dir.glob("*.tmp")), "tmp sidecar leaked"


def test_video_save_jobs_is_fsynced(tmp_path, fsync_spy, monkeypatch):
    from routers import video

    proj_dir = tmp_path / "proj"
    monkeypatch.setattr(video, "PROJECTS_DIR", tmp_path)
    monkeypatch.setitem(video.jobs, "job-1", {"project": "proj", "videos": []})

    try:
        video._save_jobs("proj")
    finally:
        video.jobs.pop("job-1", None)

    out = proj_dir / "jobs.json"
    assert fsync_spy["n"] >= 1, "jobs write was not fsync'd (raw write?)"
    assert "job-1" in json.loads(out.read_text(encoding="utf-8"))
    assert not list(proj_dir.glob("*.tmp")), "tmp sidecar leaked"
