"""Restart the same paid prediction; never infer permission to resubmit."""

import asyncio
import copy
from pathlib import Path
import shutil
import time

import httpx
import pytest

from providers import replicate
import routers.control_plane as cp
from services.generation_recovery import PredictionCheckpoint, input_hash, runner_lock, store_lock
from tests.test_control_plane_dossier_execution import HEADERS, job_body, lab
from tests.test_generation_moderation_isolation import queue_silhouettes
from tests.test_replicate_generation_retry import Script as LegacyScript, created, done, status, fast_clock, MODEL


class Script(LegacyScript):
    def handler(self, request):
        if request.method == "GET" and self.polls and isinstance(self.polls[0], asyncio.CancelledError):
            self.calls.append((request.method, request.url.path))
            raise self.polls.pop(0)
        return super().handler(request)


def checkpoint(records):
    def save(record):
        records[:] = [copy.deepcopy(record)]
    return PredictionCheckpoint(records[-1] if records else None, save)


async def provider_run(script, records, prompt="truck on a ridge"):
    params = {"model_id": MODEL, "entry": {"_prediction_checkpoint": checkpoint(records)},
              "duration": 6, "resolution": "1080p"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(script.handler)) as client:
        return await replicate.generate(prompt, params, client)


@pytest.mark.asyncio
async def test_accepted_prediction_survives_poll_cancellation_without_another_post():
    records = []
    first = Script([created("paid-id")], [asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await provider_run(first, records)
    assert records[-1]["predictionId"] == "paid-id"
    second = Script([], [done()])
    await provider_run(second, records)
    assert second.calls == [("GET", "/v1/predictions/paid-id")]


@pytest.mark.asyncio
async def test_lost_submission_response_is_not_resubmitted_on_restart():
    records = []
    first = Script([httpx.ReadTimeout("submission response lost")])
    with pytest.raises(httpx.ReadTimeout):
        await provider_run(first, records)
    assert records[-1]["state"] == "submitting"
    second = Script([])
    with pytest.raises(RuntimeError, match="generation_submission_uncertain"):
        await provider_run(second, records)
    assert second.calls == []


@pytest.mark.asyncio
async def test_durable_intent_failure_prevents_paid_submission():
    def fail(_record):
        raise OSError("disk full")
    script = Script([])
    async with httpx.AsyncClient(transport=httpx.MockTransport(script.handler)) as client:
        with pytest.raises(OSError, match="disk full"):
            await replicate.generate("truck", {"model_id": MODEL,
                "entry": {"_prediction_checkpoint": PredictionCheckpoint(None, fail)}}, client)
    assert script.calls == []


@pytest.mark.asyncio
async def test_changed_input_cannot_reuse_or_replace_a_paid_prediction():
    records = []
    with pytest.raises(asyncio.CancelledError):
        await provider_run(Script([created()], [asyncio.CancelledError()]), records)
    second = Script([])
    with pytest.raises(RuntimeError, match="input_mismatch"):
        await provider_run(second, records, prompt="different truck")
    assert second.calls == []


@pytest.mark.asyncio
async def test_existing_pa_retry_budget_survives_restart():
    records = []
    interrupted = status("failed", error="Prediction interrupted; please retry (code: PA)")
    first = Script([created("p1"), created("p2")], [interrupted, asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await provider_run(first, records)
    assert records[-1]["submission"] == 1 and records[-1]["predictionId"] == "p2"
    second = Script([], [interrupted])
    with pytest.raises(RuntimeError, match="Replicate failed"):
        await provider_run(second, records)
    assert second.calls == [("GET", "/v1/predictions/p2")]


@pytest.mark.asyncio
async def test_identity_write_failure_after_creation_never_resubmits():
    records = []
    def save(record):
        if record["state"] == "submitted":
            raise OSError("identity write failed")
        records[:] = [copy.deepcopy(record)]
    first = Script([created("paid-id")])
    async with httpx.AsyncClient(transport=httpx.MockTransport(first.handler)) as client:
        with pytest.raises(OSError, match="identity write failed"):
            await replicate.generate("truck on a ridge", {"model_id": MODEL,
                "duration": 6, "resolution": "1080p",
                "entry": {"_prediction_checkpoint": PredictionCheckpoint(None, save)}}, client)
    assert first.count("POST", "predictions") == 1
    second = Script([])
    with pytest.raises(RuntimeError, match="submission_uncertain"):
        await provider_run(second, records)
    assert second.calls == []


@pytest.mark.asyncio
async def test_submission_backoff_does_not_shorten_accepted_processing_budget(monkeypatch, fast_clock):
    monkeypatch.setattr(replicate.time, "time", lambda: 1_700_000_000 + fast_clock["now"])
    remaining = []
    async def finished(_client, _headers, _id, *, remaining_seconds):
        remaining.append(remaining_seconds)
        return "succeeded", "https://replicate.delivery/video.mp4"
    monkeypatch.setattr(replicate, "_await_prediction", finished)
    script = Script([httpx.Response(429, json={"retry_after": 30}), created()])
    records = []
    await provider_run(script, records)
    assert remaining == [600]
    assert records[-1]["submittedAt"] == 1_700_000_030


@pytest.mark.asyncio
@pytest.mark.parametrize("finished", [True, False])
async def test_restart_does_not_reset_processing_deadline(finished):
    params = replicate._build_hailuo_input("truck on a ridge", {"duration": 6, "resolution": "1080p"})
    records = [{"model": MODEL, "inputHash": input_hash(MODEL, params),
                "state": "submitted", "predictionId": "paid-id", "submission": 0,
                "submittedAt": time.time() - 601}]
    script = Script([], [done() if finished else status("processing")])
    if finished:
        await provider_run(script, records)
        assert script.calls == [("GET", "/v1/predictions/paid-id")]
    else:
        with pytest.raises(RuntimeError, match="timed out"):
            await provider_run(script, records)
        assert script.calls == [("GET", "/v1/predictions/paid-id"),
                                ("POST", "/v1/predictions/paid-id/cancel")]


def install_journal_provider(monkeypatch, *, interrupt_poll=None, interrupt_render=None, refuse=None,
                             refusal=None):
    calls = []
    seen = {"creates": 0, "interrupted": False}

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            seen["creates"] += 1
            return created(f"p{seen['creates']}")
        prediction = request.url.path.rsplit("/", 1)[-1]
        if prediction == interrupt_poll and not seen["interrupted"]:
            seen["interrupted"] = True
            raise asyncio.CancelledError("generation_runtime_shutdown")
        if prediction == refuse:
            return status("failed", **(refusal or {"error": "The input or output was flagged as sensitive. (E005)"}))
        return status("succeeded", output="https://replicate.delivery/" + prediction)

    async def generate(job_id, index, provider, prompt, aspect_ratio, resolution, duration,
                       image_data_uri, jobs, output_dir, _url_prefix, **extra):
        entry = jobs[job_id]["videos"][index]
        params = dict(extra, entry=entry, aspect_ratio=aspect_ratio, resolution=resolution,
                      duration=duration, image_data_uri=image_data_uri)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            try:
                url = await replicate.generate(prompt, params, client)
            except Exception as error:
                entry.update(status="error", error=str(error))
                return
        prediction = url.rsplit("/", 1)[-1]
        if prediction == interrupt_render and not seen["interrupted"]:
            seen["interrupted"] = True
            raise asyncio.CancelledError("generation_runtime_shutdown")
        output_dir.mkdir(parents=True, exist_ok=True)
        if extra.get("crop_mode") == "both":
            master = output_dir / f"{prediction}-master.mp4"
            master.write_bytes((prediction + "master").encode())
            entry["provider_master_file"] = master.name
            entry["crops"] = []
            for n in range(5):
                file = output_dir / f"{prediction}-crop{n}.mp4"
                file.write_bytes((prediction + f"crop{n}").encode())
                entry["crops"].append(dict(file=file.name, cropMode="both", cropIndex=n,
                                            cropCount=5, width=1080, height=1920))
            entry["file"] = entry["crops"][0]["file"]
        else:
            video = output_dir / f"{prediction}.mp4"
            video.write_bytes((prediction + "video").encode())
            still = output_dir / f"{prediction}.jpg"
            still.write_bytes((prediction + "still").encode())
            entry.update(file=video.name, provider_image_file=still.name)
        entry["status"] = "done"

    async def treatment(source, target, *_args, **_kwargs):
        shutil.copyfile(source, target)

    async def thumbnail(root, _video, index):
        target = root / f"thumbnail-{index}.jpg"
        target.write_bytes(b"thumbnail")
        return cp._generated_manifest(root, target)

    monkeypatch.setattr(cp, "generate_one", generate)
    monkeypatch.setattr(cp, "run_color_correct", treatment)
    monkeypatch.setattr(cp, "_thumbnail_manifest", thumbnail)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["poll", "render"])
async def test_original_batch_resumes_with_completed_bytes_and_same_paid_id(lab, monkeypatch, phase):
    job_id, body, _ = queue_silhouettes(lab, monkeypatch, 3)
    calls = install_journal_provider(monkeypatch, **{"interrupt_" + phase: "p2"})
    with pytest.raises(asyncio.CancelledError):
        await cp._run_dossier_generation(job_id)
    paused = cp._get_job_or_404(job_id)
    assert paused["status"] == "queued" and paused["resumePending"] is True
    assert paused["completedGenerationCalls"] == [0]
    assert paused["providerCheckpoints"]["1"]["predictionId"] == "p2"
    original = copy.deepcopy(paused["clips"][0])
    assert Path(paused["artifactRoot"]).exists()
    monkeypatch.setattr(cp, "_GENERATION_RUNTIME_ID", "replacement-runtime")
    await cp._run_dossier_generation(job_id)
    completed = cp._get_job_or_404(job_id)
    assert completed["status"] == "completed" and completed["providerCallsCompleted"] == 3
    assert completed["completedGenerationCalls"] == [0, 1, 2]
    assert completed["clips"][0] == original
    cp._verify_preserved_generation(Path(completed["artifactRoot"]), completed["clips"])
    assert len([c for c in calls if c[0] == "POST"]) == 3
    before = list(calls)
    await cp._run_dossier_generation(job_id)
    assert calls == before
    assert lab[0].post("/api/control-plane/v1/jobs", json=body, headers=HEADERS).json()["jobId"] == job_id


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed", [1, 5])
async def test_restart_between_crop_claims_keeps_exact_claim_and_finishes_set(lab, monkeypatch, claimed):
    client, _, _ = lab
    job_id = client.post("/api/control-plane/v1/jobs", json=job_body(quantity=5), headers=HEADERS).json()["jobId"]
    calls = install_journal_provider(monkeypatch)
    claim = cp._claim_unique_generated_clip
    seen = []
    def interrupted_claim(*args):
        claim(*args)
        seen.append(args)
        if len(seen) == claimed:
            raise asyncio.CancelledError("generation_runtime_shutdown")
    monkeypatch.setattr(cp, "_claim_unique_generated_clip", interrupted_claim)
    with pytest.raises(asyncio.CancelledError):
        await cp._run_dossier_generation(job_id)
    first = copy.deepcopy(cp._get_job_or_404(job_id)["clips"][0])
    await cp._run_dossier_generation(job_id)
    completed = cp._get_job_or_404(job_id)
    assert completed["status"] == "completed" and len(completed["clips"]) == 5
    assert completed["providerCallsCompleted"] == 1 and completed["completedGenerationCalls"] == [0]
    assert completed["clips"][0] == first
    assert {c["delivery"]["crop"]["index"] for c in completed["clips"]} == set(range(5))
    assert len([c for c in calls if c[0] == "POST"]) == 1
    cp._verify_preserved_generation(Path(completed["artifactRoot"]), completed["clips"])


@pytest.mark.asyncio
async def test_corrupt_preserved_bytes_fail_without_spending_or_deleting_evidence(lab, monkeypatch):
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 2)
    calls = install_journal_provider(monkeypatch, interrupt_poll="p2")
    with pytest.raises(asyncio.CancelledError):
        await cp._run_dossier_generation(job_id)
    paused = cp._get_job_or_404(job_id)
    root = Path(paused["artifactRoot"])
    (root / paused["clips"][0]["path"]).write_bytes(b"changed bytes")
    before = list(calls)
    await cp._run_dossier_generation(job_id)
    assert calls == before and root.exists()
    assert cp._get_job_or_404(job_id)["error"] == "generation_checkpoint_artifact_mismatch"


def test_status_restarts_only_exact_page_and_checkpointed_active_job(lab, monkeypatch):
    client, _, started = lab
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 1)
    cp._update_job(job_id, runtimeId="dead-runtime")
    prior = len(started)
    assert client.get(f"/api/control-plane/v1/jobs/{job_id}", headers={**HEADERS, "X-RT-Page-Id": "tt-other"}).status_code == 404
    assert len(started) == prior
    result = client.get(f"/api/control-plane/v1/jobs/{job_id}", headers=HEADERS).json()
    assert result["status"] == "queued" and len(started) == prior + 1


@pytest.mark.asyncio
async def test_restart_keeps_refusal_terminal_and_continues_original_next_candidate(lab, monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_MODERATION_RETRY_DAILY_BUDGET", "0")  # no varied retry available
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 2)
    calls = install_journal_provider(monkeypatch, interrupt_poll="p2", refuse="p1")
    with pytest.raises(asyncio.CancelledError):
        await cp._run_dossier_generation(job_id)
    await cp._run_dossier_generation(job_id)
    completed = cp._get_job_or_404(job_id)
    assert completed["status"] == "completed" and len(completed["clips"]) == 1
    assert completed["providerCallsCompleted"] == 1
    assert [f["providerRequestId"] for f in completed["providerFailures"]] == ["p1"]
    assert len([c for c in calls if c[0] == "POST"]) == 2
    assert calls.count(("GET", "/v1/predictions/p1")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["promptHash", "sourceTreatment"])
async def test_preserved_provenance_cannot_be_reinterpreted(lab, monkeypatch, field):
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 2)
    calls = install_journal_provider(monkeypatch, interrupt_poll="p2")
    with pytest.raises(asyncio.CancelledError):
        await cp._run_dossier_generation(job_id)
    job = cp._get_job_or_404(job_id)
    job["clips"][0][field] = "different"
    cp._update_job(job_id, clips=job["clips"])
    before = list(calls)
    await cp._run_dossier_generation(job_id)
    assert cp._get_job_or_404(job_id)["error"] == "generation_checkpoint_provenance_mismatch"
    assert calls == before


