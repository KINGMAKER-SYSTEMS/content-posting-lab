"""Original-attempt receipts prevent repeated paid LaMa creates."""

import asyncio
import json

import httpx
import pytest

from providers import replicate
from routers.recreate import _remove_text_with_retry
from services import generation_budget


IMAGE = "data:image/png;base64,fixture"
OUTPUT = "https://fixture.example/clean.png"


def _provider(monkeypatch):
    monkeypatch.setitem(replicate.API_KEYS, "replicate", "fixture-token")
    monkeypatch.setattr(replicate, "_generate_text_mask", lambda _: "fixture-mask")
    monkeypatch.setattr(generation_budget, "per_gen_cost_usd_by_model", lambda _: 0.405)
    debits = []
    monkeypatch.setattr(generation_budget, "debit_generation_spend_at",
                        lambda *args: debits.append(args[-1]) or True)
    return debits


def test_ambiguous_create_never_posts_or_debits_again(monkeypatch, tmp_path):
    debits = _provider(monkeypatch)
    posts = []

    class Client:
        async def post(self, *args, **kwargs):
            posts.append(kwargs)
            raise httpx.ReadTimeout("response lost")

    receipt = tmp_path / "first.json"
    for _ in range(2):
        with pytest.raises((httpx.ReadTimeout, RuntimeError)):
            asyncio.run(_remove_text_with_retry(IMAGE, Client(), "first frame", receipt))
    assert len(posts) == len(debits) == 1
    assert json.loads(receipt.read_text())["state"] == "submission_intent"


def test_known_prediction_repolls_original_id_without_new_create(monkeypatch, tmp_path):
    debits = _provider(monkeypatch)
    posts = []
    polls = []

    class Response:
        status_code = 201

        def json(self):
            return {"id": "prediction-original"}

    class Client:
        async def post(self, *args, **kwargs):
            posts.append(kwargs)
            return Response()

    async def poll(*args, **kwargs):
        polls.append(args[2])
        if len(polls) == 1:
            raise httpx.ReadTimeout("poll lost")
        return OUTPUT

    monkeypatch.setattr(replicate, "_poll_prediction", poll)
    receipt = tmp_path / "last.json"
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(_remove_text_with_retry(IMAGE, Client(), "last frame", receipt))
    assert asyncio.run(_remove_text_with_retry(IMAGE, Client(), "last frame", receipt)) == OUTPUT
    assert asyncio.run(_remove_text_with_retry(IMAGE, Client(), "last frame", receipt)) == OUTPUT
    assert posts and len(posts) == len(debits) == 1
    assert polls == ["prediction-original", "prediction-original"]
    assert json.loads(receipt.read_text())["state"] == "complete"


def test_changed_frame_cannot_reuse_original_prediction(monkeypatch, tmp_path):
    _provider(monkeypatch)
    class Client:
        async def post(self, *args, **kwargs):
            raise httpx.ReadTimeout("lost")
    receipt = tmp_path / "first.json"
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(_remove_text_with_retry(IMAGE, Client(), "first frame", receipt))
    with pytest.raises(RuntimeError, match="input mismatch"):
        asyncio.run(_remove_text_with_retry(IMAGE + "changed", Client(), "first frame", receipt))

def test_second_start_and_delete_cannot_overlap_owned_job(monkeypatch, tmp_path):
    from httpx import ASGITransport, AsyncClient
    from app import app
    from routers import recreate
    from services import generation_budget

    monkeypatch.setattr(generation_budget, "jobs_store_path", lambda: tmp_path / "budget.json")
    monkeypatch.setattr(recreate, "get_project_recreate_dir", lambda project: tmp_path / "recreate")
    entered = asyncio.Event()
    release = asyncio.Event()
    runs = []

    async def owned(*args):
        runs.append(args)
        (tmp_path / "recreate" / "same-job").mkdir(parents=True, exist_ok=True)
        entered.set()
        await release.wait()

    monkeypatch.setattr(recreate, "_run_pipeline_owned", owned)
    errors = []

    async def send(*args):
        errors.append(args)

    monkeypatch.setattr(recreate, "_send", send)

    async def exercise():
        first = asyncio.create_task(recreate._run_pipeline("same-job", "url", "fixture"))
        await entered.wait()
        await recreate._run_pipeline("same-job", "url", "fixture")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.delete("/api/recreate/jobs/same-job", params={"project": "fixture"})
        release.set()
        await first
        return response

    response = asyncio.run(exercise())
    assert response.status_code == 409
    assert len(runs) == 1
    assert errors[0][1] == "error"
