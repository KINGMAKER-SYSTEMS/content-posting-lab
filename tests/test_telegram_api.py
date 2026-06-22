"""API tests for routers/telegram.py — the largest previously-untested router (55 endpoints).

Covers the data-critical, regression-prone surface called out by the ticket:

  * GET    /api/telegram/status                      -> aggregate status
  * PUT    /api/telegram/bot-token                   -> set token + (stubbed) bot start
  * DELETE /api/telegram/bot-token                   -> stop bot + clear token
  * GET/POST/PUT/DELETE /api/telegram/posters[/{id}] -> poster CRUD
  * POST   /api/telegram/posters/{id}/pages          -> page assignment
  * DELETE /api/telegram/posters/{id}/pages/{ig}     -> page unassignment
  * POST   /api/telegram/posters/reset-defaults      -> re-seed defaults (no clobber)
  * POST   /api/telegram/staging-group/scan-inventory (+ GET poll)
  * DELETE /api/telegram/inventory/scan

All persistent state (telegram_config.json) is redirected to a throwaway tmp dir
so the real Railway-volume file is never touched. The aiogram bot is never
started — telegram_bot.* primitives are stubbed so no network call ever fires
and the "no destructive bot delete+repost / no sends on startup" gates hold.
"""

import pytest

import telegram_bot as tg_bot
from routers import telegram as tg_router
from services import json_store
from services import telegram as tg


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch, tmp_path):
    """Point services.telegram at a throwaway config + fresh locks, and ensure
    no real bot token leaks in from the environment.

    We write an EMPTY config up front so load_config() does NOT auto-seed the
    default staging group + default posters (which would make poster-count /
    slug-collision / staging-not-configured assertions non-deterministic). Tests
    that need posters create them explicitly; the reset-defaults test relies on
    starting from this empty baseline.
    """
    cfg_path = tmp_path / "telegram_config.json"
    monkeypatch.setattr(tg, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(json_store, "_LOCKS", {})
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("MINIAPP_AGENT_KEY", raising=False)
    # Reset the module-level background-scan job between tests.
    monkeypatch.setattr(tg_router, "_scan_job", None, raising=False)
    # Empty baseline with NO staging group and NO posters.
    empty = tg._empty_config()
    empty["staging_group"] = {}
    tg.save_config(empty)
    yield


@pytest.fixture
def fake_bot(monkeypatch):
    """Pretend the bot is running, without touching the network.

    Returns a marker object so _require_bot() passes. start/stop/validate are
    stubbed; validate_group returns an admin forum so poster/staging creation
    succeeds without aiogram.
    """
    sentinel = object()
    monkeypatch.setattr(tg_bot, "get_bot", lambda: sentinel)

    async def _validate_group(chat_id):
        return {"is_admin": True, "is_forum": True, "title": f"Group {chat_id}"}

    monkeypatch.setattr(tg_bot, "validate_group", _validate_group)
    return sentinel


# ── status ──────────────────────────────────────────────────────────────────


def test_status_reports_unconfigured_bot(sync_client):
    r = sync_client.get("/api/telegram/status")
    assert r.status_code == 200
    body = r.json()
    assert body["bot_configured"] is False
    assert body["bot_running"] is False
    assert body["poster_count"] == 0
    assert body["staging_group"] is None


# ── bot-token ─────────────────────────────────────────────────────────────────


def test_set_bot_token_starts_bot_and_persists(sync_client, monkeypatch):
    started = {}

    async def _start_bot(token):
        started["token"] = token
        # Mimic the bot writing its resolved username back to config.
        with tg.mutate_config() as cfg:
            cfg["bot_username"] = "test_bot"

    monkeypatch.setattr(tg_bot, "get_bot", lambda: None)
    monkeypatch.setattr(tg_bot, "start_bot", _start_bot)

    r = sync_client.put("/api/telegram/bot-token", json={"token": "123:ABC"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "bot_username": "test_bot"}
    assert started["token"] == "123:ABC"
    assert tg.get_bot_token() == "123:ABC"


def test_set_bot_token_clears_token_when_start_fails(sync_client, monkeypatch):
    """A failed bot start must NOT leave a dangling token in config (rollback)."""

    async def _start_bot(token):
        raise RuntimeError("invalid token")

    monkeypatch.setattr(tg_bot, "get_bot", lambda: None)
    monkeypatch.setattr(tg_bot, "start_bot", _start_bot)

    r = sync_client.put("/api/telegram/bot-token", json={"token": "bad"})
    assert r.status_code == 400
    assert "Failed to start bot" in r.json()["detail"]
    assert tg.get_bot_token() is None


def test_delete_bot_token_clears_config(sync_client, monkeypatch):
    tg.set_bot_token("123:ABC")

    stopped = {"called": False}

    async def _stop_bot():
        stopped["called"] = True

    monkeypatch.setattr(tg_bot, "get_bot", lambda: object())
    monkeypatch.setattr(tg_bot, "stop_bot", _stop_bot)

    r = sync_client.delete("/api/telegram/bot-token")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert stopped["called"] is True
    assert tg.get_bot_token() is None


# ── poster CRUD ───────────────────────────────────────────────────────────────


def test_create_poster_requires_running_bot(sync_client):
    # No fake_bot fixture → _require_bot raises 503.
    r = sync_client.post("/api/telegram/posters", json={"name": "Seffra", "chat_id": -100})
    assert r.status_code == 503


def test_create_poster_slugifies_and_persists(sync_client, fake_bot):
    r = sync_client.post("/api/telegram/posters", json={"name": "John Balik", "chat_id": -100})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["poster_id"] == "john-balik"
    assert tg.get_poster("john-balik") is not None

    listed = sync_client.get("/api/telegram/posters")
    assert any(p["name"] == "John Balik" for p in listed.json())


def test_create_poster_avoids_slug_collision(sync_client, fake_bot):
    r1 = sync_client.post("/api/telegram/posters", json={"name": "Gigi", "chat_id": -1})
    r2 = sync_client.post("/api/telegram/posters", json={"name": "Gigi", "chat_id": -2})
    assert r1.json()["poster_id"] == "gigi"
    assert r2.json()["poster_id"] == "gigi-2"


def test_update_poster_404_when_missing(sync_client):
    r = sync_client.put("/api/telegram/posters/nope", json={"name": "X"})
    assert r.status_code == 404


def test_update_poster_changes_name(sync_client, fake_bot):
    tg.set_poster("p1", {"name": "Old", "chat_id": -100})
    r = sync_client.put("/api/telegram/posters/p1", json={"name": "New"})
    assert r.status_code == 200
    assert tg.get_poster("p1")["name"] == "New"
    # chat_id unchanged when omitted (no bot revalidation needed)
    assert tg.get_poster("p1")["chat_id"] == -100


def test_delete_poster_404_when_missing(sync_client):
    r = sync_client.delete("/api/telegram/posters/ghost")
    assert r.status_code == 404


def test_delete_poster_removes_it(sync_client):
    tg.set_poster("p1", {"name": "Doomed", "chat_id": -100})
    r = sync_client.delete("/api/telegram/posters/p1")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert tg.get_poster("p1") is None


def test_reset_defaults_seeds_without_clobbering_custom(sync_client):
    tg.set_poster("custom", {"name": "Custom Person", "chat_id": -999})
    r = sync_client.post("/api/telegram/posters/reset-defaults")
    assert r.status_code == 200
    body = r.json()
    assert body["added"] >= 1
    # Custom poster survives the re-seed and is counted in the total.
    assert tg.get_poster("custom") is not None
    assert body["total"] == len(tg.list_posters())

    # Re-running adds nothing new (idempotent / append-only).
    again = sync_client.post("/api/telegram/posters/reset-defaults")
    assert again.json()["added"] == 0


# ── page assignment ───────────────────────────────────────────────────────────


def test_assign_pages_404_when_poster_missing(sync_client):
    r = sync_client.post("/api/telegram/posters/nope/pages", json={"page_ids": ["a"]})
    assert r.status_code == 404


def test_assign_pages_persists_ids(sync_client):
    tg.set_poster("p1", {"name": "P", "chat_id": -100})
    r = sync_client.post(
        "/api/telegram/posters/p1/pages", json={"page_ids": ["ig-a", "ig-b"]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assigned"] == 2
    assert set(body["poster_page_ids"]) == {"ig-a", "ig-b"}
    assert set(tg.get_poster("p1")["page_ids"]) == {"ig-a", "ig-b"}


def test_unassign_page_404_when_poster_missing(sync_client):
    r = sync_client.delete("/api/telegram/posters/nope/pages/ig-a")
    assert r.status_code == 404


def test_unassign_page_removes_id_without_bot(sync_client, monkeypatch):
    """No bot running → no topic delete is attempted (no destructive bot ops),
    but the assignment is still removed from config."""
    monkeypatch.setattr(tg_bot, "get_bot", lambda: None)
    tg.set_poster("p1", {"name": "P", "chat_id": -100})
    tg.assign_pages_to_poster("p1", ["ig-a", "ig-b"])

    r = sync_client.delete("/api/telegram/posters/p1/pages/ig-a")
    assert r.status_code == 200
    assert r.json()["topic_deleted"] is False
    assert tg.get_poster("p1")["page_ids"] == ["ig-b"]


# ── inventory scan job ────────────────────────────────────────────────────────


def test_scan_inventory_requires_bot(sync_client):
    r = sync_client.post("/api/telegram/staging-group/scan-inventory")
    assert r.status_code == 503


def test_scan_inventory_400_without_staging_group(sync_client, fake_bot):
    r = sync_client.post("/api/telegram/staging-group/scan-inventory")
    assert r.status_code == 400
    assert "Staging group not configured" in r.json()["detail"]


def test_scan_status_idle_by_default(sync_client):
    r = sync_client.get("/api/telegram/staging-group/scan-inventory")
    assert r.status_code == 200
    assert r.json() == {"status": "idle"}


def test_delete_inventory_scan_returns_count(sync_client):
    r = sync_client.delete("/api/telegram/inventory/scan")
    assert r.status_code == 200
    # clear_scan_inventory returns an int count of removed items (0 when empty).
    assert r.json() == {"ok": True, "removed": 0}
