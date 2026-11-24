"""Local render batching: jobs for the loaded model go first, nobody starves."""

import asyncio
import time

import httpx
import pytest

from providers import comfyui
from routers import video as video_router
from services import local_render_order as order
from services import local_worker_queue as queue

SERVER = "http://comfy.test:8188"
WAN, H3 = comfyui.WAN_T2V, comfyui.H3_FL2VA
HOUR = 3600.0


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(comfyui, "_FREE_SETTLE_SECONDS", 0)
    monkeypatch.delenv("LOCAL_BATCH_MAX_WAIT_SECONDS", raising=False)
    monkeypatch.setenv("LOCAL_WORKER_KEY", "k")
    comfyui._server_gates.clear()
    comfyui._server_model.clear()
    queue.reset()
    yield
    comfyui._server_gates.clear()
    comfyui._server_model.clear()
    queue.reset()


# --- the ordering rule -----------------------------------------------------

def test_nothing_waiting_picks_nothing():
    assert order.pick([], WAN, 0.0, HOUR) is None


def test_unknown_server_state_takes_the_oldest_job():
    assert order.pick([(H3, 5.0), (WAN, 1.0)], None, 10.0, HOUR) == 1


def test_loaded_model_jobs_go_ahead_of_older_ones():
    pending = [(H3, 0.0), (WAN, 1.0), (H3, 2.0), (WAN, 3.0)]
    assert order.pick(pending, WAN, 60.0, HOUR) == 1


def test_switches_when_the_loaded_model_has_nothing_left():
    assert order.pick([(H3, 4.0), (H3, 2.0)], WAN, 60.0, HOUR) == 1


def test_a_job_waiting_past_the_limit_goes_next():
    pending = [(H3, 0.0), (WAN, 100.0)]
    assert order.pick(pending, WAN, HOUR - 1, HOUR) == 1
    assert order.pick(pending, WAN, HOUR, HOUR) == 0


def test_zero_limit_is_plain_oldest_first():
    assert order.pick([(H3, 0.0), (WAN, 1.0)], WAN, 2.0, 0.0) == 0


@pytest.mark.parametrize("raw, expected", [
    (None, 1800.0), ("", 1800.0), ("600", 600.0), ("0", 0.0), ("-5", 0.0), ("soon", 1800.0)])
def test_limit_comes_from_the_environment(monkeypatch, raw, expected):
    if raw is not None:
        monkeypatch.setenv("LOCAL_BATCH_MAX_WAIT_SECONDS", raw)
    assert order.max_wait_seconds() == expected


# --- worker queue (lab on Railway, worker on the studio) -------------------

def _enqueue(*models: str) -> list[queue.Job]:
    return [queue.enqueue({"model_id": m, "prompt": "p", "params": {}}) for m in models]


async def test_worker_claims_drain_one_model_before_the_next():
    jobs = _enqueue(WAN, H3, WAN, H3, WAN)
    claimed = [queue.claim()["id"] for _ in jobs]
    assert claimed == [jobs[i].id for i in (0, 2, 4, 1, 3)]
    assert queue.claim() is None


async def test_worker_claim_switches_for_a_job_that_waited_too_long(monkeypatch):
    monkeypatch.setenv("LOCAL_BATCH_MAX_WAIT_SECONDS", "60")
    w1, h1, w2 = _enqueue(WAN, H3, WAN)
    assert queue.claim()["id"] == w1.id
    h1.created -= 120  # has been waiting two minutes behind the Wan batch
    assert queue.claim()["id"] == h1.id
    assert queue.claim()["id"] == w2.id


async def test_worker_claim_skips_jobs_whose_owner_gave_up():
    w1, h1, w2 = _enqueue(WAN, H3, WAN)
    assert queue.claim()["id"] == w1.id
    w2.future.cancel()
    assert queue.claim()["id"] == h1.id
    assert queue.claim() is None


# --- direct mode (lab talks to ComfyUI itself) -----------------------------

