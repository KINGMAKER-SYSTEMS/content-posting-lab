"""No Lab code falls back to a production URL when its env var is unset.

Incident 2026-09-26: a local probe ran the sound-sync handler un-stubbed and
its hardcoded Campaign Hub default made one real GET to production
/api/campaigns. Each site below must now fail closed, before any request,
when its variable is unset: routes answer 503 "<service> not configured" and
library/job code raises ConfigError.

Every outbound path is captured by an httpx MockTransport (or a stubbed
``httpx.stream``) that records requests, so "zero outbound requests" is
asserted directly. Only dummy hosts appear here.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import services.campaign_hub as hub
import services.shipstream_source_manifest as source_manifest
from services import sound_cache
from tests.master_pages_fixtures import master_pages


HUB = "https://hub.test"
VAULT = "https://shipstream.test"
HANDLE = "night.walks"
PAGE_ID = "acct:operator:night-walks"


def _is_config_error(error: BaseException) -> bool:
    try:
        from services.config_errors import ConfigError
    except ImportError:
        return False
    return isinstance(error, ConfigError)


@pytest.fixture
def outbound(monkeypatch):
    """Record every request any httpx client would send; answer 404."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.path == "/api/campaigns":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    real_async_client = httpx.AsyncClient
    real_client = httpx.Client

    class RecordingAsyncClient(real_async_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    class RecordingClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    def recording_stream(method, url, **kwargs):
        client = RecordingClient()
        return client.stream(method, url, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", RecordingAsyncClient)
    monkeypatch.setattr(httpx, "Client", RecordingClient)
    monkeypatch.setattr(httpx, "stream", recording_stream)
    return sent


def _hosts(sent: list[httpx.Request]) -> list[str]:
    return [str(request.url) for request in sent]


# ── Campaign Hub (services/campaign_hub.py) ──────────────────────────────


async def test_hub_campaign_list_fails_closed_without_env(outbound, monkeypatch):
    monkeypatch.delenv("CAMPAIGN_HUB_URL", raising=False)
    with pytest.raises(Exception) as caught:
        await hub.fetch_all_campaigns()
    assert _hosts(outbound) == []
    assert _is_config_error(caught.value)
    assert str(caught.value) == "Campaign Hub not configured"


async def test_hub_campaign_detail_fails_closed_without_env(outbound, monkeypatch):
    monkeypatch.delenv("CAMPAIGN_HUB_URL", raising=False)
    with pytest.raises(Exception) as caught:
        await hub.fetch_campaign_detail("some-slug")
    assert _hosts(outbound) == []
    assert _is_config_error(caught.value)


async def test_sound_sync_handler_fails_closed_without_env(outbound, monkeypatch):
    """The exact handler from the incident, run un-stubbed apart from I/O."""
    monkeypatch.delenv("CAMPAIGN_HUB_URL", raising=False)
    monkeypatch.setattr(hub, "list_sounds", lambda active_only=False: [])
    with pytest.raises(Exception) as caught:
        await hub.sync_sound_status(notion_campaigns=[])
    assert _hosts(outbound) == []
    assert _is_config_error(caught.value)


async def test_sound_prepare_fails_closed_without_env(outbound, monkeypatch):
    monkeypatch.delenv("CAMPAIGN_HUB_URL", raising=False)
    with pytest.raises(Exception) as caught:
        await sound_cache.prepare_sound("deadbeefcafe", "Artist - Song")
    assert _hosts(outbound) == []
    assert _is_config_error(caught.value)


def test_hub_reports_unconfigured_without_env(monkeypatch):
    monkeypatch.delenv("CAMPAIGN_HUB_URL", raising=False)
    assert hub.is_configured() is False


@pytest.mark.parametrize("value", [
    "", "   ", "hub.test", "ftp://hub.test", "https://", "https://user:pw@hub.test",
])
async def test_hub_rejects_malformed_env_before_any_request(outbound, monkeypatch, value):
    monkeypatch.setenv("CAMPAIGN_HUB_URL", value)
    assert hub.is_configured() is False
    with pytest.raises(Exception) as caught:
        await hub.fetch_all_campaigns()
    assert _hosts(outbound) == []
    assert _is_config_error(caught.value)


async def test_hub_uses_only_the_configured_origin(outbound, monkeypatch):
    monkeypatch.setenv("CAMPAIGN_HUB_URL", HUB + "/")
    assert hub.is_configured() is True
    assert await hub.fetch_all_campaigns() == []
    with pytest.raises(httpx.HTTPStatusError):
        await hub.fetch_campaign_detail("some-slug")
    assert _hosts(outbound) == [f"{HUB}/api/campaigns", f"{HUB}/api/campaign/some-slug"]


def _route_client(monkeypatch) -> TestClient:
    from routers import slideshow as slideshow_router
    from routers import telegram as telegram_router

    monkeypatch.setattr(telegram_router, "notion_configured", lambda: False)
    monkeypatch.setattr(hub, "list_sounds", lambda active_only=False: [])
    app = FastAPI()
    app.include_router(slideshow_router.router, prefix="/api/slideshow")
    app.include_router(telegram_router.router, prefix="/api/telegram")
    return TestClient(app)


def test_sound_sync_route_answers_503_without_env(outbound, monkeypatch):
    monkeypatch.delenv("CAMPAIGN_HUB_URL", raising=False)
    response = _route_client(monkeypatch).post("/api/telegram/sounds/sync")
    assert _hosts(outbound) == []
    assert response.status_code == 503
    assert response.json() == {"detail": "Campaign Hub not configured"}


def test_sound_prepare_route_answers_503_without_env(outbound, monkeypatch):
    monkeypatch.delenv("CAMPAIGN_HUB_URL", raising=False)
    response = _route_client(monkeypatch).post(
        "/api/slideshow/sounds/prepare",
        json={"telegram_sound_id": "deadbeefcafe", "label": "Artist - Song"},
    )
    assert _hosts(outbound) == []
    assert response.status_code == 503
    assert response.json() == {"detail": "Campaign Hub not configured"}


# ── ShipStream vault (services/shipstream_source_manifest.py) ────────────


def _sourced_intent() -> dict:
    intent, _revision = master_pages(
        PAGE_ID,
        handle=HANDLE,
        content_niche="POV — Night Core",
        content_engine="sourced_video",
        vault_url=f"{VAULT}/vault/{HANDLE}",
    )
    return intent


def test_shipstream_manifest_url_has_no_default_origin(monkeypatch):
    monkeypatch.delenv("SHIPSTREAM_VAULT_ORIGIN", raising=False)
    with pytest.raises(Exception) as caught:
        source_manifest.source_manifest_url(HANDLE)
    assert _is_config_error(caught.value)
    assert str(caught.value) == "ShipStream not configured"


@pytest.mark.parametrize("load", [
    source_manifest.load_shipstream_source_dna_library,
    source_manifest.load_shipstream_source_projection,
])
def test_shipstream_load_fails_closed_before_fetch(outbound, monkeypatch, load):
    monkeypatch.delenv("SHIPSTREAM_VAULT_ORIGIN", raising=False)
    with pytest.raises(source_manifest.ShipStreamSourceUnavailable) as caught:
        load(_sourced_intent(), page_id=PAGE_ID)
    assert _hosts(outbound) == []
    assert _is_config_error(caught.value)


@pytest.mark.parametrize("value", [
    "http://shipstream.test",
    "https://shipstream.test/vault",
    "https://user:pw@shipstream.test",
    "https://shipstream.test?x=1",
    "shipstream.test",
])
def test_shipstream_rejects_malformed_env_before_fetch(outbound, monkeypatch, value):
    monkeypatch.setenv("SHIPSTREAM_VAULT_ORIGIN", value)
    with pytest.raises(Exception) as caught:
        source_manifest.load_shipstream_source_dna_library(_sourced_intent(), page_id=PAGE_ID)
    assert _hosts(outbound) == []
    assert _is_config_error(caught.value)


def test_shipstream_fetches_only_from_the_configured_origin(outbound, monkeypatch):
    monkeypatch.setenv("SHIPSTREAM_VAULT_ORIGIN", VAULT)
    with pytest.raises(source_manifest.ShipStreamSourceMissing):
        source_manifest.load_shipstream_source_dna_library(_sourced_intent(), page_id=PAGE_ID)
    assert _hosts(outbound) == [
        f"{VAULT}/assets/vault%2F{HANDLE}%2Fsource-manifest.json",
    ]


def test_shipstream_vault_url_must_match_the_configured_origin(outbound, monkeypatch):
    monkeypatch.setenv("SHIPSTREAM_VAULT_ORIGIN", "https://other-vault.test")
    with pytest.raises(source_manifest.ShipStreamSourceError, match="does not match"):
        source_manifest.load_shipstream_source_dna_library(_sourced_intent(), page_id=PAGE_ID)
    assert _hosts(outbound) == []
