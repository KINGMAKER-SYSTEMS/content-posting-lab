"""Local ComfyUI engines: workflow shape, server protocol, and free metering."""

import asyncio
import base64
import io
import json

import httpx
import pytest
from PIL import Image

from providers import PROVIDERS, comfyui
from routers import video as video_router
from services import generation_budget

SERVER = "http://comfy.test:8188"


@pytest.fixture(autouse=True)
def fresh_comfy_server_state(monkeypatch):
    monkeypatch.setattr(comfyui, "_FREE_SETTLE_SECONDS", 0)
    comfyui._server_gates.clear()
    comfyui._server_model.clear()
    yield
    comfyui._server_gates.clear()
    comfyui._server_model.clear()


def _image_data_uri(width: int, height: int) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _t2v(**params) -> dict:
    return comfyui.build_workflow("a calm sea", {"model_id": comfyui.WAN_T2V, **params})


def test_t2v_fast_uses_lightning_lora_and_vertical_480p():
    graph = _t2v(aspect_ratio="9:16", resolution="480p")
    assert graph["latent"]["inputs"] == {"width": 480, "height": 832, "length": 81, "batch_size": 1}
    assert graph["shift_high"]["inputs"]["model"] == ["lora_high", 0]
    high, low = graph["sample_high"]["inputs"], graph["sample_low"]["inputs"]
    assert (high["steps"], high["cfg"], high["end_at_step"]) == (4, 1.0, 2)
    assert (low["start_at_step"], low["end_at_step"], low["add_noise"]) == (2, 4, "disable")
    assert low["latent_image"] == ["sample_high", 0]
    assert graph["positive"]["inputs"]["text"] == "a calm sea"
    assert graph["save"]["class_type"] == "SaveVideo"


def test_t2v_quality_mode_drops_lora_and_honors_steps():
    graph = _t2v(aspect_ratio="16:9", resolution="720p", go_fast=False, sample_steps=30)
    assert "lora_high" not in graph and "lora_low" not in graph
    assert graph["shift_high"]["inputs"]["model"] == ["unet_high", 0]
    assert graph["latent"]["inputs"]["width"] == 1280
    assert graph["sample_high"]["inputs"]["steps"] == 30
    assert graph["sample_high"]["inputs"]["cfg"] == 3.5
    assert graph["sample_low"]["inputs"]["start_at_step"] == 15


def test_frames_snap_to_wan_latent_grid_and_custom_negative_wins():
    graph = _t2v(num_frames=100, negative_prompt="  text overlays ")
    assert graph["latent"]["inputs"]["length"] == 97
    assert graph["negative"]["inputs"]["text"] == "text overlays"
    assert _t2v()["negative"]["inputs"]["text"] == comfyui._WAN_NEGATIVE


@pytest.mark.parametrize("params", [
    {"resolution": "1080p"},
    {"aspect_ratio": "4:3"},
    {"go_fast": "yes"},
    {"num_frames": "81"},
])
def test_t2v_rejects_unsupported_requests(params):
    with pytest.raises(ValueError):
        _t2v(**params)


def test_unknown_model_has_no_workflow():
    with pytest.raises(ValueError):
        comfyui.build_workflow("x", {"model_id": "comfyui/nope"})


def test_i2v_requires_image_and_keeps_its_shape():
    with pytest.raises(ValueError, match="requires an image"):
        comfyui.build_workflow("x", {"model_id": comfyui.WAN_I2V})
    graph = comfyui.build_workflow("x", {
        "model_id": comfyui.WAN_I2V, "resolution": "480p",
        "image_data_uri": _image_data_uri(900, 1600), "image_name": "start.png"})
    latent = graph["latent"]
    assert latent["class_type"] == "WanImageToVideo"
    assert (latent["inputs"]["width"], latent["inputs"]["height"]) == (480, 848)
    assert graph["start_image"]["inputs"]["image"] == "start.png"
    # The image conditioning, not the bare prompt, feeds both samplers.
    assert graph["sample_high"]["inputs"]["positive"] == ["latent", 0]
    assert graph["sample_high"]["inputs"]["latent_image"] == ["latent", 2]


