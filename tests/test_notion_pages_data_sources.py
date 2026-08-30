"""Tests for the multi-data-source Master Pages fetch.

Notion migrated databases to a multi-data-source model and the pinned
2022-06-28 API version refuses to query any database that has more than one
("Databases with multiple data sources are not supported in this API
version" — observed in production 2026-08-18, roster cache stuck 23 days
stale). The fetch now resolves the database's data sources and queries each
one under 2025-09-03. httpx is stubbed; Notion is never hit.
"""

import httpx
import pytest

import services.notion_pages as np
import services.roster as roster
from services import json_store


def _notion_page(username, group=None):
    props = {"Account Username": {"title": [{"plain_text": username}]}}
    if group:
        props["Group"] = {"select": {"name": group}}
    return {"id": f"page-{username}", "properties": props}


class FakeAsyncClient:
    """Answers the two calls the fetch makes, asserting versions as it goes."""

    def __init__(self, data_sources, pages_by_source):
        self._data_sources = data_sources
        self._pages = pages_by_source
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self.calls.append(("GET", url, headers.get("Notion-Version")))
        assert headers["Notion-Version"] == np.NOTION_VERSION_DATA_SOURCES
        return httpx.Response(200, json={"data_sources": self._data_sources},
                              request=httpx.Request("GET", url))

    async def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, headers.get("Notion-Version"), dict(json)))
        assert headers["Notion-Version"] == np.NOTION_VERSION_DATA_SOURCES
        source_id = url.rsplit("/data_sources/", 1)[1].split("/")[0]
        pages = self._pages[source_id]
        start = 0
        cursor = (json or {}).get("start_cursor")
        if cursor:
            start = int(cursor.split("-", 1)[1])
        batch = pages[start:start + 2]  # tiny page size to force pagination
        has_more = start + 2 < len(pages)
        return httpx.Response(200, json={
            "results": batch,
            "has_more": has_more,
            "next_cursor": f"cursor-{start + 2}" if has_more else None,
        }, request=httpx.Request("POST", url))


@pytest.mark.asyncio
async def test_every_data_source_is_queried_and_paginated(monkeypatch):
    monkeypatch.setattr(np, "NOTION_API_KEY", "key")
    monkeypatch.setattr(np, "NOTION_PAGES_DB", "db")
    fake = FakeAsyncClient(
        data_sources=[{"id": "ds-1"}, {"id": "ds-2"}],
        pages_by_source={
            "ds-1": [_notion_page("a.page", "WARNER"), _notion_page("b.page"), _notion_page("c.page")],
            "ds-2": [_notion_page("d.page", "ATLANTIC")],
        },
    )
    monkeypatch.setattr(np.httpx, "AsyncClient", lambda *a, **k: fake)

    rows = await np.fetch_all_pages()

    assert [r["name"] for r in rows] == ["a.page", "b.page", "c.page", "d.page"]
    assert rows[0]["group"] == "WARNER"
    assert rows[3]["group"] == "ATLANTIC"
    # Both data sources queried; the three-row source paginated past one
    # cursor; no call went out on the pinned legacy version.
    posts = [c for c in fake.calls if c[0] == "POST"]
    assert sum(1 for c in posts if "ds-1" in c[1]) == 2
    assert sum(1 for c in posts if "ds-2" in c[1]) == 1
    assert all(c[2] == np.NOTION_VERSION_DATA_SOURCES for c in fake.calls)


@pytest.mark.asyncio
async def test_single_source_database_behaves_like_the_old_query(monkeypatch):
    monkeypatch.setattr(np, "NOTION_API_KEY", "key")
    monkeypatch.setattr(np, "NOTION_PAGES_DB", "db")
    fake = FakeAsyncClient(
        data_sources=[{"id": "ds-only"}],
        pages_by_source={"ds-only": [_notion_page("only.page")]},
    )
    monkeypatch.setattr(np.httpx, "AsyncClient", lambda *a, **k: fake)

    rows = await np.fetch_all_pages()
    assert [r["name"] for r in rows] == ["only.page"]


def _parsed_row(name, page_id, *, archived=False):
    return {
        "integration_id": np.mint_integration_id(name),
        "name": name,
        "provider": "tiktok",
        "tiktok_url": f"https://www.tiktok.com/@{name}",
        "signup_email": "",
        "fwd_address": "",
        "password": "",
        "poster_name": "PIXEL-1",
        "group": "INTERNAL",
        "group_label": "Internal",
        "account_type": "theme",
        "notes": "",
        "notion_page_id": page_id,
        "source": "notion",
        "status": "inactive" if archived else "In Production",
        "account_status": "inactive" if archived else "In Production",
        "pipeline": "",
        "page_type": "",
        "content_niche": "TRUCK",
        "content_engine": "ai_video",
        "automation_mode": "Automation",
        "vault_url": "https://example.com/vault",
        "archived": archived,
        "sounds_reference": "",
        "go_live_date": "",
        "drive_folder_url": "",
    }


@pytest.mark.asyncio
async def test_sync_prunes_stale_notion_rows_but_preserves_legacy_operator_rows(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(roster, "ROSTER_PATH", tmp_path / "page_roster.json")
    monkeypatch.setattr(json_store, "_LOCKS", {})
    roster.save_roster({
        "version": 1,
        "pages": {
            "acct:old-name": {
                "integration_id": "acct:old-name", "name": "old.name",
                "source": "notion", "notion_page_id": "page-renamed",
            },
            "postiz:operator": {
                "integration_id": "postiz:operator", "name": "Operator row",
                "source": None,
            },
        },
    })
    active = _parsed_row("same.name", "page-active")
    archived_duplicate = _parsed_row(
        "same.name", "page-archived", archived=True,
    )
    renamed = _parsed_row("new.name", "page-renamed")

    async def fetch():
        return [archived_duplicate, active, renamed]

    monkeypatch.setattr(np, "fetch_all_pages", fetch)
    result = await np.sync_into_roster()
    pages = roster.load_roster()["pages"]
    assert result["errors"] == []
    assert result["total_in_notion"] == 3
    assert "acct:old-name" not in pages
    assert pages["acct:new-name"]["notion_page_id"] == "page-renamed"
    assert pages["acct:same-name"]["notion_page_id"] == "page-active"
    assert pages["postiz:operator"]["name"] == "Operator row"


@pytest.mark.asyncio
async def test_sync_never_chooses_between_two_active_master_pages_rows(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(roster, "ROSTER_PATH", tmp_path / "page_roster.json")
    monkeypatch.setattr(json_store, "_LOCKS", {})
    roster.save_roster({
        "version": 1,
        "pages": {
            "acct:duplicate": {
                "integration_id": "acct:duplicate", "name": "duplicate",
                "source": "notion", "notion_page_id": "prior-page",
            },
        },
    })

    async def fetch():
        return [
            _parsed_row("duplicate", "page-one"),
            _parsed_row("duplicate", "page-two"),
        ]

    monkeypatch.setattr(np, "fetch_all_pages", fetch)
    result = await np.sync_into_roster()
    assert result["errors"] == [
        "duplicate: duplicate active Master Pages identity",
    ]
    assert roster.load_roster()["pages"]["acct:duplicate"]["notion_page_id"] == "prior-page"
