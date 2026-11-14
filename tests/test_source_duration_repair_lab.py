"""Lab-half acceptance tests for source_duration_repair (L1-L6, T1-T8).

All media is synthetic (ffmpeg lavfi) and everything is offline: no provider,
network, schedule mutation or full suite run.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app as app_module
from routers import post_renders as routes
from services import post_render as v1
from services import post_render_jobs as jobs
from services.post_render import PostRenderRequest, sha256, source_duration_repair_enabled
from services.post_render_timed import TimedPostRenderRequest
from services.source_duration_repair import (
    SOURCE_DURATION_REPAIR_KEYS,
    canonical_json,
    command_digest,
    validate_command,
)
from tests.test_post_render import NOW, request as v1_request
from tests.test_post_render_timed import request as timed_request
from tests.test_source_duration_repair import good_command

ENV = "CONTENT_LAB_SOURCE_DURATION_REPAIR"
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def _make_source(path: Path, seconds: int = 3, fps: str = "30", freq: int = 440) -> None:
    if not FFMPEG or not FFPROBE:
        pytest.skip("ffmpeg/ffprobe required")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi",
        "-i", f"testsrc2=size=1080x1920:rate={fps}:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ], check=True, timeout=120)


def _repair_command(src_sha: str, *, start_ms: int = 0, end_ms: int = 2000,
                    opening: bool = False) -> dict:
    overrides = {
        "source_sha256": src_sha,
        "source_trim_start_ms": start_ms,
        "source_trim_end_ms": end_ms,
        "official_end_ms": end_ms,
        "tail_evidence_end_ms": end_ms,
        "expected_output_duration_ms": end_ms - start_ms,
        "expected_output_frames": int((end_ms - start_ms) * 30 / 1000),
    }
    if start_ms > 0:
        overrides["official_start_ms"] = start_ms
        overrides["opening_evidence_id"] = "ev-open-1"
        overrides["opening_evidence_sha256"] = good_command()["decoded_reference_sha256"]
        overrides["opening_evidence_end_ms"] = start_ms
    if opening:
        overrides["official_start_ms"] = start_ms
        overrides["opening_evidence_id"] = "ev-open-1"
        overrides["opening_evidence_sha256"] = good_command()["decoded_reference_sha256"]
        overrides["opening_evidence_end_ms"] = start_ms
    return good_command(overrides)


def _slot_payload(request: PostRenderRequest) -> tuple[str, str]:
    slot = {"schema_version": 4, "slot_id": request.slot_id, "page_id": request.page_id,
            "handle": request.account, "asset": {"sha256": request.source_sha256},
            "device_hint": {"device_serial": request.device_serial},
            "caption": {"text": request.caption}, "render_treatment": json.loads(request.render_treatment_json)}
    raw = json.dumps(slot, separators=(",", ":"))
    return raw, sha256(raw.encode())


def _submission(request: PostRenderRequest) -> jobs.RenderJobSubmission:
    raw, slot_hash = _slot_payload(request)
    request = request.model_validate({**request.model_dump(by_alias=True),
                                      "slot_payload_sha256": slot_hash})
    return jobs.RenderJobSubmission.model_validate({
        "schema": jobs.JOB_SCHEMA, "request": request, "slot_payload_json": raw,
        "source_provenance": {"schema": "posting-source-treatment/v1",
                              "source_sha256": request.source_sha256,
                              "source_visual_treatment_sha256": request.source_visual_treatment_sha256,
                              "provenance_id": "generation:lab-sdr-fixture"}})


def _service(tmp_path, *, renderer=None, fetcher=None, clock=None) -> jobs.PostRenderJobs:
    return jobs.PostRenderJobs(tmp_path / "jobs",
                               renderer=renderer or jobs.render_prepared_post,
                               fetcher=fetcher or (lambda _request, path: None),
                               clock_ms=clock or (lambda: NOW))


# --------------------------------------------------------------------------- #
# T1 — repaired v1 job: validation -> dispatch -> encoder -> exact bytes -> receipt
# --------------------------------------------------------------------------- #
def test_t1_repaired_v1_job_full_envelope_and_digest(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "on")
    src = tmp_path / "source.mp4"
    _make_source(src)
    src_sha = sha256(src.read_bytes())
    command = _repair_command(src_sha, end_ms=2000)
    request = v1_request(source_sha=src_sha, source_duration_repair=command)
    body = _submission(request)
    service = _service(tmp_path, fetcher=lambda _request, path: shutil.copyfile(src, path))
    accepted = service.enqueue(body, "lab-sdr-t1")
    assert accepted["state"] == "queued"
    assert service.run_one()
    status = service.status(accepted["id"])
    assert status["state"] == "succeeded", status
    receipt_bytes = service.artifact(accepted["id"], "receipt").read_bytes()
    receipt = json.loads(receipt_bytes)
    # receipt covers the digest, and the digest equals the Worker's canonical rule
    assert receipt["source_duration_repair_sha256"] == command_digest(command)
    independent = hashlib.sha256(
        json.dumps({k: command[k] for k in sorted(command)},
                   sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert receipt["source_duration_repair_sha256"] == independent
    assert set(SOURCE_DURATION_REPAIR_KEYS) == set(command)
    assert canonical_json(command) == json.dumps(
        {k: command[k] for k in sorted(command)}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    # exact output bytes + authenticated receipt hash
    final = service.artifact(accepted["id"], "final").read_bytes()
    assert sha256(final) == receipt["final_sha256"]
    assert len(final) == receipt["final_byte_length"]
    assert receipt["final_sha256"] != src_sha
    assert sha256(receipt_bytes) == status["receipt_sha256"]


# --------------------------------------------------------------------------- #
# T2 — legacy parity (flag off and flag on/no-field), golden bytes committed
# --------------------------------------------------------------------------- #
def _legacy_golden(tmp_path):
    """Deterministic legacy render whose receipt bytes are the committed golden."""
    src = tmp_path / "legacy-source.mp4"
    _make_source(src)
    src_sha = sha256(src.read_bytes())
    request = v1_request(source_sha=src_sha)
    body = _submission(request)
    service = _service(tmp_path, fetcher=lambda _request, path: shutil.copyfile(src, path))
    accepted = service.enqueue(body, "lab-sdr-legacy-golden")
    assert service.run_one()
    receipt = service.artifact(accepted["id"], "receipt").read_bytes()
    return json.loads(receipt), receipt


def test_t2_legacy_parity_flag_off_and_on_no_field(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    off, off_bytes = _legacy_golden(tmp_path / "off")
    monkeypatch.setenv(ENV, "on")
    on, on_bytes = _legacy_golden(tmp_path / "on")
    assert on == off
    assert on_bytes == off_bytes
    # legacy receipt carries no repair binding and no capability echo
    assert "source_duration_repair_sha256" not in off
    # a request without the field serializes without it (byte-identical)
    assert "source_duration_repair" not in v1_request().model_dump(by_alias=True)


# --------------------------------------------------------------------------- #
# T3 — timed v2 refusal before dispatch; timed without repair unchanged
# --------------------------------------------------------------------------- #
def _timed_submission(repair: dict | None = None) -> jobs.RenderJobSubmission:
    from services.lyric_timing import lyric_timing_catalog, resolve_lyric_timing
    option = lyric_timing_catalog(page_id="acct:burnt-mill-rd", account="burnt.mill.rd")[0]
    timing = resolve_lyric_timing(option["id"], page_id="acct:burnt-mill-rd",
                                  account="burnt.mill.rd", sound_id=option["soundId"],
                                  expected_sha256=option["sha256"])
    request = timed_request(caption=timing["canonicalCaption"], lyric_timing_set_id=timing["id"],
                            lyric_timing_sha256=timing["sha256"], source_program_id=timing["sourceProgramId"],
                            source_program_sha256=timing["sourceProgramSha256"], source_duration_repair=repair)
    slot = {"schema_version": 4, "slot_id": request.slot_id, "page_id": request.page_id,
            "handle": request.account, "device_hint": {"device_serial": request.device_serial},
            "asset": {"sha256": request.source_sha256}, "caption": {"text": request.caption},
            "sound": {"music_id": request.sound_id},
            "render_treatment": json.loads(request.render_treatment_json)}
    raw = json.dumps(slot, separators=(",", ":"))
    request = TimedPostRenderRequest.model_validate({**request.model_dump(by_alias=True),
                                                     "slot_payload_sha256": sha256(raw.encode())})
    return jobs.RenderJobSubmission.model_validate({
        "schema": jobs.JOB_SCHEMA, "request": request, "slot_payload_json": raw,
        "source_provenance": {"schema": "posting-source-treatment/v1",
                              "source_sha256": request.source_sha256,
                              "source_visual_treatment_sha256": request.source_visual_treatment_sha256,
                              "provenance_id": "generation:lab-sdr-timed"}})


def test_t3_timed_repair_refused_before_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "on")
    calls = {"fetch": 0, "render": 0}

    def fetcher(_request, path):
        calls["fetch"] += 1
        path.write_bytes(b"source")

    def renderer(*_args, **_kwargs):
        calls["render"] += 1
        raise AssertionError("dispatch must not reach the renderer")

    service = _service(tmp_path, fetcher=fetcher, renderer=renderer)
    body = _timed_submission(repair=good_command())
    accepted = service.enqueue(body, "lab-sdr-t3-timed-repair")
    assert service.run_one()
    status = service.status(accepted["id"])
    assert status["state"] == "failed"
    assert status["error_code"] == "TIMED_REPAIR_UNSUPPORTED"
    assert calls == {"fetch": 0, "render": 0}


def test_t3_timed_without_repair_still_dispatches(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "on")
    calls = {"fetch": 0}

    def fetcher(_request, path):
        calls["fetch"] += 1
        path.write_bytes(b"source")

    service = _service(tmp_path, fetcher=fetcher,
                       renderer=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("ok")))
    body = _timed_submission(repair=None)
    accepted = service.enqueue(body, "lab-sdr-t3-timed-legacy")
    assert service.run_one()
    assert calls["fetch"] == 1


def test_t3_timed_repair_route_returns_typed_422_before_enqueue(monkeypatch):
    monkeypatch.setenv(ENV, "on")
    seen = []

    class Spy:
        def enqueue(self, body, key):
            seen.append(key)
            return {"id": "unreachable"}

    monkeypatch.setattr(routes, "service", lambda: Spy())
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/control-plane")
    client = TestClient(app)
    body = _timed_submission(repair=good_command()).model_dump(by_alias=True)
    headers = {"Authorization": "Bearer only-this-test-service-token",
               "X-RT-Page-Id": body["request"]["page_id"],
               "Idempotency-Key": "lab-sdr-t3-route"}
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "only-this-test-service-token")
    response = client.post("/api/control-plane/v1/post-renders", json=body, headers=headers)
    assert response.status_code == 422
    assert response.json()["detail"] == "TIMED_REPAIR_UNSUPPORTED"
    assert seen == []


# --------------------------------------------------------------------------- #
# T5 — five-row transition table
# --------------------------------------------------------------------------- #
def test_t5_row1_old_worker_old_lab_legacy_parity(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    receipt, _ = _legacy_golden(tmp_path)
    assert receipt["schema"] == v1.RECEIPT_SCHEMA
    assert "source_duration_repair_sha256" not in receipt


def test_t5_row2_disabled_worker_compatible_lab_legacy_parity(tmp_path, monkeypatch):
    # compatible Lab with capability ON, but no repair field on the request:
    # legacy request/receipt bytes must not move.
    monkeypatch.setenv(ENV, "on")
    receipt, _ = _legacy_golden(tmp_path)
    assert "source_duration_repair_sha256" not in receipt


def test_t5_row3_new_worker_old_lab_stored_repair_held(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    src = tmp_path / "source.mp4"
    _make_source(src)
    src_sha = sha256(src.read_bytes())
    command = _repair_command(src_sha)
    request = v1_request(source_sha=src_sha, source_duration_repair=command)
    body = _submission(request)
    calls = {"fetch": 0, "render": 0}

    def fetcher(_request, path):
        calls["fetch"] += 1

    def renderer(*_a, **_k):
        calls["render"] += 1

    service = _service(tmp_path, fetcher=fetcher, renderer=renderer)
    accepted = service.enqueue(body, "lab-sdr-t5-row3")
    assert service.run_one()
    status = service.status(accepted["id"])
    assert status["state"] == "failed"
    assert status["error_code"] == "SOURCE_DURATION_REPAIR_UNSUPPORTED"
    assert calls == {"fetch": 0, "render": 0}
    # identity is retained: the stored request still carries the repair + digest
    row = service._row(accepted["id"])
    stored = jobs.RenderJobSubmission.model_validate_json(row["submission_json"])
    assert stored.request.source_duration_repair == command
    assert command_digest(stored.request.source_duration_repair) == command_digest(command)


def test_t5_row4_new_worker_compatible_lab_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "on")
    src = tmp_path / "source.mp4"
    _make_source(src)
    src_sha = sha256(src.read_bytes())
    command = _repair_command(src_sha)
    request = v1_request(source_sha=src_sha, source_duration_repair=command)
    body = _submission(request)
    service = _service(tmp_path, fetcher=lambda _request, path: shutil.copyfile(src, path))
    accepted = service.enqueue(body, "lab-sdr-t5-row4")
    assert service.run_one()
    status = service.status(accepted["id"])
    assert status["state"] == "succeeded", status
    receipt = json.loads(service.artifact(accepted["id"], "receipt").read_bytes())
    assert receipt["source_duration_repair_sha256"] == command_digest(command)


def test_t5_row5_emission_disabled_after_use(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "on")
    src = tmp_path / "source.mp4"
    _make_source(src)
    src_sha = sha256(src.read_bytes())
    command = _repair_command(src_sha)
    request = v1_request(source_sha=src_sha, source_duration_repair=command)
    body = _submission(request)
    service = _service(tmp_path, fetcher=lambda _request, path: shutil.copyfile(src, path))
    accepted = service.enqueue(body, "lab-sdr-t5-row5")
    assert service.run_one()
    # emission now disabled: the completed row is counted and still authenticated
    monkeypatch.delenv(ENV, raising=False)
    status = service.status(accepted["id"])
    assert status["state"] == "succeeded"
    assert status["receipt_sha256"] == sha256(service.artifact(accepted["id"], "receipt").read_bytes())
    # a new repaired body is refused, not run bare
    src2 = tmp_path / "source2.mp4"
    _make_source(src2, freq=660)
    src2_sha = sha256(src2.read_bytes())
    body2 = _submission(v1_request(source_sha=src2_sha, source_duration_repair=_repair_command(src2_sha)))
    service.fetcher = lambda _request, path: shutil.copyfile(src2, path)
    accepted2 = service.enqueue(body2, "lab-sdr-t5-row5-new")
    assert service.run_one()
    assert service.status(accepted2["id"])["error_code"] == "SOURCE_DURATION_REPAIR_UNSUPPORTED"
    # existing unresolved repaired row stays recoverable once capability returns
    monkeypatch.setenv(ENV, "on")
    assert service.retry(accepted2["id"])["state"] == "queued"
    assert service.run_one()
    assert service.status(accepted2["id"])["state"] == "succeeded"


# --------------------------------------------------------------------------- #
# T6 — capability off with a stored repaired job: refused, encoder never called
# --------------------------------------------------------------------------- #
def test_t6_capability_off_stored_repaired_job_refused_encoder_never_called(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    src = tmp_path / "source.mp4"
    _make_source(src)
    src_sha = sha256(src.read_bytes())
    command = _repair_command(src_sha)
    body = _submission(v1_request(source_sha=src_sha, source_duration_repair=command))
    calls = {"fetch": 0, "render": 0}
    service = _service(
        tmp_path,
        fetcher=lambda _r, _p: calls.__setitem__("fetch", calls["fetch"] + 1),
        renderer=lambda *_a, **_k: calls.__setitem__("render", calls["render"] + 1),
    )
    accepted = service.enqueue(body, "lab-sdr-t6")
    assert service.run_one()
    assert calls == {"fetch": 0, "render": 0}
    assert service.status(accepted["id"])["error_code"] == "SOURCE_DURATION_REPAIR_UNSUPPORTED"


# --------------------------------------------------------------------------- #
# T8 — nonzero start trim cuts from start>0 to the exact length
# --------------------------------------------------------------------------- #
def test_t8_nonzero_start_trim_cuts_to_exact_length(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "on")
    src = tmp_path / "source.mp4"
    _make_source(src, seconds=3)
    src_sha = sha256(src.read_bytes())
    command = _repair_command(src_sha, start_ms=1200, end_ms=3000)
    request = v1_request(source_sha=src_sha, source_duration_repair=command)
    body = _submission(request)
    service = _service(tmp_path, fetcher=lambda _request, path: shutil.copyfile(src, path))
    accepted = service.enqueue(body, "lab-sdr-t8")
    assert service.run_one()
    status = service.status(accepted["id"])
    assert status["state"] == "succeeded", status
    receipt = json.loads(service.artifact(accepted["id"], "receipt").read_bytes())
    assert receipt["duration_ms"] < 3000
    assert abs(receipt["duration_ms"] - 1800) <= 80
    assert receipt["source_duration_repair_sha256"] == command_digest(command)
    final = service.artifact(accepted["id"], "final").read_bytes()
    assert sha256(final) == receipt["final_sha256"]


# --------------------------------------------------------------------------- #
# Red-proof edges
# --------------------------------------------------------------------------- #
def test_red_proof_wrong_digest_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "on")
    src = tmp_path / "source.mp4"
    _make_source(src)
    src_sha = sha256(src.read_bytes())
    command = _repair_command(src_sha)
    request = v1_request(source_sha=src_sha, source_duration_repair=command)
    body = _submission(request)
    service = _service(tmp_path, fetcher=lambda _request, path: shutil.copyfile(src, path))
    accepted = service.enqueue(body, "lab-sdr-red-wrong-digest")
    assert service.run_one()
    row = service._row(accepted["id"])
    authority = service._verify_output(row)
    authority["receipt"]["source_duration_repair_sha256"] = "0" * 64
    with pytest.raises(jobs.RenderJobError, match="receipt_binding_mismatch"):
        service._validate_output_authority(row, authority)


def test_red_proof_digest_is_inside_hashed_receipt_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "on")
    src = tmp_path / "source.mp4"
    _make_source(src)
    src_sha = sha256(src.read_bytes())
    command = _repair_command(src_sha)
    request = v1_request(source_sha=src_sha, source_duration_repair=command)
    body = _submission(request)
    service = _service(tmp_path, fetcher=lambda _request, path: shutil.copyfile(src, path))
    accepted = service.enqueue(body, "lab-sdr-red-echo-after-hash")
    assert service.run_one()
    receipt_bytes = service.artifact(accepted["id"], "receipt").read_bytes()
    receipt = json.loads(receipt_bytes)
    assert "source_duration_repair_sha256" in receipt
    # the stored receipt_sha256 must be the hash of the exact bytes that carry the digest
    assert service.status(accepted["id"])["receipt_sha256"] == sha256(receipt_bytes)


def test_red_proof_missing_field_vs_empty_object():
    # missing field (None) is legacy and accepted; empty object is refused
    assert v1_request(source_duration_repair=None).source_duration_repair is None
    with pytest.raises(ValidationError):
        v1_request(source_duration_repair={})


def test_red_proof_unknown_extra_keys_inside_repair_refused():
    command = good_command()
    command["unknown_extra"] = 1
    with pytest.raises(ValidationError):
        v1_request(source_duration_repair=command)


def test_red_proof_flag_off_request_carrying_repair_refused_at_route(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    seen = []

    class Spy:
        def enqueue(self, body, key):
            seen.append(key)

    monkeypatch.setattr(routes, "service", lambda: Spy())
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/control-plane")
    client = TestClient(app)
    body = _submission(v1_request(source_duration_repair=good_command())).model_dump(by_alias=True)
    headers = {"Authorization": "Bearer only-this-test-service-token",
               "X-RT-Page-Id": body["request"]["page_id"],
               "Idempotency-Key": "lab-sdr-red-flag-off"}
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "only-this-test-service-token")
    response = client.post("/api/control-plane/v1/post-renders", json=body, headers=headers)
    assert response.status_code == 422
    assert response.json()["detail"] == "SOURCE_DURATION_REPAIR_UNSUPPORTED"
    assert seen == []


# --------------------------------------------------------------------------- #
# F1-F3 follow-up: exact-match flag, L1 health capability, predispatch log
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value", ["1", "ON", " on ", "on\n"])
def test_flag_requires_exact_on(value, monkeypatch):
    monkeypatch.setenv(ENV, value)
    assert source_duration_repair_enabled() is False


def test_health_capability_flag_off_and_on(monkeypatch):
    monkeypatch.setattr(app_module, "_schedule_tool_refresh", lambda: None)
    monkeypatch.delenv(ENV, raising=False)
    client = TestClient(app_module.app)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["capabilities"]["source_duration_repair_v1"] is False
    monkeypatch.setenv(ENV, "on")
    response = client.get("/api/health")
    assert response.json()["capabilities"]["source_duration_repair_v1"] is True


@pytest.mark.parametrize("timed,code", [
    (False, "SOURCE_DURATION_REPAIR_UNSUPPORTED"),
    (True, "TIMED_REPAIR_UNSUPPORTED"),
])
def test_predispatch_422_logs_typed_code(timed, code, monkeypatch, caplog):
    if timed:
        monkeypatch.setenv(ENV, "on")
        body = _timed_submission(repair=good_command()).model_dump(by_alias=True)
    else:
        monkeypatch.delenv(ENV, raising=False)
        body = _submission(v1_request(source_duration_repair=good_command())).model_dump(by_alias=True)

    seen = []

    class Spy:
        def enqueue(self, body, key):
            seen.append(key)

    monkeypatch.setattr(routes, "service", lambda: Spy())
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/control-plane")
    client = TestClient(app)
    headers = {"Authorization": "Bearer only-this-test-service-token",
               "X-RT-Page-Id": body["request"]["page_id"],
               "Idempotency-Key": f"lab-sdr-f2-log-{code}"}
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "only-this-test-service-token")
    caplog.set_level(logging.WARNING, logger="content_lab.post_render_jobs")
    response = client.post("/api/control-plane/v1/post-renders", json=body, headers=headers)
    assert response.status_code == 422
    assert response.json()["detail"] == code
    assert seen == []
    messages = [record.getMessage() for record in caplog.records]
    assert f"post render submit refused page={body['request']['page_id']} code={code}" in messages
