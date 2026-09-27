"""A rejected candidate never cancels distinct planned candidates.

These tests pin the path where no varied moderation retry is available
(the per-page daily retry budget is 0): the refusal stays terminal and only
the job's other planned candidates run. Bounded retries are covered by
tests/test_generation_moderation_retry.py.
"""

import asyncio
import hashlib
import json
from pathlib import Path
import shutil

import pytest

import routers.control_plane as cp
import routers.control_plane_recipes as recipes
from tests.master_pages_fixtures import bind_current_intent, master_pages
from tests.test_control_plane_dossier_execution import (
    HEADERS, PAGE_ID, TOKEN, job_body, lab, recipe_publication,
)

MODERATION = "Replicate failed: The input or output was flagged as sensitive. (E005)"
CREDIT = 'Replicate start failed: {"status":402,"title":"Insufficient credit"}'


def no_retry_budget(monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_MODERATION_RETRY_DAILY_BUDGET", "0")


def queue_silhouettes(lab, monkeypatch, quantity):
    client, _, _ = lab
    publication = recipe_publication()
    spec = json.loads(publication["recipeSpecCanonical"])
    intent, revision = master_pages(PAGE_ID, handle="tucker.reeves", content_niche="silhouette")
    bind_current_intent(monkeypatch, cp, intent, revision)
    spec.update(masterPages=intent, masterPagesHash=revision)
    spec["demand"]["formatMix"] = {"silhouette-truck": 1.0}
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    publication.update(recipeId="silhouette-truck:master", recipeSpecCanonical=canonical,
                       recipeSpecHash="sha256:" + hashlib.sha256(canonical.encode()).hexdigest())
    assert client.post("/api/control-plane/v1/recipes", json=publication,
                       headers={**HEADERS, "Idempotency-Key": "silhouette-publication"}).status_code == 200
    body = job_body(quantity=quantity, lockedRecipeId=publication["recipeId"],
                    masterPages=intent, masterPagesHash=revision)
    queued = client.post("/api/control-plane/v1/jobs", json=body, headers=HEADERS)
    assert queued.status_code == 200
    job_id = queued.json()["jobId"]
    recipe = cp.resolve_generation_recipe(publication)
    assert recipe.engine == "flux-image"
    assert recipe.provider_model == "black-forest-labs/flux-2-pro"
    return job_id, body, recipe


def install_provider(monkeypatch, outcomes, calls, after_call=None):
    async def generate(provider_job_id, index, provider, prompt, aspect_ratio, resolution,
                       duration, image_data_uri, jobs, output_dir, url_prefix, **extra):
        call_index = len(calls)
        calls.append({"id": provider_job_id, "provider": provider, "prompt": prompt,
                      "aspect_ratio": aspect_ratio, "duration": duration, "extra": extra})
        outcome = outcomes[call_index]
        if outcome == "cancel":
            raise asyncio.CancelledError()
        entry = jobs[provider_job_id]["videos"][index]
        if outcome != "done":
            entry.update(status="error", error=outcome, provider_request_id=f"prediction-{call_index}")
        else:
            video = output_dir / f"{provider_job_id}.mp4"
            video.write_bytes(f"approved-video-{call_index}".encode())
            still = output_dir / f"{provider_job_id}.jpg"
            still.write_bytes(f"approved-still-{call_index}".encode())
            entry.update(status="done", file=video.name, provider_image_file=still.name)
        if after_call:
            after_call(call_index)

    async def treatment(source, destination, *_args, **_kwargs):
        shutil.copyfile(source, destination)

    async def thumbnail(job_root, video, index):
        target = job_root / f"thumbnail-{index}.jpg"
        target.write_bytes(b"thumbnail")
        return cp._generated_manifest(job_root, target)

    monkeypatch.setattr(cp, "generate_one", generate)
    monkeypatch.setattr(cp, "run_color_correct", treatment)
    monkeypatch.setattr(cp, "_thumbnail_manifest", thumbnail)


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [{0}, {9}, {0, 3, 6, 9}, set(range(10))])
async def test_moderation_isolated_to_each_of_ten_original_candidates(lab, monkeypatch, rejected):
    client, _, started = lab
    no_retry_budget(monkeypatch)
    job_id, body, recipe = queue_silhouettes(lab, monkeypatch, 10)
    original = cp._load_jobs()["jobs"][job_id]
    assert original["providerCallsPlanned"] == 10
    calls = []
    install_provider(monkeypatch, [MODERATION if i in rejected else "done" for i in range(10)], calls)
    await cp._run_dossier_generation(job_id)
    stored = cp._load_jobs()["jobs"][job_id]
    assert len(calls) == 10, "finish the original plan, never buy a replacement candidate"
    assert [call["id"] for call in calls] == [f"{job_id}-g{i:02d}" for i in range(10)]
    assert [cp.prompt_sha256(call["prompt"]) for call in calls] == [
        item["promptHash"] for item in original["promptPlan"]
    ], "without retry budget, do not retry, rewrite or replace the refused prompt"
    assert len({call["prompt"] for call in calls}) == 10
    assert all(call["provider"] == recipe.engine and call["extra"]["model_id"] == recipe.provider_model
               and call["aspect_ratio"] == "9:16" and call["duration"] == 7 for call in calls)
    assert all(call["extra"] == calls[0]["extra"] for call in calls), "safety/provider settings never change"
    assert stored["providerCallsPlanned"] == 10
    assert stored["providerCallsCompleted"] == 10 - len(rejected)
    assert stored["status"] == ("completed" if len(rejected) < 10 else "failed")
    assert stored["error"] == "provider_generation_failed"
    assert [f["generationIndex"] for f in stored["providerFailures"]] == sorted(rejected)
    assert all(f["class"] == "moderation" and f["model"] == recipe.provider_model
               and f["providerRequestId"] == f"prediction-{f['generationIndex']}"
               for f in stored["providerFailures"])
    assert stored["providerFailure"] == stored["providerFailures"][-1]
    assert {clip["generationIndex"] for clip in stored["clips"]} == set(range(10)) - rejected
    for clip in stored["clips"]:
        assert (Path(stored["artifactRoot"]) / clip["path"]).is_file()
        assert (Path(stored["artifactRoot"]) / clip["generatedStill"]["path"]).is_file()
        assert clip["sourceTreatment"]["sourceSha256"] == clip["sha256"]
    if len(rejected) == 10:
        assert not Path(stored["artifactRoot"]).exists()
    status = client.get(f"/api/control-plane/v1/jobs/{job_id}",
                        headers={"Authorization": f"Bearer {TOKEN}", "X-RT-Page-Id": PAGE_ID}).json()
    expected_keys = {"schema", "jobId", "status", "progress"}
    if stored["status"] == "failed":
        expected_keys |= {"error", "errorClass", "errorDetail"}
    assert set(status) == expected_keys
    if stored["status"] == "failed":
        assert (status["errorClass"], status["errorDetail"]) == ("moderation", "E005")
    assert status["status"] == stored["status"]
    replay = client.post("/api/control-plane/v1/jobs", json=body, headers=HEADERS)
    assert replay.status_code == 200 and replay.json()["jobId"] == job_id
    assert started == [job_id], "terminal replay must not restart any refused or completed prediction"


