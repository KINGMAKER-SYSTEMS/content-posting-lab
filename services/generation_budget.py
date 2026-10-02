"""Daily spend budget for paid generation admission.

The Lab's generation lane has per-request ceilings (``MAX_JOB_QUANTITY``,
``MAX_PROVIDER_CALLS``, ``MAX_CAPABILITY_QUANTITY``) but no daily total: over
14 unattended days the Control Plane Worker admits unbounded paid jobs. This
module adds a daily USD total, persisted in the SAME durable job store
(``control_plane_jobs.json``) under a top-level ``generationBudget`` key — no
new database or file. It is checked before a paid call and counted at
admission (a fail-closed reserve), and it rolls over at 00:00 UTC.

Scope: this meter covers every *new* paid generation admission — the Control
Plane generated-job plan (first attempt of each planned provider call) and the
operator-UI ``POST /api/video/generate`` route (xAI and Replicate providers).
Moderation re-submissions are a separate paid path: they are bounded per page
per UTC day by ``CONTENT_LAB_MODERATION_RETRY_DAILY_BUDGET`` (default 6) and
reported as ``moderationRetryCostUsd`` on the job, not debited from this meter.

Non-generation work (source recut, slideshows, truck-master recovery, burns,
renders, posting) never passes through this meter.
"""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("content_lab.generation_budget")


class BudgetLedgerCorrupt(Exception):
    """The durable current-day ledger is malformed and cannot be trusted.

    Callers must refuse spend (fail closed), never reset the total to zero.
    """

BUDGET_KEY = "generationBudget"
USD_BUDGET_ENV = "LAB_GENERATION_DAILY_BUDGET_USD"
DEFAULT_DAILY_BUDGET_USD = 25.0
# Fail-closed per-gen cost charged when a recipe/provider carries no usable
# ``cost_per_gen_usd``. 5.0 is the highest known paid per-gen price in the
# catalog (xAI Grok, ~$5/video); charging it for an unknown provider can only
# over-count, never let an unmetered paid provider bill as free.
DEFAULT_COST_PER_GEN_USD = 5.0
# Why 25: a conservative, fleet-wide ceiling sized to the repo's own catalog
# per-job costs (truck $0.56, scenic/boat/silhouette $1.20, silhouette-still
# $0.30), i.e. roughly 20-90 control-plane jobs/day. It is NOT a measured
# baseline; operators should set LAB_GENERATION_DAILY_BUDGET_USD from real
# spend and watch /api/health.generation_budget.
BUDGET_DEFAULT_NOTE = (
    "Default $25/day is a conservative fleet-wide ceiling (~20-90 control-plane "
    "jobs/day at current per-job costs), not a measured baseline; set "
    "LAB_GENERATION_DAILY_BUDGET_USD from measured spend."
)

# Pinned per-model costs for provider submissions that bypass the PROVIDERS
# catalog dispatch (raw-HTTP lanes in ABN/recreate). Kept here so every billable
# submission is priced in exactly one place. An unlisted model fails closed.
_MODEL_COST_USD: dict[str, float] = {
    "black-forest-labs/flux-schnell": 0.003,  # Replicate ~$0.003/image
    "wan-video/wan-2.5-i2v": 0.30,            # ~$0.06/s × 5 s looping b-roll
    "dpakkk/image-object-removal": 0.02,      # LaMa inpainting (recreate)
}


def per_gen_cost_usd_by_model(model_id: str) -> float:
    """Cost of one billable submission for an exact Replicate model id.

    Pinned to ``_MODEL_COST_USD``; an unlisted model raises (fail closed) so a
    raw-HTTP paid lane can never meter as free. Used by the ABN and recreate
    lanes that do not go through ``providers.generate_one``.
    """
    cost = _MODEL_COST_USD.get(model_id)
    if cost is None:
        raise ValueError(f"unpriced model {model_id!r}; cannot meter")
    return float(cost)


