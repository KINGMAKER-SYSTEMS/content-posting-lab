"""Current published prices follow the actual unchanged provider payload.

Rates are pinned to the exact Replicate model pages and direct xAI classic
Grok docs; resolution/duration normalization and input fees remain explicit.
"""

import pytest

from services import generation_budget


def test_grok_priced_by_requested_duration():
    assert generation_budget.per_gen_cost_usd("grok", 10, resolution="480p") == pytest.approx(0.50)
    assert generation_budget.per_gen_cost_usd("grok", 5, resolution="720p") == pytest.approx(0.35)
    assert generation_budget.per_gen_cost_usd("grok", 15, resolution="720p") == pytest.approx(1.05)


def test_wan_prices_are_per_output_not_requested_seconds():
    assert generation_budget.per_gen_cost_usd("wan-t2v", 10) == pytest.approx(0.05)
    assert generation_budget.per_gen_cost_usd("wan-i2v-fast", 15) == pytest.approx(0.05)


def test_pruna_prices_normalized_duration():
    assert generation_budget.per_gen_cost_usd("pruna-pvideo", 15) == pytest.approx(0.20)


def test_hailuo_prices_normalized_duration_and_resolution():
    assert generation_budget.per_gen_cost_usd("hailuo", 6) == pytest.approx(0.28)
    assert generation_budget.per_gen_cost_usd("hailuo", 15) == pytest.approx(0.56)
    assert generation_budget.per_gen_cost_usd("hailuo", 15, resolution="1080p") == pytest.approx(0.49)


def test_unknown_provider_fails_closed():
    with pytest.raises(ValueError):
        generation_budget.per_gen_cost_usd("no-such-provider", 10)


def test_per_second_provider_requires_duration():
    with pytest.raises(ValueError):
        generation_budget.per_gen_cost_usd("grok", None)
    with pytest.raises(ValueError):
        generation_budget.per_gen_cost_usd("grok", 0)
    with pytest.raises(ValueError):
        generation_budget.per_gen_cost_usd("grok", float("nan"))


