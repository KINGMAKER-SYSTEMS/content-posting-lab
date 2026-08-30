"""Tests for routers/control_plane.py GET /api/control-plane/v1/roster.

The roster snapshot is the only door the Notion/Master Pages ontology walks
through on its way to the control plane. The properties that matter:

  * it carries ONLY the ontology fields — never the account credentials the
    roster cache also holds (password, signup_email, fwd_address, aliases);
  * a null field is a true statement ("Notion does not say"), never a
    dropped key — the plane raises blockers from these, it must be able to
    tell "unknown" from "absent";
  * the snapshot version is a content hash — same roster, same version;
    any change, different version. The plane reconciles on that signal;
  * rows with no stable identity never cross — the plane could never
    address them;
  * capturedAt is the roster cache's mtime, not the request time.

The roster data file is redirected to a throwaway tmp dir; Notion is never
hit.
"""

import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import services.roster as roster
from services import json_store
import routers.control_plane as cp
from routers.control_plane import router


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(roster, "ROSTER_PATH", tmp_path / "page_roster.json")
    monkeypatch.setattr(json_store, "_LOCKS", {})
    app = FastAPI()
    app.include_router(router, prefix="/api/control-plane")
    return TestClient(app)


def _seed(**pages):
    data = {"version": 1, "pages": {}}
    for page_id, fields in pages.items():
        row = {"integration_id": page_id, "name": fields.pop("name")}
        row.update(fields)
        data["pages"][page_id] = row
    roster.save_roster(data)


NOTION_ROW = {
    "name": "truck.tok.daily",
    "provider": "tiktok",
    "group": "WARNER",
    "group_label": "Warner UGC",
    "page_type": "UGC theme page",
    "account_type": "ugc",
    "poster_name": "mon",
    "status": "Active",
    "project": "trucks",
    "tiktok_url": "https://www.tiktok.com/@truck.tok.daily",
    "notion_page_id": "2f4b1c40-aaaa-bbbb-cccc-ddddeeeeffff",
    "source": "notion",
    "content_engine": "ai_video",
    "automation_mode": "Automation",
    "vault_url": "https://shipstream.risingtidesviral.com/vault/truck.tok.daily",
    "pipeline": "Flow Stage",
    "sounds_reference": "https://example.com/sounds/trucks",
    "archived": False,
    # Credentials the cache legitimately holds — none may cross.
    "password": "hunter2",
    "signup_email": "truck@example.com",
    "fwd_address": "secret@example.com",
    "email_alias": "truck@risingtidesviral.com",
}


def test_requires_lane_header(client):
    assert client.get("/api/control-plane/v1/roster").status_code == 400


def test_snapshot_carries_the_ontology_and_only_the_ontology(client):
    _seed(**{"acct:truck-tok-daily": dict(NOTION_ROW)})
    res = client.get("/api/control-plane/v1/roster", headers={"X-RT-Lane": "warner"})
    assert res.status_code == 200
    body = res.json()
    assert body["schema"] == "content-lab.response.v1"
    assert body["snapshotVersion"].startswith("r")

    [page] = body["pages"]
    assert page["pageId"] == "acct:truck-tok-daily"
    assert page["handle"] == "truck.tok.daily"
    assert page["group"] == "WARNER"
    assert page["groupLabel"] == "Warner UGC"
    assert page["pageType"] == "UGC theme page"
    assert page["posterName"] == "mon"
    assert page["project"] == "trucks"
    assert page["source"] == "notion"
    assert page["contentEngine"] == "ai_video"
    assert page["automationMode"] == "Automation"
    assert page["vaultUrl"] == "https://shipstream.risingtidesviral.com/vault/truck.tok.daily"
    assert page["pipeline"] == "Flow Stage"
    assert page["archived"] is False

    leaked = json.dumps(page)
    for secret in ("hunter2", "truck@example.com", "secret@example.com", "email_alias", "password"):
        assert secret not in leaked


def test_null_fields_are_explicit_never_dropped(client):
    _seed(**{"acct:bare": {"name": "bare.page", "source": "notion", "project": None, "group": None}})
    res = client.get("/api/control-plane/v1/roster", headers={"X-RT-Lane": "warner"})
    [page] = res.json()["pages"]
    for field in ("group", "groupLabel", "pageType", "accountType", "posterName",
                  "status", "project", "tiktokUrl", "notionPageId",
                  "contentEngine", "automationMode", "vaultUrl", "pipeline",
                  "soundsReference"):
        assert field in page, f"{field} must be present even when null"
        assert page[field] is None
    assert page["source"] == "notion"
    assert page["archived"] is False


def test_version_is_a_content_hash_not_a_counter(client):
    _seed(**{"acct:a": {"name": "a.page", "source": "notion"}, "acct:b": {"name": "b.page", "source": "notion"}})
    first = client.get("/api/control-plane/v1/roster", headers={"X-RT-Lane": "warner"}).json()
    again = client.get("/api/control-plane/v1/roster", headers={"X-RT-Lane": "warner"}).json()
    assert first["snapshotVersion"] == again["snapshotVersion"]

    _seed(**{"acct:a": {"name": "a.page", "source": "notion"}, "acct:b": {"name": "b.page", "source": "notion", "group": "WARNER"}})
    changed = client.get("/api/control-plane/v1/roster", headers={"X-RT-Lane": "warner"}).json()
    assert changed["snapshotVersion"] != first["snapshotVersion"]


