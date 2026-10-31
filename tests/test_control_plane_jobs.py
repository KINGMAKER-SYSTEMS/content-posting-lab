"""Legacy project-library jobs cannot bypass the hash-bound dossier contract."""

import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.control_plane as cp
from tests.master_pages_fixtures import bind_current_intent, master_pages


TOKEN = "test-control-plane-token"
PAGE_ID = "acct:truck-page"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "X-RT-Page-Id": PAGE_ID,
    "X-RT-Lane": "content-bucket-control-plane",
    "Idempotency-Key": "acct:truck-page:hash1:source_replenish",
}


@pytest.fixture
def lab(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    project = projects / "trucks"
    videos = project / "videos"
    videos.mkdir(parents=True)
    prompts = project / "prompts.json"
    prompts.write_text(json.dumps([{"prompt": "server-owned", "provider": "wan-t2v"}]))
    (videos / "a.mp4").write_bytes(b"clip-bytes")
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir()
    (recipes_dir / "trucks.json").write_text(json.dumps({
        "registered": True, "project": "trucks", "maxQuantity": 3,
    }))
    monkeypatch.setattr(cp, "PROJECTS_DIR", projects)
    monkeypatch.setattr(cp, "_jobs_path", lambda: tmp_path / "jobs.json")
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", TOKEN)
    intent, revision = master_pages(PAGE_ID, handle="truck.page")
    bind_current_intent(monkeypatch, cp, intent, revision)
    app = FastAPI()
    app.include_router(cp.router, prefix="/api/control-plane")
    version = "v" + hashlib.sha256(prompts.read_bytes()).hexdigest()[:12]
    return TestClient(app), version, intent, revision


def job_body(version, intent, revision, **overrides):
    body = {
        "pageId": PAGE_ID,
        "lane": "content-bucket-control-plane",
        "engine": "content_lab",
        "lockedRecipeId": "trucks",
        "recipeVersion": version,
        "quantity": 1,
        "constraints": {},
        "sourceIsolation": {"partitionKey": f"page:{PAGE_ID}"},
        "policyHash": "sha256:policy",
        "masterPages": intent,
        "masterPagesHash": revision,
    }
    body.update(overrides)
    return body


def test_legacy_project_recipe_cannot_execute_without_master_pages_engine_alignment(lab):
    client, version, intent, revision = lab
    response = client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(version, intent, revision),
        headers=HEADERS,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "job Master Pages intent is missing, stale, or engine-mismatched"
    assert not cp._jobs_path().exists()


def test_intent_format_is_advertised_before_recipe_publication(lab):
    client, _, _, _ = lab
    response = client.get(
        "/api/control-plane/v1/capabilities",
        headers={"X-RT-Page-Id": PAGE_ID, "Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    capabilities = response.json()["capabilities"]
    assert any(entry["recipeId"] == "truck-scenic:master"
               and entry["engine"] == "ai_video"
               and entry["maxQuantity"] > 0 for entry in capabilities)
    assert all(entry["recipeId"] != "trucks" for entry in capabilities)


def test_job_contract_rejects_prompt_fields_unknown_fields_and_missing_auth(lab):
    client, version, intent, revision = lab
    assert client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(version, intent, revision, prompt="caller text"),
        headers=HEADERS,
    ).status_code == 400
    assert client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(version, intent, revision, nonsense=1),
        headers=HEADERS,
    ).status_code == 400
    assert client.post(
        "/api/control-plane/v1/jobs", json=job_body(version, intent, revision),
    ).status_code == 401


def test_job_master_pages_hash_and_current_roster_are_exact(lab, monkeypatch):
    client, version, intent, revision = lab
    mismatched = job_body(version, intent, "sha256:" + "0" * 64)
    assert client.post("/api/control-plane/v1/jobs", json=mismatched, headers=HEADERS).status_code == 409
    monkeypatch.setattr(cp, "_current_master_pages_intent", lambda *_: None)
    assert client.post(
        "/api/control-plane/v1/jobs",
        json=job_body(version, intent, revision),
        headers=HEADERS,
    ).status_code == 409


def _store_status_job(status, error=None):
    job_id = "cpl-0123456789abcdef"
    job = {
        "jobId": job_id,
        "pageId": PAGE_ID,
        "status": status,
        "progress": 0,
        "clips": [],
    }
    if error is not None:
        job["error"] = error
    store = cp._empty_jobs()
    store["jobs"][job_id] = job
    cp._save_jobs(store)
    return job_id


def test_status_serves_named_reason_for_legacy_blank_terminal_error(lab):
    client, _, _, _ = lab
    job_id = _store_status_job("failed", " \t ")

    response = client.get(f"/api/control-plane/v1/jobs/{job_id}", headers=HEADERS)

    assert response.status_code == 200
    error = response.json()["error"]
    assert error == "lab_failure_reason_unrecorded"
    assert error == error.strip()
    assert 0 < len(error) <= 300


@pytest.mark.parametrize("status", ["queued", "running"])
def test_status_omits_error_for_nonterminal_jobs(lab, status):
    client, _, _, _ = lab
    job_id = _store_status_job(status, "stale failure should not leak")

    response = client.get(f"/api/control-plane/v1/jobs/{job_id}", headers=HEADERS)

    assert response.status_code == 200
    assert "error" not in response.json()


def test_message_less_generation_failure_writer_records_named_reason(lab):
    _client, _, _, _ = lab
    job_id = _store_status_job("running")

    # This is the generic generation-runner seam: its old `str(error)[:300]`
    # was empty for RuntimeError(), leaving the Worker unable to validate polls.
    cp._update_job(
        job_id,
        status="failed",
        error=cp._exception_failure_reason(RuntimeError()),
    )

    stored = cp._load_jobs()["jobs"][job_id]
    assert stored["error"] == "generation_failed_without_message:RuntimeError"
    assert 0 < len(stored["error"]) <= 300


@pytest.mark.parametrize("raw,expected", [
    ("a" + " " * 400 + "b", "a"),
    ("🙂" * 200, "🙂" * 150),
    ("\ufeffprefix\ufeff", "prefix"),
    ("\ufeff", "lab_failure_reason_unrecorded"),
    ("\x1cunchanged\x1c", "\x1cunchanged\x1c"),
    ("a" * 299, "a" * 299),
    ("a" * 300, "a" * 300),
    ("a" * 301, "a" * 300),
    ("a" * 299 + "🙂", "a" * 299),
    ("a" * 298 + "🙂", "a" * 298 + "🙂"),
    ("\ufeff" + " " * 301 + "z", "z"),
])
def test_legacy_failure_reason_matches_worker_string_contract(lab, raw, expected):
    client, _, _, _ = lab
    job_id = _store_status_job("failed", raw)
    before = cp._jobs_path().read_bytes()
    response = client.get(f"/api/control-plane/v1/jobs/{job_id}", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["error"] == expected
    assert cp._jobs_path().read_bytes() == before


@pytest.mark.parametrize("raw,expected", [
    ("a" + " " * 400 + "b", "a"),
    ("🙂" * 200, "🙂" * 150),
    ("\ufeffx\ufeff", "x"),
    ("\ufeff", "generation_failed_without_message"),
    ("\x1cunchanged\x1c", "\x1cunchanged\x1c"),
])
def test_failure_writer_persists_worker_compatible_reason(lab, raw, expected):
    _client, _, _, _ = lab
    job_id = _store_status_job("running")
    cp._update_job(job_id, status="failed", error=raw)
    assert cp._load_jobs()["jobs"][job_id]["error"] == expected


@pytest.mark.parametrize("raw,expected", [
    ("\ud800", "\ufffd"),
    ("\udfff", "\ufffd"),
    ("x🙂\ud800z\udfffy", "x🙂\ufffdz\ufffdy"),
])
def test_unpaired_surrogates_are_safe_in_writer_and_status(lab, raw, expected):
    client, _, _, _ = lab
    job_id = _store_status_job("running")
    cp._update_job(job_id, status="failed", error=raw)
    assert cp._load_jobs()["jobs"][job_id]["error"] == expected
    legacy_id = _store_status_job("failed", "placeholder")
    legacy = json.loads(cp._jobs_path().read_text())
    legacy["jobs"][legacy_id]["error"] = raw
    cp._jobs_path().write_text(json.dumps(legacy, ensure_ascii=True))
    before = cp._jobs_path().read_bytes()
    response = client.get(f"/api/control-plane/v1/jobs/{legacy_id}", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["error"] == expected
    assert cp._jobs_path().read_bytes() == before


@pytest.mark.parametrize("char", list("\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"))
def test_worker_trim_whitespace_boundaries(lab, char):
    client, _, _, _ = lab
    job_id = _store_status_job("failed", char + "word" + char)
    before = cp._jobs_path().read_bytes()
    response = client.get(f"/api/control-plane/v1/jobs/{job_id}", headers=HEADERS)
    assert response.json()["error"] == "word"
    assert cp._jobs_path().read_bytes() == before


@pytest.mark.parametrize("char", list("\x1c\x1d\x1e\x1f\x85"))
def test_python_only_whitespace_is_not_discarded(lab, char):
    client, _, _, _ = lab
    job_id = _store_status_job("failed", char + "word" + char)
    response = client.get(f"/api/control-plane/v1/jobs/{job_id}", headers=HEADERS)
    assert response.json()["error"] == char + "word" + char
