"""Pre-body checks follow the effective router's actual registration order."""

import asyncio

import pytest
from fastapi import APIRouter, Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient

from services.route_auth import HUB, RouteAuthMiddleware, require_callers, require_worker

pytestmark = pytest.mark.real_route_auth


@pytest.fixture(autouse=True)
def worker_key(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "dispatch-test-worker")


def exchange(app, path, method="GET", *, authenticated=False, websocket=False):
    entered, received, sent = [], [], []

    async def inner(scope, receive, send):
        entered.append(scope)

    async def receive():
        received.append(True)
        return {"type": "websocket.connect"} if websocket else {
            "type": "http.request", "body": b"{bad json", "more_body": False,
        }

    async def send(message):
        sent.append(message)

    scope = {
        "type": "websocket" if websocket else "http", "path": path,
        "raw_path": path.encode(), "method": method, "root_path": "",
        "scheme": "ws" if websocket else "http", "http_version": "1.1",
        "query_string": b"", "server": ("test", 80), "client": ("test", 1),
        "headers": [(b"authorization", b"Bearer dispatch-test-worker")] if authenticated else [],
        "subprotocols": [],
    }
    asyncio.run(RouteAuthMiddleware(inner, app.router)(scope, receive, send))
    return entered, received, sent


@pytest.mark.parametrize("public_iterator", [True, False])
def test_nested_prefixes_retain_include_level_auth_before_body(monkeypatch, public_iterator):
    if not public_iterator:
        from fastapi import routing
        monkeypatch.setattr(routing, "iter_route_contexts", None, raising=False)
    leaf = APIRouter()

    @leaf.post("/items/{item_id}")
    def create(item_id: str, body: dict):
        return {"item_id": item_id, "body": body}

    parent = APIRouter()
    parent.include_router(leaf, prefix="/inner", dependencies=[Depends(require_worker)])
    app = FastAPI()
    app.include_router(parent, prefix="/outer")
    entered, received, sent = exchange(app, "/outer/inner/items/one", method="POST")
    assert not entered and not received
    assert sent[0]["status"] == 401
    entered, received, sent = exchange(app, "/outer/inner/items/one", method="POST", authenticated=True)
    assert len(entered) == 1 and not received and not sent
    app.add_middleware(RouteAuthMiddleware, routes_owner=app.router)
    response = TestClient(app).post(
        "/outer/inner/items/one", json={"answer": 1},
        headers={"Authorization": "Bearer dispatch-test-worker"},
    )
    assert response.status_code == 200
    assert response.json() == {"item_id": "one", "body": {"answer": 1}}


@pytest.mark.parametrize("protected_first", [False, True])
def test_first_duplicate_path_controls_both_dispatch_and_auth(protected_first):
    app = FastAPI()
    public = APIRouter()
    protected = APIRouter(dependencies=[Depends(require_worker)])
    public.add_api_route("/same", lambda: "public", methods=["GET"])
    protected.add_api_route("/same", lambda: "protected", methods=["GET"])
    for router in ((protected, public) if protected_first else (public, protected)):
        app.include_router(router)
    entered, received, sent = exchange(app, "/same")
    assert not received
    if protected_first:
        assert not entered and sent[0]["status"] == 401
    else:
        assert len(entered) == 1 and not sent
    app.add_middleware(RouteAuthMiddleware, routes_owner=app.router)
    response = TestClient(app).get("/same", headers={"Authorization": "Bearer dispatch-test-worker"})
    assert response.json() == ("protected" if protected_first else "public")


@pytest.mark.parametrize("full_protected", [False, True])
def test_earlier_partial_match_does_not_hide_later_full_match(full_protected):
    app = FastAPI()
    post = APIRouter(dependencies=[] if full_protected else [Depends(require_worker)])
    get = APIRouter(dependencies=[Depends(require_worker)] if full_protected else [])
    post.add_api_route("/same", lambda: "post", methods=["POST"])
    get.add_api_route("/same", lambda: "get", methods=["GET"])
    app.include_router(post)
    app.include_router(get)
    entered, received, sent = exchange(app, "/same", method="GET")
    assert not received
    if full_protected:
        assert not entered and sent[0]["status"] == 401
    else:
        assert len(entered) == 1 and not sent
    app.add_middleware(RouteAuthMiddleware, routes_owner=app.router)
    response = TestClient(app).get("/same", headers={"Authorization": "Bearer dispatch-test-worker"})
    assert response.json() == "get"


def test_websocket_include_auth_precedes_receive_and_accept():
    leaf = APIRouter()

    @leaf.websocket("/events")
    async def events(websocket: WebSocket):
        await websocket.accept()

    parent = APIRouter()
    parent.include_router(leaf, prefix="/inner", dependencies=[Depends(require_worker)])
    app = FastAPI()
    app.include_router(parent, prefix="/outer")
    entered, received, sent = exchange(app, "/outer/inner/events", websocket=True)
    assert not entered and not received
    assert len(sent) == 1 and sent[0]["type"] == "websocket.close" and sent[0]["code"] == 1008
    entered, received, sent = exchange(app, "/outer/inner/events", websocket=True, authenticated=True)
    assert len(entered) == 1 and not received and not sent


def test_public_mount_stops_before_later_protected_catchall():
    app = FastAPI()

    async def mounted(scope, receive, send):
        pass

    app.mount("/static", mounted)
    catchall = APIRouter(dependencies=[Depends(require_worker)])
    catchall.add_api_route("/{path:path}", lambda path: path, methods=["GET"])
    app.include_router(catchall)
    entered, received, sent = exchange(app, "/static/item")
    assert len(entered) == 1 and not received and not sent
    entered, received, sent = exchange(app, "/other")
    assert not entered and not received and sent[0]["status"] == 401


def test_distinct_include_callers_keep_separate_cached_dependencies(monkeypatch):
    monkeypatch.setenv("LAB_HUB_API_KEY", "dispatch-test-hub")
    leaf = APIRouter()
    calls = []

    @leaf.post("/items")
    def create(body: dict):
        calls.append(body)
        return body

    app = FastAPI()
    app.include_router(leaf, prefix="/worker", dependencies=[Depends(require_worker)])
    app.include_router(leaf, prefix="/hub", dependencies=[Depends(require_callers(HUB))])
    app.add_middleware(RouteAuthMiddleware, routes_owner=app.router)
    client = TestClient(app)
    worker = {"Authorization": "Bearer dispatch-test-worker"}
    hub = {"X-API-Key": "dispatch-test-hub"}
    for _ in range(3):
        assert client.post("/worker/items", headers=worker, json={"caller": "worker"}).status_code == 200
        assert client.post("/hub/items", headers=hub, json={"caller": "hub"}).status_code == 200
        assert client.post("/worker/items", headers=hub, content=b"{bad json").status_code == 401
        assert client.post("/hub/items", headers=worker, content=b"{bad json").status_code == 401
    assert calls == [{"caller": "worker"}, {"caller": "hub"}] * 3
