"""Lost-ACK lookup binds the original immutable request without paid work."""

import json

import pytest
from fastapi.testclient import TestClient

import app as app_module
import routers.control_plane as cp
from tests.master_pages_fixtures import master_pages

TOKEN = "lookup-control-plane-bearer"
APP_KEY = "lookup-app-api-key"
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
    monkeypatch.setattr(app_module, "_APP_API_KEY", APP_KEY)
    client = TestClient(app_module.app)
    intent, revision = master_pages(PAGE)
    body = {
        "pageId": PAGE,
        "lane": LANE,
        "engine": "ai_video",
        "lockedRecipeId": "truck-scenic:master",
        "recipeVersion": "v1",
        "quantity": 1,
        "constraints": {},
        "sourceIsolation": {"partitionKey": f"page:{PAGE}"},
        "policyHash": "sha256:policy",
        "masterPages": intent,
        "masterPagesHash": revision,
    }
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "X-API-Key": APP_KEY,
        "X-RT-Page-Id": PAGE,
        "X-RT-Lane": LANE,
        "Idempotency-Key": KEY,
    }
    job = {
        "jobId": JOB_ID,
        "idempotencyKey": KEY,
        "jobRequestHash": cp._job_request_hash(body, page_id=PAGE, lane=LANE, master_pages=intent),
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

    return client, headers, body, job, path, persist


def post(client, headers, body):
    return client.post(URL, headers=headers, json=body)


def test_lost_ack_lookup_returns_only_safe_receipt_without_side_effects(lookup, monkeypatch):
    client, headers, body, _, path, persist = lookup
    original = persist()
    def unexpected(*args, **kwargs):
        raise AssertionError("lookup must not start, resume, save or debit")
    monkeypatch.setattr(cp, "_load_jobs", unexpected)
    monkeypatch.setattr(cp, "_save_jobs", unexpected)
    monkeypatch.setattr(cp, "_start_dossier_generation", unexpected)
    monkeypatch.setattr(cp.generation_budget, "reserve", unexpected, raising=False)
    response = post(client, headers, body)
    assert response.status_code == 200
    assert response.json() == {
        "schema": cp.RESPONSE_SCHEMA,
        "jobId": JOB_ID,
        "status": "queued",
    }
    assert path.read_bytes() == original


def test_app_and_control_plane_credentials_are_both_required(lookup):
    client, headers, body, _, _, persist = lookup
    persist()
    assert APP_KEY != TOKEN
    no_app = {k: v for k, v in headers.items() if k != "X-API-Key"}
    no_plane = {k: v for k, v in headers.items() if k != "Authorization"}
    assert post(client, no_app, body).status_code == 401
    assert post(client, no_plane, body).status_code == 401
    assert post(client, {**headers, "Authorization": f"Bearer {APP_KEY}"}, body).status_code == 401
    assert post(client, {**headers, "X-API-Key": TOKEN}, body).status_code == 401


def test_missing_foreign_and_archived_are_indistinguishable(lookup):
    client, headers, body, job, path, persist = lookup
    persist()
    missing = post(client, {**headers, "Idempotency-Key": "missing-key-123"}, body)
    other_page = "acct:other"
    other_intent, other_revision = master_pages(other_page)
    foreign_page = post(
        client, {**headers, "X-RT-Page-Id": other_page},
        {**body, "pageId": other_page, "masterPages": other_intent,
         "masterPagesHash": other_revision},
    )
    foreign_lane = post(
        client, {**headers, "X-RT-Lane": "other-lane"},
        {**body, "lane": "other-lane"},
    )
    archived_bytes = persist(jobs={})
    archived = post(client, headers, body)
    for response in (missing, foreign_page, foreign_lane, archived):
        assert response.status_code == 404
        assert response.json() == missing.json()
    assert path.read_bytes() == archived_bytes


@pytest.mark.parametrize("mutation", [
    {"jobId": "cpl-ffffffffffffffff"},
    {"idempotencyKey": "different-key-123"},
    {"pageId": "acct:other"},
    {"lane": "another-lane"},
    {"status": None},
    {"jobRequestHash": "sha256:" + "0" * 64},
    {"jobRequestHash": None},
])
def test_mismatched_and_legacy_live_row_is_not_accepted(lookup, mutation):
    client, headers, body, job, _, persist = lookup
    persist(jobs={JOB_ID: {**job, **mutation}})
    assert post(client, headers, body).status_code == 404


def test_altered_original_request_holds_without_paid_work(lookup):
    client, headers, body, _, _, persist = lookup
    persist()
    altered = {**body, "quantity": 2}
    response = post(client, headers, altered)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "reconciliation_required"


def test_corrupt_store_fails_closed(lookup):
    client, headers, body, _, path, _ = lookup
    path.write_text("{not-json")
    response = post(client, headers, body)
    assert response.status_code == 503
    assert response.json()["detail"] == "job store temporarily unavailable"


def test_malformed_mapping_fails_closed(lookup):
    client, headers, body, _, _, persist = lookup
    persist(keys=[KEY])
    assert post(client, headers, body).status_code == 503


def test_legacy_bool_quantity_exact_original_reconciles_but_integer_does_not(lookup):
    client, headers, body, job, _, persist = lookup
    original = {**body, "quantity": True}
    true_hash = cp._job_request_hash(
        original, page_id=PAGE, lane=LANE, master_pages=body["masterPages"],
    )
    persist(jobs={JOB_ID: {**job, "jobRequestHash": true_hash}})
    assert post(client, headers, original).status_code == 200
    assert post(client, headers, body).status_code == 404
