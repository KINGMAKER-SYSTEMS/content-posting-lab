"""Authority files must survive read failures and first-use races unchanged."""

import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from services import content_requests as cr, json_store, roster, telegram as tg


@pytest.mark.parametrize("module,field,mutate", [
    (cr, "REQUESTS_PATH", lambda: cr.add_request("p", "clips")),
    (roster, "ROSTER_PATH", lambda: roster.set_page("p", {"name": "Page"})),
    (tg, "CONFIG_PATH", lambda: tg.set_bot_token("test-token")),
])
@pytest.mark.parametrize("contents", [b'{"unfinished":', b'null', b'[]', b'{}', b'\xff'])
def test_invalid_existing_store_is_never_replaced(tmp_path, monkeypatch, module, field, mutate, contents):
    path = tmp_path / "state.json"
    path.write_bytes(contents)
    monkeypatch.setattr(module, field, path)
    with pytest.raises(json_store.JsonStoreUnavailable):
        mutate()
    assert path.read_bytes() == contents


@pytest.mark.parametrize("module,field,contents,read", [
    (cr, "REQUESTS_PATH", '{"requests": [null]}', cr.load_requests),
    (roster, "ROSTER_PATH", '{"pages": {"p": null}}', roster.load_roster),
    (tg, "CONFIG_PATH", '{"version":1,"posters":[],"sounds":[],"inventory":{},"staging_group":{}}', tg.load_config),
])
def test_malformed_collection_refuses(tmp_path, monkeypatch, module, field, contents, read):
    path = tmp_path / "state.json"
    path.write_text(contents)
    monkeypatch.setattr(module, field, path)
    with pytest.raises(json_store.JsonStoreUnavailable):
        read()
    assert path.read_text() == contents


def test_unreadable_existing_store_refuses(tmp_path, monkeypatch):
    path = tmp_path / "requests.json"
    path.write_text('{"requests": []}')
    monkeypatch.setattr(cr, "REQUESTS_PATH", path)
    real_read = type(path).read_text

    def denied(p, *args, **kwargs):
        if p == path:
            raise PermissionError("test refusal")
        return real_read(p, *args, **kwargs)

    monkeypatch.setattr(type(path), "read_text", denied)
    with pytest.raises(json_store.JsonStoreUnavailable):
        cr.add_request("p", "clips")
    assert path.read_bytes() == b'{"requests": []}'


def test_missing_first_use_stores_remain_supported(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "REQUESTS_PATH", tmp_path / "requests.json")
    monkeypatch.setattr(roster, "ROSTER_PATH", tmp_path / "roster.json")
    monkeypatch.setattr(tg, "CONFIG_PATH", tmp_path / "telegram.json")
    assert cr.add_request("p", "clips")["status"] == "open"
    assert roster.set_page("p", {"name": "Page"})["name"] == "Page"
    tg.set_bot_token("test-token")
    assert tg.get_bot_token() == "test-token"


def test_first_reader_seed_holds_transaction_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "CONFIG_PATH", tmp_path / "telegram.json")
    entered = threading.Event()
    release = threading.Event()
    real_seed = tg._seed_config

    def seed():
        entered.set()
        assert release.wait(2)
        return real_seed()

    monkeypatch.setattr(tg, "_seed_config", seed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(tg.load_config)
        try:
            assert entered.wait(1)
            # A reader-side seed must own the same lock a setter needs.
            lock = json_store.lock_for(tg.CONFIG_PATH)
            acquired = lock.acquire(blocking=False)
            if acquired:
                lock.release()
            assert not acquired
            writer = pool.submit(tg.set_bot_token, "test-token")
        finally:
            release.set()
        reader.result(timeout=2)
        writer.result(timeout=2)
    assert tg.get_bot_token() == "test-token"


@pytest.mark.asyncio
async def test_corrupt_request_history_http_503(tmp_path, monkeypatch):
    from app import app
    from routers import miniapp

    path = tmp_path / "requests.json"
    path.write_bytes(b'{"unfinished":')
    monkeypatch.setattr(cr, "REQUESTS_PATH", path)
    monkeypatch.setattr(miniapp, "_require_poster", lambda request: {"poster_id": "p"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="http://test") as client:
        response = await client.post("/api/miniapp/requests", json={"text": "clips"})
    assert response.status_code == 503
    assert path.read_bytes() == b'{"unfinished":'
