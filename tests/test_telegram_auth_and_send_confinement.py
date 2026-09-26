"""Security regressions for the Telegram router.

1. /api/telegram/* was listed in app._AUTH_SKIP, so with APP_API_KEY set every
   Telegram route (bot token replace/delete, staging-group repoint, posters,
   /send, /send-batch, /assign-batch ...) still answered anonymous callers.
   The bot uses long polling (telegram_bot.start_bot -> start_polling); there is
   no webhook route Telegram itself calls, so nothing under /api/telegram/ may
   stay exempt. /api/miniapp/* keeps its own initData HMAC auth.
2. POST /send accepted any file under the repository root. The Railway volume
   is mounted AT /app/projects, so that included page_roster.json, cookies.txt,
   telegram_config.json, control_plane_jobs.json and the repo's own files.
   Only real media files inside project media dirs (or the legacy output dirs)
   may be sent.
3. /send-batch and /assign-batch joined an unchecked batch_id onto the burn dir.

The bot is never started and no network call fires: send_media_to_topic is
stubbed and records what it would have sent.
"""

import os
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

import app as app_module
import telegram_bot as tg_bot
from routers import telegram as tg_router
from services import json_store
from services import telegram as tg

TEST_API_KEY = "test-api-key-not-a-secret"


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch, tmp_path):
    monkeypatch.setattr(tg, "CONFIG_PATH", tmp_path / "telegram_config.json")
    monkeypatch.setattr(json_store, "_LOCKS", {})
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    empty = tg._empty_config()
    empty["staging_group"] = {}
    tg.save_config(empty)
    yield


@pytest.fixture
def sync_client():
    """No lifespan: the API-key middleware and these routes need none, and the
    bot must never start. Server exceptions surface as test failures."""
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setattr(app_module, "_APP_API_KEY", TEST_API_KEY)
    return TEST_API_KEY


# ── 1. auth ─────────────────────────────────────────────────────────────────


def _telegram_routes() -> list[tuple[str, str]]:
    out = []
    for route in tg_router.router.routes:
        if not isinstance(route, APIRoute):
            continue
        path = "/api/telegram" + route.path
        # Fill path params with a harmless literal.
        while "{" in path:
            start = path.index("{")
            end = path.index("}", start)
            path = path[:start] + "x1" + path[end + 1 :]
        for method in sorted(route.methods):
            out.append((method, path))
    return out


def test_route_inventory_is_complete():
    routes = _telegram_routes()
    # Guard against the parametrised test silently covering nothing.
    assert len(routes) >= 50
    paths = {p for _, p in routes}
    for must in (
        "/api/telegram/status",
        "/api/telegram/bot-token",
        "/api/telegram/staging-group",
        "/api/telegram/posters",
        "/api/telegram/send",
        "/api/telegram/send-batch",
        "/api/telegram/assign-batch",
    ):
        assert must in paths


@pytest.mark.parametrize("method,path", _telegram_routes())
def test_every_telegram_route_requires_api_key(sync_client, api_key, method, path):
    r = sync_client.request(method, path)
    assert r.status_code == 401, f"{method} {path} -> {r.status_code}"
    assert r.json() == {"detail": "Invalid or missing API key"}


@pytest.mark.parametrize("method,path", _telegram_routes())
def test_every_telegram_route_rejects_wrong_api_key(sync_client, api_key, method, path):
    r = sync_client.request(method, path, headers={"x-api-key": "wrong"})
    assert r.status_code == 401, f"{method} {path} -> {r.status_code}"


def test_status_with_api_key_header(sync_client, api_key):
    r = sync_client.get("/api/telegram/status", headers={"x-api-key": api_key})
    assert r.status_code == 200, r.text


def test_status_with_bearer_api_key(sync_client, api_key):
    r = sync_client.get("/api/telegram/status", headers={"Authorization": f"Bearer {api_key}"})
    assert r.status_code == 200, r.text


