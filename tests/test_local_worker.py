"""Local worker hand-off: queue rules, worker endpoints, and the worker loop."""

import asyncio
import importlib.util
import json
import time
from pathlib import Path

import httpx
import pytest

from project_manager import get_project_video_dir
from providers import comfyui
from routers import video as video_router
from services import local_worker_queue as queue

KEY = "worker-secret"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
AUTH = {"X-Worker-Key": KEY}


@pytest.fixture(autouse=True)
def fresh_comfy_server_state(monkeypatch):
    monkeypatch.setattr(comfyui, "_FREE_SETTLE_SECONDS", 0)
    comfyui._server_gates.clear()
    comfyui._server_model.clear()
    yield
    comfyui._server_gates.clear()
    comfyui._server_model.clear()


@pytest.fixture(autouse=True)
def clean_queue(monkeypatch):
    queue.reset()
    monkeypatch.setenv("LOCAL_WORKER_KEY", KEY)
    yield
    queue.reset()


def _claim_headers(job: dict) -> dict:
    return {**AUTH, "X-Claim-Token": job["claim_token"]}


# --- queue rules -----------------------------------------------------------

async def test_claimed_job_resolves_with_the_uploaded_path(tmp_path, monkeypatch):
    monkeypatch.setattr(queue, "_WAIT_SLICE_SECONDS", 0.01)
    job = queue.enqueue({"model_id": "m", "prompt": "p", "params": {}})
    entry: dict = {}
    waiter = asyncio.create_task(queue.wait(job, entry, 60))
    claimed = queue.claim()
    assert claimed["id"] == job.id and claimed["prompt"] == "p"
    await asyncio.sleep(0.05)
    assert entry["status"] == "polling"
    video = tmp_path / "v.mp4"
    queue.complete(job.id, claimed["claim_token"], video)
    assert await waiter == video
    # A finished job accepts nothing further.
    with pytest.raises(queue.ClaimRejected):
        queue.fail(job.id, claimed["claim_token"], "late")


async def test_worker_failure_reaches_the_waiting_job(monkeypatch):
    monkeypatch.setattr(queue, "_WAIT_SLICE_SECONDS", 0.01)
    job = queue.enqueue({"model_id": "m", "prompt": "p", "params": {}})
    waiter = asyncio.create_task(queue.wait(job, {}, 60))
    claimed = queue.claim()
    queue.fail(job.id, claimed["claim_token"], "UNETLoader: file missing")
    with pytest.raises(queue.LocalWorkerError, match="UNETLoader: file missing"):
        await waiter


async def test_wrong_claim_token_is_rejected():
    job = queue.enqueue({"model_id": "m", "prompt": "p", "params": {}})
    queue.claim()
    for call in (lambda: queue.heartbeat(job.id, "nope"),
                 lambda: queue.complete(job.id, "nope", Path("x")),
                 lambda: queue.fail(job.id, "", "x")):
        with pytest.raises(queue.ClaimRejected):
            call()
    assert not job.future.done()


async def test_job_fails_closed_when_no_worker_is_online(monkeypatch):
    monkeypatch.setattr(queue, "_WAIT_SLICE_SECONDS", 0.01)
    monkeypatch.setattr(queue, "WORKER_STALE_SECONDS", 0.03)
    job = queue.enqueue({"model_id": "m", "prompt": "p", "params": {}})
    with pytest.raises(queue.LocalWorkerError, match="no local worker is online"):
        await queue.wait(job, {}, 60)
    assert queue.claim() is None  # the abandoned job is not handed out later


async def test_job_fails_when_worker_goes_silent_mid_render(monkeypatch):
    monkeypatch.setattr(queue, "_WAIT_SLICE_SECONDS", 0.01)
    monkeypatch.setattr(queue, "WORKER_STALE_SECONDS", 0.05)
    job = queue.enqueue({"model_id": "m", "prompt": "p", "params": {}})
    queue.claim()
    with pytest.raises(queue.LocalWorkerError, match="went offline"):
        await queue.wait(job, {}, 60)


async def test_render_timeout_counts_from_the_claim(monkeypatch):
    monkeypatch.setattr(queue, "_WAIT_SLICE_SECONDS", 0.01)
    job = queue.enqueue({"model_id": "m", "prompt": "p", "params": {}})
    claimed = queue.claim()

    async def keep_alive():
        while True:
            queue.heartbeat(job.id, claimed["claim_token"])
            await asyncio.sleep(0.005)

    beat = asyncio.create_task(keep_alive())
    try:
        with pytest.raises(queue.LocalWorkerError, match="timed out"):
            await queue.wait(job, {}, 0.05)
    finally:
        beat.cancel()


async def test_jobs_are_claimed_oldest_first():
    first = queue.enqueue({"model_id": "m", "prompt": "one", "params": {}})
    queue.enqueue({"model_id": "m", "prompt": "two", "params": {}})
    assert queue.claim()["id"] == first.id
    assert queue.claim()["prompt"] == "two"
    assert queue.claim() is None


