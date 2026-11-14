"""Independent state-contract tests for services.scrambler (GLM seat 2, 2026-10-29).

Behavioral pins written from the LATEST steering (LATEST-SPEED-STEERING.txt)
and the required user contracts, NOT from the current implementation:

- production speed vocabulary slow 0.3..0.8 (0.05 grid) + fast 1.1/1.2/1.3
  ONLY (1.4 retired), default fast share 0.0 (slow-only; fast branch is opt-in
  via SCRAMBLER_FAST_SHARE), per-page override for real
  colon-bearing page ids (``acct:<uuid>``);
- replay of the same (page, job, quantity) returns the STORED draw whatever
  config/date/history changed in between, and an invalid NEW config refuses
  new draws without breaking an existing valid replay;
- persisted state fails closed: duplicate cut indices, noncontiguous job
  cuts, off-grid speeds (0.301) and retired speeds (1.4), malformed JSON;
- release keeps rendered rows; the 24 h report counts rendered only and
  excludes future-dated rows;
- a ledger that vanished after creation refuses (marker), first bootstrap
  initializes; invalid ``now``/SHA inputs never poison the ledger;
- long-run invariants: 5-9 s / 0.5 s-grid lengths, no consecutive length or
  treatment repeats, unique full combos, cumulative reverse <= floor(cuts/15)
  at every prefix, <= 1 reverse per ET day.

All ledgers live under pytest ``tmp_path``; the real roster path is never
touched (every call passes an explicit ``path``).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.scrambler import (
    FAST_SPEEDS,
    RENDERED,
    RESERVED,
    ScramblerDraw,
    ScramblerHistoryCorrupt,
    ScramblerHistoryLost,
    enrich_job_reservations,
    fast_share_for,
    mark_cut_rendered,
    release_job_reservations,
    reserve_job_cuts,
    scrambler_daily_report,
    scrambler_history_rows,
)

PRODUCTION_SPEEDS = frozenset({round(0.3 + 0.05 * i, 2) for i in range(11)}) | {1.1, 1.2, 1.3}
COLON_PAGE = "acct:123e4567-e89b-12d3-a456-426614174000"
DAY0 = "2026-10-28T12:00:00+00:00"


@pytest.fixture(autouse=True)
def clean_scrambler_env(monkeypatch):
    """No scrambler switch leaks between tests; the ledger path is always explicit."""
    for key in ("SCRAMBLER_ENABLED", "SCRAMBLER_PAGES", "SCRAMBLER_DENY_PAGES",
                "SCRAMBLER_FAST_SHARE", "SCRAMBLER_PAGE_FAST_SHARE",
                "SCRAMBLER_TREATMENT_WEIGHTS", "SCRAMBLER_HISTORY_PATH"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def ledger_path(tmp_path) -> Path:
    path = tmp_path / "scrambler-history.json"
    # First read bootstraps deliberately: creates the ledger and its marker.
    assert scrambler_history_rows("boot-page", path=path) == []
    return path


def _read_ledger(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_ledger(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _enrich_item(cut_index: int) -> dict:
    return {
        "cutIndex": cut_index,
        "masterSha256": f"m{cut_index}" + "0" * 62,
        "masterSourceId": f"src-{cut_index}",
        "libraryStartMs": 1000 * cut_index,
        "originStartMs": 1000 * cut_index,
        "originEndMs": 1000 * cut_index + 6000,
    }


# ---------------------------------------------------------------------------
# Configuration vocabulary (latest steering)
# ---------------------------------------------------------------------------

def test_default_fast_share_is_zero():
    """The shipped default is slow-only (0% fast), matching canonical §7a step 1.

    The fast branch (1.1/1.2/1.3x) stays available but is opt-in via
    SCRAMBLER_FAST_SHARE; 0.1 was the pre-review pilot default and over-consumes
    runway if left on by default.
    """
    assert fast_share_for("some-page") == 0.0


def test_production_fast_vocabulary_is_1_1_1_2_1_3_only():
    """1.4 is retired from the production vocabulary; slow stays 0.3..0.8."""
    assert FAST_SPEEDS == (1.1, 1.2, 1.3)


def test_colon_page_id_override_wins_over_global(monkeypatch):
    """``acct:<uuid>`` page ids get their real per-page override, not the text
    before the first colon."""
    monkeypatch.setenv("SCRAMBLER_FAST_SHARE", "0.3")
    monkeypatch.setenv("SCRAMBLER_PAGE_FAST_SHARE", f"{COLON_PAGE}:0.8")
    assert fast_share_for(COLON_PAGE) == 0.8
    # A page with no override falls back to the global share.
    assert fast_share_for("plain-page") == 0.3


# ---------------------------------------------------------------------------
# Replay determinism
# ---------------------------------------------------------------------------

def test_replay_returns_stored_draws_after_history_date_and_config_changes(
        monkeypatch, ledger_path):
    draws = reserve_job_cuts("p", "job-1", 1, now=DAY0, path=ledger_path)
    reserve_job_cuts("p", "job-2", 2, now=DAY0, path=ledger_path)
    monkeypatch.setenv("SCRAMBLER_FAST_SHARE", "0.9")
    monkeypatch.setenv("SCRAMBLER_PAGE_FAST_SHARE", "p:0.9")
    monkeypatch.setenv("SCRAMBLER_TREATMENT_WEIGHTS", "none:1,camera:9")
    later = (datetime.fromisoformat(DAY0) + timedelta(days=30)).isoformat()
    replay = reserve_job_cuts("p", "job-1", 1, now=later, path=ledger_path)
    assert replay == draws
    assert all(isinstance(draw, ScramblerDraw) for draw in replay)


def test_invalid_new_config_keeps_existing_replay_and_refuses_new_draws(
        monkeypatch, ledger_path):
    path = ledger_path
    draws = reserve_job_cuts("p", "job-1", 1, now=DAY0, path=path)
    monkeypatch.setenv("SCRAMBLER_FAST_SHARE", "invalid")
    # The stored operation replays unchanged; only NEW draws refuse.
    assert reserve_job_cuts("p", "job-1", 1, now=DAY0, path=path) == draws
    with pytest.raises((ValueError, ScramblerHistoryCorrupt)):
        reserve_job_cuts("p", "job-2", 1, now=DAY0, path=path)


# ---------------------------------------------------------------------------
# Fail-closed persisted state
# ---------------------------------------------------------------------------

def test_duplicate_cut0_rows_refuse_quantity2_replay(ledger_path):
    """Two rows for one (job, cutIndex) must fail closed - never replay cut 0
    twice as a fake quantity-2 job."""
    reserve_job_cuts("p", "job-1", 1, now=DAY0, path=ledger_path)
    data = _read_ledger(ledger_path)
    rows = data["pages"]["p"]
    rows.append(json.loads(json.dumps(rows[-1])))  # same cutIndex, same slotId
    data["counters"]["p"]["cuts"] += 1
    _write_ledger(ledger_path, data)
    with pytest.raises(ScramblerHistoryCorrupt):
        reserve_job_cuts("p", "job-1", 2, now=DAY0, path=ledger_path)


def test_noncontiguous_job_cut_indices_fail_closed(ledger_path):
    reserve_job_cuts("p", "job-1", 3, now=DAY0, path=ledger_path)
    data = _read_ledger(ledger_path)
    rows = data["pages"]["p"]
    for row in rows:
        if row["cutIndex"] == 2:
            row["cutIndex"] = 5  # job now holds cuts {0, 1, 5}: a gap
            row["slotId"] = f"job-1#cut-{row['cutIndex']}"  # keep the row keyed
    _write_ledger(ledger_path, data)
    with pytest.raises(ScramblerHistoryCorrupt):
        scrambler_history_rows("p", path=ledger_path)


def test_persisted_off_grid_speed_0301_fails_closed(ledger_path):
    reserve_job_cuts("p", "job-1", 1, now=DAY0, path=ledger_path)
    data = _read_ledger(ledger_path)
    data["pages"]["p"][0]["speed"] = 0.301
    _write_ledger(ledger_path, data)
    with pytest.raises(ScramblerHistoryCorrupt):
        scrambler_history_rows("p", path=ledger_path)


def test_persisted_retired_speed_1_4_fails_closed(ledger_path):
    """A 1.4 row is out of the production vocabulary: loading it must refuse,
    never normalize it into a usable draw."""
    reserve_job_cuts("p", "job-1", 1, now=DAY0, path=ledger_path)
    data = _read_ledger(ledger_path)
    data["pages"]["p"][0]["speed"] = 1.4
    _write_ledger(ledger_path, data)
    with pytest.raises(ScramblerHistoryCorrupt):
        scrambler_history_rows("p", path=ledger_path)


def test_malformed_ledger_json_fails_closed(ledger_path):
    ledger_path.write_text("not-json{", encoding="utf-8")
    with pytest.raises(ScramblerHistoryCorrupt):
        scrambler_history_rows("p", path=ledger_path)


def test_partial_enrichment_of_two_reserved_cuts_refuses(ledger_path):
    """Enriching a 2-cut job with only cut 0's window is a named refusal; cut 1
    must not be left with null origin truth."""
    reserve_job_cuts("p", "job-1", 2, now=DAY0, path=ledger_path)
    with pytest.raises((ScramblerHistoryCorrupt, ValueError)):
        enrich_job_reservations("p", "job-1", [_enrich_item(0)], path=ledger_path)
    rows = _read_ledger(ledger_path)["pages"]["p"]
    assert all(row["masterSha256"] is None for row in rows)


# ---------------------------------------------------------------------------
# Release, rendered preservation, 24 h report
# ---------------------------------------------------------------------------

def test_release_keeps_rendered_row_and_its_report_presence(ledger_path):
    """A later failure of the same job never erases an already rendered cut."""
    reserve_job_cuts("p", "job-1", 2, now=DAY0, path=ledger_path)
    mark_cut_rendered("p", "job-1", 0, asset_sha256="a" * 64,
                      rendered_at=DAY0, path=ledger_path)
    release_job_reservations("p", "job-1", reason="cut 1 encode failed",
                             path=ledger_path)
    rows = scrambler_history_rows("p", path=ledger_path)
    assert [row["state"] for row in rows] == [RENDERED, "released"]
    assert [row["cutIndex"] for row in rows] == [0, 1]
    assert rows[0]["assetSha256"] == "a" * 64
    report = scrambler_daily_report(rows, now=DAY0.replace("12:00", "14:00"))
    assert report["cuts"] == 1


def test_report_counts_rendered_only_and_excludes_future_rows(ledger_path):
    path = ledger_path
    reserve_job_cuts("p", "past", 1, now=DAY0, path=path)
    mark_cut_rendered("p", "past", 0, asset_sha256="b" * 64,
                      rendered_at=DAY0, path=path)
    reserve_job_cuts("p", "pending", 1, now=DAY0, path=path)  # never rendered
    reserve_job_cuts("p", "future", 1, now=DAY0, path=path)
    mark_cut_rendered("p", "future", 0, asset_sha256="c" * 64,
                      rendered_at="2026-11-15T12:00:00+00:00", path=path)
    rows = scrambler_history_rows("p", path=path)
    assert len(rows) == 3  # reserved rows stay distinguishable from rendered
    report = scrambler_daily_report(rows, now=DAY0.replace("12:00", "14:00"))
    assert report["cuts"] == 1
    assert report["distinctTreatment"] == 1
    assert report["zeroVarietyCuts"] is False


# ---------------------------------------------------------------------------
# Loss vs bootstrap, and write validation
# ---------------------------------------------------------------------------

def test_known_created_history_loss_refuses_while_bootstrap_initializes(ledger_path):
    marker = ledger_path.with_name(ledger_path.name + ".created")
    assert ledger_path.exists() and marker.exists()
    ledger_path.unlink()  # the ledger disappears, the marker remains: LOSS
    with pytest.raises(ScramblerHistoryLost):
        scrambler_history_rows("p", path=ledger_path)
    with pytest.raises(ScramblerHistoryLost):
        reserve_job_cuts("p", "job-1", 1, now=DAY0, path=ledger_path)


def test_invalid_now_and_render_inputs_refuse_without_poisoning(ledger_path):
    path = ledger_path
    with pytest.raises(ValueError):
        reserve_job_cuts("p", "job-1", 1, now="not-a-date", path=path)
    assert scrambler_history_rows("p", path=path) == []
    reserve_job_cuts("p", "job-1", 1, now=DAY0, path=path)
    with pytest.raises(ScramblerHistoryCorrupt):
        mark_cut_rendered("p", "job-1", 0, asset_sha256="  ",
                          rendered_at=DAY0, path=path)
    with pytest.raises(ScramblerHistoryCorrupt):
        mark_cut_rendered("p", "job-1", 0, asset_sha256="d" * 64,
                          rendered_at="not-a-date", path=path)
    row = scrambler_history_rows("p", path=path)[0]
    assert row["state"] == RESERVED and "renderedAt" not in row


# ---------------------------------------------------------------------------
# Long-run invariants (bounded: 15 ET days x 4 cuts = 60 draws)
# ---------------------------------------------------------------------------

def test_long_run_prefix_reverse_cap_day_cap_and_variety_invariants(ledger_path):
    start = datetime.fromisoformat(DAY0)
    for day in range(15):
        now = (start + timedelta(days=day)).isoformat()
        draws = reserve_job_cuts("p", f"job-{day:02d}", 4, now=now,
                                 path=ledger_path)
        assert len(draws) == 4
    rows = scrambler_history_rows("p", path=ledger_path)
    assert len(rows) == 60

    reverses = 0
    per_day_reverses: dict[str, int] = {}
    combos = set()
    previous = None
    for index, row in enumerate(rows, start=1):
        assert row["lengthMs"] % 500 == 0 and 5000 <= row["lengthMs"] <= 9000
        assert row["speed"] in PRODUCTION_SPEEDS
        combo = (row["speed"], row["lengthMs"], row["treatment"], row["mirror"])
        assert combo not in combos  # full combo unique across the history
        combos.add(combo)
        if previous is not None:
            assert row["lengthMs"] != previous["lengthMs"]
            assert row["treatment"] != previous["treatment"]
        if row["treatment"] == "reverse":
            reverses += 1
            per_day_reverses[row["etDay"]] = \
                per_day_reverses.get(row["etDay"], 0) + 1
        # Cumulative cap at EVERY prefix: committed reverses <= floor(cuts/15).
        assert reverses <= index // 15, f"prefix {index} holds {reverses} reverses"
        previous = row
    assert max(per_day_reverses.values(), default=0) <= 1  # one reverse per ET day
