"""Cloudflare Access JWT verification and the caller-class dependency (services/route_auth.py).

A locally generated RSA keypair stands in for the team's certs; no network.
"""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from services import route_auth
from tests import route_auth_support as s

pytestmark = pytest.mark.real_route_auth


@pytest.fixture
def idp(monkeypatch):
    s.configure_all(monkeypatch)
    return s.install_idp(monkeypatch)


def test_valid_token_verifies(idp):
    claims = route_auth.verify_access_jwt(s.mint())
    assert claims is not None and claims["email"] == "operator@example.invalid"
    assert idp, "the certs were fetched through PyJWKClient"


@pytest.mark.parametrize(
    "label,token",
    [
        ("signed by another key", lambda: s.mint(key=s.OTHER_KEY)),
        ("unsigned alg=none", lambda: s.mint(alg="none")),
        ("HS256 with a shared secret", lambda: s.mint(alg="HS256")),
        ("wrong audience", lambda: s.mint(aud="another-app-aud")),
        ("wrong issuer", lambda: s.mint(iss="https://evil.cloudflareaccess.com")),
        ("expired", lambda: s.mint(exp_offset=-3600)),
        ("unknown kid", lambda: s.mint(kid="some-other-kid")),
        ("garbage", lambda: "not-a-jwt"),
        ("empty", lambda: ""),
    ],
)
def test_forged_or_invalid_tokens_rejected(idp, label, token):
    assert route_auth.verify_access_jwt(token()) is None, label


def test_tampered_payload_rejected(idp):
    head, body, sig = s.mint().split(".")
    other_head, other_body, _ = s.mint(claims={"email": "attacker@example.invalid"}).split(".")
    assert route_auth.verify_access_jwt(f"{head}.{other_body}.{sig}") is None


def test_missing_exp_rejected(idp):
    import jwt
    import time

    token = jwt.encode(
        {"aud": [s.ACCESS_AUD], "iss": s.TEAM_DOMAIN, "iat": int(time.time())},
        s.IDP_KEY,
        algorithm="RS256",
        headers={"kid": s.KID},
    )
    assert route_auth.verify_access_jwt(token) is None


def test_unconfigured_access_verifies_nothing(monkeypatch):
    s.unconfigure_all(monkeypatch)
    fetches = s.install_idp(monkeypatch)
    assert route_auth.verify_access_jwt(s.mint()) is None
    assert fetches == []


def test_multiple_audiences_accepted(monkeypatch):
    s.configure_all(monkeypatch)
    monkeypatch.setenv("LAB_ACCESS_AUD", f"other-aud, {s.ACCESS_AUD}")
    assert route_auth.verify_access_jwt(s.mint()) is not None


# ── the dependency, on a throwaway app ──────────────────────────────


def _app(dep):
    app = FastAPI()
    calls = []

    @app.post("/x", dependencies=[Depends(dep)])
    def handler():
        calls.append(1)
        return {"ok": True}

    return TestClient(app), calls


def test_each_class_admits_only_its_own_credential(monkeypatch):
    s.configure_all(monkeypatch)
    for dep in route_auth.ALL_DEPENDENCIES:
        client, calls = _app(dep)
        for caller in route_auth.CALLER_CLASSES:
            r = client.post("/x", headers=s.valid_headers(caller))
            if caller in dep.route_auth_callers:
                assert r.status_code == 200, (dep.__name__, caller, r.status_code)
            else:
                assert r.status_code == 401, (dep.__name__, caller, r.status_code)
        assert len(calls) == len(dep.route_auth_callers)


def test_wrong_credentials_never_pass(monkeypatch):
    s.configure_all(monkeypatch)
    monkeypatch.setenv("APP_API_KEY", s.APP_KEY)
    for dep in route_auth.ALL_DEPENDENCIES:
        client, calls = _app(dep)
        assert client.post("/x").status_code == 401
        wrong = s.wrong_header_sets()
        if getattr(dep, "worker_token_in_x_api_key", False):
            wrong.pop("worker-token-as-x-api-key")
        for label, headers in wrong.items():
            assert client.post("/x", headers=headers).status_code == 401, (dep.__name__, label)
        assert calls == []


