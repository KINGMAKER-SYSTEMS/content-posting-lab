"""Pilot fixes for the Scrambler review (HIGH-1, HIGH-2, MED-1, MED-2, MED-3).

HIGH-2  every drawn cut has an origin window the live Worker admits (5-10 s)
        and a receipt the Worker can prepare (the recipe look, clipSpeed 0.5-2);
        the drawn speed stays in the manifest-only record.
HIGH-1  ``tools/scrambler_report.py --preflight`` and the startup log line name
        every missing pilot setting; nothing is added to any HTTP response.
MED-1   a refused cut is skipped and released; vetted cuts still deliver; a
        rendered cut its job never delivered loses its origin hold after 24 h.
MED-2   the feed parse runs off the event loop before the jobs lock and one
        parse serves the enqueue and every cut of the job.
MED-3   page ids match the roster integration_id exactly.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import routers.control_plane as cp
from services import scrambler as s
from services import scrambler_originality as so
from services import scrambler_preflight as pf
from services.control_plane_sources import scrambler_pending_windows
from services.scrambler import RELEASED, RENDERED, scrambler_history_rows
from services.source_treatment import normalized_visual_treatment
from tests.test_control_plane_source_execution import (  # noqa: F401
    _isolated_source_dna,  # autouse: main #279 isolated source-DNA registry
    PAGE_ID,
    headers,
    job_body,
    lab,
)
from tests.test_scrambler_executor_behavior import (  # noqa: F401
    _job_scrambler_records,
    _originality_feed,
    _scrambler_on,
    _wire_executor,
    isolated_scrambler_state,
)

ROOT = Path(__file__).resolve().parents[1]
PILOT_PAGES = "acct:trailheadtravis,acct:backpack-bohemian,acct:freddy-2am"
SLOW_GRID = [round(0.30 + 0.05 * i, 2) for i in range(11)]
LENGTH_GRID = list(range(5_000, 9_001, 500))


@pytest.fixture(autouse=True)
def _fresh_index_cache():
    so._index_cache.clear()
    yield
    so._index_cache.clear()


# ---------------------------------------------------------------------------
# HIGH-2: Worker admission of every drawn cut
# ---------------------------------------------------------------------------

def test_worker_window_rule_over_the_whole_slow_grid():
    """Every (speed 0.30..0.80 step 0.05, length 5..9 s) pair: the Lab admits it
    exactly when the Worker's 5-10 s origin-window rule does, and an admitted
    pair always delivers 5-9 s of output (window / speed)."""
    admitted = []
    for speed in SLOW_GRID:
        for length in LENGTH_GRID:
            window = round(length * speed)
            assert s.worker_admits_window(speed, length) == (5_000 <= window <= 10_000)
            if s.worker_admits_window(speed, length):
                admitted.append((speed, length))
                assert 5_000 <= window / speed <= 9_000 + 1
    # 0.3x-0.55x can never reach a 5 s origin window inside a 9 s output.
    assert {speed for speed, _ in admitted} == {0.6, 0.65, 0.7, 0.75, 0.8}
    assert len(admitted) == 20


@pytest.mark.parametrize("fast_share", ["0", "1"])
def test_every_draw_is_worker_admissible(tmp_path, monkeypatch, fast_share):
    monkeypatch.setenv("SCRAMBLER_FAST_SHARE", fast_share)
    path = tmp_path / "history.json"
    day0 = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    for job in range(40):
        moment = (day0 + timedelta(hours=job)).isoformat()
        for draw in s.reserve_job_cuts("acct:admission", f"job-{job}", 3, path=path, now=moment):
            assert 5_000 <= draw.window_ms() <= 10_000, draw
            assert 5_000 <= draw.length_ms <= 9_000
        s.release_job_reservations("acct:admission", f"job-{job}", reason="test", path=path)


def test_lab_receipt_normalizer_matches_the_worker_speed_range():
    """The Worker validates receipt clipSpeed in 0.5-2; the Lab does too, so a
    receipt the Worker would call malformed is never written."""
    base = {"filters": {}, "clipCrop": {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5}}
    assert normalized_visual_treatment({**base, "clipSpeed": 0.5})["clipSpeed"] == 0.5
    for bad in (0.25, 0.3, 0.45, 2.01):
        with pytest.raises(ValueError):
            normalized_visual_treatment({**base, "clipSpeed": bad})


@pytest.mark.asyncio
async def test_delivered_scrambler_clips_pass_worker_admission(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    monkeypatch.setenv("SCRAMBLER_TREATMENT_WEIGHTS", "none:1,split_swap:0,camera:0,grade:0")
    response = client.post("/api/control-plane/v1/jobs", json=job_body(3),
                           headers=headers("pilot-admission"))
    assert response.status_code == 200, response.json()
    job_id = response.json()["jobId"]
    _wire_executor(monkeypatch, tmp_path, [job_id])
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert len(job["clips"]) == 3
    for clip in job["clips"]:
        window = clip["source"]["cutWindow"]
        origin = window["originalEndMs"] - window["originalStartMs"]
        assert 5_000 <= origin <= 10_000                      # reserveSourceWindow
        speed = clip["scrambler"]["effectiveSpeed"]
        assert 5_000 <= window["durationMs"] / speed <= 9_001  # real output 5-9 s
        receipt = clip["sourceTreatment"]
        # sourcePreparationBlocker: receipt look == the recipe look exactly.
        assert receipt["visualTreatment"] == normalized_visual_treatment(
            receipt["sourceRecipeTreatment"])


# ---------------------------------------------------------------------------
# HIGH-1: preflight names every missing setting
# ---------------------------------------------------------------------------

def _pilot_env(tmp_path: Path) -> dict:
    volume = tmp_path / "projects"
    feed = volume / "originality"
    feed.mkdir(parents=True)
    (feed / "usage.jsonl").write_text("")
    (feed / "origins.json").write_text("{}")
    return {
        "SCRAMBLER_ENABLED": "1",
        "SCRAMBLER_PAGES": PILOT_PAGES,
        "SCRAMBLER_FAST_SHARE": "0",
        "SCRAMBLER_WORKER_SOURCE_WINDOW_COMPAT": "1",
        "CONTENT_LAB_ORIGINALITY_USAGE_PATH": str(feed / "usage.jsonl"),
        "SCRAMBLER_FEED_R2_BUCKET": "rt-content-buckets",
        "SCRAMBLER_FEED_R2_PREFIX": "originality/feed/",
        "RAILWAY_VOLUME_MOUNT_PATH": str(volume),
        "SCRAMBLER_HISTORY_PATH": str(volume / "scrambler-history.json"),
    }


def test_preflight_complete_pilot_env_is_ready(tmp_path):
    assert pf.preflight_problems(_pilot_env(tmp_path)) == []


def test_preflight_names_every_setting_the_review_pilot_config_lacks(tmp_path):
    """The config as first written (switches only) refuses every footage job;
    preflight must name each missing piece."""
    env = {"SCRAMBLER_ENABLED": "1", "SCRAMBLER_PAGES": PILOT_PAGES, "SCRAMBLER_FAST_SHARE": "0"}
    text = "\n".join(pf.preflight_problems(env))
    for name in ("SCRAMBLER_WORKER_SOURCE_WINDOW_COMPAT", "CONTENT_LAB_ORIGINALITY_USAGE_PATH",
                 "no durable history path", "SCRAMBLER_FEED_R2_BUCKET",
                 "SCRAMBLER_FEED_R2_PREFIX"):
        assert name in text, name


@pytest.mark.parametrize(("change", "expected"), [
    (lambda env, tmp: env.pop("SCRAMBLER_WORKER_SOURCE_WINDOW_COMPAT"), "SCRAMBLER_WORKER_SOURCE_WINDOW_COMPAT must be 1"),
    (lambda env, tmp: env.update(SCRAMBLER_ENABLED="0"), "SCRAMBLER_ENABLED must be 1"),
    (lambda env, tmp: env.pop("CONTENT_LAB_ORIGINALITY_USAGE_PATH"), "CONTENT_LAB_ORIGINALITY_USAGE_PATH is not set"),
    (lambda env, tmp: Path(env["CONTENT_LAB_ORIGINALITY_USAGE_PATH"]).unlink(), "file is missing"),
    (lambda env, tmp: os.utime(env["CONTENT_LAB_ORIGINALITY_USAGE_PATH"], (time.time() - 30 * 3600,) * 2), "is stale"),
    (lambda env, tmp: (Path(env["CONTENT_LAB_ORIGINALITY_USAGE_PATH"]).parent / "origins.json").unlink(), "origins map is missing"),
    (lambda env, tmp: os.utime(Path(env["CONTENT_LAB_ORIGINALITY_USAGE_PATH"]).parent / "origins.json", (time.time() - 30 * 3600,) * 2), "origins map is stale"),
    (lambda env, tmp: (Path(env["CONTENT_LAB_ORIGINALITY_USAGE_PATH"]).parent / "origins.json").write_text("[]"), "originality feed does not load"),
    (lambda env, tmp: env.update(SCRAMBLER_HISTORY_PATH=str(tmp / "elsewhere.json")), "is not on the durable volume"),
    (lambda env, tmp: env.update(SCRAMBLER_HISTORY_PATH="relative.json"), "must be an absolute path"),
    (lambda env, tmp: env.pop("SCRAMBLER_FEED_R2_BUCKET"), "SCRAMBLER_FEED_R2_BUCKET is not set"),
    (lambda env, tmp: env.pop("SCRAMBLER_FEED_R2_PREFIX"), "SCRAMBLER_FEED_R2_PREFIX is not set"),
    (lambda env, tmp: env.update(SCRAMBLER_FAST_SHARE="lots"), "SCRAMBLER_FAST_SHARE must be a number"),
    (lambda env, tmp: env.update(SCRAMBLER_PAGES="freddy.2am"), "'freddy.2am' is not a roster integration_id"),
])
def test_preflight_names_each_missing_setting(tmp_path, change, expected):
    env = _pilot_env(tmp_path)
    change(env, tmp_path)
    problems = pf.preflight_problems(env)
    assert any(expected in problem for problem in problems), problems


def _run_cli(env: dict) -> subprocess.CompletedProcess:
    clean = {key: value for key, value in os.environ.items()
             if not key.startswith(("SCRAMBLER_", "CONTENT_LAB_ORIGINALITY_", "RAILWAY_"))}
    return subprocess.run([sys.executable, str(ROOT / "tools" / "scrambler_report.py"), "--preflight"],
                          env={**clean, **env}, capture_output=True, text=True, timeout=60)


def test_preflight_cli_exit_codes(tmp_path):
    ready = _run_cli(_pilot_env(tmp_path))
    assert ready.returncode == 0, ready.stdout + ready.stderr
    assert json.loads(ready.stdout) == {"problems": [], "ready": True,
                                         "schema": "content-lab.scrambler-preflight.v1"}
    missing = _run_cli({"SCRAMBLER_ENABLED": "1", "SCRAMBLER_PAGES": PILOT_PAGES})
    assert missing.returncode == 1
    report = json.loads(missing.stdout)
    assert report["ready"] is False
    assert any("SCRAMBLER_WORKER_SOURCE_WINDOW_COMPAT" in p for p in report["problems"])


def test_startup_log_line(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="scrambler.preflight")
    assert pf.log_startup_preflight(env={}) == []
    assert "scrambler preflight: OFF" in caplog.text
    caplog.clear()
    problems = pf.log_startup_preflight(env={"SCRAMBLER_ENABLED": "1", "SCRAMBLER_PAGES": PILOT_PAGES})
    assert problems
    assert "scrambler preflight: NOT READY" in caplog.text
    assert "SCRAMBLER_WORKER_SOURCE_WINDOW_COMPAT" in caplog.text
    caplog.clear()
    assert pf.log_startup_preflight(env=_pilot_env(tmp_path)) == []
    assert "scrambler preflight: ready" in caplog.text


def test_preflight_is_not_on_any_http_path():
    """No new response field: the HTTP routers never import the preflight."""
    for router in (ROOT / "routers").glob("*.py"):
        assert "scrambler_preflight" not in router.read_text(), router


# ---------------------------------------------------------------------------
# MED-1: refused cuts are skipped and released; vetted cuts deliver
# ---------------------------------------------------------------------------

def _enqueue(client, monkeypatch, tmp_path, quantity, key):
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    monkeypatch.setenv("SCRAMBLER_TREATMENT_WEIGHTS", "none:1,split_swap:0,camera:0,grade:0")
    response = client.post("/api/control-plane/v1/jobs", json=job_body(quantity), headers=headers(key))
    assert response.status_code == 200, response.json()
    return response.json()["jobId"]


def _states(job_id):
    rows = sorted((r for r in scrambler_history_rows(PAGE_ID) if r["jobId"] == job_id),
                  key=lambda r: r["cutIndex"])
    return [r["state"] for r in rows]


class _RefuseFirstWindow:
    def __init__(self, inner):
        self.inner, self.calls = inner, 0

    def canonical_origin_window(self, *args, **kwargs):
        return self.inner.canonical_origin_window(*args, **kwargs)

    def window_allowed(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return False, "test_shown_recently"
        return self.inner.window_allowed(*args, **kwargs)


@pytest.mark.asyncio
async def test_originality_refused_cut_is_skipped_and_others_deliver(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    job_id = _enqueue(client, monkeypatch, tmp_path, 2, "pilot-refuse-originality")
    lane = _wire_executor(monkeypatch, tmp_path, [job_id])
    proxy = _RefuseFirstWindow(so.load_originality_index())
    monkeypatch.setattr(cp, "load_originality_index_cached", lambda: proxy)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert len(job["clips"]) == 1 and len(lane["renders"]) == 1
    assert job["clips"][0]["scrambler"]["cutIndex"] == 1
    assert _states(job_id) == [RELEASED, RENDERED]
    released = [r for r in scrambler_history_rows(PAGE_ID) if r["jobId"] == job_id][0]
    assert released["releaseReason"].startswith(
        "cut_refused:scrambler_execution_originality_refused:test_shown_recently")


@pytest.mark.asyncio
async def test_duration_refused_cut_is_skipped_and_released(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    job_id = _enqueue(client, monkeypatch, tmp_path, 2, "pilot-refuse-duration")
    lane = _wire_executor(monkeypatch, tmp_path, [job_id])
    monkeypatch.setattr(cp, "probe_source_output_duration_seconds",
                        lambda path: 12.0 if Path(path).stem.endswith("0001") else
                        _job_scrambler_records(job_id, 0)["lengthMs"] / 1000.0)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert [clip["scrambler"]["cutIndex"] for clip in job["clips"]] == [0]
    assert _states(job_id) == [RENDERED, RELEASED]
    assert len(lane["renders"]) == 2
    assert not Path(lane["renders"][1]["dst"]).exists()   # the refused output is removed


@pytest.mark.asyncio
async def test_renderability_refusal_skips_only_that_cut(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    job_id = _enqueue(client, monkeypatch, tmp_path, 2, "pilot-refuse-render")
    _wire_executor(monkeypatch, tmp_path, [job_id])
    real = cp.assert_cut_renderable

    def refuse_first(page, job, cut_index, **kwargs):
        if cut_index == 0:
            raise s.ScramblerExhausted("scrambler_render_consecutive_length")
        return real(page, job, cut_index, **kwargs)

    monkeypatch.setattr(cp, "assert_cut_renderable", refuse_first)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "completed", job.get("error")
    assert _states(job_id) == [RELEASED, RENDERED]


@pytest.mark.asyncio
async def test_every_cut_refused_fails_with_the_first_reason(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    job_id = _enqueue(client, monkeypatch, tmp_path, 2, "pilot-refuse-all")
    lane = _wire_executor(monkeypatch, tmp_path, [job_id])

    def refuse(*_args, **_kwargs):
        raise s.ScramblerExhausted("scrambler_reverse_render_day_exhausted")

    monkeypatch.setattr(cp, "assert_cut_renderable", refuse)
    await cp._run_dossier_source(job_id)
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "failed"
    assert job["error"] == "scrambler_reverse_render_day_exhausted"
    assert lane["renders"] == []
    assert _states(job_id) == [RELEASED, RELEASED]


def _failed_job_with_rendered_cut(client, monkeypatch, tmp_path):
    job_id = _enqueue(client, monkeypatch, tmp_path, 2, "pilot-undelivered")
    _wire_executor(monkeypatch, tmp_path, [job_id])
    calls = []

    async def render(src, dst, correction, **kwargs):
        calls.append(dst)
        if len(calls) > 1:
            raise RuntimeError("second cut encode failed")
        Path(dst).write_bytes(b"first-cut")

    monkeypatch.setattr(cp, "run_color_correct", render)
    return job_id


@pytest.mark.asyncio
async def test_undelivered_rendered_hold_is_released_after_the_grace_period(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    job_id = _failed_job_with_rendered_cut(client, monkeypatch, tmp_path)
    await cp._run_dossier_source(job_id)
    assert cp._load_jobs()["jobs"][job_id]["status"] == "failed"
    assert _states(job_id) == [RENDERED, RELEASED]
    index = so.load_originality_index()
    assert len(scrambler_pending_windows(index, "another-job")) == 1
    rendered_at = next(r for r in scrambler_history_rows(PAGE_ID) if r["state"] == RENDERED)["renderedAt"]
    at = datetime.fromisoformat(rendered_at)
    status = lambda job: (cp._load_jobs()["jobs"].get(job) or {}).get("status")  # noqa: E731
    # Inside the grace period, or for a job that delivered, nothing moves.
    assert s.release_undelivered_holds(status, now=(at + timedelta(hours=23)).isoformat()) == 0
    assert s.release_undelivered_holds(lambda job: "completed",
                                       now=(at + timedelta(hours=25)).isoformat()) == 0
    assert s.release_undelivered_holds(lambda job: "running",
                                       now=(at + timedelta(hours=25)).isoformat()) == 0
    assert len(scrambler_pending_windows(index, "another-job")) == 1
    # A failed job's rendered cut can never deliver: its hold goes.
    assert s.release_undelivered_holds(status, now=(at + timedelta(hours=25)).isoformat()) == 1
    assert scrambler_pending_windows(index, "another-job") == ()
    row = next(r for r in scrambler_history_rows(PAGE_ID) if r["jobId"] == job_id and r["cutIndex"] == 0)
    assert row["state"] == RENDERED            # combo and reverse credit unchanged
    assert row["holdRelease"]["reason"] == s.UNDELIVERED_HOLD_REASON


@pytest.mark.asyncio
async def test_next_enqueue_releases_undelivered_holds(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    job_id = _failed_job_with_rendered_cut(client, monkeypatch, tmp_path)
    await cp._run_dossier_source(job_id)
    monkeypatch.setattr(s, "UNDELIVERED_HOLD_TIMEOUT_S", 0.0)
    response = client.post("/api/control-plane/v1/jobs", json=job_body(1),
                           headers=headers("pilot-undelivered-next"))
    assert response.status_code == 200, response.json()
    row = next(r for r in scrambler_history_rows(PAGE_ID) if r["jobId"] == job_id and r["cutIndex"] == 0)
    assert row["holdRelease"]["reason"] == s.UNDELIVERED_HOLD_REASON


def test_hold_release_field_is_validated(tmp_path):
    path = tmp_path / "history.json"
    s.reserve_job_cuts("acct:hold", "job", 1, path=path)
    data = json.loads(path.read_text())
    data["pages"]["acct:hold"][0]["holdRelease"] = {"at": "2026-10-29T12:00:00+00:00",
                                                     "reason": s.UNDELIVERED_HOLD_REASON}
    path.write_text(json.dumps(data))
    with pytest.raises(s.ScramblerHistoryCorrupt, match="holdRelease_invalid"):
        s.scrambler_history_rows("acct:hold", path=path)   # only a RENDERED row may carry it


# ---------------------------------------------------------------------------
# MED-2: parse off the event loop, before the jobs lock, once per job
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_feed_parse_is_off_loop_before_the_lock_and_once_per_job(lab, monkeypatch, tmp_path):
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    monkeypatch.setenv("SCRAMBLER_TREATMENT_WEIGHTS", "none:1,split_swap:0,camera:0,grade:0")
    events = []
    real_load, real_lock = so.load_originality_index, cp.lock_for

    def counting_load(*args, **kwargs):
        events.append(("load", threading.get_ident()))
        return real_load(*args, **kwargs)

    def recording_lock(path, *args, **kwargs):
        if Path(path) == Path(cp._jobs_path()):
            events.append(("jobs_lock", threading.get_ident()))
        return real_lock(path, *args, **kwargs)

    monkeypatch.setattr(so, "load_originality_index", counting_load)
    monkeypatch.setattr(cp, "lock_for", recording_lock)
    response = client.post("/api/control-plane/v1/jobs", json=job_body(3),
                           headers=headers("pilot-offloop"))
    assert response.status_code == 200, response.json()
    kinds = [kind for kind, _ in events]
    assert "load" in kinds and "jobs_lock" in kinds
    assert kinds.index("load") < kinds.index("jobs_lock")
    load_thread = dict(events)["load"]
    lock_thread = next(ident for kind, ident in events if kind == "jobs_lock")
    assert load_thread != lock_thread           # parsed in a worker thread
    job_id = response.json()["jobId"]
    _wire_executor(monkeypatch, tmp_path, [job_id])
    await cp._run_dossier_source(job_id)
    assert cp._load_jobs()["jobs"][job_id]["status"] == "completed"
    # One parse served the enqueue and all three cuts of the job.
    assert [kind for kind, _ in events].count("load") == 1


def test_cached_index_reparses_on_change_and_still_refuses_stale(tmp_path, monkeypatch):
    feed = tmp_path / "feed"
    feed.mkdir()
    (feed / "usage.jsonl").write_text("")
    (feed / "origins.json").write_text("{}")
    retired = feed / "retired.json"
    retired.write_text("[]")
    monkeypatch.setenv("CONTENT_LAB_ORIGINALITY_USAGE_PATH", str(feed / "usage.jsonl"))
    monkeypatch.setenv("CONTENT_LAB_ORIGINALITY_RETIRED_PATH", str(retired))
    monkeypatch.delenv("CONTENT_LAB_ORIGINALITY_ORIGINS_PATH", raising=False)
    calls = []
    real = so.load_originality_index
    monkeypatch.setattr(so, "load_originality_index",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    first = so.load_originality_index_cached()
    assert so.load_originality_index_cached() is first and len(calls) == 1
    os.utime(feed / "origins.json", (time.time() + 5,) * 2)
    assert so.load_originality_index_cached() is not first and len(calls) == 2
    # Even an unchanged, in-age cache entry refuses once the ledger is stale.
    monkeypatch.setattr(so, "INDEX_CACHE_MAX_AGE_S", 10.0 ** 9)
    so.load_originality_index_cached()
    later = time.time() + so.USAGE_STALE_AFTER_S + 3600
    with pytest.raises(so.ScramblerOriginalityUnavailable, match="usage_ledger_stale"):
        so.load_originality_index_cached(now_epoch=later)


# ---------------------------------------------------------------------------
# MED-3: page ids are the roster integration_id, matched exactly
# ---------------------------------------------------------------------------

def test_pilot_page_ids_match_exactly(monkeypatch):
    from services.notion_pages import mint_integration_id

    monkeypatch.setenv("SCRAMBLER_ENABLED", "1")
    monkeypatch.setenv("SCRAMBLER_WORKER_SOURCE_WINDOW_COMPAT", "1")
    monkeypatch.setenv("SCRAMBLER_PAGES", PILOT_PAGES)
    # The roster id of the vault handle freddy.2am is acct:freddy-2am.
    assert mint_integration_id("freddy.2am") == "acct:freddy-2am"
    assert s.scrambler_gate("acct:freddy-2am") == (True, "scrambler_on")
    for other in ("freddy.2am", "Freddy.2am", "acct:freddy.2am", "acct:Freddy-2am"):
        assert s.scrambler_gate(other) == (False, "scrambler_not_listed"), other
    for page in PILOT_PAGES.split(","):
        assert pf.ROSTER_PAGE_ID_RE.match(page), page
    for bad in ("freddy.2am", "Freddy.2am", "acct:freddy.2am"):
        assert not pf.ROSTER_PAGE_ID_RE.match(bad), bad


def test_docs_carry_the_exact_pilot_env():
    doc = (ROOT / "docs" / "SCRAMBLER.md").read_text()
    for line in ("SCRAMBLER_ENABLED=1", f"SCRAMBLER_PAGES={PILOT_PAGES}", "SCRAMBLER_FAST_SHARE=0",
                 "SCRAMBLER_WORKER_SOURCE_WINDOW_COMPAT=1",
                 "CONTENT_LAB_ORIGINALITY_USAGE_PATH=/app/projects/originality/usage.jsonl",
                 "SCRAMBLER_FEED_R2_BUCKET=rt-content-buckets",
                 "SCRAMBLER_FEED_R2_PREFIX=originality/feed/",
                 "SCRAMBLER_HISTORY_PATH=/app/projects/scrambler-history.json"):
        assert f"    {line}\n" in doc, line
