"""Compose Scrambler's real enqueue/ledger with the source runtime claim."""
import asyncio
import hashlib
import threading

import pytest
import routers.control_plane as cp
from services.generation_recovery import runner_lock
from services.scrambler import RENDERED, RESERVED, RELEASED, scrambler_history_rows
from tests.test_scrambler_executor_behavior import (
    lab, isolated_scrambler_state, _isolated_source_dna, _scrambler_on,
    _originality_feed, _wire_executor, job_body, headers, PAGE_ID, TOKEN,
)


def enqueue(lab, monkeypatch, *, treatment="none", key="owned-scrambler"):
    client, tmp_path, _ = lab
    _scrambler_on(monkeypatch)
    _originality_feed(monkeypatch, tmp_path)
    monkeypatch.setenv("SCRAMBLER_TREATMENT_WEIGHTS", ",".join(
        f"{name}:{int(name == treatment)}" for name in ("none", "split_swap", "camera", "grade")))
    response = client.post("/api/control-plane/v1/jobs", json=job_body(1), headers=headers(key))
    assert response.status_code == 200, response.json()
    job_id = response.json()["jobId"]
    _wire_executor(monkeypatch, tmp_path, [job_id])
    return job_id


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["mark", "transform", "split_swap", "release"])
async def test_cancel_drains_side_effect_before_job_and_page_claim_release(lab, monkeypatch, stage):
    treatment = {"transform": "camera", "split_swap": "split_swap"}.get(stage, "none")
    job_id = enqueue(lab, monkeypatch, treatment=treatment, key="drain-" + stage)
    if stage == "release":
        async def fail(*_args, **_kwargs):
            raise RuntimeError("controlled render failure")
        monkeypatch.setattr(cp, "run_color_correct", fail)
    name = {"mark": "mark_cut_rendered", "transform": "apply_transforms",
            "split_swap": "apply_split_swap", "release": "release_job_reservations"}[stage]
    actual = getattr(cp, name)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = 0
    def latched(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(10), "fixture barrier timed out"
        try:
            return actual(*args, **kwargs)
        finally:
            finished.set()
    monkeypatch.setattr(cp, name, latched)
    task = asyncio.create_task(cp._run_dossier_source(job_id))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel("owned-cancel")
        await asyncio.sleep(0.01)
        task.cancel("second-cancel")
        await asyncio.sleep(0.01)
        assert not task.done(), "the durable side effect is still running"
        with runner_lock(cp._jobs_path(), job_id) as acquired:
            assert not acquired
        page_claim = "scrambler-page-" + hashlib.sha256(PAGE_ID.encode()).hexdigest()
        with runner_lock(cp._jobs_path(), page_claim) as acquired:
            assert not acquired
        # A foreign reader cannot terminalize this owner or release its draws.
        monkeypatch.setattr(cp, "_GENERATION_RUNTIME_ID", "another-runtime")
        status = await cp.job_status(job_id, x_rt_page_id=PAGE_ID, authorization=f"Bearer {TOKEN}")
        assert status["status"] == "running"
        assert scrambler_history_rows(PAGE_ID)[0]["state"] == RESERVED
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await task
    assert cancelled.value.args == ("owned-cancel",)
    assert finished.is_set()
    with runner_lock(cp._jobs_path(), job_id) as acquired:
        assert acquired
    rows = scrambler_history_rows(PAGE_ID)
    assert rows[0]["state"] == (RENDERED if stage == "mark" else RELEASED)
    if stage == "mark":
        job = cp._load_jobs()["jobs"][job_id]
        assert job["scramblerRenderedClips"][0]["sha256"] == rows[0]["assetSha256"]
        assert __import__("pathlib").Path(job["artifactRoot"]).exists()


@pytest.mark.asyncio
async def test_rejected_durable_checkpoint_never_commits_rendered_ledger(lab, monkeypatch):
    job_id = enqueue(lab, monkeypatch, key="checkpoint-refused")
    actual = cp._update_source_job
    def reject(target, **fields):
        if "scramblerRenderedClips" in fields:
            actual(target, status="failed", error="controlled state fence")
            return None
        return actual(target, **fields)
    monkeypatch.setattr(cp, "_update_source_job", reject)
    commits = []
    monkeypatch.setattr(cp, "mark_cut_rendered", lambda *_args, **_kwargs: commits.append(True))
    await cp._run_dossier_source(job_id)
    assert commits == []
    assert cp._load_jobs()["jobs"][job_id]["status"] == "failed"
    assert scrambler_history_rows(PAGE_ID)[0]["state"] == RELEASED


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["status", "replay"])
@pytest.mark.parametrize("claim", [None, True, "1", 2, 1])
async def test_expiry_releases_only_a_proven_claim_after_terminal_write(lab, monkeypatch, door, claim):
    job_id = enqueue(lab, monkeypatch, key="expiry-" + door)
    store = cp._load_jobs()
    if claim is None:
        store["jobs"][job_id].pop("sourceClaimVersion")
    else:
        store["jobs"][job_id]["sourceClaimVersion"] = claim
    cp._save_jobs(store)
    actual = cp.release_job_reservations
    calls = []
    def release(*args, **kwargs):
        assert cp._load_jobs()["jobs"][job_id]["status"] == "failed"
        with runner_lock(cp._jobs_path(), job_id) as acquired:
            assert not acquired, "terminalization must retain its claimed identity"
        calls.append(True)
        return actual(*args, **kwargs)
    monkeypatch.setattr(cp, "release_job_reservations", release)
    monkeypatch.setattr(cp, "_GENERATION_RUNTIME_ID", "another-runtime")
    if door == "status":
        result = await cp.job_status(job_id, x_rt_page_id=PAGE_ID, authorization=f"Bearer {TOKEN}")
    else:
        client = lab[0]
        response = await asyncio.to_thread(client.post, "/api/control-plane/v1/jobs",
                                          json=job_body(1), headers=headers("expiry-" + door))
        assert response.status_code == 200
        result = response.json()
    valid = type(claim) is int and claim == 1
    assert result["status"] == ("failed" if valid else "queued")
    assert calls == ([True] if valid else [])
    assert scrambler_history_rows(PAGE_ID)[0]["state"] == (RELEASED if valid else RESERVED)


