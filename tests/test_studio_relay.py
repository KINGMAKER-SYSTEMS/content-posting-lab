"""Relay mode forwards requests byte-for-byte and never loads the Lab."""
import asyncio
import hashlib
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from aiohttp import web

from services.studio_relay import build_app, relay_origin

ROOT = Path(__file__).resolve().parents[1]
BODY = os.urandom(300_000)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(make_app, port):
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def target():
        asyncio.set_event_loop(loop)
        runner = web.AppRunner(loop.run_until_complete(make_app()))
        loop.run_until_complete(runner.setup())
        loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", port).start())
        ready.set()
        loop.run_forever()
    threading.Thread(target=target, daemon=True).start()
    assert ready.wait(10)
    return loop


@pytest.fixture(scope="module")
def relay():
    seen = {}

    async def upstream_app():
        async def clip(request):
            return web.Response(body=BODY, content_type="video/mp4", headers={"Set-Cookie": "a=1"})

        async def echo(request):
            seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
            return web.Response(status=201, body=await request.read(), headers={"X-Lab": "yes"})

        async def gone(request):
            return web.Response(status=404, text="artifact not found")

        async def moved(request):
            return web.Response(status=302, headers={"Location": "/elsewhere"})
        app = web.Application()
        app.router.add_get("/clip", clip)
        app.router.add_post("/echo", echo)
        app.router.add_get("/gone", gone)
        app.router.add_get("/moved", moved)
        return app
    up, port = _free_port(), _free_port()
    _serve(upstream_app, up)
    _serve(lambda: build_app(f"http://127.0.0.1:{up}"), port)
    return f"http://127.0.0.1:{port}", seen


def test_download_passes_through_byte_for_byte(relay):
    base, _ = relay
    with urllib.request.urlopen(base + "/clip?token=abc") as response:
        data = response.read()
        assert response.status == 200
        assert response.headers["Content-Type"] == "video/mp4"
        assert response.headers["Content-Length"] == str(len(BODY))
        assert response.headers["Set-Cookie"] == "a=1"
    assert hashlib.sha256(data).digest() == hashlib.sha256(BODY).digest()


def test_post_body_credentials_and_status_pass_through(relay):
    base, seen = relay
    request = urllib.request.Request(base + "/echo", data=b'{"x":1}', method="POST", headers={
        "Authorization": "Bearer t", "X-RT-Lane": "lane", "Content-Type": "application/json"})
    with urllib.request.urlopen(request) as response:
        assert response.status == 201
        assert response.read() == b'{"x":1}'
        assert response.headers["X-Lab"] == "yes"
    assert seen["headers"]["authorization"] == "Bearer t"
    assert seen["headers"]["x-rt-lane"] == "lane"
    assert seen["headers"]["x-forwarded-host"] == base.split("//")[1]


def test_upstream_errors_and_redirects_are_not_rewritten(relay):
    base, _ = relay
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(base + "/gone")
    assert error.value.code == 404 and error.value.read() == b"artifact not found"

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.build_opener(NoRedirect).open(base + "/moved")
    assert error.value.code == 302 and error.value.headers["Location"] == "/elsewhere"


def test_unreachable_upstream_answers_502():
    port = _free_port()
    _serve(lambda: build_app(f"http://127.0.0.1:{_free_port()}"), port)
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health")
    assert error.value.code == 502


@pytest.mark.parametrize("value", [
    "", "http://example.com", "https://example.com/path", "https://example.com?x=1",
    "https://user:pw@example.com", "ftp://example.com", "example.com",
])
def test_bad_relay_origin_is_refused(value):
    with pytest.raises(ValueError):
        relay_origin(value)


def test_relay_origin_is_normalised():
    assert relay_origin(" https://lab.example.com/ ") == "https://lab.example.com"


def test_main_in_relay_mode_serves_the_relay_and_never_imports_the_lab(relay, tmp_path):
    base, _ = relay
    port = _free_port()
    probe = tmp_path / "sitecustomize.py"
    probe.write_text(
        "import sys, importlib.abc\n"
        "class _Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path, target=None):\n"
        "        if name == 'app': raise ImportError('the Lab app must not load in relay mode')\n"
        "sys.meta_path.insert(0, _Block())\n")
    env = {**os.environ, "CONTENT_LAB_RELAY_ORIGIN": base, "PORT": str(port),
           "PYTHONPATH": f"{tmp_path}{os.pathsep}{ROOT}"}
    process = subprocess.Popen([sys.executable, "main.py"], cwd=ROOT, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 20
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/clip") as response:
                    assert hashlib.sha256(response.read()).digest() == hashlib.sha256(BODY).digest()
                break
            except (urllib.error.URLError, ConnectionError):
                assert process.poll() is None, process.stdout.read().decode()
                assert time.time() < deadline, "relay did not start"
                time.sleep(0.2)
    finally:
        process.terminate()
        process.wait(10)
