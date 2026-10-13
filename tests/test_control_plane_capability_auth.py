"""Capability reads require the worker credential before page intent is loaded."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.control_plane as cp


PAGE_ID = "tt-tucker-reeves"
TOKEN = "test-control-plane-token"


def _client():
    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    return TestClient(app)


def test_capabilities_refuse_missing_invalid_and_unconfigured_bearer_before_data_read(monkeypatch):
    def forbidden_read(page_id):
        raise AssertionError("unauthorized request reached page intent")

    monkeypatch.setattr(cp, "_current_intent_for_capabilities", forbidden_read)
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("APP_API_KEY", "browser-facing-key")
    url = "/api/control-plane/v1/capabilities"
    page = {"X-RT-Page-Id": PAGE_ID}
    with _client() as client:
        assert client.get(url, headers=page).status_code == 401
        assert client.get(url, headers={**page, "Authorization": "Bearer wrong"}).status_code == 401
        assert client.get(url, headers={**page, "Authorization": "Bearer browser-facing-key"}).status_code == 401
        monkeypatch.delenv("CONTROL_PLANE_TOKEN")
        assert client.get(url, headers={**page, "Authorization": f"Bearer {TOKEN}"}).status_code == 503


def test_capabilities_accept_worker_bearer_and_preserve_response(monkeypatch):
    calls = []
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setattr(cp, "_current_intent_for_capabilities", lambda page_id: calls.append(page_id) or None)
    with _client() as client:
        response = client.get(
            "/api/control-plane/v1/capabilities",
            headers={"X-RT-Page-Id": PAGE_ID, "Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 200
    assert calls == [PAGE_ID]
    assert response.json() == cp._capabilities_response([])