def test_fails_closed_503_when_unconfigured(monkeypatch):
    s.unconfigure_all(monkeypatch)
    s.install_idp(monkeypatch)
    for dep in route_auth.ALL_DEPENDENCIES:
        client, calls = _app(dep)
        assert client.post("/x").status_code == 503
        assert client.post("/x", headers=s.all_valid_headers()).status_code == 503
        assert calls == []


def test_partially_configured_route_uses_the_configured_class(monkeypatch):
    s.unconfigure_all(monkeypatch)
    monkeypatch.setenv("LAB_HUB_API_KEY", s.HUB_KEY)
    client, calls = _app(route_auth.require_access_or_hub)
    assert client.post("/x").status_code == 401
    assert client.post("/x", headers=s.valid_headers(route_auth.ACCESS)).status_code == 401
    assert client.post("/x", headers=s.valid_headers(route_auth.HUB)).status_code == 200
    assert calls == [1]


def test_certs_outage_is_503_not_open(monkeypatch):
    s.configure_all(monkeypatch)
    s.install_idp(monkeypatch, fail=True)
    client, calls = _app(route_auth.require_access)
    assert client.post("/x", headers=s.valid_headers(route_auth.ACCESS)).status_code == 503
    assert calls == []
    # another valid class still works during an IdP outage
    client, calls = _app(route_auth.require_access_or_hub)
    assert client.post("/x", headers={**s.valid_headers(route_auth.ACCESS), **s.valid_headers(route_auth.HUB)}).status_code == 200


def test_unknown_caller_class_refused():
    with pytest.raises(ValueError):
        route_auth.require_callers("anyone")
    with pytest.raises(ValueError):
        route_auth.require_callers()


def test_email_routing_dependency_keeps_the_176_token_forms(monkeypatch):
    """require_access_or_control_plane_token: Access JWT, or CONTROL_PLANE_TOKEN as
    Bearer or X-API-Key (the #176 contract); never the Hub key or APP_API_KEY."""
    s.configure_all(monkeypatch)
    monkeypatch.setenv("APP_API_KEY", s.APP_KEY)
    dep = route_auth.require_access_or_control_plane_token
    assert dep in route_auth.ALL_DEPENDENCIES
    client, calls = _app(dep)
    for headers in (s.valid_headers(route_auth.ACCESS), s.valid_headers(route_auth.WORKER),
                    {"X-API-Key": s.WORKER_TOKEN}):
        assert client.post("/x", headers=headers).status_code == 200
    for headers in (s.valid_headers(route_auth.HUB), {"X-API-Key": s.APP_KEY},
                    {"Authorization": f"Bearer {s.APP_KEY}"}, {"X-API-Key": "wrong"}):
        assert client.post("/x", headers=headers).status_code == 401
    assert len(calls) == 3
    s.unconfigure_all(monkeypatch)
    assert client.post("/x", headers={"X-API-Key": s.WORKER_TOKEN}).status_code == 503


def test_access_writes_need_a_same_origin_signal(monkeypatch):
    """Review D1: the edge adds the JWT from the login cookie, so an Access-authenticated
    write needs Sec-Fetch-Site: same-origin or an allowlisted Origin, else 403."""
    s.configure_all(monkeypatch)
    for dep in (route_auth.require_access, route_auth.require_access_or_hub,
                route_auth.require_access_or_control_plane_token):
        client, calls = _app(dep)
        jwt_only = {"Cf-Access-Jwt-Assertion": s.mint()}
        for extra in ({}, {"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"},
                      {"Sec-Fetch-Site": "same-site"}, {"Origin": "null"},
                      {"Sec-Fetch-Site": "cross-site", "Origin": s.LAB_ORIGIN}):
            assert client.post("/x", headers={**jwt_only, **extra}).status_code == 403, (dep.__name__, extra)
        assert calls == []
        assert client.post("/x", headers={**jwt_only, "Sec-Fetch-Site": "same-origin"}).status_code == 200
        assert client.post("/x", headers={**jwt_only, "Origin": s.LAB_ORIGIN}).status_code == 200
        monkeypatch.delenv("LAB_ALLOWED_ORIGINS")
        assert client.post("/x", headers={**jwt_only, "Origin": s.LAB_ORIGIN}).status_code == 403
        monkeypatch.setenv("LAB_ALLOWED_ORIGINS", s.LAB_ORIGIN)