def test_unauthenticated_bot_token_replace_is_refused(sync_client, api_key, monkeypatch):
    started = {}

    async def _start_bot(token):
        started["token"] = token

    monkeypatch.setattr(tg_bot, "get_bot", lambda: None)
    monkeypatch.setattr(tg_bot, "start_bot", _start_bot)
    tg.set_bot_token("111:original")

    r = sync_client.put("/api/telegram/bot-token", json={"token": "999:attacker"})
    assert r.status_code == 401
    assert started == {}
    assert tg.get_bot_token() == "111:original"

    r = sync_client.delete("/api/telegram/bot-token")
    assert r.status_code == 401
    assert tg.get_bot_token() == "111:original"


def test_health_stays_exempt(sync_client, api_key):
    assert sync_client.get("/api/health").status_code == 200


def test_miniapp_stays_on_its_own_initdata_auth(sync_client, api_key, monkeypatch):
    monkeypatch.delenv("MINIAPP_DEV_AUTH", raising=False)
    r = sync_client.get("/api/miniapp/me")
    # Reaches the mini-app router (initData auth), not the API-key middleware.
    assert r.status_code == 401
    assert r.json()["detail"] != "Invalid or missing API key"


def test_other_api_routes_still_require_key(sync_client, api_key):
    assert sync_client.get("/api/roster/").status_code == 401


def test_no_telegram_webhook_route_exists():
    """If a webhook route is ever added it must verify
    X-Telegram-Bot-Api-Secret-Token and be exempted explicitly, not by prefix."""
    for route in tg_router.router.routes:
        assert "webhook" not in getattr(route, "path", "").lower()
    assert not any(s.startswith("/api/telegram") for s in app_module._AUTH_SKIP)


# ── 2. /send confinement ────────────────────────────────────────────────────


