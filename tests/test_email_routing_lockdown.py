"""Email routing lockdown: the forwarding account-takeover chain stays closed.

Unauthenticated, three routes chained into a takeover of a page's TikTok mail:
POST /api/email/destinations (add + self-verify an attacker address),
DELETE /api/email/rules/{id} (drop the page's forwarding rule) and
POST /api/email/auto-create (recreate the same account_name-derived alias
forwarding to the attacker). These tests pin:

* all three require CONTROL_PLANE_TOKEN (Bearer or X-API-Key) and ONLY it --
  the browser-bundled APP_API_KEY is refused; missing or wrong credentials
  are 401 and touch neither Cloudflare nor the roster;
  an unset CONTROL_PLANE_TOKEN fails closed (503) even when APP_API_KEY is set;
* destinations are normalised once (ASCII, exactly one '@', lowercased) and
  that same value is both validated and sent to Cloudflare;
* EMAIL_DESTINATION_DOMAINS, when set, refuses destinations outside it;
* auto-create never silently re-points an alias a roster page already records;
* the Pipeline intake mint-alias flow (direct service call) is not gated.
"""

import pytest
from fastapi.testclient import TestClient

import routers.email_routing as r
import routers.pipeline as pipeline_router
from app import app

TOKEN = "lockdown-test-token"
APP_KEY = "lockdown-app-key"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _boom_async(name):
    async def _f(*a, **k):
        raise AssertionError(f"{name} must not be called")
    return _f


def _boom_sync(name):
    def _f(*a, **k):
        raise AssertionError(f"{name} must not be called")
    return _f


def _async(value):
    async def _f(*a, **k):
        return value
    return _f


@pytest.fixture(scope="module")
def client():
    # One app lifespan for the module (startup is slow); handlers resolve the
    # monkeypatched names per request, and headers are passed per call.
    with TestClient(app) as c:
        yield c


@pytest.fixture
def cf_forbidden(monkeypatch):
    """CF configured, but every CF client call and roster touch is a failure."""
    monkeypatch.setattr(r, "get_config", lambda: {"configured": True, "domain": "rt.example"})
    for name in ("add_destination", "create_rule", "delete_rule", "list_rules", "list_destinations"):
        monkeypatch.setattr(r, name, _boom_async(name))
    for name in ("get_page", "set_page", "list_all_pages"):
        # raising=False: list_all_pages is new to this router; lets the same
        # file run (red) against the pre-fix router.
        monkeypatch.setattr(r, name, _boom_sync(name), raising=False)


@pytest.fixture
def token_set(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.delenv("APP_API_KEY", raising=False)
    monkeypatch.delenv("EMAIL_DESTINATION_DOMAINS", raising=False)


def _call(client, route, headers=None):
    if route == "destinations":
        return client.post("/api/email/destinations", json={"email": "evil@attacker.test"}, headers=headers)
    if route == "delete":
        return client.delete("/api/email/rules/rule_victim", headers=headers)
    if route == "auto-create":
        return client.post(
            "/api/email/auto-create",
            json={"integration_id": "i-other", "account_name": "Victim Page", "destination": "evil@attacker.test"},
            headers=headers,
        )
    raise AssertionError(route)


ROUTES = ("destinations", "delete", "auto-create")


# ── (a) auth ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("route", ROUTES)
def test_unauthenticated_is_401_without_side_effects(client, cf_forbidden, token_set, route):
    resp = _call(client, route)
    assert resp.status_code == 401, resp.text


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer wrong"},
        {"X-API-Key": "wrong"},
        {"Authorization": TOKEN},  # not a Bearer scheme
    ],
)
def test_wrong_credential_is_401_without_side_effects(client, cf_forbidden, token_set, route, headers):
    resp = _call(client, route, headers=headers)
    assert resp.status_code == 401, resp.text


@pytest.mark.parametrize("route", ROUTES)
def test_unset_control_plane_token_fails_closed_even_with_app_key(client, cf_forbidden, monkeypatch, route):
    monkeypatch.delenv("CONTROL_PLANE_TOKEN", raising=False)
    monkeypatch.setenv("APP_API_KEY", APP_KEY)
    # APP_API_KEY is read by app.py's middleware at import time only, so it
    # does not gate this request; the route itself must refuse.
    resp = _call(client, route, headers={"X-API-Key": APP_KEY})
    assert resp.status_code == 503, resp.text


@pytest.mark.parametrize("route", ROUTES)
def test_blank_control_plane_token_fails_closed(client, cf_forbidden, monkeypatch, route):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "   ")
    monkeypatch.delenv("APP_API_KEY", raising=False)
    resp = _call(client, route, headers={"Authorization": "Bearer "})
    assert resp.status_code == 503, resp.text


