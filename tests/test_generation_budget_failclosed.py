"""P2: invalid/corrupt numeric budget state must fail closed for spend.

A malformed ``LAB_GENERATION_DAILY_BUDGET_USD`` (NaN/inf) or a corrupt
current-day ledger ``spentUsd`` must refuse spend, never reset to zero and
never read as "unlimited". These red tests fail on fce90bc (the old meter
accepts NaN and silently resets a corrupt ledger to 0.0).
"""

import json
import math

import pytest

from services import generation_budget


def test_daily_budget_refuses_nan_and_infinity(monkeypatch):
    """NaN / +inf / -inf are not "a number >= 0" and must refuse all spend."""
    for raw in ("nan", "NaN", "inf", "Infinity", "-inf", "+infinity"):
        monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, raw)
        assert generation_budget.daily_budget_usd() == 0.0, raw
    monkeypatch.delenv(generation_budget.USD_BUDGET_ENV, raising=False)
    # Unset uses the configured finite ceiling; the emergency stop is explicit "0".
    assert generation_budget.daily_budget_usd() == generation_budget.DEFAULT_DAILY_BUDGET_USD


def test_unset_default_is_a_finite_nonzero_configured_ceiling(monkeypatch):
    """Eric's "never quiet" rule: unset must NOT disable generation. The configured
    default is a finite, positive ceiling — never 0, never NaN/inf."""
    monkeypatch.delenv(generation_budget.USD_BUDGET_ENV, raising=False)
    default = generation_budget.daily_budget_usd()
    assert default == generation_budget.DEFAULT_DAILY_BUDGET_USD
    assert math.isfinite(default)
    assert default > 0


def test_explicit_zero_is_the_named_emergency_stop(monkeypatch, tmp_path):
    """An explicit ``LAB_GENERATION_DAILY_BUDGET_USD=0`` refuses all paid
    generation by name (the emergency stop), unlike unset (configured default)."""
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "0")
    assert generation_budget.daily_budget_usd() == 0.0
    store = {"jobs": {}, "byIdempotency": {}, "served": {}}
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(store))
    assert generation_budget.debit_generation_spend_at(path, 0.28, "paid-1") is False
    assert generation_budget.debit_generation_spend_at(path, 0.28, "paid-2") is False


def test_spent_usd_fails_closed_on_corrupt_current_day(tmp_path):
    """A malformed current-day ``spentUsd`` must raise, never read as zero."""
    store = {
        "jobs": {}, "byIdempotency": {}, "served": {},
        generation_budget.BUDGET_KEY: {
            "day": generation_budget.utc_day(),
            "spentUsd": "junk",
            "calls": {},
        },
    }
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(store))
    with pytest.raises(generation_budget.BudgetLedgerCorrupt):
        generation_budget.spent_usd_at(path)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, "junk"])
def test_spent_usd_fails_closed_on_nonfinite_spent(bad, tmp_path):
    store = {
        "jobs": {}, "byIdempotency": {}, "served": {},
        generation_budget.BUDGET_KEY: {
            "day": generation_budget.utc_day(),
            "spentUsd": bad,
            "calls": {},
        },
    }
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(store))
    with pytest.raises(generation_budget.BudgetLedgerCorrupt):
        generation_budget.spent_usd_at(path)


def test_reserve_refuses_spend_on_corrupt_current_day(monkeypatch, tmp_path):
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
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(store))
    with pytest.raises(generation_budget.BudgetLedgerCorrupt):
        generation_budget.debit_generation_spend_at(path, 0.28, "replicate")


def test_invalid_amount_fails_closed(monkeypatch, tmp_path):
    """NaN/inf/negative amounts must not be silently read as zero-and-pass."""
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "25.0")
    store = {"jobs": {}, "byIdempotency": {}, "served": {}}
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(store))
    for bad in (float("nan"), float("inf"), -1.0):
        with pytest.raises(ValueError):
            generation_budget.debit_generation_spend_at(path, bad, "replicate")


def test_finite_amounts_still_reserve(monkeypatch, tmp_path):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "25.0")
    store = {"jobs": {}, "byIdempotency": {}, "served": {}}
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(store))
    assert generation_budget.debit_generation_spend_at(path, 0.28, "replicate") is True
    assert math.isfinite(generation_budget.spent_usd_at(path))


@pytest.mark.parametrize("operation", ["debit", "admission", "summary"])
@pytest.mark.parametrize("original", [
    b"{}", b'{"version":1,"byIdempotency":{},"served":{}}',
    b'{"jobs":[]}', b'{"jobs":null}',
])
def test_existing_legacy_store_requires_jobs_mapping(operation, original, tmp_path):
    path = tmp_path / "jobs.json"
    path.write_bytes(original)
    if operation == "summary":
        value = generation_budget.summary_at(path)
        assert value["corrupt"] is True
        assert value["spentUsd"] is None and value["remainingUsd"] == 0
    else:
        with pytest.raises(generation_budget.BudgetLedgerCorrupt):
            if operation == "debit":
                generation_budget.debit_generation_spend_at(path, 0.28, "new-paid-intent")
            else:
                generation_budget.can_reserve_at(path, 0.28)
    assert path.read_bytes() == original
    assert not generation_budget.ledger_path(path).exists()


@pytest.mark.parametrize("operation", ["debit", "admission", "summary"])
def test_unreadable_legacy_store_uses_named_corrupt_contract(operation, monkeypatch, tmp_path):
    from pathlib import Path
    path = tmp_path / "jobs.json"
    original = b'{"jobs":{},"byIdempotency":{},"served":{}}'
    path.write_bytes(original)
    read_bytes = Path.read_bytes
    def unreadable(target):
        if target == path:
            raise PermissionError("fixture denied read")
        return read_bytes(target)
    monkeypatch.setattr(Path, "read_bytes", unreadable)
    if operation == "summary":
        value = generation_budget.summary_at(path)
        assert value["corrupt"] is True
        assert value["spentUsd"] is None and value["remainingUsd"] == 0
    else:
        with pytest.raises(generation_budget.BudgetLedgerCorrupt):
            if operation == "debit":
                generation_budget.debit_generation_spend_at(path, 0.28, "new-paid-intent")
            else:
                generation_budget.can_reserve_at(path, 0.28)
    assert read_bytes(path) == original
    assert not generation_budget.ledger_path(path).exists()


@pytest.mark.parametrize("operation", ["debit", "admission", "summary"])
def test_only_absent_legacy_store_starts_empty(operation, tmp_path):
    path = tmp_path / "absent-jobs.json"
    if operation == "debit":
        assert generation_budget.debit_generation_spend_at(path, 0.28, "first-paid-intent")
        assert generation_budget.spent_usd_at(path) == pytest.approx(0.28)
    elif operation == "admission":
        assert generation_budget.can_reserve_at(path, 0.28)
        assert not generation_budget.ledger_path(path).exists()
    else:
        value = generation_budget.summary_at(path)
        assert value["corrupt"] is False and value["spentUsd"] == 0
        assert not generation_budget.ledger_path(path).exists()
    assert not path.exists()  # private ledger initialization never overwrites legacy job bytes
