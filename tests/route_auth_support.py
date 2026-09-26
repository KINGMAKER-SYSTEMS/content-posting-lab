"""Shared helpers for route-auth tests: dummy credentials and a local Access IdP.

The repo is public: every value here is a dummy. The Access "IdP" is an RSA
keypair generated in-process; its JWKS is served to ``PyJWKClient`` by
replacing ``fetch_data``, so nothing touches the network.
"""

from __future__ import annotations

import json
import re
import time

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import Mount, Route

from services import route_auth

WORKER_TOKEN = "dummy-worker-bearer-not-a-secret"
HUB_KEY = "dummy-hub-key-not-a-secret"
APP_KEY = "dummy-app-api-key-not-a-secret"
TEAM_DOMAIN = "https://dummy-team.cloudflareaccess.com"
ACCESS_AUD = "dummy-lab-aud-tag-0000000000000000"
KID = "dummy-kid-1"


def _new_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


IDP_KEY = _new_key()
OTHER_KEY = _new_key()


def jwks_for(private_key, kid: str = KID) -> dict:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return {"keys": [jwk]}


def mint(
    *,
    key=None,
    kid: str = KID,
    aud=ACCESS_AUD,
    iss: str = TEAM_DOMAIN,
    exp_offset: int = 600,
    alg: str = "RS256",
    claims: dict | None = None,
) -> str:
    now = int(time.time())
    payload = {
        "aud": [aud] if isinstance(aud, str) else aud,
        "iss": iss,
        "iat": now - 5,
        "exp": now + exp_offset,
        "email": "operator@example.invalid",
        "sub": "dummy-subject",
    }
    payload.update(claims or {})
    if alg == "none":
        return jwt.encode(payload, None, algorithm="none", headers={"kid": kid})
    if alg.startswith("HS"):
        return jwt.encode(payload, "dummy-shared-secret-32-bytes-long!!", algorithm=alg, headers={"kid": kid})
    return jwt.encode(payload, key or IDP_KEY, algorithm=alg, headers={"kid": kid})


def install_idp(monkeypatch, jwks: dict | None = None, *, fail: bool = False) -> list[str]:
    """Serve ``jwks`` for the dummy team's certs URL; returns the fetch log."""
    fetches: list[str] = []
    url = f"{TEAM_DOMAIN}/cdn-cgi/access/certs"
    client = jwt.PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=5)

    def fetch_data():
        fetches.append(url)
        if fail:
            raise jwt.PyJWKClientConnectionError("dummy certs outage")
        return jwks if jwks is not None else jwks_for(IDP_KEY)

    monkeypatch.setattr(client, "fetch_data", fetch_data)
    monkeypatch.setattr(route_auth, "_jwk_clients", {url: client})
    return fetches


def configure_all(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", WORKER_TOKEN)
    monkeypatch.setenv("LAB_HUB_API_KEY", HUB_KEY)
    monkeypatch.setenv("LAB_ACCESS_TEAM_DOMAIN", TEAM_DOMAIN)
    monkeypatch.setenv("LAB_ACCESS_AUD", ACCESS_AUD)
    install_idp(monkeypatch)


def unconfigure_all(monkeypatch) -> None:
    for name in ("CONTROL_PLANE_TOKEN", "LAB_HUB_API_KEY", "LAB_ACCESS_TEAM_DOMAIN", "LAB_ACCESS_AUD"):
        monkeypatch.delenv(name, raising=False)


def valid_headers(caller: str) -> dict[str, str]:
    if caller == route_auth.WORKER:
        return {"Authorization": f"Bearer {WORKER_TOKEN}"}
    if caller == route_auth.HUB:
        return {"X-API-Key": HUB_KEY}
    if caller == route_auth.ACCESS:
        return {"Cf-Access-Jwt-Assertion": mint()}
    raise ValueError(caller)


def all_valid_headers() -> dict[str, str]:
    out: dict[str, str] = {}
    for caller in route_auth.CALLER_CLASSES:
        out.update(valid_headers(caller))
    return out


def wrong_header_sets() -> dict[str, dict[str, str]]:
    """Credentials that must never pass: wrong values, wrong slots, forged JWTs."""
    return {
        "wrong-bearer": {"Authorization": "Bearer wrong-token"},
        "wrong-x-api-key": {"X-API-Key": "wrong-key"},
        "hub-key-as-bearer": {"Authorization": f"Bearer {HUB_KEY}"},
        "worker-token-as-x-api-key": {"X-API-Key": WORKER_TOKEN},
        "app-api-key": {"X-API-Key": APP_KEY, "Authorization": f"Bearer {APP_KEY}"},
        "jwt-other-key": {"Cf-Access-Jwt-Assertion": mint(key=OTHER_KEY)},
        "jwt-unsigned": {"Cf-Access-Jwt-Assertion": mint(alg="none")},
        "jwt-wrong-aud": {"Cf-Access-Jwt-Assertion": mint(aud="another-app-aud")},
        "jwt-expired": {"Cf-Access-Jwt-Assertion": mint(exp_offset=-3600)},
        "cf-identity-headers-only": {
            "Cf-Access-Authenticated-User-Email": "operator@example.invalid",
            "Cf-Access-Jwt-Assertion": "not-a-jwt",
        },
    }


# ── route enumeration shared with the credential sweep ───────────────


def route_keys(app) -> list[tuple[str, str]]:
    """Every (method, path) the app answers: APIRoutes, websockets, plain routes, mounts."""
    keys: list[tuple[str, str]] = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            keys.extend((m, route.path) for m in sorted(route.methods))
        elif isinstance(route, APIWebSocketRoute):
            keys.append(("WS", route.path))
        elif isinstance(route, Mount):
            keys.append(("MOUNT", route.path))
        elif isinstance(route, Route):
            keys.extend((m, route.path) for m in sorted(route.methods or ()))
        else:  # pragma: no cover - a new route type must be classified
            keys.append((type(route).__name__, getattr(route, "path", "?")))
    return keys


def find_route(app, method: str, path: str):
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == path and method in route.methods:
            return route
        if method == "WS" and isinstance(route, APIWebSocketRoute) and route.path == path:
            return route
    raise KeyError((method, path))


DUMMY_PARAMS = {
    "integration_id": "acct:dummy",
    "page_id": "dummy-page",
    "project_name": "dummy-project",
    "name": "dummy-project",
    "job_id": "cpl-0000000000000000",
    "jid": "dummy-job",
    "batch_id": "dummy-batch",
    "poster_id": "dummy-poster",
    "user_id": "1",
    "sound_id": "dummy-sound",
    "rule_id": "dummy-rule",
    "index": "0",
    "ep_id": "dummy-ep",
    "vid": "dummy-vid",
    "note_id": "dummy-note",
    "project_id": "dummy-project",
    "filename": "dummy.png",
    "account_name": "dummy-account",
    "request_id": "dummy-request",
    "username": "dummy-user",
    "kind": "final",
    "library_id": "dummy-library",
    "clip_sha256": "0" * 64,
    "full_path": "index.html",
}


def fill(path: str) -> str:
    return re.sub(r"\{([^}:]+)(?::[^}]*)?\}", lambda m: DUMMY_PARAMS.get(m.group(1), "x"), path)
