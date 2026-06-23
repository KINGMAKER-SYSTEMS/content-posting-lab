"""Email routing router: dead rule-management endpoints are gone.

Frontend only uses POST /api/email/auto-create (create) and
DELETE /api/email/rules/{rule_id} (delete). GET/POST /api/email/rules and
PUT /api/email/rules/{rule_id} were dead code and removed.
"""

from app import app


def _routes() -> set[tuple[str, frozenset]]:
    out = set()
    for r in app.routes:
        path = getattr(r, "path", None)
        methods = getattr(r, "methods", None)
        if path and methods:
            out.add((path, frozenset(methods)))
    return out


def test_dead_rule_management_routes_removed():
    paths = {p for p, _ in _routes()}
    methods_by_path: dict[str, set] = {}
    for p, m in _routes():
        methods_by_path.setdefault(p, set()).update(m)

    # The list-rules collection endpoint is gone entirely.
    assert "/api/email/rules" not in paths

    # The rule_id endpoint survives, but ONLY for DELETE — no PUT.
    rule_id_methods = methods_by_path.get("/api/email/rules/{rule_id}", set())
    assert "DELETE" in rule_id_methods
    assert "PUT" not in rule_id_methods


def test_surviving_email_routes_present():
    paths = {p for p, _ in _routes()}
    for survivor in (
        "/api/email/status",
        "/api/email/auto-create",
        "/api/email/destinations",
        "/api/email/rules/{rule_id}",
    ):
        assert survivor in paths, f"{survivor} should still be registered"


def test_roster_imports_are_module_level():
    """Regression: `from services.roster import ...` was wedged mid-file after a
    function def. All imports in routers/email_routing.py must live at module top
    level (no nested/function-scoped imports)."""
    import ast
    import inspect

    import routers.email_routing as r

    tree = ast.parse(inspect.getsource(r))
    top_level = {id(n) for n in tree.body}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            assert id(node) in top_level, (
                f"import on line {node.lineno} is not at module level"
            )

    names = {a.name for n in ast.walk(tree)
             if isinstance(n, ast.ImportFrom) and n.module == "services.roster"
             for a in n.names}
    assert {"get_page", "set_page"} <= names


def test_update_rule_service_removed():
    import services.email_routing as svc

    assert not hasattr(svc, "update_rule")
    # The functions still relied on (auto-create + pipeline alias minting) stay.
    assert hasattr(svc, "create_rule")
    assert hasattr(svc, "list_rules")
    assert hasattr(svc, "delete_rule")


# ── Behavioral tests for the sale-intake critical paths ──────────────────────
#
# These exercise the auto-create / delete-rule logic with the CF client and roster
# stubbed out, so no live Cloudflare calls happen. We patch the names *as imported
# into the router module* (routers.email_routing), which is where the handlers
# resolve them.

import pytest  # noqa: E402

import routers.email_routing as r  # noqa: E402


@pytest.fixture
def configured(monkeypatch):
    """Pretend CF Email Routing is configured for the test."""
    monkeypatch.setattr(
        r, "get_config", lambda: {"configured": True, "domain": "rt.example"}
    )


def _async(value):
    async def _f(*a, **k):
        return value
    return _f


def _async_raise(exc):
    async def _f(*a, **k):
        raise exc
    return _f


# --- guard: unconfigured CF ---------------------------------------------------


def test_auto_create_requires_configured(sync_client, monkeypatch):
    monkeypatch.setattr(r, "get_config", lambda: {"configured": False, "domain": ""})
    resp = sync_client.post(
        "/api/email/auto-create",
        json={"integration_id": "i1", "account_name": "Acme", "destination": "a@b.com"},
    )
    assert resp.status_code == 503


# --- alias collision detection (409) -----------------------------------------


def test_auto_create_rejects_alias_collision(sync_client, monkeypatch, configured):
    # An existing rule already claims acme@rt.example.
    existing = [{"matchers": [{"type": "literal", "field": "to", "value": "acme@rt.example"}]}]
    monkeypatch.setattr(r, "list_rules", _async(existing))
    # Destination would otherwise be fine, but collision short-circuits first.
    monkeypatch.setattr(r, "list_destinations", _async([{"email": "a@b.com", "verified": "2026-01-01"}]))
    create_called = {"hit": False}
    monkeypatch.setattr(r, "create_rule", _async_raise(AssertionError("must not create on collision")))

    resp = sync_client.post(
        "/api/email/auto-create",
        json={"integration_id": "i1", "account_name": "Acme", "destination": "a@b.com"},
    )
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]
    assert create_called["hit"] is False


