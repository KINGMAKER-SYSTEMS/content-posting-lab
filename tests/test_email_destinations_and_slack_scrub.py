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


@pytest.mark.real_route_auth
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


@pytest.mark.real_route_auth
def test_destinations_list_fails_closed_without_token(client, cf, monkeypatch):
    monkeypatch.delenv("CONTROL_PLANE_TOKEN", raising=False)
    resp = client.get("/api/email/destinations")
    assert resp.status_code == 503, resp.text
    assert TEAM_INBOX not in resp.text
    assert cf["n"] == 0


@pytest.mark.real_route_auth
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
    monkeypatch.setattr(pipeline, "list_all_pages", lambda: list(store.values()))

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
    "notion_page_id": "abcdefabcdef4abcabcdefabcdefabcd",  # canonical id, not in the roster
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


# ── #20 follow-up (review D1/D2): notion_page_id cannot hijack a live page ───
#
# Real roster file + a fake Notion (fetch/update/email writes) so the whole
# intake -> sync -> /setup chain from the review runs in memory.

import asyncio  # noqa: E402

from fastapi import HTTPException  # noqa: E402

import services.notion_pages as notion_pages  # noqa: E402


# Notion page ids in canonical form (32 lowercase hex); Notion itself returns
# the dashed UUID form, which intake also accepts.
VICTIM_NPID = "5f1c2a3b4d5e46f7a8b9c0d1e2f3a4b5"
OWN_NPID = "0a1b2c3d4e5f40718293a4b5c6d7e8f9"
FRESH_NPID = "9e8d7c6b5a4f43e2d1c0b9a8f7e6d5c4"
DONE_NPID = "11112222333344445555666677778888"


def _dashed(npid):
    return f"{npid[:8]}-{npid[8:12]}-{npid[12:16]}-{npid[16:20]}-{npid[20:]}"


def _row(npid, username, email, status="In Production"):
    return {
        "id": npid,
        "properties": {
            "Account Username": {"title": [{"plain_text": username}]},
            "email": {"email": email},
            "Account Status": {"select": {"name": status}},
            "Pipeline": {"select": {"name": "Flow Stage"}},
        },
    }


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(roster_service, "ROSTER_PATH", tmp_path / "page_roster.json")
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("DEFAULT_INTAKE_PASSWORD", "intake-test-value-not-real")
    notion, writes, calls = {}, [], {"update": 0}

    async def fetch_all():
        return [p for p in (notion_pages.parse_page(v) for v in notion.values()) if p]

    async def update_intake(npid, *, account_username=None, **k):
        calls["update"] += 1
        npid = notion_pages.canonical_notion_page_id(npid)
        if account_username:
            notion[npid]["properties"]["Account Username"] = {
                "title": [{"plain_text": account_username.strip()}]
            }

    async def email_fields(npid, email=None, fwd_address=None):
        npid = notion_pages.canonical_notion_page_id(npid)
        writes.append((npid, email))
        notion[npid]["properties"]["email"] = {"email": email}

    monkeypatch.setattr(notion_pages, "fetch_all_pages", fetch_all)
    monkeypatch.setattr(pipeline, "notion_configured", lambda: True)
    monkeypatch.setattr(pipeline, "update_intake_page", update_intake)
    monkeypatch.setattr(pipeline, "create_intake_page", _boom_async("create_intake_page"))
    monkeypatch.setattr(pipeline, "update_page_email_fields", email_fields)
    monkeypatch.setattr(pipeline, "_mint_random_alias", _boom_async("_mint_random_alias"))
    monkeypatch.setattr(pipeline, "cf_get_config", lambda: {"domain": "alias.example"})
    return notion, writes, calls


def _intake(**fields):
    return asyncio.run(
        pipeline.submit_intake(pipeline.IntakeRequest(**fields), authorization=None, x_api_key=None)
    )


