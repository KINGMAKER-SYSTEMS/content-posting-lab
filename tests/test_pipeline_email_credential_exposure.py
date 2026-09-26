"""HTTP-level exposure tests for /api/pipeline and /api/email.

Production does not set APP_API_KEY, so both routers answer the public
internet. The roster cache behind them holds account credentials (Notion
signup email + password, forwarding address, Cloudflare alias/rule/destination,
free-text notes). These tests pin:

  * no /api/pipeline or /api/email response carries a seeded credential value
    or key, at any depth, on any route that returns roster data;
  * non-credential fields (names, statuses, poster, R2/Telegram state, has_*
    presence flags) are unchanged;
  * the only credential-shaped keys served are values the request itself
    created (a freshly minted alias) and the team's destination inboxes;
  * the Slack handoff never carries the account password.

HTTP only, so it runs red on origin/main for the real reasons. Notion,
Cloudflare, R2, Telegram and Slack are all stubbed; state lives in tmp_path.
"""

import random

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import services.roster as roster
import services.telegram as tg
from services import json_store
from routers import email_routing as email_router
from routers import pipeline as pipeline_router

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
CF = {"configured": True, "domain": "rt.example", "account_id": "acct", "token": "cf-test"}


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(roster, "ROSTER_PATH", tmp_path / "page_roster.json")
    monkeypatch.setattr(tg, "CONFIG_PATH", tmp_path / "telegram_config.json")
    monkeypatch.setattr(json_store, "_LOCKS", {})
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("APP_API_KEY", raising=False)
    # Intake refuses to run without the operator's intake password (#171);
    # a test value, never a real one.
    monkeypatch.setenv("DEFAULT_INTAKE_PASSWORD", "intake-test-value-not-real")
    # Pipeline externals: nothing configured, nothing reachable.
    monkeypatch.setattr(pipeline_router, "notion_configured", lambda: False)
    monkeypatch.setattr(pipeline_router, "slack_configured", lambda: False)
    monkeypatch.setattr(pipeline_router, "cf_get_config", lambda: dict(CF))
    monkeypatch.setattr(pipeline_router, "resolve_poster_for_page", lambda page: None)
    monkeypatch.setattr(pipeline_router, "get_cookie_status", lambda name: "valid")
    monkeypatch.setattr(pipeline_router.r2, "is_configured", lambda: False)
    yield


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(pipeline_router.router, prefix="/api/pipeline")
    app.include_router(email_router.router, prefix="/api/email")
    return TestClient(app)


