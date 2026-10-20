"""Real-handler regressions for editor GET writes and operator poster binding."""

import json

import pytest
from fastapi.testclient import TestClient

import app as app_module
from routers import agenticnews
from services import route_auth, telegram
from tests import route_auth_support as s

pytestmark = pytest.mark.real_route_auth


@pytest.fixture
def client(monkeypatch):
    s.configure_all(monkeypatch)
    monkeypatch.setattr(app_module, "_APP_API_KEY", "")
    # No lifespan: these routes need neither the factory nor the Telegram bot.
    return TestClient(app_module.app)


@pytest.mark.parametrize("suffix", ["", "/openshot"], ids=["load", "openshot"])
@pytest.mark.parametrize("same_site", ["fetch-site", "origin"])
def test_editor_get_requires_same_site_before_import(client, monkeypatch, tmp_path, suffix, same_site):
    monkeypatch.setattr(agenticnews.db, "ASSETS_DIR", tmp_path)
    monkeypatch.setattr(agenticnews.editor_render, "detect_render_backends", lambda: {
        "openshot": {"available": False, "reason": "test"},
    })
    episode = "auth_episode"
    source = tmp_path / f"{episode}_timeline.json"
    source.write_text(json.dumps({
        "episodeId": episode,
        "title": "Auth regression",
        "segments": [{"segmentId": "s0", "durationSec": 2, "shots": [{
            "src": "/agenticnews-assets/frame.png", "startSec": 0,
            "durationSec": 2, "type": "artifact",
        }]}],
    }))
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    url = f"/api/agenticnews/editor-timelines/{episode}{suffix}"
    jwt = {"Cf-Access-Jwt-Assertion": s.mint()}
    for origin in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": "https://evil.invalid"}, {}):
        response = client.get(url, headers={**jwt, **origin})
        after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        assert (response.status_code, after) == (403, before), "unsafe GET must not save editor state"
    allowed = {"Sec-Fetch-Site": "same-origin"} if same_site == "fetch-site" else {"Origin": s.LAB_ORIGIN}
    response = client.get(url, headers={**jwt, **allowed})
    assert response.status_code == 200, response.text
    saved = agenticnews._editor_timeline_store().load(episode)
    assert saved["projectId"] == episode
    assert len(saved["clips"]) == 1
    assert source.read_bytes() == before[source.relative_to(tmp_path)]


@pytest.mark.parametrize("operation", ["bind", "unbind"])
def test_poster_binding_accepts_access_without_agent_key(client, monkeypatch, tmp_path, operation):
    monkeypatch.setenv("MINIAPP_AGENT_KEY", "dummy-server-agent-key")
    monkeypatch.setattr(telegram, "CONFIG_PATH", tmp_path / "telegram_config.json")
    telegram.save_config(telegram._empty_config())
    telegram.set_poster("p1", {"name": "Poster One", "telegram_user_ids": [42] if operation == "unbind" else []})
    before = telegram.CONFIG_PATH.read_bytes()
    method = "POST" if operation == "bind" else "DELETE"
    url = "/api/telegram/posters/p1/users" + ("/42" if operation == "unbind" else "")
    body = {"json": {"user_id": 42, "username": "test_operator"}} if operation == "bind" else {}
    for headers in ({}, {"X-Agent-Key": "dummy-server-agent-key"},
                    {**s.valid_headers(route_auth.ACCESS), "Sec-Fetch-Site": "cross-site"}):
        response = client.request(method, url, headers=headers, **body)
        assert response.status_code in (401, 403)
        assert telegram.CONFIG_PATH.read_bytes() == before
    response = client.request(method, url, headers=s.valid_headers(route_auth.ACCESS), **body)
    assert response.status_code == 200, "Access-only operator must be able to bind and unbind"
    expected = [42] if operation == "bind" else []
    assert response.json()["telegram_user_ids"] == expected
    assert telegram.get_poster("p1")["telegram_user_ids"] == expected
    if operation == "bind":
        assert telegram.get_poster("p1")["telegram_username"] == "test_operator"
