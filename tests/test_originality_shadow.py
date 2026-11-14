"""The originality shadow over the source cut planner (gap G39, shadow only).

The planner never refuses: `plan_source_cuts` keeps choosing exactly the same
windows while `services/originality_shadow.py` logs (logger
``content_lab.originality_shadow``) every chosen window that would reuse
footage the network has already shown, according to the daily originality
usage summary. The summary is PRIVATE (it maps every page handle to its
footage — originality + pixel89 ruling 2026-10-28): it is read from the file
named by ``CONTENT_LAB_ORIGINALITY_SUMMARY_PATH``; a
``CONTENT_LAB_ORIGINALITY_SUMMARY_URL`` source is honoured only with the
auth token in ``CONTENT_LAB_ORIGINALITY_SUMMARY_TOKEN`` (never logged) and
without a token it never fetches. All env unset -> the feature is fully off:
zero file reads, zero network calls.
"""
from __future__ import annotations

from dataclasses import replace
import asyncio
import builtins
import json
import logging
import os
from pathlib import Path
import threading
import time

import httpx
import pytest

from services import originality_shadow
from services.control_plane_sources import (
    SourceCut,
    plan_source_cuts,
    resolve_source_recipe,
)
from tests.test_control_plane_source_execution import PAGE_ID, publication
from tests.source_dna_fixture import isolate_source_dna_registry

NOW = 1_800_014_400.0  # 2027-01-15T12:00:00Z, fixed for every test here
NOW_ISO = "2027-01-15T12:00:00+00:00"
OTHER_PAGE = "acct:stay-inmy-lane"
SUMMARY_URL = "https://unit.test/assets/originality/usage-summary.json"
SUMMARY_TOKEN = "unit-test-secret-token"
ORIGIN = "1uMmco2zNZc"
# A small usage summary in the REAL capture's shape (real file sha256
# bd49683504f634a5..., 2026-10-29T00:53:32Z: duration_s can be null, rows
# are [start_s, end_s, page, shows, last_shown_at] with -0400/-0500 offsets,
# masters rows {origin_id, offset_s|null}); page handles and the second
# origin id are redacted so no real page-to-footage mapping ships in the repo.
FIXTURE = Path(__file__).parent / "fixtures" / "originality-usage-summary.v1.json"


@pytest.fixture(autouse=True)
def _isolated_source_dna(monkeypatch, tmp_path):
    """Retired chase/BTL libraries are deleted, so source-dna tests run against
    an isolated copy of the registry plus the non-retired dirt-bike fixture."""
    isolate_source_dna_registry(monkeypatch, tmp_path)


@pytest.fixture(autouse=True)
def _offline_shipstream_manifest(monkeypatch):
    """The same offline vault the execution tests pin (their autouse fixture
    is module-scoped): any manifest read answers unreachable instead of
    leaving the process."""
    import services.shipstream_source_manifest as source_manifest

    def unreachable(_url):
        raise source_manifest.ShipStreamSourceUnavailable(
            "ShipStream vault is offline in tests")

    monkeypatch.setattr(source_manifest, "_fetch_manifest", unreachable)


def _master(sha: str, *, origin: dict | None = None, duration_ms: int = 600_000,
            offset_ms: int = 0):
    from services.source_dna_registry import MasterSource
    return MasterSource(
        f"src-{sha[:6]}", sha, 1_000, f"{sha}.mp4", "video/mp4",
        f"vault/unit/{sha}.mp4", duration_ms, offset_ms,
        {"sourceUrl": "https://unit.test/a.mp4", "acquiredAt": NOW_ISO,
         "authority": "unit"},
        origin,
    )


def _origin_recipe(masters) -> object:
    base = resolve_source_recipe(publication())
    assert base is not None
    return replace(base, masters=tuple(masters))


def _summary(rows, *, duration_s=None, generated_at=NOW_ISO, masters=None):
    origin = {"seconds": rows}
    if duration_s is not None:
        origin["duration_s"] = duration_s
    return {"generated_at": generated_at, "origins": {ORIGIN: origin},
            "masters": masters or {}}


def _row(start, end, page=OTHER_PAGE, shows=1, last_shown_at=None):
    return [start, end, page, shows, last_shown_at or NOW_ISO]


@pytest.fixture
def shadow(monkeypatch):
    """URL env on WITH a token (the ruling: no unauthenticated fetch), frozen
    clock, recorder fetch; returns the recorder box."""
    monkeypatch.setenv(originality_shadow.SUMMARY_URL_ENV, SUMMARY_URL)
    monkeypatch.setenv(originality_shadow.SUMMARY_TOKEN_ENV, SUMMARY_TOKEN)
    monkeypatch.setattr(originality_shadow, "_now", lambda: NOW)
    originality_shadow.reset_shadow_state()
    box = {"fetches": 0, "tokens": [], "response": None, "raise": None}

    def fetch(url, token):
        box["fetches"] += 1
        box["tokens"].append(token)
        if box["raise"] is not None:
            raise box["raise"]
        return box["response"]

    monkeypatch.setattr(originality_shadow, "_fetch_summary", fetch)
    yield box
    originality_shadow.reset_shadow_state()


