"""Scrambler fix round final2: red proofs for the Tides findings on the pilot fixes.

* MED-A: a delivered cut keeps its origin-window hold after its job is archived
  (only a ``failed`` job releases; an unknown job fails closed).
* LOW-1: the forced draws in the daily-report test are the stored draws.
* LOW-2: preflight names a malformed ``SCRAMBLER_PAGE_FAST_SHARE`` with the
  same parser the enqueue uses.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

import routers.control_plane as cp
from services import scrambler as s
from services import scrambler_originality as so
from services import scrambler_preflight as pf
from services.control_plane_sources import scrambler_pending_windows
from services.scrambler import RENDERED, scrambler_history_rows
from tests.test_control_plane_source_execution import (  # noqa: F401
    _isolated_source_dna,  # autouse: main #279 isolated source-DNA registry
    PAGE_ID,
    headers,
    job_body,
    lab,
)
from tests.test_scrambler_executor_behavior import (  # noqa: F401
    _originality_feed,
    _scrambler_on,
    _wire_executor,
    isolated_scrambler_state,
)
from tests.test_scrambler_pilot_fixes import _pilot_env


@pytest.fixture(autouse=True)
def _fresh_index_cache():
    so._index_cache.clear()
    yield
    so._index_cache.clear()


# ---------------------------------------------------------------------------
# MED-A: archived (completed) jobs keep their delivered cuts' holds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delivered_cut_keeps_its_hold_after_its_job_is_archived(lab, monkeypatch, tmp_path):
    """A COMPLETED (delivered) cut that has not posted yet must keep holding its
    origin window. Job-store compaction archives completed jobs after 30 days,
    after which the create_job status lambda returns None ("unknown")."""
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    monkeypatch.setenv("SCRAMBLER_TREATMENT_WEIGHTS", "none:1,split_swap:0,camera:0,grade:0")
    response = client.post("/api/control-plane/v1/jobs", json=job_body(1),
                           headers=headers("final2-archived"))
    assert response.status_code == 200, response.json()
    job_id = response.json()["jobId"]
    _wire_executor(monkeypatch, tmp_path, [job_id])
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")      # delivered to the Worker
    row = next(r for r in scrambler_history_rows(PAGE_ID) if r["jobId"] == job_id)
    assert row["state"] == RENDERED and row.get("postingObservation") is None
    index = so.load_originality_index()
    assert len(scrambler_pending_windows(index, "another-job")) == 1
    # 31 days later the Lab restarts (forced compaction pass) and archives the job.
    later = datetime.fromisoformat(job["completedAt"]) + timedelta(days=31)
    summary = cp.run_compaction_once(cp._jobs_path(), later)
    assert summary["archived"] >= 1
    assert job_id not in cp._load_jobs()["jobs"]
    # The exact status lookup create_job passes to release_undelivered_holds.
    status = lambda held: (cp._load_jobs()["jobs"].get(held) or {}).get("status")  # noqa: E731
    released = s.release_undelivered_holds(status, now=later.isoformat())
    # A delivered, not-yet-posted clip can still post: its window must stay held.
    assert released == 0, "a delivered cut lost its origin-window hold after archival"
    assert len(scrambler_pending_windows(index, "another-job")) == 1


def test_only_a_failed_job_releases_a_rendered_hold(tmp_path):
    """Unit form of MED-A: unknown (None) and every non-failed status keep the
    hold; only ``failed`` releases it, and only after the grace period."""
    path = tmp_path / "history.json"
    s.reserve_job_cuts("page", "j1", 1, path=path)
    s.mark_cut_rendered("page", "j1", 0, asset_sha256="a" * 64,
                        rendered_at="2026-10-01T00:00:00+00:00", path=path)
    later = "2026-10-02T01:00:00+00:00"   # 25 h after render
    for status in (None, "completed", "running", "queued", "cancelled", ""):
        assert s.release_undelivered_holds(lambda job, st=status: st, now=later, path=path) == 0, status
    assert s.release_undelivered_holds(lambda job: "failed",
                                       now="2026-10-01T23:00:00+00:00", path=path) == 0
    assert s.release_undelivered_holds(lambda job: "failed", now=later, path=path) == 1


# ---------------------------------------------------------------------------
# LOW-1: the edited daily-report test's forced D is Worker-admissible
# ---------------------------------------------------------------------------

def test_daily_report_forced_d_is_admissible_and_stored(tmp_path, monkeypatch):
    path = tmp_path / "history.json"
    forced = [s.ScramblerDraw("none", 0.8, 8000, 0, False)]
    monkeypatch.setattr(s, "_candidate", lambda *args, **kwargs: forced[0])
    for job, draw in (("a", forced[0]), ("b", s.ScramblerDraw("camera", 0.75, 7500, 0, False)),
                      ("c", s.ScramblerDraw("none", 0.7, 8500, 0, True)),
                      ("d", s.ScramblerDraw("camera", 0.6, 9000, 0, True))):
        assert s.worker_admits_window(draw.speed, draw.length_ms), draw
        forced[0] = draw
        s.reserve_job_cuts("page", job, 1, path=path)
    d = next(r for r in s.scrambler_history_rows("page", path=path) if r["jobId"] == "d")
    assert (d["treatment"], d["speed"], d["lengthMs"], d["mirror"]) == ("camera", 0.6, 9000, True), d


# ---------------------------------------------------------------------------
# LOW-2: preflight parses SCRAMBLER_PAGE_FAST_SHARE like the enqueue does
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["acct:freddy-2am:9", "acct:freddy-2am:x",
                                   "acct:trailheadtravis:0.2,acct:freddy-2am:-0.1"])
def test_preflight_names_a_malformed_page_fast_share(tmp_path, monkeypatch, value):
    env = dict(_pilot_env(tmp_path), SCRAMBLER_PAGE_FAST_SHARE=value)
    problems = pf.preflight_problems(env, full=False)
    assert any("SCRAMBLER_PAGE_FAST_SHARE" in p and "acct:freddy-2am" in p for p in problems), problems
    assert pf.log_startup_preflight(env=env)            # startup line is NOT READY too
    # ...and it is exactly the setting the enqueue refuses on.
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    with pytest.raises(ValueError, match="scrambler_page_fast_share_invalid"):
        s.reserve_job_cuts("acct:freddy-2am", "j-fast-share", 1,
                           path=Path(env["SCRAMBLER_HISTORY_PATH"]))


def test_preflight_accepts_a_valid_page_fast_share(tmp_path, monkeypatch):
    env = dict(_pilot_env(tmp_path),
               SCRAMBLER_PAGE_FAST_SHARE="acct:freddy-2am:0.25,acct:trailheadtravis:1")
    assert pf.preflight_problems(env, full=False) == []
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    assert s.fast_share_for("acct:freddy-2am") == 0.25
    assert s.fast_share_for("acct:trailheadtravis") == 1.0
    assert s.fast_share_for("acct:backpack-bohemian") == 0.0
