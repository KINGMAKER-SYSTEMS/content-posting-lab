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


def test_update_rule_service_removed():
    import services.email_routing as svc

    assert not hasattr(svc, "update_rule")
    # The functions still relied on (auto-create + pipeline alias minting) stay.
    assert hasattr(svc, "create_rule")
    assert hasattr(svc, "list_rules")
    assert hasattr(svc, "delete_rule")
