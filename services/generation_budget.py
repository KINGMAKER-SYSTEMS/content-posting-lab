"""Daily spend budget for paid generation admission.

The Lab's generation lane has per-request ceilings (``MAX_JOB_QUANTITY``,
``MAX_PROVIDER_CALLS``, ``MAX_CAPABILITY_QUANTITY``) but no daily total: over
14 unattended days the Control Plane Worker admits unbounded paid jobs. This
module adds a daily USD total in a private indexed SQLite ledger next to the
durable job store. Legacy job JSON is preserved byte-for-byte during migration.
Every new paid submission is reserved against the ledger at its
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

import logging
import math
import os
import contextlib
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

log = logging.getLogger("content_lab.generation_budget")


class BudgetLedgerCorrupt(Exception):
    """The durable current-day ledger is malformed and cannot be trusted.

    Callers must refuse spend (fail closed), never reset the total to zero.
    """

BUDGET_KEY = "generationBudget"
USD_BUDGET_ENV = "LAB_GENERATION_DAILY_BUDGET_USD"
# Configured daily reservation ceiling; its stated planning basis is NOT a measurement
# (Eric's "never quiet" rule forbids a 0.0 default: unset must keep pages
# generating). Derivation (prose in BUDGET_DEFAULT_NOTE):
#   base demand  = 402 immutable recipe publications (events.md:694)
#                  × 1 standard 768p/6s Hailuo/day × $0.28/gen
#                  (published standard 768p/6s price)        $112.56
#   retry margin = a STATED 50% of base, for the moderation/PA overhead the
#                  meter now counts (E005 variants up to 3 attempts = 1 + 2
#                  rewrites; PA interruption +1 resubmission). Refusals and
#                  interruptions are exceptional, so 50% — not the 6× absolute
#                  worst case, which would be $675 and erase the ceiling.  $ 56.28
#   ABN/recreate = a NAMED flat margin, not formula-derived: the residual after
#                  the base and retry terms (175.00 - 112.56 - 56.28 = 6.16).
#                  Its thumbnail + cached b-roll components (Flux $0.003 + Wan
#                  $0.50 x _BG_LIB_TARGET 8 slots + LaMa $0.405) sum to $4.408
#                  in total; the lane's normal-day cadence is NOT measured
#                  in-repo.                                                $  6.16
#                                                                           --------
#                                                                           $175.00
DEFAULT_DAILY_BUDGET_USD = 175.0
BUDGET_DEFAULT_NOTE = (
    "Default $175/day is a configured reservation ceiling, not measured spend. "
    "Its planning basis is 402 recipes × one standard 768p/6s $0.28 Hailuo "
    "prediction = $112.56, "
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
    "wan-video/wan-2.5-i2v": 0.50,            # unchanged 5s, default 720p: $0.10/s
    "dpakkk/image-object-removal": 0.405,     # conservative T4 × default 30min maximum
}


# Raw WAN pricing: https://replicate.com/wan-video/wan-2.5-i2v/api/schema
# and https://replicate.com/wan-video/wan-2.5-i2v . LaMa reservation assumes the
# pinned public version remains on T4 at $0.000225/s and the documented default
# 1800s server maximum (not its 180s local polling timeout). No invoice-exactness
# or refund is claimed. Reverify these assumptions if hardware/deadlines change:
# https://replicate.com/pricing
# https://replicate.com/docs/topics/predictions/lifecycle/
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
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()


def _next_reset(now: datetime | None = None) -> datetime:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
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
    *, resolution: str | None = None, parameters: dict[str, Any] | None = None,
) -> float:
    """Price one exact model through the same payload authority as the UI."""
    from providers import PROVIDERS

    for provider_id, info in PROVIDERS.items():
        if not isinstance(info, dict):
            continue
        if model_id in (info.get("models") or []):
            return per_gen_cost_usd(provider_id, duration_seconds, resolution=resolution, parameters=parameters)
    raise ValueError(f"unknown replicate model: {model_id!r}")


def per_gen_cost_usd(
    provider_id: str,
    duration_seconds: int | float | None = None,
    *, resolution: str | None = None, parameters: dict[str, Any] | None = None,
) -> float:
    """Price the unchanged provider payload, never an assumed clip duration.

    The catalog holds current published rates. Builders select the charged
    resolution, duration and interpolation. This pure calculation never sends
    requests or changes creative inputs; an unsupported price fails closed.
    """
    from providers import PROVIDERS
    from providers.replicate import _INPUT_BUILDERS

    info = PROVIDERS.get(provider_id)
    if not isinstance(info, dict):
        raise ValueError(f"unknown provider: {provider_id!r}")
    params = dict(parameters or {})
    if duration_seconds is not None:
        if not _is_finite_nonnegative(duration_seconds):
            raise ValueError("duration must be a finite non-negative number")
        params["duration"] = duration_seconds
    if resolution is not None:
        params["resolution"] = resolution
    if "resolution" in params and not isinstance(params["resolution"], str):
        raise ValueError("resolution must be a string")
    model = (info.get("models") or [None])[0]
    if model == "grok-imagine-video":
        duration = params.get("duration")
        if type(duration) is not int or not 1 <= duration <= 15:
            raise ValueError("Grok duration must be an integer from 1 to 15")
        rate = (info.get("cost_per_second_usd_by_resolution") or {}).get(params.get("resolution", "720p"))
        cost = charged_cost_per_gen(rate) * duration
        if params.get("image_data_uri"):
            cost += charged_cost_per_gen(info.get("cost_per_input_image_usd"))
        return round(cost, 4)
    builder = _INPUT_BUILDERS.get(model)
    if builder is None:
        raise ValueError(f"provider {provider_id!r} has no priced payload builder")
    params.setdefault("variant", info.get("variant"))
    # Image content does not select a WAN price. Admission runs before the
    # immutable recipe anchor is loaded; this placeholder is never submitted.
    if model in {"wan-video/wan-2.2-i2v-a14b", "wan-video/wan-2.2-i2v-fast"}:
        params.setdefault("image_data_uri", "pricing-only")
    payload = builder("", params)
    if model == "prunaai/p-video":
        rate = (info.get("cost_per_second_usd_by_resolution") or {}).get(payload["resolution"])
        return round(charged_cost_per_gen(rate) * payload["duration"], 4)
    if model == "black-forest-labs/flux-2-pro":
        # This builder does not forward input images, custom dimensions or
        # arbitrary MP values. Charge the run fee plus its actual output MP.
        megapixels = {"1 MP": 1, "2 MP": 2}.get(payload["resolution"])
        if megapixels is None:
            raise ValueError("unpriced FLUX output resolution")
        return round(charged_cost_per_gen(info.get("cost_per_run_usd")) +
                     charged_cost_per_gen(info.get("cost_per_output_megapixel_usd")) * megapixels, 4)
    price = (info.get("cost_per_gen_usd_by_resolution") or {}).get(payload["resolution"])
    if model == "minimax/hailuo-2.3":
        price = price.get(payload["duration"]) if isinstance(price, dict) else None
    elif model == "wan-video/wan-2.2-i2v-fast":
        interpolation = payload["interpolate_output"]
        if type(interpolation) is not bool:
            raise ValueError("WAN interpolation must be a boolean")
        price = price.get("interpolate" if interpolation else "base") if isinstance(price, dict) else None
    return charged_cost_per_gen(price)


def _legacy_spent_usd(store: dict[str, Any], now: datetime | None = None) -> float:
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


def debit_generation_spend_at(
    path: Path | str,
    amount_usd: float,
    debit_id: str,
    now: datetime | None = None,
) -> bool:
    """Atomically append one exact paid-intent ID and its UTC-day amount.

    A known ID remains free even after rollover or a pricing change. No debit
    rewrites the job store or scans prior debit rows. Uncertain intents stay
    charged; this meter never refunds, deletes, or re-submits provider work.
    """
    amount = _money(_finite_usd(amount_usd, "amount_usd"))
    if amount <= 0 or not isinstance(debit_id, str) or not debit_id:
        raise ValueError("a paid debit requires a positive amount and exact non-empty ID")
    with _database(path, write=True) as db:
        # The writer lock can wait across midnight; charge the actual locked
        # submission day. An explicitly supplied clock remains deterministic.
        day = utc_day(now)
        old = db.execute("SELECT amount_usd FROM debits WHERE id=?", (debit_id,)).fetchone()
        if old is not None:
            _money(old[0])  # Corrupt audit evidence never grants a free replay.
            return True
        spent = _database_spent(db, day)
        total = _add_money(spent, amount)
        if total > _money(daily_budget_usd()):
            return False
        db.execute("INSERT INTO debits(id,day,amount_usd) VALUES(?,?,?)",
                   (debit_id, day, str(amount)))
        db.execute("INSERT INTO days(day,spent_usd) VALUES(?,?) "
                   "ON CONFLICT(day) DO UPDATE SET spent_usd=excluded.spent_usd",
                   (day, str(total)))
        return True


def jobs_store_path() -> Path:
    """The durable job store adjacent to the private generation-budget ledger.

    Mirrors ``routers.control_plane._jobs_path()`` (the roster cache dir on the
    Railway volume) without importing the router, keeping this module pure and
    free of router imports. The operator-UI generate route reserves against this
    same private ledger so Replicate and xAI share one daily total.
    """
    from services.roster import ROSTER_PATH

    return ROSTER_PATH.parent / "control_plane_jobs.json"


def can_reserve_at(path: Path | str, amount_usd: float, now: datetime | None = None) -> bool:
    """Read-only admission check; queued work reserves only when it executes."""
    return _add_money(_spent_money_at(path, now), _money(_finite_usd(amount_usd, "amount_usd"))) <= _money(daily_budget_usd())


def ledger_path(path: Path | str) -> Path:
    path = Path(path).resolve()
    return path.parent / "_generation_budget" / (path.name + ".sqlite3")


def _origin_path(path: Path) -> Path:
    return path.with_suffix(".origin.json")


def _money(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise BudgetLedgerCorrupt("generation budget amount is corrupt") from error
    if not amount.is_finite() or amount < 0:
        raise BudgetLedgerCorrupt("generation budget amount is corrupt")
    if not math.isfinite(float(amount)):
        raise BudgetLedgerCorrupt("generation budget amount exceeds finite storage bounds")
    return amount


def _add_money(left: Decimal, right: Decimal) -> Decimal:
    # A finite float can have 309 integral places. Preserve even tiny charges
    # beside a large configured or migrated total without a global context change.
    with localcontext() as context:
        context.prec = 700
        return left + right


def _legacy_snapshot(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}, b""
    try:
        store = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise BudgetLedgerCorrupt("legacy jobs JSON is corrupt; refusing migration") from error
    if not isinstance(store, dict) or ("jobs" in store and not isinstance(store["jobs"], dict)):
        raise BudgetLedgerCorrupt("legacy jobs store is malformed; refusing migration")
    if BUDGET_KEY in store:
        state = store[BUDGET_KEY]
        if not isinstance(state, dict):
            raise BudgetLedgerCorrupt("legacy budget is malformed")
        try:
            day = datetime.strptime(state["day"], "%Y-%m-%d").date().isoformat()
        except (KeyError, TypeError, ValueError) as error:
            raise BudgetLedgerCorrupt("legacy budget day is malformed") from error
        if day != state["day"] or not _is_finite_nonnegative(state.get("spentUsd")):
            raise BudgetLedgerCorrupt("legacy budget total is malformed")
        debits = state.get("debits", {})
        if not isinstance(debits, dict) or any(
            not isinstance(key, str) or not key or not _is_finite_nonnegative(amount)
            for key, amount in debits.items()
        ):
            raise BudgetLedgerCorrupt("legacy debit identity or amount is malformed")
    return store, raw


_APPLICATION_ID = 0x434C4247
_SCHEMA = (
    "CREATE TABLE migration(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,instance TEXT NOT NULL,source_path TEXT NOT NULL,source_hash TEXT NOT NULL,legacy_json BLOB NOT NULL)",
    "CREATE TABLE days(day TEXT PRIMARY KEY,spent_usd TEXT NOT NULL)",
    "CREATE TABLE debits(id TEXT PRIMARY KEY,day TEXT,amount_usd TEXT NOT NULL)",
    "CREATE TRIGGER immutable_debits_update BEFORE UPDATE ON debits BEGIN SELECT RAISE(ABORT,'immutable debit'); END",
    "CREATE TRIGGER immutable_debits_delete BEFORE DELETE ON debits BEGIN SELECT RAISE(ABORT,'immutable debit'); END",
    "CREATE TRIGGER immutable_migration_update BEFORE UPDATE ON migration BEGIN SELECT RAISE(ABORT,'immutable migration'); END",
    "CREATE TRIGGER immutable_migration_delete BEFORE DELETE ON migration BEGIN SELECT RAISE(ABORT,'immutable migration'); END",
)


def _initialize_database(source: Path, target: Path) -> None:
    """One atomic migration; original JSON and unknown intents remain intact."""
    store, raw = _legacy_snapshot(source)
    instance = uuid.uuid4().hex
    temporary = target.with_name(target.name + "." + instance + ".init")
    db = sqlite3.connect(temporary)
    try:
        db.execute("PRAGMA synchronous=FULL")
        db.execute(f"PRAGMA application_id={_APPLICATION_ID}")
        db.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA:
            db.execute(statement)
        db.execute("INSERT INTO migration VALUES(1,1,?,?,?,?)",
                   (instance, str(source), hashlib.sha256(raw).hexdigest(), raw))
        state = store.get(BUDGET_KEY)
        if state is not None:
            db.execute("INSERT INTO days VALUES(?,?)", (state["day"], str(_money(state["spentUsd"]))))
            # Old IDs have no recorded submission day; preserve them without
            # inventing dates or re-debiting today's total from their sum.
            db.executemany("INSERT INTO debits VALUES(?,NULL,?)",
                           ((key, str(_money(amount))) for key, amount in state.get("debits", {}).items()))
        db.commit()
        db.close()
        temporary.chmod(0o600)
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        db.close()
        if temporary.exists():
            temporary.unlink()


def _database_identity(db: sqlite3.Connection, source: Path, target: Path) -> dict[str, Any]:
    if db.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID:
        raise BudgetLedgerCorrupt("budget database identity mismatch")
    row = db.execute("SELECT version,instance,source_path FROM migration WHERE id=1").fetchone()
    if row is None or row[0] != 1 or row[2] != str(source) or not isinstance(row[1], str) or len(row[1]) != 32:
        raise BudgetLedgerCorrupt("budget migration identity mismatch")
    identity = {"version": row[0], "instance": row[1], "sourcePath": row[2]}
    marker = _origin_path(target)
    if marker.exists():
        try:
            recorded = json.loads(marker.read_bytes())
        except (ValueError, UnicodeError) as error:
            raise BudgetLedgerCorrupt("budget origin marker is corrupt") from error
        if recorded != identity:
            raise BudgetLedgerCorrupt("budget origin marker mismatch")
    return identity


@contextlib.contextmanager
def _database(path: Path | str, *, write: bool = False):
    from services.generation_recovery import store_lock
    from services.json_store import atomic_save

    source = Path(path).resolve()
    target = ledger_path(source)
    marker = _origin_path(target)
    if target.parent.is_symlink() or target.is_symlink() or marker.is_symlink():
        raise BudgetLedgerCorrupt("budget storage cannot be a symlink")
    if marker.exists() and not target.exists():
        raise BudgetLedgerCorrupt("budget database missing; refusing to reset paid history")
    guard = store_lock(target) if write else contextlib.nullcontext()
    db = None
    try:
        if write:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with guard:
            if not target.exists():
                if not write:
                    raise BudgetLedgerCorrupt("budget database missing")
                _initialize_database(source, target)
            # mode=rw never creates a blank replacement after a missing DB.
            db = sqlite3.connect(target.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                 uri=True, timeout=5, isolation_level=None)
            identity = _database_identity(db, source, target)
            if write:
                # Reconcile the bounded marker after a crash between the atomic
                # database replacement and marker write, before any paid call.
                if not marker.exists():
                    atomic_save(marker, identity)
                    marker.chmod(0o600)
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                if write:
                    db.commit()
            except BaseException:
                if write:
                    db.rollback()
                raise
    except (sqlite3.Error, OSError) as error:
        raise BudgetLedgerCorrupt("generation budget storage unavailable or corrupt") from error
    finally:
        if db is not None:
            db.close()


def _database_spent(db: sqlite3.Connection, day: str) -> Decimal:
    row = db.execute("SELECT spent_usd FROM days WHERE day=?", (day,)).fetchone()
    return Decimal(0) if row is None else _money(row[0])


def _spent_money_at(path: Path | str, now: datetime | None = None) -> Decimal:
    target = ledger_path(path)
    if not target.exists() and not _origin_path(target).exists():
        store, _ = _legacy_snapshot(Path(path).resolve())
        return _money(_legacy_spent_usd(store, now))
    with _database(path) as db:
        return _database_spent(db, utc_day(now))


def spent_usd_at(path: Path | str, now: datetime | None = None) -> float:
    return float(_spent_money_at(path, now))


def summary_at(path: Path | str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    budget = daily_budget_usd()
    try:
        spent = spent_usd_at(path, now)
        corrupt = False
    except BudgetLedgerCorrupt:
        spent, corrupt = budget, True
    return {"day": utc_day(now), "budgetUsd": budget,
            "spentUsd": None if corrupt else round(spent, 4),
            "remainingUsd": round(max(0.0, budget - spent), 4),
            "resetsAt": next_reset_iso(now), "note": BUDGET_DEFAULT_NOTE, "corrupt": corrupt}


def failure_detail(now: datetime | None = None) -> str:
    """Named reset within the Worker's existing bounded errorDetail contract."""
    return "daily_budget reset=" + next_reset_iso(now).replace("+00:00", "Z")
