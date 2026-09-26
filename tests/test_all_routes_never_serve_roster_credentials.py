"""Whole-app sweep: no GET route serves a roster credential.

Seeds the roster cache with sentinel credentials (signup email, password,
forwarding address, Cloudflare alias/rule/destination, notes), then calls
EVERY registered GET route of the real app -- path parameters filled with the
seeded page id -- both anonymously and with every machine credential the app
accepts, and asserts that no response body contains any sentinel.

Outbound network is refused at the socket layer, so nothing leaves the
machine; routes that need an upstream simply fail, which is fine -- a failure
body must not leak either. The lifespan (bots, schedulers) is not started.
Routes that stream forever are listed in STREAMING with the reason; a route
that does not answer within the deadline fails the test, so a new streaming
route has to be classified here rather than silently skipped.
"""

import re
import socket
import threading

import pytest
from fastapi.testclient import TestClient

import services.roster as roster
import services.telegram as tg
from services import json_store
from tests import route_auth_support as ras

# Real per-route auth (not the conftest bypass): the "machine" pass presents
# every credential the app accepts, including the Hub key and an Access JWT.
pytestmark = pytest.mark.real_route_auth

PAGE_ID = "sentinel-page"
TOKEN = "cp-test-token-not-a-secret"
SENTINELS = {
    "signup_email": "sentinel-signup@example.invalid",
    "password": "SENTINEL-PASSWORD-9f3a",
    "fwd_address": "sentinel-forward@example.invalid",
    "email_alias": "sentinel-alias@example.invalid",
    "email_rule_id": "SENTINEL-RULE-ID-77c1",
    "fwd_destination": "sentinel-destination@example.invalid",
    "notes": "SENTINEL-NOTES login backup code 123456",
}

# path -> why it is not called here (it never returns a body that ends).
STREAMING: dict[str, str] = {
    "/api/debug/stream": "SSE of debug_logger entries (request lines, no bodies); never ends",
    "/api/agenticnews/stream": "SSE news feed unrelated to the roster; never ends",
}

DEADLINE_S = 30


def _fill(path: str) -> str:
    def value(match: re.Match) -> str:
        name = match.group(1).split(":")[0]
        if name in {"integration_id", "page_id", "account", "account_id", "handle"}:
            return PAGE_ID
        if name in {"project_name", "project", "name"}:
            return "sentinel-project"
        if name == "full_path":
            return "index.html"
        return "x"

    return re.sub(r"\{([^}]+)\}", value, path)


def _get_routes(app):
    """GET paths from the enumeration the route-auth guard classifies."""
    seen = set()
    for method, path in ras.route_keys(app):
        if method != "GET" or path in seen or path in STREAMING:
            continue
        seen.add(path)
        yield path


@pytest.fixture
def sweep_app(monkeypatch, tmp_path):
    monkeypatch.setattr(roster, "ROSTER_PATH", tmp_path / "page_roster.json")
    monkeypatch.setattr(tg, "CONFIG_PATH", tmp_path / "telegram_config.json")
    monkeypatch.setattr(json_store, "_LOCKS", {})
    monkeypatch.delenv("APP_API_KEY", raising=False)
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("LAB_HUB_API_KEY", ras.HUB_KEY)
    monkeypatch.setenv("LAB_ACCESS_TEAM_DOMAIN", ras.TEAM_DOMAIN)
    monkeypatch.setenv("LAB_ACCESS_AUD", ras.ACCESS_AUD)
    ras.install_idp(monkeypatch)

    real_connect = socket.socket.connect

    def local_only(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost") and not host.startswith("/"):
            raise OSError("network disabled in the credential sweep")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(OSError("dns disabled")))

    for iid, status in ((PAGE_ID, "In Production"), ("sentinel-live", "Live"), ("sentinel-new", "New — Pending Setup")):
        roster.set_page(iid, {
            "name": iid,
            "provider": "tiktok",
            "source": "notion",
            "status": status,
            "pipeline": "Flow Stage",
            "project": "sentinel-project",
            "poster_name": "mon",
            "group": "WARNER",
            "notion_page_id": f"notion-{iid}",
            **SENTINELS,
        })

    from app import app

    return app


def _call(client, url, headers):
    out = {}

    def run():
        try:
            out["response"] = client.get(url, headers=headers)
        except Exception as exc:  # an exception carries no body to leak
            out["error"] = repr(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(DEADLINE_S)
    if thread.is_alive():
        pytest.fail(f"GET {url} did not answer within {DEADLINE_S}s; classify it in STREAMING")
    return out


def test_no_get_route_serves_a_roster_credential(sweep_app):
    client = TestClient(sweep_app, raise_server_exceptions=False)
    anonymous = {}
    machine = {
        "Authorization": f"Bearer {TOKEN}",
        "X-API-Key": ras.HUB_KEY,
        "Cf-Access-Jwt-Assertion": ras.mint(),
        "X-RT-Lane": "content-bucket-control-plane",
        "X-RT-Page-Id": PAGE_ID,
    }
    covered = []
    leaks = []
    statuses = {}
    for path in _get_routes(sweep_app):
        url = _fill(path)
        for label, headers in (("anonymous", anonymous), ("machine", machine)):
            out = _call(client, url, headers)
            response = out.get("response")
            if response is None:
                continue
            body = response.content.decode("utf-8", "replace")
            statuses[(label, url)] = (response.status_code, PAGE_ID in body)
            for field, sentinel in SENTINELS.items():
                if sentinel in body:
                    leaks.append(f"{label} GET {url} -> {response.status_code} serves {field}")
        covered.append(path)
    assert covered, "no GET routes found"
    assert len(covered) > 50, covered
    assert not leaks, "\n".join(leaks)
    # The sweep is not vacuous: the roster-backed routes answered 200 and
    # really did serve the seeded page (by id), just not its credentials.
    for url in (
        "/api/roster/",
        "/api/roster/project/sentinel-project",
        "/api/pipeline/stages",
        f"/api/pipeline/{PAGE_ID}/workspace",
        f"/api/pipeline/{PAGE_ID}/health",
        "/api/pages/",
        "/api/control-plane/v1/roster",
    ):
        assert statuses.get(("machine", url)) == (200, True), (url, statuses.get(("machine", url)))
