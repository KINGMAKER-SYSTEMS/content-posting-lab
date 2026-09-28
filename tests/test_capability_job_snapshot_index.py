"""C3: capabilities() for one page touches only that page's jobs, via an
index built once per snapshot — never a full scan of the store per request.
"""

import pytest

from routers import control_plane as cp
from services.json_store import atomic_save

PAGE_COUNT = 50
JOBS_PER_PAGE = 20


def _synthetic_store():
    jobs = {}
    for page_index in range(PAGE_COUNT):
        page_id = f"page-{page_index}"
        for job_index in range(JOBS_PER_PAGE):
            job_id = f"{page_id}-job-{job_index}"
            jobs[job_id] = {
                "jobId": job_id,
                "pageId": page_id,
                "sourceKind": "page_source_import",
                "status": "completed",
                "sourceUrl": f"https://example.test/{page_id}/{job_index}",
            }
    return {"version": 1, "jobs": jobs, "byIdempotency": {}, "served": {}}


@pytest.fixture
def job_path(tmp_path, monkeypatch):
    path = tmp_path / "jobs.json"
    monkeypatch.setattr(cp, "_jobs_path", lambda: path)
    cp._jobs_snapshot = None
    cp._jobs_decode_pending.clear()
    atomic_save(path, _synthetic_store())
    return path


def test_index_covers_every_page_correctly_scoped(job_path):
    snapshot = cp._read_jobs_snapshot_object()
    assert set(snapshot.by_page) == {f"page-{i}" for i in range(PAGE_COUNT)}
    for page_index in range(PAGE_COUNT):
        page_id = f"page-{page_index}"
        assert len(snapshot.by_page[page_id]) == JOBS_PER_PAGE
        assert all(
            job["pageId"] == page_id for job in snapshot.by_page[page_id].values()
        )
    assert set(snapshot.by_source_kind) == {"page_source_import"}
    assert len(snapshot.by_source_kind["page_source_import"]) == PAGE_COUNT * JOBS_PER_PAGE


def test_index_built_once_per_snapshot_not_once_per_request(job_path, monkeypatch):
    calls = []
    real_build = cp._build_jobs_indices

    def counted(data):
        calls.append(1)
        return real_build(data)

    monkeypatch.setattr(cp, "_build_jobs_indices", counted)
    monkeypatch.setattr(cp, "_current_intent_for_capabilities", lambda _: ({}, "hash"))
    monkeypatch.setattr(cp, "list_registered_recipe_bindings", lambda *args: [])
    monkeypatch.setattr(cp, "load_engine_registry", lambda: ({}, "hash"))

    # One decode publishes one index; PAGE_COUNT further requests for
    # PAGE_COUNT different pages, against the same unchanged file, must all
    # reuse it rather than rebuilding per request.
    for page_index in range(PAGE_COUNT):
        result = cp.capabilities(x_rt_page_id=f"page-{page_index}", x_page_id=None)
        assert result["capabilities"] == []

    assert len(calls) == 1


def test_capabilities_lookup_touches_only_its_own_page_slice(job_path, monkeypatch):
    monkeypatch.setattr(cp, "_current_intent_for_capabilities", lambda _: ({}, "hash"))
    monkeypatch.setattr(cp, "list_registered_recipe_bindings", lambda *args: [])
    monkeypatch.setattr(cp, "load_engine_registry", lambda: ({}, "hash"))

    real_identity = cp.canonical_source_identity
    calls = []

    def counted_identity(url):
        calls.append(url)
        return real_identity(url)

    monkeypatch.setattr(cp, "canonical_source_identity", counted_identity)

    cp.capabilities(x_rt_page_id="page-7", x_page_id=None)

    # Only page-7's own JOBS_PER_PAGE jobs were ever considered — not the
    # PAGE_COUNT * JOBS_PER_PAGE jobs in the whole store.
    assert len(calls) == JOBS_PER_PAGE
    assert all("page-7/" in url for url in calls)
