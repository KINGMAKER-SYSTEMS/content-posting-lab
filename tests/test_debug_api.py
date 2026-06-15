"""Auth-gating tests for the debug router (logs/stream/jobs/errors/health/clear).

Security invariant: debug endpoints expose internal logs, errors, job traces, and a ring
buffer that has historically carried recoverable API-key fragments. They must NEVER be public
on a deployed (Railway) environment. The router-level `_gate_debug` dependency enforces:

  - MINIAPP_AGENT_KEY set      → matching X-Agent-Key required everywhere.
  - key unset + deployed       → fail closed (503), regardless of header.
  - key unset + local dev      → open.
"""

import pytest


@pytest.fixture(autouse=True)
def _clean_debug_env(monkeypatch):
    # Start each test from a known-clean state; individual tests set what they need.
    monkeypatch.delenv("MINIAPP_AGENT_KEY", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("RAILWAY_SERVICE_ID", raising=False)


def test_debug_open_in_local_dev(sync_client, monkeypatch):
    # No key, not deployed → open (dev convenience).
    r = sync_client.get("/api/debug/health")
    assert r.status_code == 200
    assert "buffer" in r.json()


def test_debug_fails_closed_when_deployed_without_key(sync_client, monkeypatch):
    # No key but deployed → must NOT be public. Even with a header, no key means deny.
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    r = sync_client.get("/api/debug/health")
    assert r.status_code == 503

    r2 = sync_client.get("/api/debug/errors", headers={"X-Agent-Key": "anything"})
    assert r2.status_code == 503


def test_debug_fails_closed_on_railway_service_id(sync_client, monkeypatch):
    # The other deployed signal (service id) also closes the door.
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "svc_123")
    r = sync_client.get("/api/debug/logs")
    assert r.status_code == 503


def test_debug_requires_matching_key_when_configured(sync_client, monkeypatch):
    monkeypatch.setenv("MINIAPP_AGENT_KEY", "s3cret")

    # Missing key → 401.
    assert sync_client.get("/api/debug/health").status_code == 401
    # Wrong key → 401.
    assert sync_client.get(
        "/api/debug/health", headers={"X-Agent-Key": "nope"}
    ).status_code == 401
    # Correct key → 200, even when deployed.
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    ok = sync_client.get("/api/debug/health", headers={"X-Agent-Key": "s3cret"})
    assert ok.status_code == 200
