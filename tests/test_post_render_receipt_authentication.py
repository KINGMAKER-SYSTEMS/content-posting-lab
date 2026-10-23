"""Exact receipt-byte authentication and safe retained-history refusal."""
import base64
import json
import shutil
import statistics
import time

import pytest

from services import post_render_jobs as jobs
from tests.test_post_render_jobs import service, submission
from tests.test_post_render_dual_dedup import dual_render, payload
from tests.test_post_render_legacy_upgrade import legacy, offline


def finished(worker, slot="first", caption="same"):
    job = worker.enqueue(payload("slot:" + slot, caption), "request:" + slot)
    assert worker.run_one()
    assert worker.status(job["id"])["state"] == "succeeded"
    worker.acknowledge(job["id"])
    worker.retire(job["id"])
    return job["id"]


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_double_stripped_pair_refuses_verification_and_repeat(tmp_path, shared, restart):
    worker = service(tmp_path, renderer=dual_render)
    if shared:
        finished(worker, "owner", "other")
    jid = finished(worker)
    original = dict(worker._row(jid))
    authority = json.loads(original["output_authority_json"])
    del authority["burned_fallback"]
    del authority["receipt"]["burned_fallback"]
    with worker._db() as db:
        db.execute("UPDATE jobs SET output_authority_json=? WHERE id=?", (json.dumps(authority), jid))
    if restart:
        worker = service(tmp_path, renderer=dual_render)
    with pytest.raises(jobs.RenderJobError):
        worker._verify_output(worker._row(jid))
    with pytest.raises(jobs.RenderJobError):
        worker.status(jid)
    again = worker.enqueue(payload("slot:repeat", "same"), "request:repeat")
    assert worker.run_one()
    assert worker.status(again["id"])["state"] == "failed"
    assert worker.status(again["id"])["error_code"] in {"duplicate_output", "output_authority_uncertain"}
    assert worker._row(jid)["receipt_sha256"] == original["receipt_sha256"]
    assert not worker._output(original).exists()


@pytest.mark.parametrize("edit", ["projection", "raw", "remove_raw", "forged_raw"])
def test_receipt_pin_authenticates_exact_bytes_and_projection(tmp_path, edit):
    worker = service(tmp_path, renderer=dual_render)
    jid = finished(worker)
    row = worker._row(jid)
    authority = json.loads(row["output_authority_json"])
    assert jobs.sha256(base64.b64decode(authority["receipt_bytes_b64"])) == row["receipt_sha256"]
    # A valid shape and unchanged original pins must not authorize changed receipt facts.
    authority["receipt"]["completed_at_ms"] -= 1
    if edit == "raw":
        authority["receipt_bytes_b64"] = base64.b64encode(b"{}").decode()
    elif edit == "remove_raw":
        del authority["receipt_bytes_b64"]
    elif edit == "forged_raw":
        authority["receipt_bytes_b64"] = base64.b64encode(json.dumps(authority["receipt"]).encode()).decode()
    with worker._db() as db:
        db.execute("UPDATE jobs SET output_authority_json=? WHERE id=?", (json.dumps(authority), jid))
    with pytest.raises(jobs.RenderJobError, match="output_authority_changed"):
        worker._verify_output(worker._row(jid))