@pytest.fixture
def shadow_file(monkeypatch, tmp_path):
    """File env on (the primary source), frozen clock; returns (path, box)
    where box counts real reads via a wrapper around _read_summary_file."""
    path = tmp_path / "usage-summary.json"
    path.write_text(FIXTURE.read_text())
    monkeypatch.setenv(originality_shadow.SUMMARY_PATH_ENV, str(path))
    monkeypatch.delenv(originality_shadow.SUMMARY_URL_ENV, raising=False)
    monkeypatch.delenv(originality_shadow.SUMMARY_TOKEN_ENV, raising=False)
    monkeypatch.setattr(originality_shadow, "_now", lambda: NOW)
    originality_shadow.reset_shadow_state()
    real_read = originality_shadow._read_summary_file
    box = {"reads": 0}

    def counting_read(location):
        box["reads"] += 1
        return real_read(location)

    monkeypatch.setattr(originality_shadow, "_read_summary_file", counting_read)
    yield path, box
    originality_shadow.reset_shadow_state()


def _logged(caplog, event):
    return [
        json.loads(record.message) for record in caplog.records
        if record.name == "content_lab.originality_shadow"
        and record.getMessage().startswith("{")
        and json.loads(record.message).get("event") == event
    ]


def _observe(recipe, cuts, page_id=PAGE_ID, job_id=None):
    originality_shadow.observe_planned_cuts(recipe, cuts, page_id=page_id,
                                            job_id=job_id)


# ---------------------------------------------------------------------------
# Shadow off: every source env unset -> zero file reads AND zero network
# calls, plans unchanged.
# ---------------------------------------------------------------------------

def test_shadow_off_env_unset_makes_zero_fetches_and_identical_plans(monkeypatch):
    box = {"fetches": 0, "reads": 0}

    def counting_fetch(url, token):
        box["fetches"] += 1
        return b"{}"

    def counting_read(path):
        box["reads"] += 1
        return b"{}"

    monkeypatch.delenv(originality_shadow.SUMMARY_URL_ENV, raising=False)
    monkeypatch.delenv(originality_shadow.SUMMARY_TOKEN_ENV, raising=False)
    monkeypatch.delenv(originality_shadow.SUMMARY_PATH_ENV, raising=False)
    monkeypatch.setattr(originality_shadow, "_fetch_summary", counting_fetch)
    monkeypatch.setattr(originality_shadow, "_read_summary_file", counting_read)
    originality_shadow.reset_shadow_state()
    recipe = _origin_recipe([_master("a" * 64)])
    planned = plan_source_cuts(recipe, 5, set())
    assert planned
    assert plan_source_cuts(recipe, 5, set(), shadow_page_id=PAGE_ID,
                            shadow_job_id="job-1") == planned
    # Every env unset -> the shadow is fully off: no file reads, no fetches.
    assert box["fetches"] == 0 and box["reads"] == 0
    counts = originality_shadow.shadow_counts()
    assert counts["would_reuse"] == 0 and counts["unavailable"] == 0
    assert counts["cross_page"] == 0 and counts["unknown_origin"] == 0
    originality_shadow.reset_shadow_state()


def test_shadow_never_changes_the_plan(caplog, shadow):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    recipe = _origin_recipe([
        _master("a" * 64, origin={"originSourceId": ORIGIN,
                                  "originStartMs": 600_000,
                                  "originEndMs": 1_200_000}, offset_ms=600_000),
        _master("b" * 64),
    ])
    # The whole origin shown recently on another page: every cut of master a
    # would be reused footage, yet the plan must not move by a single window.
    shadow["response"] = json.dumps(_summary([_row(600, 1_200)])).encode()
    planned = plan_source_cuts(recipe, 5, set(), shadow_page_id=PAGE_ID,
                               shadow_job_id="job-1")
    assert _logged(caplog, "would_reuse")
    assert [(c.master.sha256, c.start_ms, c.duration_ms, c.slot_id) for c in planned] == \
        [(c.master.sha256, c.start_ms, c.duration_ms, c.slot_id)
         for c in plan_source_cuts(recipe, 5, set())]


# ---------------------------------------------------------------------------
# Shadow on: one reason per crafted case, nothing for a fresh window.
# ---------------------------------------------------------------------------

