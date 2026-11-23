"""POST /api/control-plane/v1/source-cut-records: windows cut outside a Lab job.

A clip an operator tool cut from a Lab master never became a Lab job, so the
planners did not know its window was used. Once recorded, every later cut
from that master must refuse an overlap, the record must survive repeats
without doubling, and a dry run must write nothing.
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.control_plane as cp
from services.control_plane_sources import source_cut_overlaps_used_frames

TOKEN = "test-control-plane-token"
MASTER = "dcfc2e531e7ad365aaa6066094462765cbec5ca82e9f273d78c8d03f4575f47b"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-RT-Lane": "content-bucket-control-plane"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    return TestClient(app)


def _body(apply, cuts=((23000, 6000), (38000, 6000))):
    return {
        "schema": "content-lab.source-cut-records.v1", "pageId": "acct:thingsleftunsaid52",
        "sourceLibraryId": "shipstream-thingsleftunsaid52-fca169b3bae44946", "masterSha256": MASTER, "masterDurationMs": 600000,
        "usedAt": "2026-11-23T14:20:33Z", "note": "tlu-recut-1123", "apply": apply,
        "cuts": [{"startMs": start, "durationMs": duration} for start, duration in cuts],
    }


def _overlaps(start_ms, duration_ms):
    master = SimpleNamespace(sha256=MASTER)
    recipe = SimpleNamespace(masters=[master], source_library_id="other-lib", source_library_hash="h")
    slots, _history = cp._source_dna_cut_ledger(cp._load_jobs(), recipe, "")
    return source_cut_overlaps_used_frames(recipe, slots, master, start_ms, duration_ms)


def test_recorded_windows_block_overlapping_cuts_and_repeat_safely(client):
    url = "/api/control-plane/v1/source-cut-records"
    dry = client.post(url, headers=HEADERS, json=_body(False))
    assert dry.status_code == 200 and dry.json()["wouldWrite"] == [[23000, 6000], [38000, 6000]]
    assert not _overlaps(25000, 6000)                      # dry run wrote nothing

    wrote = client.post(url, headers=HEADERS, json=_body(True))
    assert wrote.status_code == 200 and wrote.json()["written"] == [[23000, 6000], [38000, 6000]]
    assert _overlaps(25000, 6000) and _overlaps(17500, 6000)   # any overlap is refused
    assert not _overlaps(29000, 6000)                      # the gap between stays free

    again = client.post(url, headers=HEADERS, json=_body(False))
    assert again.json()["alreadyRecorded"] == [[23000, 6000], [38000, 6000]]
    assert again.json()["wouldWrite"] == []
    client.post(url, headers=HEADERS, json=_body(True))
    rows = cp.compaction.read_archive_index(cp._load_jobs())["sourceDnaCuts"]
    assert len(rows) == 2                                  # a repeat never doubles a row


@pytest.mark.parametrize("change", [
    {"masterSha256": "DCFC"}, {"cuts": []}, {"cuts": [{"startMs": -1, "durationMs": 6000}]},
    {"cuts": [{"startMs": 1, "durationMs": 6000}] * 2}, {"usedAt": "2026-11-23"}, {"apply": "yes"},
    {"extra": 1}, {"cuts": [{"startMs": 595000, "durationMs": 6000}]}, {"masterDurationMs": 0},
])
def test_bad_bodies_are_refused_and_write_nothing(client, change):
    response = client.post("/api/control-plane/v1/source-cut-records", headers=HEADERS,
                           json={**_body(True), **change})
    assert response.status_code == 400
    assert not _overlaps(23000, 6000)


def test_needs_the_machine_bearer(client):
    response = client.post("/api/control-plane/v1/source-cut-records",
                           headers={"X-RT-Lane": "content-bucket-control-plane"}, json=_body(True))
    assert response.status_code == 401
    assert not _overlaps(23000, 6000)


def test_keeps_existing_rows_and_checks_a_known_master_length(client):
    cp._save_jobs({**cp._empty_jobs(), "jobs": {"j": {
        "pageId": "acct:x", cp.page_source_library.JOB_LIBRARY_MASTERS_KEY: [{"sha256": MASTER, "durationMs": 600000}],
    }}, cp.compaction.ARCHIVE_INDEX_KEY: {**cp.compaction.empty_index(), "sourceDnaCuts": [
        {"masterSha256": MASTER, "startMs": 100000, "durationMs": 6000}]}})
    url = "/api/control-plane/v1/source-cut-records"
    assert client.post(url, headers=HEADERS, json={**_body(True), "masterDurationMs": 599000}).status_code == 409
    response = client.post(url, headers=HEADERS, json=_body(True))
    assert response.status_code == 200 and response.json()["masterKnownToLab"] is True
    assert _overlaps(100000, 6000) and _overlaps(23000, 6000)   # old row kept, new rows added


def test_refuses_when_the_index_is_lost_but_archives_exist(client):
    cp._jobs_path().with_name(cp._jobs_path().name + ".archive-202611.jsonl").write_text("{}\n")
    response = client.post("/api/control-plane/v1/source-cut-records", headers=HEADERS, json=_body(True))
    assert response.status_code == 409
    assert cp.compaction.ARCHIVE_INDEX_KEY not in cp._load_jobs()