@pytest.mark.asyncio
async def test_cancel_during_outer_failure_release_still_terminalizes_before_claim_drop(lab, monkeypatch):
    job_id = enqueue(lab, monkeypatch, key="outer-release-cancel")
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("controlled setup failure")
    monkeypatch.setattr(cp, "load_registered_recipe", unavailable)
    actual = cp.release_job_reservations
    entered, release = threading.Event(), threading.Event()
    def latched(*args, **kwargs):
        if kwargs.get("reason") == "execution_setup_failed":
            entered.set()
            assert release.wait(10)
        return actual(*args, **kwargs)
    monkeypatch.setattr(cp, "release_job_reservations", latched)
    task = asyncio.create_task(cp._run_dossier_source(job_id))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel("first cleanup cancellation")
        await asyncio.sleep(0.01)
        task.cancel("second cleanup cancellation")
        await asyncio.sleep(0.01)
        with runner_lock(cp._jobs_path(), job_id) as acquired:
            assert not acquired
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cp._load_jobs()["jobs"][job_id]["status"] == "failed"
    assert scrambler_history_rows(PAGE_ID)[0]["state"] == RELEASED


@pytest.mark.asyncio
async def test_skipped_status_transition_never_releases_private_reservations(lab, monkeypatch):
    job_id = enqueue(lab, monkeypatch, key="skip-terminal-write")
    monkeypatch.setattr(cp, "_GENERATION_RUNTIME_ID", "another-runtime")
    monkeypatch.setattr(cp, "_update_source_job", lambda *_args, **_kwargs: None)
    calls = []
    monkeypatch.setattr(cp, "release_job_reservations", lambda *_args, **_kwargs: calls.append(True))
    result = await cp.job_status(job_id, x_rt_page_id=PAGE_ID, authorization=f"Bearer {TOKEN}")
    assert result["status"] == "queued"
    assert calls == []
    assert scrambler_history_rows(PAGE_ID)[0]["state"] == RESERVED


