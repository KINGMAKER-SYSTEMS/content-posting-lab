"""Daily TOTAL generation budget: meter, refusal at the cap, UTC rollover,
and proof that non-generation work (burns, renders, source, slideshow) is
never gated.

These tests fail on 0d9dd68 (no budget code exists there) — the red-proof for
Lane F2: a per-request ceiling never becomes a daily total, so money leaks
across 14 unattended days.
"""

import pathlib
from datetime import datetime, timedelta, timezone

import pytest

import routers.control_plane as cp
from services import generation_budget
from tests.test_control_plane_dossier_execution import (
    HEADERS, PAGE_ID, TOKEN, job_body, lab,  # noqa: F401 (fixture)
)


# ── meter (pure) ─────────────────────────────────────────────────────────

def test_daily_budget_parses_spend_safe(monkeypatch):
    monkeypatch.delenv(generation_budget.USD_BUDGET_ENV, raising=False)
    assert generation_budget.daily_budget_usd() == generation_budget.DEFAULT_DAILY_BUDGET_USD
    for raw, expected in [("0", 0.0), ("12.5", 12.5), (" 3 ", 3.0), ("-1", 0.0), ("many", 0.0)]:
        monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, raw)
        assert generation_budget.daily_budget_usd() == expected


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


# ── admission gate (integration) ─────────────────────────────────────────

def test_create_job_refuses_generation_at_the_daily_budget(lab, monkeypatch):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "0")
    client, _, _ = lab
    response = client.post("/api/control-plane/v1/jobs", json=job_body(), headers=HEADERS)
    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["error"] == "generation_daily_budget_reached"
    assert isinstance(detail["resets_at"], str) and detail["resets_at"].endswith("+00:00")


def test_create_job_reserves_and_admits_generation_under_budget(lab, monkeypatch):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "25.0")
    client, _, _ = lab
    response = client.post("/api/control-plane/v1/jobs", json=job_body(), headers=HEADERS)
    assert response.status_code == 200
    store = cp._load_jobs()
    # truck-scenic: quantity 2 -> 1 provider call (clips_per_gen 5) at $0.28.
    assert generation_budget.spent_usd(store) == pytest.approx(0.28)


def test_create_job_budget_is_cumulative_across_jobs(lab, monkeypatch):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "0.5")
    client, _, _ = lab
    first = client.post("/api/control-plane/v1/jobs", json=job_body(), headers=HEADERS)
    assert first.status_code == 200
    second = client.post(
        "/api/control-plane/v1/jobs", json=job_body(),
        headers={**HEADERS, "Idempotency-Key": "tt-tucker-reeves:policy:second-budget"},
    )
    # 0.28 + 0.28 = 0.56 > 0.5, so the second job is refused.
    assert second.status_code == 429
    assert second.json()["detail"]["error"] == "generation_daily_budget_reached"


def test_budget_429_carries_retry_after_consistent_with_resets_at(lab, monkeypatch):
    """F1: the budget 429 sends a standard Retry-After header (seconds until
    resets_at) alongside the unchanged JSON body, so a Retry-After-honouring
    client backs off instead of spinning."""
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "0")
    client, _, _ = lab
    before = datetime.now(timezone.utc)
    response = client.post("/api/control-plane/v1/jobs", json=job_body(), headers=HEADERS)
    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["error"] == "generation_daily_budget_reached"
    resets_at = detail["resets_at"]
    retry_after = response.headers.get("Retry-After")
    assert retry_after is not None and retry_after.isdigit() and int(retry_after) >= 1
    reset_dt = datetime.fromisoformat(resets_at)
    seconds_until_reset = (reset_dt - before).total_seconds()
    assert abs(int(retry_after) - seconds_until_reset) <= 2
    # Retry-After is an int (whole seconds), never 0.
    assert int(retry_after) >= 1


# ── non-generation work is never gated ───────────────────────────────────

def test_generation_budget_gate_lives_only_in_the_control_plane_lane():
    """Burns, renders, source recut, slideshows and posting never call the meter.

    The gate is reachable only from routers/control_plane.py; every other
    router must not even import it, so a burn/render/posting request can never
    be blocked by the daily generation budget.
    """
    repo = pathlib.Path(cp.__file__).resolve().parents[1]
    importers = []
    callers = []
    for path in (repo / "routers").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "generation_budget" in text:
            importers.append(path.name)
        if "reserve_generation_spend" in text:
            callers.append(path.name)
    assert callers == ["control_plane.py"], f"unexpected budget gate callers: {callers}"
    # control_plane is the only router allowed to import the meter.
    assert importers == ["control_plane.py"], f"unexpected budget importers: {importers}"


def test_health_reports_the_generation_budget(sync_client, monkeypatch, tmp_path):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "7.0")
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    response = sync_client.get("/api/health")
    assert response.status_code == 200
    budget = response.json()["generation_budget"]
    assert budget["budgetUsd"] == 7.0
    assert budget["spentUsd"] == 0.0
    assert "remainingUsd" in budget and "resetsAt" in budget and "day" in budget
