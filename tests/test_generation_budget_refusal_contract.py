"""ONE refusal contract with the Worker (fix/lab-budget-resets-at).

Two halves, same names:

(1) ADMISSION: POST /api/control-plane/v1/jobs returns HTTP 429 with body
    {"detail":{"error":"generation_daily_budget_reached","resets_at":"<ISO UTC>"}}
    and a Retry-After header (ONE clock sample for both) when the day's metered
    spend already leaves no room for the job's FIRST billable submission. No job
    row, no ghost state.

(2) MID-JOB: when a later billable submission (retry / PA resubmission / second
    call) is refused by the meter, the job ends failed with error
    "generation_daily_budget_reached", errorClass "generation_budget",
    errorDetail "daily_budget", and "resetsAt" (camelCase) in the
    GET /api/control-plane/v1/jobs/{id} JSON. Partial outputs stay.
"""

import pytest

import routers.control_plane as cp
from services import generation_budget
from tests.test_control_plane_dossier_execution import HEADERS, PAGE_ID, TOKEN, job_body, lab  # noqa: F401
from tests.test_generation_moderation_isolation import (
    install_provider, no_retry_budget, queue_silhouettes,
)

FLUX_COST = 0.03


def status_of(client, job_id):
    return client.get(f"/api/control-plane/v1/jobs/{job_id}",
                      headers={"Authorization": f"Bearer {TOKEN}", "X-RT-Page-Id": PAGE_ID}).json()


# ── (1) admission ──────────────────────────────────────────────────────────

def test_create_refused_at_admission_when_budget_has_no_room_for_first_call(lab, monkeypatch):
    """An explicit zero budget (the named emergency stop) refuses the create at
    admission with 429 + body + Retry-After, and stores NO job row."""
    client, _, _ = lab
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "0")
    before = dict(cp._load_jobs()["jobs"])

    response = client.post("/api/control-plane/v1/jobs", json=job_body(), headers=HEADERS)

    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["error"] == "generation_daily_budget_reached"
    assert isinstance(detail["resets_at"], str) and detail["resets_at"].endswith("+00:00")
    retry_after = response.headers.get("Retry-After")
    assert retry_after is not None and retry_after.isdigit() and int(retry_after) >= 1
    # No job row, no ghost state.
    assert cp._load_jobs()["jobs"] == before


def test_create_refused_at_admission_uses_one_clock_sample(lab, monkeypatch):
    """The body `resets_at` and the `Retry-After` header are computed from the
    SAME captured instant, so midnight cannot split them."""
    client, _, _ = lab
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "0")

    seen = {}

    def fake_next_reset_iso(now=None):
        seen["reset_now"] = now
        return "2026-10-02T00:00:00+00:00"

    def fake_retry_after_seconds(now=None):
        seen["retry_now"] = now
        return 42

    monkeypatch.setattr(generation_budget, "next_reset_iso", fake_next_reset_iso)
    monkeypatch.setattr(generation_budget, "retry_after_seconds", fake_retry_after_seconds)

    response = client.post("/api/control-plane/v1/jobs", json=job_body(), headers=HEADERS)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "42"
    assert seen["reset_now"] is not None
    assert seen["reset_now"] is seen["retry_now"]


# ── (2) mid-job ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mid_job_budget_refusal_exposes_resets_at_and_keeps_partial_output(lab, monkeypatch):
    """The first call fits under the meter; the second is refused. The job ends
    failed with the full refusal contract and keeps the first call's clip."""
    client, _, _ = lab
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, str(FLUX_COST))
    no_retry_budget(monkeypatch)
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 2)

    calls = []
    install_provider(monkeypatch, ["done", "done"], calls)
    await cp._run_dossier_generation(job_id)

    stored = cp._load_jobs()["jobs"][job_id]
    assert [call["id"] for call in calls] == [f"{job_id}-g00"], "second call was refused before submission"
    assert stored["status"] == "failed"
    assert stored["error"] == "generation_daily_budget_reached"
    assert stored["errorClass"] == "generation_budget"
    assert stored["errorDetail"] == "daily_budget"
    assert isinstance(stored.get("resetsAt"), str) and stored["resetsAt"].endswith("+00:00")
    # Partial paid output is preserved, not discarded.
    assert [clip["generationIndex"] for clip in stored["clips"]] == [0]
    assert stored["providerCallsCompleted"] == 1

    # The Worker reads these fields from GET /v1/jobs/{id}.
    status = status_of(client, job_id)
    assert status["status"] == "failed"
    assert status["error"] == "generation_daily_budget_reached"
    assert status["errorClass"] == "generation_budget"
    assert status["errorDetail"] == "daily_budget"
    assert status["resetsAt"] == stored["resetsAt"]