# --- provider in worker-queue mode -----------------------------------------

async def test_provider_queues_only_whitelisted_fields(monkeypatch, tmp_path):
    monkeypatch.setitem(comfyui.API_KEYS, "comfyui", comfyui.WORKER_QUEUE)
    monkeypatch.setattr(queue, "_WAIT_SLICE_SECONDS", 0.01)
    entry: dict = {}
    task = asyncio.create_task(comfyui.generate("a calm sea", {
        "entry": entry, "model_id": comfyui.H3_FL2VA, "aspect_ratio": "9:16", "duration": 6,
        "job_id": "lab-job#0", "cost_usd": 0.0, "variant": None, "go_fast": True}, None))
    await asyncio.sleep(0.02)
    claimed = queue.claim()
    assert claimed["model_id"] == comfyui.H3_FL2VA and claimed["prompt"] == "a calm sea"
    assert claimed["params"] == {"aspect_ratio": "9:16", "duration": 6, "go_fast": True}
    assert entry["provider_request_id"] == claimed["id"]
    video = tmp_path / "v.mp4"
    queue.complete(claimed["id"], claimed["claim_token"], video)
    assert await task == video


async def test_provider_refuses_a_bad_request_before_queueing(monkeypatch):
    monkeypatch.setitem(comfyui.API_KEYS, "comfyui", comfyui.WORKER_QUEUE)
    with pytest.raises(ValueError):
        await comfyui.generate("x", {"entry": {}, "model_id": comfyui.WAN_I2V}, None)
    assert queue.claim() is None


# --- worker endpoints ------------------------------------------------------

def test_endpoints_are_closed_without_a_configured_key(sync_client, monkeypatch):
    monkeypatch.delenv("LOCAL_WORKER_KEY")
    assert sync_client.post("/api/local-worker/claim", headers=AUTH).status_code == 503


@pytest.mark.parametrize("headers", [{}, {"X-Worker-Key": "wrong"}])
def test_endpoints_reject_a_missing_or_wrong_key(sync_client, headers):
    assert sync_client.post("/api/local-worker/claim", headers=headers).status_code == 401
    assert sync_client.post("/api/local-worker/jobs/x/result", headers=headers, content=MP4).status_code == 401
    assert not queue.worker_online()


def test_claim_with_nothing_waiting_returns_null_and_marks_worker_online(sync_client):
    response = sync_client.post("/api/local-worker/claim", headers=AUTH)
    assert response.status_code == 200 and response.json() == {"job": None}
    assert queue.worker_online()


def test_unknown_job_is_refused_on_every_job_route(sync_client):
    headers = {**AUTH, "X-Claim-Token": "t"}
    assert sync_client.post("/api/local-worker/jobs/nope/heartbeat", headers=headers).status_code == 409
    assert sync_client.post("/api/local-worker/jobs/nope/fail", headers=headers, json={"error": "x"}).status_code == 409
    assert sync_client.post("/api/local-worker/jobs/nope/result", headers=headers, content=MP4).status_code == 409


def _start_local_job(sync_client, monkeypatch) -> str:
    monkeypatch.setitem(video_router.API_KEYS, "comfyui", comfyui.WORKER_QUEUE)
    response = sync_client.post("/api/video/generate", data={
        "prompt": "a calm sea", "provider": "local-h3-vertical", "count": "1", "duration": "5",
        "aspect_ratio": "9:16", "project": "video-suite"})
    assert response.status_code == 200, response.text
    return response.json()["job_id"]


def _claim(sync_client) -> dict:
    for _ in range(200):
        job = sync_client.post("/api/local-worker/claim", headers=AUTH).json()["job"]
        if job:
            return job
        time.sleep(0.01)
    raise AssertionError("job was never queued")


def _wait_for(sync_client, job_id: str, status: str) -> dict:
    video = {}
    for _ in range(400):
        video = sync_client.get(f"/api/video/jobs/{job_id}").json()["videos"][0]
        if video["status"] == status:
            return video
        time.sleep(0.02)
    raise AssertionError(f"job stayed {video.get('status')}: {video.get('error')}")


def test_worker_upload_completes_the_lab_job(sync_client, monkeypatch):
    monkeypatch.setattr(queue, "_WAIT_SLICE_SECONDS", 0.01)
    job_id = _start_local_job(sync_client, monkeypatch)
    job = _claim(sync_client)
    assert job["model_id"] == comfyui.H3_FL2VA and job["prompt"] == "a calm sea"
    assert sync_client.post(f"/api/local-worker/jobs/{job['id']}/heartbeat",
                            headers=_claim_headers(job)).status_code == 200
    upload = sync_client.post(f"/api/local-worker/jobs/{job['id']}/result",
                              headers=_claim_headers(job), content=MP4)
    assert upload.status_code == 200 and upload.json()["bytes"] == len(MP4)
    video = _wait_for(sync_client, job_id, "done")
    assert (get_project_video_dir("video-suite") / video["file"]).read_bytes() == MP4
    # The same claim cannot deliver twice.
    assert sync_client.post(f"/api/local-worker/jobs/{job['id']}/result",
                            headers=_claim_headers(job), content=MP4).status_code == 409