@pytest.fixture
def workspace(monkeypatch, isolated_projects_root):
    """Lay the tmp workspace out like the container: PROJECT_ROOT=/app and the
    volume mounted at /app/projects holding the runtime state files."""
    root = isolated_projects_root.parent
    monkeypatch.setattr(tg_router, "PROJECT_ROOT", root)
    projects = isolated_projects_root
    for name, body in {
        "page_roster.json": '{"pages": {"p": {"password": "x"}}}',
        "cookies.txt": "# cookies",
        "telegram_config.json": "{}",
        "control_plane_jobs.json": "{}",
    }.items():
        (projects / name).write_text(body, encoding="utf-8")
    (root / ".env").write_text("K=V\n", encoding="utf-8")
    (root / "app.py").write_text("# app\n", encoding="utf-8")
    burned = projects / "proj" / "burned" / "b1"
    burned.mkdir(parents=True)
    (burned / "burned_1.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    videos = projects / "proj" / "videos"
    videos.mkdir(parents=True)
    (videos / "clip.mp4").write_bytes(b"mp4")
    (videos / "notes.json").write_text("{}", encoding="utf-8")
    (videos / "evil.mp4").symlink_to(projects / "page_roster.json")
    cpg = projects / "control_plane_generated" / "videos"
    cpg.mkdir(parents=True)
    (cpg / "gen.mp4").write_bytes(b"mp4")
    out = root / "output"
    out.mkdir()
    (out / "legacy.mp4").write_bytes(b"mp4")
    return root


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(tg_bot, "get_bot", lambda: object())

    async def _send_media_to_topic(chat_id, topic_id, file_path, caption=None):
        calls.append({"chat_id": chat_id, "topic_id": topic_id, "file_path": file_path})
        return {"message_id": len(calls), "file_id": f"f{len(calls)}"}

    monkeypatch.setattr(tg_bot, "send_media_to_topic", _send_media_to_topic)
    with tg.mutate_config() as cfg:
        cfg["staging_group"] = {"chat_id": -100123, "name": "Staging", "topics": {}}
    tg.set_staging_topic("ig1", 7, "Page One")
    tg.set_staging_topic("ig2", 8, "Page Two")
    return calls


def _send(client, file_path: str):
    return client.post(
        "/api/telegram/send",
        json={"integration_id": "ig1", "file_path": file_path},
    )


def test_send_accepts_project_media(sync_client, workspace, sent):
    target = workspace / "projects" / "proj" / "burned" / "b1" / "burned_1.mp4"
    r = _send(sync_client, str(target))
    assert r.status_code == 200, r.text
    assert [Path(c["file_path"]) for c in sent] == [target.resolve()]


def test_send_accepts_legacy_output_media(sync_client, workspace, sent):
    r = _send(sync_client, str(workspace / "output" / "legacy.mp4"))
    assert r.status_code == 200, r.text
    assert len(sent) == 1


@pytest.mark.parametrize(
    "rel",
    [
        "projects/page_roster.json",
        "projects/cookies.txt",
        "projects/telegram_config.json",
        "projects/control_plane_jobs.json",
        ".env",
        "app.py",
        "projects/proj/videos/notes.json",
        "projects/proj/videos/evil.mp4",  # symlink -> page_roster.json
        "projects/proj/videos/../../page_roster.json",
        "projects/proj/burned/b1/../../../cookies.txt",
        "projects/control_plane_generated/videos/gen.mp4",
        "projects/proj/videos/clip.mp4\x00.json",
    ],
)
def test_send_refuses_non_media_paths(sync_client, workspace, sent, rel):
    r = _send(sync_client, str(workspace) + "/" + rel)
    assert r.status_code == 400, f"{rel} -> {r.status_code} {r.text}"
    assert sent == []


def test_send_refuses_relative_traversal(sync_client, workspace, sent, monkeypatch):
    monkeypatch.chdir(workspace / "projects" / "proj" / "videos")
    r = _send(sync_client, "../../page_roster.json")
    assert r.status_code == 400
    assert sent == []


def test_send_missing_media_is_404(sync_client, workspace, sent):
    r = _send(sync_client, str(workspace / "projects" / "proj" / "videos" / "nope.mp4"))
    assert r.status_code == 404
    assert sent == []


# ── 3. batch_id confinement ─────────────────────────────────────────────────


@pytest.fixture
def victim_batch(workspace):
    victim = workspace / "projects" / "victim" / "burned" / "vb"
    victim.mkdir(parents=True)
    (victim / "burned_9.mp4").write_bytes(b"mp4")
    return victim


@pytest.mark.parametrize("endpoint", ["/api/telegram/send-batch", "/api/telegram/assign-batch"])
@pytest.mark.parametrize("batch_id", ["../../victim/burned/vb", "..", "b1/../../../victim/burned/vb"])
def test_batch_id_traversal_refused(sync_client, workspace, sent, victim_batch, endpoint, batch_id):
    r = sync_client.post(
        endpoint,
        json={"integration_id": "ig1", "integration_ids": ["ig1"], "project": "proj", "batch_id": batch_id},
    )
    assert r.status_code == 400, r.text
    assert sent == []


@pytest.mark.parametrize("endpoint", ["/api/telegram/send-batch", "/api/telegram/assign-batch"])
def test_batch_skips_symlink_escaping_batch(sync_client, workspace, sent, endpoint):
    batch = workspace / "projects" / "proj" / "burned" / "b1"
    (batch / "burned_2.mp4").symlink_to(workspace / "projects" / "page_roster.json")
    r = sync_client.post(
        endpoint,
        json={"integration_id": "ig1", "integration_ids": ["ig1"], "project": "proj", "batch_id": "b1"},
    )
    assert r.status_code == 200, r.text
    assert [os.path.basename(c["file_path"]) for c in sent] == ["burned_1.mp4"]


@pytest.mark.parametrize("endpoint", ["/api/telegram/send-batch", "/api/telegram/assign-batch"])
def test_batch_happy_path(sync_client, workspace, sent, endpoint):
    r = sync_client.post(
        endpoint,
        json={"integration_id": "ig1", "integration_ids": ["ig1"], "project": "proj", "batch_id": "b1"},
    )
    assert r.status_code == 200, r.text
    assert len(sent) == 1