def _seed_victim(notion):
    notion[VICTIM_NPID] = _row(VICTIM_NPID, "victim_page", "victim-real@alias.example")
    asyncio.run(notion_pages.sync_into_roster())
    roster_service.set_page("acct:victim-page", {
        "email_alias": "victim-real@alias.example",
        "email_rule_id": "rule_live",
        "fwd_destination": "henry@team.example",
    })
    return json.dumps(roster_service.get_page("acct:victim-page"), sort_keys=True)


@pytest.mark.parametrize("npid", [VICTIM_NPID, VICTIM_NPID.upper(), _dashed(VICTIM_NPID), f"  {_dashed(VICTIM_NPID)} "])
def test_anonymous_intake_with_live_pages_notion_id_is_refused(world, npid):
    """D1: fresh handle + a live page's notion_page_id must not rename that row."""
    notion, writes, calls = world
    before = _seed_victim(notion)
    with pytest.raises(HTTPException) as exc:
        _intake(
            account_username="fresh-unused-handle",
            notion_page_id=npid,
            email_alias="attacker@evil.example",
            fwd_destination="attacker@evil.example",
        )
    assert exc.value.status_code == 409
    assert "victim" not in exc.value.detail
    assert calls["update"] == 0
    assert notion[VICTIM_NPID]["properties"]["Account Username"]["title"][0]["plain_text"] == "victim_page"
    assert json.dumps(roster_service.get_page("acct:victim-page"), sort_keys=True) == before
    assert roster_service.get_page("acct:fresh-unused-handle") is None


def test_notion_id_hijack_chain_never_reaches_victim_email(world):
    """D1 end to end: intake -> sync -> /setup must not write the attacker alias."""
    notion, writes, calls = world
    _seed_victim(notion)
    with pytest.raises(HTTPException):
        _intake(
            account_username="fresh-unused-handle",
            notion_page_id=VICTIM_NPID,
            email_alias="attacker@evil.example",
            fwd_destination="attacker@evil.example",
        )
    asyncio.run(notion_pages.sync_into_roster())
    assert roster_service.get_page("acct:victim-page")["email_rule_id"] == "rule_live"
    if roster_service.get_page("acct:fresh-unused-handle"):
        try:
            asyncio.run(pipeline.run_setup("acct:fresh-unused-handle"))
        except Exception:
            pass
    assert (VICTIM_NPID, "attacker@evil.example") not in writes
    assert notion[VICTIM_NPID]["properties"]["email"]["email"] == "victim-real@alias.example"


def test_legit_ui_intake_completes_its_own_synced_placeholder(world):
    """D2: step-1 placeholder synced before step 2 with handle == email name."""
    notion, writes, calls = world
    notion[OWN_NPID] = _row(OWN_NPID, "samb-truck-04", "samb-truck-04@alias.example",
                              status="New — Pending Setup")
    asyncio.run(notion_pages.sync_into_roster())
    assert roster_service.get_page("acct:samb-truck-04")
    out = _intake(
        account_username="samb-truck-04",
        notion_page_id=OWN_NPID,
        email_alias="samb-truck-04@alias.example",
        fwd_destination="henry@team.example",
    )
    assert out["ok"] is True
    assert calls["update"] == 1
    assert roster_service.get_page("acct:samb-truck-04")["email_alias"] == "samb-truck-04@alias.example"


def test_placeholder_completion_with_real_handle_is_unchanged(world):
    notion, writes, calls = world
    notion[OWN_NPID] = _row(OWN_NPID, "acct-7gx2k4mz", "acct-7gx2k4mz@alias.example",
                              status="New — Pending Setup")
    asyncio.run(notion_pages.sync_into_roster())
    out = _intake(
        account_username="real.tiktok_handle",
        notion_page_id=OWN_NPID,
        email_alias="acct-7gx2k4mz@alias.example",
        fwd_destination="henry@team.example",
    )
    assert out["ok"] is True


