"""Follow-up to the email-routing lockdown (#176).

1. GET /api/email/destinations lists the team's real forwarding inboxes. It
   now requires CONTROL_PLANE_TOKEN (fail closed, APP_API_KEY refused) and
   never reaches Cloudflare for an anonymous caller.
2. The Slack pipeline handoff carries no login email, password or free-text
   notes -- only pointers to Notion.
"""

import json

import pytest
from fastapi.testclient import TestClient

import routers.email_routing as r
import services.slack as slack
from app import app

TOKEN = "dest-slack-test-token"
TEAM_INBOX = "ops-inbox@team.example"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def cf(monkeypatch):
    monkeypatch.setattr(r, "get_config", lambda: {"configured": True, "domain": "rt.example"})
    calls = {"n": 0}

    async def _list():
        calls["n"] += 1
        return [{"email": TEAM_INBOX, "verified": "2026-01-01"}]

    monkeypatch.setattr(r, "list_destinations", _list)
    return calls


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"X-API-Key": "browser-bundle-key"}],
)
def test_destinations_list_refuses_anonymous_and_leaks_nothing(client, cf, monkeypatch, headers):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("APP_API_KEY", "browser-bundle-key")
    resp = client.get("/api/email/destinations", headers=headers)
    assert resp.status_code == 401, resp.text
    assert TEAM_INBOX not in resp.text
    assert cf["n"] == 0


def test_destinations_list_fails_closed_without_token(client, cf, monkeypatch):
    monkeypatch.delenv("CONTROL_PLANE_TOKEN", raising=False)
    resp = client.get("/api/email/destinations")
    assert resp.status_code == 503, resp.text
    assert TEAM_INBOX not in resp.text
    assert cf["n"] == 0