def test_i2v_rejects_unreadable_image():
    with pytest.raises(ValueError):
        comfyui.build_workflow("x", {
            "model_id": comfyui.WAN_I2V,
            "image_data_uri": "data:image/png;base64," + base64.b64encode(b"not an image").decode()})


class _FakeComfy:
    """Minimal ComfyUI HTTP surface: upload, prompt, history."""

    def __init__(self, history: dict):
        self.history = history
        self.requests: list[httpx.Request] = []
        self.polls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/upload/image":
            return httpx.Response(200, json={"name": "uploaded.png", "subfolder": "", "type": "input"})
        if path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "p-1", "node_errors": {}})
        if path == "/history/p-1":
            self.polls += 1
            return httpx.Response(200, json={} if self.polls == 1 else {"p-1": self.history})
        return httpx.Response(404)


_DONE = {
    "status": {"status_str": "success", "completed": True, "messages": []},
    "outputs": {"save": {"images": [
        {"filename": "wan_00001_.mp4", "subfolder": "content-lab", "type": "output"}], "animated": [True]}},
}


async def _run(fake: _FakeComfy, monkeypatch, params: dict) -> tuple[str, dict]:
    monkeypatch.setitem(comfyui.API_KEYS, "comfyui", SERVER + "/")
    monkeypatch.setattr(comfyui, "_POLL_SECONDS", 0)
    entry: dict = {}
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
        url = await comfyui.generate("a calm sea", {"entry": entry, **params}, client)
    return url, entry


async def test_generate_submits_polls_and_returns_view_url(monkeypatch):
    fake = _FakeComfy(_DONE)
    url, entry = await _run(fake, monkeypatch, {"model_id": comfyui.WAN_T2V, "aspect_ratio": "9:16"})
    assert url == f"{SERVER}/view?filename=wan_00001_.mp4&subfolder=content-lab&type=output"
    assert entry == {"provider_request_id": "p-1", "status": "polling"}
    assert fake.polls == 2
    submitted = json.loads(next(r for r in fake.requests if r.url.path == "/prompt").content)
    assert submitted["prompt"]["latent"]["inputs"]["height"] == 832


async def test_generate_uploads_start_image_for_i2v(monkeypatch):
    fake = _FakeComfy(_DONE)
    await _run(fake, monkeypatch, {
        "model_id": comfyui.WAN_I2V, "image_data_uri": _image_data_uri(1600, 900)})
    paths = [r.url.path for r in fake.requests]
    assert paths.index("/upload/image") < paths.index("/prompt")
    submitted = json.loads(next(r for r in fake.requests if r.url.path == "/prompt").content)
    assert submitted["prompt"]["start_image"]["inputs"]["image"] == "uploaded.png"
    assert submitted["prompt"]["latent"]["inputs"]["width"] > submitted["prompt"]["latent"]["inputs"]["height"]


async def test_generate_surfaces_execution_error(monkeypatch):
    failed = {"status": {"status_str": "error", "completed": False, "messages": [
        ["execution_error", {"node_type": "UNETLoader", "exception_message": "file missing"}]]}, "outputs": {}}
    with pytest.raises(RuntimeError, match="UNETLoader: file missing"):
        await _run(_FakeComfy(failed), monkeypatch, {"model_id": comfyui.WAN_T2V})


async def test_generate_surfaces_rejected_workflow(monkeypatch):
    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "invalid prompt"}})

    monkeypatch.setitem(comfyui.API_KEYS, "comfyui", SERVER)
    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
        with pytest.raises(RuntimeError, match="rejected the workflow"):
            await comfyui.generate("x", {"entry": {}, "model_id": comfyui.WAN_T2V}, client)


async def test_generate_requires_configured_server(monkeypatch):
    monkeypatch.setitem(comfyui.API_KEYS, "comfyui", None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(_FakeComfy(_DONE))) as client:
        with pytest.raises(RuntimeError, match="COMFYUI_URL"):
            await comfyui.generate("x", {"entry": {}, "model_id": comfyui.WAN_T2V}, client)


