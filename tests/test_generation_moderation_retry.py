"""A confirmed moderation refusal gets at most two deterministic varied retries.

Mocked provider, dummy keys only. Credit, auth and every other class still
fail fast with no retry; a per-page UTC-day retry budget bounds spend.
"""

import asyncio
import copy
from pathlib import Path

import pytest

from providers import base
import routers.control_plane as cp
from services import moderation_retry
from tests.master_pages_fixtures import master_pages
from tests.test_control_plane_dossier_execution import HEADERS, PAGE_ID, TOKEN, job_body, lab  # noqa: F401 (fixture)
from tests.test_generation_moderation_isolation import (
    CREDIT, MODERATION, install_provider, queue_silhouettes,
)
from tests.test_generation_restart_recovery import install_journal_provider

AUTH = 'Replicate start failed: {"title":"Unauthenticated","detail":"Invalid token.","status":401}'
MODERATION_AUTH = "Replicate failed: Warning: Moderation check failed: Error code: 401 - account deactivated"
FLUX_COST = 0.03


def status_of(client, job_id):
    return client.get(f"/api/control-plane/v1/jobs/{job_id}",
                      headers={"Authorization": f"Bearer {TOKEN}", "X-RT-Page-Id": PAGE_ID}).json()


def calls_for(calls, job_id, index):
    return [call for call in calls if call["id"].startswith(f"{job_id}-g{index:02d}")]


@pytest.mark.asyncio
async def test_e005_then_one_varied_retry_succeeds_and_output_counts(lab, monkeypatch):
    client, _, _ = lab
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 1)
    original = cp._load_jobs()["jobs"][job_id]
    calls = []
    install_provider(monkeypatch, [MODERATION, "done", "done"], calls)
    await cp._run_dossier_generation(job_id)
    stored = cp._load_jobs()["jobs"][job_id]
    assert len(calls) == 2, "exactly one varied retry"
    assert [call["id"] for call in calls] == [f"{job_id}-g00", f"{job_id}-g00-r1"]
    base_prompt = calls[0]["prompt"]
    assert cp.prompt_sha256(base_prompt) == original["promptPlan"][0]["promptHash"]
    assert calls[1]["prompt"] == moderation_retry.variant_prompt(base_prompt, 1)[0] != base_prompt
    assert calls[1]["extra"] == calls[0]["extra"], "same engine, model and safety settings"
    assert stored["status"] == "completed" and "error" not in stored
    assert stored["providerCallsCompleted"] == 1 and stored["completedGenerationCalls"] == [0]
    assert stored.get("providerFailures", []) == []
    assert stored["promptPlan"] == original["promptPlan"], "the immutable plan is never rewritten"
    [clip] = stored["clips"]
    assert clip["generationIndex"] == 0
    assert clip["promptHash"] == cp.prompt_sha256(calls[1]["prompt"])
    assert clip["basePromptHash"] == original["promptPlan"][0]["promptHash"]
    assert clip["promptVariant"] == "clothed-family-safe.v1"
    assert (Path(stored["artifactRoot"]) / clip["path"]).is_file()
    assert clip["sourceTreatment"]["sourceSha256"] == clip["sha256"]
    rows = stored["generationAttempts"]["0"]
    assert [(r["attempt"], r["outcome"], r["class"], r["errorDetail"], r["costUsd"]) for r in rows] == [
        (0, "refused", "moderation", "E005", FLUX_COST),
        (1, "succeeded", None, None, FLUX_COST),
    ]
    assert rows[0]["providerRequestId"] == "prediction-0"
    assert stored["moderationRetryCostUsd"] == FLUX_COST
    assert set(status_of(client, job_id)) == {"schema", "jobId", "status", "progress"}
    artifacts = client.get(f"/api/control-plane/v1/jobs/{job_id}/artifacts", headers={
        "Authorization": f"Bearer {TOKEN}", "X-RT-Page-Id": PAGE_ID}).json()
    assert [a["sha256"] for a in artifacts["artifacts"]] == [clip["sha256"]]


