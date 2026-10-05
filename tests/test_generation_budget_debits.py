"""Submission-time debit idempotency and UTC-day accounting.

Codex P1 re-check: a new billable provider submission is debited at submission
time with a durable idempotent id; resuming/polling the SAME prediction is free
(even across a restart that straddles midnight); a NEW submission after
midnight is charged to the new day.
"""

from datetime import datetime, timezone

import pytest

from services import generation_budget


def _store():
    return {"jobs": {}, "byIdempotency": {}, "served": {}}


def test_debit_is_idempotent_for_the_same_id(monkeypatch, tmp_path):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "1.0")
    store = tmp_path / "jobs.json"
    assert generation_budget.debit_generation_spend_at(store, 0.28, "job:g00:s0") is True
    # Re-debiting the same id (poll/resume) never charges a second time.
    assert generation_budget.debit_generation_spend_at(store, 0.28, "job:g00:s0") is True
    assert generation_budget.spent_usd_at(store) == pytest.approx(0.28)


def test_distinct_debit_ids_each_charge(monkeypatch, tmp_path):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "1.0")
    store = tmp_path / "jobs.json"
    assert generation_budget.debit_generation_spend_at(store, 0.28, "job:g00:s0") is True
    assert generation_budget.debit_generation_spend_at(store, 0.28, "job:g00-r1:s0") is True
    assert generation_budget.spent_usd_at(store) == pytest.approx(0.56)


def test_debit_refuses_at_the_cap(monkeypatch, tmp_path):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "0.5")
    store = tmp_path / "jobs.json"
    assert generation_budget.debit_generation_spend_at(store, 0.28, "job:g00:s0") is True
    # A second distinct submission would exceed 0.5 -> refused, never spent.
    assert generation_budget.debit_generation_spend_at(store, 0.28, "job:g01:s0") is False
    assert generation_budget.spent_usd_at(store) == pytest.approx(0.28)


def test_same_debit_id_is_free_across_midnight(monkeypatch, tmp_path):
    """Resuming the SAME prediction after midnight stays free: the idempotent
    debit id survives the rollover, so a resume never re-charges the new day."""
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "1.0")
    store = tmp_path / "jobs.json"
    day1 = datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc)
    day2 = datetime(2026, 10, 1, 0, 5, tzinfo=timezone.utc)
    assert generation_budget.debit_generation_spend_at(store, 0.28, "job:g00:s0", now=day1) is True
    assert generation_budget.spent_usd_at(store, now=day1) == pytest.approx(0.28)
    assert generation_budget.debit_generation_spend_at(store, 0.28, "job:g00:s0", now=day2) is True
    assert generation_budget.spent_usd_at(store, now=day2) == 0.0


def test_new_submission_after_midnight_charges_the_new_day(monkeypatch, tmp_path):
    """A NEW billable submission after midnight is charged to the new day's
    fresh ledger, independently of the previous day's spend."""
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "1.0")
    store = tmp_path / "jobs.json"
    day1 = datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc)
    day2 = datetime(2026, 10, 1, 0, 5, tzinfo=timezone.utc)
    generation_budget.debit_generation_spend_at(store, 0.28, "job:g00:s0", now=day1)
    assert generation_budget.debit_generation_spend_at(store, 0.28, "job:g01:s0", now=day2) is True
    assert generation_budget.spent_usd_at(store, now=day2) == pytest.approx(0.28)


def test_debit_preserves_legacy_bytes_without_whole_history_rewrite(monkeypatch, tmp_path):
    """Debit a real legacy store while preserving every old identity and byte."""
    import json
    import sqlite3

    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "1.0")
    day = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
    ledger = {"day": "2026-10-04", "spentUsd": 0.28, "calls": {},
              "debits": {f"unknown-old-intent-{i}": 0.28 for i in range(2000)}}
    path = tmp_path / "jobs.json"
    original = json.dumps({**_store(), "generationBudget": ledger,
                           "jobs": {"ambiguous": {"providerCheckpoints": {"state": "submitting"}}}}).encode()
    path.write_bytes(original)

    assert generation_budget.debit_generation_spend_at(path, 0.28, "new-call", now=day)
    assert path.read_bytes() == original, "a debit must not rewrite legacy job or debit history"
    with sqlite3.connect(generation_budget.ledger_path(path)) as db:
        assert db.execute("SELECT COUNT(*) FROM debits").fetchone()[0] == 2001
        assert db.execute("SELECT legacy_json FROM migration").fetchone()[0] == original
    next_day = datetime(2026, 10, 5, 1, tzinfo=timezone.utc)
    assert generation_budget.debit_generation_spend_at(path, 0.28, "unknown-old-intent-1999", now=next_day)
    assert generation_budget.spent_usd_at(path, now=next_day) == 0
    assert generation_budget.spent_usd_at(path, now=day) == pytest.approx(0.56)
    assert path.read_bytes() == original


def _process_debit(arguments):
    path, identity = arguments
    return generation_budget.debit_generation_spend_at(path, 0.28, identity)


def test_cross_process_cap_and_known_id_replay(monkeypatch, tmp_path):
    from concurrent.futures import ProcessPoolExecutor
    import sqlite3

    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "1")
    path = tmp_path / "jobs.json"
    with ProcessPoolExecutor(max_workers=4) as workers:
        accepted = list(workers.map(_process_debit, [(str(path), f"paid-{i}") for i in range(12)]))
    assert sum(accepted) == 3
    assert generation_budget.spent_usd_at(path) == pytest.approx(0.84)
    with sqlite3.connect(generation_budget.ledger_path(path)) as db:
        assert db.execute("SELECT COUNT(*) FROM debits").fetchone()[0] == 3
        known = db.execute("SELECT id FROM debits LIMIT 1").fetchone()[0]
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "0")
    assert generation_budget.debit_generation_spend_at(path, 0.56, known), "known intent remains free after price/config changes"
    assert not generation_budget.debit_generation_spend_at(path, 0.28, "new-after-stop")
    assert generation_budget.spent_usd_at(path) == pytest.approx(0.84)


