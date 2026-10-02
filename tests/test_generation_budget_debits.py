"""Submission-time debit idempotency and UTC-day accounting.

Codex P1 re-check: a new billable provider submission is debited at submission
time with a durable idempotent id; resuming/polling the SAME prediction is free
(even across a restart that straddles midnight); a NEW submission after
midnight is charged to the new day. Red on fce90bc (which has no
``debit_generation_spend`` at all).
"""

from datetime import datetime, timezone

import pytest

from services import generation_budget


def _store():
    return {"jobs": {}, "byIdempotency": {}, "served": {}}


def test_debit_is_idempotent_for_the_same_id(monkeypatch):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "1.0")
    store = _store()
    assert generation_budget.debit_generation_spend(store, 0.28, "job:g00:s0") is True
    # Re-debiting the same id (poll/resume) never charges a second time.
    assert generation_budget.debit_generation_spend(store, 0.28, "job:g00:s0") is True
    assert generation_budget.spent_usd(store) == pytest.approx(0.28)


def test_distinct_debit_ids_each_charge(monkeypatch):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "1.0")
    store = _store()
    assert generation_budget.debit_generation_spend(store, 0.28, "job:g00:s0") is True
    assert generation_budget.debit_generation_spend(store, 0.28, "job:g00-r1:s0") is True
    assert generation_budget.spent_usd(store) == pytest.approx(0.56)


def test_debit_refuses_at_the_cap(monkeypatch):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "0.5")
    store = _store()
    assert generation_budget.debit_generation_spend(store, 0.28, "job:g00:s0") is True
    # A second distinct submission would exceed 0.5 -> refused, never spent.
    assert generation_budget.debit_generation_spend(store, 0.28, "job:g01:s0") is False
    assert generation_budget.spent_usd(store) == pytest.approx(0.28)


def test_same_debit_id_is_free_across_midnight(monkeypatch):
    """Resuming the SAME prediction after midnight stays free: the idempotent
    debit id survives the rollover, so a resume never re-charges the new day."""
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "1.0")
    store = _store()
    day1 = datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc)
    day2 = datetime(2026, 10, 1, 0, 5, tzinfo=timezone.utc)
    assert generation_budget.debit_generation_spend(store, 0.28, "job:g00:s0", now=day1) is True
    assert generation_budget.spent_usd(store, now=day1) == pytest.approx(0.28)
    assert generation_budget.debit_generation_spend(store, 0.28, "job:g00:s0", now=day2) is True
    assert generation_budget.spent_usd(store, now=day2) == 0.0


def test_new_submission_after_midnight_charges_the_new_day(monkeypatch):
    """A NEW billable submission after midnight is charged to the new day's
    fresh ledger, independently of the previous day's spend."""
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "1.0")
    store = _store()
    day1 = datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc)
    day2 = datetime(2026, 10, 1, 0, 5, tzinfo=timezone.utc)
    generation_budget.debit_generation_spend(store, 0.28, "job:g00:s0", now=day1)
    assert generation_budget.debit_generation_spend(store, 0.28, "job:g01:s0", now=day2) is True
    assert generation_budget.spent_usd(store, now=day2) == pytest.approx(0.28)
