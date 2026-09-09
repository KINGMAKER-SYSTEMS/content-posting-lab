import concurrent.futures
import json
import threading
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from routers import post_renders as routes
from services import post_render_jobs as jobs
from services.post_render import PostRenderRequest, sha256
from tests.test_post_render import NOW, actual_source, request as render_request


def submission(*, slot_id="slot:fixture-a", source=b"source", program_id="playlist:fixture"):
    request = render_request(sha256(source), slot_id=slot_id, program_id=program_id)
    slot = {"schema_version": 4, "slot_id": request.slot_id, "page_id": request.page_id,
            "handle": request.account, "asset": {"sha256": request.source_sha256},
            "device_hint": {"device_serial": request.device_serial},
            "caption": {"text": request.caption}, "render_treatment": json.loads(request.render_treatment_json)}
    raw = json.dumps(slot, separators=(",", ":"))
    request = PostRenderRequest.model_validate({**request.model_dump(by_alias=True), "slot_payload_sha256": sha256(raw.encode())})
    return jobs.RenderJobSubmission.model_validate({"schema": jobs.JOB_SCHEMA, "request": request,
        "slot_payload_json": raw, "source_provenance": {"schema": "posting-source-treatment/v1",
        "source_sha256": request.source_sha256, "source_visual_treatment_sha256": request.source_visual_treatment_sha256,
        "provenance_id": "generation:explicit-fixture"}})


def fake_render(source, output, request, *, clock_ms):
    output.mkdir()
    final, qa = b"final:" + request.caption.encode(), b"qa frame"
    (output / "final.mp4").write_bytes(final)
    (output / "qa.jpg").write_bytes(qa)
    receipt = {key: getattr(request, key) for key in ("slot_id", "slot_payload_sha256", "page_id", "program_id",
        "device_serial", "account", "source_sha256", "caption_sha256", "treatment_sha256", "renderer_id", "renderer_version")}
    receipt.update(schema="posting-prepared-artifact/v1", final_sha256=sha256(final), final_byte_length=len(final),
                   qa_frame_sha256=sha256(qa), final_object_key=f"posting/final/{sha256(final)}.mp4", mime_type="video/mp4",
                   width=1080, height=1920, duration_ms=1000, completed_at_ms=clock_ms())
    (output / "receipt.json").write_text(json.dumps(receipt, separators=(",", ":")))
    (output / "decode.json").write_text(json.dumps({"final_sha256": sha256(final), "qa_frame_sha256": sha256(qa),
        "decoded_video_frames": 30, "final_probe": {"duration_ms": 1000}}))


def service(tmp_path, *, renderer=fake_render, fetcher=None, clock=None):
    return jobs.PostRenderJobs(tmp_path, renderer=renderer,
        fetcher=fetcher or (lambda _request, path: path.write_bytes(b"source")), clock_ms=clock or (lambda: NOW))


def test_enqueue_survives_new_instance_and_duplicate_requests_are_one_job(tmp_path):
    first = service(tmp_path)
    payload = submission()
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: first.enqueue(payload, "same-request"), range(8)))
    assert len({result["id"] for result in results}) == 1
    second = service(tmp_path)
    assert second.status(results[0]["id"])["state"] == "queued"
    assert second.run_one()
    assert second.status(results[0]["id"])["state"] == "succeeded"
    assert not second.run_one()
    assert first.enqueue(payload, "different-key")["id"] == results[0]["id"]
    with pytest.raises(jobs.RenderJobError) as error:
        first.enqueue(submission(program_id="playlist:changed"), "same-request")
    assert error.value.code == "idempotency_conflict"
    with pytest.raises(jobs.RenderJobError):
        first.enqueue(submission(slot_id="slot:other"), "different-key")


class Crash(BaseException):
    pass