def test_only_declared_local_providers_meter_as_free():
    assert generation_budget.is_local_provider("local-wan-t2v")
    assert generation_budget.per_gen_cost_usd("local-wan-t2v", 5, resolution="720p") == 0.0
    assert generation_budget.per_gen_cost_usd("local-wan-i2v", 5) == 0.0
    assert not generation_budget.is_local_provider("wan-t2v")
    assert not generation_budget.is_local_provider("no-such-provider")
    assert generation_budget.per_gen_cost_usd("wan-t2v", 5) > 0
    for provider_id, info in PROVIDERS.items():
        assert (info.get("billing") == "local") == (info["key_id"] == "comfyui"), provider_id


def test_local_providers_listed_only_when_server_configured(sync_client, monkeypatch):
    monkeypatch.setitem(video_router.API_KEYS, "comfyui", None)
    listed = {p["id"] for p in sync_client.get("/api/video/providers").json()}
    assert "local-wan-t2v" not in listed
    monkeypatch.setitem(video_router.API_KEYS, "comfyui", SERVER)
    listed = {p["id"] for p in sync_client.get("/api/video/providers").json()}
    assert {"local-wan-t2v", "local-wan-i2v"} <= listed
    schemas = sync_client.get("/api/video/provider-schemas").json()
    assert schemas["local-wan-i2v"]["image_required"] is True


def test_local_generate_runs_without_debit_even_at_zero_budget(sync_client, monkeypatch):
    """The paid emergency stop (budget 0) must not block or charge local work."""
    monkeypatch.setitem(video_router.API_KEYS, "comfyui", SERVER)
    monkeypatch.setenv("LAB_GENERATION_DAILY_BUDGET_USD", "0")
    debits: list = []
    monkeypatch.setattr(generation_budget, "debit_generation_spend_at",
                        lambda *args, **kwargs: debits.append(args) or True)
    seen: dict = {}

    async def fake_generate_one(job_id, index, provider, prompt, aspect_ratio, resolution,
                                duration, image_data_uri, jobs, output_dir, url_prefix,
                                on_complete=None, **extra):
        seen.update(provider=provider, cost_usd=extra.get("cost_usd"))
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
    for _ in range(100):
        if sync_client.get(f"/api/video/jobs/{job_id}").json()["videos"][0]["status"] == "done":
            break
    assert seen == {"provider": "local-wan-t2v", "cost_usd": 0.0}
    assert debits == []


def test_local_i2v_generate_without_image_is_refused_before_a_job_exists(sync_client, monkeypatch):
    monkeypatch.setitem(video_router.API_KEYS, "comfyui", SERVER)
    response = sync_client.post("/api/video/generate", data={
        "prompt": "x", "provider": "local-wan-i2v", "project": "video-suite"})
    assert response.status_code == 400
    assert "requires an image" in response.json()["detail"]
    assert sync_client.get("/api/video/jobs", params={"project": "video-suite"}).json() == []


