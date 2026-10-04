"""Daily spend budget for paid generation admission.

The Lab's generation lane has per-request ceilings (``MAX_JOB_QUANTITY``,
``MAX_PROVIDER_CALLS``, ``MAX_CAPABILITY_QUANTITY``) but no daily total: over
14 unattended days the Control Plane Worker admits unbounded paid jobs. This
module adds a daily USD total, persisted in the SAME durable job store
(``control_plane_jobs.json``) under a top-level ``generationBudget`` key — no
new database or file. Every new paid submission is reserved against it at its
own UTC submission day (fail-closed); resuming or polling the same prediction
stays free, and the total rolls over at 00:00 UTC.

Scope: this meter covers every *new* paid generation submission — the Control
Plane generated-job plan (each attempt of each planned provider call) and the
operator-UI ``POST /api/video/generate`` route (xAI and Replicate providers).
A moderation re-submission is a distinct billable prediction and IS debited
from this meter; ``CONTENT_LAB_MODERATION_RETRY_DAILY_BUDGET`` (default 6) is
an additional per-page count bound, not a replacement for the USD cap.

Non-generation work (source recut, slideshows, truck-master recovery, burns,
renders, posting) never passes through this meter.
"""

from __future__ import annotations

import json
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
# Derived daily ceiling — an UPPER BOUND from repo facts, NOT a measurement
# (Eric's "never quiet" rule forbids a 0.0 default: unset must keep pages
# generating). Derivation (prose in BUDGET_DEFAULT_NOTE):
#   base demand  = 402 immutable recipe publications (events.md:694)
#                  × 1 Hailuo keeper/day × $0.28/gen
#                  (providers/__init__.py hailuo.cost_per_gen_usd)        $112.56
#   retry margin = a STATED 50% of base, for the moderation/PA overhead the
#                  meter now counts (E005 variants up to 3 attempts = 1 + 2
#                  rewrites; PA interruption +1 resubmission). Refusals and
#                  interruptions are exceptional, so 50% — not the 6× absolute
#                  worst case, which would be $675 and erase the ceiling.  $ 56.28
#   ABN/recreate = a NAMED flat margin, not formula-derived: the residual after
#                  the base and retry terms (175.00 - 112.56 - 56.28 = 6.16).
#                  Its thumbnail + cached b-roll components (Flux $0.003 + Wan
#                  $0.30 x _BG_LIB_TARGET 8 slots + LaMa $0.02) sum to $2.42
#                  in total; the lane's normal-day cadence is NOT measured
#                  in-repo.                                                $  6.16
#                                                                           --------
#                                                                           $175.00
DEFAULT_DAILY_BUDGET_USD = 175.0
# Fail-closed per-gen cost charged when a recipe/provider carries no usable
# ``cost_per_gen_usd``. 5.0 is the highest known paid per-gen price in the
# catalog (xAI Grok, ~$5/video); charging it for an unknown provider can only
# over-count, never let an unmetered paid provider bill as free.
DEFAULT_COST_PER_GEN_USD = 5.0
BUDGET_DEFAULT_NOTE = (
    "Default $175/day is an upper bound derived from repo facts, not a "
    "measurement: 402 published recipes × one $0.28 Hailuo keeper = $112.56, "
    "plus a stated 50% moderation/PA retry margin ($56.28) and a named $6.16 "
    "ABN/recreate margin. Unset uses this default; set "
    "LAB_GENERATION_DAILY_BUDGET_USD=0 for the named emergency stop (all paid "
    "generation refused)."
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

    - unset / blank    -> ``DEFAULT_DAILY_BUDGET_USD`` (the derived ceiling)
    - ``> 0``          -> that ceiling
    - ``0``            -> named emergency stop: refuse all paid generation
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
    if value == 0.0:
        log.warning(
            "%s=0 is the named emergency stop; all paid generation refused",
            USD_BUDGET_ENV,
        )
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


def catalog_cost_usd_by_model(
    model_id: str,
    duration_seconds: int | float | None = None,
) -> float:
    """Cost of one billable submission for an exact Replicate model id.

    Maps ``model_id`` to its provider entry in ``providers.PROVIDERS`` and
    prices it exactly as the operator-UI route does (flat ``cost_per_gen_usd``
    when present, else ``cost_per_second_usd`` × duration). An unknown model, a
    provider with no pricing, or a per-second provider without a duration raises
    ``ValueError`` — a paid lane can never meter as free. This is the single
    pricing source of truth for the moderation-retry (executor) lane.
    """
    from providers import PROVIDERS

    for provider_id, info in PROVIDERS.items():
        if not isinstance(info, dict):
            continue
        if model_id in (info.get("models") or []):
            return per_gen_cost_usd(provider_id, duration_seconds)
    raise ValueError(f"unknown replicate model: {model_id!r}")


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

    On a UTC day rollover ``spentUsd`` and ``calls`` reset, but the idempotent
    ``debits`` map is PRESERVED so resuming/polling a prediction submitted on a
    prior day stays free (the same debit id is recognised as already charged),
    while a *new* submission (a fresh debit id) is charged to the new day.
    """
    state = store.get(BUDGET_KEY)
    if not isinstance(state, dict) or state.get("day") != day:
        prior_debits = state.get("debits", {}) if isinstance(state, dict) else {}
        state = store[BUDGET_KEY] = _fresh_ledger(day)
        state["debits"] = prior_debits
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


def can_reserve(
    store: dict[str, Any],
    amount_usd: float,
    now: datetime | None = None,
) -> bool:
    """Read-only: can the current-day ledger afford ``amount_usd`` right now?

    The admission-time cheap check. Never mutates ``store`` (a refusal must leave
    no partial ledger write behind). Raises ``ValueError`` for a non-finite/
    negative amount and ``BudgetLedgerCorrupt`` for a malformed current-day
    ledger — both fail closed. The per-submission ``debit_generation_spend``
    remains the real cap; this only avoids creating a job that cannot even
    afford its first billable submission.
    """
    budget = daily_budget_usd()
    amount = _finite_usd(amount_usd, "amount_usd")
    return spent_usd(store, now) + amount <= budget + 1e-9


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
    from services.json_store import atomic_save

    with kernel_lock(path):
        store = _load_jobs_store_or_initialize(path)
        ok = debit_generation_spend(store, amount_usd, debit_id, now=now)
        atomic_save(path, store)
        return ok


def _load_jobs_store_or_initialize(path: Path | str) -> dict[str, Any]:
    """Load an existing job store or initialize only when it is absent.

    A corrupt/unreadable file is not equivalent to a new store: replacing it
    with an empty ledger would erase today's reserved spend and hand back the
    full daily budget. Fail closed and leave the original bytes untouched.
    """
    store_path = Path(path)
    try:
        with store_path.open("r", encoding="utf-8") as stream:
            store = json.load(stream)
    except FileNotFoundError:
        return {"version": 1, "jobs": {}, "byIdempotency": {}, "served": {}}
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BudgetLedgerCorrupt(
            f"cannot read generation job store {store_path}: {error}"
        ) from error

    if not isinstance(store, dict) or not isinstance(store.get("jobs"), dict):
        raise BudgetLedgerCorrupt(
            f"generation job store {store_path} has an invalid top-level shape"
        )
    return store


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
    from services.json_store import atomic_save

    with kernel_lock(path):
        store = _load_jobs_store_or_initialize(path)
        ok = reserve_generation_spend(store, amount_usd, key_id, now=now)
        atomic_save(path, store)
        return ok


def can_reserve_at(path: Path | str, amount_usd: float, now: datetime | None = None) -> bool:
    """Read-only admission check; queued work reserves only when it executes."""
    from services.generation_recovery import store_lock as kernel_lock
    with kernel_lock(path):
        store = _load_jobs_store_or_initialize(path)
        return can_reserve(store, amount_usd, now=now)


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
