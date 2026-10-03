"""Unauthenticated static routes must never serve a file outside their tree.

Two public (non-/api/, so never behind APP_API_KEY) file routes in app.py:

- the SPA fallback ``GET /{full_path:path}`` used to do ``FRONTEND_DIR / full_path``
  with no containment, so ``//<abs path>``, ``../`` and ``%2e%2e/`` read any file
  the process could open (roster JSON, /proc/self/environ);
- the ``/projects`` StaticFiles mount sits on the Railway volume root
  (railway.toml mounts the volume at /app/projects), so page_roster.json,
  telegram_config.json, cookies.txt, control_plane_jobs.json and the
  bearer-gated control_plane_generated/ tree were one plain GET away.

Requests are driven through raw ASGI scopes as well as TestClient: httpx
normalises ``..`` client-side, uvicorn does not, so only a raw scope proves
what production receives.
"""
import asyncio
from urllib.parse import unquote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount

import app as app_module

SENTINEL = b"SENTINEL-OUTSIDE-DIST-7f3a9c"
INDEX = b"<!doctype html><title>lab spa index</title>"
ASSET = b"console.log('lab asset');"


def _asgi_get(asgi_app, raw_path: str) -> tuple[int, bytes]:
    """GET ``raw_path`` the way uvicorn delivers it: percent-decoded once, no
    dot-segment normalisation."""
    raw = raw_path.encode("latin-1")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": unquote(raw_path),
        "raw_path": raw,
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    status = 0
    body = bytearray()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    asyncio.run(asgi_app(scope, receive, send))
    return status, bytes(body)


# ---------------------------------------------------------------- SPA fallback


@pytest.fixture
def spa(tmp_path, monkeypatch):
    dist = tmp_path / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_bytes(INDEX)
    (dist / "assets" / "index-abc123.js").write_bytes(ASSET)
    (dist / "favicon.svg").write_bytes(b"<svg/>")
    secret = tmp_path / "secret.txt"
    secret.write_bytes(SENTINEL)
    (tmp_path / "frontend" / "roster.json").write_bytes(SENTINEL)
    # A symlink planted inside dist that points out of it.
    (dist / "assets" / "escape.txt").symlink_to(secret)
    monkeypatch.setattr(app_module, "FRONTEND_DIR", dist)
    test_app = FastAPI()
    # The exact production handler, mounted the way app.py mounts it.
    test_app.get("/{full_path:path}")(app_module.serve_frontend)
    return test_app, secret


def _spa_payloads(secret):
    abs_secret = str(secret)
    return [
        "/../secret.txt",
        "/../../secret.txt",
        "/..%2f..%2fsecret.txt",
        "/../../" + abs_secret.lstrip("/"),
        "/assets/../../secret.txt",
        "/assets/../../../secret.txt",
        "/%2e%2e/secret.txt",
        "/%2E%2E/%2e%2e/secret.txt",
        "/..%2fsecret.txt",
        "/%2e%2e%2fsecret.txt",
        "/assets%2f..%2f..%2fsecret.txt",
        "/../roster.json",
        "/%2e%2e/roster.json",
        "/" + abs_secret,  # //<absolute path>
        "//" + abs_secret.lstrip("/"),
        "/%2f" + abs_secret.lstrip("/"),
        "/..\\secret.txt",
        "/%5c..%5csecret.txt",
        "/..%5csecret.txt",
        "/assets\\..\\..\\secret.txt",
        "/%252e%252e/secret.txt",  # double encoding
        "/..%252fsecret.txt",
        "/../secret.txt%00.js",
        "/assets/escape.txt",  # symlink escaping dist
        "/./../secret.txt",
    ]


def test_spa_fallback_never_serves_files_outside_dist(spa):
    test_app, secret = spa
    for payload in _spa_payloads(secret):
        status, body = _asgi_get(test_app, payload)
        assert SENTINEL not in body, f"{payload!r} leaked a file outside frontend/dist"
        assert status in (200, 404), payload
        if status == 200:
            assert body == INDEX, f"{payload!r} served something other than index.html"


def test_spa_fallback_via_testclient_never_leaks(spa):
    test_app, secret = spa
    client = TestClient(test_app)
    for payload in _spa_payloads(secret):
        resp = client.get(payload)
        assert SENTINEL not in resp.content, payload


def test_spa_fallback_still_serves_assets_and_routes(spa):
    test_app, _ = spa
    client = TestClient(test_app)
    assert client.get("/assets/index-abc123.js").content == ASSET
    assert client.get("/favicon.svg").content == b"<svg/>"
    for route in ("/", "/burn", "/pages/some-page", "/clipper/job/123", "/assets/missing.js"):
        resp = client.get(route)
        assert resp.status_code == 200, route
        assert resp.content == INDEX, route
    # Raw-scope legit asset too.
    assert _asgi_get(test_app, "/assets/index-abc123.js") == (200, ASSET)


