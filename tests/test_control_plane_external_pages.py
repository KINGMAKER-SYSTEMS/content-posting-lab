"""A page registered outside Notion resolves for jobs and stays out of the fleet.

These tests use the real roster store and the real
``_current_master_pages_intent`` resolver (no ``bind_current_intent`` stub), so
"registered" means the same roster row every source import, clip job and
capability read goes through. Notion is never hit: sync gets a stub fetch.
"""

from __future__ import annotations

import copy
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import routers.control_plane as cp
import routers.control_plane_external_pages as external
import services.notion_pages as notion_pages
import services.roster as roster
from services import json_store
from services.control_plane_source_imports import SOURCE_IMPORT_SCHEMA
from services.master_pages_contract import intent_hash


TOKEN = "test-control-plane-token"
LANE = "content-bucket-control-plane"
PAGE_ID = "ns-tt-6b3e9c1a-k7pq3m2x"
HANDLE = "night.walks.artist"
URL = f"/api/control-plane/v1/external-pages/{PAGE_ID}"
AUTH = {"Authorization": f"Bearer {TOKEN}", "X-RT-Lane": LANE}

NOTION_ID = "acct:truck-tok-daily"
NOTION_ROW = {
    "integration_id": NOTION_ID,
    "name": "truck.tok.daily",
    "provider": "tiktok",
    "group": "WARNER",
    "group_label": "Warner UGC",
    "page_type": "UGC theme page",
    "account_type": "ugc",
    "poster_name": "mon",
    "status": "Active",
    "tiktok_url": "https://www.tiktok.com/@truck.tok.daily",
    "notion_page_id": "2f4b1c40aaaabbbbccccddddeeeeffff",
    "source": "notion",
    "content_niche": "TRUCK",
    "content_engine": "ai_video",
    "automation_mode": "Automation",
    "vault_url": "https://shipstream.test/vault/truck.tok.daily",
    "archived": False,
}


def northstar_intent(**overrides):
    """The intent the control plane writes for a North Star page (PR 1165)."""
    intent = {
        "schema": "master-pages.page-intent.v1",
        "pageId": PAGE_ID,
        "handle": HANDLE,
        "group": "NORTH STAR",
        "groupLabel": None,
        "pageType": "Artist page",
        "accountType": "artist",
        "posterName": None,
        "status": "Active",
        "project": None,
        "tiktokUrl": None,
        "notionPageId": None,
        "source": "northstar",
        "contentNiche": "POV — Night Core",
        "contentEngine": "sourced_video",
        "automationMode": "Automation",
        "vaultUrl": f"https://shipstream.test/vault/{HANDLE}",
        "pipeline": None,
        "soundsReference": None,
        "archived": False,
    }
    intent.update(overrides)
    return intent


@pytest.fixture
def lab(monkeypatch, tmp_path):
    monkeypatch.setattr(roster, "ROSTER_PATH", tmp_path / "page_roster.json")
    monkeypatch.setattr(json_store, "_LOCKS", {})
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    monkeypatch.setenv("CONTENT_LAB_CONTROL_PLANE_ORIGIN", "https://control.example.com")
    monkeypatch.setenv("CONTENT_LAB_PUBLIC_ORIGIN", "https://content-lab.example.com")
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    monkeypatch.setattr(cp, "_generation_root", lambda: tmp_path / "generated")
    monkeypatch.setattr(cp, "validate_source_url", lambda value: value)
    started: list[str] = []
    monkeypatch.setattr(cp, "_start_page_source_import", started.append)
    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    app.include_router(external.router, prefix="/api/control-plane")
    return TestClient(app), started


def _seed(*rows):
    roster.save_roster({
        "version": 1,
        "pages": {row["integration_id"]: dict(row) for row in rows},
    })


def _roster_bytes():
    path = roster.ROSTER_PATH
    return path.read_bytes() if path.exists() else None


def _register(client, intent=None):
    return client.put(URL, headers=AUTH, json=intent or northstar_intent())


def _source_import(client, intent, key="source:intake:northstar:001"):
    return client.post(
        "/api/control-plane/v1/source-imports",
        headers={**AUTH, "X-RT-Page-Id": PAGE_ID, "Idempotency-Key": key},
        json={
            "schema": SOURCE_IMPORT_SCHEMA,
            "pageId": PAGE_ID,
            "format": "pov-night-core",
            "sourceUrl": "https://cdn.example.com/source/artist-footage.mp4",
            "masterPages": intent,
            "masterPagesHash": intent_hash(intent),
        },
    )