def test_restart_recovers_completed_uncommitted_result_without_rendering_twice(tmp_path, monkeypatch):
    calls = []
    def renderer(*args, **kwargs):
        calls.append(1)
        return fake_render(*args, **kwargs)
    first = service(tmp_path, renderer=renderer)
    job = first.enqueue(submission(), "crash-after-output")
    monkeypatch.setattr(first, "_finish", lambda *_: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        first.run_one()
    assert first.status(job["id"])["state"] == "running"
    second = service(tmp_path, renderer=renderer)
    assert second.run_one()
    assert second.status(job["id"])["state"] == "succeeded"
    assert second.status(job["id"])["attempts"] == 1
    assert calls == [1]


def test_restart_fences_incomplete_attempt_and_never_serves_partial_bytes(tmp_path):
    def interrupted(_source, output, _request, **_kwargs):
        output.mkdir()
        (output / "final.mp4").write_bytes(b"partial")
        raise Crash()
    first = service(tmp_path, renderer=interrupted)
    job = first.enqueue(submission(), "crash-partial-output")
    with pytest.raises(Crash):
        first.run_one()
    old = first._row(job["id"])
    with pytest.raises(jobs.RenderJobError):
        first.artifact(job["id"], "final")
    second = service(tmp_path)
    assert second.run_one()
    current = second._row(job["id"])
    assert current["attempt_id"] != old["attempt_id"]
    assert current["attempts"] == 2
    assert second.artifact(job["id"], "final").read_bytes().startswith(b"final:")
    assert first._output(old).joinpath("final.mp4").read_bytes() == b"partial"


def test_busy_job_does_not_block_another_and_cannot_be_claimed_twice(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def renderer(source, output, request, **kwargs):
        if request.slot_id == "slot:slow":
            entered.set()
            assert release.wait(5)
        fake_render(source, output, request, **kwargs)
    first = service(tmp_path, renderer=renderer)
    slow = first.enqueue(submission(slot_id="slot:slow"), "slow-request")
    fast = first.enqueue(submission(slot_id="slot:fast"), "fast-request")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first.run_one)
        assert entered.wait(2)
        second = service(tmp_path, renderer=renderer)
        assert second.run_one()
        assert second.status(fast["id"])["state"] == "succeeded"
        assert second.status(slow["id"])["attempts"] == 1
        release.set()
        assert future.result(timeout=2)


def test_transient_retries_back_off_and_stop_after_three_attempts(tmp_path):
    now = [NOW]
    def unavailable(_request, _path):
        raise jobs.RenderJobError("source_unavailable")
    worker = service(tmp_path, fetcher=unavailable, clock=lambda: now[0])
    job = worker.enqueue(submission(), "bounded-retry")
    for attempt in range(1, 4):
        assert worker.run_one()
        state = worker.status(job["id"])
        assert state["attempts"] == attempt
        assert state["state"] == ("failed" if attempt == 3 else "queued")
        assert not worker.run_one()
        now[0] += 60_000
    assert not worker.status(job["id"])["retryable"]
    with pytest.raises(jobs.RenderJobError):
        worker.retry(job["id"])


@pytest.mark.parametrize("change", ["wrong_source", "wrong_treatment", "slot_binding", "malformed_slot"])
def test_missing_or_rebound_provenance_and_slot_are_rejected(change):
    payload = submission().model_dump(by_alias=True)
    if change == "missing":
        del payload["source_provenance"]
    elif change == "wrong_source":
        payload["source_provenance"]["source_sha256"] = "0" * 64
    elif change == "wrong_treatment":
        payload["source_provenance"]["source_visual_treatment_sha256"] = "0" * 64
    elif change == "slot_binding":
        payload["request"]["account"] = "another.account"
    else:
        payload["slot_payload_json"] = "[]"
        payload["request"]["slot_payload_sha256"] = sha256(b"[]")
    with pytest.raises(ValidationError):
        jobs.RenderJobSubmission.model_validate(payload)


def client_for(worker, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "only-this-test-service-token")
    monkeypatch.setattr(routes, "service", lambda: worker)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/control-plane")
    return TestClient(app)


def test_all_job_and_artifact_routes_require_auth_and_preserve_readiness(tmp_path, monkeypatch):
    worker = service(tmp_path)
    client = client_for(worker, monkeypatch)
    path = "/api/control-plane/v1/post-renders"
    payload = submission().model_dump(by_alias=True)
    assert client.post(path, json=payload).status_code == 401
    headers = {"Authorization": "Bearer only-this-test-service-token", "X-RT-Page-Id": "page-a", "Idempotency-Key": "api-request"}
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code == 202
    job = response.json()
    wrong_page = {**headers, "X-RT-Page-Id": "other-page"}
    assert client.get(path + "/" + job["id"], headers=wrong_page).status_code == 404
    assert client.get(path + "/" + job["id"] + "/artifacts/final", headers=wrong_page).status_code == 404
    for url in (path + "/" + job["id"], path + "/" + job["id"] + "/artifacts/final"):
        assert client.get(url).status_code == 401
    assert client.post(path + "/" + job["id"] + "/retry").status_code == 401
    final_url = path + "/" + job["id"] + "/artifacts/final"
    assert client.get(final_url, headers=headers).status_code == 409
    worker.run_one()
    final = client.get(final_url, headers=headers)
    assert final.status_code == 200 and final.content.startswith(b"final:")
    assert final.headers["cache-control"] == "no-store"
    worker.artifact(job["id"], "final").write_bytes(b"tampered")
    assert client.get(final_url, headers=headers).status_code == 409
    assert "only-this-test-service-token" not in client.get(path + "/" + job["id"], headers=headers).text


def source_response(data=b"source", **headers):
    values = {"content-type": "video/mp4", "content-length": str(len(data)), "x-source-sha256": sha256(data)}
    values.update(headers)
    return httpx.Response(200, headers=values, stream=httpx.ByteStream(data))


def test_source_fetch_uses_dedicated_machine_origin_and_preserves_page_media_origin(tmp_path, monkeypatch):
    from routers.control_plane import _source_media_origin

    monkeypatch.setenv("CONTENT_LAB_CONTROL_PLANE_ORIGIN", "https://control.risingtidesviral.com")
    monkeypatch.setenv("CONTENT_LAB_POST_RENDER_SOURCE_ORIGIN", "https://content-buckets.risingtidesviral.com/")
    monkeypatch.setenv("CONTROL_PLANE_SERVICE_ID", "service-id")
    monkeypatch.setenv("CONTROL_PLANE_SERVICE_SECRET", "service-secret")
    seen = []
    request = submission().request
    def handle(incoming):
        seen.append(incoming)
        return source_response()
    settings = jobs.SourceSettings.from_environment()
    target = tmp_path / "source.mp4"
    jobs.fetch_source(request, target, settings, transport=httpx.MockTransport(handle))
    assert target.read_bytes() == b"source"
    assert seen[0].url.host == "content-buckets.risingtidesviral.com"
    assert seen[0].url.path.startswith("/api/control-plane/v1/service/posting/v1/artifacts/")
    assert _source_media_origin() == "https://control.risingtidesviral.com"
    assert seen[0].url.params["slot_payload_sha256"] == request.slot_payload_sha256
    assert seen[0].headers["CF-Access-Client-Id"] == "service-id"
    assert seen[0].headers["CF-Access-Client-Secret"] == "service-secret"
    assert "%3A" in seen[0].url.raw_path.decode()
    assert "service-secret" not in repr(settings)


@pytest.mark.parametrize("missing", ["CONTENT_LAB_POST_RENDER_SOURCE_ORIGIN", "CONTROL_PLANE_SERVICE_ID", "CONTROL_PLANE_SERVICE_SECRET"])
def test_source_settings_require_machine_configuration_without_browser_fallback(monkeypatch, missing):
    monkeypatch.setenv("CONTENT_LAB_CONTROL_PLANE_ORIGIN", "https://control.risingtidesviral.com")
    monkeypatch.setenv("CONTENT_LAB_POST_RENDER_SOURCE_ORIGIN", "https://content-buckets.risingtidesviral.com")
    monkeypatch.setenv("CONTROL_PLANE_SERVICE_ID", "service-id")
    monkeypatch.setenv("CONTROL_PLANE_SERVICE_SECRET", "service-secret")
    monkeypatch.delenv(missing)
    with pytest.raises(jobs.RenderJobError, match="source_service_not_configured"):
        jobs.SourceSettings.from_environment()


@pytest.mark.parametrize("kind", ["redirect", "forbidden", "missing", "digest", "length", "encoding"])
def test_source_fetch_refuses_redirects_revocation_and_mismatched_response(tmp_path, kind):
    seen = []
    def handle(incoming):
        seen.append(incoming)
        if kind == "redirect":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
        if kind in {"forbidden", "missing"}:
            return httpx.Response(403 if kind == "forbidden" else 404)
        if kind == "digest":
            return source_response(**{"x-source-sha256": "0" * 64})
        if kind == "length":
            return source_response(**{"content-length": "1"})
        return source_response(**{"content-encoding": "gzip"})
    target = tmp_path / "source.mp4"
    with pytest.raises(jobs.RenderJobError):
        jobs.fetch_source(submission().request, target, jobs.SourceSettings("https://control.example", "id", "secret"), transport=httpx.MockTransport(handle))
    assert len(seen) == 1 and not target.exists()


@pytest.mark.parametrize("origin", ["http://localhost", "https://user:secret@example.com", "https://example.com/path", "https://example.com?url=bad"])
def test_source_origin_is_server_owned_https_only(origin):
    with pytest.raises(jobs.RenderJobError):
        jobs.SourceSettings(origin, "id", "secret")


def test_real_durable_job_produces_and_serves_final_artifact(actual_source, tmp_path, monkeypatch):
    source_bytes = actual_source.read_bytes()
    worker = jobs.PostRenderJobs(tmp_path, fetcher=lambda _request, path: path.write_bytes(source_bytes), clock_ms=lambda: NOW)
    client = client_for(worker, monkeypatch)
    headers = {"Authorization": "Bearer only-this-test-service-token", "X-RT-Page-Id": "page-a", "Idempotency-Key": "real-render-request"}
    response = client.post("/api/control-plane/v1/post-renders", json=submission(source=source_bytes).model_dump(by_alias=True), headers=headers)
    assert response.status_code == 202
    assert worker.run_one()
    job = worker.status(response.json()["id"])
    assert job["state"] == "succeeded"
    final = client.get(job["artifacts"]["final"], headers=headers)
    receipt_response = client.get(job["artifacts"]["receipt"], headers=headers)
    receipt = receipt_response.json()
    assert sha256(final.content) == receipt["final_sha256"]
    assert sha256(receipt_response.content) == job["receipt_sha256"]
    assert (receipt["width"], receipt["height"]) == (1080, 1920)


def test_missing_provenance_is_a_durable_local_regeneration_state(tmp_path):
    payload = submission().model_dump(by_alias=True)
    del payload["source_provenance"]
    request = jobs.RenderJobSubmission.model_validate(payload)
    worker = service(tmp_path, fetcher=lambda *_: pytest.fail("must not fetch without provenance"))
    result = worker.enqueue(request, "missing-provenance")
    assert result["state"] == "regeneration_needed"
    assert result["next_action"] == "regenerate_source_with_treatment_provenance"
    assert result["attempts"] == 0
    assert not worker.run_one()
    assert service(tmp_path).status(result["id"])["state"] == "regeneration_needed"
    healthy = worker.enqueue(submission(slot_id="slot:healthy"), "healthy-provenance")
    worker.fetcher = lambda _request, path: path.write_bytes(b"source")
    assert worker.run_one()
    assert worker.status(healthy["id"])["state"] == "succeeded"


def test_explicit_unlock_releases_ownership_despite_inherited_file_description(tmp_path):
    import os
    import signal
    path = tmp_path / "owner.lock"
    lock = jobs._locked(path)
    assert lock is not None
    ready_read, ready_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(ready_read)
        os.write(ready_write, b"ready")
        while True:
            signal.pause()
    os.close(ready_write)
    try:
        assert os.read(ready_read, 5) == b"ready"
        with lock:
            assert jobs._locked(path) is None
        replacement = jobs._locked(path)
        assert replacement is not None
        with replacement:
            pass
    finally:
        os.close(ready_read)
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)


