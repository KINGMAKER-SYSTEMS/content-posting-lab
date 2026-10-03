"""Daily TOTAL generation budget: meter, refusal at the cap, UTC rollover,
and proof that non-generation work (burns, renders, source, slideshow) is
never gated.

These tests fail on 0d9dd68 (no budget code exists there) — the red-proof for
Lane F2: a per-request ceiling never becomes a daily total, so money leaks
across 14 unattended days.
"""

import pathlib
import threading
from datetime import datetime, timedelta, timezone

import pytest

import routers.control_plane as cp
from routers import video as video_router
from services import generation_budget
from services.json_store import atomic_load
from tests.test_control_plane_dossier_execution import (
    HEADERS, PAGE_ID, TOKEN, job_body, lab,  # noqa: F401 (fixture)
)


async def _fake_generate_one(
    job_id, index, provider, prompt, aspect_ratio, resolution, duration,
    image_data_uri, jobs, output_dir, url_prefix, on_complete=None, **extra,
):
    """Hermetic stand-in for providers.base.generate_one (no network)."""
    entry = jobs[job_id]["videos"][index]
    entry["status"] = "done"
    entry["file"] = f"{job_id}_{index}.mp4"
    if on_complete:
        on_complete(job_id)


# ── meter (pure) ─────────────────────────────────────────────────────────

def test_daily_budget_parses_spend_safe(monkeypatch):
    monkeypatch.delenv(generation_budget.USD_BUDGET_ENV, raising=False)
    # Unset == the derived finite ceiling (never 0, never a silent unproven $25).
    assert generation_budget.daily_budget_usd() == generation_budget.DEFAULT_DAILY_BUDGET_USD
    for raw, expected in [("0", 0.0), ("12.5", 12.5), (" 3 ", 3.0), ("-1", 0.0), ("many", 0.0)]:
        monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, raw)
        assert generation_budget.daily_budget_usd() == expected


def test_missing_cost_fails_closed():
    """F5: a missing/zero/negative/non-numeric/non-finite cost is rejected, so
    an unpriced paid provider can never meter as free. A real positive cost
    passes through unchanged."""
    for bad in (None, 0, 0.0, -1, "junk", float("nan"), float("inf")):
        with pytest.raises(ValueError):
            generation_budget.charged_cost_per_gen(bad)
    assert generation_budget.charged_cost_per_gen(0.28) == pytest.approx(0.28)
    assert generation_budget.charged_cost_per_gen("0.12") == pytest.approx(0.12)


def test_reserve_counts_and_refuses_at_the_cap(monkeypatch):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "1.0")
    store = {"jobs": {}, "byIdempotency": {}, "served": {}}
    assert generation_budget.reserve_generation_spend(store, 0.28, "replicate") is True
    assert generation_budget.reserve_generation_spend(store, 0.28, "replicate") is True
    assert generation_budget.reserve_generation_spend(store, 0.28, "replicate") is True
    # 3 x 0.28 = 0.84; a 4th would exceed 1.0.
    assert generation_budget.reserve_generation_spend(store, 0.28, "replicate") is False
    assert generation_budget.spent_usd(store) == pytest.approx(0.84)


def test_rollover_resets_spend_at_the_next_utc_day(monkeypatch):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "1.0")
    store = {"jobs": {}, "byIdempotency": {}, "served": {}}
    day1 = datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc)
    assert generation_budget.reserve_generation_spend(store, 0.5, "replicate", now=day1) is True
    assert generation_budget.spent_usd(store, now=day1) == pytest.approx(0.5)
    # Same amount on the next UTC day is a fresh ledger.
    day2 = datetime(2026, 10, 1, 0, 5, tzinfo=timezone.utc)
    assert generation_budget.spent_usd(store, now=day2) == 0.0
    assert generation_budget.reserve_generation_spend(store, 0.5, "replicate", now=day2) is True
    summary = generation_budget.summary(store, now=day2)
    assert summary["day"] == "2026-10-01"
    assert summary["spentUsd"] == pytest.approx(0.5)
    assert summary["resetsAt"].startswith("2026-10-02")


def test_summary_reports_budget_and_resets_at(monkeypatch):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "2.0")
    store = {"jobs": {}, "byIdempotency": {}, "served": {}}
    generation_budget.reserve_generation_spend(store, 0.75, "replicate")
    summary = generation_budget.summary(store)
    assert summary["budgetUsd"] == 2.0
    assert summary["spentUsd"] == pytest.approx(0.75)
    assert summary["remainingUsd"] == pytest.approx(1.25)
    assert "resetsAt" in summary and "day" in summary


# ── admission: pricing is validated, spend is metered at submission ─────