def test_destinations_list_served_with_control_plane_token(client, cf, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    resp = client.get("/api/email/destinations", headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["destinations"][0]["email"] == TEAM_INBOX


# ── Slack handoff ────────────────────────────────────────────────────────────

PAGE = {
    "integration_id": "i-slack",
    "name": "samb.truck.04",
    "pipeline": "Flow Stage",
    "email_alias": "samb-truck-04@alias.example",
    "signup_email": "signup-secret@alias.example",
    "password": "Hunter2-Secret!",
    "notes": "backup codes 1234-5678; recovery phone +1 555 0100",
    "poster_name": "Poster P",
    "sounds_reference": "sound-ref",
    "notion_page_id": "abc-def",
}


async def test_slack_handoff_carries_no_email_password_or_notes(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL_FLOW_STAGE", "https://hooks.slack.invalid/T/B/X")
    sent = {}

    async def _post(text, blocks=None, *, webhook_url=None):
        sent["payload"] = {"text": text, "blocks": blocks}
        return {"ok": True, "error": None}

    monkeypatch.setattr(slack, "post_message", _post)
    result = await slack.post_pipeline_handoff(dict(PAGE))
    assert result["ok"] is True

    body = json.dumps(sent["payload"])
    for secret in (
        PAGE["email_alias"],
        PAGE["signup_email"],
        "samb-truck-04@",
        PAGE["password"],
        "backup codes",
        "555 0100",
    ):
        assert secret not in body, secret
    # The handoff still works: handle, poster and Notion pointers survive.
    assert PAGE["name"] in body
    assert "Poster P" in body
    assert "see Notion" in body
    assert "notion.so/abcdef" in body


# ── #20: anonymous /intake cannot rewrite a live page's email linkage ────────

import routers.pipeline as pipeline  # noqa: E402
import services.roster as roster_service  # noqa: E402

LIVE = {
    "integration_id": "acct:samb-truck-04",
    "name": "samb.truck_04",
    "email_alias": "samb-truck-04@alias.example",
    "email_rule_id": "rule_live",
    "fwd_destination": "henry@team.example",
}


def _boom_async(name):
    async def _f(*a, **k):
        raise AssertionError(f"{name} must not run")
    return _f


def _boom_sync(name):
    def _f(*a, **k):
        raise AssertionError(f"{name} must not run")
    return _f


@pytest.fixture
def intake_env(monkeypatch):
    """Notion configured, a live page in an in-memory roster, CF/Notion stubbed."""
    monkeypatch.setenv("DEFAULT_INTAKE_PASSWORD", "intake-test-value-not-real")
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setattr(pipeline, "notion_configured", lambda: True)
    store = {LIVE["integration_id"]: dict(LIVE)}
    monkeypatch.setattr(pipeline, "get_page", lambda iid: store.get(iid))

    def _set_page(iid, fields):
        store.setdefault(iid, {"integration_id": iid}).update(fields)
        return store[iid]

    monkeypatch.setattr(roster_service, "set_page", _set_page)
    calls = {"mint": 0, "create": 0, "update": 0}

    async def _mint(**k):
        calls["mint"] += 1
        return {"alias": "acct-new@alias.example", "destination": "henry@team.example", "rule_id": "rule_new"}

    async def _create(**k):
        calls["create"] += 1
        return {"id": "notion-new"}

    async def _update(*a, **k):
        calls["update"] += 1

    async def _sync():
        return {"added": 1, "updated": 0}

    monkeypatch.setattr(pipeline, "_mint_random_alias", _mint)
    monkeypatch.setattr(pipeline, "create_intake_page", _create)
    monkeypatch.setattr(pipeline, "update_intake_page", _update)
    monkeypatch.setattr(pipeline, "sync_into_roster", _sync)
    return store, calls


ATTACK = {
    "email_alias": "attacker@evil.example",
    "fwd_destination": "attacker@evil.example",
    "notion_page_id": "victim-notion-row",
}


@pytest.mark.parametrize("handle", ["samb.truck_04", "SAMB.TRUCK_04", " samb-truck-04 ", "samb_truck.04"])
def test_anonymous_intake_for_live_handle_is_refused_without_side_effects(client, intake_env, handle):
    store, calls = intake_env
    before = json.dumps(store, sort_keys=True)
    resp = client.post("/api/pipeline/intake", json={"account_username": handle, **ATTACK})
    assert resp.status_code == 409, resp.text
    assert "samb" not in resp.json()["detail"]
    assert calls == {"mint": 0, "create": 0, "update": 0}
    assert json.dumps(store, sort_keys=True) == before


def test_intake_for_live_handle_with_browser_key_is_refused(client, intake_env, monkeypatch):
    store, calls = intake_env
    monkeypatch.setenv("APP_API_KEY", "browser-bundle-key")
    before = json.dumps(store, sort_keys=True)
    resp = client.post(
        "/api/pipeline/intake",
        json={"account_username": "samb.truck_04", **ATTACK},
        headers={"X-API-Key": "browser-bundle-key"},
    )
    assert resp.status_code == 409, resp.text
    assert json.dumps(store, sort_keys=True) == before


def test_intake_for_live_handle_with_control_plane_token_proceeds(client, intake_env):
    store, calls = intake_env
    resp = client.post(
        "/api/pipeline/intake",
        json={"account_username": "samb-truck-04", "email_alias": "ops-new@alias.example"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert resp.status_code == 200, resp.text
    assert store["acct:samb-truck-04"]["email_alias"] == "ops-new@alias.example"


def test_anonymous_intake_for_new_handle_is_unchanged(client, intake_env):
    store, calls = intake_env
    resp = client.post("/api/pipeline/intake", json={"account_username": "brand.new_handle"})
    assert resp.status_code == 200, resp.text
    assert calls["mint"] == 1 and calls["create"] == 1
    assert store["acct:brand.new_handle"]["email_alias"] == "acct-new@alias.example"
    assert store["acct:brand.new_handle"]["email_rule_id"] == "rule_new"


# ── #14: 409s are not alias-existence oracles ────────────────────────────────


def test_mint_alias_collision_does_not_echo_alias(client, monkeypatch):
    monkeypatch.setenv("DEFAULT_INTAKE_PASSWORD", "intake-test-value-not-real")
    monkeypatch.setattr(pipeline, "notion_configured", lambda: False)
    monkeypatch.setattr(
        pipeline, "cf_get_config",
        lambda: {"configured": True, "account_id": "a", "token": "t", "domain": "alias.example"},
    )

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": [{"email": "henry@risingtidesent.com", "verified": True}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(pipeline.httpx, "AsyncClient", _Client)
    monkeypatch.delenv("EMAIL_HANDOFF_DEFAULT", raising=False)

    async def _rules():
        return [{"matchers": [{"value": "victim-alias@alias.example"}]}]

    monkeypatch.setattr(pipeline, "cf_list_rules", _rules)
    monkeypatch.setattr(pipeline, "cf_create_rule", _boom_async("cf_create_rule"))
    resp = client.post("/api/pipeline/mint-alias", json={"desired_local": "victim-alias"})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "already taken" in detail
    assert "victim-alias" not in detail and "alias.example" not in detail


def _email_cf(monkeypatch, rules):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.delenv("EMAIL_DESTINATION_DOMAINS", raising=False)
    monkeypatch.setattr(r, "get_config", lambda: {"configured": True, "domain": "rt.example"})

    async def _rules():
        return rules

    monkeypatch.setattr(r, "list_rules", _rules)
    monkeypatch.setattr(r, "create_rule", _boom_async("create_rule"))


def test_auto_create_collision_does_not_echo_alias(client, monkeypatch):
    _email_cf(monkeypatch, [{"matchers": [{"value": "victim-page@rt.example"}]}])
    monkeypatch.setattr(r, "get_page", lambda iid: None)
    monkeypatch.setattr(r, "list_all_pages", lambda: [])
    resp = client.post(
        "/api/email/auto-create",
        json={"integration_id": "i1", "account_name": "Victim Page", "destination": "henry@team.example"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "already exists" in detail
    assert "victim-page" not in detail and "rt.example" not in detail


@pytest.mark.parametrize("integration_id", ["i-victim", "i-other"])
def test_auto_create_repoint_409_does_not_echo_recorded_alias(client, monkeypatch, integration_id):
    _email_cf(monkeypatch, [])
    victim = {
        "integration_id": "i-victim",
        "email_alias": "victim-page@rt.example",
        "fwd_destination": "henry@team.example",
    }
    monkeypatch.setattr(r, "get_page", lambda iid: dict(victim) if iid == "i-victim" else None)
    monkeypatch.setattr(r, "list_all_pages", lambda: [dict(victim)])
    resp = client.post(
        "/api/email/auto-create",
        json={"integration_id": integration_id, "account_name": "Victim Page", "destination": "other@team.example"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "victim-page" not in detail and "i-victim" not in detail