def test_authenticated_add_destination_reaches_cf(client, monkeypatch, token_set):
    monkeypatch.setattr(r, "get_config", lambda: {"configured": True, "domain": "rt.example"})
    seen = {}

    async def _add(email):
        seen["email"] = email
        return {"email": email, "verified": None}

    monkeypatch.setattr(r, "add_destination", _add)
    resp = client.post("/api/email/destinations", json={"email": "ops@risingtides.test"}, headers={"X-API-Key": TOKEN})
    assert resp.status_code == 200, resp.text
    assert seen["email"] == "ops@risingtides.test"


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("header", ["X-API-Key", "Authorization"])
def test_app_api_key_is_refused_even_when_control_plane_token_is_set(
    client, cf_forbidden, monkeypatch, token_set, route, header
):
    """APP_API_KEY ships in the public frontend bundle (VITE_APP_API_KEY); it
    must never unlock the mail-re-pointing routes."""
    monkeypatch.setenv("APP_API_KEY", APP_KEY)
    value = APP_KEY if header == "X-API-Key" else f"Bearer {APP_KEY}"
    resp = _call(client, route, headers={header: value})
    assert resp.status_code == 401, resp.text


def test_status_read_stays_open(client, monkeypatch, token_set):
    """/status (configured + alias domain, no addresses) is still anonymous.
    GET /destinations is gated; see tests/test_email_destinations_and_slack_scrub.py."""
    monkeypatch.setattr(r, "get_config", lambda: {"configured": True, "domain": "rt.example"})
    assert client.get("/api/email/status").status_code == 200


# ── (b) destination domain allowlist ─────────────────────────────────────────


def test_allowlist_refuses_outside_domain_on_add_destination(client, cf_forbidden, token_set, monkeypatch):
    monkeypatch.setenv("EMAIL_DESTINATION_DOMAINS", "risingtidesent.com, @risingtides.test")
    resp = client.post("/api/email/destinations", json={"email": "evil@attacker.test"}, headers=AUTH)
    assert resp.status_code == 403, resp.text


def test_allowlist_refuses_lookalike_subdomain_and_suffix(client, cf_forbidden, token_set, monkeypatch):
    monkeypatch.setenv("EMAIL_DESTINATION_DOMAINS", "risingtidesent.com")
    for email in ("x@evil.risingtidesent.com", "x@risingtidesent.com.evil.test", "risingtidesent.com", "@risingtidesent.com"):
        resp = client.post("/api/email/destinations", json={"email": email}, headers=AUTH)
        assert resp.status_code in (400, 403), (email, resp.text)


KELVIN = "x@\u212aingmaker.com"  # KELVIN SIGN: lowercases to ASCII "k"


@pytest.mark.parametrize(
    "email",
    [KELVIN, "a@evil.test@kingmaker.com", "a b@kingmaker.com", "a@king\tmaker.com", "a@", "@kingmaker.com"],
)
@pytest.mark.parametrize("route", ["destinations", "auto-create"])
def test_malformed_or_non_ascii_destination_is_refused(client, cf_forbidden, token_set, monkeypatch, email, route):
    """What the allowlist checks must be exactly what CF receives: Unicode that
    lowercases into an allowed domain, or a second '@', must not slip past."""
    monkeypatch.setenv("EMAIL_DESTINATION_DOMAINS", "kingmaker.com")
    if route == "destinations":
        resp = client.post("/api/email/destinations", json={"email": email}, headers=AUTH)
    else:
        resp = client.post(
            "/api/email/auto-create",
            json={"integration_id": "i1", "account_name": "Acme", "destination": email},
            headers=AUTH,
        )
    assert resp.status_code in (400, 403), (email, resp.text)


def test_malformed_destination_refused_even_without_allowlist(client, cf_forbidden, token_set):
    for email in (KELVIN, "a@evil.test@kingmaker.com"):
        resp = client.post("/api/email/destinations", json={"email": email}, headers=AUTH)
        assert resp.status_code == 400, (email, resp.text)


def test_allowlist_refuses_outside_domain_on_auto_create(client, cf_forbidden, token_set, monkeypatch):
    monkeypatch.setenv("EMAIL_DESTINATION_DOMAINS", "risingtidesent.com")
    resp = _call(client, "auto-create", headers=AUTH)
    assert resp.status_code == 403, resp.text


