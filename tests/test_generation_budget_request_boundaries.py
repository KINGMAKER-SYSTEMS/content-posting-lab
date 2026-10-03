"""Paid-create accounting at queued execution and bounded HTTP retry boundaries."""

import asyncio
import copy
import inspect
from datetime import datetime, timezone

import httpx
import pytest

from providers import base, replicate
from routers import video
from services import generation_budget
from services.json_store import atomic_load
from tests.test_replicate_generation_retry import MODEL, Script, created, done


DAY1 = datetime(2026, 10, 2, 23, 59, 50, tzinfo=timezone.utc)
DAY2 = datetime(2026, 10, 3, 0, 0, 10, tzinfo=timezone.utc)


@pytest.fixture
def utc_clock(monkeypatch):
    clock = {"now": DAY1}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr(generation_budget, "datetime", Clock)
    return clock


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,cost", [("hailuo", 0.28), ("grok", 3.0)])
@pytest.mark.parametrize("execution_day_full", [False, True])
async def test_queued_ui_create_is_charged_on_execution_day(monkeypatch, utc_clock, provider, cost, execution_day_full):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, str(cost))
    monkeypatch.setitem(base.API_KEYS, "replicate", "fixture-token")
    monkeypatch.setitem(base.API_KEYS, "xai", "fixture-token")
    semaphore = asyncio.Semaphore(0)
    monkeypatch.setattr(video, "_gen_semaphore", semaphore)
    monkeypatch.setattr(base, "FORCE_LANDSCAPE", set())
    observed = []

    def handler(request):
        if request.method == "POST":
            observed.append(copy.deepcopy(atomic_load(generation_budget.jobs_store_path())))
            return httpx.Response(201 if provider == "hailuo" else 200,
                                  json={"id": "paid-id", "request_id": "paid-id"})
        return httpx.Response(200, json={"status": "succeeded", "output": "https://fixture.test/video.mp4",
                                       "video": {"url": "https://fixture.test/video.mp4"}})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(base.httpx, "AsyncClient",
                        lambda *a, **kw: original_client(transport=httpx.MockTransport(handler)))

    async def download(_client, _url, dest):
        dest.write_bytes(b"fixture")

    monkeypatch.setattr(base, "download_media", download)
    tasks = []
    create_task = asyncio.create_task

    def capture(coro):
        task = create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(video.asyncio, "create_task", capture)
    args = {name: param.default.default for name, param in inspect.signature(video.generate_video).parameters.items()}
    args.update(prompt="fixture scene", provider=provider, count=1, duration=6, project="budget-boundary")
    result = await video.generate_video(**args)
    await asyncio.sleep(0)
    assert observed == [], "the real provider must wait behind the queue semaphore"
    assert generation_budget.spent_usd(atomic_load(generation_budget.jobs_store_path(), default={})) == 0
    utc_clock["now"] = DAY2
    if execution_day_full:
        assert generation_budget.debit_generation_spend_at(generation_budget.jobs_store_path(), cost, "other-producer")
    semaphore.release()
    await asyncio.gather(*tasks)
    if execution_day_full:
        entry = video.jobs[result["job_id"]]["videos"][0]
        assert entry["status"] == "error" and entry["error"] == "generation_daily_budget_reached"
        assert observed == [], "execution-day exhaustion must stop the paid create"
        return
    assert video.jobs[result["job_id"]]["videos"][0]["status"] == "done"
    assert len(observed) == 1
    assert observed[0]["generationBudget"]["day"] == "2026-10-03"
    assert observed[0]["generationBudget"]["spentUsd"] == pytest.approx(cost)


async def run_metered(script, monkeypatch, utc_clock=None):
    cost = 0.28
    assert generation_budget.debit_generation_spend_at(
        generation_budget.jobs_store_path(), cost, "boundary-job:s0")

    async def no_sleep(_seconds):
        if utc_clock is not None:
            utc_clock["now"] = DAY2

    monkeypatch.setattr(replicate, "_sleep", no_sleep)
    params = {"model_id": MODEL, "entry": {}, "duration": 6, "resolution": "1080p",
              "job_id": "boundary-job", "cost_usd": cost}
    async with httpx.AsyncClient(transport=httpx.MockTransport(script.handler)) as client:
        return await replicate.generate("fixture scene", params, client)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [500, 503])
async def test_each_http_create_retry_is_metered(monkeypatch, code):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "1")
    script = Script([httpx.Response(code, json={"status": code}), created()], [done()])
    await run_metered(script, monkeypatch)
    assert script.count("POST", "predictions") == 2
    ledger = atomic_load(generation_budget.jobs_store_path())["generationBudget"]
    assert ledger["spentUsd"] == pytest.approx(0.56)
    assert len(ledger["debits"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [500, 503])
async def test_http_create_retry_cannot_exceed_remaining_budget(monkeypatch, code):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "0.5")
    script = Script([httpx.Response(code, json={"status": code}), created()], [done()])
    with pytest.raises(RuntimeError, match="generation_daily_budget_reached"):
        await run_metered(script, monkeypatch)
    assert script.count("POST", "predictions") == 1
    assert atomic_load(generation_budget.jobs_store_path())["generationBudget"]["spentUsd"] == pytest.approx(0.28)


@pytest.mark.asyncio
async def test_http_create_backoff_crossing_midnight_charges_the_new_request_day(monkeypatch, utc_clock):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, "0.28")
    script = Script([httpx.Response(503, json={"status": 503}), created()], [done()])
    await run_metered(script, monkeypatch, utc_clock)
    assert script.count("POST", "predictions") == 2
    ledger = atomic_load(generation_budget.jobs_store_path())["generationBudget"]
    assert ledger["day"] == "2026-10-03"
    assert ledger["spentUsd"] == pytest.approx(0.28)
    assert len(ledger["debits"]) == 2, "the first day's request history survives rollover"


@pytest.mark.asyncio
async def test_every_bounded_create_attempt_has_its_own_debit(monkeypatch):
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, str(0.28 * replicate.START_ATTEMPTS))
    script = Script([httpx.Response(500, json={"status": 500})] * (replicate.START_ATTEMPTS - 1)
                    + [created()], [done()])
    await run_metered(script, monkeypatch)
    assert script.count("POST", "predictions") == replicate.START_ATTEMPTS
    ledger = atomic_load(generation_budget.jobs_store_path())["generationBudget"]
    assert ledger["spentUsd"] == pytest.approx(0.28 * replicate.START_ATTEMPTS)
    assert len(ledger["debits"]) == replicate.START_ATTEMPTS