def test_worker_failure_marks_the_lab_job_failed(sync_client, monkeypatch):
    monkeypatch.setattr(queue, "_WAIT_SLICE_SECONDS", 0.01)
    job_id = _start_local_job(sync_client, monkeypatch)
    job = _claim(sync_client)
    assert sync_client.post(f"/api/local-worker/jobs/{job['id']}/fail", headers=_claim_headers(job),
                            json={"error": "ComfyUI generation failed: out of memory"}).status_code == 200
    video = _wait_for(sync_client, job_id, "error")
    assert "out of memory" in video["error"]


def test_upload_that_is_not_a_video_is_refused_and_job_stays_open(sync_client, monkeypatch):
    monkeypatch.setattr(queue, "_WAIT_SLICE_SECONDS", 0.01)
    _start_local_job(sync_client, monkeypatch)
    job = _claim(sync_client)
    bad = sync_client.post(f"/api/local-worker/jobs/{job['id']}/result",
                           headers=_claim_headers(job), content=b"<html>error page</html>")
    assert bad.status_code == 400
    assert sync_client.post(f"/api/local-worker/jobs/{job['id']}/result",
                            headers=_claim_headers(job), content=MP4).status_code == 200


def test_oversized_upload_is_refused(sync_client, monkeypatch):
    monkeypatch.setattr(queue, "_WAIT_SLICE_SECONDS", 0.01)
    monkeypatch.setenv("CONTENT_LAB_PROVIDER_DOWNLOAD_BYTES", "32")
    _start_local_job(sync_client, monkeypatch)
    job = _claim(sync_client)
    assert sync_client.post(f"/api/local-worker/jobs/{job['id']}/result",
                            headers=_claim_headers(job), content=MP4).status_code == 413


def test_local_engines_are_offered_when_only_a_worker_key_is_set(sync_client, monkeypatch):
    monkeypatch.setitem(video_router.API_KEYS, "comfyui", comfyui.WORKER_QUEUE)
    listed = {p["id"] for p in sync_client.get("/api/video/providers").json()}
    assert {"local-h3", "local-h3-vertical", "local-wan-t2v", "local-wan-i2v"} <= listed


# --- the worker script -----------------------------------------------------

def _load_worker_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "local_comfy_worker.py"
    spec = importlib.util.spec_from_file_location("local_comfy_worker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeLabAndComfy:
    """One transport answering for both hosts the worker talks to."""

    def __init__(self, history: dict):
        self.history = history
        self.calls: list[tuple[str, str]] = []
        self.uploaded: bytes | None = None
        self.failure: str | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        self.calls.append((host, path))
        if host == "lab.test":
            assert request.headers["x-worker-key"] == KEY
            if path.endswith("/result"):
                assert request.headers["x-claim-token"] == "tok"
                self.uploaded = request.content
            elif path.endswith("/fail"):
                self.failure = json.loads(request.content)["error"]
            return httpx.Response(200, json={"ok": True})
        if path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "p-1"})
        if path == "/history/p-1":
            return httpx.Response(200, json={"p-1": self.history})
        if path == "/view":
            return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})
        return httpx.Response(404)


_JOB = {"id": "j1", "claim_token": "tok", "model_id": comfyui.H3_FL2VA,
        "prompt": "a calm sea", "params": {"aspect_ratio": "9:16", "duration": 2}}


async def test_worker_renders_and_uploads_a_claimed_job(monkeypatch):
    module = _load_worker_module()
    monkeypatch.setattr(comfyui, "_POLL_SECONDS", 0)
    fake = _FakeLabAndComfy({
        "status": {"status_str": "success", "completed": True, "messages": []},
        "outputs": {"save": {"images": [{"filename": "h3_1.mp4", "subfolder": "content-lab", "type": "output"}]}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
        worker = module.Worker("http://lab.test/", KEY, "http://comfy.test:8188", client)
        assert await worker.run_job(dict(_JOB)) is True
    assert fake.uploaded == MP4 and fake.failure is None
    assert ("comfy.test", "/prompt") in fake.calls
    assert ("lab.test", "/api/local-worker/jobs/j1/result") in fake.calls


async def test_worker_reports_a_failed_render(monkeypatch):
    module = _load_worker_module()
    monkeypatch.setattr(comfyui, "_POLL_SECONDS", 0)
    fake = _FakeLabAndComfy({"status": {"status_str": "error", "completed": False, "messages": [
        ["execution_error", {"node_type": "UNETLoader", "exception_message": "file missing"}]]}, "outputs": {}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
        worker = module.Worker("http://lab.test", KEY, "http://comfy.test:8188", client)
        assert await worker.run_job(dict(_JOB)) is False
    assert fake.uploaded is None
    assert "UNETLoader: file missing" in fake.failure
