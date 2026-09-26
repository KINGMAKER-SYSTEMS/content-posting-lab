"""A confirmed moderation refusal gets at most two deterministic varied retries.

Mocked provider, dummy keys only. Credit, auth and every other class still
fail fast with no retry; a per-page UTC-day retry budget bounds spend.
"""

import asyncio
import copy
import json
from pathlib import Path
import re

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
# No HTTP code, only the "deactivated" wording: still moderation-class, never retried.
MODERATION_DEACTIVATED = "Replicate failed: Warning: Moderation check failed: moderation account deactivated"
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
    assert clip["promptVariant"] == "family-safe.v2"
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
    assert [r["promptVariant"] for r in rows] == [None, "family-safe.v2", "backlit-shapes.v2"]
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
    assert base.classify_provider_error(MODERATION_AUTH) == "provider_auth"
    assert base.classify_provider_error(MODERATION_DEACTIVATED) == "moderation"


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
    install_provider(monkeypatch, [MODERATION_DEACTIVATED, "done", "done"], calls)
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
    assert stored["clips"][0]["promptVariant"] == "backlit-shapes.v2"


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
        assert [clip["promptVariant"] for clip in preserved] == ["family-safe.v2"]
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


# ---------------------------------------------------------------------------
# Review round 2 (#181): the retry trigger is exactly E005, the observed auth
# failure inside the moderation check is provider_auth, and the three spend
# guards (durable reservation, in-flight counting, confirmed refusal) are pinned.
# ---------------------------------------------------------------------------


# The one auth shape seen in production (2026-09-25, Hailuo job): an OpenAI-style
# 401 raised by the provider's own moderation dependency. Dummy text only.
OBSERVED_MODERATION_AUTH = (
    "Replicate failed: Warning: Moderation check failed: Error code: 401 - "
    "{'error': {'message': 'Your OpenAI account has been deactivated', "
    "'code': 'account_deactivated'}}"
)
# The Worker's pinned validator (control-plane-worker contentLabClient.js:1769/1771,
# identical on Worker main 7774c0334).
WORKER_CLASS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
WORKER_DETAIL = re.compile(r"^[A-Za-z0-9 _.:,;/()#=+-]{1,64}$")


def install_keyed_provider(monkeypatch, outcomes, calls, before=None):
    """Mock generate_one keyed by provider job id.

    An outcome is "done" or (error, provider_request_id); ``before`` maps a
    provider job id to an async hook awaited when that call starts.
    """
    install_provider(monkeypatch, [], [])  # treatment + thumbnail doubles

    async def generate(provider_job_id, index, provider, prompt, aspect_ratio, resolution,
                       duration, image_data_uri, jobs, output_dir, url_prefix, **extra):
        calls.append({"id": provider_job_id, "prompt": prompt})
        if before and provider_job_id in before:
            await before[provider_job_id]()
        entry = jobs[provider_job_id]["videos"][index]
        outcome = outcomes[provider_job_id]
        if outcome == "done":
            video = output_dir / f"{provider_job_id}.mp4"
            video.write_bytes(f"approved-video-{provider_job_id}".encode())
            still = output_dir / f"{provider_job_id}.jpg"
            still.write_bytes(f"approved-still-{provider_job_id}".encode())
            entry.update(status="done", file=video.name, provider_image_file=still.name,
                         provider_request_id=f"prediction-{provider_job_id}")
        else:
            error, request_id = outcome
            entry.update(status="error", error=error)
            if request_id is not None:
                entry["provider_request_id"] = request_id

    monkeypatch.setattr(cp, "generate_one", generate)


def queue_second_job(lab, key):
    client, _, _ = lab
    intent, revision = master_pages(PAGE_ID, handle="tucker.reeves", content_niche="silhouette")
    response = client.post("/api/control-plane/v1/jobs", json=job_body(
        quantity=1, lockedRecipeId="silhouette-truck:master", masterPages=intent,
        masterPagesHash=revision), headers={**HEADERS, "Idempotency-Key": key})
    assert response.status_code == 200
    return response.json()["jobId"]


# Fix 1: retry ONLY on the exact E005 refusal ---------------------------------

NOT_E005_MODERATION_CLASS = {
    "429-in-moderation": "Replicate failed: Warning: Moderation check failed: Error code: 429 - "
                         "You exceeded your current quota (insufficient_quota)",
    "5xx-in-safety": "Replicate failed: Warning: Safety check failed: Error code: 503 - "
                     "service unavailable",
    "402-in-moderation": "Replicate failed: Warning: Moderation check failed: Error code: 402 - "
                         "payment required",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(NOT_E005_MODERATION_CLASS))
