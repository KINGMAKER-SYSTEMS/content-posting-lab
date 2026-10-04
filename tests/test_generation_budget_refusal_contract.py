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
    errorDetail "daily_budget reset=<canonical UTC>" in the existing closed
    GET /api/control-plane/v1/jobs/{id} JSON. Partial paid outputs stay.
"""

import pytest

import routers.control_plane as cp
from services import generation_budget
from tests.test_control_plane_dossier_execution import HEADERS, PAGE_ID, TOKEN, job_body, lab  # noqa: F401
from tests.test_generation_moderation_isolation import (
    install_provider, no_retry_budget, queue_silhouettes,
)

FLUX_COST = 0.045


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
    import re
    assert re.fullmatch(r"daily_budget reset=\d{4}-\d{2}-\d{2}T00:00:00Z", stored["errorDetail"])
    # Partial paid output is preserved, not discarded.
    assert [clip["generationIndex"] for clip in stored["clips"]] == [0]
    assert stored["providerCallsCompleted"] == 1

    # These fields fit the current Worker status schema. Scheduling and paid
    # output admission need independent consumer verification.
    status = status_of(client, job_id)
    assert status["status"] == "failed"
    assert status["error"] == "generation_daily_budget_reached"
    assert status["errorClass"] == "generation_budget"
    assert status["errorDetail"] == stored["errorDetail"]
    assert len(status["errorDetail"]) <= 64
    assert set(status) <= {"schema", "jobId", "status", "progress", "error", "errorClass", "errorDetail"}


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_kind", ["pa", "http500"])
async def test_provider_retry_budget_refusal_keeps_named_job_contract(lab, monkeypatch, retry_kind):
    import httpx
    import re
    import sqlite3
    from providers import base, replicate

    client, _, _ = lab
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, str(FLUX_COST))
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 1)
    posts = []
    def handler(request):
        if request.method == "POST":
            posts.append(request.url.path)
            assert len(posts) == 1, "the exhausted retry must never send another paid POST"
            if retry_kind == "http500":
                return httpx.Response(500, text="temporary provider error")
            return httpx.Response(201, json={"id": "already-paid-id"})
        return httpx.Response(200, json={"status": "failed", "error": "Prediction interrupted (code: PA)"})
    original_client = httpx.AsyncClient
    monkeypatch.setattr(base.httpx, "AsyncClient", lambda *a, **kw: original_client(transport=httpx.MockTransport(handler)))
    async def no_wait(_):
        pass
    monkeypatch.setattr(replicate, "_sleep", no_wait)
    monkeypatch.setattr(cp, "generate_one", base.generate_one)
    await cp._run_dossier_generation(job_id)
    stored = cp._load_jobs()["jobs"][job_id]
    assert stored["status"] == "failed"
    assert stored["error"] == "generation_daily_budget_reached"
    assert stored["errorClass"] == "generation_budget"
    assert re.fullmatch(r"daily_budget reset=\d{4}-\d{2}-\d{2}T00:00:00Z", stored["errorDetail"])
    assert len(posts) == 1
    assert stored["providerCallsCompleted"] == 0 and stored["clips"] == []
    assert generation_budget.spent_usd_at(generation_budget.jobs_store_path()) == pytest.approx(FLUX_COST)
    with sqlite3.connect(generation_budget.ledger_path(generation_budget.jobs_store_path())) as db:
        assert db.execute("SELECT COUNT(*) FROM debits").fetchone()[0] == 1
    if retry_kind == "pa":
        assert stored["providerFailure"]["providerRequestId"] == "already-paid-id"
    status = status_of(client, job_id)
    assert status["errorClass"] == "generation_budget"
    assert status["errorDetail"] == stored["errorDetail"]
    assert "resetsAt" not in status


@pytest.mark.parametrize("lane", ["operator", "control_plane"])
def test_admission_clock_drives_decision_reset_and_retry_after(lab, sync_client, monkeypatch, lane):
    from datetime import datetime, timezone
    from routers import video

    yesterday = datetime(2026, 10, 2, 23, 59, 50, tzinfo=timezone.utc)
    today = datetime(2026, 10, 3, 0, 0, 10, tzinfo=timezone.utc)
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "1")
    monkeypatch.setitem(video.API_KEYS, "xai", "fixture-token")
    path = cp._jobs_path() if lane == "control_plane" else generation_budget.jobs_store_path()
    assert generation_budget.debit_generation_spend_at(path, 1, "already-paid-yesterday", now=yesterday)
    before = dict(cp._load_jobs()["jobs"])
    clock = {"now": yesterday}
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]
    monkeypatch.setattr(cp, "datetime", Clock)
    monkeypatch.setattr(video, "datetime", Clock)
    monkeypatch.setattr(generation_budget, "datetime", Clock)
    can_reserve = generation_budget.can_reserve_at
    decision_samples = []
    def crossing_midnight(path, amount, now=None):
        decision_samples.append(now)
        affordable = can_reserve(path, amount, now=now)
        clock["now"] = today
        return affordable
    monkeypatch.setattr(generation_budget, "can_reserve_at", crossing_midnight)
    if lane == "control_plane":
        client, _, started = lab
        response = client.post("/api/control-plane/v1/jobs", json=job_body(), headers=HEADERS)
        assert started == [] and cp._load_jobs()["jobs"] == before
    else:
        response = sync_client.post("/api/video/generate", data={
            "prompt": "fixture scene", "provider": "grok", "count": "1",
            "duration": "5", "resolution": "720p", "project": "admission-clock",
        })
    assert response.status_code == 429
    assert response.json()["detail"]["error"] == "generation_daily_budget_reached"
    assert response.json()["detail"]["resets_at"] == "2026-10-03T00:00:00+00:00"
    assert response.headers["Retry-After"] == "10"
    assert decision_samples == [yesterday]
    assert generation_budget.spent_usd_at(path, now=yesterday) == 1
    assert generation_budget.spent_usd_at(path, now=today) == 0