# --- unverified destination rejection (422) ----------------------------------


def test_auto_create_rejects_unverified_destination(sync_client, monkeypatch, configured):
    monkeypatch.setattr(r, "list_rules", _async([]))  # no collision
    # CF knows the address but it is NOT verified yet (verified is null).
    monkeypatch.setattr(
        r, "list_destinations",
        _async([{"email": "a@b.com", "verified": None}]),
    )
    monkeypatch.setattr(r, "create_rule", _async_raise(AssertionError("must not create for unverified dest")))

    resp = sync_client.post(
        "/api/email/auto-create",
        json={"integration_id": "i1", "account_name": "Acme", "destination": "a@b.com"},
    )
    assert resp.status_code == 422
    assert "not a verified" in resp.json()["detail"]


def test_auto_create_rejects_unknown_destination(sync_client, monkeypatch, configured):
    monkeypatch.setattr(r, "list_rules", _async([]))
    # CF has a different verified address; the requested one is unknown entirely.
    monkeypatch.setattr(
        r, "list_destinations",
        _async([{"email": "someone@else.com", "verified": "2026-01-01"}]),
    )
    monkeypatch.setattr(r, "create_rule", _async_raise(AssertionError("must not create for unknown dest")))

    resp = sync_client.post(
        "/api/email/auto-create",
        json={"integration_id": "i1", "account_name": "Acme", "destination": "a@b.com"},
    )
    assert resp.status_code == 422


# --- happy path: verified destination, no collision --------------------------