def test_allowlist_admits_listed_domain(client, token_set, monkeypatch):
    monkeypatch.setenv("EMAIL_DESTINATION_DOMAINS", "RisingTidesEnt.com")
    monkeypatch.setattr(r, "get_config", lambda: {"configured": True, "domain": "rt.example"})
    sent = {}

    async def _add(email):
        sent["email"] = email
        return {"email": email}

    monkeypatch.setattr(r, "add_destination", _add)
    resp = client.post("/api/email/destinations", json={"email": "  Ops@RisingTidesEnt.com "}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    # CF receives the same normalised value the allowlist validated.
    assert sent["email"] == "ops@risingtidesent.com"


def test_auto_create_sends_normalised_destination_to_cf(client, token_set, monkeypatch):
    monkeypatch.setenv("EMAIL_DESTINATION_DOMAINS", "risingtidesent.com")
    monkeypatch.setattr(r, "get_config", lambda: {"configured": True, "domain": "rt.example"})
    monkeypatch.setattr(r, "list_rules", _async([]))
    monkeypatch.setattr(r, "list_destinations", _async([{"email": "Henry@RisingTidesEnt.com", "verified": "x"}]))
    monkeypatch.setattr(r, "get_page", lambda iid: {"integration_id": iid})
    monkeypatch.setattr(r, "list_all_pages", lambda: [], raising=False)
    saved, created = {}, {}
    monkeypatch.setattr(r, "set_page", lambda iid, fields: saved.update(fields))

    async def _create(alias, destination):
        created["destination"] = destination
        return {"id": "rule_n"}

    monkeypatch.setattr(r, "create_rule", _create)
    resp = client.post(
        "/api/email/auto-create",
        json={"integration_id": "i1", "account_name": "Acme", "destination": " HENRY@risingtidesent.COM "},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert created["destination"] == "henry@risingtidesent.com"
    assert saved["fwd_destination"] == "henry@risingtidesent.com"


def test_allowlist_unset_does_not_block_but_warns(client, token_set, monkeypatch, caplog):
    monkeypatch.setattr(r, "get_config", lambda: {"configured": True, "domain": "rt.example"})
    monkeypatch.setattr(r, "add_destination", _async({"email": "a@b.test"}))
    with caplog.at_level("WARNING", logger="routers.email_routing"):
        resp = client.post("/api/email/destinations", json={"email": "a@b.test"}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert any("EMAIL_DESTINATION_DOMAINS" in rec.getMessage() for rec in caplog.records)


# ── (c) no silent re-pointing of an existing page alias ──────────────────────


@pytest.fixture
def cf_ok(monkeypatch):
    monkeypatch.setattr(r, "get_config", lambda: {"configured": True, "domain": "rt.example"})
    monkeypatch.setattr(r, "list_rules", _async([]))  # victim's rule was deleted
    monkeypatch.setattr(
        r, "list_destinations",
        _async([
            {"email": "henry@risingtidesent.com", "verified": "2026-01-01"},
            {"email": "evil@attacker.test", "verified": "2026-09-25"},
        ]),
    )
    created = {}

    async def _create(alias, destination):
        created["alias"] = alias
        created["destination"] = destination
        return {"id": "rule_new"}

    monkeypatch.setattr(r, "create_rule", _create)
    return created


VICTIM = {
    "integration_id": "i-victim",
    "name": "Victim Page",
    "email_alias": "victim-page@rt.example",
    "email_rule_id": "rule_victim",
    "fwd_destination": "henry@risingtidesent.com",
}


def test_auto_create_refuses_repointing_page_alias_without_replace(client, cf_ok, token_set, monkeypatch):
    monkeypatch.setattr(r, "get_page", lambda iid: dict(VICTIM) if iid == "i-victim" else None)
    monkeypatch.setattr(r, "list_all_pages", lambda: [dict(VICTIM)], raising=False)
    monkeypatch.setattr(r, "set_page", _boom_sync("set_page"))
    resp = client.post(
        "/api/email/auto-create",
        json={"integration_id": "i-victim", "account_name": "Victim Page", "destination": "evil@attacker.test"},
        headers=AUTH,
    )
    assert resp.status_code == 409, resp.text
    assert "replace" in resp.json()["detail"]
    # R-LOW-1: the 409 must not disclose the page's existing alias (the
    # credential scrub strips JSON keys, not free text in "detail").
    assert VICTIM["email_alias"] not in resp.text
    assert cf_ok == {}


def test_auto_create_refuses_alias_owned_by_another_page(client, cf_ok, token_set, monkeypatch):
    """The alias derives from account_name, so an unrelated integration_id must
    not be a way around the per-page check."""
    monkeypatch.setattr(r, "get_page", lambda iid: dict(VICTIM) if iid == "i-victim" else None)
    monkeypatch.setattr(r, "list_all_pages", lambda: [dict(VICTIM)], raising=False)
    monkeypatch.setattr(r, "set_page", _boom_sync("set_page"))
    resp = _call(client, "auto-create", headers=AUTH)  # integration_id i-other
    assert resp.status_code == 409, resp.text
    assert cf_ok == {}


def test_auto_create_same_alias_same_destination_is_allowed(client, cf_ok, token_set, monkeypatch):
    """Healing a deleted rule back to the recorded destination is not a re-point."""
    monkeypatch.setattr(r, "get_page", lambda iid: dict(VICTIM))
    monkeypatch.setattr(r, "list_all_pages", lambda: [dict(VICTIM)], raising=False)
    monkeypatch.setattr(r, "set_page", lambda iid, fields: None)
    resp = client.post(
        "/api/email/auto-create",
        json={"integration_id": "i-victim", "account_name": "Victim Page", "destination": "Henry@RisingTidesEnt.com"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert cf_ok["alias"] == "victim-page"


def test_auto_create_replace_true_re_points_behind_auth(client, cf_ok, token_set, monkeypatch):
    monkeypatch.setattr(r, "get_page", lambda iid: dict(VICTIM))
    monkeypatch.setattr(r, "list_all_pages", lambda: [dict(VICTIM)], raising=False)
    saved = {}
    monkeypatch.setattr(r, "set_page", lambda iid, fields: saved.update(fields))
    resp = client.post(
        "/api/email/auto-create",
        json={
            "integration_id": "i-victim",
            "account_name": "Victim Page",
            "destination": "evil@attacker.test",
            "replace": True,
        },
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert saved["fwd_destination"] == "evil@attacker.test"


def test_auto_create_replace_true_still_requires_auth(client, cf_forbidden, token_set):
    resp = client.post(
        "/api/email/auto-create",
        json={"integration_id": "i-victim", "account_name": "Victim Page", "destination": "evil@attacker.test", "replace": True},
    )
    assert resp.status_code == 401, resp.text


# ── Pipeline intake mint-alias stays ungated ─────────────────────────────────


class _FakeResp:
    def raise_for_status(self):
        return None

    def json(self):
        return {"result": [{"email": "henry@risingtidesent.com", "verified": True}]}


class _FakeAsyncClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        return _FakeResp()


def test_pipeline_mint_alias_unaffected_without_credentials(client, token_set, monkeypatch):
    monkeypatch.setenv("EMAIL_DESTINATION_DOMAINS", "risingtidesent.com")
    monkeypatch.delenv("EMAIL_HANDOFF_DEFAULT", raising=False)
    monkeypatch.setattr(pipeline_router, "_intake_password", lambda: "intake-test")
    monkeypatch.setattr(pipeline_router, "notion_configured", lambda: False)
    monkeypatch.setattr(
        pipeline_router, "cf_get_config",
        lambda: {"configured": True, "account_id": "acct", "token": "tok", "domain": "rt.example"},
    )
    monkeypatch.setattr(pipeline_router.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(pipeline_router, "cf_list_rules", _async([]))
    created = {}

    async def _create(alias_local, destination):
        created["alias_local"] = alias_local
        created["destination"] = destination
        return {"id": "rule-mint"}

    monkeypatch.setattr(pipeline_router, "cf_create_rule", _create)
    resp = client.post("/api/pipeline/mint-alias", json={"desired_local": "samb-truck-99"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["alias"] == "samb-truck-99@rt.example"
    assert created == {"alias_local": "samb-truck-99", "destination": "henry@risingtidesent.com"}


def test_email_route_error_details_never_interpolate_roster_values():
    """A1 source guard (R-LOW-1 class): every HTTPException detail in
    routers/email_routing.py is a literal, or an f-string built only from the
    caller's own normalised destination. Roster-derived values (page, alias,
    rule id, integration id) can never be formatted into error text."""
    import ast
    import inspect

    allowed_names = {"destination"}  # the caller's own normalised input
    tree = ast.parse(inspect.getsource(r))
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "HTTPException"):
            continue
        for kw in node.keywords:
            if kw.arg != "detail":
                continue
            for sub in ast.walk(kw.value):
                if isinstance(sub, ast.FormattedValue):
                    names = {n.id for n in ast.walk(sub.value) if isinstance(n, ast.Name)}
                    if not names <= allowed_names:
                        offenders.append((node.lineno, sorted(names)))
                elif isinstance(sub, ast.Call) and kw.value is sub:
                    # detail=str(e): the CF upstream error (502), never roster data.
                    if not (getattr(sub.func, "id", None) == "str" and len(sub.args) == 1
                            and isinstance(sub.args[0], ast.Name) and sub.args[0].id == "e"):
                        offenders.append((node.lineno, ["<call>"]))
    assert offenders == [], offenders
