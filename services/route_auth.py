"""Per-route caller authentication that fails CLOSED on its own.

The global ``check_api_key`` middleware in app.py only enforces anything when
APP_API_KEY is set, and production keeps it unset. Every route that spends
provider money, changes state or returns roster PII therefore carries one of
the dependencies below, which work regardless of APP_API_KEY.

Caller classes (a route lists the classes that may call it; ANY ONE valid
credential from those classes is accepted, nothing else):

- ``WORKER``: the control-plane Worker. ``Authorization: Bearer`` equal to
  ``CONTROL_PLANE_TOKEN``. The email-routing routes also take that token as
  ``X-API-Key`` (the #176 contract), via ``worker_token_in_x_api_key``.
- ``HUB``: the Campaign Hub proxies (and other server-side scripts given the
  same key). ``X-API-Key`` equal to ``LAB_HUB_API_KEY``. APP_API_KEY is never
  accepted here: the frontend can bake it into its public bundle.
- ``ACCESS``: a person (or an Access service token) that passed the
  Cloudflare Access application in front of the Lab. The edge adds
  ``Cf-Access-Jwt-Assertion``; it is verified here (RS256 signature against
  the team's certs, ``aud`` = ``LAB_ACCESS_AUD``, ``iss`` = the team domain,
  ``exp``/``iat`` required). An unverified header is never trusted.

Status codes: 503 when none of the route's caller classes is configured
(missing env), 401 when no presented credential is valid for the route, 403
for a cross-origin Access-authenticated write. The handler never runs.

CSRF: the edge adds the Access JWT from the operator's login cookie, so a
cross-site form POST or websocket from another page would arrive with a valid
JWT. An Access-authenticated unsafe request (POST/PUT/PATCH/DELETE, or any
websocket) therefore also needs a same-origin signal: ``Sec-Fetch-Site:
same-origin``, or an ``Origin`` listed in ``LAB_ALLOWED_ORIGINS``
(comma-separated, e.g. the Lab's Access hostname). Bearer and Hub-key callers
are not cookie-borne and need neither.

``RouteAuthMiddleware`` runs the same check before routing hands the request
to FastAPI, so an unauthenticated body is never parsed (no 422, no multipart
spool); the per-route dependency stays as defence in depth.
"""

from __future__ import annotations

import hmac
import logging
import os
import threading
from typing import Callable, Iterable

import jwt
from fastapi import HTTPException, WebSocketException
from starlette.requests import HTTPConnection
from starlette.status import WS_1008_POLICY_VIOLATION

log = logging.getLogger("route_auth")

WORKER = "worker"
HUB = "hub"
ACCESS = "access"
CALLER_CLASSES = (WORKER, HUB, ACCESS)

ENV_WORKER_TOKEN = "CONTROL_PLANE_TOKEN"
ENV_HUB_KEY = "LAB_HUB_API_KEY"
ENV_ACCESS_TEAM_DOMAIN = "LAB_ACCESS_TEAM_DOMAIN"
ENV_ACCESS_AUD = "LAB_ACCESS_AUD"
ENV_ALLOWED_ORIGINS = "LAB_ALLOWED_ORIGINS"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

ACCESS_JWT_HEADER = "cf-access-jwt-assertion"
ACCESS_ALGORITHMS = ["RS256"]
ACCESS_LEEWAY_S = 30


class _Unavailable(Exception):
    """The identity provider's signing keys could not be fetched."""


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _access_config() -> tuple[str, list[str]] | None:
    """(issuer, audiences) when Access verification is configured, else None."""
    team = _env(ENV_ACCESS_TEAM_DOMAIN).rstrip("/")
    auds = [a.strip() for a in _env(ENV_ACCESS_AUD).split(",") if a.strip()]
    if not team or not auds:
        return None
    if not team.startswith("https://"):
        team = "https://" + team.removeprefix("http://")
    return team, auds


def configured(caller: str) -> bool:
    if caller == WORKER:
        return bool(_env(ENV_WORKER_TOKEN))
    if caller == HUB:
        return bool(_env(ENV_HUB_KEY))
    if caller == ACCESS:
        return _access_config() is not None
    raise ValueError(caller)


# ── Cloudflare Access JWT ────────────────────────────────────────────

_jwk_clients: dict[str, jwt.PyJWKClient] = {}
_jwk_lock = threading.Lock()


