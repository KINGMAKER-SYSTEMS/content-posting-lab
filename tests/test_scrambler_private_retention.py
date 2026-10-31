"""Private produced Scrambler media is not an unconfirmed stray."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import routers.control_plane as cp
from services import generated_media_retention as retention
from tests.test_scrambler_executor_behavior import (
    lab, isolated_scrambler_state, _isolated_source_dna, _scrambler_on,
    _originality_feed, _wire_executor, job_body, headers,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("pressure,hours", [("normal", 48), ("tight", 24), ("floor", 6)])
async def test_actual_failed_scrambler_checkpoint_survives_retention(lab, monkeypatch, pressure, hours):
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    monkeypatch.setenv("SCRAMBLER_TREATMENT_WEIGHTS", "none:1,split_swap:0,camera:0,grade:0")
    made = client.post("/api/control-plane/v1/jobs", json=job_body(1), headers=headers("private-retention"))
    assert made.status_code == 200
    job_id = made.json()["jobId"]
    _wire_executor(monkeypatch, tmp_path, [job_id])
    actual = cp.mark_cut_rendered
    def committed_then_cancelled(*args, **kwargs):
        actual(*args, **kwargs)
        raise asyncio.CancelledError()
    monkeypatch.setattr(cp, "mark_cut_rendered", committed_then_cancelled)
    with pytest.raises(asyncio.CancelledError):
        await cp._run_dossier_source(job_id)
    store = cp._load_jobs(); job = store["jobs"][job_id]
    assert job["status"] == "failed" and job["clips"] == []
    assert len(job["scramblerRenderedClips"]) == 1
    clip = job["scramblerRenderedClips"][0]
    directory = Path(job["artifactRoot"])
    media, thumbnail = directory / clip["path"], directory / clip["thumbnail"]["path"]
    before = media.read_bytes(), thumbnail.read_bytes()
    stray = directory / "unreferenced.part"; stray.write_bytes(b"true intermediate")
    now = datetime.fromisoformat(job["completedAt"]) + timedelta(hours=hours, seconds=1)
    confirmations = []
    sweep = retention.GeneratedMediaRetention(confirm=lambda *args: confirmations.append(args) or False)
    from services.scrambler import scrambler_history_path
    ledger = scrambler_history_path(); ledger_before = ledger.read_bytes()
    summary = sweep.sweep(store, cp._generation_root(), now, pressure=pressure)
    assert ledger.read_bytes() == ledger_before
    assert (media.read_bytes(), thumbnail.read_bytes()) == before
    assert summary["stray_files"] == 1 and not stray.exists()
    assert confirmations == []
    assert cp._load_jobs() == store


@pytest.fixture
def private_job(tmp_path):
    root = (tmp_path / "generated").resolve()
    job_id = "cpl-1111111111111111"
    directory = root / "acct:page" / "dossier-v1" / job_id
    clip = {"path": "treated/cut-0000.mp4", "sha256": "a" * 64, "bytes": 5,
            "thumbnail": {"path": "thumbnails/0000.jpg", "sha256": "b" * 64, "bytes": 4}}
    for relative, data in [(clip["path"], b"video"), (clip["thumbnail"]["path"], b"jpeg"), ("true-stray.part", b"stray")]:
        file = directory / relative; file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(data)
    now = datetime(2026, 10, 30, 18, tzinfo=timezone.utc)
    job = {"jobId": job_id, "pageId": "acct:page", "sourceKind": "dossier_source_dna",
           "sourceClaimVersion": 1, "runtimeId": "a" * 32, "status": "failed", "error": "generation_cancelled",
           "artifactRoot": str(directory), "completedAt": (now - timedelta(days=3)).isoformat(),
           "clips": [], "scramblerRenderedClips": [clip]}
    return root, directory, job_id, job, now


@pytest.mark.parametrize("confirmed", [False, True])
def test_matching_public_checkpoint_keeps_existing_r2_cleanup(private_job, confirmed):
    root, directory, job_id, job, now = private_job
    job["status"] = "completed"
    job["clips"] = copy.deepcopy(job["scramblerRenderedClips"])
    calls = []
    sweep = retention.GeneratedMediaRetention(confirm=lambda *args: calls.append(args) or confirmed)
    summary = sweep.sweep({"jobs": {job_id: job}}, root, now)
    assert len(calls) == 1
    assert summary["stray_files"] == 1
    for relative in ["treated/cut-0000.mp4", "thumbnails/0000.jpg"]:
        assert (directory / relative).exists() is (not confirmed)
    assert summary["clip_files"] == (2 if confirmed else 0)
    if confirmed:
        # Stale private metadata after successful R2 cleanup is not a whole-job
        # hold: a later real intermediate can still be reclaimed.
        stray = directory / "another.part"; stray.parent.mkdir(parents=True, exist_ok=True); stray.write_bytes(b"later")
        assert sweep.sweep({"jobs": {job_id: job}}, root, now)["stray_files"] == 1


@pytest.mark.parametrize("bad", [None, {}, False, 0, "bad", [None], [{}]])
def test_malformed_private_checkpoint_holds_whole_job(private_job, bad):
    root, directory, job_id, job, now = private_job
    job["scramblerRenderedClips"] = bad
    plan = retention.GeneratedMediaRetention(confirm=lambda *_: True).plan({"jobs": {job_id: job}}, root, now)
    assert plan.clip_files == plan.stray_files == []
    assert (directory / "true-stray.part").is_file()


@pytest.mark.parametrize("change", ["escape", "absolute", "dot", "double-slash", "bad-sha", "bool-size",
                                   "bad-thumbnail", "thumbnail-escape", "wrong-job", "wrong-page",
                                   "wrong-kind", "duplicate-conflict", "public-conflict", "symlink", "directory", "size-mismatch"])
def test_ambiguous_private_checkpoint_holds_whole_job(private_job, change):
    root, directory, job_id, job, now = private_job
    clip = job["scramblerRenderedClips"][0]
    if change == "escape": clip["path"] = "../outside.mp4"
    elif change == "absolute": clip["path"] = str(directory / clip["path"])
    elif change == "dot": clip["path"] = "treated/./cut-0000.mp4"
    elif change == "double-slash": clip["path"] = "treated//cut-0000.mp4"
    elif change == "bad-sha": clip["sha256"] = "bad"
    elif change == "bool-size": clip["bytes"] = True
    elif change == "bad-thumbnail": clip["thumbnail"] = None
    elif change == "thumbnail-escape": clip["thumbnail"]["path"] = "../../outside.jpg"
    elif change == "wrong-job": job["jobId"] = "cpl-2222222222222222"
    elif change == "wrong-page": job["pageId"] = "acct:other"
    elif change == "wrong-kind": job["sourceKind"] = "page_source_import"
    elif change == "duplicate-conflict": job["scramblerRenderedClips"].append({**clip, "sha256": "c" * 64})
    elif change == "public-conflict": job["clips"] = [{**clip, "sha256": "c" * 64}]
    elif change == "directory": clip["path"] = "treated"
    elif change == "size-mismatch": clip["bytes"] += 1
    elif change == "symlink":
        original = directory / clip["path"]; original.unlink(); original.symlink_to(directory / "true-stray.part")
    plan = retention.GeneratedMediaRetention(confirm=lambda *_: True).plan({"jobs": {job_id: job}}, root, now)
    assert plan.clip_files == plan.stray_files == []
    assert (directory / "true-stray.part").is_file()


def test_unenumerable_private_checkpoint_holds_whole_job(private_job):
    class Unenumerable(list):
        def __iter__(self):
            raise RuntimeError("unavailable checkpoint")
    root, directory, job_id, job, now = private_job
    job["scramblerRenderedClips"] = Unenumerable(job["scramblerRenderedClips"])
    plan = retention.GeneratedMediaRetention(confirm=lambda *_: True).plan({"jobs": {job_id: job}}, root, now)
    assert plan.clip_files == plan.stray_files == []
    assert (directory / "true-stray.part").is_file()