@pytest.mark.parametrize("provider", ["pruna-pvideo", "pruna-pvideo-vertical"])
@pytest.mark.parametrize("duration,resolution,budget,status", [
    (6, "1080p", "0.15", 429),
    (6, "1080p", "0.24", 200),
    (6, "720p", "0.12", 200),
    (15, "720p", "0.20", 200),
    (15, "1080p", "0.40", 200),
    (6, "invalid", "0.12", 200),
    (1, "1080p", "0.03", 429),
])
def test_pvideo_actual_payload_cap(monkeypatch, provider, duration, resolution, budget, status):
    """Use the real admission route; close queued work before any provider call."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import video

    monkeypatch.setitem(video.API_KEYS, "replicate", "fixture-token")
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, budget)
    queued = []

    class QueuedTask:
        def add_done_callback(self, callback):
            pass

    def capture(coroutine):
        queued.append(coroutine)
        coroutine.close()
        return QueuedTask()

    monkeypatch.setattr(video.asyncio, "create_task", capture)
    app = FastAPI()
    app.include_router(video.router, prefix="/api/video")
    response = TestClient(app).post("/api/video/generate", data={
        "prompt": "fixture scene", "provider": provider, "count": 1,
        "duration": duration, "resolution": resolution, "project": "pvideo-cap",
    })
    assert response.status_code == status, response.text
    if status == 429:
        assert response.json()["detail"]["error"] == "generation_daily_budget_reached"
        assert not queued and not video.jobs
    else:
        assert len(queued) == 1


@pytest.mark.parametrize("provider", ["pruna-pvideo", "pruna-pvideo-vertical"])
@pytest.mark.parametrize("duration,resolution,expected", [
    (None, None, 0.12), (15, "1080p", 0.40), (0, "1080p", 0.04),
    (6.9, "1080p", 0.24), (6, "720p", 0.12), (6, "invalid", 0.12),
])
def test_pvideo_prices_the_unchanged_standard_builder(provider, duration, resolution, expected):
    from providers import PROVIDERS, replicate
    from services import moderation_retry

    parameters = {"variant": PROVIDERS[provider]["variant"], "draft": True}
    if duration is not None:
        parameters["duration"] = duration
    if resolution is not None:
        parameters["resolution"] = resolution
    actual = replicate._build_pvideo_input("fixture scene", parameters)
    assert actual["draft"] is False
    assert generation_budget.per_gen_cost_usd(provider, duration, resolution=resolution) == pytest.approx(expected)
    assert moderation_retry.attempt_cost_usd("prunaai/p-video", duration, resolution=resolution) == pytest.approx(expected)


@pytest.mark.parametrize("provider,duration,resolution,parameters,expected", [
    ("hailuo", 6, "768p", {}, 0.28),
    ("hailuo", 10, "768p", {}, 0.56),
    ("hailuo", 15, "1080p", {}, 0.49),
    ("hailuo", 7, "invalid", {}, 0.28),
    ("wan-t2v", 15, "480p", {"num_frames": 121, "frames_per_second": 5}, 0.05),
    ("wan-t2v", 1, "720p", {"interpolate_output": True}, 0.10),
    ("wan-i2v", 15, "480p", {"num_frames": 100, "frames_per_second": 24}, 0.40),
    ("wan-i2v", 1, "720p", {}, 1.00),
    ("wan-i2v-fast", 15, "480p", {}, 0.05),
    ("wan-i2v-fast", 15, "480p", {"interpolate_output": True}, 0.065),
    ("wan-i2v-fast", 1, "720p", {"interpolate_output": False}, 0.11),
    ("wan-i2v-fast", 1, "720p", {"interpolate_output": True}, 0.145),
    ("flux-image", 7, "1080p", {}, 0.045),
    ("flux-image", 7, "1080p", {"image_resolution": "1 MP"}, 0.03),
    ("flux-image", 7, "1080p", {"image_resolution": "2 MP", "image_data_uri": "not-forwarded"}, 0.045),
    ("grok", 10, "480p", {}, 0.50),
    ("grok", 10, "720p", {}, 0.70),
    ("grok", 1, "720p", {"image_data_uri": "data:image/png;base64,fixture"}, 0.072),
])
def test_current_prices_match_provider_payload(provider, duration, resolution, parameters, expected):
    from providers import PROVIDERS
    from services import moderation_retry

    assert generation_budget.per_gen_cost_usd(
        provider, duration, resolution=resolution, parameters=parameters,
    ) == pytest.approx(expected)
    model = PROVIDERS[provider]["models"][0]
    assert moderation_retry.attempt_cost_usd(
        model, duration, resolution=resolution, parameters=parameters,
    ) == pytest.approx(expected)


@pytest.mark.parametrize("provider,duration,resolution,parameters", [
    ("grok", 6, "1080p", {}), ("grok", 16, "720p", {}),
    ("grok", True, "720p", {}), ("grok", 6.5, "720p", {}),
    ("wan-t2v", 6, "1080p", {}),
    ("wan-i2v-fast", 6, "720p", {"interpolate_output": "yes"}),
    ("flux-image", 6, "1080p", {"image_resolution": "4 MP"}),
    ("hailuo", 6, [], {}), ("pruna-pvideo", False, "720p", {}),
])
def test_unsupported_payload_prices_fail_closed(provider, duration, resolution, parameters):
    from providers import PROVIDERS
    from services import moderation_retry

    with pytest.raises(ValueError):
        generation_budget.per_gen_cost_usd(provider, duration, resolution=resolution, parameters=parameters)
    # A known model's invalid price must not become None and fall back to its
    # stale recipe estimate at the executor boundary.
    with pytest.raises(ValueError):
        moderation_retry.attempt_cost_usd(PROVIDERS[provider]["models"][0], duration,
                                          resolution=resolution, parameters=parameters)


@pytest.mark.parametrize("provider,duration,resolution,budget", [
    ("hailuo", 6, "1080p", "0.28"),
    ("hailuo", 10, "768p", "0.28"),
    ("flux-image", 7, "1080p", "0.03"),
    ("wan-i2v", 15, "720p", "0.90"),
])
def test_known_underpriced_payload_is_refused_before_queue(monkeypatch, provider, duration, resolution, budget):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import video

    monkeypatch.setitem(video.API_KEYS, "replicate", "fixture-token")
    monkeypatch.setenv(generation_budget.USD_BUDGET_ENV, budget)
    queued = []
    class QueuedTask:
        def add_done_callback(self, callback):
            pass
    def capture(coroutine):
        queued.append(coroutine)
        coroutine.close()
        return QueuedTask()
    monkeypatch.setattr(video.asyncio, "create_task", capture)
    app = FastAPI()
    app.include_router(video.router, prefix="/api/video")
    response = TestClient(app).post("/api/video/generate", data={
        "prompt": "fixture scene", "provider": provider, "count": 1,
        "duration": duration, "resolution": resolution, "project": "catalog-cap",
    })
    assert response.status_code == 429, response.text
    assert response.json()["detail"]["error"] == "generation_daily_budget_reached"
    assert not queued and not video.jobs
