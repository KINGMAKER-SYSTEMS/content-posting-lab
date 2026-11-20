"""Local-first generation with a bounded hosted fallback (providers/backend_order.py).

The Studio-down drill points COMFYUI_URL at a loopback port nothing listens on:
the job must land on the hosted twin, log one ALERT for the outage (not one per
job) and one fallback line per job, and never reserve the same job twice.
"""

import json
import logging
import socket
import time

import httpx
import pytest

from providers import PROVIDERS, backend_order, base, comfyui, replicate
from services import generation_budget, local_worker_queue
from services import visual_admission as gate

KEY = "worker-secret"


def _unreachable_url() -> str:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens here now: connections are refused
    return f"http://127.0.0.1:{port}"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    backend_order.reset()
    local_worker_queue.reset()
    comfyui._server_locks.clear()
    comfyui._server_model.clear()
    comfyui._server_waiting.clear()
    monkeypatch.setattr(comfyui, "_FREE_SETTLE_SECONDS", 0)
    store = tmp_path / "control_plane_jobs.json"
    monkeypatch.setattr(generation_budget, "jobs_store_path", lambda: store)
    monkeypatch.setenv("LAB_GENERATION_DAILY_BUDGET_USD", "50")
    monkeypatch.delenv("CONTENT_LAB_BACKEND_ORDER", raising=False)
    monkeypatch.delenv("CONTENT_LAB_VISION_STUDIO_URL", raising=False)

    async def fake_download(client, url, dest):
        dest.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)

    monkeypatch.setattr(base, "download_media", fake_download)
    yield
    backend_order.reset()
    local_worker_queue.reset()


class _Recorder:
    def __init__(self, result="https://hosted.test/out.mp4", errors=()):
        self.calls: list[dict] = []
        self.result = result
        self.errors = list(errors)

    async def __call__(self, prompt, params, client):
        self.calls.append(params)
        if self.errors:
            raise self.errors.pop(0)
        params["entry"]["provider_request_id"] = f"req-{len(self.calls)}"
        return self.result