async def test_moderation_class_text_without_e005_is_never_retried(lab, monkeypatch, shape):
    failure = NOT_E005_MODERATION_CLASS[shape]
    assert base.classify_provider_error(failure) == "moderation", "the class alone is too wide"
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 2)
    calls = []
    install_provider(monkeypatch, [failure, "done", "done"], calls)
    await cp._run_dossier_generation(job_id)
    stored = cp._load_jobs()["jobs"][job_id]
    assert [call["id"] for call in calls] == [f"{job_id}-g00", f"{job_id}-g01"], \
        "no paid retry for a moderation-class failure that is not a confirmed E005 refusal"
    assert [r["attempt"] for r in stored["generationAttempts"]["0"]] == [0]
    assert stored["providerFailures"][0]["terminal"] == "moderation_not_e005_not_retried"
    assert stored["moderationRetryCostUsd"] == 0


@pytest.mark.asyncio
async def test_failed_prediction_whose_logs_mention_safety_is_not_retried(lab, monkeypatch):
    # An empty prediction ``error`` falls back to its logs (providers/replicate.py
    # _poll_prediction); logs echoing ``disable_safety_filter`` match "safety".
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 2)
    logs = "Using seed 1234\ndisable_safety_filter=True\nCUDA out of memory"
    calls = install_journal_provider(monkeypatch, refuse="p1", refusal={"error": None, "logs": logs})
    await cp._run_dossier_generation(job_id)
    stored = cp._load_jobs()["jobs"][job_id]
    [row] = stored["generationAttempts"]["0"]
    assert (row["class"], row["providerRequestId"]) == ("moderation", "p1")
    assert row["detail"].startswith("Replicate failed: Using seed")
    assert [c for c in calls if c[0] == "POST"] == [
        ("POST", "/v1/models/black-forest-labs/flux-2-pro/predictions")] * 2, \
        "p1 and call 1's p2 only; the OOM is never re-bought as a moderation retry"
    assert stored["providerFailures"][0]["terminal"] == "moderation_not_e005_not_retried"


@pytest.mark.asyncio
async def test_exact_e005_is_the_retry_trigger(lab, monkeypatch):
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 2)
    calls = []
    install_provider(monkeypatch, [MODERATION, "done", "done"], calls)
    await cp._run_dossier_generation(job_id)
    assert [call["id"] for call in calls] == [f"{job_id}-g00", f"{job_id}-g00-r1", f"{job_id}-g01"]


# Fix 2: the observed auth failure inside the moderation check -----------------

@pytest.mark.asyncio
async def test_observed_auth_failure_inside_moderation_check_is_provider_auth(lab, monkeypatch):
    for message in (OBSERVED_MODERATION_AUTH,
                    "Replicate failed: Moderation check failed: ERROR CODE: 403 - forbidden"):
        assert base.classify_provider_error(message) == "provider_auth"
    assert base.provider_error_code(OBSERVED_MODERATION_AUTH) == "moderation model HTTP 401"
    client, _, _ = lab
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 2)
    calls = []
    install_provider(monkeypatch, [OBSERVED_MODERATION_AUTH, "done", "done"], calls)
    await cp._run_dossier_generation(job_id)
    stored = cp._load_jobs()["jobs"][job_id]
    assert [call["id"] for call in calls] == [f"{job_id}-g00"], "never retried, fails fast"
    assert stored["status"] == "failed" and stored["error"] == "provider_generation_failed"
    assert (stored["errorClass"], stored["errorDetail"]) == ("provider_auth", "moderation model HTTP 401")
    assert [r["outcome"] for r in stored["generationAttempts"]["0"]] == ["failed"]
    status = status_of(client, job_id)
    assert set(status) == {"schema", "jobId", "status", "progress", "error", "errorClass", "errorDetail"}
    assert (status["errorClass"], status["errorDetail"]) == ("provider_auth", "moderation model HTTP 401")
    assert WORKER_CLASS.fullmatch(status["errorClass"]) and WORKER_DETAIL.fullmatch(status["errorDetail"])


# Fix 3: the three spend guards, each killed by a named mutation --------------

