"""Lost-ACK reconciliation must be scoped and must not resume paid work."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.control_plane as cp

TOKEN = "lookup-test-bearer"
PAGE = "acct:truck-page"
LANE = "content-bucket-control-plane"
KEY = "acct:truck-page:hash1:source_replenish"
JOB_ID = "cpl-0123456789abcdef"
URL = "/api/control-plane/v1/job-receipts/by-idempotency-key"


@pytest.fixture
def lookup(monkeypatch, tmp_path):
    path = tmp_path / "jobs.json"
    monkeypatch.setattr(cp, "_jobs_path", lambda: path)
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    client = TestClient(app)
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "X-RT-Page-Id": PAGE,
        "X-RT-Lane": LANE,
        "Idempotency-Key": KEY,
    }
    job = {
        "jobId": JOB_ID,
        "idempotencyKey": KEY,
        "pageId": PAGE,
        "lane": LANE,
        "status": "queued",
        "token": "private-download-token",
        "providerFailures": [{"raw": "private provider content"}],
    }

    def persist(*, jobs=None, keys=None):
        store = {
            "version": 1,
            "jobs": {JOB_ID: job} if jobs is None else jobs,
            "byIdempotency": {KEY: JOB_ID} if keys is None else keys,
            "served": {},
        }
        path.write_text(json.dumps(store))
        return path.read_bytes()

    return client, headers, job, path, persist


def test_lost_ack_lookup_returns_only_safe_receipt_without_side_effects(lookup, monkeypatch):
    client, headers, job, path, persist = lookup
    original = persist()
    def unexpected(*args, **kwargs):
        raise AssertionError("lookup must not start, resume, save or debit")
    monkeypatch.setattr(cp, "_load_jobs", unexpected)
    monkeypatch.setattr(cp, "_save_jobs", unexpected)
    monkeypatch.setattr(cp, "_start_dossier_generation", unexpected)
    monkeypatch.setattr(cp.generation_budget, "reserve", unexpected, raising=False)
    response = client.get(URL, headers=headers)
    assert response.status_code == 200
    assert response.json() == {
        "schema": cp.RESPONSE_SCHEMA,
        "jobId": JOB_ID,
        "status": "queued",
    }
    assert path.read_bytes() == original


def test_lookup_auth_page_and_lane_isolation(lookup):
    client, headers, _, _, persist = lookup
    persist()
    assert client.get(URL, headers={k: v for k, v in headers.items() if k != "Authorization"}).status_code == 401
    missing = client.get(URL, headers={**headers, "Idempotency-Key": "missing-key-123"})
    wrong_page = client.get(URL, headers={**headers, "X-RT-Page-Id": "acct:other"})
    wrong_lane = client.get(URL, headers={**headers, "X-RT-Lane": "another-lane"})
    for response in (missing, wrong_page, wrong_lane):
        assert response.status_code == 404
        assert response.json() == missing.json()


def test_archived_mapping_and_missing_key_are_indistinguishable(lookup):
    client, headers, _, path, persist = lookup
    original = persist(jobs={})
    archived = client.get(URL, headers=headers)
    absent = client.get(URL, headers={**headers, "Idempotency-Key": "missing-key-123"})
    assert archived.status_code == 404
    assert archived.json() == absent.json()
    assert path.read_bytes() == original


@pytest.mark.parametrize("mutation", [
    {"jobId": "cpl-ffffffffffffffff"},
    {"idempotencyKey": "different-key-123"},
    {"pageId": "acct:other"},
    {"lane": "another-lane"},
    {"status": None},
])
def test_mismatched_live_row_is_not_accepted(lookup, mutation):
    client, headers, job, _, persist = lookup
    persist(jobs={JOB_ID: {**job, **mutation}})
    assert client.get(URL, headers=headers).status_code == 404


def test_corrupt_store_fails_closed_without_start(lookup):
    client, headers, _, path, _ = lookup
    path.write_text("{not-json")
    response = client.get(URL, headers=headers)
    assert response.status_code == 503
    assert response.json()["detail"] == "job store temporarily unavailable"


def test_malformed_mapping_fails_closed(lookup):
    client, headers, job, _, persist = lookup
    persist(keys=[KEY])
    response = client.get(URL, headers=headers)
    assert response.status_code == 503
