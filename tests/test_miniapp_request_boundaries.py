"""Request lifecycle, media URL identity and event-loop availability."""

import asyncio
import threading
from urllib.parse import unquote, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from routers import miniapp
from services import content_requests as cr
from services.poster_content import _static_url_for


@pytest.mark.parametrize("path", ["provider/a#1?.mp4", "provider/a%20b.mp4",
                                   "provider/a b.mp4", "clips/job/clip_é.mp4"])
def test_media_url_retains_exact_filename(path):
    url = urlsplit(_static_url_for("demo", path))
    assert url.query == url.fragment == ""
    prefix = "/projects/demo/" if path.startswith("clips/") else "/projects/demo/videos/"
    assert unquote(url.path) == prefix + path


@pytest.mark.parametrize("quantity", [0, -1, True, False, 1.5, "2"])
def test_quantity_refused_before_service_write(tmp_path, monkeypatch, quantity):
    path = tmp_path / "requests.json"
    monkeypatch.setattr(cr, "REQUESTS_PATH", path)
    with pytest.raises(ValueError):
        cr.add_request("p", "clips", quantity=quantity)
    assert not path.exists()


@pytest.mark.parametrize("quantity", [None, 1, 15])
def test_optional_positive_quantity_remains_supported(tmp_path, monkeypatch, quantity):
    monkeypatch.setattr(cr, "REQUESTS_PATH", tmp_path / "requests.json")
    assert cr.add_request("p", "clips", quantity=quantity)["quantity"] == quantity


def test_fulfillment_retries_and_reopening(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "REQUESTS_PATH", tmp_path / "requests.json")
    monkeypatch.setattr(cr, "_now", lambda: "A")
    rid = cr.add_request("p", "clips")["id"]
    assert cr.update_request(rid, status="fulfilled")["fulfilled_at"] == "A"
    monkeypatch.setattr(cr, "_now", lambda: "B")
    assert cr.update_request(rid, status="fulfilled")["fulfilled_at"] == "A"
    assert cr.update_request(rid, status="open")["fulfilled_at"] is None
    assert cr.update_request(rid, status="fulfilled")["fulfilled_at"] == "B"


@pytest.fixture
def api(monkeypatch):
    app = FastAPI()
    app.include_router(miniapp.router, prefix="/api/miniapp")
    monkeypatch.setattr(miniapp, "_require_poster", lambda request: {"poster_id": "p"})
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", [0, -1, True, 1.5])
async def test_quantity_http_refusal(api, monkeypatch, quantity):
    monkeypatch.setattr(cr, "add_request", lambda **kw: pytest.fail("invalid request persisted"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                base_url="http://test") as client:
        result = await client.post("/api/miniapp/requests", json={"text": "clips", "quantity": quantity})
    assert result.status_code == 422


@pytest.mark.asyncio
async def test_slow_scan_keeps_event_loop_available(api, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def scan(*args, **kwargs):
        entered.set()
        if not release.wait(2):
            raise RuntimeError("event loop did not release the scan")
        return {"pages": [], "videos": []}

    monkeypatch.setattr(miniapp, "videos_for_poster", scan)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                base_url="http://test") as client:
        pending = asyncio.create_task(client.get("/api/miniapp/videos"))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            release.set()
            assert (await pending).status_code == 200
        finally:
            release.set()
            if not pending.done():
                await pending