@pytest.mark.asyncio
async def test_e005_three_times_is_terminal_named_moderation_with_no_fourth_call(lab, monkeypatch):
    client, _, _ = lab
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 1)
    calls = []
    install_provider(monkeypatch, [MODERATION, MODERATION, MODERATION, "done"], calls)
    await cp._run_dossier_generation(job_id)
    stored = cp._load_jobs()["jobs"][job_id]
    assert len(calls) == 3, "N=2 retries: exactly three paid attempts for the clip, never a fourth"
    assert [call["id"] for call in calls] == [f"{job_id}-g00", f"{job_id}-g00-r1", f"{job_id}-g00-r2"]
    assert len({call["prompt"] for call in calls}) == 3
    assert stored["status"] == "failed" and stored["error"] == "provider_generation_failed"
    assert (stored["errorClass"], stored["errorDetail"]) == ("moderation", "E005")
    [failure] = stored["providerFailures"]
    assert failure["terminal"] == "moderation_retries_exhausted" and failure["attempts"] == 3
    assert failure["class"] == "moderation" and failure["providerRequestId"] == "prediction-2"
    rows = stored["generationAttempts"]["0"]
    assert [r["outcome"] for r in rows] == ["refused"] * 3
    assert [r["promptVariant"] for r in rows] == [None, "clothed-family-safe.v1", "backlit-silhouette-shapes.v1"]
    assert all(r["costUsd"] == FLUX_COST for r in rows), "a refused prediction is billed"
    assert stored["moderationRetryCostUsd"] == 2 * FLUX_COST
    status = status_of(client, job_id)
    assert set(status) == {"schema", "jobId", "status", "progress", "error", "errorClass", "errorDetail"}
    assert (status["error"], status["errorClass"], status["errorDetail"]) == (
        "provider_generation_failed", "moderation", "E005")
    before = len(calls)
    await cp._run_dossier_generation(job_id)
    assert len(calls) == before, "a terminal job never spends again"


@pytest.mark.asyncio
async def test_402_is_never_retried_and_fails_fast(lab, monkeypatch):
    client, _, _ = lab
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 2)
    calls = []
    install_provider(monkeypatch, [CREDIT, "done", "done"], calls)
    await cp._run_dossier_generation(job_id)
    stored = cp._load_jobs()["jobs"][job_id]
    assert len(calls) == 1, "no retry and no later planned spend"
    assert stored["status"] == "failed" and stored["error"] == "provider_generation_failed"
    assert (stored["errorClass"], stored["errorDetail"]) == ("insufficient_credit", "HTTP 402")
    assert "terminal" not in stored["providerFailures"][0]
    assert [r["outcome"] for r in stored["generationAttempts"]["0"]] == ["failed"]
    assert stored["moderationRetryCostUsd"] == 0
    status = status_of(client, job_id)
    assert (status["errorClass"], status["errorDetail"]) == ("insufficient_credit", "HTTP 402")


def test_provider_auth_is_a_named_class():
    assert base.classify_provider_error(AUTH) == "provider_auth"
    assert base.classify_provider_error('Replicate start failed: {"status": 403}') == "provider_auth"
    assert base.classify_provider_error(CREDIT) == "insufficient_credit"
    assert base.classify_provider_error('{"status":429}') == "rate_limited"
    assert base.classify_provider_error(MODERATION_AUTH) == "moderation"


@pytest.mark.asyncio
async def test_401_is_provider_auth_with_exactly_one_call(lab, monkeypatch):
    client, _, _ = lab
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 2)
    calls = []
    install_provider(monkeypatch, [AUTH, "done", "done"], calls)
    await cp._run_dossier_generation(job_id)
    stored = cp._load_jobs()["jobs"][job_id]
    assert len(calls) == 1
    assert stored["status"] == "failed"
    assert (stored["errorClass"], stored["errorDetail"]) == ("provider_auth", "HTTP 401")
    status = status_of(client, job_id)
    assert (status["errorClass"], status["errorDetail"]) == ("provider_auth", "HTTP 401")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    'Replicate start failed: {"status":429,"detail":"throttled"}',
    "ReadTimeout(submission response lost)",
    "unknown provider fault",
])
async def test_other_classes_are_never_retried(lab, monkeypatch, failure):
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 2)
    calls = []
    install_provider(monkeypatch, [failure, "done", "done"], calls)
    await cp._run_dossier_generation(job_id)
    stored = cp._load_jobs()["jobs"][job_id]
    assert len(calls) == 1
    assert stored["status"] == "failed"
    assert stored["errorClass"] == base.classify_provider_error(failure) != "moderation"