def test_auto_create_succeeds_for_verified_destination(sync_client, monkeypatch, configured):
    monkeypatch.setattr(r, "list_rules", _async([]))
    # Match is case-insensitive against the verified list.
    monkeypatch.setattr(
        r, "list_destinations",
        _async([{"email": "A@B.com", "verified": "2026-01-01"}]),
    )
    monkeypatch.setattr(r, "create_rule", _async({"id": "rule_123"}))
    monkeypatch.setattr(r, "get_page", lambda iid: None)  # no roster page to link

    resp = sync_client.post(
        "/api/email/auto-create",
        json={"integration_id": "i1", "account_name": "Acme Corp!", "destination": "a@b.com"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["alias"] == "acme-corp@rt.example"  # sanitized local part
    assert body["rule"]["id"] == "rule_123"


def test_auto_create_links_roster_page_on_success(sync_client, monkeypatch, configured):
    monkeypatch.setattr(r, "list_rules", _async([]))
    monkeypatch.setattr(r, "list_destinations", _async([{"email": "a@b.com", "verified": "x"}]))
    monkeypatch.setattr(r, "create_rule", _async({"id": "rule_xyz"}))

    saved = {}

    def _get_page(iid):
        return saved if saved else {"integration_id": iid}

    def _set_page(iid, fields):
        saved.update(fields)

    monkeypatch.setattr(r, "get_page", _get_page)
    monkeypatch.setattr(r, "set_page", _set_page)

    resp = sync_client.post(
        "/api/email/auto-create",
        json={"integration_id": "i1", "account_name": "Acme", "destination": "a@b.com"},
    )
    assert resp.status_code == 200
    assert saved["email_alias"] == "acme@rt.example"
    assert saved["email_rule_id"] == "rule_xyz"
    assert saved["fwd_destination"] == "a@b.com"


def test_auto_create_rejects_empty_alias(sync_client, monkeypatch, configured):
    # An account name with no alphanumerics sanitizes to empty → 400.
    monkeypatch.setattr(r, "list_rules", _async([]))
    monkeypatch.setattr(r, "list_destinations", _async([{"email": "a@b.com", "verified": "x"}]))
    resp = sync_client.post(
        "/api/email/auto-create",
        json={"integration_id": "i1", "account_name": "!!!", "destination": "a@b.com"},
    )
    assert resp.status_code == 400


# --- rule deletion ------------------------------------------------------------


def test_delete_rule_unlinks_roster_page(sync_client, monkeypatch, configured):
    deleted = {"id": None}

    async def _del(rule_id):
        deleted["id"] = rule_id
        return True

    monkeypatch.setattr(r, "delete_rule", _del)

    cleared = {}
    monkeypatch.setattr(r, "get_page", lambda iid: {"integration_id": iid, "email_alias": "x@y"})
    monkeypatch.setattr(r, "set_page", lambda iid, fields: cleared.update(fields))

    resp = sync_client.delete("/api/email/rules/rule_99?integration_id=i1")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}
    assert deleted["id"] == "rule_99"
    # Roster fields cleared.
    assert cleared == {"email_alias": None, "email_rule_id": None, "fwd_destination": None}


def test_delete_rule_rejects_mismatched_rule_for_page(sync_client, monkeypatch, configured):
    """If the page is linked to a *different* rule, deletion must be refused (409)
    and the CF rule must NOT be deleted — otherwise we silently nuke the wrong
    rule and orphan the page's real one."""
    def _boom_delete(*a, **k):
        raise AssertionError("must not delete a rule that does not belong to the page")

    monkeypatch.setattr(r, "delete_rule", _boom_delete)
    monkeypatch.setattr(
        r, "get_page",
        lambda iid: {"integration_id": iid, "email_rule_id": "rule_OWNED"},
    )

    def _boom_set(*a, **k):
        raise AssertionError("roster must not be modified on a mismatch")

    monkeypatch.setattr(r, "set_page", _boom_set)

    resp = sync_client.delete("/api/email/rules/rule_WRONG?integration_id=i1")
    assert resp.status_code == 409
    assert "does not belong" in resp.json()["detail"]


def test_delete_rule_allows_matching_rule_for_page(sync_client, monkeypatch, configured):
    """When the page's linked rule matches rule_id, deletion proceeds and unlinks."""
    deleted = {"id": None}

    async def _del(rule_id):
        deleted["id"] = rule_id
        return True

    monkeypatch.setattr(r, "delete_rule", _del)

    cleared = {}
    monkeypatch.setattr(
        r, "get_page",
        lambda iid: {"integration_id": iid, "email_rule_id": "rule_OWNED"},
    )
    monkeypatch.setattr(r, "set_page", lambda iid, fields: cleared.update(fields))

    resp = sync_client.delete("/api/email/rules/rule_OWNED?integration_id=i1")
    assert resp.status_code == 200
    assert deleted["id"] == "rule_OWNED"
    assert cleared == {"email_alias": None, "email_rule_id": None, "fwd_destination": None}


def test_delete_rule_without_integration_does_not_touch_roster(sync_client, monkeypatch, configured):
    monkeypatch.setattr(r, "delete_rule", _async(True))

    def _boom(*a, **k):
        raise AssertionError("roster must not be touched without integration_id")

    monkeypatch.setattr(r, "get_page", _boom)
    monkeypatch.setattr(r, "set_page", _boom)

    resp = sync_client.delete("/api/email/rules/rule_99")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}


def test_delete_rule_propagates_cf_error_as_502(sync_client, monkeypatch, configured):
    monkeypatch.setattr(r, "delete_rule", _async_raise(RuntimeError("CF 500")))
    resp = sync_client.delete("/api/email/rules/rule_99")
    assert resp.status_code == 502
    assert "CF 500" in resp.json()["detail"]


# ── _cf_upstream helper: shared 502 conversion across every endpoint ──────────


def test_get_destinations_propagates_cf_error_as_502(sync_client, monkeypatch, configured):
    monkeypatch.setattr(r, "list_destinations", _async_raise(RuntimeError("CF down")))
    resp = sync_client.get("/api/email/destinations")
    assert resp.status_code == 502
    assert "CF down" in resp.json()["detail"]


def test_add_destination_propagates_cf_error_as_502(sync_client, monkeypatch, configured):
    monkeypatch.setattr(r, "add_destination", _async_raise(RuntimeError("CF boom")))
    resp = sync_client.post("/api/email/destinations", json={"email": "a@b.com"})
    assert resp.status_code == 502
    assert "CF boom" in resp.json()["detail"]


def test_auto_create_propagates_cf_error_as_502(sync_client, monkeypatch, configured):
    monkeypatch.setattr(r, "list_rules", _async([]))
    monkeypatch.setattr(r, "list_destinations", _async([{"email": "a@b.com", "verified": "x"}]))
    monkeypatch.setattr(r, "create_rule", _async_raise(RuntimeError("CF create failed")))
    resp = sync_client.post(
        "/api/email/auto-create",
        json={"integration_id": "i1", "account_name": "Acme", "destination": "a@b.com"},
    )
    assert resp.status_code == 502
    assert "CF create failed" in resp.json()["detail"]


