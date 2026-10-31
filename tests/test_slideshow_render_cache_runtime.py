"""Actual enqueue/runner/adapter contract against a synthetic stateful provider.

No production provider incident is reproduced. Network and local encoding alone
are replaced. Duplicate dispatch is explicit; no automatic retry is asserted.
"""
import json
from pathlib import Path

import httpx
import pytest

from routers import control_plane as cp
from services import control_plane_slideshows as slides
from tests.test_control_plane_slideshow_execution import (
    lab, headers, job_body, PAGE_ID, RENDERER_REVISION,
)


def _same_plan_ids(payload, library):
    """Choose identities through the real planner, without replacing its output."""
    recipe = slides.resolve_slideshow_recipe(payload)
    assert recipe is not None
    seen = {}
    for number in range(512):
        suffix = f"{number:016x}"
        plans = slides.plan_slideshows(recipe, library, 1, set(), "cpl-" + suffix)
        assert len(plans) == 1
        signature = plans[0]["signature"]
        if signature in seen:
            return seen[signature], suffix, signature
        seen[signature] = suffix
    raise AssertionError("fixture must offer two real job identities selecting one plan")


def _stateful_provider(monkeypatch, library, *, ambiguous=False):
    source_bytes = {item.key: b"x" * item.bytes for item in library.objects}
    cache, jobs, posts = {}, {}, []
    lost = False

    def respond(request):
        nonlocal lost
        path = request.url.path
        if request.url.host == "r2.example":
            assert request.method in {"GET", "HEAD"}
            assert "authorization" not in request.headers
            return httpx.Response(200, content=source_bytes[path.lstrip("/")],
                                  headers={"etag": '"fixture-stable-source"'})
        assert request.url.host == "syzygy.example"
        assert request.headers["authorization"] == "Bearer syz_test_key"
        if path == "/readyz":
            return httpx.Response(200, json={"ok": True, "revision": RENDERER_REVISION})
        if path == "/api/v1/render":
            assert request.method == "POST"
            key = request.headers["idempotency-key"]
            body = json.loads(request.content)
            posts.append((key, body))
            if key not in cache:
                remote = f"{len(cache) + 1:012x}"
                cache[key] = remote
                jobs[remote] = "done" if ambiguous or len(cache) > 1 else "failed"
            remote = cache[key]
            if ambiguous and not lost:
                lost = True  # Accepted remotely, but the reply never reached Lab.
                raise httpx.ReadTimeout("fixture response lost after acceptance", request=request)
            return httpx.Response(200, json={"job_id": remote, "quality": "full"})
        if path.startswith("/api/v1/render/"):
            remote = path.rsplit("/", 1)[1]
            status = jobs[remote]
            return httpx.Response(200, json={
                "status": status, "progress": 100,
                "result": {"play_url": f"/render-file/{remote}.mp4"} if status == "done" else None,
                "quality": "full", "error": "fixture render failed" if status == "failed" else None,
            })
        assert path.startswith("/render-file/") and request.method == "GET"
        return httpx.Response(200, content=b"synthetic-provider-output")

    actual_client = httpx.AsyncClient
    monkeypatch.setattr(slides.httpx, "AsyncClient", lambda **kwargs: actual_client(
        transport=httpx.MockTransport(respond), **kwargs))

    async def encode(src, dst, *_args, **_kwargs):
        assert Path(src).read_bytes() == b"synthetic-provider-output"
        Path(dst).write_bytes(b"synthetic-treated-output")

    monkeypatch.setattr(cp, "run_color_correct", encode)
    return cache, jobs, posts


def _enqueue(client, payload, key):
    response = client.post("/api/control-plane/v1/jobs", json=job_body(1, payload), headers=headers(key))
    assert response.status_code == 200, response.json()
    return response.json()["jobId"]


@pytest.mark.asyncio
async def test_actual_new_job_escapes_failed_remote_cache_and_request_replay_stays_put(lab, monkeypatch):
    client, _root, started, library, payload = lab
    one, two, signature = _same_plan_ids(payload, library)
    suffixes = iter((one, two))
    original_hex = cp._secrets.token_hex
    monkeypatch.setattr(cp._secrets, "token_hex", lambda size=32: next(suffixes) if size == 8 else original_hex(size))
    cache, remote_jobs, posts = _stateful_provider(monkeypatch, library)
    first = _enqueue(client, payload, "cache-prior-failed")
    assert cp._load_jobs()["jobs"][first]["slideshowPlan"][0]["signature"] == signature
    await cp._run_syzygy_slideshow(first)
    failed = cp._load_jobs()["jobs"][first]
    assert failed["status"] == "failed" and "Syzygy render failed" in failed["error"]
    assert len(posts) == len(cache) == 1 and list(remote_jobs.values()) == ["failed"]
    before = cp._jobs_path().read_bytes()
    assert _enqueue(client, payload, "cache-prior-failed") == first
    assert cp._jobs_path().read_bytes() == before
    assert started == [first] and len(posts) == 1
    second = _enqueue(client, payload, "cache-explicit-new-job")
    assert second != first and started == [first, second]
    assert cp._load_jobs()["jobs"][second]["slideshowPlan"][0]["signature"] == signature
    await cp._run_syzygy_slideshow(second)
    complete = cp._load_jobs()["jobs"][second]
    assert complete["status"] == "completed", complete.get("error")
    assert len(complete["clips"]) == 1
    assert len(posts) == len(cache) == 2 and posts[0][0] != posts[1][0]
    assert posts[0][1] == posts[1][1], "same real plan/payload: only request identity differs"
    assert cp._load_jobs()["jobs"][first] == failed


@pytest.mark.asyncio
async def test_explicit_ambiguous_same_job_redispatch_reuses_remote_key(lab, monkeypatch):
    client, _root, started, library, payload = lab
    cache, remote_jobs, posts = _stateful_provider(monkeypatch, library, ambiguous=True)
    job_id = _enqueue(client, payload, "cache-ambiguous-same-job")
    await cp._run_syzygy_slideshow(job_id)
    assert cp._load_jobs()["jobs"][job_id]["status"] == "failed"
    assert len(posts) == len(cache) == 1 and list(remote_jobs.values()) == ["done"]
    assert _enqueue(client, payload, "cache-ambiguous-same-job") == job_id
    assert started == [job_id] and len(posts) == 1
    # Explicit duplicate worker dispatch, not a claim of automatic API resume.
    await cp._run_syzygy_slideshow(job_id)
    complete = cp._load_jobs()["jobs"][job_id]
    assert complete["status"] == "completed", complete.get("error")
    assert len(complete["clips"]) == 1
    assert len(posts) == 2 and len(cache) == 1
    assert posts[0] == posts[1], "ambiguous retry keeps key and exact payload"
