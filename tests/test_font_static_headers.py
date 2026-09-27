"""/fonts answers as a font with a cache keyed by the file's own bytes.

The Control Plane proxies /fonts/<file> for every Dossier open and the Schedule
board's caption faces (about 510 KB a Dossier open). python:3.11-slim ships no
/etc/mime.types, so StaticFiles guessed application/octet-stream and sent no
Cache-Control; the proxy then refused to cache a body that was not declared a
font. The mount now names the font type itself and sends a strong, content
hash ETag with a cache header: a day by default, and a year, immutable, when
the request pins the same hash with ?v=.
"""
import hashlib
import mimetypes

from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount

import app as app_module


def _client(tmp_path, monkeypatch):
    # The production image has no mime database: prove the type does not depend on one.
    monkeypatch.setattr(mimetypes, "guess_type", lambda *a, **k: (None, None))
    (tmp_path / "Face.ttf").write_bytes(b"\x00\x01\x00\x00ttf-bytes")
    (tmp_path / "Face.woff2").write_bytes(b"wOF2woff2-bytes")
    asgi = Starlette(routes=[Mount("/fonts", app=app_module.FontStaticFiles(directory=str(tmp_path), check_dir=False))])
    return TestClient(asgi)


def test_fonts_are_served_as_fonts_without_a_mime_database(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ttf = client.get("/fonts/Face.ttf")
    assert ttf.status_code == 200
    assert ttf.headers["content-type"] == "font/ttf"
    assert client.get("/fonts/Face.woff2").headers["content-type"] == "font/woff2"


def test_font_cache_is_keyed_by_the_file_hash(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    digest = hashlib.sha256(b"\x00\x01\x00\x00ttf-bytes").hexdigest()
    plain = client.get("/fonts/Face.ttf")
    assert plain.headers["etag"] == f'"{digest}"'
    assert plain.headers["cache-control"] == "public, max-age=86400"
    pinned = client.get(f"/fonts/Face.ttf?v={digest[:16]}")
    assert pinned.headers["cache-control"] == "public, max-age=31536000, immutable"
    stale = client.get("/fonts/Face.ttf?v=0000000000000000")
    assert stale.headers["cache-control"] == "public, max-age=86400"
    revalidated = client.get("/fonts/Face.ttf", headers={"If-None-Match": f'"{digest}"'})
    assert revalidated.status_code == 304


def test_font_misses_are_not_cached(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    missing = client.get("/fonts/Nope.ttf")
    assert missing.status_code == 404
    assert "immutable" not in missing.headers.get("cache-control", "")


def test_real_app_fonts_mount_is_the_font_server():
    mounts = {r.path: r.app for r in app_module.app.routes if isinstance(r, Mount)}
    assert isinstance(mounts["/fonts"], app_module.FontStaticFiles)
    assert isinstance(mounts["/fonts"], app_module.SafeStaticFiles), "keeps the NUL-byte 404"
