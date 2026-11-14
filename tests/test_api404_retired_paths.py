"""Removed/unknown ``/api/*`` paths must 404, never the built frontend SPA.

``app.py`` registers ``GET /{full_path:path}`` (the SPA catch-all) whenever
``frontend/dist`` exists at app construction. Before the fix, a removed API
path fell into that catch-all: ``GET`` returned the SPA ``index.html`` and
every other verb returned a misleading ``405``. The fix adds an unconditional
``/api/{full_path:path}`` catch-all that 404s unknown API paths while deferring
to any real route's own ``405 + Allow``.

These tests build a real ``frontend/dist`` in ``tmp_path`` and drive the REAL
``app.app`` router through ``TestClient`` (no lifespan, matching
``tests/conftest.py`` isolation). The SPA catch-all is registered exactly the
way ``app.py`` does under the built condition, so the API catch-all must win
over it for ``/api/`` paths but not for client routes like ``/apiculture``.
"""
import pytest

import app as app_module
from fastapi.testclient import TestClient

INDEX = b"<!doctype html><title>lab spa index</title>\n"
ASSET = b"console.log('lab asset');"
KEY = "test-api-key-404"


@pytest.fixture(autouse=True)
def restore_app_routes():
    """Undo the SPA catch-all these tests add, so later test files see the app as imported."""
    saved = list(app_module.app.router.routes)
    yield
    app_module.app.router.routes[:] = saved


def _build_dist(tmp_path):
    dist = tmp_path / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_bytes(INDEX)
    (dist / "assets" / "index-abc123.js").write_bytes(ASSET)
    return dist


def _ensure_spa_catchall():
    """Register the SPA catch-all the way app.py does, if not already present.

    app.py only registers ``GET /{full_path:path}`` when frontend/dist exists at
    import, which is not the case in a clean checkout. This lets the tests
    exercise the real app in the *built* state deterministically; the API
    catch-all under test is the real app.py route registered unconditionally.
    """
    for route in app_module.app.routes:
        if getattr(route, "path", None) == "/{full_path:path}":
            return
    app_module.app.get("/{full_path:path}")(app_module.serve_frontend)


def _drop_spa_catchall():
    """Make the app look unbuilt, whatever earlier code registered (restored after each test)."""
    app_module.app.router.routes[:] = [
        r for r in app_module.app.router.routes if getattr(r, "endpoint", None) is not app_module.serve_frontend
    ]


def _client():
    # No context manager: entering it would run the lifespan (DB seed + the
    # autonomous factory loop), which tests/conftest.py deliberately avoids.
    return TestClient(app_module.app)


# ------------------------------------------------------------------- built state


def test_unknown_api_standard_methods_404_not_spa(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "FRONTEND_DIR", _build_dist(tmp_path))
    _ensure_spa_catchall()
    client = _client()
    for method in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE"):
        resp = client.request(method, "/api/agenticnews/chat")
        assert resp.status_code == 404, (method, resp.status_code, resp.text)
        # body must not be the SPA; HEAD has no body.
        if method != "HEAD":
            assert resp.content != INDEX
            assert "text/html" not in resp.headers.get("content-type", "")


def test_removed_chat_five_paths_404(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "FRONTEND_DIR", _build_dist(tmp_path))
    _ensure_spa_catchall()
    client = _client()
    assert client.post("/api/agenticnews/chat", json={"text": "x"}).status_code == 404
    assert client.get("/api/agenticnews/chat/poll").status_code == 404
    assert client.get("/api/agenticnews/chat/inbox").status_code == 404
    assert client.post("/api/agenticnews/chat/reply", json={"text": "x"}).status_code == 404
    assert client.get("/api/agenticnews/chat/history").status_code == 404


def test_known_api_wrong_method_keeps_405_and_allow(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "FRONTEND_DIR", _build_dist(tmp_path))
    _ensure_spa_catchall()
    client = _client()
    # /api/health is GET-only (auth-exempt). A wrong verb must stay 405 with
    # the same Allow header, not be swallowed into the API 404.
    for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
        resp = client.request(method, "/api/health")
        assert resp.status_code == 405, (method, resp.status_code)
        assert resp.headers.get("allow") == "GET", (method, resp.headers.get("allow"))
        if method != "HEAD":  # HEAD has no body by definition
            assert resp.json()["detail"] == "Method Not Allowed"