@pytest.fixture
def hosted(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(replicate, "generate", recorder)
    return recorder


def _spent() -> float:
    return generation_budget.spent_usd_at(generation_budget.jobs_store_path())


async def _job(tmp_path, provider, job_id="job-1", entry_extra=None, **extra):
    entry = {"index": 0, "status": "queued", **(entry_extra or {})}
    jobs = {job_id: {"videos": [entry]}}
    await base.generate_one(job_id, 0, provider, "a calm sea", "9:16", "480p", 5, None,
                            jobs, tmp_path, **extra)
    return jobs[job_id]["videos"][0]


def _messages(caplog, needle):
    return [r.getMessage() for r in caplog.records if needle in r.getMessage()]


# --- the Studio-down drill ---------------------------------------------------

async def test_studio_down_drill_falls_to_hosted_with_one_warning(monkeypatch, tmp_path, hosted, caplog):
    monkeypatch.setenv("CONTENT_LAB_BACKEND_ORDER", "on")
    monkeypatch.setitem(base.API_KEYS, "comfyui", _unreachable_url())
    caplog.set_level(logging.INFO, logger="content_lab.backend_order")

    first = await _job(tmp_path, "wan-t2v", "job-1")
    second = await _job(tmp_path, "wan-t2v", "job-2")

    for entry in (first, second):
        assert entry["status"] == "done", entry.get("error")
        assert entry["served_provider"] == "wan-t2v"
        assert entry["billing"] == "hosted"
        assert entry["backend_attempts"] == [
            {"provider": "local-wan-t2v", "outcome": "skipped", "reason": "unreachable"},
            {"provider": "wan-t2v", "outcome": "served"},
        ]
    assert len(hosted.calls) == 2
    # The outage is handled, so it is one warning line, not an alert.
    assert _messages(caplog, "unavailable") == [
        "backend-order: local engine local-wan-t2v unavailable (unreachable); serving from the hosted twin"]
    assert _messages(caplog, "ALERT") == []
    assert len(_messages(caplog, "backend-order fallback")) == 2
    # The caller reserved the provider it asked for; the fallback adds nothing.
    assert _spent() == 0


async def test_local_request_falls_to_hosted_twin_and_reserves_once(monkeypatch, tmp_path, hosted):
    monkeypatch.setenv("CONTENT_LAB_BACKEND_ORDER", "on")
    monkeypatch.setitem(base.API_KEYS, "comfyui", _unreachable_url())

    entry = await _job(tmp_path, "local-wan-t2v", "job-9")
    assert entry["status"] == "done", entry.get("error")
    assert entry["served_provider"] == "wan-t2v"
    call = hosted.calls[0]
    assert call["model_id"] == PROVIDERS["wan-t2v"]["models"][0]
    assert call["cost_usd"] == pytest.approx(0.05)
    assert _spent() == pytest.approx(0.05)

    # A retry of the same job reuses its reservation: never billed twice.
    again = await _job(tmp_path, "local-wan-t2v", "job-9")
    assert again["status"] == "done"
    assert _spent() == pytest.approx(0.05)


async def test_hosted_fallback_respects_the_zero_budget_stop(monkeypatch, tmp_path, hosted):
    monkeypatch.setenv("CONTENT_LAB_BACKEND_ORDER", "on")
    monkeypatch.setenv("LAB_GENERATION_DAILY_BUDGET_USD", "0")
    monkeypatch.setitem(base.API_KEYS, "comfyui", _unreachable_url())

    entry = await _job(tmp_path, "local-wan-t2v")
    assert entry["status"] == "error"
    assert entry["error"] == "generation_daily_budget_reached"
    assert hosted.calls == []


async def test_off_by_default_a_job_uses_only_its_own_provider(monkeypatch, tmp_path, hosted):
    monkeypatch.setitem(base.API_KEYS, "comfyui", _unreachable_url())

    entry = await _job(tmp_path, "local-wan-t2v")
    assert entry["status"] == "error"  # same failure as before this change
    assert hosted.calls == []
    assert "backend_attempts" not in entry and "served_provider" not in entry


# --- local first -------------------------------------------------------------

async def _ready(client):
    return True, "ok"


async def test_reachable_studio_serves_the_hosted_request_locally(monkeypatch, tmp_path, hosted):
    monkeypatch.setenv("CONTENT_LAB_BACKEND_ORDER", "on")
    monkeypatch.setenv("LAB_GENERATION_DAILY_BUDGET_USD", "0")  # free either way
    monkeypatch.setattr(backend_order, "local_ready", _ready)
    local = _Recorder("http://comfy.test/view?filename=wan.mp4")
    monkeypatch.setattr(comfyui, "generate", local)

    entry = await _job(tmp_path, "wan-t2v", model_id="wan-video/wan-2.2-t2v-fast", cost_usd=0.05)
    assert entry["status"] == "done", entry.get("error")
    assert entry["served_provider"] == "local-wan-t2v"
    assert entry["billing"] == "local"
    assert local.calls[0]["model_id"] == comfyui.WAN_T2V  # the caller's hosted model id is replaced
    assert local.calls[0]["cost_usd"] == 0.0
    assert local.calls[0]["local_claim_timeout"] == 600
    assert hosted.calls == []


async def test_failed_local_render_falls_forward_then_recovers(monkeypatch, tmp_path, hosted, caplog):
    monkeypatch.setenv("CONTENT_LAB_BACKEND_ORDER", "on")
    monkeypatch.setattr(backend_order, "local_ready", _ready)
    local = _Recorder("http://comfy.test/view?filename=wan.mp4",
                      errors=[RuntimeError("ComfyUI generation timed out")])
    monkeypatch.setattr(comfyui, "generate", local)
    caplog.set_level(logging.INFO, logger="content_lab.backend_order")

    first = await _job(tmp_path, "wan-t2v", "job-1")
    assert first["served_provider"] == "wan-t2v"
    assert first["backend_attempts"][0]["outcome"] == "failed"
    assert first["backend_attempts"][0]["reason"] == "timeout"
    second = await _job(tmp_path, "wan-t2v", "job-2")
    assert second["served_provider"] == "local-wan-t2v"
    assert len(_messages(caplog, "unavailable (timeout)")) == 1
    assert _messages(caplog, "ALERT") == []
    assert _messages(caplog, "is back") == ["backend-order: local engine local-wan-t2v is back"]


async def test_request_the_local_engine_cannot_take_goes_hosted_without_alert(monkeypatch, tmp_path, hosted, caplog):
    monkeypatch.setenv("CONTENT_LAB_BACKEND_ORDER", "on")
    monkeypatch.setattr(backend_order, "local_ready", _ready)
    monkeypatch.setattr(comfyui, "generate", _Recorder(errors=[ValueError("resolution must be 480p or 720p")]))
    caplog.set_level(logging.INFO, logger="content_lab.backend_order")

    entry = await _job(tmp_path, "wan-t2v")
    assert entry["served_provider"] == "wan-t2v"
    assert entry["backend_attempts"][0] == {"provider": "local-wan-t2v", "outcome": "skipped",
                                            "reason": "request_shape"}
    assert _messages(caplog, "ALERT") == []


async def test_alert_fires_only_when_every_backend_failed(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("CONTENT_LAB_BACKEND_ORDER", "on")
    monkeypatch.setitem(base.API_KEYS, "comfyui", _unreachable_url())
    failing = _Recorder(errors=[RuntimeError("Prediction failed (code: E005)")])
    monkeypatch.setattr(replicate, "generate", failing)
    caplog.set_level(logging.INFO, logger="content_lab.backend_order")

    entry = await _job(tmp_path, "wan-i2v-fast", "job-x")
    assert entry["status"] == "error" and "E005" in entry["error"]
    assert len(failing.calls) == 1  # no second paid try
    alerts = _messages(caplog, "ALERT backend-order")
    assert len(alerts) == 1
    assert "job=job-x#0 every backend failed: local-wan-i2v skipped (unreachable), then wan-i2v-fast" in alerts[0]


async def test_studio_down_then_back_returns_jobs_to_local(monkeypatch, tmp_path, hosted, caplog):
    """Recovery drill: a real loopback ComfyUI /queue that is down, then comes back."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Queue(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"queue_running": [], "queue_pending": []}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    monkeypatch.setenv("CONTENT_LAB_BACKEND_ORDER", "on")
    url = _unreachable_url()
    monkeypatch.setitem(base.API_KEYS, "comfyui", url)
    local = _Recorder(url + "/view?filename=wan.mp4")
    monkeypatch.setattr(comfyui, "generate", local)
    caplog.set_level(logging.INFO, logger="content_lab.backend_order")

    down = await _job(tmp_path, "wan-t2v", "job-1")
    assert down["served_provider"] == "wan-t2v" and local.calls == []

    server = ThreadingHTTPServer(("127.0.0.1", int(url.rsplit(":", 1)[1])), Queue)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        back = await _job(tmp_path, "wan-t2v", "job-2")
    finally:
        server.shutdown()
        server.server_close()
    assert back["served_provider"] == "local-wan-t2v" and back["billing"] == "local"
    assert len(local.calls) == 1 and len(hosted.calls) == 1
    assert _messages(caplog, "is back") == ["backend-order: local engine local-wan-t2v is back"]
    assert _messages(caplog, "ALERT") == []


async def test_resumed_hosted_prediction_never_detours_local(monkeypatch, tmp_path, hosted):
    monkeypatch.setenv("CONTENT_LAB_BACKEND_ORDER", "on")

    async def never(client):
        raise AssertionError("a resume must not probe the local engine")

    monkeypatch.setattr(backend_order, "local_ready", never)

    class Checkpoint:
        record = {"state": "submitted", "predictionId": "abc"}

    entry = await _job(tmp_path, "wan-t2v", entry_extra={"_prediction_checkpoint": Checkpoint()})
    assert entry["status"] == "done"
    assert len(hosted.calls) == 1
    assert "backend_attempts" not in entry


# --- refusing a busy engine --------------------------------------------------

async def test_busy_comfyui_is_refused_not_queued(monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_BACKEND_ORDER", "on")
    server = "http://comfy.test:8188"
    monkeypatch.setitem(base.API_KEYS, "comfyui", server)
    depth = {"running": 1, "pending": 1}

    def queue(request):
        assert request.url.path == "/queue"
        return httpx.Response(200, json={"queue_running": [[0, "a"]] * depth["running"],
                                         "queue_pending": [[1, "b"]] * depth["pending"]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(queue)) as client:
        assert await backend_order.local_ready(client) == (False, "busy")
        depth.update(pending=0)
        assert await backend_order.local_ready(client) == (True, "ok")
        comfyui._server_waiting[server] = 1  # one more job of ours waiting for the lock
        assert await backend_order.local_ready(client) == (False, "busy")


def test_worker_readiness_follows_check_ins():
    assert local_worker_queue.worker_ready(2) == (False, "worker_offline")
    local_worker_queue.touch(busy=True)
    assert local_worker_queue.worker_ready(2) == (False, "worker_busy")
    local_worker_queue.touch()
    assert local_worker_queue.worker_ready(2) == (True, "ok")


async def test_worker_queue_full_is_refused(monkeypatch):
    local_worker_queue.touch()
    local_worker_queue.enqueue({"model_id": "m"})
    local_worker_queue.enqueue({"model_id": "m"})
    assert local_worker_queue.worker_ready(2) == (False, "queue_full")


async def test_unclaimed_worker_job_gives_up_after_the_claim_timeout(monkeypatch):
    monkeypatch.setattr(local_worker_queue, "_WAIT_SLICE_SECONDS", 0.01)
    local_worker_queue.touch()
    job = local_worker_queue.enqueue({"model_id": "m"})
    with pytest.raises(local_worker_queue.LocalWorkerError, match="not claimed"):
        await local_worker_queue.wait(job, {}, 60, claim_timeout=0.05)
    assert local_worker_queue.claim() is None  # a late worker cannot take it


def test_busy_check_in_takes_no_job(sync_client, monkeypatch):
    monkeypatch.setenv("LOCAL_WORKER_KEY", KEY)
    resp = sync_client.post("/api/local-worker/claim", params={"busy": "true"},
                            headers={"X-Worker-Key": KEY})
    assert resp.status_code == 200 and resp.json() == {"job": None}
    assert local_worker_queue.worker_ready(2) == (False, "worker_busy")


async def test_worker_skips_claims_while_its_comfyui_is_busy(monkeypatch):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "local_comfy_worker", Path(__file__).resolve().parents[1] / "scripts" / "local_comfy_worker.py")
    worker_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker_mod)
    state = {"running": [[0, "someone-else"]]}

    def comfy(request):
        return httpx.Response(200, json={"queue_running": state["running"], "queue_pending": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(comfy)) as client:
        worker = worker_mod.Worker("http://lab.test", KEY, "http://comfy.test:8188", client)
        assert await worker.busy() == "comfyui_busy"
        state["running"] = []
        assert await worker.busy() is None
        monkeypatch.setenv("LOCAL_WORKER_MAX_LOAD", "-1")
        assert await worker.busy() == "load"


# --- the order file ----------------------------------------------------------

def test_shipped_order_file_is_valid_and_local_first(monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_BACKEND_ORDER", "on")
    assert backend_order.config() is not None
    assert backend_order.chain_for("wan-i2v") == ("local-wan-i2v", "wan-i2v")
    assert backend_order.chain_for("local-wan-i2v") == ("local-wan-i2v", "wan-i2v-fast")
    assert backend_order.chain_for("h3-highspeed") == ("local-h3-vertical", "h3-highspeed")
    assert backend_order.chain_for("hailuo") == ("hailuo",)
    assert backend_order.chain_for("flux-image") == ("flux-image",)
    assert backend_order.config()["vision"]["authority"] == "hint"


def _order_file(tmp_path, monkeypatch, **overrides):
    data = json.loads(backend_order.DEFAULT_PATH.read_text())
    data.update(overrides)
    path = tmp_path / "order.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv("CONTENT_LAB_BACKEND_ORDER", str(path))
    backend_order.reset()
    return data


@pytest.mark.parametrize("chains", [
    {"video-t2v": [["wan-t2v", "local-wan-t2v"]]},           # local after paid never runs
    {"video-i2v": [["local-wan-i2v", "wan-i2v", "hailuo"]]},  # two paid providers
    {"video-t2v": [["local-wan-t2v", "no-such-engine"]]},
])
def test_bad_order_file_fails_closed_to_each_jobs_own_provider(tmp_path, monkeypatch, caplog, chains):
    _order_file(tmp_path, monkeypatch, chains=chains)
    assert backend_order.config() is None
    assert backend_order.chain_for("wan-t2v") == ("wan-t2v",)
    assert len(_messages(caplog, "ALERT backend-order")) == 1


def test_vision_verdict_authority_needs_a_passing_agreement_run(tmp_path, monkeypatch, caplog):
    data = json.loads(backend_order.DEFAULT_PATH.read_text())
    studio = dict(data["vision"]["studio"], authority="verdict")
    _order_file(tmp_path, monkeypatch, vision={"studio": studio})
    assert backend_order.config()["vision"]["authority"] == "hint"
    assert _messages(caplog, "without a passing agreement record")

    passed = dict(studio, agreement={"model": studio["fast_model"], "rate": 0.97, "frames": 240})
    _order_file(tmp_path, monkeypatch, vision={"studio": passed})
    assert backend_order.config()["vision"]["authority"] == "verdict"
    low = dict(studio, agreement={"model": studio["fast_model"], "rate": 0.90, "frames": 240})
    _order_file(tmp_path, monkeypatch, vision={"studio": low})
    assert backend_order.config()["vision"]["authority"] == "hint"


# --- Studio vision -----------------------------------------------------------

def _vision_fake(calls, studio_verdict="clean", studio_error=None):
    def request(samples, deadline, *, url, model, provider, key=""):
        calls.append((provider, model))
        if provider == "studio":
            if studio_error:
                raise studio_error
            return {"verdict": studio_verdict, "reason": "local evidence",
                    "model": {"name": model, "provider": provider, "fallback": False, "fallbackReason": None}}
        return {"verdict": "clean", "reason": "hosted evidence",
                "model": {"name": model, "provider": provider, "fallback": False, "fallbackReason": None}}
    return request


def test_studio_vision_is_a_hint_beside_the_hosted_verdict(monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_BACKEND_ORDER", "on")
    monkeypatch.setenv("CONTENT_LAB_VISION_STUDIO_URL", "http://127.0.0.1:11434/v1/chat/completions")
    monkeypatch.delenv("CONTENT_LAB_VISION_API_KEY", raising=False)
    monkeypatch.setattr(gate, "_studio_resident", lambda url, model, deadline: False)
    calls = []
    monkeypatch.setattr(gate, "_vision_request", _vision_fake(calls, studio_verdict="text"))

    result = gate._vision(["ZmFrZQ=="], time.monotonic() + 5)
    assert calls[0] == ("studio", "mimo-v2.6:9b-nvfp4")
    assert ("studio", "ornith-1.5:122b-nvfp4") not in calls  # decided; ornith never asked
    assert result["model"]["provider"] != "studio"  # the hosted fallback decided
    hint = result["model"]["studioHint"]
    assert hint["authority"] == "hint"
    assert hint["fast"] == {"model": "mimo-v2.6:9b-nvfp4", "verdict": "text", "reason": "local evidence"}


def test_hard_model_is_asked_only_when_already_loaded(monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_BACKEND_ORDER", "on")
    monkeypatch.setenv("CONTENT_LAB_VISION_STUDIO_URL", "http://127.0.0.1:11434/v1/chat/completions")
    monkeypatch.delenv("CONTENT_LAB_VISION_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(gate, "_vision_request", _vision_fake(calls, studio_verdict="unavailable"))

    monkeypatch.setattr(gate, "_studio_resident", lambda url, model, deadline: False)
    result = gate._vision(["ZmFrZQ=="], time.monotonic() + 5)
    assert result["model"]["studioHint"]["hard"] == {"model": "ornith-1.5:122b-nvfp4", "skipped": "not_loaded"}

    calls.clear()
    monkeypatch.setattr(gate, "_studio_resident", lambda url, model, deadline: True)
    gate._vision(["ZmFrZQ=="], time.monotonic() + 5)
    assert ("studio", "ornith-1.5:122b-nvfp4") in calls


def test_studio_down_vision_still_reaches_the_hosted_fallback(monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_BACKEND_ORDER", "on")
    monkeypatch.setenv("CONTENT_LAB_VISION_STUDIO_URL", _unreachable_url() + "/v1/chat/completions")
    monkeypatch.delenv("CONTENT_LAB_VISION_API_KEY", raising=False)
    monkeypatch.setattr(gate, "_studio_resident", lambda url, model, deadline: False)
    calls = []
    monkeypatch.setattr(gate, "_vision_request", _vision_fake(
        calls, studio_error=httpx.ConnectError("refused")))

    result = gate._vision(["ZmFrZQ=="], time.monotonic() + 5)
    assert result["verdict"] == "clean" and result["model"]["provider"] != "studio"
    assert result["model"]["studioHint"]["fast"]["error"] == "vision_transport_unavailable"


def test_studio_vision_verdict_after_a_passing_agreement_run(tmp_path, monkeypatch):
    data = json.loads(backend_order.DEFAULT_PATH.read_text())
    studio = dict(data["vision"]["studio"], authority="verdict",
                  agreement={"model": data["vision"]["studio"]["fast_model"], "rate": 0.97, "frames": 240})
    _order_file(tmp_path, monkeypatch, vision={"studio": studio})
    monkeypatch.setenv("CONTENT_LAB_VISION_STUDIO_URL", "http://127.0.0.1:11434/v1/chat/completions")
    monkeypatch.delenv("CONTENT_LAB_VISION_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(gate, "_vision_request", _vision_fake(calls, studio_verdict="clean"))

    result = gate._vision(["ZmFrZQ=="], time.monotonic() + 5)
    assert result["model"]["provider"] == "studio" and result["model"]["fallback"] is True
    assert calls == [("studio", "mimo-v2.6:9b-nvfp4")]  # no paid fallback call


def test_vision_without_studio_config_is_unchanged(monkeypatch):
    monkeypatch.delenv("CONTENT_LAB_VISION_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(gate, "_vision_request", _vision_fake(calls))
    result = gate._vision(["ZmFrZQ=="], time.monotonic() + 5)
    assert "studioHint" not in result["model"]
    assert all(provider != "studio" for provider, _ in calls)


def test_stuck_window_covers_the_slowest_engine_in_the_chain(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from routers import video as video_router

    monkeypatch.setattr(video_router, "_save_jobs", lambda project: True)
    created = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()

    def job():
        return {"provider": "wan-t2v", "created_at": created, "videos": [{"status": "polling"}]}

    off = job()
    video_router._sweep_stuck_entries(off)
    assert off["videos"][0]["status"] == "error"  # hosted-only: 20-minute window, as before

    monkeypatch.setenv("CONTENT_LAB_BACKEND_ORDER", "on")
    monkeypatch.setitem(base.API_KEYS, "comfyui", None)
    hosted_only = job()
    video_router._sweep_stuck_entries(hosted_only)
    assert hosted_only["videos"][0]["status"] == "error"  # no local engine: window unchanged

    monkeypatch.setitem(base.API_KEYS, "comfyui", "worker-queue")
    on = job()
    video_router._sweep_stuck_entries(on)
    assert on["videos"][0]["status"] == "polling"  # may be rendering on the local engine