def _clip_job(client, intent, key="clips:northstar:0001"):
    return client.post(
        "/api/control-plane/v1/jobs",
        headers={**AUTH, "X-RT-Page-Id": PAGE_ID, "Idempotency-Key": key},
        json={
            "pageId": PAGE_ID,
            "lane": LANE,
            "engine": "sourced_video",
            "lockedRecipeId": "dossier:ns-tt-6b3e9c1a-k7pq3m2x",
            "recipeVersion": "sha256:" + "a" * 64,
            "policyHash": "sha256:" + "b" * 64,
            "quantity": 1,
            "masterPages": intent,
            "masterPagesHash": intent_hash(intent),
        },
    )


# ── Registration ────────────────────────────────────────────────────────────


def test_register_requires_control_plane_bearer_and_lane_before_any_write(lab, monkeypatch):
    client, _ = lab
    assert client.put(URL, headers={"X-RT-Lane": LANE}, json=northstar_intent()).status_code == 401
    assert client.put(
        URL, headers={"Authorization": "Bearer wrong", "X-RT-Lane": LANE}, json=northstar_intent(),
    ).status_code == 401
    assert client.put(
        URL, headers={"Authorization": f"Bearer {TOKEN}"}, json=northstar_intent(),
    ).status_code == 400
    assert client.put(
        URL, headers={**AUTH, "X-RT-Lane": "warner"}, json=northstar_intent(),
    ).status_code == 400
    assert client.delete(URL, headers={"X-RT-Lane": LANE}).status_code == 401
    monkeypatch.delenv("CONTROL_PLANE_TOKEN")
    assert _register(client).status_code == 503
    assert _roster_bytes() is None


def test_register_stores_a_non_notion_row_and_answers_the_resolved_intent(lab):
    client, _ = lab
    intent = northstar_intent()

    created = _register(client, intent)
    assert created.status_code == 200
    assert created.json() == {
        "schema": cp.RESPONSE_SCHEMA,
        "pageId": PAGE_ID,
        "status": "created",
        "masterPages": intent,
        "masterPagesHash": intent_hash(intent),
    }
    row = roster.get_page(PAGE_ID)
    assert row["source"] == "northstar"
    assert row["notion_page_id"] is None
    assert row["name"] == HANDLE
    assert cp._current_master_pages_intent(PAGE_ID) == (intent, intent_hash(intent))
    assert cp._current_master_pages_intent(PAGE_ID, intent) == (intent, intent_hash(intent))
    assert cp._current_intent_for_capabilities(PAGE_ID) == (intent, intent_hash(intent))

    before = _roster_bytes()
    repeat = _register(client, intent)
    assert repeat.status_code == 200
    assert repeat.json()["status"] == "unchanged"
    assert _roster_bytes() == before

    moved = northstar_intent(contentNiche="POV — Scenic")
    updated = _register(client, moved)
    assert updated.json()["status"] == "updated"
    assert updated.json()["masterPagesHash"] == intent_hash(moved)
    assert cp._current_master_pages_intent(PAGE_ID) == (moved, intent_hash(moved))


@pytest.mark.parametrize(
    ("intent", "detail"),
    [
        (northstar_intent(source="notion"), "source must name an external"),
        (northstar_intent(source=None), "source must name an external"),
        (northstar_intent(source="postiz"), "source must name an external"),
        (northstar_intent(notionPageId="2f4b1c40aaaabbbbccccddddeeeeffff"), "no Notion page id"),
        (northstar_intent(contentNiche=None), "missing contentNiche"),
        (northstar_intent(vaultUrl="  "), "missing vaultUrl"),
        (northstar_intent(handle="Night.Walks.Artist"), "send the handle in lowercase"),
        (northstar_intent(pageId="ns-tt-someone-else"), "complete master-pages.page-intent.v1"),
        ({k: v for k, v in northstar_intent().items() if k != "pipeline"}, "complete master-pages"),
        ({**northstar_intent(), "extra": "field"}, "complete master-pages"),
        (northstar_intent(schema="master-pages.page-intent.v2"), "complete master-pages"),
        (["not", "an", "object"], "intent object"),
    ],
)
def test_register_refuses_malformed_or_notion_intents_without_writing(lab, intent, detail):
    client, _ = lab
    response = _register(client, intent)
    assert response.status_code == 400
    assert detail in response.json()["detail"]
    assert _roster_bytes() is None