def test_logs_each_reason_and_nothing_for_a_fresh_window(caplog, shadow):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    recipe = _origin_recipe([
        # cross_page: shown on another page inside 90 d.
        _master("a" * 64, origin={"originSourceId": ORIGIN,
                                  "originStartMs": 600_000,
                                  "originEndMs": 1_200_000}, offset_ms=600_000),
        # shows_cap: this page, a covered second already at 2 shows in 30 d.
        _master("c" * 64, origin={"originSourceId": ORIGIN,
                                  "originStartMs": 1_200_000,
                                  "originEndMs": 1_800_000}, offset_ms=1_200_000),
        # gap_lt_7d: this page, one show three days ago.
        _master("d" * 64, origin={"originSourceId": ORIGIN,
                                  "originStartMs": 1_800_000,
                                  "originEndMs": 2_400_000}, offset_ms=1_800_000),
        # fresh: nothing shown inside its window.
        _master("e" * 64, origin={"originSourceId": ORIGIN,
                                  "originStartMs": 2_400_000,
                                  "originEndMs": 3_000_000}, offset_ms=2_400_000),
        # no origin anywhere: unknown_origin.
        _master("f" * 64),
    ])
    three_days_ago = "2027-01-12T12:00:00+00:00"
    shadow["response"] = json.dumps(_summary([
        _row(605, 615, page=OTHER_PAGE),
        _row(1_205, 1_215, page=PAGE_ID, shows=2),
        _row(1_805, 1_815, page=PAGE_ID, shows=1, last_shown_at=three_days_ago),
    ])).encode()
    by_master = {master.sha256: master for master in recipe.masters}

    def cut(sha, start_ms, duration_ms=6_000):
        return SourceCut(by_master[sha], start_ms, duration_ms, True)

    _observe(recipe, [
        cut("a" * 64, 5_000), cut("c" * 64, 5_000), cut("d" * 64, 5_000),
        cut("e" * 64, 5_000), cut("f" * 64, 5_000),
    ], job_id="job-2")
    events = _logged(caplog, "would_reuse")
    reasons = {event["master_sha"]: event["reason"] for event in events}
    assert reasons == {
        "a" * 64: "cross_page",
        "c" * 64: "shows_cap",
        "d" * 64: "gap_lt_7d",
        "f" * 64: "unknown_origin",
    }
    # Every event carries exactly the structured fields the ledger reads.
    for event in events:
        assert set(event) == {"event", "page", "origin_id", "window", "reason",
                              "master_sha", "job_id", "summary_generated_at"}
        assert event["page"] == PAGE_ID
        assert event["job_id"] == "job-2"
        assert event["summary_generated_at"] == NOW_ISO
    assert _logged(caplog, "would_reuse")[0]["window"] == [605.0, 611.0]
    for event in events:
        if event["window"] is not None:
            assert event["window"][0] < event["window"][1]
    assert _logged(caplog, "would_reuse")[0]["window"] == [605.0, 611.0]
    unknown = next(e for e in events if e["reason"] == "unknown_origin")
    assert unknown["origin_id"] is None
    counts = originality_shadow.shadow_counts()
    assert counts["would_reuse"] == 4
    assert counts["cross_page"] == 1 and counts["shows_cap"] == 1
    assert counts["gap_lt_7d"] == 1 and counts["unknown_origin"] == 1


def test_unknown_origin_resolved_via_the_summary_masters_table(caplog, shadow):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    master = _master("a" * 64)
    recipe = _origin_recipe([master])
    shadow["response"] = json.dumps(_summary(
        [_row(100, 109, page=OTHER_PAGE)],
        masters={"a" * 64: {"origin_id": ORIGIN, "offset_s": 100.0}},
    )).encode()
    _observe(recipe, [SourceCut(master, 0, 9_000, True)], job_id="job-masters")
    events = _logged(caplog, "would_reuse")
    assert [e["reason"] for e in events] == ["cross_page"]
    assert events[0]["window"] == [100.0, 109.0]


# ---------------------------------------------------------------------------
# Unavailable summaries: one log per cache period, planning unchanged.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["stale_27h", "http_500", "timeout",
                                  "invalid_json", "oversize"])
def test_unavailable_summary_logs_once_and_never_raises_or_blocks(caplog, shadow,
                                                                  mode):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    recipe = _origin_recipe([_master(
        "a" * 64, origin={"originSourceId": ORIGIN, "originStartMs": 0,
                          "originEndMs": 600_000})])
    if mode == "stale_27h":
        shadow["response"] = json.dumps(_summary(
            [_row(0, 600)], generated_at="2027-01-14T09:00:00+00:00")).encode()
    elif mode == "http_500":
        shadow["raise"] = originality_shadow.SummaryUnavailable("http 500")
    elif mode == "timeout":
        shadow["raise"] = originality_shadow.SummaryUnavailable(httpx.ReadTimeout("t"))
    elif mode == "invalid_json":
        shadow["response"] = b"{not json"
    else:
        shadow["raise"] = originality_shadow.SummaryUnavailable("too large")
    planned = plan_source_cuts(recipe, 3, set(), shadow_page_id=PAGE_ID,
                               shadow_job_id="job-3")
    assert planned
    # Planning is unchanged: the same windows the shadow-less planner gives.
    assert [(c.start_ms, c.duration_ms) for c in planned] == \
        [(c.start_ms, c.duration_ms) for c in plan_source_cuts(recipe, 3, set())]
    assert len(_logged(caplog, "unavailable")) == 1
    # Cached unavailable: no second fetch, no second log inside the period,
    # and no per-cut would_reuse noise.
    plan_source_cuts(recipe, 3, set(), shadow_page_id=PAGE_ID)
    assert shadow["fetches"] == 1
    assert len(_logged(caplog, "unavailable")) == 1
    assert _logged(caplog, "would_reuse") == []


def test_a_fresh_summary_is_fetched_once_and_cached_15_minutes(shadow, monkeypatch):
    shadow["response"] = json.dumps(_summary([])).encode()
    recipe = _origin_recipe([_master(
        "a" * 64, origin={"originSourceId": ORIGIN, "originStartMs": 0,
                          "originEndMs": 600_000})])
    assert plan_source_cuts(recipe, 1, set())
    assert plan_source_cuts(recipe, 1, set())
    assert shadow["fetches"] == 1
    clock = {"t": NOW}
    monkeypatch.setattr(originality_shadow, "_now", lambda: clock["t"])
    clock["t"] = NOW + 14 * 60
    assert plan_source_cuts(recipe, 1, set())
    assert shadow["fetches"] == 1
    clock["t"] = NOW + 15 * 60 + 1
    assert plan_source_cuts(recipe, 1, set())
    assert shadow["fetches"] == 2


# ---------------------------------------------------------------------------
# Speed-adjusted windows and the edge cases from the brief.
# ---------------------------------------------------------------------------