def test_cf_upstream_does_not_mask_deliberate_http_exception():
    """A handler that raises a real HTTPException (e.g. a 409) inside the context
    must surface with its own status, not be rewritten to 502."""
    from fastapi import HTTPException as _HE

    with pytest.raises(_HE) as exc:
        with r._cf_upstream():
            raise _HE(status_code=409, detail="conflict")
    assert exc.value.status_code == 409
    assert exc.value.detail == "conflict"


def test_cf_upstream_converts_bare_exception_to_502():
    from fastapi import HTTPException as _HE

    with pytest.raises(_HE) as exc:
        with r._cf_upstream():
            raise RuntimeError("kaboom")
    assert exc.value.status_code == 502
    assert exc.value.detail == "kaboom"


# ── Service-level tests for services/email_routing.py ────────────────────────
#
# These exercise the real async httpx calls inside the service (not the router).
# We drive httpx.AsyncClient with a MockTransport so no live Cloudflare traffic
# happens, then assert on success parsing AND the error paths the ticket cares
# about: missing config, HTTP 403/500, timeouts, and network partitions.

import httpx  # noqa: E402

import services.email_routing as svc  # noqa: E402

CF_ENV = {
    "CF_API_TOKEN": "tok-123",
    "CF_ZONE_ID": "zone-abc",
    "CF_ACCOUNT_ID": "acct-xyz",
    "CF_EMAIL_DOMAIN": "rt.example",
}


@pytest.fixture
def cf_configured(monkeypatch):
    for k, v in CF_ENV.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def cf_unconfigured(monkeypatch):
    for k in CF_ENV:
        monkeypatch.delenv(k, raising=False)


def _install_transport(monkeypatch, handler):
    """Force the service's httpx.AsyncClient to use a MockTransport handler.

    The service builds its own client as ``httpx.AsyncClient(timeout=15)`` with
    no transport arg, so we wrap the class to inject one. ``handler`` receives an
    ``httpx.Request`` and returns an ``httpx.Response`` (or raises to simulate a
    network fault / timeout).
    """
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(svc.httpx, "AsyncClient", _factory)


# --- get_config ---------------------------------------------------------------


def test_get_config_reports_configured(cf_configured):
    cfg = svc.get_config()
    assert cfg["configured"] is True
    assert cfg["zone_id"] == "zone-abc"
    assert cfg["domain"] == "rt.example"
    assert cfg["account_id"] == "acct-xyz"
    assert cfg["token"] == "tok-123"


def test_get_config_reports_unconfigured_when_partial(monkeypatch, cf_unconfigured):
    monkeypatch.setenv("CF_API_TOKEN", "tok-only")
    cfg = svc.get_config()
    assert cfg["configured"] is False


# --- unconfigured short-circuits (no network) ---------------------------------


async def test_list_rules_returns_empty_when_unconfigured(cf_unconfigured, monkeypatch):
    def _no_calls(request):  # pragma: no cover - must never fire
        raise AssertionError("must not hit the network when unconfigured")

    _install_transport(monkeypatch, _no_calls)
    assert await svc.list_rules() == []


async def test_list_destinations_returns_empty_when_unconfigured(cf_unconfigured, monkeypatch):
    def _no_calls(request):  # pragma: no cover
        raise AssertionError("must not hit the network when unconfigured")

    _install_transport(monkeypatch, _no_calls)
    assert await svc.list_destinations() == []


async def test_create_rule_raises_when_unconfigured(cf_unconfigured):
    with pytest.raises(ValueError, match="not configured"):
        await svc.create_rule("acme", "a@b.com")


async def test_delete_rule_raises_when_unconfigured(cf_unconfigured):
    with pytest.raises(ValueError, match="not configured"):
        await svc.delete_rule("rule_1")


async def test_add_destination_raises_when_unconfigured(cf_unconfigured):
    with pytest.raises(ValueError, match="not configured"):
        await svc.add_destination("a@b.com")


# --- happy paths: real request building + response parsing --------------------