def test_local_jobs_get_a_longer_stuck_window(monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(video_router, "_save_jobs", lambda project: True)
    created = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
    local = {"provider": "local-wan-t2v", "created_at": created, "videos": [{"status": "polling"}]}
    paid = {"provider": "wan-t2v", "created_at": created, "videos": [{"status": "polling"}]}
    video_router._sweep_stuck_entries(local)
    video_router._sweep_stuck_entries(paid)
    assert local["videos"][0]["status"] == "polling"
    assert paid["videos"][0]["status"] == "error"


def _h3(**params) -> dict:
    return comfyui.build_workflow("a calm sea", {"model_id": comfyui.H3_FL2VA, **params})


def test_h3_text_to_video_fast_defaults():
    graph = _h3(aspect_ratio="9:16", duration=5)
    node = graph["h3"]["inputs"]
    assert (node["width"], node["height"], node["length"]) == (768, 1344, 124)
    assert node["prompt"] == "a calm sea"
    assert "first_frame" not in node and "last_frame" not in node
    assert graph["sigmas"]["inputs"]["steps"] == 8
    assert graph["guider"]["inputs"]["model"] == ["lora", 0]
    assert graph["clip"]["inputs"]["type"] == "minimax"
    # Picture only: the lab posts with campaign sounds.
    assert "audio" not in graph["video"]["inputs"]


@pytest.mark.parametrize("duration,frames", [(2, 56), (4, 107), (10, 243), (15, 362)])
def test_h3_frame_counts_sit_on_the_17n_plus_5_grid(duration, frames):
    length = _h3(duration=duration)["h3"]["inputs"]["length"]
    assert length == frames and length % 17 == 5


def test_h3_quality_mode_and_frames_follow_the_request():
    graph = _h3(aspect_ratio="16:9", go_fast=False, sample_steps=30,
                image_data_uri=_image_data_uri(900, 1600), image_name="first.png",
                last_image_data_uri=_image_data_uri(900, 1600), last_image_name="last.png")
    assert "lora" not in graph
    assert graph["sigmas"]["inputs"]["steps"] == 30
    node = graph["h3"]["inputs"]
    # A start image sets the shape; the aspect-ratio control does not.
    assert node["width"] < node["height"] and node["width"] % 32 == 0 and node["height"] % 32 == 0
    assert graph["first_frame"]["inputs"]["image"] == "first.png"
    assert graph["last_frame"]["inputs"]["image"] == "last.png"
    assert node["first_frame"] == ["first_frame", 0] and node["last_frame"] == ["last_frame", 0]


def test_h3_rejects_unsupported_aspect_ratio():
    with pytest.raises(ValueError):
        _h3(aspect_ratio="4:3")


async def test_generate_uploads_both_h3_frames(monkeypatch):
    fake = _FakeComfy(_DONE)
    await _run(fake, monkeypatch, {
        "model_id": comfyui.H3_FL2VA, "image_data_uri": _image_data_uri(900, 1600),
        "last_image_data_uri": _image_data_uri(900, 1600)})
    paths = [r.url.path for r in fake.requests]
    assert paths.count("/upload/image") == 2
    submitted = json.loads(next(r for r in fake.requests if r.url.path == "/prompt").content)["prompt"]
    assert submitted["first_frame"]["inputs"]["image"] == "uploaded.png"
    assert submitted["last_frame"]["inputs"]["image"] == "uploaded.png"


def test_local_h3_is_listed_free_and_offers_a_last_frame(sync_client, monkeypatch):
    monkeypatch.setitem(video_router.API_KEYS, "comfyui", SERVER)
    assert "local-h3" in {p["id"] for p in sync_client.get("/api/video/providers").json()}
    assert generation_budget.per_gen_cost_usd("local-h3", 10) == 0.0
    assert sync_client.get("/api/video/provider-schemas").json()["local-h3"]["last_image_supported"] is True


def test_h3_landscape_variant_always_renders_16x9_for_the_crop_pipeline():
    from providers import base

    assert "local-h3" in base.FORCE_LANDSCAPE and "local-h3-vertical" not in base.FORCE_LANDSCAPE
    # The set is derived from the catalog flag, not a separate list of ids.
    assert base.FORCE_LANDSCAPE == {pid for pid, info in PROVIDERS.items() if info.get("landscape_master") is True}
    assert base.FORCE_LANDSCAPE == {"hailuo", "pruna-pvideo", "local-h3"}
    assert PROVIDERS["local-h3"]["variant"] == "landscape"
    for params in ({"aspect_ratio": "9:16"}, {"image_data_uri": _image_data_uri(900, 1600)}):
        node = _h3(variant="landscape", **params)["h3"]["inputs"]
        assert (node["width"], node["height"]) == (1344, 768)


def test_h3_vertical_variant_is_native_9x16_unless_an_image_sets_the_shape():
    assert PROVIDERS["local-h3-vertical"]["variant"] == "vertical"
    node = _h3(variant="vertical", aspect_ratio="16:9")["h3"]["inputs"]
    assert (node["width"], node["height"]) == (768, 1344)
    node = _h3(variant="vertical", image_data_uri=_image_data_uri(1600, 900))["h3"]["inputs"]
    assert node["width"] > node["height"]


def test_h3_schemas_mirror_hailuo_and_the_vertical_engine(sync_client):
    schemas = sync_client.get("/api/video/provider-schemas").json()
    assert schemas["local-h3"]["crop_mode"]["default"] == "both"
    assert "crop_mode" not in schemas["local-h3-vertical"]
    assert schemas["local-h3-vertical"]["_advanced"]["go_fast"]["default"] is True


async def test_generate_fails_fast_when_the_server_drops_the_job(monkeypatch):
    """A cancelled prompt or a restarted server must not hang until the timeout."""
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "p-1"})
        if request.url.path == "/queue":
            return httpx.Response(200, json={"queue_running": [], "queue_pending": [[3, "other"]]})
        return httpx.Response(200, json={})  # history never learns about p-1

    monkeypatch.setitem(comfyui.API_KEYS, "comfyui", SERVER)
    monkeypatch.setattr(comfyui, "_POLL_SECONDS", 0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="no longer has this job"):
            await comfyui.generate("x", {"entry": {}, "model_id": comfyui.WAN_T2V}, client)
    assert requests.count("/queue") == 1