@pytest.mark.asyncio
async def test_failed_replay_terminal_write_never_releases_private_reservations(lab, monkeypatch):
    job_id = enqueue(lab, monkeypatch, key="failed-terminal-write")
    monkeypatch.setattr(cp, "_GENERATION_RUNTIME_ID", "another-runtime")
    def fail(*_args, **_kwargs):
        raise OSError("controlled persistence failure")
    monkeypatch.setattr(cp, "_save_jobs", fail)
    calls = []
    monkeypatch.setattr(cp, "release_job_reservations", lambda *_args, **_kwargs: calls.append(True))
    with pytest.raises(OSError, match="controlled persistence failure"):
        await asyncio.to_thread(lab[0].post, "/api/control-plane/v1/jobs",
                                json=job_body(1), headers=headers("failed-terminal-write"))
    assert cp._load_jobs()["jobs"][job_id]["status"] == "queued"
    assert calls == []
    assert scrambler_history_rows(PAGE_ID)[0]["state"] == RESERVED


@pytest.mark.asyncio
async def test_page_claim_covers_final_cleanup_before_next_same_page_execution(lab, monkeypatch):
    first = enqueue(lab, monkeypatch, key="page-cleanup-first")
    second = enqueue(lab, monkeypatch, key="page-cleanup-second")
    second_entered = asyncio.Event()
    async def execution(job_id):
        if job_id == second:
            second_entered.set()
        await cp._settle_source_update(job_id, status="completed")
    monkeypatch.setattr(cp, "_run_dossier_source_inner", execution)
    actual = cp.release_job_reservations
    entered, release = threading.Event(), threading.Event()
    def latched(page_id, job_id, **kwargs):
        if job_id == first and kwargs.get("reason") == "execution_finished":
            entered.set()
            assert release.wait(10)
        return actual(page_id, job_id, **kwargs)
    monkeypatch.setattr(cp, "release_job_reservations", latched)
    tasks = [asyncio.create_task(cp._run_dossier_source(first))]
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        tasks.append(asyncio.create_task(cp._run_dossier_source(second)))
        await asyncio.sleep(0.05)
        assert not second_entered.is_set(), "next execution entered before prior cleanup settled"
        page_claim = "scrambler-page-" + hashlib.sha256(PAGE_ID.encode()).hexdigest()
        with runner_lock(cp._jobs_path(), page_claim) as acquired:
            assert not acquired
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert second_entered.is_set()
    assert [row["state"] for row in scrambler_history_rows(PAGE_ID)] == [RELEASED, RELEASED]


@pytest.mark.asyncio
async def test_cancelled_page_waiter_terminalizes_only_its_own_reservations(lab, monkeypatch):
    first = enqueue(lab, monkeypatch, key="page-wait-first")
    second = enqueue(lab, monkeypatch, key="page-wait-cancel")
    entered, release = asyncio.Event(), asyncio.Event()
    async def execution(job_id):
        assert job_id == first, "cancelled waiter must never enter render lane"
        await cp._settle_source_update(job_id, status="running")
        entered.set()
        await release.wait()
        await cp._settle_source_update(job_id, status="completed")
    monkeypatch.setattr(cp, "_run_dossier_source_inner", execution)
    task = asyncio.create_task(cp._run_dossier_source(first))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        waiter = asyncio.create_task(cp._run_dossier_source(second))
        await asyncio.sleep(0.02)
        waiter.cancel("waiting cancellation")
        with pytest.raises(asyncio.CancelledError):
            await waiter
        jobs = cp._load_jobs()["jobs"]
        assert jobs[first]["status"] == "running"
        assert jobs[second]["status"] == "failed"
        states = {row["jobId"]: row["state"] for row in scrambler_history_rows(PAGE_ID)}
        assert states == {first: RESERVED, second: RELEASED}
        with runner_lock(cp._jobs_path(), first) as acquired:
            assert not acquired
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_first_cancellation_survives_late_side_effect_error():
    entered, release = threading.Event(), threading.Event()
    def work():
        entered.set()
        assert release.wait(5)
        raise RuntimeError("late side effect error")
    task = asyncio.create_task(cp._settle_source_call(work))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel("original cancellation")
        await asyncio.sleep(0.01)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError) as error:
        await task
    assert error.value.args == ("original cancellation",)