def test_register_refuses_the_notion_id_namespace(lab):
    client, _ = lab
    page_id = "acct:night-walks-artist"
    response = client.put(
        f"/api/control-plane/v1/external-pages/{page_id}",
        headers=AUTH,
        json=northstar_intent(pageId=page_id),
    )
    assert response.status_code == 400
    assert "reserved for Notion" in response.json()["detail"]
    assert _roster_bytes() is None


def test_register_refuses_an_id_or_handle_a_notion_row_holds(lab):
    client, _ = lab
    _seed(NOTION_ROW)
    before = _roster_bytes()

    # The Notion row's handle under a new external id, in any letter case.
    clash = client.put(URL, headers=AUTH, json=northstar_intent(
        handle="truck.tok.daily",
        vaultUrl="https://shipstream.test/vault/truck.tok.daily",
    ))
    assert clash.status_code == 409
    assert clash.json()["detail"] == "handle is held by a Notion Master Pages row"
    assert _roster_bytes() == before
    roster.set_page(NOTION_ID, {"name": "Truck.Tok.Daily"})
    before = _roster_bytes()
    assert client.put(URL, headers=AUTH, json=northstar_intent(
        handle="truck.tok.daily",
        vaultUrl="https://shipstream.test/vault/truck.tok.daily",
    )).status_code == 409
    assert _roster_bytes() == before

    # A Notion-shaped id outside the acct: namespace (a hand-made row).
    _seed({**NOTION_ROW, "integration_id": PAGE_ID, "name": "other.handle"})
    before = _roster_bytes()
    held = _register(client)
    assert held.status_code == 409
    assert held.json()["detail"] == "page id is held by a Notion Master Pages row"
    assert _roster_bytes() == before


def test_register_refuses_legacy_rows_and_handles_another_external_page_holds(lab):
    client, _ = lab
    _seed({"integration_id": PAGE_ID, "name": "postiz row", "source": None})
    before = _roster_bytes()
    legacy = _register(client)
    assert legacy.status_code == 409
    assert legacy.json()["detail"] == "page id is held by a roster row this route does not own"
    assert _roster_bytes() == before

    _seed()
    assert _register(client).status_code == 200
    second = northstar_intent(pageId="ns-tt-0000aaaa-zzzz9999")
    response = client.put(
        "/api/control-plane/v1/external-pages/ns-tt-0000aaaa-zzzz9999",
        headers=AUTH,
        json=second,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "handle is held by another roster page"


# ── Source imports, clip jobs and capabilities resolve the registered page ──


def test_source_import_runs_for_a_registered_external_page_and_not_before(lab):
    client, started = lab
    intent = northstar_intent()

    refused = _source_import(client, intent)
    assert refused.status_code == 409
    assert refused.json()["detail"] == "source import Master Pages intent does not match the current roster"
    assert started == []

    assert _register(client, intent).status_code == 200
    accepted = _source_import(client, intent)
    assert accepted.status_code == 200, accepted.json()
    assert accepted.json()["status"] == "queued"
    assert started == [accepted.json()["jobId"]]
    job = cp._load_jobs()["jobs"][accepted.json()["jobId"]]
    assert job["masterPages"] == intent
    assert job["masterPagesHash"] == intent_hash(intent)

    # A stale intent (the page moved niche after this caller read it) refuses.
    _register(client, northstar_intent(contentNiche="POV — Scenic"))
    stale = _source_import(client, intent, key="source:intake:northstar:002")
    assert stale.status_code == 409
    assert stale.json()["detail"] == "source import Master Pages intent does not match the current roster"


def test_clip_job_passes_the_roster_check_for_a_registered_external_page(lab):
    client, _ = lab
    intent = northstar_intent()

    refused = _clip_job(client, intent)
    assert refused.status_code == 409
    assert refused.json()["detail"] == "job Master Pages intent does not match the current roster"

    assert _register(client, intent).status_code == 200
    # Past the roster check, the job stops at the next gate: this page has no
    # dossier publication yet. That gate is the recipe slice's, not this one's.
    passed = _clip_job(client, intent)
    assert passed.status_code == 409
    assert passed.json()["detail"] == "hash-bound dossier publication is required"


def test_capabilities_resolve_the_registered_page_intent(lab):
    client, _ = lab
    assert cp._current_intent_for_capabilities(PAGE_ID) is None
    _register(client)
    intent = northstar_intent()
    assert cp._current_intent_for_capabilities(PAGE_ID) == (intent, intent_hash(intent))
    response = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"Authorization": f"Bearer {TOKEN}", "X-RT-Page-Id": PAGE_ID},
    )
    assert response.status_code == 200


