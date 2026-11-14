"""Actual enqueue and executor cleanup before the render loop."""
import pytest
import routers.control_plane as cp
from services.scrambler import scrambler_history_rows, RELEASED
from tests.test_scrambler_executor_behavior import (
    lab, isolated_scrambler_state, _isolated_source_dna, _scrambler_on, _originality_feed,
    job_body, headers, PAGE_ID, _run,
)

@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["strategy", "publication"])
async def test_early_execution_failure_releases_committed_draw(
        lab, monkeypatch, failure):
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    response = client.post("/api/control-plane/v1/jobs", json=job_body(1),
                           headers=headers("early-"+failure))
    assert response.status_code == 200, response.json()
    job_id = response.json()["jobId"]
    if failure == "strategy":
        monkeypatch.setattr(cp, "_job_matches_current_master_pages", lambda job: False)
    else:
        def missing(*args, **kwargs):
            raise RuntimeError("publication unavailable")
        monkeypatch.setattr(cp, "load_registered_recipe", missing)
    await _run(job_id)
    assert cp._load_jobs()["jobs"][job_id]["status"] == "failed"
    assert [row["state"] for row in scrambler_history_rows(PAGE_ID)
            if row["jobId"] == job_id] == [RELEASED]

@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["retire", "commitment"])
async def test_queue_delay_rechecks_authority_before_any_render(lab, monkeypatch, change):
    import json, os
    from pathlib import Path
    from tests.test_scrambler_executor_behavior import MASTER_SHA, _wire_executor
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    response = client.post("/api/control-plane/v1/jobs", json=job_body(1),
                           headers=headers("delay-"+change))
    assert response.status_code == 200, response.json()
    job_id = response.json()["jobId"]
    lane = _wire_executor(monkeypatch, tmp_path, [job_id])
    if change == "retire":
        Path(os.environ["CONTENT_LAB_ORIGINALITY_RETIRED_PATH"]).write_text(
            json.dumps([{"master_sha256s": [MASTER_SHA]}]))
    else:
        store = cp._load_jobs()
        # A different valid speed must still mismatch the frozen commitment.
        record = store["jobs"][job_id]["sourceCuts"][0]["scrambler"]
        record["mirror"] = not record["mirror"]
        cp._save_jobs(store)
    await _run(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "failed", job
    if change == "retire":
        assert job["error"].startswith("scrambler_execution_originality_refused:retired_master:"), job["error"]
    else:
        assert job["error"] == "scrambler_execution_commitment_mismatch"
    assert lane["renders"] == []
    assert [row["state"] for row in scrambler_history_rows(PAGE_ID)
            if row["jobId"] == job_id] == [RELEASED]

def test_missing_ledger_and_marker_cannot_reset_existing_jobs(lab, monkeypatch):
    import os
    from pathlib import Path
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    first = client.post("/api/control-plane/v1/jobs", json=job_body(1),
                        headers=headers("before-history-loss"))
    assert first.status_code == 200, first.json()
    path = Path(os.environ["SCRAMBLER_HISTORY_PATH"])
    path.unlink()
    path.with_name(path.name + ".created").unlink()
    second = client.post("/api/control-plane/v1/jobs", json=job_body(1),
                         headers=headers("after-history-loss"))
    assert second.status_code == 409, second.json()
    assert "history" in second.json()["detail"]
    assert not path.exists()

def test_crash_before_durable_job_save_releases_uncommitted_draw(lab, monkeypatch):
    from services.scrambler import reserve_job_cuts
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    reserve_job_cuts(PAGE_ID, "orphan-before-save", 1)
    response = client.post("/api/control-plane/v1/jobs", json=job_body(1),
                           headers=headers("recover-crashed-enqueue"))
    assert response.status_code == 200, response.json()
    orphan = [row for row in scrambler_history_rows(PAGE_ID)
              if row["jobId"] == "orphan-before-save"]
    assert [row["state"] for row in orphan] == [RELEASED]
    assert orphan[0]["releaseReason"] == "uncommitted_enqueue_recovered"

@pytest.mark.asyncio
async def test_on_jobs_execute_one_at_a_time_per_page(lab, monkeypatch):
    import asyncio
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    ids = []
    for i in range(2):
        response = client.post("/api/control-plane/v1/jobs", json=job_body(1),
                               headers=headers("serialized-"+str(i)))
        assert response.status_code == 200, response.json()
        ids.append(response.json()["jobId"])
    events = []
    async def execution(job_id):
        events.append(("start", job_id))
        await asyncio.sleep(0.02)
        events.append(("end", job_id))
    monkeypatch.setattr(cp, "_run_dossier_source_inner", execution)
    await asyncio.gather(*(cp._run_dossier_source(job_id) for job_id in ids))
    assert events == [("start", ids[0]), ("end", ids[0]),
                      ("start", ids[1]), ("end", ids[1])]

@pytest.mark.asyncio
async def test_cancel_during_committed_thread_keeps_full_recovery_checkpoint(lab, monkeypatch):
    import asyncio
    from tests.test_scrambler_executor_behavior import _wire_executor
    from services.scrambler import RENDERED
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    response = client.post("/api/control-plane/v1/jobs", json=job_body(1),
                           headers=headers("cancel-after-commit"))
    assert response.status_code == 200, response.json()
    job_id = response.json()["jobId"]
    _wire_executor(monkeypatch, tmp_path, [job_id])
    commit = cp.mark_cut_rendered
    def commit_then_cancel(*args, **kwargs):
        commit(*args, **kwargs)
        # Model cancellation delivered by the await after its background
        # thread committed, before the caller appends the in-memory manifest.
        raise asyncio.CancelledError()
    monkeypatch.setattr(cp, "mark_cut_rendered", commit_then_cancel)
    with pytest.raises(asyncio.CancelledError):
        await _run(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "failed"
    assert job["clips"] == []
    assert len(job["scramblerRenderedClips"]) == 1
    rows = [row for row in scrambler_history_rows(PAGE_ID) if row["jobId"] == job_id]
    assert rows[0]["state"] == RENDERED
    assert job["scramblerRenderedClips"][0]["sha256"] == rows[0]["assetSha256"]