def _recording_server(events: list[str]):
    state = {"n": 0, "polls": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/free":
            events.append("free")
            return httpx.Response(200, json={})
        if path == "/prompt":
            body = request.content.decode()
            events.append("h3" if "minimax_h3" in body else "wan")
            state["n"] += 1
            return httpx.Response(200, json={"prompt_id": f"p-{state['n']}"})
        if path.startswith("/history/"):
            pid = path.rsplit("/", 1)[1]
            state["polls"][pid] = state["polls"].get(pid, 0) + 1
            if state["polls"][pid] < 3:
                return httpx.Response(200, json={})
            return httpx.Response(200, json={pid: {
                "status": {"status_str": "success", "completed": True, "messages": []},
                "outputs": {"save": {"images": [
                    {"filename": "x.mp4", "subfolder": "content-lab", "type": "output"}]}}}})
        return httpx.Response(404)

    return handler


async def test_direct_jobs_run_grouped_by_model_with_one_switch(monkeypatch):
    events: list[str] = []
    monkeypatch.setitem(comfyui.API_KEYS, "comfyui", SERVER)
    monkeypatch.setattr(comfyui, "_POLL_SECONDS", 0.001)
    arrivals = [WAN, H3, WAN, H3, WAN]
    async with httpx.AsyncClient(transport=httpx.MockTransport(_recording_server(events))) as client:
        await asyncio.gather(*(
            comfyui.generate("x", {"entry": {}, "model_id": m, "aspect_ratio": "9:16"}, client)
            for m in arrivals))
    assert events == ["free", "wan", "wan", "wan", "free", "h3", "h3"]
    assert not comfyui._server_gates[SERVER].locked()


async def test_gate_drops_a_waiter_cancelled_in_line():
    gate = comfyui._ServerGate(SERVER)
    await gate.acquire(WAN)
    first = asyncio.create_task(gate.acquire(H3))
    second = asyncio.create_task(gate.acquire(H3))
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    gate.release()
    await asyncio.wait_for(second, 1)
    assert gate.locked()
    gate.release()
    assert not gate.locked()


async def test_gate_passes_on_a_turn_handed_to_a_cancelled_waiter():
    gate = comfyui._ServerGate(SERVER)
    await gate.acquire(WAN)
    first = asyncio.create_task(gate.acquire(H3))
    second = asyncio.create_task(gate.acquire(H3))
    await asyncio.sleep(0)
    gate.release()   # hands the server to `first`...
    first.cancel()   # ...which is cancelled before it can run
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.wait_for(second, 1)
    assert gate.locked()
    gate.release()
    assert not gate.locked()


# --- admission -------------------------------------------------------------

def test_local_jobs_do_not_take_the_paid_providers_slots(sync_client, monkeypatch):
    """With every paid slot busy, a local generation still runs."""
    monkeypatch.setitem(video_router.API_KEYS, "comfyui", SERVER)
    monkeypatch.setattr(video_router, "_gen_semaphore", asyncio.Semaphore(0))
    monkeypatch.setattr(video_router, "_local_gen_semaphore", asyncio.Semaphore(1))

    async def fake_generate_one(job_id, index, provider, prompt, aspect_ratio, resolution,
                                duration, image_data_uri, jobs, output_dir, url_prefix,
                                on_complete=None, **extra):
        entry = jobs[job_id]["videos"][index]
        (output_dir / "local.mp4").write_bytes(b"video")
        entry.update(status="done", file="local.mp4", url=f"{url_prefix}/local.mp4")
        if on_complete:
            on_complete(job_id)

    monkeypatch.setattr(video_router, "generate_one", fake_generate_one)
    response = sync_client.post("/api/video/generate", data={
        "prompt": "a calm sea", "provider": "local-wan-t2v", "count": "1",
        "aspect_ratio": "9:16", "resolution": "480p", "project": "video-suite"})
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    deadline = time.monotonic() + 5
    status = None
    while time.monotonic() < deadline:
        status = sync_client.get(f"/api/video/jobs/{job_id}").json()["videos"][0]["status"]
        if status == "done":
            break
        time.sleep(0.01)
    assert status == "done"


@pytest.mark.parametrize("raw, expected", [(None, 60), ("200", 200), ("0", 1), ("lots", 60)])
def test_local_admission_limit_comes_from_the_environment(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("LOCAL_GEN_MAX_PENDING", raising=False)
    else:
        monkeypatch.setenv("LOCAL_GEN_MAX_PENDING", raw)
    assert video_router._local_gen_limit() == expected

