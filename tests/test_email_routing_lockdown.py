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
* the Pipeline mint-alias flow calls the CF service directly; it is gated by
  operator (Access) auth, not by this token.
"""

import pytest
from fastapi.testclient import TestClient

import routers.email_routing as r
import routers.pipeline as pipeline_router
from app import app

# The real route-auth dependency (not the conftest bypass): these tests pin who may call.
pytestmark = pytest.mark.real_route_auth

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


# ── Pipeline mint-alias: operator (Access) only, not the email-routing token ──


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


def test_pipeline_mint_alias_needs_an_operator_not_the_email_token(client, token_set, monkeypatch):
    from tests import route_auth_support as ras

    monkeypatch.setenv("LAB_ACCESS_TEAM_DOMAIN", ras.TEAM_DOMAIN)
    monkeypatch.setenv("LAB_ACCESS_AUD", ras.ACCESS_AUD)
    ras.install_idp(monkeypatch)
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
    body = {"desired_local": "samb-truck-99"}
    assert client.post("/api/pipeline/mint-alias", json=body).status_code == 401
    assert client.post(
        "/api/pipeline/mint-alias", json=body, headers={"Authorization": f"Bearer {TOKEN}"}
    ).status_code == 401
    assert created == {}
    resp = client.post("/api/pipeline/mint-alias", json=body, headers=ras.valid_headers("access"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["alias"] == "samb-truck-99@rt.example"
    assert created == {"alias_local": "samb-truck-99", "destination": "henry@risingtidesent.com"}


# ── A1 source guard (R-LOW-1 class): error text is an ALLOWLIST ──────────────
#
# Purpose: catch ACCIDENTAL disclosure of roster values (another page's alias,
# rule id, integration id) in error text written in routers/email_routing.py.
# It is a lint for honest mistakes, not a defence against a hostile author.
#
# An error message (HTTPException detail, keyword or positional) is accepted
# only if it is:
#   * a plain string literal;
#   * an f-string whose only interpolated name is ``destination``, in a
#     function where ``destination`` is bound exactly once-per-assignment by
#     ``destination = _require_allowed_destination(req.<destination|email>)``
#     (``req`` being that function's own, never re-bound, request-body
#     parameter) and by no other binding form (parameter, lambda parameter,
#     comprehension target, except-as, import-as, walrus, tuple unpack, for,
#     with, match capture, global/nonlocal);
#   * exactly ``str(e)`` inside ``_cf_upstream``'s ``except Exception as e``
#     handler, with ``e`` not re-bound anywhere in that handler.
# The name HTTPException (and any import alias of it) may only appear as the
# callee of a direct call or as an ``except`` type; any other use (subclass
# base, assignment, walrus/tuple rebind, container, partial, argument) is
# flagged, as is the string "HTTPException" (getattr). Any *Response class,
# including one imported under an alias, is flagged wherever it is used; so
# is ``**kwargs`` to HTTPException and a dict literal with a "detail" key.
#
# Limits (shapes this guard does not see):
#   * File scope only: routers/email_routing.py. Messages built in other
#     modules (the auth dependency in services/roster_public.py, the Cloudflare
#     client whose exception text becomes the 502 ``str(e)``) are not checked;
#     a roster value raised inside ``with _cf_upstream():`` would surface
#     through that 502.
#   * Dynamic dispatch the AST cannot resolve: e.g. ``getattr`` with a computed
#     name, ``importlib``/``__import__``, ``eval``/``exec``, objects passed in
#     from other modules.
#   * Custom exception classes turned into responses by an app-level
#     ``exception_handler`` (none exists in app.py today).
#   * Response headers (``HTTPException(..., headers=...)``) and anything
#     other than the error message text.

import ast as _ast  # noqa: E402
import inspect as _inspect  # noqa: E402


def _bindings(scope, name: str) -> list:
    """Every node in ``scope`` that binds ``name`` (any binding form)."""
    found = []
    for node in _ast.walk(scope):
        if isinstance(node, _ast.Name) and node.id == name and isinstance(node.ctx, (_ast.Store, _ast.Del)):
            found.append(node)
        elif isinstance(node, _ast.arg) and node.arg == name:
            found.append(node)
        elif isinstance(node, _ast.ExceptHandler) and node.name == name:
            found.append(node)
        elif isinstance(node, _ast.alias) and (node.asname or node.name.split(".")[0]) == name:
            found.append(node)
        elif isinstance(node, (_ast.Global, _ast.Nonlocal)) and name in node.names:
            found.append(node)
        elif hasattr(_ast, "MatchAs") and isinstance(node, (_ast.MatchAs, _ast.MatchStar)) and node.name == name:
            found.append(node)
    return found


def _error_text_offenders(source: str) -> list[tuple[int, str]]:
    tree = _ast.parse(source)
    parents: dict = {}
    for node in _ast.walk(tree):
        for child in _ast.iter_child_nodes(node):
            parents[child] = node

    exc_names = {"HTTPException"}
    response_names: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.ImportFrom, _ast.Import)):
            for alias in node.names:
                base = alias.name.split(".")[-1]
                if base == "HTTPException" and alias.asname:
                    exc_names.add(alias.asname)
                if base.endswith("Response"):
                    response_names.add(alias.asname or base)

    offenders: list[tuple[int, str]] = []

    def ancestors(node):
        while node in parents:
            node = parents[node]
            yield node

    def enclosing_function(node):
        return next((a for a in ancestors(node) if isinstance(a, (_ast.FunctionDef, _ast.AsyncFunctionDef))), None)

    def is_request_field(call, fn) -> bool:
        if len(call.args) != 1 or call.keywords:
            return False
        arg = call.args[0]
        if not (isinstance(arg, _ast.Attribute) and arg.attr in {"destination", "email"}
                and isinstance(arg.value, _ast.Name)):
            return False
        req = arg.value.id
        params = [a.arg for a in fn.args.posonlyargs + fn.args.args]
        return bool(params) and params[0] == req and len(_bindings(fn, req)) == 1  # only the parameter

    def destination_is_own_input(node) -> bool:
        fn = enclosing_function(node)
        if fn is None:
            return False
        binds = _bindings(fn, "destination")
        if not binds:
            return False
        for b in binds:
            stmt = parents.get(b)
            if not (isinstance(b, _ast.Name) and isinstance(b.ctx, _ast.Store)
                    and isinstance(stmt, _ast.Assign) and stmt.targets == [b]
                    and isinstance(stmt.value, _ast.Call)
                    and getattr(stmt.value.func, "id", None) == "_require_allowed_destination"
                    and is_request_field(stmt.value, fn)):
                return False
        return True

    def is_cf_upstream_str_e(node) -> bool:
        if not (isinstance(node, _ast.Call) and getattr(node.func, "id", None) == "str"
                and len(node.args) == 1 and not node.keywords
                and isinstance(node.args[0], _ast.Name) and node.args[0].id == "e"):
            return False
        handler = next((a for a in ancestors(node) if isinstance(a, _ast.ExceptHandler)), None)
        fn = enclosing_function(node)
        if not (handler is not None and handler.name == "e"
                and isinstance(handler.type, _ast.Name) and handler.type.id == "Exception"
                and fn is not None and fn.name == "_cf_upstream"):
            return False
        # ``e`` must be bound only by the handler itself.
        return all(not _bindings(stmt, "e") for stmt in handler.body)

    def allowed(value) -> bool:
        if isinstance(value, _ast.Constant) and isinstance(value.value, str):
            return True
        if isinstance(value, _ast.JoinedStr):
            return all(
                isinstance(part, _ast.Constant)
                or (isinstance(part, _ast.FormattedValue) and isinstance(part.value, _ast.Name)
                    and part.value.id == "destination" and part.format_spec is None
                    and destination_is_own_input(value))
                for part in value.values
            )
        return is_cf_upstream_str_e(value)

    for node in _ast.walk(tree):
        if isinstance(node, _ast.Constant) and node.value == "HTTPException":
            offenders.append((node.lineno, "HTTPException by string"))
        name = node.id if isinstance(node, _ast.Name) else node.attr if isinstance(node, _ast.Attribute) else None
        if isinstance(name, str) and (name.endswith("Response") or name in response_names):
            offenders.append((node.lineno, f"raw response class {name}"))
        if isinstance(name, str) and name in exc_names:
            parent = parents.get(node)
            direct_call = isinstance(parent, _ast.Call) and parent.func is node
            except_type = isinstance(parent, _ast.ExceptHandler) and parent.type is node or (
                isinstance(parent, _ast.Tuple) and isinstance(parents.get(parent), _ast.ExceptHandler)
            )
            if not (direct_call or except_type):
                offenders.append((node.lineno, f"indirect use of {name}"))
        if isinstance(node, _ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, _ast.Constant) and key.value == "detail" and not allowed(value):
                    offenders.append((node.lineno, "dict detail"))
        if not isinstance(node, _ast.Call):
            continue
        fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if fname not in exc_names:
            continue
        if any(kw.arg is None for kw in node.keywords):
            offenders.append((node.lineno, "**kwargs"))
        messages = [kw.value for kw in node.keywords if kw.arg == "detail"] + node.args[1:2]
        offenders += [(node.lineno, "detail") for m in messages if not allowed(m)]
    return offenders


def test_email_route_error_details_never_interpolate_roster_values():
    """A1: every error message in routers/email_routing.py is on the allowlist.

    Guards against ACCIDENTAL leaks of roster values in this file's error
    text; see the Limits in the comment block above for what it cannot see.
    """
    assert _error_text_offenders(_inspect.getsource(r)) == []


_GUARD_PRELUDE = """
from fastapi import HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
import fastapi
import functools