def test_known_api_wrong_method_keeps_405_behind_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "FRONTEND_DIR", _build_dist(tmp_path))
    monkeypatch.setattr(app_module, "_APP_API_KEY", KEY)
    _ensure_spa_catchall()
    client = _client()
    # GET-only, non-exempt route: wrong verb + valid key must still 405.
    resp = client.post(
        "/api/agenticnews/factory/state", headers={"Authorization": f"Bearer {KEY}"}
    )
    assert resp.status_code == 405, resp.text
    assert resp.headers.get("allow") == "GET"


def test_client_routes_and_lookalikes_still_serve_spa(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "FRONTEND_DIR", _build_dist(tmp_path))
    _ensure_spa_catchall()
    client = _client()
    for route in ("/", "/burn", "/pages/some-page", "/apiculture", "/apiculture/deep"):
        resp = client.get(route)
        assert resp.status_code == 200, (route, resp.status_code)
        assert resp.content == INDEX, (route, resp.content)


def test_assets_still_serve_from_dist(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "FRONTEND_DIR", _build_dist(tmp_path))
    _ensure_spa_catchall()
    client = _client()
    assert client.get("/assets/index-abc123.js").content == ASSET


def test_bare_api_and_api_slash_decided(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "FRONTEND_DIR", _build_dist(tmp_path))
    _ensure_spa_catchall()
    client = _client()
    # bare /api has no trailing slash: the auth middleware guards only
    # startswith("/api/"), so /api is NOT an API path and keeps the existing
    # frontend catch-all behavior (SPA when built).
    assert client.get("/api").status_code == 200
    assert client.get("/api").content == INDEX
    # /api/ (nothing after the prefix) is left as it was: the catch-all only
    # claims /api/<something>, so the built app still answers with the SPA.
    assert client.get("/api/").status_code == 200
    assert client.get("/api/").content == INDEX


