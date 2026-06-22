"""API tests for routers/roster.py — the previously untested dedup / sync surface.

Covers the four state-mutating / state-polling endpoints that have no other
coverage and that silently corrupt the posting schedule if they break:

  * GET  /api/roster/duplicates      -> find_duplicates  (collision detection)
  * POST /api/roster/dedup           -> dedup_roster     (data mutation + merge)
  * GET  /api/roster/sync-notion/status -> notion_sync_status (config polling)
  * POST /api/roster/sync-notion     -> sync_from_notion  (bulk import / gating)

All persistent state (page_roster.json, telegram_config.json) is redirected to a
throwaway tmp dir so the real Railway-volume files are never touched. Notion is
never hit — sync_into_roster is stubbed.
"""

import pytest

import services.roster as roster
import services.telegram as tg
from services import json_store
from routers import roster as roster_router


@pytest.fixture(autouse=True)
def isolate_service_files(monkeypatch, tmp_path):
    """Point the roster + telegram data files at a fresh tmp dir, fresh locks."""
    monkeypatch.setattr(roster, "ROSTER_PATH", tmp_path / "page_roster.json")
    monkeypatch.setattr(tg, "CONFIG_PATH", tmp_path / "telegram_config.json")
    monkeypatch.setattr(json_store, "_LOCKS", {})
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    yield


def _add_page(ig_id, name, **extra):
    roster.set_page(ig_id, {"name": name, "provider": "tiktok", **extra})


# ── _normalize_name (the heart of collision detection) ──────────────────────


def test_normalize_name_strips_emoji_case_and_whitespace():
    n = roster_router._normalize_name
    assert n("My Page 🎵") == n("my   page")
    assert n("  Foo  Bar ") == "foo bar"
    assert n("🔥Hot🔥") == "hot"


# ── find_duplicates ─────────────────────────────────────────────────────────


def test_find_duplicates_none_when_unique(sync_client):
    _add_page("a", "Alpha")
    _add_page("b", "Beta")
    r = sync_client.get("/api/roster/duplicates")
    assert r.status_code == 200
    body = r.json()
    assert body["total_pages"] == 2
    assert body["duplicate_names"] == 0
    assert body["duplicates"] == []


def test_find_duplicates_detects_emoji_and_case_collisions(sync_client):
    _add_page("id1", "Cool Page", project="proj1")
    _add_page("id2", "cool page 🎶")  # same normalized name
    _add_page("id3", "Unique")

    r = sync_client.get("/api/roster/duplicates")
    assert r.status_code == 200
    body = r.json()
    assert body["duplicate_names"] == 1
    dupe = body["duplicates"][0]
    assert dupe["name"] == "cool page"
    assert dupe["count"] == 2
    ids = {e["integration_id"] for e in dupe["entries"]}
    assert ids == {"id1", "id2"}
    # project flag surfaced per-entry
    by_id = {e["integration_id"]: e for e in dupe["entries"]}
    assert by_id["id1"]["has_project"] is True
    assert by_id["id2"]["has_project"] is False


def test_find_duplicates_surfaces_inventory_counts(sync_client):
    _add_page("keep", "Dup")
    _add_page("drop", "dup")
    cfg = tg.load_config()
    cfg["inventory"]["keep"] = [
        {"forwarded": True},
        {"forwarded": False},
        {"forwarded": False},
    ]
    tg.save_config(cfg)

    r = sync_client.get("/api/roster/duplicates")
    entries = {e["integration_id"]: e for e in r.json()["duplicates"][0]["entries"]}
    assert entries["keep"]["inventory_total"] == 3
    assert entries["keep"]["inventory_pending"] == 2
    assert entries["keep"]["inventory_forwarded"] == 1
    assert entries["drop"]["inventory_total"] == 0


# ── dedup_roster ────────────────────────────────────────────────────────────