def test_speed_adjusted_overlap_matches_the_brief_example(caplog, shadow):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    # A 0.5x delivered 9 s cut is planned as a 4.5 s master window 30 s into
    # a master whose origin starts at 600 s: it covers origin 630-634.5 and
    # overlaps a shown second at 634.
    master = _master("a" * 64, origin={"originSourceId": ORIGIN,
                                       "originStartMs": 600_000,
                                       "originEndMs": 1_200_000},
                     offset_ms=600_000)
    recipe = _origin_recipe([master])
    shadow["response"] = json.dumps(
        _summary([_row(634, 640, page=OTHER_PAGE)])).encode()
    _observe(recipe, [SourceCut(master, 30_000, 4_500, True)], job_id="job-speed")
    events = _logged(caplog, "would_reuse")
    assert [e["reason"] for e in events] == ["cross_page"]
    assert events[0]["window"] == [630.0, 634.5]


def test_null_origin_start_touches_the_whole_origin(caplog, shadow):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    master = _master("a" * 64, origin={"originSourceId": ORIGIN,
                                       "originStartMs": None,
                                       "originEndMs": None})
    recipe = _origin_recipe([master])
    shadow["response"] = json.dumps(_summary(
        [_row(100, 110, page=OTHER_PAGE)], duration_s=600)).encode()
    _observe(recipe, [SourceCut(master, 0, 6_000, True)], job_id="job-null-start")
    events = _logged(caplog, "would_reuse")
    assert [e["reason"] for e in events] == ["cross_page"]
    assert events[0]["window"] == [0.0, 600.0]


def test_origin_missing_duration_without_a_start_is_unbounded(caplog, shadow):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    master = _master("a" * 64, origin={"originSourceId": ORIGIN,
                                       "originStartMs": None,
                                       "originEndMs": None})
    recipe = _origin_recipe([master])
    shadow["response"] = json.dumps(_summary(
        [_row(99_999, 1_000_000, page=OTHER_PAGE)])).encode()  # no duration_s
    _observe(recipe, [SourceCut(master, 0, 6_000, True)], job_id="job-unbounded")
    events = _logged(caplog, "would_reuse")
    assert [e["reason"] for e in events] == ["cross_page"]
    # An open end is null in the JSON log, never Infinity (invalid JSON).
    assert events[0]["window"] == [0.0, None]


def test_partitioned_masters_of_one_origin_do_not_trip_cross_page(caplog, shadow):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    # Two masters of one origin partitioned into non-overlapping ranges (the
    # RideScapes x4 shape): the other page's shown range is 10-500 s, this
    # page's master starts at origin 600 s, so its windows are fresh even
    # though the origin id is shared.
    master = _master("b" * 64, origin={"originSourceId": ORIGIN,
                                       "originStartMs": 600_000,
                                       "originEndMs": 1_200_000},
                     offset_ms=600_000)
    recipe = _origin_recipe([
        _master("a" * 64, origin={"originSourceId": ORIGIN, "originStartMs": 0,
                                  "originEndMs": 600_000}, offset_ms=0),
        master,
    ])
    shadow["response"] = json.dumps(
        _summary([_row(10, 500, page=OTHER_PAGE)])).encode()
    _observe(recipe, [SourceCut(master, 0, 6_000, True),
                      SourceCut(master, 30_000, 6_000, True)])
    assert _logged(caplog, "would_reuse") == []


@pytest.mark.parametrize("offset", ["-04:00", "-0400", "Z"])
def test_last_shown_at_timezone_offsets_are_parsed(caplog, shadow, offset):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    master = _master("a" * 64, origin={"originSourceId": ORIGIN,
                                       "originStartMs": 0,
                                       "originEndMs": 600_000})
    recipe = _origin_recipe([master])
    shadow["response"] = json.dumps(_summary(
        [_row(0, 600, page=OTHER_PAGE,
              last_shown_at=f"2027-01-15T09:00:00{offset}")])).encode()
    _observe(recipe, [SourceCut(master, 0, 6_000, True)], job_id="job-tz")
    assert [e["reason"] for e in _logged(caplog, "would_reuse")] == ["cross_page"]


def test_shows_and_windows_are_counted_per_page(caplog, shadow):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    # Many shows on OTHER pages, but all outside the 90 d window (2026-10-02
    # is 105 d before NOW): nothing.
    master = _master("a" * 64, origin={"originSourceId": ORIGIN,
                                       "originStartMs": 0,
                                       "originEndMs": 600_000})
    recipe = _origin_recipe([master])
    shadow["response"] = json.dumps(_summary([
        _row(0, 600, page=OTHER_PAGE, shows=2,
             last_shown_at="2026-10-01T00:00:00+00:00"),
        _row(0, 600, page="acct:third", shows=5,
             last_shown_at="2026-10-02T00:00:00+00:00"),
    ])).encode()
    _observe(recipe, [SourceCut(master, 0, 6_000, True)], job_id="job-count-a")
    assert _logged(caplog, "would_reuse") == []
    # The same-second show cap applies to THIS page only: 2 shows on another
    # page inside 30 d is cross_page (page differs), never shows_cap.
    shadow["response"] = json.dumps(_summary([
        _row(0, 600, page=OTHER_PAGE, shows=2,
             last_shown_at="2027-01-10T00:00:00+00:00"),
    ])).encode()
    originality_shadow.reset_shadow_state()
    _observe(recipe, [SourceCut(master, 0, 6_000, True)], job_id="job-count-b")
    assert [e["reason"] for e in _logged(caplog, "would_reuse")] == ["cross_page"]