@pytest.mark.asyncio
async def test_retry_reservation_is_on_disk_before_the_paid_retry_call(lab, monkeypatch):
    # Mutation (a): drop the atomic_save of the reservation in
    # _reserve_moderation_retry. A crash during the paid retry would then
    # restart against a budget that never saw the debit.
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 1)
    seen = []

    async def inspect_disk():
        seen.append(copy.deepcopy(cp._load_jobs()["jobs"][job_id].get("generationAttempts")))

    calls = []
    install_keyed_provider(monkeypatch, {
        f"{job_id}-g00": (MODERATION, "prediction-0"), f"{job_id}-g00-r1": "done",
    }, calls, before={f"{job_id}-g00-r1": inspect_disk})
    await cp._run_dossier_generation(job_id)
    assert [call["id"] for call in calls] == [f"{job_id}-g00", f"{job_id}-g00-r1"]
    [on_disk] = seen
    assert [(r["attempt"], r["outcome"]) for r in on_disk["0"]] == [(0, "refused"), (1, "pending")], \
        "the reservation is durable before any money is spent on the retry"


@pytest.mark.asyncio
async def test_in_flight_retry_of_another_job_counts_against_the_page_budget(lab, monkeypatch):
    # Mutation (b): retries_used ignores ``pending`` rows. Job A's retry is in
    # flight (reserved, not yet answered) when job B, same page, is refused.
    monkeypatch.setenv("CONTENT_LAB_MODERATION_RETRY_DAILY_BUDGET", "1")
    job_a, _, _ = queue_silhouettes(lab, monkeypatch, 1)
    job_b = queue_second_job(lab, "in-flight-budget-job")

    async def run_b_while_a_retry_is_in_flight():
        await cp._run_dossier_generation(job_b)

    calls = []
    install_keyed_provider(monkeypatch, {
        f"{job_a}-g00": (MODERATION, "prediction-a0"), f"{job_a}-g00-r1": "done",
        f"{job_b}-g00": (MODERATION, "prediction-b0"), f"{job_b}-g00-r1": "done",
    }, calls, before={f"{job_a}-g00-r1": run_b_while_a_retry_is_in_flight})
    await cp._run_dossier_generation(job_a)
    assert [call["id"] for call in calls] == [f"{job_a}-g00", f"{job_a}-g00-r1", f"{job_b}-g00"], \
        "B's retry would be a second paid retry for the page on a budget of 1"
    b = cp._load_jobs()["jobs"][job_b]
    assert b["providerFailures"][0]["terminal"] == "moderation_retry_budget_exhausted"
    assert cp._load_jobs()["jobs"][job_a]["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["no-prediction-id", "not-replicate-failed"])
async def test_retry_requires_a_prediction_id_and_a_replicate_failed_signal(lab, monkeypatch, shape):
    # Mutation (c): confirmed_refusal reduced to ``class == "moderation"``.
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 2)
    first = ((MODERATION, None) if shape == "no-prediction-id" else
             ("Replicate start failed: The input or output was flagged as sensitive. (E005)",
              "prediction-0"))
    calls = []
    install_keyed_provider(monkeypatch, {
        f"{job_id}-g00": first, f"{job_id}-g00-r1": "done", f"{job_id}-g01": "done",
    }, calls)
    await cp._run_dossier_generation(job_id)
    stored = cp._load_jobs()["jobs"][job_id]
    assert [call["id"] for call in calls] == [f"{job_id}-g00"], \
        "an unconfirmed refusal is never retried; it fails fast like any other fault"
    assert stored["status"] == "failed" and stored["error"] == "provider_generation_failed"
    assert [r["outcome"] for r in stored["generationAttempts"]["0"]] == ["failed"]


# Recommended: neutral rewordings and a cost table pinned to the catalog -------

PERSON_WORDS = {
    "people", "person", "persons", "man", "men", "woman", "women", "adult", "adults",
    "couple", "couples", "lover", "lovers", "figure", "figures", "body", "bodies",
    "human", "humans", "child", "children", "everyone", "someone", "clothed",
    "clothing", "silhouette", "silhouettes", "he", "she", "her", "his",
}
PEOPLE_FREE_PROMPTS = [
    "A clean unbadged dark pickup truck parked alone on a desert highway at golden hour.",
    "A small fishing boat moored in a calm harbor at dawn, soft mist over the water",
    "A featureless salt flat under a deep purple dusk sky with one lone oak tree.",
]


def words(text):
    return set(re.findall(r"[a-z]+", text.lower()))


