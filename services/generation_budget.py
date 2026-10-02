"""Daily TOTAL spend budget for paid generation providers.

The Lab's generation lane has per-request ceilings (``MAX_JOB_QUANTITY``,
``MAX_PROVIDER_CALLS``, ``MAX_CAPABILITY_QUANTITY``) but no daily total: over
14 unattended days the Control Plane Worker admits unbounded paid jobs. This
module adds a daily USD total, persisted in the SAME durable job store
(``control_plane_jobs.json``) under a top-level ``generationBudget`` key — no
new database or file. It is checked before a paid call and counted at
admission (a fail-closed reserve), and it rolls over at 00:00 UTC.

Non-generation work (source recut, slideshows, truck-master recovery, burns,
renders, posting) never passes through this meter.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("content_lab.generation_budget")

BUDGET_KEY = "generationBudget"
USD_BUDGET_ENV = "LAB_GENERATION_DAILY_BUDGET_USD"
DEFAULT_DAILY_BUDGET_USD = 25.0
# Fail-closed per-gen cost charged when a recipe/provider carries no usable
# ``cost_per_gen_usd``. 5.0 is the highest known paid per-gen price in the
# catalog (xAI Grok, ~$5/video); charging it for an unknown provider can only
# over-count, never let an unmetered paid provider bill as free.
DEFAULT_COST_PER_GEN_USD = 5.0


def daily_budget_usd() -> float:
    """The per-provider-family daily total, in USD. Fail closed.

    - unset            -> ``DEFAULT_DAILY_BUDGET_USD`` (the new ceiling)
    - ``> 0``          -> that ceiling
    - ``0``            -> refuse all paid generation (hard stop)
    - negative / junk  -> refuse all paid generation (fail closed, spend-safe)
    """
    raw = os.environ.get(USD_BUDGET_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_DAILY_BUDGET_USD
    try:
        value = float(raw.strip())
    except ValueError:
        value = -1.0
    if value < 0:
        log.warning(
            "%s=%r is not a number >= 0; paid generation disabled (fail closed)",
            USD_BUDGET_ENV, raw,
        )
        return 0.0
    return value


def utc_day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).date().isoformat()


def _next_reset(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc) + timedelta(days=1)


def next_reset_iso(now: datetime | None = None) -> str:
    return _next_reset(now).isoformat()


def retry_after_seconds(now: datetime | None = None) -> int:
    """Whole seconds until the next UTC reset, for a ``Retry-After`` header.

    Always ``>= 1`` so a client never reads ``0`` (which means "may retry
    immediately") when the reset is under a second away.
    """
    now = now or datetime.now(timezone.utc)
    seconds = (_next_reset(now) - now).total_seconds()
    return max(1, int(seconds) + (1 if seconds > int(seconds) else 0))


def charged_cost_per_gen(value: Any) -> float:
    """A usable per-gen cost, never zero.

    A missing, zero, negative or non-numeric cost is charged the fail-closed
    ``DEFAULT_COST_PER_GEN_USD`` so an unknown paid provider can never meter as
    free. A positive numeric cost is returned unchanged.
    """
    try:
        cost = float(value)
    except (TypeError, ValueError):
        cost = 0.0
    return cost if cost > 0 else DEFAULT_COST_PER_GEN_USD


def _fresh_ledger(day: str) -> dict[str, Any]:
    return {"day": day, "spentUsd": 0.0, "calls": {}}


def _ledger(store: dict[str, Any], day: str) -> dict[str, Any]:
    """Return the store's ledger for ``day``, normalising a stale day in place."""
    state = store.get(BUDGET_KEY)
    if not isinstance(state, dict) or state.get("day") != day:
        state = store[BUDGET_KEY] = _fresh_ledger(day)
    spent = state.get("spentUsd")
    if not isinstance(spent, (int, float)):
        state["spentUsd"] = 0.0
    calls = state.get("calls")
    if not isinstance(calls, dict):
        state["calls"] = {}
    return state


def spent_usd(store: dict[str, Any], now: datetime | None = None) -> float:
    """Current-day reserved spend, rollover-aware and read-only."""
    day = utc_day(now)
    state = store.get(BUDGET_KEY)
    if not isinstance(state, dict) or state.get("day") != day:
        return 0.0
    spent = state.get("spentUsd")
    return float(spent) if isinstance(spent, (int, float)) else 0.0


def reserve_generation_spend(
    store: dict[str, Any],
    amount_usd: float,
    key_id: str,
    now: datetime | None = None,
) -> bool:
    """Check-and-reserve ``amount_usd`` against the daily total. Returns False
    when the call would exceed the budget (the caller must refuse, never spend).

    ``store`` is the caller's live job-store dict and is mutated in place; the
    caller already holds ``lock_for(_jobs_path())`` and will persist it.
    """
    budget = daily_budget_usd()
    day = utc_day(now)
    state = _ledger(store, day)
    amount = float(amount_usd) if isinstance(amount_usd, (int, float)) and amount_usd > 0 else 0.0
    if state["spentUsd"] + amount > budget + 1e-9:
        return False
    state["spentUsd"] = round(state["spentUsd"] + amount, 4)
    state["calls"][key_id] = int(state["calls"].get(key_id, 0) or 0) + 1
    return True


def summary(store: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Current budget totals for a watcher heartbeat (/api/health)."""
    day = utc_day(now)
    spent = spent_usd(store, now)
    budget = daily_budget_usd()
    return {
        "day": day,
        "budgetUsd": budget,
        "spentUsd": round(spent, 4),
        "remainingUsd": round(max(0.0, budget - spent), 4),
        "resetsAt": next_reset_iso(now),
    }