@pytest.mark.asyncio
@pytest.mark.parametrize("hard_failure", [CREDIT, "ReadTimeout(submission response lost)", "provider timed out", "unknown provider fault"])
@pytest.mark.parametrize("has_prior_output", [False, True])
async def test_non_moderation_failure_stops_remaining_plan(lab, monkeypatch, hard_failure, has_prior_output):
    no_retry_budget(monkeypatch)
    outcomes = (["done"] if has_prior_output else []) + [MODERATION, hard_failure, "done"]
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, len(outcomes))
    calls = []
    install_provider(monkeypatch, outcomes, calls)
    await cp._run_dossier_generation(job_id)
    stored = cp._load_jobs()["jobs"][job_id]
    assert len(calls) == len(outcomes) - 1, "no later spend after credit/ambiguous/other faults"
    assert stored["status"] == ("completed" if has_prior_output else "failed")
    assert stored["providerCallsCompleted"] == int(has_prior_output)
    assert stored["error"] == "provider_generation_failed"
    assert len(stored["clips"]) == int(has_prior_output)
    assert [f["class"] for f in stored["providerFailures"]] == ["moderation", cp.classify_provider_error(hard_failure)]


@pytest.mark.asyncio
async def test_moderation_does_not_continue_after_page_strategy_changes(lab, monkeypatch):
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 2)
    calls = []
    def change_strategy(_index):
        monkeypatch.setattr(cp, "_job_matches_current_master_pages", lambda _job: False)
    install_provider(monkeypatch, [MODERATION, "done"], calls, after_call=change_strategy)
    await cp._run_dossier_generation(job_id)
    stored = cp._load_jobs()["jobs"][job_id]
    assert len(calls) == 1
    assert stored["status"] == "failed" and stored["error"] == "master_pages_strategy_changed"
    assert stored["clips"] == []
    assert not Path(stored["artifactRoot"]).exists()


@pytest.mark.asyncio
async def test_cancellation_after_moderation_keeps_its_failure_and_cleans_only_own_job(lab, monkeypatch):
    no_retry_budget(monkeypatch)
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 3)
    calls = []
    install_provider(monkeypatch, [MODERATION, "cancel", "done"], calls)
    with pytest.raises(asyncio.CancelledError):
        await cp._run_dossier_generation(job_id)
    stored = cp._load_jobs()["jobs"][job_id]
    assert len(calls) == 2
    assert stored["status"] == "failed" and stored["error"] == "generation_cancelled"
    assert stored["providerFailure"]["class"] == "moderation"
    assert not Path(stored["artifactRoot"]).exists()