async def test_generate_keeps_waiting_while_the_job_is_still_queued(monkeypatch):
    polls = {"history": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "p-1"})
        if request.url.path == "/queue":
            return httpx.Response(200, json={"queue_running": [], "queue_pending": [[3, "p-1", {}]]})
        polls["history"] += 1
        return httpx.Response(200, json={"p-1": _DONE} if polls["history"] > 12 else {})

    monkeypatch.setitem(comfyui.API_KEYS, "comfyui", SERVER)
    monkeypatch.setattr(comfyui, "_POLL_SECONDS", 0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        url = await comfyui.generate("x", {"entry": {}, "model_id": comfyui.WAN_T2V}, client)
    assert url.startswith(f"{SERVER}/view?")


async def test_models_are_released_only_when_the_next_job_needs_different_ones(monkeypatch):
    fake = _FakeComfy(_DONE)
    monkeypatch.setitem(comfyui.API_KEYS, "comfyui", SERVER)
    monkeypatch.setattr(comfyui, "_POLL_SECONDS", 0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
        for model_id in (comfyui.WAN_T2V, comfyui.WAN_T2V, comfyui.H3_FL2VA):
            fake.polls = 0
            await comfyui.generate("x", {"entry": {}, "model_id": model_id}, client)
    paths = [r.url.path for r in fake.requests if r.url.path in ("/free", "/prompt")]
    # first job (unknown server state), then again only at the switch to H3
    assert paths == ["/free", "/prompt", "/prompt", "/free", "/prompt"]
    freed = json.loads(next(r for r in fake.requests if r.url.path == "/free").content)
    assert freed == {"unload_models": True, "free_memory": True}


async def test_jobs_for_one_server_render_one_at_a_time(monkeypatch):
    events: list[str] = []
    state = {"n": 0, "polls": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/prompt":
            state["n"] += 1
            pid = f"p-{state['n']}"
            events.append(f"start {pid}")
            return httpx.Response(200, json={"prompt_id": pid})
        if path.startswith("/history/"):
            pid = path.rsplit("/", 1)[1]
            state["polls"][pid] = state["polls"].get(pid, 0) + 1
            if state["polls"][pid] < 3:
                return httpx.Response(200, json={})
            if f"end {pid}" not in events:
                events.append(f"end {pid}")
            return httpx.Response(200, json={pid: _DONE})
        return httpx.Response(404)

    monkeypatch.setitem(comfyui.API_KEYS, "comfyui", SERVER)
    monkeypatch.setattr(comfyui, "_POLL_SECONDS", 0.001)
    entries = [{}, {}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await asyncio.gather(*(
            comfyui.generate("x", {"entry": entry, "model_id": comfyui.WAN_T2V}, client)
            for entry in entries))
    assert events == ["start p-1", "end p-1", "start p-2", "end p-2"]


def test_local_h3_duration_ranges_carry_a_step_for_the_worker_catalog():
    # The posting Worker's catalog check needs min/max/step/default on a range.
    for provider in ("local-h3", "local-h3-vertical"):
        duration = video_router.PROVIDER_SCHEMAS[provider]["duration"]
        assert {"min", "max", "step", "default"} <= set(duration)