def test_old_canonical_receipt_reconstructs_exact_bytes_without_raw(tmp_path):
    def renderer(source, output, request, *, clock_ms):
        dual_render(source, output, request, clock_ms=clock_ms)
        receipt = json.loads((output / "receipt.json").read_bytes())
        (output / "receipt.json").write_bytes(json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
    worker = service(tmp_path, renderer=renderer)
    jid = finished(worker, caption="café")
    authority = json.loads(worker._row(jid)["output_authority_json"])
    del authority["receipt_bytes_b64"]
    with worker._db() as db:
        db.execute("UPDATE jobs SET output_authority_json=? WHERE id=?", (json.dumps(authority), jid))
        db.execute("DELETE FROM idempotency WHERE key LIKE '/output-%'")
    worker = service(tmp_path, renderer=renderer)
    assert worker._verify_output(worker._row(jid))["receipt"] == authority["receipt"]
    assert worker.status(jid)["state"] == "succeeded"
    again = worker.enqueue(payload("slot:repeat", "café"), "request:repeat")
    assert worker.run_one()
    assert worker.status(again["id"])["error_code"] == "duplicate_output"


@pytest.mark.parametrize("stored", [None, "{", "{}", "stripped"])
def test_unknown_historical_identity_blocks_only_inapp_and_recovers(tmp_path, stored):
    worker = service(tmp_path, renderer=dual_render)
    finished(worker, "owner", "other")
    jid = finished(worker)
    original = worker._row(jid)["output_authority_json"]
    if stored == "stripped":
        authority = json.loads(original)
        del authority["burned_fallback"]
        del authority["receipt"]["burned_fallback"]
        authority.pop("receipt_bytes_b64")
        stored = json.dumps(authority)
    with worker._db() as db:
        db.execute("DELETE FROM idempotency WHERE key LIKE '/output-%'")
        db.execute("UPDATE jobs SET output_authority_json=? WHERE id=?", (stored, jid))
    worker = service(tmp_path, renderer=dual_render)
    again = worker.enqueue(payload("slot:repeat", "same"), "request:repeat")
    assert worker.run_one()
    assert worker.status(again["id"])["error_code"] == "output_authority_uncertain"
    # A bad in-app history never starts another paid render automatically and
    # does not prevent unrelated ordinary work.
    from tests.test_post_render_jobs import _write_fake_render
    worker.renderer = lambda source, output, request, clock_ms: _write_fake_render(output, request, clock_ms=clock_ms, final=b"ordinary")
    ordinary = worker.enqueue(submission(slot_id="slot:ordinary"), "request:ordinary")
    assert worker.run_one()
    assert worker.status(ordinary["id"])["state"] == "succeeded"
    with worker._db() as db:
        db.execute("UPDATE jobs SET output_authority_json=? WHERE id=?", (original, jid))
    worker = service(tmp_path, renderer=dual_render)
    with worker._db() as db:
        assert not db.execute("SELECT 1 FROM idempotency WHERE key LIKE '/output-uncertain/%'").fetchone()
    repeated = worker.enqueue(payload("slot:recovered", "same"), "request:recovered")
    assert worker.run_one()
    assert worker.status(repeated["id"])["error_code"] == "duplicate_output"


def test_unverifiable_noncanonical_legacy_tombstone_is_preserved(tmp_path):
    worker, jid = legacy(tmp_path)
    original = dict(worker._row(jid))
    shutil.rmtree(worker._output(original).parent.parent)
    with pytest.raises(jobs.RenderJobError, match="output_authority_changed"):
        worker._verify_output(worker._row(jid))
    with pytest.raises(jobs.RenderJobError):
        worker.acknowledge(jid)
    assert dict(worker._row(jid)) == original


def test_receipt_authentication_measured_cost(tmp_path):
    worker = service(tmp_path, renderer=dual_render)
    jid = finished(worker)
    row = worker._row(jid)
    authority = worker._load_output_authority(row)
    samples = []
    for _ in range(1000):
        start = time.perf_counter_ns()
        worker._authenticate_receipt(row, authority)
        samples.append((time.perf_counter_ns() - start) / 1000000)
    print(f"RECEIPT_AUTH n=1000 median_ms={statistics.median(samples):.6f} max_ms={max(samples):.6f}")


def test_direct_reservation_rejects_stripped_retained_identity(tmp_path):
    worker = service(tmp_path, renderer=dual_render)
    jid = finished(worker)
    authority = json.loads(worker._row(jid)["output_authority_json"])
    del authority["burned_fallback"]
    del authority["receipt"]["burned_fallback"]
    with worker._db() as db:
        db.execute("UPDATE jobs SET output_authority_json=? WHERE id=?", (json.dumps(authority), jid))
    row = worker._row(jid)
    with worker._db() as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(jobs.RenderJobError):
            worker._reserve_output_identity(db, row, authority)