def test_placeholder_alias_cannot_be_repointed_anonymously(world):
    notion, writes, calls = world
    notion[OWN_NPID] = _row(OWN_NPID, "acct-7gx2k4mz", "acct-7gx2k4mz@alias.example",
                              status="New — Pending Setup")
    asyncio.run(notion_pages.sync_into_roster())
    with pytest.raises(HTTPException) as exc:
        _intake(
            account_username="real.tiktok_handle",
            notion_page_id=OWN_NPID,
            email_alias="attacker@evil.example",
        )
    assert exc.value.status_code == 409
    assert calls["update"] == 0


def test_unsynced_placeholder_notion_id_is_unchanged(world):
    notion, writes, calls = world
    notion[FRESH_NPID] = _row(FRESH_NPID, "acct-aaaa1111", "acct-aaaa1111@alias.example",
                                status="New — Pending Setup")  # not synced yet
    out = _intake(
        account_username="brand.new_handle",
        notion_page_id=FRESH_NPID,
        email_alias="acct-aaaa1111@alias.example",
    )
    assert out["ok"] is True


def test_control_plane_token_may_reuse_a_live_notion_id(world):
    notion, writes, calls = world
    _seed_victim(notion)
    out = asyncio.run(pipeline.submit_intake(
        pipeline.IntakeRequest(
            account_username="victim_page", notion_page_id=VICTIM_NPID,
            email_alias="victim-real@alias.example",
        ),
        authorization=f"Bearer {TOKEN}", x_api_key=None,
    ))
    assert out["ok"] is True


def test_n1_renamed_pending_page_is_not_a_placeholder(world):
    """N1: a page step 2 already renamed (name != alias local part) is protected
    even while still New — Pending Setup."""
    notion, writes, calls = world
    notion[DONE_NPID] = _row(DONE_NPID, "real.handle_01", "acct-bbbb2222@alias.example",
                               status="New — Pending Setup")
    asyncio.run(notion_pages.sync_into_roster())
    before = json.dumps(roster_service.list_all_pages(), sort_keys=True)
    with pytest.raises(HTTPException) as exc:
        _intake(
            account_username="fresh-unused-handle",
            notion_page_id=DONE_NPID,
            email_alias="acct-bbbb2222@alias.example",
        )
    assert exc.value.status_code == 409
    assert calls["update"] == 0
    assert json.dumps(roster_service.list_all_pages(), sort_keys=True) == before


# ── unicode / slug-fallback coverage for the handle guard (DS review gap) ────


def test_handle_guard_catches_unicode_that_folds_to_a_live_handle(client, intake_env):
    """KELVIN SIGN lowercases to ASCII 'k': both id forms land on the live page."""
    store, calls = intake_env
    store["acct:kingmaker"] = {"integration_id": "acct:kingmaker", "email_alias": "km@alias.example"}
    resp = client.post("/api/pipeline/intake", json={"account_username": "Kingmaker"})
    assert resp.status_code == 409, resp.text
    assert calls == {"mint": 0, "create": 0, "update": 0}


def test_handle_guard_catches_slug_fallback_collision(client, intake_env):
    """A handle with no slug characters maps to acct:unknown under both id forms;
    an existing acct:unknown page is protected like any other."""
    store, calls = intake_env
    store["acct:unknown"] = {"integration_id": "acct:unknown", "email_alias": "u@alias.example"}
    resp = client.post("/api/pipeline/intake", json={"account_username": "!!!"})
    assert resp.status_code == 409, resp.text
    assert calls == {"mint": 0, "create": 0, "update": 0}


# ── D3 (N8–N10): one canonical Notion page id, checked at the HTTP layer ─────
#
# The fake Notion here is an httpx MockTransport: it records the exact request
# path, so nothing is matched by string equality in a helper.

import httpx as _httpx  # noqa: E402

_RealAsyncClient = _httpx.AsyncClient

_FULLWIDTH = str.maketrans("0123456789", "０１２３４５６７８９")

BYPASS_FORMS = [
    f"{VICTIM_NPID}#x",
    f"nope/../{VICTIM_NPID}",
    f"{VICTIM_NPID}?x=1",
    f"%2e%2e/{VICTIM_NPID}",
    VICTIM_NPID.translate(_FULLWIDTH),
    f"{VICTIM_NPID[:16]} {VICTIM_NPID[16:]}",
]


