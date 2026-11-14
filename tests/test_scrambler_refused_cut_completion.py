"""A completed Scrambler job that refused one cut.

Lab main narrows a completed source job's ``sourceCuts`` to the cuts that made
a clip. With the Scrambler ON, a refused cut is skipped and its own ledger
reservation released (MED-1). Together: the refused cut's hold is released
exactly once (the job-end release must not touch it again, and the
undelivered-hold sweep never reaches it), and the stored job lists only the
delivered cut. No Scrambler reader uses the job's ``sourceCuts`` list for
holds; they all read the ledger.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import routers.control_plane as cp
from services import scrambler_originality as so
from services.scrambler import (
    RELEASED,
    RENDERED,
    live_reservation_rows,
    release_undelivered_holds,
    scrambler_history_rows,
)
from tests.test_control_plane_source_execution import (  # noqa: F401
    _isolated_source_dna,
    PAGE_ID,
    lab,
)
from tests.test_scrambler_executor_behavior import (  # noqa: F401
    _wire_executor,
    isolated_scrambler_state,
)
from tests.test_scrambler_pilot_fixes import (  # noqa: F401
    _RefuseFirstWindow,
    _enqueue,
    _fresh_index_cache,
)


def _job_rows(job_id):
    return {row["cutIndex"]: row for row in scrambler_history_rows(PAGE_ID)
            if row["jobId"] == job_id}


@pytest.mark.asyncio
async def test_refused_cut_is_released_once_and_dropped_from_completed_job(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    job_id = _enqueue(client, monkeypatch, tmp_path, 2, "refused-cut-completion")
    planned = cp._load_jobs()["jobs"][job_id]["sourceCuts"]
    assert [cut["scrambler"]["cutIndex"] for cut in planned] == [0, 1]
    lane = _wire_executor(monkeypatch, tmp_path, [job_id])
    proxy = _RefuseFirstWindow(so.load_originality_index())
    monkeypatch.setattr(cp, "load_originality_index_cached", lambda: proxy)

    real_release = cp.release_job_reservations
    calls, released = [], []

    def counting_release(page_id, released_job, **kwargs):
        before = {i: row["state"] for i, row in _job_rows(job_id).items()}
        real_release(page_id, released_job, **kwargs)
        calls.append((released_job, kwargs.get("cut_index"), kwargs["reason"]))
        for index, row in _job_rows(job_id).items():
            if row["state"] == RELEASED and before.get(index) != RELEASED:
                released.append((index, kwargs["reason"]))

    monkeypatch.setattr(cp, "release_job_reservations", counting_release)
    await cp._run_dossier_source(job_id)

    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert len(lane["renders"]) == 1
    assert [clip["scrambler"]["cutIndex"] for clip in job["clips"]] == [1]
    # Main's completed-job rewrite: only the delivered cut stays listed.
    assert job["sourceCuts"] == [planned[1]]

    # Ledger: the refused cut went RESERVED -> RELEASED exactly once, by the
    # per-cut refusal; the job-end release found nothing left to release.
    assert len(released) == 1, (released, calls)
    assert released[0][0] == 0
    assert released[0][1].startswith("cut_refused:scrambler_execution_originality_refused")
    assert [c for c in calls if c[1] == 0] == [(job_id, 0, released[0][1])]
    rows = _job_rows(job_id)
    assert rows[0]["state"] == RELEASED and rows[1]["state"] == RENDERED
    assert rows[0]["releaseReason"] == released[0][1]   # never overwritten
    first_release_at = rows[0]["releasedAt"]

    # Holds come from the ledger: only the delivered cut still holds a window.
    assert [(r["jobId"], r["cutIndex"]) for r in live_reservation_rows()
            if r["jobId"] == job_id] == [(job_id, 1)]
    # The undelivered-hold sweep (keyed on job status) never touches a
    # completed job, even long after the grace period.
    later = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    jobs = cp._load_jobs()["jobs"]
    assert release_undelivered_holds(
        lambda held: (jobs.get(held) or {}).get("status"), now=later) == 0
    rows = _job_rows(job_id)
    assert rows[0]["releasedAt"] == first_release_at
    assert rows[1]["state"] == RENDERED and rows[1].get("holdRelease") is None