def test_huge_seconds_lists_stay_under_the_planning_budget(shadow):
    rows = [[i, i + 1, OTHER_PAGE if i % 2 else PAGE_ID, 1 + i % 3, NOW_ISO]
            for i in range(10_000)]
    shadow["response"] = json.dumps(_summary(rows)).encode()
    master = _master("a" * 64, origin={"originSourceId": ORIGIN,
                                       "originStartMs": 0,
                                       "originEndMs": 600_000})
    recipe = _origin_recipe([master])
    cuts = plan_source_cuts(recipe, 10, set())
    assert len(cuts) == 10
    best = None
    for _ in range(3):
        start = time.perf_counter()
        originality_shadow.observe_planned_cuts(recipe, cuts, page_id=PAGE_ID)
        spent = time.perf_counter() - start
        best = spent if best is None else min(best, spent)
    assert best < 0.050, f"shadow evaluation took {best * 1000:.1f} ms for 10k rows"


@pytest.mark.asyncio
async def test_on_the_event_loop_the_shadow_reads_only_a_warm_cache(shadow):
    # Inside a running loop the shadow may only read a warm cache: fetching
    # would block the ASGI loop for up to the 3 s timeout. Off the loop (the
    # capabilities poll runs in FastAPI's threadpool) it fetches normally.
    recipe = _origin_recipe([_master("a" * 64)])
    planned = plan_source_cuts(recipe, 1, set(), shadow_page_id=PAGE_ID)
    assert planned and shadow["fetches"] == 0
    cuts = await asyncio.to_thread(plan_source_cuts, recipe, 1, set())
    assert cuts and shadow["fetches"] == 1


# ---------------------------------------------------------------------------
# FILE source (the primary, private source; ruling 2026-10-28 21:1x).
# ---------------------------------------------------------------------------

def test_file_source_reads_the_fixture_and_logs_reuse(caplog, shadow_file):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    path, box = shadow_file
    # Master "a"*64 carries no origin; the summary's masters table resolves
    # it to origin ORIGIN at offset 600 s, and the fixture's other-page row
    # [600, 900] (5 d old) covers the whole 6 s cut -> cross_page.
    master = _master("a" * 64)
    recipe = _origin_recipe([master])
    _observe(recipe, [SourceCut(master, 0, 6_000, True)], job_id="job-file")
    events = _logged(caplog, "would_reuse")
    assert [e["reason"] for e in events] == ["cross_page"]
    assert events[0]["origin_id"] == ORIGIN
    assert events[0]["window"] == [600.0, 606.0]
    assert box["reads"] == 1


def test_file_source_is_cached_and_reread_when_mtime_moves(shadow_file,
                                                           monkeypatch):
    path, box = shadow_file
    master = _master("a" * 64)
    recipe = _origin_recipe([master])
    cut = SourceCut(master, 0, 6_000, True)
    _observe(recipe, [cut])
    assert box["reads"] == 1
    # Inside the 15 min cache period: no re-read.
    _observe(recipe, [cut])
    assert box["reads"] == 1
    # A moved mtime (the commit-refreshed summary) re-reads immediately.
    stamp = os.stat(path).st_mtime
    os.utime(path, (stamp + 2.0, stamp + 2.0))
    _observe(recipe, [cut])
    assert box["reads"] == 2
    # After the cache period it re-reads even without an mtime move.
    clock = {"t": NOW}
    monkeypatch.setattr(originality_shadow, "_now", lambda: clock["t"])
    clock["t"] = NOW + 15 * 60 + 1
    _observe(recipe, [cut])
    assert box["reads"] == 3


def test_missing_summary_file_logs_once_and_never_blocks(caplog, shadow_file,
                                                          monkeypatch):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    path, _box = shadow_file
    monkeypatch.setenv(originality_shadow.SUMMARY_PATH_ENV,
                       str(path.parent / "not-there.json"))
    originality_shadow.reset_shadow_state()
    recipe = _origin_recipe([_master(
        "a" * 64, origin={"originSourceId": ORIGIN, "originStartMs": 0,
                          "originEndMs": 600_000})])
    planned = plan_source_cuts(recipe, 3, set(), shadow_page_id=PAGE_ID,
                               shadow_job_id="job-4")
    assert [(c.start_ms, c.duration_ms) for c in planned] == \
        [(c.start_ms, c.duration_ms) for c in plan_source_cuts(recipe, 3, set())]
    assert len(_logged(caplog, "unavailable")) == 1
    plan_source_cuts(recipe, 3, set(), shadow_page_id=PAGE_ID)
    assert len(_logged(caplog, "unavailable")) == 1  # once per cache period
    assert _logged(caplog, "would_reuse") == []
    assert originality_shadow.shadow_counts()["unavailable"] == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads chmod-000 files")
def test_unreadable_summary_file_logs_once_and_never_blocks(caplog, shadow_file,
                                                            monkeypatch):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    path, _box = shadow_file
    os.chmod(path, 0)
    try:
        recipe = _origin_recipe([_master(
            "a" * 64, origin={"originSourceId": ORIGIN, "originStartMs": 0,
                              "originEndMs": 600_000})])
        assert plan_source_cuts(recipe, 1, set(), shadow_page_id=PAGE_ID)
        unavailable = _logged(caplog, "unavailable")
        assert len(unavailable) == 1
        assert unavailable[0]["reason"] == "summary read failed"
        assert _logged(caplog, "would_reuse") == []
    finally:
        os.chmod(path, 0o644)