def test_pages_are_deterministically_ordered(client):
    _seed(**{"acct:z": {"name": "z", "source": "notion"}, "acct:a": {"name": "a", "source": "notion"}, "acct:m": {"name": "m", "source": "notion"}})
    pages = client.get("/api/control-plane/v1/roster", headers={"X-RT-Lane": "warner"}).json()["pages"]
    assert [p["pageId"] for p in pages] == ["acct:a", "acct:m", "acct:z"]


def test_rows_without_stable_identity_never_cross(client):
    data = {"version": 1, "pages": {"row-no-name": {"integration_id": "row-no-name", "source": "notion"}}}
    roster.save_roster(data)
    pages = client.get("/api/control-plane/v1/roster", headers={"X-RT-Lane": "warner"}).json()["pages"]
    assert pages == []


def test_captured_at_is_the_cache_mtime_not_the_request_clock(client):
    _seed(**{"acct:a": {"name": "a.page", "source": "notion"}})
    body = client.get("/api/control-plane/v1/roster", headers={"X-RT-Lane": "warner"}).json()
    mtime = os.path.getmtime(roster.ROSTER_PATH)
    assert abs(body["capturedAt"] and __import__("datetime").datetime.fromisoformat(body["capturedAt"]).timestamp() - mtime) < 1


def test_empty_cache_is_an_empty_snapshot_not_an_error(client):
    body = client.get("/api/control-plane/v1/roster", headers={"X-RT-Lane": "warner"}).json()
    assert body["pages"] == []
    assert body["capturedAt"] is None
    assert body["snapshotVersion"].startswith("r")


def test_legacy_operator_rows_do_not_enter_the_master_pages_projection(client):
    _seed(
        **{
            "acct:notion": {"name": "notion.page", "source": "notion"},
            "postiz:legacy": {"name": "legacy.page", "source": None},
        },
    )
    pages = client.get(
        "/api/control-plane/v1/roster", headers={"X-RT-Lane": "warner"},
    ).json()["pages"]
    assert [page["pageId"] for page in pages] == ["acct:notion"]


def test_snapshot_carries_the_content_niche(client):
    _seed(**{"acct:truck-tok-daily": dict(NOTION_ROW, content_niche="TRUCK")})
    res = client.get("/api/control-plane/v1/roster", headers={"X-RT-Lane": "warner"})
    [page] = res.json()["pages"]
    assert page["contentNiche"] == "TRUCK"


def test_current_intent_rebinds_one_notion_identity_to_the_operational_rail_page_id(client):
    _seed(**{"acct:miles-of-memories77": dict(
        NOTION_ROW,
        name="miles.of.memories77",
        group="ATLANTIC",
        content_niche="POV — Night Core",
        content_engine="sourced_video",
        notion_page_id="3281465b-b829-807d-b852-dffeb7a48468",
        vault_url="https://shipstream.risingtidesviral.com/vault/miles.of.memories77",
    )})
    snapshot = client.get(
        "/api/control-plane/v1/roster", headers={"X-RT-Lane": "automation"},
    ).json()["pages"][0]
    operational_id = "acct:rail:6880a7944b9f074c700d6218"
    asserted = {"schema": "master-pages.page-intent.v1", **snapshot, "pageId": operational_id}
    resolved = cp._current_master_pages_intent(operational_id, asserted)
    assert resolved is not None
    intent, revision = resolved
    assert intent == asserted
    assert revision.startswith("sha256:")


def test_machine_refresh_returns_counts_without_roster_rows(client, monkeypatch):
    import services.notion_pages as notion_pages

    monkeypatch.setattr(notion_pages, "is_configured", lambda: True)
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-control-plane-token")

    async def refresh():
        return {
            "added": 3,
            "updated": 19,
            "total_in_notion": 22,
            "errors": ["bounded failure"],
            "pages": [{"password": "must-not-cross"}],
        }

    monkeypatch.setattr(notion_pages, "sync_into_roster", refresh)
    response = client.post(
        "/api/control-plane/v1/roster/refresh",
        headers={
            "X-RT-Lane": "content-bucket-control-plane",
            "Authorization": "Bearer test-control-plane-token",
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "schema": "content-lab.response.v1",
        "added": 3,
        "updated": 19,
        "totalInNotion": 22,
        "errorCount": 1,
    }
    assert "password" not in response.text


def test_machine_refresh_requires_lane_and_notion_configuration(client, monkeypatch):
    import services.notion_pages as notion_pages

    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-control-plane-token")
    assert client.post("/api/control-plane/v1/roster/refresh").status_code == 400
    unauthorized = client.post(
        "/api/control-plane/v1/roster/refresh",
        headers={"X-RT-Lane": "content-bucket-control-plane"},
    )
    assert unauthorized.status_code == 401
    monkeypatch.setattr(notion_pages, "is_configured", lambda: False)
    response = client.post(
        "/api/control-plane/v1/roster/refresh",
        headers={
            "X-RT-Lane": "content-bucket-control-plane",
            "Authorization": "Bearer test-control-plane-token",
        },
    )
    assert response.status_code == 503