def test_frontend_file_rejects_before_touching_disk(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "a.js").write_bytes(b"a")
    ok = app_module._frontend_file("a.js", dist)
    assert ok is not None and ok.read_bytes() == b"a"
    for bad in ("", "/etc/passwd", "../a.js", "x/../a.js", "./a.js", "a.js/", "a\\b",
                "a\x00.js", "%2e%2e/a.js", "dir"):
        assert app_module._frontend_file(bad, dist) is None, bad


def test_spa_fallback_404_when_frontend_not_built(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "FRONTEND_DIR", tmp_path / "missing")
    test_app = FastAPI()
    test_app.get("/{full_path:path}")(app_module.serve_frontend)
    assert TestClient(test_app).get("/anything").status_code == 404


def test_real_app_catch_all_uses_confined_handler():
    catch_alls = [r for r in app_module.app.routes if getattr(r, "path", None) == "/{full_path:path}"]
    for route in catch_alls:  # registered only when frontend/dist exists at import
        assert route.endpoint is app_module.serve_frontend


# ------------------------------------------------------------ /projects mount


@pytest.fixture
def projects(tmp_path):
    root = tmp_path / "vol" / "projects"
    (root / "demo" / "videos" / "slideshow").mkdir(parents=True)
    (root / "demo" / "clips" / "job1").mkdir(parents=True)
    (root / "demo" / "slideshow-images").mkdir(parents=True)
    (root / "demo" / "videos" / "a.mp4").write_bytes(b"VIDEO-A")
    (root / "demo" / "videos" / "slideshow" / "s.mp4").write_bytes(b"VIDEO-S")
    (root / "demo" / "clips" / "job1" / "clip_000.mp4").write_bytes(b"CLIP-0")
    (root / "demo" / "slideshow-images" / "i.jpg").write_bytes(b"IMG")
    # Volume-root state that must never be public.
    for name in ("page_roster.json", "telegram_config.json", "cookies.txt",
                 "control_plane_jobs.json", "content_requests.json",
                 "agenticnews.db", "abn_memory.json", ".env"):
        (root / name).write_bytes(SENTINEL)
    (root / "demo" / "jobs.json").write_bytes(SENTINEL)
    # JSON/text sidecars inside media dirs are API-only state, never public.
    (root / "demo" / "clips" / "job1" / "_state.json").write_bytes(SENTINEL)
    (root / "demo" / "clips" / "job1" / "job_meta.json").write_bytes(SENTINEL)
    (root / "demo" / "clips" / "job1" / "clips.zip").write_bytes(SENTINEL)
    (root / "demo" / "videos" / "prompts.json").write_bytes(SENTINEL)
    (root / "demo" / "burned" / "b1").mkdir(parents=True)
    (root / "demo" / "burned" / "b1" / "batch_meta.json").write_bytes(SENTINEL)
    (root / "demo" / "burned" / "b1" / "burned_000.MP4").write_bytes(b"BURNED")
    (root / "demo" / "captions" / "artist").mkdir(parents=True)
    (root / "demo" / "captions" / "artist" / "captions.csv").write_bytes(SENTINEL)
    (root / "demo" / "videos" / "noext").write_bytes(SENTINEL)
    (root / "demo" / "clips" / "job1" / "clip_000_thumb.jpg").write_bytes(b"THUMB")
    (root / "demo" / "slideshow-audio").mkdir(parents=True)
    (root / "demo" / "slideshow-audio" / "t.m4a").write_bytes(b"AUDIO")
    (root / "demo" / "videos" / ".secret").write_bytes(SENTINEL)
    gen = root / "control_plane_generated" / "page1" / "videos"
    gen.mkdir(parents=True)
    (gen / "g.mp4").write_bytes(SENTINEL)
    rec = root / "control_plane_recipes"
    rec.mkdir()
    (rec / "r.json").write_bytes(SENTINEL)
    (tmp_path / "vol" / "outside.txt").write_bytes(SENTINEL)
    # Symlinks planted under allowed media dirs that resolve to volume state.
    (root / "demo" / "videos" / "roster.mp4").symlink_to(root / "page_roster.json")
    (root / "demo" / "videos" / "ck.mp4").symlink_to(root / "cookies.txt")
    (root / "demo" / "videos" / "gen.mp4").symlink_to(gen / "g.mp4")
    (root / "demo" / "videos" / "jobs.mp4").symlink_to(root / "demo" / "jobs.json")
    (root / "demo" / "clips" / "job1" / "out.mp4").symlink_to(tmp_path / "vol" / "outside.txt")
    (root / "demo" / "clips" / "linkdir").symlink_to(root / "control_plane_generated")
    # A symlink that stays inside project media is still served.
    (root / "demo" / "videos" / "alias.mp4").symlink_to(root / "demo" / "videos" / "a.mp4")
    asgi = Starlette(routes=[
        Mount("/projects", app=app_module.ProjectMediaFiles(directory=str(root), check_dir=False)),
    ])
    return asgi