def test_stale_summary_file_is_unavailable_logs_once(caplog, shadow_file,
                                                     monkeypatch):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    path, _box = shadow_file
    value = json.loads(path.read_text())
    value["generated_at"] = "2027-01-14T09:00:00Z"  # 27 h before NOW
    path.write_text(json.dumps(value))
    recipe = _origin_recipe([_master(
        "a" * 64, origin={"originSourceId": ORIGIN, "originStartMs": 0,
                          "originEndMs": 600_000})])
    assert plan_source_cuts(recipe, 1, set(), shadow_page_id=PAGE_ID)
    unavailable = _logged(caplog, "unavailable")
    assert len(unavailable) == 1 and unavailable[0]["reason"] == "summary is stale"
    assert _logged(caplog, "would_reuse") == []


def test_oversize_summary_file_is_refused_by_stat_before_any_read(caplog,
                                                                  shadow_file,
                                                                  monkeypatch):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    path, box = shadow_file
    # Valid JSON, padded past the 8 MB cap: the stat check refuses it before
    # the file is ever opened.
    real_open = builtins.open
    opens = {"count": 0}

    def counting_open(*args, **kwargs):
        opens["count"] += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr(originality_shadow, "open", counting_open,
                        raising=False)
    padded = '{"generated_at": "%s", "origins": {}, "masters": {}}%s' % (
        NOW_ISO, " " * (originality_shadow.SUMMARY_MAX_BYTES + 1))
    path.write_text(padded)
    recipe = _origin_recipe([_master(
        "a" * 64, origin={"originSourceId": ORIGIN, "originStartMs": 0,
                          "originEndMs": 600_000})])
    assert plan_source_cuts(recipe, 1, set(), shadow_page_id=PAGE_ID)
    unavailable = _logged(caplog, "unavailable")
    assert len(unavailable) == 1
    assert unavailable[0]["reason"] == "summary too large"
    assert box["reads"] == 1  # one refused read call...
    assert opens["count"] == 0  # ...that never opened the file
    assert _logged(caplog, "would_reuse") == []


def test_file_source_wins_over_a_configured_url(monkeypatch, caplog, tmp_path):
    # Both sources configured: the private file is the source the shadow
    # reads; the URL never sees a request.
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    path = tmp_path / "usage-summary.json"
    path.write_text(FIXTURE.read_text())
    monkeypatch.setenv(originality_shadow.SUMMARY_PATH_ENV, str(path))
    monkeypatch.setenv(originality_shadow.SUMMARY_URL_ENV, SUMMARY_URL)
    monkeypatch.setenv(originality_shadow.SUMMARY_TOKEN_ENV, SUMMARY_TOKEN)
    monkeypatch.setattr(originality_shadow, "_now", lambda: NOW)
    originality_shadow.reset_shadow_state()
    box = {"fetches": 0}

    def refused_fetch(url, token):
        box["fetches"] += 1
        raise AssertionError("the URL source must not be consulted")

    monkeypatch.setattr(originality_shadow, "_fetch_summary", refused_fetch)
    master = _master("a" * 64)
    recipe = _origin_recipe([master])
    _observe(recipe, [SourceCut(master, 0, 6_000, True)], job_id="job-file-wins")
    assert box["fetches"] == 0
    assert [e["reason"] for e in _logged(caplog, "would_reuse")] == ["cross_page"]
    originality_shadow.reset_shadow_state()


# ---------------------------------------------------------------------------
# URL source: token-gated only (ruling 2026-10-28 21:1x).
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, body=b"{}", status=200):
        self._body = body
        self.status_code = status
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("http error", request=None,
                                        response=None)

    def iter_bytes(self):
        yield self._body