def test_dedup_keeps_highest_inventory_and_merges(sync_client):
    # Scoring: inventory items weigh 100 each, project only 10. So the entry
    # with 2 inventory items (200) beats the 1-item-plus-project entry (110).
    _add_page("one_item", "Page", project="p")  # score 100 + 10 = 110
    _add_page("two_items", "page")              # score 200 -> survivor

    cfg = tg.load_config()
    cfg["inventory"]["one_item"] = [{"forwarded": True}]
    cfg["inventory"]["two_items"] = [{"forwarded": False}, {"forwarded": False}]
    tg.save_config(cfg)

    r = sync_client.post("/api/roster/dedup")
    assert r.status_code == 200
    body = r.json()
    assert body["removed"] == 1
    assert body["remaining"] == 1
    # only the *removed* entry's inventory is counted as merged
    assert body["inventory_merged"] == 1

    # the higher-inventory entry survives, the other is gone
    pages = {p["integration_id"] for p in roster.list_all_pages()}
    assert pages == {"two_items"}

    # merged inventory persisted to disk under the survivor, dupe key removed
    inv = tg.load_config()["inventory"]
    assert len(inv["two_items"]) == 3
    assert "one_item" not in inv


def test_dedup_noop_when_no_duplicates(sync_client):
    _add_page("a", "Alpha")
    _add_page("b", "Beta")
    r = sync_client.post("/api/roster/dedup")
    body = r.json()
    assert body["removed"] == 0
    assert body["remaining"] == 2
    assert body["inventory_merged"] == 0


def test_dedup_score_prefers_topic_over_plain(sync_client):
    """With no inventory, the entry that owns a staging topic must win."""
    _add_page("plain", "Page")
    _add_page("with_topic", "page")

    cfg = tg.load_config()
    cfg["staging_group"]["topics"]["with_topic"] = {
        "topic_id": 42,
        "topic_name": "page",
    }
    tg.save_config(cfg)

    r = sync_client.post("/api/roster/dedup")
    assert r.json()["removed"] == 1
    survivors = {p["integration_id"] for p in roster.list_all_pages()}
    assert survivors == {"with_topic"}


# ── notion_sync_status ──────────────────────────────────────────────────────


def test_sync_status_reflects_is_configured(sync_client, monkeypatch):
    import services.notion_pages as np

    monkeypatch.setattr(np, "is_configured", lambda: True)
    assert sync_client.get("/api/roster/sync-notion/status").json() == {
        "configured": True
    }

    monkeypatch.setattr(np, "is_configured", lambda: False)
    assert sync_client.get("/api/roster/sync-notion/status").json() == {
        "configured": False
    }


# ── sync_from_notion ────────────────────────────────────────────────────────


def test_sync_from_notion_503_when_unconfigured(sync_client, monkeypatch):
    import services.notion_pages as np

    monkeypatch.setattr(np, "is_configured", lambda: False)
    r = sync_client.post("/api/roster/sync-notion")
    assert r.status_code == 503
    assert "Notion not configured" in r.json()["detail"]


def test_sync_from_notion_passes_through_result(sync_client, monkeypatch):
    import services.notion_pages as np

    async def fake_sync():
        return {"added": 3, "updated": 1, "total_in_notion": 4, "pages": []}

    monkeypatch.setattr(np, "is_configured", lambda: True)
    monkeypatch.setattr(np, "sync_into_roster", fake_sync)

    r = sync_client.post("/api/roster/sync-notion")
    assert r.status_code == 200
    assert r.json()["added"] == 3
    assert r.json()["updated"] == 1


def test_sync_from_notion_wraps_errors_as_500(sync_client, monkeypatch):
    import services.notion_pages as np

    async def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(np, "is_configured", lambda: True)
    monkeypatch.setattr(np, "sync_into_roster", boom)

    r = sync_client.post("/api/roster/sync-notion")
    assert r.status_code == 500
    assert "kaboom" in r.json()["detail"]
