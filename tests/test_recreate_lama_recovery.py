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

@pytest.mark.parametrize("known_id", [False, True])
def test_delete_refuses_unresolved_paid_attempt_and_replay_is_original(monkeypatch, tmp_path, known_id):
    from fastapi import HTTPException
    from routers import recreate

    debits = _provider(monkeypatch)
    job_dir = tmp_path / "recreate" / "same-job"
    job_dir.mkdir(parents=True)
    monkeypatch.setattr(recreate, "get_project_recreate_dir", lambda _: job_dir.parent)
    posts = []
    polls = []

    class Response:
        status_code = 201

        def json(self):
            return {"id": "original-prediction"}

    class Client:
        async def post(self, *args, **kwargs):
            posts.append(kwargs)
            if not known_id:
                raise httpx.ReadTimeout("create reply lost")
            return Response()

    async def poll(*args, **kwargs):
        polls.append(args[2])
        raise httpx.ReadTimeout("poll reply lost")

    monkeypatch.setattr(replicate, "_poll_prediction", poll)
    receipt = job_dir / "first_lama_receipt.json"
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(_remove_text_with_retry(IMAGE, Client(), "first frame", receipt))
    with pytest.raises(HTTPException) as denied:
        asyncio.run(recreate.delete_recreate_job("same-job", project="fixture"))
    assert denied.value.status_code == 409
    assert receipt.exists()
    with pytest.raises((RuntimeError, httpx.ReadTimeout)):
        asyncio.run(_remove_text_with_retry(IMAGE, Client(), "first frame", receipt))
    assert len(posts) == len(debits) == 1
    assert polls == (["original-prediction", "original-prediction"] if known_id else [])


def test_budget_refusal_retries_same_debit_before_submission_fence(monkeypatch, tmp_path):
    _provider(monkeypatch)
    reservations = []

    def reserve(*args):
        reservations.append(args[-1])
        return len(reservations) > 1

    monkeypatch.setattr(generation_budget, "debit_generation_spend_at", reserve)
    posts = []

    class Client:
        async def post(self, *args, **kwargs):
            posts.append(kwargs)
            raise httpx.ReadTimeout("create reply lost")

    receipt = tmp_path / "first_lama_receipt.json"
    with pytest.raises(RuntimeError, match="generation_daily_budget_reached"):
        asyncio.run(_remove_text_with_retry(IMAGE, Client(), "first frame", receipt))
    assert json.loads(receipt.read_text())["state"] == "budget_pending"
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(_remove_text_with_retry(IMAGE, Client(), "first frame", receipt))
    with pytest.raises(RuntimeError, match="outcome unknown"):
        asyncio.run(_remove_text_with_retry(IMAGE, Client(), "first frame", receipt))
    assert len(posts) == 1
    assert len(reservations) == 2
    assert reservations[0] == reservations[1]

def test_paid_replay_uses_original_frames_without_redownloading(monkeypatch, tmp_path):
    from routers import recreate
    from scraper import frame_extractor

    job_dir = tmp_path / "recreate" / "same-job"
    job_dir.mkdir(parents=True)
    (job_dir / "source_video.mp4").write_bytes(b"v" * 1001)
    (job_dir / "first_frame_original.jpg").write_bytes(b"first-original")
    (job_dir / "last_frame_original.jpg").write_bytes(b"last-original")
    (job_dir / "first_lama_receipt.json").write_text('{"state":"polling"}')
    monkeypatch.setattr(recreate, "get_project_recreate_dir", lambda _: job_dir.parent)
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append("source mutation")
        raise AssertionError("paid replay must not redownload or re-extract")

    async def send(*args):
        calls.append(args[1])

    async def clean(image, client, label, receipt):
        return "https://fixture.example/clean.png"

    async def download_clean(client, url, dest, label):
        dest.write_bytes(b"clean")

    monkeypatch.setattr(frame_extractor, "download_video", forbidden)
    monkeypatch.setattr(frame_extractor, "extract_frame", forbidden)
    monkeypatch.setattr(recreate, "_get_video_duration", lambda path: asyncio.sleep(0, result=1.0))
    monkeypatch.setattr(recreate, "_remove_text_with_retry", clean)
    monkeypatch.setattr(recreate, "_download_cleaned_image", download_clean)
    monkeypatch.setattr(recreate, "_send", send)
    asyncio.run(recreate._run_pipeline_owned("same-job", "https://changed.invalid/new", "fixture"))
    assert "source mutation" not in calls
    assert "complete" in calls
    assert (job_dir / "first_frame_original.jpg").read_bytes() == b"first-original"
