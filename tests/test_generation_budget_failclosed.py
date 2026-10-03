"""P2: invalid/corrupt numeric budget state must fail closed for spend.

A malformed ``LAB_GENERATION_DAILY_BUDGET_USD`` (NaN/inf) or a corrupt
current-day ledger ``spentUsd`` must refuse spend, never reset to zero and
never read as "unlimited". These red tests fail on fce90bc (the old meter
accepts NaN and silently resets a corrupt ledger to 0.0).
"""

import math

import pytest

from services import generation_budget


def test_daily_budget_refuses_nan_and_infinity(monkeypatch):
    """NaN / +inf / -inf are not "a number >= 0" and must refuse all spend."""
    for raw in ("nan", "NaN", "inf", "Infinity", "-inf", "+infinity"):
        monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, raw)
        assert generation_budget.daily_budget_usd() == 0.0, raw
    monkeypatch.delenv(generation_budget.USD_BUDGET_ENV, raising=False)
    # Unset uses the derived finite ceiling; the emergency stop is explicit "0".
    assert generation_budget.daily_budget_usd() == generation_budget.DEFAULT_DAILY_BUDGET_USD


def test_unset_default_is_a_finite_nonzero_derived_ceiling(monkeypatch):
    """Eric's "never quiet" rule: unset must NOT disable generation. The derived
    default is a finite, positive ceiling — never 0, never NaN/inf."""
    monkeypatch.delenv(generation_budget.USD_BUDGET_ENV, raising=False)
    default = generation_budget.daily_budget_usd()
    assert default == generation_budget.DEFAULT_DAILY_BUDGET_USD
    assert math.isfinite(default)
    assert default > 0


def test_explicit_zero_is_the_named_emergency_stop(monkeypatch):
    """An explicit ``LAB_GENERATION_DAILY_BUDGET_USD=0`` refuses all paid
    generation by name (the emergency stop), unlike unset (derived default)."""
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "0")
    assert generation_budget.daily_budget_usd() == 0.0
    store = {"jobs": {}, "byIdempotency": {}, "served": {}}
    assert generation_budget.reserve_generation_spend(store, 0.28, "replicate") is False
    assert generation_budget.debit_generation_spend(store, 0.28, "job:g00:s0") is False


def test_spent_usd_fails_closed_on_corrupt_current_day():
    """A malformed current-day ``spentUsd`` must raise, never read as zero."""
    store = {
        "jobs": {}, "byIdempotency": {}, "served": {},
        generation_budget.BUDGET_KEY: {
            "day": generation_budget.utc_day(),
            "spentUsd": "junk",
            "calls": {},
        },
    }
    with pytest.raises(generation_budget.BudgetLedgerCorrupt):
        generation_budget.spent_usd(store)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, "junk"])
def test_spent_usd_fails_closed_on_nonfinite_spent(bad):
    store = {
        "jobs": {}, "byIdempotency": {}, "served": {},
        generation_budget.BUDGET_KEY: {
            "day": generation_budget.utc_day(),
            "spentUsd": bad,
            "calls": {},
        },
    }
    with pytest.raises(generation_budget.BudgetLedgerCorrupt):
        generation_budget.spent_usd(store)


def test_reserve_refuses_spend_on_corrupt_current_day(monkeypatch):
    """A corrupt ledger must not be silently reset so the whole budget returns."""
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "25.0")
    store = {
        "jobs": {}, "byIdempotency": {}, "served": {},
        generation_budget.BUDGET_KEY: {
            "day": generation_budget.utc_day(),
            "spentUsd": "not-a-number",
            "calls": {},
        },
    }
    with pytest.raises(generation_budget.BudgetLedgerCorrupt):
        generation_budget.reserve_generation_spend(store, 0.28, "replicate")


def test_invalid_amount_fails_closed(monkeypatch):
    """NaN/inf/negative amounts must not be silently read as zero-and-pass."""
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "25.0")
    store = {"jobs": {}, "byIdempotency": {}, "served": {}}
    for bad in (float("nan"), float("inf"), -1.0):
        with pytest.raises(ValueError):
            generation_budget.reserve_generation_spend(store, bad, "replicate")


def test_finite_amounts_still_reserve(monkeypatch):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "25.0")
    store = {"jobs": {}, "byIdempotency": {}, "served": {}}
    assert generation_budget.reserve_generation_spend(store, 0.28, "replicate") is True
    assert math.isfinite(generation_budget.spent_usd(store))
