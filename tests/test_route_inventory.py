from fastapi import APIRouter, FastAPI
from tests.route_inventory import registered_routes

def test_nested_hidden_routes_keep_prefixes_and_methods():
    app = FastAPI()
    parent = APIRouter()
    child = APIRouter()
    async def endpoint():
        return {"ok": True}
    child.add_api_route("/hidden", endpoint, methods=["GET"], include_in_schema=False)
    child.add_api_route("/rule/{rule_id}", endpoint, methods=["DELETE"])
    parent.include_router(child, prefix="/nested")
    app.include_router(parent, prefix="/outer")
    routes = {(path, methods) for _, path, methods in registered_routes(app)}
    assert ("/outer/nested/hidden", frozenset({"GET"})) in routes
    assert ("/outer/nested/rule/{rule_id}", frozenset({"DELETE"})) in routes
    assert "/outer/nested/hidden" not in app.openapi()["paths"]