@pytest.mark.parametrize("prompt", PEOPLE_FREE_PROMPTS)
def test_variants_never_introduce_people_into_a_people_free_prompt(prompt):
    variants = [moderation_retry.variant_prompt(prompt, k)[0]
                for k in range(1, moderation_retry.RETRIES_PER_CALL + 1)]
    assert len({prompt, *variants}) == 1 + len(variants), "each variant is still a distinct rewording"
    for variant in variants:
        assert variant.startswith(prompt.rstrip(". ")), "the original subject text is kept"
        added = (words(variant) - words(prompt)) & PERSON_WORDS
        assert not added, f"variant introduced subjects the prompt did not have: {sorted(added)}"


def test_variants_of_a_people_prompt_keep_the_clothed_silhouette_rewording():
    prompt = ("A quiet cinematic photograph of two adult lovers shown only as pure black "
              "featureless full-body silhouettes. no visible faces, no clothing detail.")
    first, second = (moderation_retry.variant_prompt(prompt, k)[0] for k in (1, 2))
    assert "clothed" in first
    assert "featureless" not in second and "fully clothed adult" in second


def test_attempt_cost_table_equals_the_generation_catalog():
    catalog = {}
    for path in sorted((Path(__file__).resolve().parents[1] / "recipes" / "generation").glob("*.json")):
        for provider in json.loads(path.read_text()).get("providers", {}).values():
            model, cost = provider.get("replicate_model"), provider.get("cost_per_gen_usd")
            if model is not None:
                assert catalog.setdefault(model, cost) == cost, f"{model} priced twice in the catalog"
    assert catalog, "the catalog was found"
    assert moderation_retry.ATTEMPT_COST_ESTIMATE_USD == catalog


# ---------------------------------------------------------------------------
# Review round 3 (#181 @ 29a0126): negation-aware person gate over the REAL
# catalog, the embedded-HTTP-status branch pinned and widened, and a distinct
# errorDetail for the moderation model's own 401/403.
# ---------------------------------------------------------------------------

from services.control_plane_generation import GenerationRecipe, compose_prompt_combination, prompt_combination_space

CATALOG_ROOT = Path(__file__).resolve().parents[1] / "recipes" / "generation"
# The families whose declared subject is people. Every other catalog family is
# people-free (and most of them say "no people" in their guards or motion).
PEOPLE_FAMILIES = {
    ("silhouette_stills.v1.json", "silhouette"),
    ("prompt_modules.v1.json", "silhouette"),
}
PERSON_WORDING = ("clothed", "every person", "Everyone", "adult with no body detail")


def catalog_families():
    return sorted((path.name, name) for path in CATALOG_ROOT.glob("*.json")
                  for name in json.loads(path.read_text()).get("families", {}))


def catalog_prompts(file_name, family_name):
    family = json.loads((CATALOG_ROOT / file_name).read_text())["families"][family_name]
    recipe = GenerationRecipe(
        recipe_id=f"{family_name}:catalog", format_slug=family_name, family_name=family_name,
        engine="catalog", provider_model="catalog", engine_registry_hash="r",
        format_contract_version="c", material_source="generated_video", asset_type="video/mp4",
        executor_version="e", prompt_catalog_hash="h", family=family, provider_config={},
        recipe_spec={},
    )
    return [compose_prompt_combination(recipe, k)[0] for k in range(prompt_combination_space(recipe))]


def test_the_people_family_list_covers_the_whole_catalog():
    families = catalog_families()
    assert len(families) == 7 and PEOPLE_FAMILIES <= set(families), \
        "a new catalog family must be classified here as people or people-free"


@pytest.mark.parametrize("file_name, family_name", catalog_families())
def test_every_real_catalog_prompt_gets_person_wording_only_if_its_family_depicts_people(
        file_name, family_name):
    depicts_people = (file_name, family_name) in PEOPLE_FAMILIES
    prompts = catalog_prompts(file_name, family_name)
    assert prompts
    for prompt in prompts:
        for k in range(1, moderation_retry.RETRIES_PER_CALL + 1):
            variant = moderation_retry.variant_prompt(prompt, k)[0]
            has_person_wording = any(text in variant[len(prompt) - 2:] for text in PERSON_WORDING)
            assert has_person_wording is depicts_people, (family_name, k, prompt[-160:], variant[-200:])
            if not depicts_people:
                assert variant.startswith(prompt.rstrip(". ")), "no replacement touched a people-free prompt"


