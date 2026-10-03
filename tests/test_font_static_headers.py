"""Font responses retain their MIME type, exact-byte validator and cache policy."""
import hashlib
import mimetypes
import os

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


def test_replaced_font_bytes_change_the_validator_even_with_the_same_size_and_mtime(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    original = client.get("/fonts/Face.ttf")
    font = tmp_path / "Face.ttf"
    stat = font.stat()
    replacement = tmp_path / "replacement.ttf"
    content = b"\x00\x01\x00\x00new-bytes"
    assert len(content) == stat.st_size
    replacement.write_bytes(content)
    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    replacement.replace(font)
    digest = hashlib.sha256(content).hexdigest()

    response = client.get(f"/fonts/Face.ttf?v={original.headers['etag'][1:17]}", headers={
        "If-None-Match": original.headers["etag"], "If-Modified-Since": original.headers["last-modified"],
    })
    assert response.status_code == 200
    assert response.content == content
    assert response.headers["etag"] == f'"{digest}"'
    assert response.headers["cache-control"] == "public, max-age=86400"
    pinned = client.get(f"/fonts/Face.ttf?v={digest[:16]}", headers={"If-None-Match": f'W/"{digest}"'})
    assert pinned.status_code == 304
    assert pinned.content == b""
    assert pinned.headers["etag"] == f'"{digest}"'
    assert pinned.headers["cache-control"] == "public, max-age=31536000, immutable"
    # An in-place edit also keeps the inode, size and restored mtime.
    content = b"\x00\x01\x00\x00end-bytes"
    font.write_bytes(content)
    os.utime(font, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    edited = client.get("/fonts/Face.ttf", headers={"If-None-Match": f'"{digest}"'})
    assert edited.status_code == 200
    assert edited.content == content
    assert edited.headers["etag"] == f'"{hashlib.sha256(content).hexdigest()}"'


def test_font_cache_reuses_unchanged_bytes_and_evicts_old_entries(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    sha256 = hashlib.sha256
    calls = 0

    def counted_sha256(*args, **kwargs):
        nonlocal calls
        calls += 1
        return sha256(*args, **kwargs)

    monkeypatch.setattr(hashlib, "sha256", counted_sha256)
    assert client.get("/fonts/Face.ttf").status_code == 200
    assert client.get("/fonts/Face.ttf?v=stale").status_code == 200
    assert calls == 1
    for index in range(128):
        (tmp_path / f"Face-{index}.ttf").write_bytes(b"font")
        assert client.get(f"/fonts/Face-{index}.ttf").status_code == 200
    assert calls == 129
    assert client.get("/fonts/Face.ttf").status_code == 200
    assert calls == 130, "the old cached file must be evicted after 128 newer entries"


def test_font_misses_are_not_cached(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    missing = client.get("/fonts/Nope.ttf")
    assert missing.status_code == 404
    assert "immutable" not in missing.headers.get("cache-control", "")


def test_real_app_fonts_mount_is_the_font_server():
    mounts = {r.path: r.app for r in app_module.app.routes if isinstance(r, Mount)}
    assert isinstance(mounts["/fonts"], app_module.FontStaticFiles)
    assert isinstance(mounts["/fonts"], app_module.SafeStaticFiles), "keeps the NUL-byte 404"
