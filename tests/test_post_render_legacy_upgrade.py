"""Baseline-persisted upgrade fixtures; never encode, fetch, or contact providers."""
import json
import shutil
import statistics
import time
from pathlib import Path

import pytest

from services import post_render_jobs as jobs
from tests.test_post_render_jobs import NOW, service, _write_fake_render
from tests.test_post_render_dual_dedup import dual_render, payload


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("external process or network forbidden")
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)


def legacy(root, *, confirm=True):
    worker = service(root, clock=lambda: NOW + jobs.DELIVERED_RETIRE_AFTER_MS["normal"] + 1)
    fixture = json.loads((Path(__file__).parent / "fixtures/post_render_legacy_clean_success.json").read_text())
    assert fixture["baseline_commit"] == "7cb7a47ea27f5c85c7a500862821acfb945f78c7"
    with worker._db() as db:
        for table in ("jobs", "attempts", "idempotency"):
            rows = fixture["tables"][table]
            for row in rows:
                columns = list(row)
                db.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                           tuple(row[key] for key in columns))
    row = fixture["tables"]["jobs"][0]
    output = worker._output(row)
    output.mkdir(parents=True)
    for name, encoded in fixture["files_hex"].items():
        (output / name).write_bytes(bytes.fromhex(encoded))
    worker.confirm_final = lambda *_: confirm
    worker.fetcher = lambda *_: pytest.fail("legacy job must not fetch")
    worker.renderer = lambda *_: pytest.fail("legacy job must not render")
    return worker, row["id"]


@pytest.mark.parametrize("operation", ["artifact", "ack_retire", "age_out", "tombstone"])
def test_persisted_baseline_upgrade_lifecycle(tmp_path, operation):
    worker, job_id = legacy(tmp_path)
    original = dict(worker._row(job_id))
    assert set(worker.status(job_id)["artifacts"]) == {"final", "qa", "receipt"}
    if operation == "artifact":
        for kind in ("final", "qa", "receipt"):
            assert worker.artifact(job_id, kind).is_file()
        with pytest.raises(jobs.RenderJobError, match="artifact_not_found"):
            worker.artifact(job_id, "burned_final")
    elif operation == "ack_retire":
        worker.acknowledge(job_id)
        worker.retire(job_id)
        assert worker.acknowledge(job_id)["retired_at_ms"] is not None
    elif operation == "age_out":
        assert worker._age_out_delivered()["retired"] == 1
    else:
        # Model a baseline tombstone that has already discarded its media.
        shutil.rmtree(worker._output(original).parent.parent)
        assert worker._verify_output(worker._row(job_id))["final_sha256"] == original["final_sha256"]
        worker.acknowledge(job_id)
    restarted = service(tmp_path)
    row = dict(restarted._row(job_id))
    assert row["submission_json"] == original["submission_json"]
    assert row["output_authority_json"] == original["output_authority_json"]
    submission = jobs.RenderJobSubmission.model_validate_json(row["submission_json"])
    assert restarted.enqueue(submission, "request:legacy")["id"] == job_id
    assert not restarted.run_one()


def test_unconfirmed_legacy_keeps_media(tmp_path):
    worker, job_id = legacy(tmp_path, confirm=False)
    assert worker._age_out_delivered()["unconfirmed"] == 1
    assert worker.artifact(job_id, "final").read_bytes() == b"legacy clean"