@pytest.mark.parametrize("prompt", [
    "A small boat carving a figure-eight across a calm lake at dawn.",
    "A small boat tracing a wide figure eight on a calm lake at dawn.",
    "The truck's dark outline figures sharply against the dusk sky.",
    "A man-made stone jetty on a calm lake at dawn, no people.",
    "An empty harbor at dawn without any people on the docks.",
    "A quiet field at dusk, free of people and vehicles.",
    "A lone pickup under a dusk sky, devoid of people.",
    "A still lake at dawn with zero people in frame.",
    "A parked truck in a field, and not any people nearby.",
])
def test_negated_or_non_person_mentions_are_not_people(prompt):
    for k in range(1, moderation_retry.RETRIES_PER_CALL + 1):
        variant = moderation_retry.variant_prompt(prompt, k)[0]
        assert not any(text in variant for text in PERSON_WORDING), variant


def test_a_person_mentioned_outside_the_negation_still_counts():
    prompt = "No cars on the road; two adults walk along the shoulder at dusk."
    assert "clothed" in moderation_retry.variant_prompt(prompt, 1)[0]


# The one real E005 shape (2026-09-25 census, all 23 refusals; zcode/flux-silhouette-failures.md:14).
CENSUS_E005 = ("Replicate failed: The input or output was flagged as sensitive. "
               "Please try again with different inputs. (E005)")

E005_WITH_HTTP_STATUS = {
    "error-code-colon": "Replicate failed: Safety check failed: Error code: 503 - unavailable (E005)",
    "python-repr-status": "Replicate failed: moderation dependency returned {'status': 500} (E005)",
    "http-token": "Replicate failed: Safety check failed: HTTP 503 Service Unavailable (E005)",
    "httpx-server-error": ("Replicate failed: Safety check failed: Server error '503 Service Unavailable' "
                           "for url 'https://moderation.invalid/v1' (E005)"),
    "error-code-no-colon": "Replicate failed: Moderation check failed: Error code 429 - quota (E005)",
    "status-code-kwarg": "Replicate failed: Moderation check failed: status_code=429 (E005)",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(E005_WITH_HTTP_STATUS))
async def test_e005_with_an_embedded_http_status_is_never_retried(lab, monkeypatch, shape):
    failure = E005_WITH_HTTP_STATUS[shape]
    assert base.classify_provider_error(failure) == "moderation"
    assert base.provider_error_code(failure) == "E005" or shape == "error-code-colon"
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 2)
    calls = []
    install_provider(monkeypatch, [failure, "done", "done"], calls)
    await cp._run_dossier_generation(job_id)
    stored = cp._load_jobs()["jobs"][job_id]
    assert [call["id"] for call in calls] == [f"{job_id}-g00", f"{job_id}-g01"], \
        "a dependency failure carrying (E005) is still not a content refusal"
    assert stored["providerFailures"][0]["terminal"] == "moderation_not_e005_not_retried"


@pytest.mark.asyncio
async def test_the_real_census_e005_message_is_retried(lab, monkeypatch):
    assert moderation_retry.retry_blocked(CENSUS_E005) is None
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 1)
    calls = []
    install_provider(monkeypatch, [CENSUS_E005, "done"], calls)
    await cp._run_dossier_generation(job_id)
    assert [call["id"] for call in calls] == [f"{job_id}-g00", f"{job_id}-g00-r1"]
    assert cp._load_jobs()["jobs"][job_id]["status"] == "completed"


# Lead decision (option b): the moderation model's own 401/403 keeps class
# provider_auth but names that dependency, so the Worker's W8 alert does not
# point at our Replicate billing. Our own token's 401/403 keeps "HTTP 40x".
LAB_DETAIL = re.compile(r"[A-Za-z0-9 _.:,;/()#=+-]{1,64}")  # routers/control_plane.py _STATUS_FAILURE_DETAIL


@pytest.mark.parametrize("message, detail", [
    (OBSERVED_MODERATION_AUTH, "moderation model HTTP 401"),
    ("Replicate failed: Warning: Moderation check failed: Error code: 403 - forbidden",
     "moderation model HTTP 403"),
    (AUTH, "HTTP 401"),
    ('Replicate start failed: {"title":"Forbidden","status":403}', "HTTP 403"),
])
def test_auth_detail_names_the_moderation_model_or_our_token(message, detail):
    assert base.classify_provider_error(message) == "provider_auth"
    assert base.provider_error_code(message) == detail
    assert WORKER_CLASS.fullmatch("provider_auth") and WORKER_DETAIL.fullmatch(detail)
    assert LAB_DETAIL.fullmatch(detail) and detail.strip() == detail