@pytest.mark.asyncio
async def test_auth_failure_inside_moderation_check_is_not_retried(lab, monkeypatch):
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 2)
    calls = []
    install_provider(monkeypatch, [MODERATION_AUTH, "done", "done"], calls)
    await cp._run_dossier_generation(job_id)
    stored = cp._load_jobs()["jobs"][job_id]
    assert [call["id"] for call in calls] == [f"{job_id}-g00", f"{job_id}-g01"], \
        "no retry; the refusal still only skips to the next planned candidate"
    assert stored["providerFailures"][0]["terminal"] == "moderation_provider_auth_not_retried"
    assert stored["status"] == "completed" and len(stored["clips"]) == 1


@pytest.mark.asyncio
async def test_daily_retry_budget_exhausted_means_no_retry_but_first_attempts_still_run(lab, monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_MODERATION_RETRY_DAILY_BUDGET", "1")
    client, _, _ = lab
    first_job, _, _ = queue_silhouettes(lab, monkeypatch, 1)
    calls = []
    install_provider(monkeypatch, [MODERATION, "done", MODERATION, "done", "done"], calls)
    await cp._run_dossier_generation(first_job)
    assert len(calls) == 2 and cp._load_jobs()["jobs"][first_job]["status"] == "completed"

    # A later job for the same page on the same UTC day: the budget is durable
    # across jobs (it is counted from every job's recorded retry rows).
    intent, revision = master_pages(PAGE_ID, handle="tucker.reeves", content_niche="silhouette")
    second = client.post("/api/control-plane/v1/jobs", json=job_body(
        quantity=2, lockedRecipeId="silhouette-truck:master", masterPages=intent,
        masterPagesHash=revision), headers={**HEADERS, "Idempotency-Key": "second-budget-job"})
    assert second.status_code == 200
    second_job = second.json()["jobId"]
    assert second_job != first_job
    await cp._run_dossier_generation(second_job)
    stored = cp._load_jobs()["jobs"][second_job]
    assert [call["id"] for call in calls[2:]] == [f"{second_job}-g00", f"{second_job}-g01"], \
        "first attempts are never refused by the budget; only the retry is withheld"
    [failure] = stored["providerFailures"]
    assert failure["terminal"] == "moderation_retry_budget_exhausted" and failure["attempts"] == 1
    assert stored["status"] == "completed" and len(stored["clips"]) == 1
    assert [r["outcome"] for r in stored["generationAttempts"]["0"]] == ["refused"]
    assert stored["moderationRetryCostUsd"] == 0


def test_budget_setting_parses_spend_safe(monkeypatch):
    monkeypatch.delenv("CONTENT_LAB_MODERATION_RETRY_DAILY_BUDGET", raising=False)
    assert moderation_retry.daily_budget() == 6
    for raw, expected in [("0", 0), ("3", 3), (" 9 ", 9), ("-1", 0), ("many", 0)]:
        monkeypatch.setenv("CONTENT_LAB_MODERATION_RETRY_DAILY_BUDGET", raw)
        assert moderation_retry.daily_budget() == expected


@pytest.mark.asyncio
async def test_variant_prompt_is_deterministic_and_recorded_on_the_job(lab, monkeypatch):
    job_id, _, recipe = queue_silhouettes(lab, monkeypatch, 1)
    plan = cp._load_jobs()["jobs"][job_id]["promptPlan"][0]
    base_prompt, _ = cp.compose_prompt_combination(recipe, plan["combinationId"])
    first = [moderation_retry.variant_prompt(base_prompt, k) for k in range(3)]
    again = [moderation_retry.variant_prompt(copy.copy(base_prompt), k) for k in range(3)]
    assert first == again
    assert first[0] == (base_prompt, None)
    assert len({prompt for prompt, _ in first}) == 3
    assert "featureless" in base_prompt and "featureless" not in first[2][0]
    with pytest.raises(ValueError):
        moderation_retry.variant_prompt(base_prompt, 3)
    calls = []
    install_provider(monkeypatch, [MODERATION, MODERATION, "done"], calls)
    await cp._run_dossier_generation(job_id)
    stored = cp._load_jobs()["jobs"][job_id]
    assert [call["prompt"] for call in calls] == [prompt for prompt, _ in first]
    rows = stored["generationAttempts"]["0"]
    assert [(r["attempt"], r["promptHash"], r["promptVariant"]) for r in rows] == [
        (k, cp.prompt_sha256(prompt), variant) for k, (prompt, variant) in enumerate(first)
    ]
    assert all(isinstance(r["at"], str) and r["at"].endswith("+00:00") for r in rows)
    assert stored["clips"][0]["promptHash"] == rows[2]["promptHash"]
    assert stored["clips"][0]["promptVariant"] == "backlit-silhouette-shapes.v1"


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", ["p2", "p3"])
async def test_restart_after_retry_resumes_its_own_prediction_and_verifies_variant_clip(lab, monkeypatch, interrupt):
    # p1 is refused (E005); p2 is the varied retry of call 0; p3 is call 1.
    # "p2": restart while the paid retry is still polling. "p3": restart after
    # the retried clip was claimed; recovery must accept its variant hash.
    job_id, body, _ = queue_silhouettes(lab, monkeypatch, 2)
    calls = install_journal_provider(monkeypatch, interrupt_poll=interrupt, refuse="p1")
    with pytest.raises(asyncio.CancelledError):
        await cp._run_dossier_generation(job_id)
    paused = cp._get_job_or_404(job_id)
    assert paused["status"] == "queued" and paused["resumePending"] is True
    assert paused["providerCheckpoints"]["0"]["predictionId"] == "p1"
    assert paused["providerCheckpoints"]["0:1"]["predictionId"] == "p2"
    preserved = copy.deepcopy(paused["clips"])
    if interrupt == "p3":
        assert [clip["promptVariant"] for clip in preserved] == ["clothed-family-safe.v1"]
        assert preserved[0]["promptHash"] != preserved[0]["basePromptHash"]
    monkeypatch.setattr(cp, "_GENERATION_RUNTIME_ID", "replacement-runtime")
    await cp._run_dossier_generation(job_id)
    completed = cp._get_job_or_404(job_id)
    assert completed["status"] == "completed", completed.get("error")
    assert completed["completedGenerationCalls"] == [0, 1]
    assert completed["clips"][:len(preserved)] == preserved
    cp._verify_preserved_generation(Path(completed["artifactRoot"]), completed["clips"])
    assert [c for c in calls if c[0] == "POST"] == [("POST", "/v1/models/black-forest-labs/flux-2-pro/predictions")] * 3, \
        "p1, its single retry p2, and call 1's p3; nothing paid is resubmitted"
    assert calls.count(("GET", "/v1/predictions/p1")) == 1
    assert calls.count(("GET", "/v1/predictions/p2")) == (2 if interrupt == "p2" else 1)
    assert [r["outcome"] for r in completed["generationAttempts"]["0"]] == ["refused", "succeeded"]
    assert completed.get("providerFailures", []) == []


@pytest.mark.asyncio
async def test_tampered_variant_hash_is_rejected_on_restart(lab, monkeypatch):
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 2)
    calls = install_journal_provider(monkeypatch, interrupt_poll="p3", refuse="p1")
    with pytest.raises(asyncio.CancelledError):
        await cp._run_dossier_generation(job_id)
    job = cp._get_job_or_404(job_id)
    job["clips"][0]["promptHash"] = job["clips"][0]["basePromptHash"]
    cp._update_job(job_id, clips=job["clips"])
    before = list(calls)
    await cp._run_dossier_generation(job_id)
    assert cp._get_job_or_404(job_id)["error"] == "generation_checkpoint_provenance_mismatch"
    assert calls == before