def test_cors_preflight_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "FRONTEND_DIR", _build_dist(tmp_path))
    _ensure_spa_catchall()
    client = _client()
    ok = client.options(
        "/api/agenticnews/chat",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-api-key",
        },
    )
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == "http://localhost:5173"
    denied = client.options(
        "/api/agenticnews/chat",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert denied.status_code == 400
    # A bare OPTIONS (not a CORS preflight) on an unknown API path is 404.
    assert client.options("/api/agenticnews/chat").status_code == 404


# ------------------------------------------------------------------ unbuilt state


def test_unknown_api_404_even_without_frontend(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "FRONTEND_DIR", tmp_path / "missing")
    _drop_spa_catchall()
    client = _client()
    for method in ("GET", "POST", "PUT", "DELETE", "TRACE"):
        resp = client.request(method, "/api/agenticnews/chat")
        assert resp.status_code == 404, (method, resp.status_code)


def test_unbuilt_bare_api_is_unchanged_404_not_a_redirect(tmp_path, monkeypatch):
    # Starlette probes "/api/" for a bare "/api"; the catch-all must not claim
    # "/api/", or the old 404 would turn into a 307 into the auth check.
    monkeypatch.setattr(app_module, "FRONTEND_DIR", tmp_path / "missing")
    _drop_spa_catchall()
    client = _client()
    for path in ("/api", "/api/"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 404, (path, resp.status_code, resp.headers.get("location"))


def _assert_slash_redirects(client):
    # Same answers Starlette's redirect_slashes gave before the catch-all existed.
    resp = client.get("/api/health/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"].endswith("/api/health")
    resp = client.get("/api/health/?probe=1", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"].endswith("/api/health?probe=1")
    resp = client.get("/api/pages", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"].endswith("/api/pages/")
    # an unknown path with or without a slash is still a plain 404
    for path in ("/api/agenticnews/chat/", "/api/no-such-thing", "/api/no-such-thing/"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 404, (path, resp.status_code, resp.headers.get("location"))


def test_trailing_slash_redirects_kept_unbuilt(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "FRONTEND_DIR", tmp_path / "missing")
    _drop_spa_catchall()
    _assert_slash_redirects(_client())


def test_root_path_prefix_is_honoured(tmp_path, monkeypatch):
    # Behind a path prefix (ASGI root_path) the catch-all must read the path the
    # way Starlette's router does, or prefixed unknown /api paths fall to the SPA.
    monkeypatch.setattr(app_module, "FRONTEND_DIR", _build_dist(tmp_path))
    _ensure_spa_catchall()
    client = TestClient(app_module.app, root_path="/lab")
    for method in ("GET", "POST", "TRACE"):
        resp = client.request(method, "/lab/api/no-such-thing", follow_redirects=False)
        assert resp.status_code == 404, (method, resp.status_code)
    resp = client.get("/lab/api/health/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"].endswith("/lab/api/health")
    assert client.get("/lab/api/health").status_code == 200


def test_trailing_slash_redirects_kept_built(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "FRONTEND_DIR", _build_dist(tmp_path))
    _ensure_spa_catchall()
    _assert_slash_redirects(_client())


def test_api_catchall_registered_unconditionally():
    catch_alls = [
        r
        for r in app_module.app.routes
        if getattr(r, "path", None) == "/api/{full_path:path}"
    ]
    assert len(catch_alls) == 1
    assert catch_alls[0].endpoint is app_module.api_not_found


def test_no_api_route_is_registered_after_the_api_catchall():
    # The catch-all keeps a real route's 405 + Allow only for routes the router
    # reaches BEFORE it: for a later route the catch-all is the first PARTIAL and
    # would answer 404. Any new /api router must be included above it in app.py.
    paths = [getattr(r, "path", "") for r in app_module.app.routes]
    position = paths.index("/api/{full_path:path}")
    later = [p for p in paths[position + 1:] if p == "/api" or p.startswith("/api/")]
    assert later == []


# ------------------------------------------------------------------ auth contract


def test_unknown_api_auth_middleware_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "FRONTEND_DIR", _build_dist(tmp_path))
    monkeypatch.setattr(app_module, "_APP_API_KEY", KEY)
    _ensure_spa_catchall()
    client = _client()

    # absent / wrong key -> 401 (middleware, unchanged)
    assert client.get("/api/agenticnews/chat").status_code == 401
    assert (
        client.get("/api/agenticnews/chat", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    # correct Bearer OR correct X-API-Key admits, even when the other is wrong
    assert (
        client.get("/api/agenticnews/chat", headers={"Authorization": f"Bearer {KEY}"}).status_code
        == 404
    )
    assert (
        client.get(
            "/api/agenticnews/chat",
            headers={"Authorization": "Bearer wrong", "X-API-Key": KEY},
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/api/agenticnews/chat",
            headers={"Authorization": f"Bearer {KEY}", "X-API-Key": "wrong"},
        ).status_code
        == 404
    )
    # a query key alone is ineffective (middleware reads no query credential)
    assert client.get(f"/api/agenticnews/chat?api_key={KEY}").status_code == 401
    # POST too
    assert client.post("/api/agenticnews/chat", json={"text": "x"}).status_code == 401
    assert (
        client.post(
            "/api/agenticnews/chat",
            json={"text": "x"},
            headers={"Authorization": f"Bearer {KEY}"},
        ).status_code
        == 404
    )


def test_unknown_api_auth_exemptions_and_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "FRONTEND_DIR", _build_dist(tmp_path))
    monkeypatch.setattr(app_module, "_APP_API_KEY", KEY)
    _ensure_spa_catchall()
    client = _client()
    # /api/health is exempt (no key required)
    assert client.get("/api/health").status_code == 200
    # bare /api is not under the /api/ prefix: no auth, still the SPA
    assert client.get("/api").status_code == 200
    # /api/ is under the auth prefix: 401 without key; with it, unchanged (SPA)
    assert client.get("/api/").status_code == 401
    assert client.get("/api/", headers={"Authorization": f"Bearer {KEY}"}).status_code == 200
    # an unknown /api/<x> with the key is 404
    assert client.get("/api/no-such-thing", headers={"Authorization": f"Bearer {KEY}"}).status_code == 404
