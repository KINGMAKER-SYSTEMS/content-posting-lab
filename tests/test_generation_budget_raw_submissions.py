"""Raw generation calls create new predictions, even for identical inputs."""

import asyncio
import json

import pytest

from providers import replicate
from services import abn_factory, generation_budget


@pytest.mark.parametrize("emergency_stop", [False, True])
def test_repeated_lama_input_cannot_bypass_budget(monkeypatch, tmp_path, emergency_stop):
    store = tmp_path / "jobs.json"
    monkeypatch.setenv("LAB_GENERATION_DAILY_BUDGET_USD", "0.04")
    monkeypatch.setattr(generation_budget, "jobs_store_path", lambda: store)
    monkeypatch.setitem(replicate.API_KEYS, "replicate", "fixture-token")
    monkeypatch.setattr(replicate, "_generate_text_mask", lambda _: "fixture-mask")
    posts = []

    class Response:
        status_code = 201

        def json(self):
            return {"id": f"prediction-{len(posts)}"}

    class Client:
        async def post(self, *args, **kwargs):
            posts.append(kwargs["json"])
            return Response()

    async def poll(*args, **kwargs):
        return "https://fixture.example/output.png"

    monkeypatch.setattr(replicate, "_poll_prediction", poll)
    image = "data:image/png;base64,same-image"
    asyncio.run(replicate.remove_text(image, Client()))
    if emergency_stop:
        monkeypatch.setenv("LAB_GENERATION_DAILY_BUDGET_USD", "0")
    else:
        asyncio.run(replicate.remove_text(image, Client()))
    with pytest.raises(RuntimeError, match="generation_daily_budget_reached"):
        asyncio.run(replicate.remove_text(image, Client()))
    expected_posts = 1 if emergency_stop else 2
    assert len(posts) == expected_posts
    assert json.loads(store.read_text())["generationBudget"]["spentUsd"] == expected_posts * 0.02


@pytest.mark.parametrize("lane,cost", [("flux", 0.003), ("wan", 0.30)])
@pytest.mark.parametrize("emergency_stop", [False, True])
def test_repeated_abn_input_cannot_bypass_budget(monkeypatch, tmp_path, lane, cost, emergency_stop):
    store = tmp_path / "jobs.json"
    monkeypatch.setenv("REPLICATE_API_TOKEN", "fixture-token")
    monkeypatch.setenv("LAB_GENERATION_DAILY_BUDGET_USD", str(cost * 2))
    monkeypatch.setattr(generation_budget, "jobs_store_path", lambda: store)
    monkeypatch.setattr(abn_factory.time, "sleep", lambda _: None)
    posts = []

    def urlopen(request, **kwargs):
        if request.data is not None:
            posts.append(request)
            body = {"output": ["https://fixture.example/still.png"],
                    "urls": {"get": "https://fixture.example/poll"}}
        else:
            body = {"status": "succeeded", "output": ["https://fixture.example/clip.mp4"]}
        from io import BytesIO

        return BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(abn_factory.urllib.request, "urlopen", urlopen)
    dest = tmp_path / "clip.mp4"
    monkeypatch.setattr(abn_factory, "_cross_scratch_path", lambda _: dest)
    monkeypatch.setattr(abn_factory, "_download", lambda *a, **k: dest.write_bytes(b"clip"))
    monkeypatch.setattr(abn_factory, "_asset_url", lambda _: "https://fixture.example/asset")

    def generate():
        if lane == "flux":
            return abn_factory._flux_sync("same prompt")
        return abn_factory._wan_i2v_sync("https://fixture.example/still.png", "same-name")

    assert generate() is not None
    if emergency_stop:
        monkeypatch.setenv("LAB_GENERATION_DAILY_BUDGET_USD", "0")
    else:
        assert generate() is not None
    assert generate() is None
    expected_posts = 1 if emergency_stop else 2
    assert len(posts) == expected_posts
    assert json.loads(store.read_text())["generationBudget"]["spentUsd"] == expected_posts * cost