def daily_budget_usd() -> float:
    """The per-provider-family daily total, in USD. Fail closed.

    - unset            -> ``DEFAULT_DAILY_BUDGET_USD`` (the new ceiling)
    - ``> 0``          -> that ceiling
    - ``0``            -> refuse all paid generation (hard stop)
    - negative / junk / NaN / inf -> refuse all paid generation (fail closed)
    """
    raw = os.environ.get(USD_BUDGET_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_DAILY_BUDGET_USD
    try:
        value = float(raw.strip())
    except ValueError:
        value = float("nan")
    if not math.isfinite(value) or value < 0:
        log.warning(
            "%s=%r is not a finite number >= 0; paid generation disabled (fail closed)",
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


def _is_finite_nonnegative(value: Any) -> bool:
    """True only for a finite, non-negative real number (bool excluded)."""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number >= 0.0


def _finite_usd(value: Any, label: str) -> float:
    """A finite non-negative USD amount, or raise (fail closed).

    Accepts numeric strings (a catalog may store ``"0.12"``) but rejects junk,
    NaN, infinity, negatives and bools. Distinct from ``_is_finite_nonnegative``
    (used for the durable ledger, where a string total is corruption).
    """
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite non-negative number, got {value!r}")
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            raise ValueError(
                f"{label} must be a finite non-negative number, got {value!r}"
            ) from None
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        raise ValueError(f"{label} must be a finite non-negative number, got {value!r}")
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} must be a finite non-negative number, got {value!r}")
    return number


def charged_cost_per_gen(value: Any) -> float:
    """A usable per-gen cost from an explicit catalog field, fail closed.

    A missing, zero, negative, NaN, inf or non-numeric cost is rejected
    (``ValueError``) so an unpriced paid provider can never meter as free and a
    corrupt cost can never silently under-count. A positive finite numeric cost
    is returned unchanged.
    """
    cost = _finite_usd(value, "cost_per_gen_usd")
    if cost == 0.0:
        raise ValueError(f"cost_per_gen_usd must be > 0, got {value!r}")
    return cost


def per_gen_cost_usd(
    provider_id: str,
    duration_seconds: int | float | None = None,
) -> float:
    """Cost of one billable generation for ``provider_id``, from the catalog.

    Pinned to the provider catalog: a flat ``cost_per_gen_usd`` is used when
    present, otherwise a ``cost_per_second_usd`` is multiplied by the requested
    duration (so a 15 s Grok request is never under-charged as a 10 s one). An
    unknown provider, a provider with no pricing, or a per-second provider
    without a duration raises ``ValueError`` — a new paid model can never meter
    as free.
    """
    from providers import PROVIDERS

    info = PROVIDERS.get(provider_id)
    if not isinstance(info, dict):
        raise ValueError(f"unknown provider: {provider_id!r}")
    flat = info.get("cost_per_gen_usd")
    if flat is not None:
        return charged_cost_per_gen(flat)
    per_sec = info.get("cost_per_second_usd")
    if per_sec is not None:
        rate = _finite_usd(per_sec, f"{provider_id}.cost_per_second_usd")
        if rate == 0.0:
            raise ValueError(f"{provider_id}.cost_per_second_usd must be > 0")
        if duration_seconds is None or not _is_finite_nonnegative(duration_seconds) or float(duration_seconds) <= 0:
            raise ValueError(
                f"{provider_id!r} is priced per second; a positive duration is required"
            )
        return round(rate * float(duration_seconds), 4)
    raise ValueError(f"provider {provider_id!r} has no pricing; cannot meter")


def _fresh_ledger(day: str) -> dict[str, Any]:
    return {"day": day, "spentUsd": 0.0, "calls": {}, "debits": {}}


def _ledger(store: dict[str, Any], day: str) -> dict[str, Any]:
    """Return the store's ledger for ``day``, normalising a stale day in place.

    Raises ``BudgetLedgerCorrupt`` when the *current-day* total is malformed:
    silently restoring it to zero would hand back the whole day's budget.
    """
    state = store.get(BUDGET_KEY)
    if not isinstance(state, dict) or state.get("day") != day:
        state = store[BUDGET_KEY] = _fresh_ledger(day)
        return state
    if not _is_finite_nonnegative(state.get("spentUsd")):
        raise BudgetLedgerCorrupt(
            f"generationBudget.spentUsd={state.get('spentUsd')!r} "
            "is not a finite non-negative number"
        )
    state.setdefault("calls", {})
    state.setdefault("debits", {})
    return state


def spent_usd(store: dict[str, Any], now: datetime | None = None) -> float:
    """Current-day reserved spend, rollover-aware and read-only.

    Raises ``BudgetLedgerCorrupt`` when the current-day total is malformed;
    it never reports corrupt state as zero.
    """
    day = utc_day(now)
    state = store.get(BUDGET_KEY)
    if not isinstance(state, dict) or state.get("day") != day:
        return 0.0
    spent = state.get("spentUsd")
    if not _is_finite_nonnegative(spent):
        raise BudgetLedgerCorrupt(
            f"generationBudget.spentUsd={spent!r} is not a finite non-negative number"
        )
    return float(spent)


def reserve_generation_spend(
    store: dict[str, Any],
    amount_usd: float,
    key_id: str,
    now: datetime | None = None,
) -> bool:
    """Check-and-reserve ``amount_usd`` against the daily total. Returns False
    when the call would exceed the budget (the caller must refuse, never spend).

    Raises ``ValueError`` for a non-finite/negative amount and
    ``BudgetLedgerCorrupt`` for a malformed current-day ledger (both fail closed).

    ``store`` is the caller's live job-store dict and is mutated in place; the
    caller already holds ``lock_for(_jobs_path())`` and will persist it.
    """
    budget = daily_budget_usd()
    day = utc_day(now)
    state = _ledger(store, day)
    amount = _finite_usd(amount_usd, "amount_usd")
    if state["spentUsd"] + amount > budget + 1e-9:
        return False
    state["spentUsd"] = round(state["spentUsd"] + amount, 4)
    state["calls"][key_id] = int(state["calls"].get(key_id, 0) or 0) + 1
    return True


def debit_generation_spend(
    store: dict[str, Any],
    amount_usd: float,
    debit_id: str,
    now: datetime | None = None,
) -> bool:
    """Reserve one new billable provider submission, idempotently, at its UTC day.

    ``debit_id`` is a durable identity for a single new billable prediction
    (e.g. ``job-1:g00:r1:s0``). Re-calling with the same id — polling or
    resuming the SAME prediction after a restart — is a no-op that returns True,
    so a resume never charges a second time. Returns False when the day's budget
    is exhausted (the caller must refuse, never spend).
    """
    budget = daily_budget_usd()
    day = utc_day(now)
    state = _ledger(store, day)
    amount = _finite_usd(amount_usd, "amount_usd")
    if not isinstance(debit_id, str) or not debit_id:
        raise ValueError("debit_id must be a non-empty string")
    debits = state.setdefault("debits", {})
    if debit_id in debits:
        return True
    if state["spentUsd"] + amount > budget + 1e-9:
        return False
    state["spentUsd"] = round(state["spentUsd"] + amount, 4)
    debits[debit_id] = round(amount, 4)
    return True


def debit_generation_spend_at(
    path: Path | str,
    amount_usd: float,
    debit_id: str,
    now: datetime | None = None,
) -> bool:
    """Idempotent submission-time reservation against the ledger at ``path``.

    For callers that do not already hold the job-store transaction (the Control
    Plane executor loop, the Replicate PA resubmission, ABN and recreate lanes).
    Loads the store fresh and persists atomically under the same kernel lock the
    admission transaction holds. Returns False when the day's budget is
    exhausted (the caller must refuse, never spend).
    """
    from services.generation_recovery import store_lock as kernel_lock
    from services.json_store import atomic_load, atomic_save

    with kernel_lock(path):
        store = atomic_load(path, default=None)
        if not isinstance(store, dict) or "jobs" not in store:
            store = {"version": 1, "jobs": {}, "byIdempotency": {}, "served": {}}
        ok = debit_generation_spend(store, amount_usd, debit_id, now=now)
        atomic_save(path, store)
        return ok


def jobs_store_path() -> Path:
    """The durable job-store file whose ``generationBudget`` key is the ledger.

    Mirrors ``routers.control_plane._jobs_path()`` (the roster cache dir on the
    Railway volume) without importing the router, keeping this module pure and
    free of router imports. The operator-UI generate route reserves against this
    same file so Replicate and xAI share one daily total.
    """
    from services.roster import ROSTER_PATH

    return ROSTER_PATH.parent / "control_plane_jobs.json"


def reserve_generation_spend_at(
    path: Path | str,
    amount_usd: float,
    key_id: str,
    now: datetime | None = None,
) -> bool:
    """Reserve against the ledger persisted at ``path`` under its kernel lock.

    For callers that do not already hold the job-store transaction (the
    operator-UI generate route). Loads the store fresh, mutates it, and persists
    atomically under the same ``generation_recovery.store_lock`` the Control
    Plane admission transaction holds, so the two lanes serialize on one ledger.
    Returns False when the call would exceed the budget (the caller must refuse,
    never spend).
    """
    from services.generation_recovery import store_lock as kernel_lock
    from services.json_store import atomic_load, atomic_save

    with kernel_lock(path):
        store = atomic_load(path, default=None)
        if not isinstance(store, dict) or "jobs" not in store:
            store = {"version": 1, "jobs": {}, "byIdempotency": {}, "served": {}}
        ok = reserve_generation_spend(store, amount_usd, key_id, now=now)
        atomic_save(path, store)
        return ok


def summary(store: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Current budget totals for a watcher heartbeat (/api/health).

    A malformed current-day ledger is reported fail-closed (``corrupt`` true and
    the budget shown fully spent) so a corrupt state never reads as unlimited.
    """
    day = utc_day(now)
    budget = daily_budget_usd()
    try:
        spent = spent_usd(store, now)
        corrupt = False
    except BudgetLedgerCorrupt:
        spent = budget
        corrupt = True
    return {
        "day": day,
        "budgetUsd": budget,
        "spentUsd": None if corrupt else round(spent, 4),
        "remainingUsd": round(max(0.0, budget - spent), 4),
        "resetsAt": next_reset_iso(now),
        "note": BUDGET_DEFAULT_NOTE,
        "corrupt": corrupt,
    }