def test_url_without_token_never_fetches(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    monkeypatch.setenv(originality_shadow.SUMMARY_URL_ENV, SUMMARY_URL)
    monkeypatch.delenv(originality_shadow.SUMMARY_TOKEN_ENV, raising=False)
    monkeypatch.delenv(originality_shadow.SUMMARY_PATH_ENV, raising=False)
    monkeypatch.setattr(originality_shadow, "_now", lambda: NOW)
    originality_shadow.reset_shadow_state()
    box = {"streams": 0}

    def refused_stream(*args, **kwargs):
        box["streams"] += 1
        raise AssertionError("no unauthenticated fetch, ever")

    monkeypatch.setattr(originality_shadow.httpx, "stream", refused_stream)
    recipe = _origin_recipe([_master(
        "a" * 64, origin={"originSourceId": ORIGIN, "originStartMs": 0,
                          "originEndMs": 600_000})])
    planned = plan_source_cuts(recipe, 3, set(), shadow_page_id=PAGE_ID,
                               shadow_job_id="job-5")
    assert [(c.start_ms, c.duration_ms) for c in planned] == \
        [(c.start_ms, c.duration_ms) for c in plan_source_cuts(recipe, 3, set())]
    assert box["streams"] == 0  # zero network calls
    unavailable = _logged(caplog, "unavailable")
    assert len(unavailable) == 1
    assert unavailable[0]["reason"] == "summary token missing"
    plan_source_cuts(recipe, 3, set(), shadow_page_id=PAGE_ID)
    assert len(_logged(caplog, "unavailable")) == 1  # logged once per period
    assert _logged(caplog, "would_reuse") == []


def test_url_with_token_sends_the_header_and_the_token_never_reaches_logs(
        caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    monkeypatch.setenv(originality_shadow.SUMMARY_URL_ENV, SUMMARY_URL)
    monkeypatch.setenv(originality_shadow.SUMMARY_TOKEN_ENV, SUMMARY_TOKEN)
    monkeypatch.delenv(originality_shadow.SUMMARY_PATH_ENV, raising=False)
    monkeypatch.setattr(originality_shadow, "_now", lambda: NOW)
    originality_shadow.reset_shadow_state()
    box = {"headers": None, "body": json.dumps(_summary([_row(0, 600)])).encode()}

    def stream(method, url, headers=None, **kwargs):
        box["headers"] = dict(headers or {})
        return _FakeResponse(box["body"])

    monkeypatch.setattr(originality_shadow.httpx, "stream", stream)
    master = _master("a" * 64, origin={"originSourceId": ORIGIN,
                                       "originStartMs": 0,
                                       "originEndMs": 600_000})
    recipe = _origin_recipe([master])
    _observe(recipe, [SourceCut(master, 0, 6_000, True)], job_id="job-6")
    # The auth header carries the token...
    assert box["headers"]["authorization"] == f"Bearer {SUMMARY_TOKEN}"
    assert box["headers"]["accept"] == "application/json"
    assert [e["reason"] for e in _logged(caplog, "would_reuse")] == ["cross_page"]
    # ...and the token appears in no log line, success or failure.
    for record in caplog.records:
        assert SUMMARY_TOKEN not in record.getMessage()
    box["body"] = b"{not json"
    originality_shadow.reset_shadow_state()
    _observe(recipe, [SourceCut(master, 0, 6_000, True)])
    assert len(_logged(caplog, "unavailable")) == 1
    for record in caplog.records:
        assert SUMMARY_TOKEN not in record.getMessage()
    originality_shadow.reset_shadow_state()


# ---------------------------------------------------------------------------
# fix3: event-loop cold/expired cache is never silent, never blocks, and the
# 26 h staleness gate is re-applied on every cached return.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_loop_cold_cache_logs_and_counts_once_per_period(caplog, shadow,
                                                              monkeypatch):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    recipe = _origin_recipe([_master("a" * 64)])
    cut = SourceCut(_master("a" * 64), 0, 6_000, True)
    clock = {"t": NOW}
    monkeypatch.setattr(originality_shadow, "_now", lambda: clock["t"])
    # On the loop a cold cache reads as unavailable, but it must LOG and COUNT
    # exactly once per cache period (never silently).
    _observe(recipe, [cut], page_id=PAGE_ID)
    _observe(recipe, [cut], page_id=PAGE_ID)
    assert len(_logged(caplog, "unavailable")) == 1
    assert _logged(caplog, "unavailable")[0]["reason"] == "event loop cache miss"
    assert originality_shadow.shadow_counts()["unavailable"] == 1
    # A new cache period: logged + counted again (still never silent).
    clock["t"] = NOW + originality_shadow.SUMMARY_CACHE_S + 1
    _observe(recipe, [cut], page_id=PAGE_ID)
    assert len(_logged(caplog, "unavailable")) == 2
    assert originality_shadow.shadow_counts()["unavailable"] == 2


@pytest.mark.asyncio
async def test_loop_expired_cache_never_returns_stale_ready(caplog, shadow,
                                                           monkeypatch):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    master = _master("a" * 64, origin={"originSourceId": ORIGIN,
                                       "originStartMs": 0,
                                       "originEndMs": 600_000})
    recipe = _origin_recipe([master])
    cut = SourceCut(master, 0, 6_000, True)
    # Warm the cache OFF the loop with a summary generated_at == NOW.
    shadow["response"] = json.dumps(_summary(
        [_row(0, 600, page=OTHER_PAGE)])).encode()
    await asyncio.to_thread(_observe, recipe, [cut], PAGE_ID, None)
    assert shadow["fetches"] == 1
    caplog.clear()  # drop the warm call's would_reuse line
    # 27 h later, ON the loop: the cache period ended and the summary is now
    # older than 26 h -> unavailable (logged once), never a stale _Ready.
    clock = {"t": NOW}
    monkeypatch.setattr(originality_shadow, "_now", lambda: clock["t"])
    clock["t"] = NOW + 27 * 3_600.0
    _observe(recipe, [cut], page_id=PAGE_ID)
    assert _logged(caplog, "would_reuse") == []
    assert len(_logged(caplog, "unavailable")) == 1
    assert _logged(caplog, "unavailable")[0]["reason"] == "summary is stale"
    assert originality_shadow.shadow_counts()["unavailable"] == 1


@pytest.mark.asyncio
async def test_loop_cold_cache_never_reads_or_fetches(caplog, monkeypatch,
                                                      tmp_path):
    # A file source so the wrong path would hit the module `open`; a cold
    # cache on the loop must return unavailable without either read or fetch.
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    path = tmp_path / "usage-summary.json"
    path.write_text(FIXTURE.read_text())
    monkeypatch.setenv(originality_shadow.SUMMARY_PATH_ENV, str(path))
    monkeypatch.delenv(originality_shadow.SUMMARY_URL_ENV, raising=False)
    monkeypatch.delenv(originality_shadow.SUMMARY_TOKEN_ENV, raising=False)
    monkeypatch.setattr(originality_shadow, "_now", lambda: NOW)
    originality_shadow.reset_shadow_state()
    calls = {"open": 0, "fetch": 0}

    def no_open(*args, **kwargs):
        calls["open"] += 1
        raise AssertionError("summary file opened on the event loop")

    def no_fetch(url, token):
        calls["fetch"] += 1
        raise AssertionError("summary fetched on the event loop")

    monkeypatch.setattr(originality_shadow, "open", no_open, raising=False)
    monkeypatch.setattr(originality_shadow, "_fetch_summary", no_fetch)
    recipe = _origin_recipe([_master("a" * 64)])
    _observe(recipe, [SourceCut(_master("a" * 64), 0, 6_000, True)],
             page_id=PAGE_ID)
    assert calls["open"] == 0 and calls["fetch"] == 0
    assert len(_logged(caplog, "unavailable")) == 1


def test_concurrent_cold_reads_fetch_once(shadow, monkeypatch):
    recipe = _origin_recipe([_master("a" * 64)])
    cut = SourceCut(_master("a" * 64), 0, 6_000, True)
    shadow["response"] = json.dumps(_summary([])).encode()
    n = 6
    barrier = threading.Barrier(n)
    real_fetch = originality_shadow._fetch_summary
    calls = {"n": 0}

    def slow_fetch(url, token):
        # Slow enough that every reader observes the cold cache before the
        # first write: without single-flight this counts n fetches.
        calls["n"] += 1
        time.sleep(0.05)
        return real_fetch(url, token)

    monkeypatch.setattr(originality_shadow, "_fetch_summary", slow_fetch)

    def worker():
        barrier.wait()
        _observe(recipe, [cut])

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls["n"] == 1


def test_negative_masters_offset_is_unknown_origin(caplog, shadow):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    master = _master("a" * 64)  # no library origin -> summary masters table
    recipe = _origin_recipe([master])
    shadow["response"] = json.dumps(_summary(
        [_row(0, 600, page=OTHER_PAGE)],
        masters={"a" * 64: {"origin_id": ORIGIN, "offset_s": -5.0}},
    )).encode()
    _observe(recipe, [SourceCut(master, 0, 6_000, True)], job_id="job-neg")
    events = _logged(caplog, "would_reuse")
    assert [e["reason"] for e in events] == ["unknown_origin"]
    assert events[0]["origin_id"] is None
    assert originality_shadow.shadow_counts()["unknown_origin"] == 1


# ---------------------------------------------------------------------------
# fix4: silent failures, loop stats, poll logging, and Infinity in logs.
# ---------------------------------------------------------------------------

def test_unexpected_state_exception_logs_warns_and_caches_unavailable(
        caplog, shadow):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    shadow["raise"] = RuntimeError("boom from a fetch")
    recipe = _origin_recipe([_master("a" * 64)])
    cut = SourceCut(_master("a" * 64), 0, 6_000, True)
    _observe(recipe, [cut], job_id="job-unexpected")
    unavailable = _logged(caplog, "unavailable")
    assert len(unavailable) == 1
    assert unavailable[0]["reason"] == "summary unavailable"
    assert originality_shadow.shadow_counts()["unavailable"] == 1
    # The unexpected failure is cached as unavailable: the second call does not
    # re-read (and does not log a second time inside the cache period).
    _observe(recipe, [cut], job_id="job-unexpected")
    assert shadow["fetches"] == 1
    assert len(_logged(caplog, "unavailable")) == 1
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING
                and r.name == "content_lab.originality_shadow"]
    assert warnings


