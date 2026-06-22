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
