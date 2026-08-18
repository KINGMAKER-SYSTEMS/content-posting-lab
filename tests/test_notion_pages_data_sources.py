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