async def test_list_rules_parses_result(cf_configured, monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"result": [{"id": "r1"}, {"id": "r2"}]})

    _install_transport(monkeypatch, handler)
    rules = await svc.list_rules()
    assert [r["id"] for r in rules] == ["r1", "r2"]
    assert seen["url"].endswith("/zones/zone-abc/email/routing/rules")
    assert seen["auth"] == "Bearer tok-123"


async def test_create_rule_builds_forward_payload(cf_configured, monkeypatch):
    import json as _json

    captured = {}

    def handler(request):
        captured["body"] = _json.loads(request.content)
        captured["method"] = request.method
        return httpx.Response(200, json={"result": {"id": "new_rule"}})

    _install_transport(monkeypatch, handler)
    result = await svc.create_rule("acme", "dest@b.com")
    assert result == {"id": "new_rule"}
    assert captured["method"] == "POST"
    body = captured["body"]
    assert body["matchers"][0]["value"] == "acme@rt.example"
    assert body["actions"][0]["type"] == "forward"
    assert body["actions"][0]["value"] == ["dest@b.com"]
    assert body["enabled"] is True


async def test_delete_rule_returns_true_on_200(cf_configured, monkeypatch):
    def handler(request):
        assert request.method == "DELETE"
        assert request.url.path.endswith("/rules/rule_42")
        return httpx.Response(200, json={"result": {"id": "rule_42"}})

    _install_transport(monkeypatch, handler)
    assert await svc.delete_rule("rule_42") is True


async def test_list_destinations_parses_result(cf_configured, monkeypatch):
    def handler(request):
        assert "/accounts/acct-xyz/email/routing/addresses" in str(request.url)
        return httpx.Response(200, json={"result": [{"email": "a@b.com", "verified": "x"}]})

    _install_transport(monkeypatch, handler)
    dests = await svc.list_destinations()
    assert dests[0]["email"] == "a@b.com"


async def test_add_destination_posts_email(cf_configured, monkeypatch):
    import json as _json

    captured = {}

    def handler(request):
        captured["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"result": {"email": "a@b.com", "id": "d1"}})

    _install_transport(monkeypatch, handler)
    result = await svc.add_destination("a@b.com")
    assert result["id"] == "d1"
    assert captured["body"] == {"email": "a@b.com"}


async def test_missing_result_key_defaults(cf_configured, monkeypatch):
    """CF responds 200 but with no 'result' key — service should not KeyError."""
    _install_transport(monkeypatch, lambda req: httpx.Response(200, json={"success": True}))
    assert await svc.list_rules() == []
    assert await svc.create_rule("acme", "a@b.com") == {}
    assert await svc.add_destination("a@b.com") == {}


# --- error paths: HTTP status failures (raise_for_status) ---------------------


@pytest.mark.parametrize("status", [403, 401, 429, 500, 502])
async def test_list_rules_raises_on_http_error(cf_configured, monkeypatch, status):
    _install_transport(monkeypatch, lambda req: httpx.Response(status, json={"errors": ["nope"]}))
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await svc.list_rules()
    assert exc.value.response.status_code == status


async def test_create_rule_raises_on_403_auth_failure(cf_configured, monkeypatch):
    _install_transport(
        monkeypatch,
        lambda req: httpx.Response(403, json={"errors": [{"message": "Authentication error"}]}),
    )
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await svc.create_rule("acme", "a@b.com")
    assert exc.value.response.status_code == 403


async def test_delete_rule_raises_on_500(cf_configured, monkeypatch):
    _install_transport(monkeypatch, lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await svc.delete_rule("rule_1")
    assert exc.value.response.status_code == 500


async def test_add_destination_raises_on_500(cf_configured, monkeypatch):
    _install_transport(monkeypatch, lambda req: httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await svc.add_destination("a@b.com")


# --- error paths: timeouts and network partitions -----------------------------


async def test_list_rules_propagates_timeout(cf_configured, monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("CF timed out", request=request)

    _install_transport(monkeypatch, handler)
    with pytest.raises(httpx.TimeoutException):
        await svc.list_rules()


async def test_create_rule_propagates_connect_error(cf_configured, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("network partition", request=request)

    _install_transport(monkeypatch, handler)
    with pytest.raises(httpx.ConnectError):
        await svc.create_rule("acme", "a@b.com")


async def test_delete_rule_propagates_network_error(cf_configured, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("no route to host", request=request)

    _install_transport(monkeypatch, handler)
    with pytest.raises(httpx.ConnectError):
        await svc.delete_rule("rule_1")
