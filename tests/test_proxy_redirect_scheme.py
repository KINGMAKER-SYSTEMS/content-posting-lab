"""Exercise entrypoint options and real Uvicorn/Starlette slash redirects.

The app import and server start are replaced; no production app startup,
network, services, credentials or deletion are involved.
"""
import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import runpy
import sys
import types
import unittest
from unittest.mock import patch

import uvicorn
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route


MAIN = Path(__file__).resolve().parents[1] / "main.py"
KEYS = ("FORWARDED_ALLOW_IPS", "RAILWAY_ENVIRONMENT_ID", "PORT")


@contextmanager
def entrypoint_env(values):
    # Touch only these three configuration keys; HOME stays unchanged.
    before = {key: os.environ.get(key) for key in KEYS}
    try:
        for key in KEYS:
            if key in values:
                os.environ[key] = values[key]
            else:
                os.environ.pop(key, None)
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def capture_start(values, name="__main__"):
    fake = types.ModuleType("app")
    fake.app = object()
    with entrypoint_env(values), patch.dict(sys.modules, {"app": fake}), patch.object(uvicorn, "run") as start:
        namespace = runpy.run_path(str(MAIN), run_name=name)
    return start, namespace, fake.app


async def health(request):
    return PlainTextResponse("healthy")


async def private(request):
    return PlainTextResponse("authentication required", status_code=401)


async def request(loaded, path, headers, client, scheme="http", query=b""):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await loaded({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": scheme, "path": path,
        "raw_path": path.encode(), "root_path": "", "query_string": query,
        "headers": [(b"host", b"lab.example")] + headers,
        "client": (client, 54321), "server": ("private-service", 8000),
    }, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], dict(start["headers"])


class ProxyRedirectSchemeTest(unittest.TestCase):
    def response(self, values, path="/api/health/", proto="https", client="10.0.0.7", scheme="http", query=b"", extra=()):
        start, _, _ = capture_start(values)
        self.assertEqual(start.call_count, 1)
        args, kwargs = start.call_args
        self.assertEqual(args, ("main:app",))
        app = Starlette(routes=[
            Route("/api/health", health), Route("/api/private", private),
            Route("/api/other/{value}", health),
        ])
        # Config loads the real middleware without opening sockets or running
        # an application lifespan. These are the actual entrypoint options.
        with entrypoint_env(values):
            config = uvicorn.Config(app, **kwargs, lifespan="off", ws="none", log_config=None)
            config.load()
        headers = list(extra)
        if proto is not None:
            headers.append((b"x-forwarded-proto", proto.encode()))
        return asyncio.run(request(config.loaded_app, path, headers, client, scheme, query))

    def assert_location(self, values, expected, **kwargs):
        status, headers = self.response(values, **kwargs)
        self.assertEqual(status, 307)
        self.assertEqual(headers[b"location"].decode(), expected)

    def test_railway_https_slash_redirect(self):
        self.assert_location({"RAILWAY_ENVIRONMENT_ID": "env-test"}, "https://lab.example/api/health")

    def test_railway_other_route_preserves_query(self):
        self.assert_location({"RAILWAY_ENVIRONMENT_ID": "env-test"}, "https://lab.example/api/other/item?q=one%20two", path="/api/other/item/", query=b"q=one%20two")

    def test_canonical_health_does_not_redirect(self):
        status, headers = self.response({"RAILWAY_ENVIRONMENT_ID": "env-test"}, path="/api/health")
        self.assertEqual(status, 200)
        self.assertNotIn(b"location", headers)

    def test_private_redirect_still_targets_private_route(self):
        self.assert_location({"RAILWAY_ENVIRONMENT_ID": "env-test"}, "https://lab.example/api/private", path="/api/private/")
        status, _ = self.response({"RAILWAY_ENVIRONMENT_ID": "env-test"}, path="/api/private")
        self.assertEqual(status, 401)

    def test_local_remote_peer_cannot_choose_https(self):
        self.assert_location({}, "http://lab.example/api/health")

    def test_empty_railway_marker_keeps_local_default(self):
        self.assert_location({"RAILWAY_ENVIRONMENT_ID": ""}, "http://lab.example/api/health")

    def test_blank_railway_marker_keeps_local_default(self):
        self.assert_location({"RAILWAY_ENVIRONMENT_ID": "  "}, "http://lab.example/api/health")

    def test_default_loopback_proxy_stays_trusted(self):
        self.assert_location({}, "https://lab.example/api/health", client="127.0.0.1")

    def test_explicit_allowlist_without_railway(self):
        self.assert_location({"FORWARDED_ALLOW_IPS": "10.0.0.7"}, "https://lab.example/api/health")

    def test_explicit_allowlist_rejects_other_peer_on_railway(self):
        self.assert_location({"RAILWAY_ENVIRONMENT_ID": "env-test", "FORWARDED_ALLOW_IPS": "10.0.0.8"}, "http://lab.example/api/health")

    def test_explicit_empty_allowlist_is_respected(self):
        self.assert_location({"RAILWAY_ENVIRONMENT_ID": "env-test", "FORWARDED_ALLOW_IPS": ""}, "http://lab.example/api/health")

    def test_explicit_wildcard_is_respected_off_railway(self):
        self.assert_location({"FORWARDED_ALLOW_IPS": "*"}, "https://lab.example/api/health")

    def test_missing_forwarded_scheme_keeps_transport_scheme(self):
        self.assert_location({"RAILWAY_ENVIRONMENT_ID": "env-test"}, "http://lab.example/api/health", proto=None)

    def test_existing_https_transport_stays_https(self):
        self.assert_location({}, "https://lab.example/api/health", proto=None, scheme="https")

    def test_forwarded_http_stays_http(self):
        self.assert_location({"RAILWAY_ENVIRONMENT_ID": "env-test"}, "http://lab.example/api/health", proto="http")

    def test_forwarded_host_does_not_replace_original_host(self):
        self.assert_location({"RAILWAY_ENVIRONMENT_ID": "env-test"}, "https://lab.example/api/health", extra=[(b"x-forwarded-host", b"other.example")])

    def test_entrypoint_preserves_other_server_settings(self):
        start, _, _ = capture_start({"PORT": "8123"})
        self.assertEqual(start.call_args.kwargs["port"], 8123)
        self.assertEqual(start.call_args.kwargs["host"], "0.0.0.0")
        self.assertIs(start.call_args.kwargs["reload"], False)
        self.assertEqual(start.call_args.kwargs["timeout_keep_alive"], 120)

    def test_import_reexports_app_without_starting_server(self):
        start, namespace, app = capture_start({}, name="lab_main_import_test")
        start.assert_not_called()
        self.assertIs(namespace["app"], app)


if __name__ == "__main__":
    unittest.main(verbosity=2)
