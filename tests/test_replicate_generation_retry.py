"""Replicate video generation survives transient provider faults without extra spend.

Each scenario replays a failure observed in production on 2026-09-23/24 (see
events.md). The invariants: a fault that proves no prediction was created, or
that hits the poll of an existing prediction, is retried; a fault that may
already have created a paid prediction is never resubmitted; account-level
refusals (402) fail immediately.
"""

import json

import httpx
import pytest

from providers import base, replicate

MODEL = "minimax/hailuo-2.3"
CREATE = f"/v1/models/{MODEL}/predictions"
VIDEO = "https://replicate.delivery/out.mp4"


class Script:
    """A scripted Replicate API: each route pops its next canned outcome."""

    def __init__(self, creates, polls=()):
        self.creates = list(creates)
        self.polls = list(polls)
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == CREATE:
            outcome = self.creates.pop(0)
        elif request.method == "POST" and request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"status": "canceled"})
        elif request.method == "GET" and request.url.path.startswith("/v1/predictions/"):
            outcome = self.polls.pop(0)
        else:  # pragma: no cover - a request the test did not script
            raise AssertionError(f"unexpected request {request.method} {request.url}")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def count(self, method, suffix):
        return sum(1 for m, path in self.calls if m == method and path.endswith(suffix))


def created(pred_id="p1"):
    return httpx.Response(201, json={"id": pred_id, "status": "starting"})


def status(value, **extra):
    return httpx.Response(200, json={"status": value, **extra})


def done():
    return status("succeeded", output=VIDEO)


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch):
    clock = {"now": 0.0, "slept": []}

    async def fake_sleep(seconds):
        clock["slept"].append(seconds)
        clock["now"] += seconds

    # raising=False keeps the red proof behavioural on the pre-fix module,
    # which has no injectable clock.
    monkeypatch.setattr(replicate, "_sleep", fake_sleep, raising=False)
    monkeypatch.setattr(replicate, "_now", lambda: clock["now"], raising=False)
    monkeypatch.setitem(replicate.API_KEYS, "replicate", "test-token")
    return clock


async def run(script):
    entry = {}
    params = {"model_id": MODEL, "entry": entry, "duration": 6, "resolution": "1080p"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(script.handler)) as client:
        return await replicate.generate("a truck on a ridge road", params, client), entry


def throttled(retry_after=4):
    body = {"detail": "Request was throttled.", "status": 429, "retry_after": retry_after}
    return httpx.Response(429, text=json.dumps(body))


async def test_submission_throttle_is_retried_after_the_hinted_delay(fast_clock):
    script = Script(creates=[throttled(4), created()], polls=[done()])
    url, entry = await run(script)
    assert url == VIDEO
    assert entry["provider_request_id"] == "p1"
    assert script.count("POST", "predictions") == 2
    assert fast_clock["slept"][0] == 4


@pytest.mark.parametrize("code", [500, 503])
async def test_submission_5xx_is_retried(code):
    script = Script(
        creates=[httpx.Response(code, text='{"detail":"Internal server error","status":%d}' % code), created()],
        polls=[done()],
    )
    url, _ = await run(script)
    assert url == VIDEO


@pytest.mark.parametrize("code", [502, 504])
async def test_gateway_submission_result_is_not_resubmitted(code):
    # A gateway error may hide a prediction the upstream already created.
    script = Script(creates=[httpx.Response(code, text="<html>gateway</html>"), created()], polls=[done()])
    with pytest.raises(RuntimeError, match="Replicate start failed"):
        await run(script)
    assert script.count("POST", "predictions") == 1


async def test_unreachable_submission_is_retried_because_nothing_was_sent():
    script = Script(creates=[httpx.ConnectTimeout(""), created()], polls=[done()])
    url, _ = await run(script)
    assert url == VIDEO