def test_no_job_id_logs_and_counts_nothing(caplog, shadow):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    master = _master("a" * 64, origin={"originSourceId": ORIGIN,
                                       "originStartMs": 0,
                                       "originEndMs": 600_000})
    recipe = _origin_recipe([master])
    shadow["response"] = json.dumps(
        _summary([_row(0, 600, page=OTHER_PAGE)])).encode()
    # The capabilities poll and the empty-plan explainer run without a job_id:
    # they warm the cache but never log or count would_reuse.
    _observe(recipe, [SourceCut(master, 0, 6_000, True)])
    assert _logged(caplog, "would_reuse") == []
    counts = originality_shadow.shadow_counts()
    assert counts["would_reuse"] == 0
    assert counts["cross_page"] == 0


def test_page_none_never_logs_unknown_origin(caplog, shadow):
    caplog.set_level(logging.INFO, logger="content_lab.originality_shadow")
    master = _master("a" * 64)  # no origin anywhere
    recipe = _origin_recipe([master])
    shadow["response"] = json.dumps(_summary([])).encode()
    _observe(recipe, [SourceCut(master, 0, 6_000, True)], page_id=None,
             job_id="job-no-page")
    assert _logged(caplog, "would_reuse") == []
    assert originality_shadow.shadow_counts()["unknown_origin"] == 0


@pytest.mark.asyncio
async def test_loop_warm_cache_never_stats_the_file(shadow_file, monkeypatch):
    path, box = shadow_file
    master = _master("a" * 64)
    recipe = _origin_recipe([master])
    cut = SourceCut(master, 0, 6_000, True)
    # Warm the cache off the loop.
    await asyncio.to_thread(_observe, recipe, [cut], PAGE_ID, "job-warm")
    assert box["reads"] == 1
    stats = {"calls": 0}
    real_mtime = originality_shadow._mtime

    def counting_mtime(location):
        stats["calls"] += 1
        return real_mtime(location)

    monkeypatch.setattr(originality_shadow, "_mtime", counting_mtime)
    # On the loop the cached mtime is reused; no os.stat may run.
    _observe(recipe, [cut], page_id=PAGE_ID, job_id="job-loop")
    assert stats["calls"] == 0