def _seed(ig_id="acct1", name="Acct One", status="In Production", **extra):
    roster.set_page(ig_id, {
        "name": name,
        "provider": "tiktok",
        "source": "notion",
        "status": status,
        "pipeline": "Flow Stage",
        "poster_name": "mon",
        "notion_page_id": f"notion-{ig_id}",
        "r2_prefix": f"accounts/{ig_id}/",
        "r2_bucket": "bucket",
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


def _assert_no_credentials(response, allowed_keys=()):
    text = response.text
    for field, sentinel in SENTINELS.items():
        assert sentinel not in text, f"{field} value served by {response.request.method} {response.request.url.path}"
    body = response.json()
    leaked = (CREDENTIAL_KEYS - set(allowed_keys)) & set(_keys_anywhere(body))
    assert not leaked, f"credential keys {sorted(leaked)} in {response.request.url.path}"


# ── /api/pipeline ────────────────────────────────────────────────────────────


def test_stages_never_carry_credentials_and_keep_page_fields(client):
    _seed("a1", "Alpha", status="In Production")
    _seed("a2", "Beta", status="Some Drifted Status")
    r = client.get("/api/pipeline/stages")
    assert r.status_code == 200
    body = r.json()
    in_prod = next(s for s in body["stages"] if s["status"] == "In Production")
    assert in_prod["count"] == 1
    page = in_prod["pages"][0]
    assert (page["integration_id"], page["name"], page["poster_name"], page["pipeline"]) == (
        "a1", "Alpha", "mon", "Flow Stage",
    )
    assert body["unassigned"][0]["_unknown_status"] == "Some Drifted Status"
    assert body["unassigned"][0]["name"] == "Beta"
    assert body["total_pages"] == 2
    _assert_no_credentials(r)


def test_workspace_never_carries_the_password(client, monkeypatch):
    _seed()
    objects = [{"key": "accounts/acct1/v1.mp4", "size": 10, "last_modified": None, "filename": "v1.mp4"}]
    monkeypatch.setattr(pipeline_router.r2, "is_configured", lambda: True)
    monkeypatch.setattr(pipeline_router.r2, "list_account_objects", lambda iid, max_keys=500: list(objects))
    r = client.get("/api/pipeline/acct1/workspace")
    assert r.status_code == 200
    body = r.json()
    assert body["page"]["name"] == "Acct One"
    assert body["page"]["r2_prefix"] == "accounts/acct1/"
    assert body["r2"]["prefix"] == "accounts/acct1/"
    assert body["r2"]["objects"] == objects
    assert body["r2"]["object_count"] == 1
    assert body["cookie_status"] == "valid"
    assert body["telegram"]["poster_id"] is None
    _assert_no_credentials(r)


def test_health_keeps_presence_flags_only(client):
    _seed()
    r = client.get("/api/pipeline/acct1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["has_email_alias"] is True
    assert body["has_r2_prefix"] is True
    _assert_no_credentials(r)


def test_transition_never_carries_credentials(client):
    _seed()
    r = client.post("/api/pipeline/acct1/transition", json={"status": "Live"})
    assert r.status_code == 200
    assert r.json()["page"]["status"] == "Live"
    _assert_no_credentials(r)


def test_setup_never_carries_credentials(client):
    _seed(status="New — Pending Setup")
    r = client.post("/api/pipeline/acct1/setup", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["completed"] is True
    assert body["steps"]["cf_alias"] == {"ok": True, "skipped": True}
    assert body["steps"]["notion_email_writeback"]["ok"] is True
    assert body["steps"]["slack_handoff"]["skipped"] is True
    assert body["page"]["status"] == "In Production"
    _assert_no_credentials(r)


def test_mint_alias_echoes_only_the_alias_it_minted(client, monkeypatch):
    _seed()

    async def fake_mint(pipeline=None, destination_override=None, desired_local=None):
        return {"alias": "fresh-01@rt.example", "rule_id": "new-rule", "destination": "team@rt.example"}

    monkeypatch.setattr(pipeline_router, "_mint_random_alias", fake_mint)
    r = client.post("/api/pipeline/mint-alias", json={"desired_local": "fresh-01"})
    assert r.status_code == 200
    assert r.json() == {"alias": "fresh-01@rt.example", "destination": "team@rt.example", "notion_page_id": None}
    _assert_no_credentials(r)


def test_intake_echoes_its_own_alias_and_not_the_synced_roster(client, monkeypatch):
    _seed()

    async def fake_update(*a, **k):
        return {}

    async def fake_sync():
        return {"added": 1, "updated": 0, "errors": [], "pages": roster.list_all_pages()}

    monkeypatch.setattr(pipeline_router, "notion_configured", lambda: True)
    monkeypatch.setattr(pipeline_router, "update_intake_page", fake_update)
    monkeypatch.setattr(pipeline_router, "sync_into_roster", fake_sync)
    r = client.post("/api/pipeline/intake", json={
        "account_username": "newhandle",
        "email_alias": "fresh-02@rt.example",
        "fwd_destination": "team@rt.example",
        "notion_page_id": "notion-new",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["email_alias"] == "fresh-02@rt.example"
    assert body["fwd_destination"] == "team@rt.example"
    assert body["synced"] is True
    _assert_no_credentials(r, allowed_keys={"email_alias", "fwd_destination"})


# ── /api/email ───────────────────────────────────────────────────────────────


@pytest.fixture
def cf(monkeypatch, client):
    # The email mutation routes (and, after #177, the destinations list) require
    # CONTROL_PLANE_TOKEN since #176; these exposure tests exercise the
    # authenticated path. Anonymous refusal is pinned in
    # tests/test_email_routing_lockdown.py.
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "exposure-test-token")
    client.headers["Authorization"] = "Bearer exposure-test-token"
    created = {}

    async def list_rules():
        return []

    async def list_destinations():
        return [{"email": "team@rt.example", "verified": "2026-01-01T00:00:00Z"}]

    async def create_rule(alias, destination):
        created["rule"] = {"id": "new-rule-1", "name": f"Roster: {alias}"}
        return created["rule"]

    async def delete_rule(rule_id):
        return None

    async def add_destination(email):
        return {"email": email, "verified": None}

    monkeypatch.setattr(email_router, "get_config", lambda: dict(CF))
    monkeypatch.setattr(email_router, "list_rules", list_rules)
    monkeypatch.setattr(email_router, "list_destinations", list_destinations)
    monkeypatch.setattr(email_router, "create_rule", create_rule)
    monkeypatch.setattr(email_router, "delete_rule", delete_rule)
    monkeypatch.setattr(email_router, "add_destination", add_destination)
    return created


def test_auto_create_returns_the_new_alias_but_no_page_credentials(client, cf):
    _seed()
    # The seeded page already records an alias, so since #176 re-pointing it
    # needs an explicit replace (no silent re-pointing).
    r = client.post("/api/email/auto-create", json={
        "integration_id": "acct1", "account_name": "New Name", "destination": "team@rt.example",
        "replace": True,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["alias"] == "new-name@rt.example"
    assert body["rule"]["id"] == "new-rule-1"
    assert body["page"]["integration_id"] == "acct1"
    assert "email_alias" not in body["page"] and "fwd_destination" not in body["page"]
    _assert_no_credentials(r)
    # The cache still holds the new alias for the server-side flows.
    assert roster.get_page("acct1")["email_alias"] == "new-name@rt.example"


def test_auto_create_for_unknown_page_returns_null_page(client, cf):
    r = client.post("/api/email/auto-create", json={
        "integration_id": "ghost", "account_name": "Ghost", "destination": "team@rt.example",
    })
    assert r.status_code == 200
    assert r.json()["page"] is None


def test_destinations_status_and_rule_delete_are_unchanged(client, cf):
    _seed(email_rule_id="rule_99")
    r = client.get("/api/email/destinations")
    assert r.status_code == 200
    assert r.json() == {"destinations": [{"email": "team@rt.example", "verified": "2026-01-01T00:00:00Z"}]}
    r = client.post("/api/email/destinations", json={"email": "new@rt.example"})
    assert r.json() == {"destination": {"email": "new@rt.example", "verified": None}}
    r = client.get("/api/email/status")
    assert r.json() == {"configured": True, "domain": "rt.example"}
    r = client.delete("/api/email/rules/rule_99?integration_id=acct1")
    assert r.json() == {"deleted": True}
    _assert_no_credentials(r)


# ── property: arbitrary credential columns never cross ───────────────────────


@pytest.mark.parametrize("seed", range(10))
def test_generated_credential_columns_never_cross(client, seed):
    from tests.test_roster_public_shape import _credential_variant

    rng = random.Random(7000 + seed)
    extra = {}
    for i in range(12):
        extra[_credential_variant(rng)] = f"GENERATED-SECRET-{seed}-{i}"
    raw = roster.load_roster()
    raw["pages"]["gen"] = {
        "integration_id": "gen", "name": "Gen", "status": "Live", "pipeline": "Flow Stage",
        **SENTINELS, **extra,
    }
    roster.save_roster(raw)
    for method, path in (
        ("get", "/api/pipeline/stages"),
        ("get", "/api/pipeline/gen/workspace"),
        ("get", "/api/pipeline/gen/health"),
        ("post", "/api/pipeline/gen/transition"),
    ):
        kwargs = {"json": {"status": "Live"}} if method == "post" else {}
        r = getattr(client, method)(path, **kwargs)
        assert r.status_code == 200, path
        assert "GENERATED-SECRET" not in r.text, path
        _assert_no_credentials(r)


# ── Slack handoff ────────────────────────────────────────────────────────────


def test_slack_handoff_never_carries_the_password(monkeypatch):
    import asyncio

    import services.slack as slack

    sent = {}

    async def fake_post(text, blocks=None, webhook_url=None):
        sent["text"] = text
        sent["blocks"] = blocks
        return {"ok": True}

    monkeypatch.setattr(slack, "post_message", fake_post)
    monkeypatch.setattr(slack, "_webhook_for_pipeline", lambda p: "https://hooks.example.invalid/x")
    page = {"name": "acct1", "pipeline": "Flow Stage", "notion_page_id": "abc", **SENTINELS}
    assert asyncio.run(slack.post_pipeline_handoff(page)) == {"ok": True}
    everything = sent["text"] + repr(sent["blocks"])
    assert SENTINELS["password"] not in everything
    assert "see Notion" in everything
    # The handle still reaches the recipient; since #177 the login email and
    # notes do not (they point at Notion like the password).
    assert "acct1" in everything
    for key in ("email_alias", "signup_email", "notes"):
        assert SENTINELS[key] not in everything, key


def test_rule_mismatch_409_does_not_echo_the_linked_rule_id(client, cf):
    _seed()
    r = client.delete("/api/email/rules/rule_WRONG?integration_id=acct1")
    assert r.status_code == 409
    assert "does not belong" in r.json()["detail"]
    assert SENTINELS["email_rule_id"] not in r.text
    assert roster.get_page("acct1")["email_rule_id"] == SENTINELS["email_rule_id"]