def _jwk_client(certs_url: str) -> jwt.PyJWKClient:
    with _jwk_lock:
        client = _jwk_clients.get(certs_url)
        if client is None:
            client = jwt.PyJWKClient(certs_url, cache_keys=True, lifespan=3600, timeout=5)
            _jwk_clients[certs_url] = client
        return client


def _signing_key(token: str, issuer: str):
    try:
        return _jwk_client(f"{issuer}/cdn-cgi/access/certs").get_signing_key_from_jwt(token).key
    except jwt.PyJWKClientConnectionError as exc:
        raise _Unavailable() from exc


def verify_access_jwt(token: str) -> dict | None:
    """Claims of a valid Access JWT for this app, else None.

    Raises ``_Unavailable`` only when the certs cannot be fetched.
    """
    config = _access_config()
    if config is None or not token:
        return None
    issuer, audiences = config
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return None
    if header.get("alg") not in ACCESS_ALGORITHMS:
        return None
    try:
        key = _signing_key(token, issuer)
        return jwt.decode(
            token,
            key,
            algorithms=ACCESS_ALGORITHMS,
            audience=audiences,
            issuer=issuer,
            leeway=ACCESS_LEEWAY_S,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except _Unavailable:
        raise
    except (jwt.PyJWTError, ValueError):
        return None


# ── the dependency ───────────────────────────────────────────────────


def _same(supplied: str, expected: str) -> bool:
    return bool(supplied) and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _presented_valid(conn: HTTPConnection, caller: str, worker_token_in_x_api_key: bool = False) -> bool:
    headers = conn.headers
    if caller == WORKER:
        expected = _env(ENV_WORKER_TOKEN)
        auth = headers.get("authorization", "")
        if auth.startswith("Bearer ") and _same(auth[len("Bearer "):].strip(), expected):
            return True
        return worker_token_in_x_api_key and _same(headers.get("x-api-key", "").strip(), expected)
    if caller == HUB:
        return _same(headers.get("x-api-key", "").strip(), _env(ENV_HUB_KEY))
    if caller == ACCESS:
        return verify_access_jwt(headers.get(ACCESS_JWT_HEADER, "").strip()) is not None
    raise ValueError(caller)


def _same_origin(conn: HTTPConnection) -> bool:
    """True when the request carries a same-origin signal (see the module docstring)."""
    site = conn.headers.get("sec-fetch-site", "").strip().lower()
    if site:
        return site == "same-origin"
    origin = conn.headers.get("origin", "").strip()
    allowed = {o.strip().rstrip("/") for o in _env(ENV_ALLOWED_ORIGINS).split(",") if o.strip()}
    return bool(origin) and origin.rstrip("/") in allowed


def side_effect_get(endpoint):
    """Mark a GET endpoint that changes state, so Access callers get the CSRF check."""
    endpoint.route_auth_side_effect = True
    return endpoint


def before_body(check):
    """Mark a dependency that reads only headers, so RouteAuthMiddleware runs it
    before the body is parsed (TOKEN/HMAC routes that are not route_auth rows)."""
    check.route_auth_before_body = True
    return check


def _needs_origin(conn: HTTPConnection) -> bool:
    if conn.scope.get("type") == "websocket":
        return True
    if conn.scope.get("method", "GET").upper() in UNSAFE_METHODS:
        return True
    return bool(getattr(conn.scope.get("endpoint"), "route_auth_side_effect", False))


def _deny(conn: HTTPConnection, status: int, detail: str):
    if conn.scope.get("type") == "websocket":
        raise WebSocketException(code=WS_1008_POLICY_VIOLATION, reason=detail)
    raise HTTPException(status_code=status, detail=detail)


def authorize(conn: HTTPConnection, callers: Iterable[str], worker_token_in_x_api_key: bool = False) -> str:
    """Return the caller class that authenticated, or raise 503/401/403."""
    live = [c for c in callers if configured(c)]
    if not live:
        _deny(conn, 503, "Route authentication is not configured")
    unavailable = False
    cross_origin = False
    for caller in live:
        try:
            if _presented_valid(conn, caller, worker_token_in_x_api_key):
                if caller == ACCESS and _needs_origin(conn) and not _same_origin(conn):
                    cross_origin = True
                    continue
                return caller
        except _Unavailable:
            unavailable = True
    if cross_origin:
        _deny(conn, 403, "Cross-origin request refused")
    if unavailable:
        log.warning("Access certs unavailable; refusing %s", conn.url.path)
        _deny(conn, 503, "Operator identity verification is unavailable")
    _deny(conn, 401, "Invalid or missing credential")
    raise AssertionError("unreachable")


def require_callers(*callers: str, worker_token_in_x_api_key: bool = False) -> Callable[[HTTPConnection], None]:
    """Build a FastAPI dependency admitting exactly these caller classes."""
    allowed = tuple(dict.fromkeys(callers))
    if not allowed or any(c not in CALLER_CLASSES for c in allowed):
        raise ValueError(f"unknown caller classes: {callers}")
    if worker_token_in_x_api_key and WORKER not in allowed:
        raise ValueError("worker_token_in_x_api_key needs the worker class")

    def dependency(conn: HTTPConnection) -> None:
        if worker_token_in_x_api_key:
            authorize(conn, allowed, worker_token_in_x_api_key=True)
        else:
            authorize(conn, allowed)

    dependency.__name__ = "require_" + "_or_".join(allowed)
    dependency.route_auth_callers = frozenset(allowed)  # type: ignore[attr-defined]
    dependency.worker_token_in_x_api_key = worker_token_in_x_api_key  # type: ignore[attr-defined]
    return dependency


# The only instances routes use (the CI guard reads ``route_auth_callers``).
require_worker = require_callers(WORKER)
require_access = require_callers(ACCESS)
require_access_or_hub = require_callers(ACCESS, HUB)
# Email routing (#176): CONTROL_PLANE_TOKEN as Bearer or X-API-Key, or an operator.
require_access_or_control_plane_token = require_callers(ACCESS, WORKER, worker_token_in_x_api_key=True)

ALL_DEPENDENCIES = (
    require_worker,
    require_access,
    require_access_or_hub,
    require_access_or_control_plane_token,
)


# ── pre-routing enforcement ──────────────────────────────────────────


def before_body_checks(route) -> list:
    """Header-only dependencies marked with ``before_body`` on this route."""
    dependant = getattr(route, "dependant", None)
    stack = list(dependant.dependencies) if dependant is not None else []
    found = []
    while stack:
        dep = stack.pop()
        if getattr(dep.call, "route_auth_before_body", False):
            found.append(dep.call)
        stack.extend(dep.dependencies)
    return found


def route_requirement(route) -> tuple[frozenset[str], bool] | None:
    """(callers, worker_token_in_x_api_key) of a route's route_auth dependency, if any."""
    dependant = getattr(route, "dependant", None)
    stack = list(dependant.dependencies) if dependant is not None else []
    while stack:
        dep = stack.pop()
        callers = getattr(dep.call, "route_auth_callers", None)
        if callers:
            return callers, bool(getattr(dep.call, "worker_token_in_x_api_key", False))
        stack.extend(dep.dependencies)
    return None


class RouteAuthMiddleware:
    """Authenticate route_auth-gated routes before the body is read.

    Finds the route the router would pick (first full match) and, when it
    carries a route_auth dependency, runs ``authorize`` with the same caller
    classes. A refusal is answered here: FastAPI never parses the body.
    """

    def __init__(self, app, routes_owner) -> None:
        self.app = app
        self._owner = routes_owner
        self._cache: dict[int, tuple] = {}

    def _requirement(self, scope):
        """(route_auth requirement, before-body checks, child scope) of the matched route."""
        from starlette.routing import Match

        for route in self._owner.routes:
            match, child_scope = route.matches(scope)
            if match == Match.FULL:
                key = id(route)
                if key not in self._cache:
                    self._cache[key] = (route_requirement(route), before_body_checks(route))
                requirement, checks = self._cache[key]
                return requirement, checks, child_scope
        return None, [], {}

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        requirement, checks, child_scope = self._requirement(scope)
        if requirement is not None or checks:
            from starlette.requests import Request

            routed = {**scope, **child_scope}
            try:
                if requirement is not None:
                    callers, token_in_key = requirement
                    authorize(HTTPConnection(routed), tuple(sorted(callers)), worker_token_in_x_api_key=token_in_key)
                for check in checks:
                    check(Request(routed) if routed["type"] == "http" else HTTPConnection(routed))
            except HTTPException as exc:
                from fastapi.responses import JSONResponse

                await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, send)
                return
            except WebSocketException as exc:
                from starlette.websockets import WebSocketClose

                await WebSocketClose(code=exc.code, reason=exc.reason or "")(scope, receive, send)
                return
        await self.app(scope, receive, send)
