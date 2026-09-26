"""HTTP-level exposure tests for /api/roster.

Production does not set APP_API_KEY, so the global /api middleware is off and
every /api/roster route answers the public internet. The roster cache holds
account credentials (Notion signup email + password, forwarding address, Cloudflare
Email Routing alias/rule/destination, free-text notes). These tests pin:

  * no /api/roster response ever carries a credential value or key, whatever
    the route (list, project, PUT, sync, legacy sync, duplicates, dedup);
  * DELETE /api/roster/{id} -- which no UI calls -- requires the machine
    credential: unauthenticated -> 401 with no roster body and nothing deleted;
  * authenticated behaviour is unchanged.

This file deliberately talks HTTP only (no import of the guard module), so it
runs red against origin/main for the real reasons, not an ImportError.
Notion is never hit; all state lives in tmp_path.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import services.roster as roster
import services.telegram as tg
from services import json_store
from routers import roster as roster_router

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

CREDENTIAL_KEYS = set(SENTINELS)


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(roster, "ROSTER_PATH", tmp_path / "page_roster.json")
    monkeypatch.setattr(tg, "CONFIG_PATH", tmp_path / "telegram_config.json")
    monkeypatch.setattr(json_store, "_LOCKS", {})
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("APP_API_KEY", raising=False)
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    yield


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(roster_router.router, prefix="/api/roster")
    return TestClient(app)


def _seed_credentialed(ig_id="acct1", name="Acct One", **extra):
    roster.set_page(ig_id, {
        "name": name,
        "provider": "tiktok",
        "project": "proj",
        "source": "notion",
        "notion_page_id": f"notion-{ig_id}",
        "tiktok_url": f"https://www.tiktok.com/@{ig_id}",
        **SENTINELS,
        **extra,
    })


def _keys_anywhere(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys_anywhere(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys_anywhere(item)


def _assert_no_credentials(response):
    text = response.text
    for field, sentinel in SENTINELS.items():
        assert sentinel not in text, f"{field} value serialised by {response.request.method} {response.request.url.path}"
    try:
        body = response.json()
    except ValueError:
        return
    leaked = CREDENTIAL_KEYS & set(_keys_anywhere(body))
    assert not leaked, f"credential keys {sorted(leaked)} in {response.request.url.path}"


def _stub_sync(monkeypatch):
    import services.notion_pages as np

    async def fake_sync():
        return {
            "added": 0,
            "updated": 1,
            "total_in_notion": 1,
            "errors": [],
            "pages": roster.list_all_pages(),
        }

    monkeypatch.setattr(np, "is_configured", lambda: True)
    monkeypatch.setattr(np, "sync_into_roster", fake_sync)


# ── no route serialises credentials ──────────────────────────────────────────


def test_list_roster_never_carries_credentials(client):
    _seed_credentialed()
    r = client.get("/api/roster/")
    assert r.status_code == 200
    page = r.json()["pages"][0]
    assert page["integration_id"] == "acct1"
    assert page["name"] == "Acct One"
    assert page["project"] == "proj"
    _assert_no_credentials(r)


def test_project_pages_never_carry_credentials(client):
    _seed_credentialed()
    r = client.get("/api/roster/project/proj")
    assert r.status_code == 200
    page = r.json()["pages"][0]
    assert page["integration_id"] == "acct1"
    assert page["has_staging_topic"] is False
    _assert_no_credentials(r)


def test_put_response_never_carries_credentials_and_keeps_them_on_disk(client):
    _seed_credentialed()
    r = client.put("/api/roster/acct1", json={"project": "proj2"})
    assert r.status_code == 200
    assert r.json()["page"]["project"] == "proj2"
    _assert_no_credentials(r)
    # The cache itself is untouched: the Lab still has what it needs server-side.
    stored = roster.get_page("acct1")
    assert stored["password"] == SENTINELS["password"]
    assert stored["email_rule_id"] == SENTINELS["email_rule_id"]


def test_sync_notion_never_returns_the_internal_roster(client, monkeypatch):
    _seed_credentialed()
    _stub_sync(monkeypatch)
    r = client.post("/api/roster/sync-notion")
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] == 1
    assert body["total_in_notion"] == 1
    assert [p["integration_id"] for p in body["pages"]] == ["acct1"]
    _assert_no_credentials(r)


def test_legacy_sync_never_returns_the_internal_roster(client, monkeypatch):
    _seed_credentialed()
    _stub_sync(monkeypatch)
    r = client.post("/api/roster/sync")
    assert r.status_code == 200
    assert r.json()["removed"] == 0
    assert [p["integration_id"] for p in r.json()["pages"]] == ["acct1"]
    _assert_no_credentials(r)


def test_duplicates_and_dedup_never_carry_credentials(client):
    _seed_credentialed("dup1", "Same Name")
    _seed_credentialed("dup2", "same name")
    r = client.get("/api/roster/duplicates")
    assert r.status_code == 200
    assert r.json()["duplicate_names"] == 1
    _assert_no_credentials(r)
    r = client.post("/api/roster/dedup")
    assert r.status_code == 200
    assert r.json()["removed"] == 1
    _assert_no_credentials(r)


def test_sync_status_is_unchanged(client, monkeypatch):
    import services.notion_pages as np

    monkeypatch.setattr(np, "is_configured", lambda: True)
    r = client.get("/api/roster/sync-notion/status")
    assert r.status_code == 200
    assert r.json() == {"configured": True}


# ── DELETE requires the machine credential ───────────────────────────────────


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer wrong-token"},
    {"Authorization": TOKEN},  # no Bearer scheme
    {"X-API-Key": "wrong-token"},
    {"Authorization": "Bearer "},
])
def test_delete_unauthenticated_is_401_and_deletes_nothing(client, headers):
    _seed_credentialed()
    r = client.delete("/api/roster/acct1", headers=headers)
    assert r.status_code == 401
    assert set(r.json()) == {"detail"}
    assert "acct1" not in r.text
    _assert_no_credentials(r)
    assert roster.get_page("acct1") is not None


def test_delete_with_control_plane_bearer_is_unchanged(client):
    _seed_credentialed()
    r = client.delete("/api/roster/acct1", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json() == {"deleted": True}
    assert roster.get_page("acct1") is None
    r = client.delete("/api/roster/acct1", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 404


def test_delete_accepts_x_api_key_and_app_api_key(client, monkeypatch):
    _seed_credentialed("a1")
    _seed_credentialed("a2")
    r = client.delete("/api/roster/a1", headers={"X-API-Key": TOKEN})
    assert r.status_code == 200
    monkeypatch.setenv("APP_API_KEY", "app-key-not-a-secret")
    r = client.delete("/api/roster/a2", headers={"Authorization": "Bearer app-key-not-a-secret"})
    assert r.status_code == 200
    assert roster.list_all_pages() == []


def test_delete_fails_closed_when_no_credential_is_configured(client, monkeypatch):
    monkeypatch.delenv("CONTROL_PLANE_TOKEN", raising=False)
    monkeypatch.delenv("APP_API_KEY", raising=False)
    _seed_credentialed()
    r = client.delete("/api/roster/acct1", headers={"Authorization": "Bearer "})
    assert r.status_code == 503
    assert roster.get_page("acct1") is not None
    r = client.delete("/api/roster/acct1")
    assert r.status_code == 503
    assert roster.get_page("acct1") is not None


def test_non_ascii_credential_is_rejected_not_crashed(client):
    _seed_credentialed()
    r = client.delete("/api/roster/acct1", headers={"X-API-Key": "tökén".encode("utf-8")})
    assert r.status_code == 401
    assert roster.get_page("acct1") is not None