def test_create_job_admission_validates_pricing_and_defers_spend(lab, monkeypatch):
    """F1 (revised): admission no longer reserves the whole planned amount (that
    would let queued work shift unbounded reserved spend across midnight).
    Admission validates the recipe provider is priced (fail closed), runs a cheap
    read-only gate that the FIRST billable submission can be afforded, and does
    not touch the ledger; each billable submission is debited at its own UTC day
    by the executor loop."""
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "100.0")
    client, _, _ = lab
    response = client.post("/api/control-plane/v1/jobs", json=job_body(), headers=HEADERS)
    assert response.status_code == 200
    # No reservation happened at admission (only the cheap read-only gate ran).
    assert generation_budget.spent_usd(cp._load_jobs()) == 0.0


# ── non-generation work is never gated ───────────────────────────────────

def test_generation_budget_gate_lives_only_in_the_paid_generation_lanes():
    """Burns, renders, source recut, slideshows and posting never call the meter.

    The gate is reachable only from the paid-generation lanes — the Control
    Plane executor/admission (routers/control_plane.py), the operator-UI
    generate route (routers/video.py), and the Replicate provider adapter
    (PA resubmission). Every other router must not even import it, so a
    burn/render/posting request can never be blocked by the daily generation
    budget.
    """
    repo = pathlib.Path(cp.__file__).resolve().parents[1]
    importers = []
    callers = []
    for path in (repo / "routers").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "generation_budget" in text:
            importers.append(path.name)
        if "reserve_generation_spend" in text or "debit_generation_spend" in text:
            callers.append(path.name)
    assert sorted(callers) == ["control_plane.py", "video.py"], f"unexpected budget gate callers: {callers}"
    # control_plane and video are the only routers allowed to import the meter.
    assert sorted(importers) == ["control_plane.py", "video.py"], f"unexpected budget importers: {importers}"


def test_health_reports_the_generation_budget(sync_client, monkeypatch, tmp_path):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "7.0")
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    response = sync_client.get("/api/health")
    assert response.status_code == 200
    budget = response.json()["generation_budget"]
    assert budget["budgetUsd"] == 7.0
    assert budget["spentUsd"] == 0.0
    assert "remainingUsd" in budget and "resetsAt" in budget and "day" in budget
    assert "note" in budget and "LAB_GENERATION_DAILY_BUDGET_USD" in budget["note"]


# ── operator UI generate path (F2) ───────────────────────────────────────

_UI_GENERATE_FORM = {
    "prompt": "test prompt",
    "provider": "grok",
    "count": "1",
    "duration": "5",
    "aspect_ratio": "9:16",
    "resolution": "720p",
    "project": "budget-suite",
}


def test_ui_generate_is_refused_at_the_daily_budget(sync_client, monkeypatch):
    """F2: the operator UI path shares the same daily meter and is refused at
    the cap with the same machine-readable reason + resets_at and Retry-After."""
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "1.0")
    monkeypatch.setitem(video_router.API_KEYS, "xai", "test-key")
    monkeypatch.setattr(video_router, "generate_one", _fake_generate_one)
    response = sync_client.post("/api/video/generate", data=_UI_GENERATE_FORM)
    # grok costs 5.0/gen; count 1 = 5.0 > 1.0 budget, so refused.
    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["error"] == "generation_daily_budget_reached"
    assert isinstance(detail["resets_at"], str) and detail["resets_at"].endswith("+00:00")
    retry_after = response.headers.get("Retry-After")
    assert retry_after is not None and retry_after.isdigit() and int(retry_after) >= 1


def test_ui_generate_debits_queued_execution_under_budget(sync_client, monkeypatch):
    """Admission checks affordability; only the queued execution debits spend."""
    completed = threading.Event()

    async def finish_job(*args, **kwargs):
        await _fake_generate_one(*args, **kwargs)
        completed.set()

    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "25.0")
    monkeypatch.setitem(video_router.API_KEYS, "xai", "test-key")
    monkeypatch.setattr(video_router, "generate_one", finish_job)
    response = sync_client.post("/api/video/generate", data=_UI_GENERATE_FORM)
    assert response.status_code == 200
    assert completed.wait(timeout=5), "the exact queued index did not complete"
    store = atomic_load(generation_budget.jobs_store_path())
    # The wrapper charges queued execution even if this fixture bypasses the
    # actual provider. Real request-boundary coverage lives in its own suite.
    assert generation_budget.spent_usd(store) == pytest.approx(2.50)
    assert len(store["generationBudget"]["debits"]) == 1


def test_ui_generate_fails_closed_for_unpriced_provider(sync_client, monkeypatch):
    """F5 (UI): a provider with no pricing is refused (never metered as free)."""
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "100.0")
    monkeypatch.setitem(video_router.API_KEYS, "replicate", "test-key")
    monkeypatch.setattr(video_router, "generate_one", _fake_generate_one)
    # hailuo carries an explicit flat 0.28; drop both pricing fields and the
    # meter must refuse the job rather than charge $0 or a guessed default.
    monkeypatch.delitem(video_router.PROVIDERS["hailuo"], "cost_per_gen_usd", raising=False)
    response = sync_client.post(
        "/api/video/generate",
        data={**_UI_GENERATE_FORM, "provider": "hailuo"},
    )
    assert response.status_code == 500
    assert response.json()["detail"]["error"] == "generation_pricing_unavailable"
