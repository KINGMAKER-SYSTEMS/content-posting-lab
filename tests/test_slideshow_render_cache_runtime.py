"""Actual enqueue/runner/adapter contract against a synthetic stateful provider.

No production provider incident is reproduced. Network and local encoding alone
are replaced. Duplicate dispatch is explicit; no automatic retry is asserted.
"""
import copy
import hashlib
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


def _stateful_provider(monkeypatch, library, *, ambiguous=False, traffic=None):
    source_bytes = {item.key: b"x" * item.bytes for item in library.objects}
    cache, jobs, posts = {}, {}, []
    lost = False

    def respond(request):
        nonlocal lost
        path = request.url.path
        if traffic is not None:
            traffic.append((request.method, request.url.host, path))
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
async def test_ambiguous_failed_job_replay_and_duplicate_runner_stay_terminal(lab, monkeypatch):
    client, _root, started, library, payload = lab
    traffic = []
    cache, remote_jobs, posts = _stateful_provider(
        monkeypatch, library, ambiguous=True, traffic=traffic,
    )
    job_id = _enqueue(client, payload, "cache-ambiguous-same-job")
    await cp._run_syzygy_slideshow(job_id)
    failed = cp._load_jobs()["jobs"][job_id]
    assert failed["status"] == "failed" and failed["clips"] == []
    assert len(posts) == len(cache) == 1 and list(remote_jobs.values()) == ["done"]
    private_before = cp._jobs_path().read_bytes()
    failed_before = copy.deepcopy(failed)
    posts_before, cache_before = copy.deepcopy(posts), dict(cache)
    remote_before, traffic_before = dict(remote_jobs), list(traffic)
    artifact_root = Path(failed["artifactRoot"])

    def artifact_files():
        return {str(path.relative_to(artifact_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in artifact_root.rglob("*") if path.is_file()}

    files_before = artifact_files()
    artifacts_before = client.get(f"/api/control-plane/v1/jobs/{job_id}/artifacts", headers=headers())
    assert artifacts_before.status_code == 200 and artifacts_before.json()["artifacts"] == []
    assert cp._jobs_path().read_bytes() == private_before
    assert _enqueue(client, payload, "cache-ambiguous-same-job") == job_id
    assert started == [job_id]
    # This is a terminal-runner refusal, not an automatic remote-job resume.
    await cp._run_syzygy_slideshow(job_id)
    artifacts_after = client.get(f"/api/control-plane/v1/jobs/{job_id}/artifacts", headers=headers())
    assert artifacts_after.status_code == 200
    assert artifacts_after.json() == artifacts_before.json()
    assert cp._load_jobs()["jobs"][job_id] == failed_before
    assert cp._jobs_path().read_bytes() == private_before
    assert posts == posts_before and cache == cache_before
    assert remote_jobs == remote_before and traffic == traffic_before
    assert artifact_files() == files_before


@pytest.mark.asyncio
async def test_explicit_active_job_adapter_retry_reuses_remote_key_and_body(lab, monkeypatch):
    """Adapter identity only: direct calls do not dispatch or resume a runner."""
    client, _root, started, library, payload = lab
    traffic = []
    cache, remote_jobs, posts = _stateful_provider(
        monkeypatch, library, ambiguous=True, traffic=traffic,
    )
    job_id = _enqueue(client, payload, "cache-active-adapter-identity")
    job = cp._load_jobs()["jobs"][job_id]
    assert job["status"] == "queued" and cp._has_slideshow_runner_claim(job)
    plan = job["slideshowPlan"][0]
    plan_before = copy.deepcopy(plan)
    recipe = slides.resolve_slideshow_recipe(payload)
    assert recipe is not None
    identity = dict(page_id=job["pageId"], recipe_version=job["recipeVersion"],
                    format_slug=recipe.format_slug, lab_job_id=job_id)
    private_before = cp._jobs_path().read_bytes()
    with pytest.raises(slides.SyzygyUnavailable, match="Syzygy request failed"):
        await slides.submit_syzygy_render(plan, **identity)
    assert len(posts) == len(cache) == 1 and list(remote_jobs.values()) == ["done"]
    remote, quality = await slides.submit_syzygy_render(plan, **identity)
    assert quality == "full" and remote == next(iter(cache.values()))
    assert len(posts) == 2 and len(cache) == len(remote_jobs) == 1
    assert posts[0] == posts[1], "same active-job adapter identity keeps key and exact payload"
    assert traffic == [("POST", "syzygy.example", "/api/v1/render")] * 2
    assert plan == plan_before
    assert cp._jobs_path().read_bytes() == private_before
    assert cp._load_jobs()["jobs"][job_id] == job
    assert started == [job_id]  # only the original enqueue, no runner execution