# ── Notion sync and the fleet projection leave the page alone ───────────────


def _notion_row(name, notion_page_id, **overrides):
    row = {
        "integration_id": notion_pages.mint_integration_id(name),
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
        "notion_page_id": notion_page_id,
        "source": "notion",
        "status": "In Production",
        "account_status": "In Production",
        "pipeline": "",
        "page_type": "",
        "content_niche": "TRUCK",
        "content_engine": "ai_video",
        "automation_mode": "Automation",
        "vault_url": "https://example.com/vault",
        "archived": False,
        "sounds_reference": "",
        "go_live_date": "",
        "drive_folder_url": "",
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_notion_sync_never_overwrites_or_removes_an_external_page(lab, monkeypatch):
    client, _ = lab
    _seed(NOTION_ROW)
    assert _register(client).status_code == 200
    registered = copy.deepcopy(roster.get_page(PAGE_ID))
    intent = northstar_intent()

    fetched: list[list[dict]] = [
        # A full fetch that drops the only prior Notion row (prunes it) ...
        [],
        # ... then one that adds Notion rows, including one whose username
        # is this external handle. Notion can only mint acct: ids, and this
        # route refuses those, so the new row lands beside the external one.
        [
            _notion_row("truck.tok.daily", "2f4b1c40aaaabbbbccccddddeeeeffff"),
            _notion_row(HANDLE, "99990000aaaabbbbccccddddeeeeffff"),
        ],
    ]
    for rows in fetched:
        async def fetch(rows=rows):
            return [dict(row) for row in rows]

        monkeypatch.setattr(notion_pages, "fetch_all_pages", fetch)
        result = await notion_pages.sync_into_roster()
        assert result["errors"] == []
        assert roster.get_page(PAGE_ID) == registered
        assert cp._current_master_pages_intent(PAGE_ID, intent) == (intent, intent_hash(intent))

    pages = roster.load_roster()["pages"]
    assert NOTION_ID in pages
    assert notion_pages.mint_integration_id(HANDLE) in pages


def test_fleet_roster_projection_never_includes_an_external_page(lab):
    client, _ = lab
    _seed(NOTION_ROW)
    projection_before, complete = cp._roster_projection()
    assert complete

    assert _register(client).status_code == 200
    projection_after, _ = cp._roster_projection()
    assert projection_after["projectionHash"] == projection_before["projectionHash"]
    assert projection_after["snapshotVersion"] == projection_before["snapshotVersion"]
    assert [page["pageId"] for page in projection_after["pages"]] == [NOTION_ID]

    snapshot = client.get("/api/control-plane/v1/roster", headers=AUTH)
    assert snapshot.status_code == 200
    assert PAGE_ID not in json.dumps(snapshot.json())
    assert HANDLE not in json.dumps(snapshot.json())


# ── Removal ─────────────────────────────────────────────────────────────────


def test_delete_removes_only_rows_this_route_owns(lab):
    client, _ = lab
    legacy = {"integration_id": "postiz:operator", "name": "operator", "source": None}
    _seed(NOTION_ROW, legacy)
    assert _register(client).status_code == 200

    for page_id, detail in (
        (NOTION_ID, "Notion Master Pages rows are removed only by Notion sync"),
        ("postiz:operator", "page id is held by a roster row this route does not own"),
    ):
        before = _roster_bytes()
        response = client.delete(f"/api/control-plane/v1/external-pages/{page_id}", headers=AUTH)
        assert response.status_code == 409
        assert response.json()["detail"] == detail
        assert _roster_bytes() == before

    deleted = client.delete(URL, headers=AUTH)
    assert deleted.status_code == 200
    assert deleted.json() == {"schema": cp.RESPONSE_SCHEMA, "pageId": PAGE_ID, "status": "deleted"}
    assert roster.get_page(PAGE_ID) is None
    assert cp._current_master_pages_intent(PAGE_ID) is None
    assert set(roster.load_roster()["pages"]) == {NOTION_ID, "postiz:operator"}

    again = client.delete(URL, headers=AUTH)
    assert again.status_code == 200
    assert again.json()["status"] == "absent"

    refused = _source_import(client, northstar_intent())
    assert refused.status_code == 409


def test_app_serves_the_external_page_routes():
    import app as app_module

    methods = {
        method
        for route in app_module.app.routes
        if getattr(route, "path", None) == "/api/control-plane/v1/external-pages/{page_id}"
        for method in route.methods
    }
    assert {"PUT", "DELETE"} <= methods