def _require_allowed_destination(x):
    return x
"""

_GUARD_MUTATIONS = {
    "f-string of page": 'def f(page):\n    raise HTTPException(status_code=409, detail=f"x {page}")',
    "concatenation": 'def f(page):\n    raise HTTPException(status_code=409, detail="x " + page["email_alias"])',
    "percent": 'def f(alias):\n    raise HTTPException(status_code=409, detail="x %s" % alias)',
    "variable built earlier": 'def f(alias):\n    msg = f"{alias}"\n    raise HTTPException(status_code=409, detail=msg)',
    "helper call as message": 'def f(page):\n    raise HTTPException(status_code=409, detail=fmt(page))',
    "helper result in variable": 'def f(page):\n    m = fmt(page)\n    raise HTTPException(status_code=409, detail=m)',
    "JSONResponse": 'def f(alias):\n    return JSONResponse({"detail": alias}, status_code=409)',
    "PlainTextResponse": 'def f(alias):\n    return PlainTextResponse(alias, status_code=409)',
    "positional message": 'def f(alias):\n    raise HTTPException(409, f"{alias}")',
    "qualified fastapi.HTTPException": 'def f(alias):\n    raise fastapi.HTTPException(status_code=409, detail=alias)',
    "aliased import": 'from fastapi import HTTPException as HE\ndef f(alias):\n    raise HE(status_code=409, detail=alias)',
    "rebound name": 'E = HTTPException\ndef f(alias):\n    raise E(status_code=409, detail=alias)',
    "**kwargs": 'def f(alias):\n    raise HTTPException(**{"status_code": 409, "detail": alias})',
    "plain dict": 'def f(alias):\n    return {"detail": alias}',
    "reassigned destination": (
        'def f(page, req):\n    destination = _require_allowed_destination(req)\n'
        '    destination = page["email_alias"]\n'
        '    raise HTTPException(status_code=422, detail=f"{destination}")'
    ),
    "destination as parameter": 'def f(destination):\n    raise HTTPException(status_code=422, detail=f"{destination}")',
    "fake str(e) outside _cf_upstream": 'def f(page):\n    e = page["email_alias"]\n    raise HTTPException(status_code=502, detail=str(e))',
    "str(e) in _cf_upstream but not the Exception handler": (
        'def _cf_upstream(page):\n    e = page["email_alias"]\n'
        '    raise HTTPException(status_code=502, detail=str(e))'
    ),
    # Round 2 (review of e3697c5):
    "walrus rebind of HTTPException": 'def f(alias):\n    (E := HTTPException)\n    raise E(409, alias)',
    "tuple rebind of HTTPException": 'E, _X = HTTPException, None\ndef f(alias):\n    raise E(409, alias)',
    "HTTPException in a container": 'EXC = {"c": HTTPException}\ndef f(alias):\n    raise EXC["c"](409, alias)',
    "functools.partial keyword": 'def f(alias):\n    raise functools.partial(HTTPException, status_code=409, detail=alias)()',
    "functools.partial positional": 'def f(alias):\n    raise functools.partial(HTTPException, 409)(alias)',
    "getattr by string": 'def f(alias):\n    raise getattr(fastapi, "HTTPException")(409, alias)',
    "subclass": 'class AliasConflict(HTTPException):\n    pass\ndef f(alias):\n    raise AliasConflict(409, alias)',
    "subclass super().__init__": (
        'class C(HTTPException):\n    def __init__(self, p):\n'
        '        super().__init__(409, p["email_alias"])\ndef f(p):\n    raise C(p)'
    ),
    "lambda parameter named destination": (
        'def f(req, alias):\n    destination = _require_allowed_destination(req.destination)\n'
        '    raise (lambda destination: HTTPException(409, f"{destination}"))(alias)'
    ),
    "comprehension target destination": (
        'def f(req, alias):\n    destination = _require_allowed_destination(req.destination)\n'
        '    raise next(HTTPException(409, f"{destination}") for destination in [alias])'
    ),
    "except-as destination": (
        'def f(req):\n    destination = _require_allowed_destination(req.destination)\n'
        '    try:\n        pass\n    except Exception as destination:\n'
        '        raise HTTPException(409, f"{destination}")'
    ),
    "import-as destination": (
        'def f(req):\n    destination = _require_allowed_destination(req.destination)\n'
        '    from os import sep as destination\n    raise HTTPException(409, f"{destination}")'
    ),
    "tuple unpack destination": (
        'def f(req, page):\n    destination = _require_allowed_destination(req.destination)\n'
        '    destination, _ = page["email_alias"], 1\n    raise HTTPException(409, f"{destination}")'
    ),
    "_require_allowed_destination of a roster value": (
        'def f(req, page):\n    destination = _require_allowed_destination(page["email_alias"])\n'
        '    raise HTTPException(422, f"{destination}")'
    ),
    "_require_allowed_destination of a re-bound req": (
        'def f(req, page):\n    req = page\n    destination = _require_allowed_destination(req.destination)\n'
        '    raise HTTPException(422, f"{destination}")'
    ),
    "e re-bound inside the _cf_upstream handler": (
        'def _cf_upstream(page):\n    try:\n        yield\n    except Exception as e:\n'
        '        e = page["email_alias"]\n        raise HTTPException(status_code=502, detail=str(e))'
    ),
    "e re-bound by walrus inside the handler": (
        'def _cf_upstream(page):\n    try:\n        yield\n    except Exception as e:\n'
        '        (e := page["email_alias"])\n        raise HTTPException(status_code=502, detail=str(e))'
    ),
    "aliased JSONResponse import": (
        'from fastapi.responses import JSONResponse as JR\ndef f(alias):\n    return JR({"x": alias}, status_code=409)'
    ),
    "aliased starlette Response import": (
        'from starlette.responses import Response as R\ndef f(alias):\n    return R(alias, status_code=409)'
    ),
}


@pytest.mark.parametrize("mutation", sorted(_GUARD_MUTATIONS))
def test_error_text_guard_flags_each_known_leak_shape(mutation):
    assert _error_text_offenders(_GUARD_PRELUDE + _GUARD_MUTATIONS[mutation]), mutation


def test_error_text_guard_accepts_the_allowed_shapes():
    ok = _GUARD_PRELUDE + (
        'def a():\n    raise HTTPException(status_code=409, detail="Alias already exists")\n'
        'def b(req):\n    destination = _require_allowed_destination(req.destination)\n'
        '    raise HTTPException(status_code=422, detail=f"Destination {destination} is not verified")\n'
        'def _cf_upstream():\n    try:\n        yield\n    except HTTPException:\n        raise\n'
        '    except Exception as e:\n        raise HTTPException(status_code=502, detail=str(e))\n'
    )
    assert _error_text_offenders(ok) == []