def test_debit_indexes_immutable_audit_and_atomic_failure(monkeypatch, tmp_path):
    import sqlite3

    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "1")
    path = tmp_path / "jobs.json"
    assert generation_budget.debit_generation_spend_at(path, 0.28, "first")
    with sqlite3.connect(generation_budget.ledger_path(path)) as db:
        for statement, argument in [
            ("SELECT amount_usd FROM debits WHERE id=?", "first"),
            ("SELECT spent_usd FROM days WHERE day=?", generation_budget.utc_day()),
        ]:
            plan = db.execute("EXPLAIN QUERY PLAN " + statement, (argument,)).fetchall()
            assert any("SEARCH" in row[3] and "INDEX" in row[3] for row in plan)
        for statement in ["UPDATE debits SET amount_usd='0'", "DELETE FROM debits", "DELETE FROM migration"]:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(statement)
        db.execute("CREATE TRIGGER fail_total BEFORE UPDATE ON days BEGIN SELECT RAISE(ABORT,'injected total write failure'); END")
    with pytest.raises(generation_budget.BudgetLedgerCorrupt):
        generation_budget.debit_generation_spend_at(path, 0.28, "second")
    assert generation_budget.spent_usd_at(path) == pytest.approx(0.28)
    with sqlite3.connect(generation_budget.ledger_path(path)) as db:
        assert db.execute("SELECT id FROM debits").fetchall() == [("first",)]


@pytest.mark.parametrize("legacy", [
    b"{broken", b'{"jobs":[],"generationBudget":{}}',
    b'{"jobs":{},"generationBudget":null}',
    b'{"generationBudget":{"day":"2026-10-04","spentUsd":"junk"}}',
    b'{"generationBudget":{"day":"2026-10-04","spentUsd":0,"debits":{"unknown":null}}}',
])
def test_corrupt_legacy_never_becomes_fresh_budget(tmp_path, legacy):
    path = tmp_path / "jobs.json"
    path.write_bytes(legacy)
    with pytest.raises(generation_budget.BudgetLedgerCorrupt):
        generation_budget.debit_generation_spend_at(path, 0.28, "new")
    assert not generation_budget.ledger_path(path).exists()
    assert path.read_bytes() == legacy
    assert generation_budget.summary_at(path)["corrupt"] is True


@pytest.mark.parametrize("fault", ["missing", "corrupt", "marker_mismatch"])
def test_database_loss_or_mismatch_never_resets_paid_history(monkeypatch, tmp_path, fault):
    import json

    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "1")
    path = tmp_path / "jobs.json"
    assert generation_budget.debit_generation_spend_at(path, 0.28, "unknown-paid-intent")
    ledger = generation_budget.ledger_path(path)
    if fault == "missing":
        ledger.unlink()
    elif fault == "corrupt":
        ledger.write_bytes(b"corrupt database")
    else:
        marker = ledger.with_suffix(".origin.json")
        value = json.loads(marker.read_text())
        value["instance"] = "0" * 32
        marker.write_text(json.dumps(value))
    with pytest.raises(generation_budget.BudgetLedgerCorrupt):
        generation_budget.debit_generation_spend_at(path, 0.28, "new-intent")
    assert generation_budget.summary_at(path)["corrupt"] is True


def test_committed_migration_marker_recovers_without_reimport(monkeypatch, tmp_path):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "1")
    path = tmp_path / "jobs.json"
    assert generation_budget.debit_generation_spend_at(path, 0.28, "first")
    generation_budget.ledger_path(path).with_suffix(".origin.json").unlink()
    # Model a crash after durable DB replacement and before the marker write.
    assert generation_budget.debit_generation_spend_at(path, 0.28, "first")
    assert generation_budget.spent_usd_at(path) == pytest.approx(0.28)


def test_offset_clock_uses_actual_utc_submission_day(monkeypatch, tmp_path):
    from datetime import timedelta
    import sqlite3

    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "1")
    path = tmp_path / "jobs.json"
    local = datetime(2026, 10, 5, 1, tzinfo=timezone(timedelta(hours=2)))
    assert generation_budget.debit_generation_spend_at(path, 0.28, "first", now=local)
    with sqlite3.connect(generation_budget.ledger_path(path)) as db:
        assert db.execute("SELECT day FROM debits").fetchall() == [("2026-10-04",)]


def test_writer_lock_crossing_midnight_uses_submission_day(monkeypatch, tmp_path):
    import contextlib
    import json
    from services import generation_recovery

    day1 = datetime(2026, 10, 4, 23, 59, 59, tzinfo=timezone.utc)
    day2 = datetime(2026, 10, 5, 0, 0, 1, tzinfo=timezone.utc)
    clock = {"now": day1}
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]
    monkeypatch.setattr(generation_budget, "datetime", Clock)
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "1")
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps({"jobs": {}, "generationBudget": {
        "day": "2026-10-04", "spentUsd": 1, "debits": {"old-paid-intent": 1},
    }}))
    original_lock = generation_recovery.store_lock
    @contextlib.contextmanager
    def crossing_lock(target):
        with original_lock(target):
            clock["now"] = day2
            yield
    monkeypatch.setattr(generation_recovery, "store_lock", crossing_lock)
    assert generation_budget.debit_generation_spend_at(path, 0.28, "new-after-lock")
    assert generation_budget.spent_usd_at(path, now=day1) == 1
    assert generation_budget.spent_usd_at(path, now=day2) == pytest.approx(0.28)