def test_new_provenance_unblocks_same_slot_and_old_enqueue_replay_preserves_it(tmp_path):
    original_payload = submission().model_dump(by_alias=True)
    original_payload["source_provenance"] = None
    original_payload["request"]["source_visual_treatment_json"] = None
    original_payload["request"]["source_visual_treatment_sha256"] = None
    original = jobs.RenderJobSubmission.model_validate(original_payload)
    worker = service(tmp_path)
    first = worker.enqueue(original, "original-lost-response")
    assert first["state"] == "regeneration_needed"
    refreshed = worker.provide_provenance(first["id"], submission())
    assert refreshed["id"] == first["id"] and refreshed["state"] == "queued"
    replay = worker.enqueue(original, "original-lost-response")
    assert replay["id"] == first["id"] and replay["state"] == "queued"
    current = jobs.RenderJobSubmission.model_validate_json(worker._row(first["id"])["submission_json"])
    assert current.source_provenance is not None
    with pytest.raises(jobs.RenderJobError):
        worker.enqueue(submission(program_id="playlist:other"), "original-lost-response")
    assert worker.run_one()
    assert worker.status(first["id"])["state"] == "succeeded"
    with pytest.raises(jobs.RenderJobError):
        worker.provide_provenance(first["id"], submission(program_id="playlist:other"))
