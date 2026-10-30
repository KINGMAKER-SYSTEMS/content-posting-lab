import logging
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.control_plane as cp
import routers.post_renders as post_renders
from services import safety_controls as controls, post_render_jobs, moderation_retry

URL = "/api/control-plane/v1/safety-controls"
TOKEN = "test-control-token"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("APP_API_KEY", "browser-key")
    monkeypatch.setattr(post_renders, "_started_service", None)
    monkeypatch.setattr(post_renders, "service", lambda: pytest.fail("manager constructed"))
    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    with TestClient(app) as c:
        yield c


def auth():
    return {"Authorization": "Bearer " + TOKEN}


def test_auth_before_config_and_dark_flag(client, monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_SAFETY_CONTROLS_ENABLED", "1")
    calls=[]
    monkeypatch.setattr(controls, "snapshot", lambda _: calls.append(1) or {})
    assert client.get(URL).status_code == 401
    assert client.get(URL, headers={"Authorization":"Bearer wrong"}).status_code == 401
    assert client.get(URL, headers={"Authorization":"Bearer browser-key"}).status_code == 401
    monkeypatch.delenv("CONTROL_PLANE_TOKEN")
    assert client.get(URL, headers=auth()).status_code == 503
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.delenv("CONTENT_LAB_SAFETY_CONTROLS_ENABLED")
    assert client.get(URL, headers=auth()).status_code == 404
    assert calls == []


def test_defaults_real_constants_and_unknown_worker(client, monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_SAFETY_CONTROLS_ENABLED", "1")
    r=client.get(URL, headers=auth())
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    data=r.json(); render=data["render"]
    assert data["schema"] == "content-lab.safety-controls.v1"
    assert render["started_worker_count"] is None and render["claim_floor_bytes"] is None
    assert render["configured_workers"] == {"state":"default", "value":2}
    assert render["max_attempts"] == post_render_jobs.MAX_ATTEMPTS
    assert data["moderation_retry_daily_per_page"] == {"state":"default", "value":6}


def test_snapshot_vs_config_and_invalid_secret_never_reflected_or_logged(client, monkeypatch, caplog):
    monkeypatch.setenv("CONTENT_LAB_SAFETY_CONTROLS_ENABLED", "1")
    monkeypatch.setattr(post_renders, "_started_service", SimpleNamespace(worker_count=3))
    secret="FAKE-SECRET-CONTROL-LEAK"
    for key in ("SYZYGY_API_KEY", "CONTROL_PLANE_SERVICE_SECRET", "ARBITRARY_SECRET", "CONTENT_LAB_POST_RENDER_ROOT", "CONTENT_LAB_PUBLIC_ORIGIN"):
        monkeypatch.setenv(key, secret + "?token=" + secret)
    monkeypatch.setenv("CONTENT_LAB_POST_RENDER_WORKERS", secret)
    monkeypatch.setenv("CONTENT_LAB_POST_RENDER_MIN_FREE_BYTES", secret)
    monkeypatch.setenv("CONTENT_LAB_MODERATION_RETRY_DAILY_BUDGET", secret)
    monkeypatch.setenv("CONTENT_LAB_PROVIDER_DOWNLOAD_SECONDS", "inf")
    with caplog.at_level(logging.WARNING):
        r=client.get(URL, headers=auth())
    assert r.status_code == 200
    data=r.json(); render=data["render"]
    assert render["started_worker_count"] == 3
    assert render["configured_workers"] == {"state":"invalid", "value":2}
    assert render["minimum_free_bytes"] == {"state":"invalid", "value":post_render_jobs.DEFAULT_MIN_FREE_BYTES}
    assert render["claim_floor_bytes"] == post_render_jobs.DEFAULT_MIN_FREE_BYTES + 3 * post_render_jobs.PEAK_RENDER_WORKSPACE_BYTES
    assert data["moderation_retry_daily_per_page"] == {"state":"invalid", "value":0}
    from providers import base as provider
    actual=provider._provider_download_deadline()
    assert data["provider_download"]["seconds"] == {"state":"invalid", "value":actual if math.isfinite(actual) else None}
    assert secret not in r.text and secret not in caplog.text


def test_valid_settings_match_consumer_behavior(client, monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_SAFETY_CONTROLS_ENABLED", "1")
    monkeypatch.setattr(post_renders, "_started_service", SimpleNamespace(worker_count=1))
    monkeypatch.setenv("CONTENT_LAB_POST_RENDER_WORKERS", "4")
    monkeypatch.setenv("CONTENT_LAB_POST_RENDER_MIN_FREE_BYTES", "12345")
    monkeypatch.setenv("CONTENT_LAB_MODERATION_RETRY_DAILY_BUDGET", "0")
    r=client.get(URL, headers=auth()); data=r.json()
    assert data["render"]["configured_workers"]["value"] == post_render_jobs._worker_count() == 4
    assert data["render"]["started_worker_count"] == 1
    assert data["render"]["minimum_free_bytes"]["value"] == post_render_jobs._min_free_bytes() == 12345
    assert data["moderation_retry_daily_per_page"]["value"] == moderation_retry.daily_budget() == 0


def test_capabilities_unchanged_and_router_has_no_public_health_route(client, monkeypatch):
    monkeypatch.setattr(cp, "_current_intent_for_capabilities", lambda _: None)
    r=client.get("/api/control-plane/v1/capabilities", headers={**auth(), "X-RT-Page-Id":"tt-test"})
    assert r.status_code == 200 and r.json() == cp._capabilities_response([])
    assert client.get("/api/health").status_code == 404


def test_fractional_provider_and_invalid_moderation_match_consumers(client, monkeypatch, caplog):
    from providers import base as provider
    monkeypatch.setenv("CONTENT_LAB_SAFETY_CONTROLS_ENABLED", "1")
    monkeypatch.setenv("CONTENT_LAB_PROVIDER_DOWNLOAD_SECONDS", "0.05")
    monkeypatch.setenv("CONTENT_LAB_MODERATION_RETRY_DAILY_BUDGET", "bad")
    result=client.get(URL, headers=auth()).json()
    assert result["provider_download"]["seconds"]["value"] == provider._provider_download_deadline() == 0.05
    with caplog.at_level(logging.WARNING):
        assert moderation_retry.daily_budget() == result["moderation_retry_daily_per_page"]["value"] == 0


def test_invalid_manager_snapshot_is_unknown(client, monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_SAFETY_CONTROLS_ENABLED", "1")
    monkeypatch.setattr(post_renders, "_started_service", SimpleNamespace(worker_count="FAKE-SECRET"))
    r=client.get(URL,headers=auth()); assert r.status_code==200
    assert r.json()["render"]["started_worker_count"] is None
    assert r.json()["render"]["claim_floor_bytes"] is None
    assert "FAKE-SECRET" not in r.text


@pytest.mark.parametrize("value", [True, 0, 5])
def test_non_worker_manager_value_is_unknown(client, monkeypatch, value):
    monkeypatch.setenv("CONTENT_LAB_SAFETY_CONTROLS_ENABLED", "1")
    monkeypatch.setattr(post_renders, "_started_service", SimpleNamespace(worker_count=value))
    data=client.get(URL,headers=auth()).json()["render"]
    assert data["started_worker_count"] is None and data["claim_floor_bytes"] is None


def test_unloaded_provider_is_unknown_without_import(client, monkeypatch):
    monkeypatch.setenv("CONTENT_LAB_SAFETY_CONTROLS_ENABLED", "1")
    monkeypatch.delitem(sys.modules, "providers.base", raising=False)
    r=client.get(URL,headers=auth())
    assert r.status_code==200
    assert r.json()["provider_download"] == {"bytes":{"state":"unknown","value":None}, "seconds":{"state":"unknown","value":None}}
    assert "providers.base" not in sys.modules


def test_docker_copies_router_and_service_directories():
    docker=(Path(controls.__file__).resolve().parents[1] / "Dockerfile").read_text()
    assert "COPY routers/ ./routers/" in docker
    assert "COPY services/ ./services/" in docker