@pytest.mark.parametrize("tamper", ["receipt", "final", "qa", "decode", "authority", "missing_authority", "null_promise", "authority_promise", "missing_pin"])
def test_legacy_still_requires_exact_persisted_authority(tmp_path, tamper):
    worker, job_id = legacy(tmp_path)
    row = worker._row(job_id)
    output = worker._output(row)
    if tamper == "missing_pin":
        with worker._db() as db:
            db.execute("UPDATE jobs SET receipt_sha256=NULL WHERE id=?", (job_id,))
    elif tamper == "null_promise":
        receipt = json.loads((output / "receipt.json").read_text())
        receipt["burned_fallback"] = None
        (output / "receipt.json").write_text(json.dumps(receipt))
    elif tamper in {"authority", "missing_authority", "authority_promise"}:
        authority = json.loads(row["output_authority_json"])
        if tamper == "authority_promise":
            authority["burned_fallback"] = None
        else:
            authority["decode"]["decoded_video_frames"] = 0
        with worker._db() as db:
            db.execute("UPDATE jobs SET output_authority_json=? WHERE id=?",
                       (None if tamper == "missing_authority" else json.dumps(authority), job_id))
    elif tamper == "receipt":
        receipt = json.loads((output / "receipt.json").read_text())
        receipt["caption_sha256"] = "0" * 64
        (output / "receipt.json").write_text(json.dumps(receipt))
    elif tamper == "decode":
        (output / "decode.json").write_text('{}')
    else:
        (output / ("final.mp4" if tamper == "final" else "qa.jpg")).write_bytes(b"changed")
    with pytest.raises(jobs.RenderJobError):
        worker.artifact(job_id, "final")
    with pytest.raises(jobs.RenderJobError):
        worker.acknowledge(job_id)
    assert worker._age_out_delivered()["failed"] == 1


@pytest.mark.parametrize("mode", ["clean_only", "null_promise", "missing_pair", "missing_qa"])
def test_new_inapp_never_uses_legacy_exemption(tmp_path, mode):
    def renderer(source, output, request, *, clock_ms):
        if mode == "clean_only":
            _write_fake_render(output, request, clock_ms=clock_ms, final=b"new clean")
            receipt = json.loads((output / "receipt.json").read_text())
            receipt["caption_delivery"] = "in_app"
            (output / "receipt.json").write_text(json.dumps(receipt))
        else:
            dual_render(source, output, request, clock_ms=clock_ms)
            if mode == "null_promise":
                receipt = json.loads((output / "receipt.json").read_text())
                receipt["burned_fallback"] = None
                (output / "receipt.json").write_text(json.dumps(receipt))
            elif mode == "missing_pair":
                shutil.rmtree(output / "burned")
            else:
                (output / "burned/qa.jpg").unlink()
    worker = service(tmp_path, renderer=renderer)
    job = worker.enqueue(payload("slot:new", "new caption"), "request:new")
    assert worker.run_one()
    assert worker.status(job["id"])["state"] == "failed"
    assert worker.status(job["id"])["error_code"].startswith("burned_fallback")


@pytest.mark.parametrize("mode", ["missing_pair", "stripped_promise", "null_promise"])
def test_succeeded_pair_cannot_downgrade_to_legacy(tmp_path, mode):
    worker = service(tmp_path, renderer=dual_render)
    job = worker.enqueue(payload("slot:pair", "pair caption"), "request:pair")
    assert worker.run_one()
    row = worker._row(job["id"])
    output = worker._output(row)
    shutil.rmtree(output / "burned")
    if mode != "missing_pair":
        receipt = json.loads((output / "receipt.json").read_text())
        if mode == "null_promise":
            receipt["burned_fallback"] = None
        else:
            del receipt["burned_fallback"]
        (output / "receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(jobs.RenderJobError):
        worker.artifact(job["id"], "final")
    with pytest.raises(jobs.RenderJobError):
        worker.acknowledge(job["id"])


def test_measure_legacy_verification_cost(tmp_path):
    worker, job_id = legacy(tmp_path)
    row = worker._row(job_id)
    samples = []
    for _ in range(100):
        start = time.perf_counter()
        worker._verify_output(row)
        samples.append((time.perf_counter() - start) * 1000)
    print(f"LEGACY_COST n=100 median_ms={statistics.median(samples):.4f} max_ms={max(samples):.4f}")


@pytest.mark.parametrize("location", ["authority", "receipt"])
def test_pair_tombstone_keeps_promised_pair_required(tmp_path, location):
    worker = service(tmp_path, renderer=dual_render)
    job = worker.enqueue(payload("slot:durable", "durable pair"), "request:durable")
    assert worker.run_one()
    worker.acknowledge(job["id"])
    worker.retire(job["id"])
    row = worker._row(job["id"])
    authority = json.loads(row["output_authority_json"])
    del (authority if location == "authority" else authority["receipt"])["burned_fallback"]
    with worker._db() as db:
        db.execute("UPDATE jobs SET output_authority_json=? WHERE id=?", (json.dumps(authority), job["id"]))
    with pytest.raises(jobs.RenderJobError):
        worker._verify_output(worker._row(job["id"]))