@pytest.fixture
def http_notion(monkeypatch, tmp_path):
    monkeypatch.setattr(roster_service, "ROSTER_PATH", tmp_path / "page_roster.json")
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("DEFAULT_INTAKE_PASSWORD", "intake-test-value-not-real")
    monkeypatch.setattr(notion_pages, "NOTION_API_KEY", "notion-test-key-not-real")
    monkeypatch.setattr(notion_pages, "NOTION_PAGES_DB", "notion-test-db")
    requests = []

    def handler(request):
        requests.append((request.method, request.url.raw_path.decode()))
        return _httpx.Response(200, json={"object": "page", "id": "x"})

    transport = _httpx.MockTransport(handler)

    def _client(*a, **k):
        k.pop("transport", None)
        return _RealAsyncClient(*a, transport=transport, **k)

    monkeypatch.setattr(notion_pages.httpx, "AsyncClient", _client)

    async def _sync():
        return {"added": 0, "updated": 0}

    monkeypatch.setattr(pipeline, "sync_into_roster", _sync)
    monkeypatch.setattr(pipeline, "_mint_random_alias", _boom_async("_mint_random_alias"))
    roster_service.set_page("acct:victim-page", {
        "name": "victim_page",
        "source": "notion",
        "status": "In Production",
        "notion_page_id": _dashed(VICTIM_NPID),
        "signup_email": "victim-real@alias.example",
        "email_alias": "victim-real@alias.example",
        "email_rule_id": "rule_live",
        "fwd_destination": "henry@team.example",
    })
    return requests


def _victim_state():
    return json.dumps(roster_service.get_page("acct:victim-page"), sort_keys=True)


@pytest.mark.parametrize("npid", BYPASS_FORMS)
def test_n10_non_canonical_notion_id_is_400_with_zero_notion_requests(http_notion, npid):
    before = _victim_state()
    with pytest.raises(HTTPException) as exc:
        _intake(
            account_username="fresh-unused-handle",
            notion_page_id=npid,
            email_alias="attacker@evil.example",
            fwd_destination="attacker@evil.example",
        )
    assert exc.value.status_code == 400
    assert http_notion == []
    assert _victim_state() == before


@pytest.mark.parametrize("npid", BYPASS_FORMS)
def test_n10_non_canonical_notion_id_is_400_even_with_the_token(http_notion, npid):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(pipeline.submit_intake(
            pipeline.IntakeRequest(account_username="victim_page", notion_page_id=npid,
                                   email_alias="victim-real@alias.example"),
            authorization=f"Bearer {TOKEN}", x_api_key=None,
        ))
    assert exc.value.status_code == 400
    assert http_notion == []


def test_n8_the_canonical_id_is_what_reaches_the_notion_url(http_notion):
    """The same canonical value drives the check and the URL: a dashed,
    upper-case id becomes exactly /v1/pages/<32 lowercase hex>."""
    out = asyncio.run(pipeline.submit_intake(
        pipeline.IntakeRequest(account_username="victim_page",
                               notion_page_id=f" {_dashed(VICTIM_NPID).upper()} ",
                               email_alias="victim-real@alias.example"),
        authorization=f"Bearer {TOKEN}", x_api_key=None,
    ))
    assert out["ok"] is True
    assert http_notion == [("PATCH", f"/v1/pages/{VICTIM_NPID}")]


@pytest.mark.parametrize("npid", BYPASS_FORMS + ["", "   "])
def test_n9_patch_page_refuses_non_canonical_ids_before_any_request(http_notion, npid):
    with pytest.raises(ValueError):
        asyncio.run(notion_pages.update_page_email_fields(npid, email="attacker@evil.example"))
    assert http_notion == []


def test_n9_patch_page_uses_the_canonical_form_for_roster_ids(http_notion):
    asyncio.run(notion_pages.update_page_status(_dashed(VICTIM_NPID), "Live"))
    assert http_notion == [("PATCH", f"/v1/pages/{VICTIM_NPID}")]
