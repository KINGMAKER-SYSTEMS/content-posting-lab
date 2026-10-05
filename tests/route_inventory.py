"""Enumerate actual routes, including lazy FastAPI includes and hidden routes."""

def registered_routes(router, prefix=""):
    for route in router.routes:
        original = getattr(route, "original_router", None)
        context = getattr(route, "include_context", None)
        if original is not None and context is not None:
            yield from registered_routes(original, prefix + context.prefix)
        else:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if path and methods:
                yield route, prefix + path, frozenset(methods)