def test_projects_mount_serves_project_media(projects):
    client = TestClient(projects)
    assert client.get("/projects/demo/videos/a.mp4").content == b"VIDEO-A"
    assert client.get("/projects/demo/videos/slideshow/s.mp4").content == b"VIDEO-S"
    assert client.get("/projects/demo/clips/job1/clip_000.mp4").content == b"CLIP-0"
    assert client.get("/projects/demo/slideshow-images/i.jpg").content == b"IMG"
    assert _asgi_get(projects, "/projects/demo/videos/a.mp4") == (200, b"VIDEO-A")
    assert client.get("/projects/demo/videos/alias.mp4").content == b"VIDEO-A"
    assert client.get("/projects/demo/clips/job1/clip_000_thumb.jpg").content == b"THUMB"
    assert client.get("/projects/demo/burned/b1/burned_000.MP4").content == b"BURNED"
    assert client.get("/projects/demo/slideshow-audio/t.m4a").content == b"AUDIO"


def test_projects_mount_refuses_volume_state_and_traversal(projects):
    payloads = [
        "/projects/page_roster.json",
        "/projects/telegram_config.json",
        "/projects/cookies.txt",
        "/projects/control_plane_jobs.json",
        "/projects/content_requests.json",
        "/projects/agenticnews.db",
        "/projects/abn_memory.json",
        "/projects/.env",
        "/projects/demo/jobs.json",
        "/projects/demo/videos/.secret",
        "/projects/control_plane_generated/page1/videos/g.mp4",
        "/projects/control_plane_recipes/r.json",
        "/projects/demo/videos/../../page_roster.json",
        "/projects/demo/videos/%2e%2e/%2e%2e/page_roster.json",
        "/projects/demo/videos/..%2f..%2fcookies.txt",
        "/projects/demo/videos/..%5c..%5ccookies.txt",
        "/projects/../outside.txt",
        "/projects/%2e%2e/outside.txt",
        "/projects/demo/videos/../../../outside.txt",
        "/projects//" + "page_roster.json",
        "/projects/demo/videos/roster.mp4",
        "/projects/demo/videos/ck.mp4",
        "/projects/demo/videos/gen.mp4",
        "/projects/demo/videos/jobs.mp4",
        "/projects/demo/clips/job1/out.mp4",
        "/projects/demo/clips/linkdir/page1/videos/g.mp4",
        "/projects/demo/videos/a.mp4%00",
        "/projects/demo/clips/job1/_state.json",
        "/projects/demo/clips/job1/job_meta.json",
        "/projects/demo/clips/job1/clips.zip",
        "/projects/demo/videos/prompts.json",
        "/projects/demo/burned/b1/batch_meta.json",
        "/projects/demo/captions/artist/captions.csv",
        "/projects/demo/videos/noext",
    ]
    client = TestClient(projects)
    for payload in payloads:
        status, body = _asgi_get(projects, payload)
        assert SENTINEL not in body, f"{payload!r} leaked volume state"
        assert status == 404, (payload, status)
        assert SENTINEL not in client.get(payload).content, payload


def test_real_app_projects_mount_is_confined():
    mounts = [r for r in app_module.app.routes if isinstance(r, Mount) and r.path == "/projects"]
    assert len(mounts) == 1
    assert isinstance(mounts[0].app, app_module.ProjectMediaFiles)


def test_other_static_mounts_404_on_nul(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"A")
    asgi = Starlette(routes=[
        Mount("/f", app=app_module.SafeStaticFiles(directory=str(tmp_path), check_dir=False)),
    ])
    assert _asgi_get(asgi, "/f/a.txt") == (200, b"A")
    assert _asgi_get(asgi, "/f/a.txt%00.png")[0] == 404


def test_real_app_static_mounts_are_nul_safe():
    mounts = {r.path: r.app for r in app_module.app.routes if isinstance(r, Mount)}
    for path in ("/fonts", "/agenticnews-assets", "/projects", "/output", "/caption-output", "/burn-output"):
        assert isinstance(mounts[path], app_module.SafeStaticFiles), path