async def test_poll_transport_error_keeps_the_same_paid_prediction():
    # 2026-09-23 cpl-508e6c57e78780e6: ReadError on the poll GET abandoned a
    # prediction that had already been created (and would be billed).
    script = Script(
        creates=[created()],
        polls=[status("processing"), httpx.ReadError(""), httpx.ConnectTimeout(""),
               httpx.Response(502, text="<html>bad gateway</html>"), done()],
    )
    url, _ = await run(script)
    assert url == VIDEO
    assert script.count("POST", "predictions") == 1, "a poll fault must never create a second prediction"


async def test_insufficient_credit_fails_immediately_without_retry():
    body = '{"title":"Insufficient credit","detail":"You have insufficient credit to run this model.","status":402}'
    script = Script(creates=[httpx.Response(402, text=body)])
    with pytest.raises(RuntimeError, match="Replicate start failed") as error:
        await run(script)
    assert script.count("POST", "predictions") == 1
    assert base.classify_provider_error(str(error.value)) == "insufficient_credit"


async def test_ambiguous_submission_read_timeout_is_not_resubmitted():
    # The request may already have created a prediction; a retry could pay twice.
    script = Script(creates=[httpx.ReadTimeout(""), created()])
    with pytest.raises(httpx.ReadTimeout):
        await run(script)
    assert script.count("POST", "predictions") == 1


async def test_throttle_retries_are_bounded():
    script = Script(creates=[throttled(1)] * replicate.START_ATTEMPTS)
    with pytest.raises(RuntimeError, match="Replicate start failed"):
        await run(script)
    assert script.count("POST", "predictions") == replicate.START_ATTEMPTS


async def test_interrupted_prediction_is_resubmitted_once():
    interrupted = status("failed", error="Prediction interrupted; please retry (code: PA)")
    script = Script(creates=[created("p1"), created("p2")], polls=[interrupted, done()])
    url, entry = await run(script)
    assert url == VIDEO
    assert entry["provider_request_id"] == "p2"

    again = Script(creates=[created("p1"), created("p2")], polls=[interrupted, interrupted])
    with pytest.raises(RuntimeError, match=r"code: PA"):
        await run(again)
    assert again.count("POST", "predictions") == 2


async def test_provider_side_failure_is_not_resubmitted():
    script = Script(creates=[created()], polls=[status("failed", error="Moderation check failed")])
    with pytest.raises(RuntimeError, match="Replicate failed: Moderation"):
        await run(script)
    assert script.count("POST", "predictions") == 1


async def test_timed_out_prediction_is_cancelled_so_it_stops_costing(monkeypatch):
    monkeypatch.setattr(replicate, "PREDICTION_DEADLINE_SECONDS", 20.0)
    script = Script(creates=[created("slow")], polls=[status("processing")] * 10)
    with pytest.raises(RuntimeError, match="timed out"):
        await run(script)
    assert ("POST", "/v1/predictions/slow/cancel") in script.calls


async def test_persistent_poll_outage_is_bounded_and_cancels():
    script = Script(creates=[created("gone")],
                    polls=[httpx.ReadError("")] * (replicate.POLL_TRANSIENT_LIMIT + 1))
    with pytest.raises(RuntimeError, match="poll unavailable"):
        await run(script)
    assert ("POST", "/v1/predictions/gone/cancel") in script.calls


@pytest.mark.parametrize("message, expected", [
    ('Replicate start failed: {"title":"Insufficient credit","detail":"x","status":402}', "insufficient_credit"),
    ('Replicate start failed: {"detail":"Request was throttled. ... less than $5.0 in credit.","status":429,"retry_after":1}', "rate_limited"),
    ('Replicate start failed: {"detail":"Internal server error","status":503}', "provider_5xx"),
    ("Replicate generation timed out after 600s (prediction abc)", "prediction_timeout"),
    ("Replicate failed: Prediction interrupted; please retry (code: PA)", "prediction_interrupted"),
    ("Replicate failed: Warning: Moderation check failed: Error code: 401", "moderation"),
    ("ReadError('')", "transport"),
    ("ConnectTimeout('')", "transport"),
    ("something new", "other"),
])
def test_provider_errors_map_to_a_closed_vocabulary(message, expected):
    assert base.classify_provider_error(message) == expected