def test_kernel_runner_claim_prevents_second_owner_and_releases(tmp_path):
    path = tmp_path / "jobs.json"
    with runner_lock(path, "cpl-1111111111111111") as first:
        assert first
        with runner_lock(path, "cpl-1111111111111111") as second:
            assert not second
    with runner_lock(path, "cpl-1111111111111111") as replacement:
        assert replacement
    with store_lock(path):
        with store_lock(path):
            pass


_start_runner = cp._start_dossier_generation


@pytest.mark.asyncio
async def test_application_shutdown_pauses_owned_runner(lab, monkeypatch):
    job_id, _, _ = queue_silhouettes(lab, monkeypatch, 1)
    entered = asyncio.Event()
    async def blocked(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(cp, "generate_one", blocked)
    _start_runner(job_id)
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(cp.shutdown_dossier_generation(), 2)
    saved = cp._get_job_or_404(job_id)
    assert saved["status"] == "queued" and saved["resumePending"] is True
    assert Path(saved["artifactRoot"]).exists()
    assert job_id not in cp._generation_tasks


def _increment_store(path, count):
    from services.json_store import atomic_load, atomic_save
    for _ in range(count):
        with store_lock(path):
            current = atomic_load(path, default={"count": 0})
            time.sleep(0.003)
            atomic_save(path, {"count": current["count"] + 1})


def test_overlapping_runtime_transactions_do_not_drop_checkpoints(tmp_path):
    import multiprocessing
    from services.json_store import atomic_load
    path = tmp_path / "jobs.json"
    workers = [multiprocessing.get_context("spawn").Process(target=_increment_store, args=(path, 8)) for _ in range(3)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
        if worker.is_alive():
            worker.terminate()
            worker.join()
        assert worker.exitcode == 0
    assert atomic_load(path)["count"] == 24
