"""Legacy ordinary submissions must not poison unrelated in-app reservations."""
import json

import pytest

from services import post_render_jobs as jobs
from tests.test_post_render_jobs import service, submission
from tests.test_post_render_dual_dedup import dual_render, payload


def invalidate(raw, defect):
    saved = json.loads(raw)
    if defect == "extra":
        saved["old_version_field"] = True
    elif defect == "missing":
        del saved["request"]["renderer_version"]
    elif defect == "hash":
        saved["request"]["slot_payload_sha256"] = "0" * 64
    elif defect == "binding":
        saved["request"]["account"] = "old.account"
    elif defect == "schema":
        saved["schema"] = "content-lab.post-render-job.v0"
    return json.dumps(saved)


def seed(worker, *, in_app=False, defect="extra", stale=False, state="succeeded"):
    body = payload("slot:old", "old") if in_app else submission(slot_id="slot:old")
    jid = worker.enqueue(body, "request:old")["id"]
    assert worker.run_one()
    row = dict(worker._row(jid))
    with worker._db() as db:
        db.execute("UPDATE jobs SET submission_json=?,state=? WHERE id=?",
                   (invalidate(row["submission_json"], defect), state, jid))
        if stale:
            db.execute("INSERT INTO idempotency(key,job_id,request_hash) VALUES(?,?,?)",
                       ("/output-uncertain/v1/" + jid, jid, row["request_hash"]))
    return jid, dict(worker._row(jid))


def run_fresh_inapp(worker):
    fresh = worker.enqueue(payload("slot:fresh", "fresh"), "request:fresh")
    assert fresh["state"] == "queued"
    assert worker.run_one()
    return worker.status(fresh["id"])


@pytest.mark.parametrize("defect", ["extra", "missing", "hash", "binding", "schema"])
@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
def test_invalid_ordinary_startup_accepts_inapp_without_rewriting_old_row(tmp_path, caplog, defect, stale, mixed):
    worker = service(tmp_path)
    jid, original = seed(worker, defect=defect)
    with pytest.raises(ValueError):
        jobs.RenderJobSubmission.model_validate_json(original["submission_json"])
    if mixed:
        worker.renderer = dual_render
        prior = worker.enqueue(payload("slot:prior", "prior"), "request:prior")
        assert worker.run_one()
        assert worker.status(prior["id"])["state"] == "succeeded"
    if stale:
        with worker._db() as db:
            db.execute("INSERT INTO idempotency(key,job_id,request_hash) VALUES(?,?,?)",
                       ("/output-uncertain/v1/" + jid, jid, original["request_hash"]))
    worker = service(tmp_path, renderer=dual_render)
    assert run_fresh_inapp(worker)["state"] == "succeeded"
    assert dict(worker._row(jid)) == original
    assert jid in caplog.text
    with worker._db() as db:
        assert not db.execute("SELECT 1 FROM idempotency WHERE key LIKE '/output-uncertain/%'").fetchone()
    # A second restart must remain healed without operator action.
    worker = service(tmp_path, renderer=dual_render)
    assert dict(worker._row(jid)) == original
    with worker._db() as db:
        assert not db.execute("SELECT 1 FROM idempotency WHERE key LIKE '/output-uncertain/%'").fetchone()


def test_receipt_pinned_non_success_ordinary_row_also_cannot_block_inapp(tmp_path):
    worker = service(tmp_path)
    jid, original = seed(worker, state="failed")
    assert original["receipt_sha256"]
    worker = service(tmp_path, renderer=dual_render)
    assert run_fresh_inapp(worker)["state"] == "succeeded"
    assert dict(worker._row(jid)) == original


@pytest.mark.parametrize("defect", ["extra", "missing", "hash", "binding", "schema"])
def test_invalid_inapp_submission_still_blocks_without_rerendering_history(tmp_path, defect):
    worker = service(tmp_path, renderer=dual_render)
    jid, original = seed(worker, in_app=True, defect=defect)
    worker = service(tmp_path, renderer=dual_render)
    assert run_fresh_inapp(worker)["error_code"] == "output_authority_uncertain"
    assert dict(worker._row(jid)) == original


@pytest.mark.parametrize("raw", ["{", "null", "[]", "{}"])
def test_unclassifiable_history_stays_uncertain(tmp_path, raw):
    worker = service(tmp_path)
    jid, _ = seed(worker)
    with worker._db() as db:
        db.execute("UPDATE jobs SET submission_json=? WHERE id=?", (raw, jid))
    worker = service(tmp_path, renderer=dual_render)
    assert run_fresh_inapp(worker)["error_code"] == "output_authority_uncertain"


def test_conflicting_treatment_copies_do_not_clear_inapp_marker(tmp_path):
    worker = service(tmp_path, renderer=dual_render)
    jid, original = seed(worker, in_app=True, stale=True)
    saved = json.loads(original["submission_json"])
    treatment = json.loads(saved["request"]["render_treatment_json"])
    del treatment["captionDelivery"]
    saved["request"]["render_treatment_json"] = json.dumps(treatment)
    with worker._db() as db:
        db.execute("UPDATE jobs SET submission_json=? WHERE id=?", (json.dumps(saved), jid))
    worker = service(tmp_path, renderer=dual_render)
    assert run_fresh_inapp(worker)["error_code"] == "output_authority_uncertain"


@pytest.mark.parametrize("candidate", [False, True])
def test_valid_ordinary_stale_marker_self_heals_even_outside_backfill_candidates(tmp_path, candidate):
    worker = service(tmp_path)
    old = worker.enqueue(submission(slot_id="slot:old"), "request:old")
    assert worker.run_one()
    with worker._db() as db:
        if not candidate:
            db.execute("UPDATE jobs SET state='failed',receipt_sha256=NULL WHERE id=?", (old["id"],))
        row = worker._row(old["id"])
        db.execute("INSERT INTO idempotency(key,job_id,request_hash) VALUES(?,?,?)",
                   ("/output-uncertain/v1/" + old["id"], old["id"], row["request_hash"]))
    worker = service(tmp_path, renderer=dual_render)
    assert run_fresh_inapp(worker)["state"] == "succeeded"
    with worker._db() as db:
        assert not db.execute("SELECT 1 FROM idempotency WHERE key LIKE '/output-uncertain/%'").fetchone()


def test_stripping_both_optins_without_changing_treatment_pin_remains_uncertain(tmp_path):
    worker = service(tmp_path, renderer=dual_render)
    jid, original = seed(worker, in_app=True, stale=True)
    saved = json.loads(original["submission_json"])
    treatment = json.loads(saved["request"]["render_treatment_json"])
    del treatment["captionDelivery"]
    saved["request"]["render_treatment_json"] = json.dumps(treatment)
    slot = json.loads(saved["slot_payload_json"])
    del slot["render_treatment"]["captionDelivery"]
    saved["slot_payload_json"] = json.dumps(slot)
    with worker._db() as db:
        db.execute("UPDATE jobs SET submission_json=? WHERE id=?", (json.dumps(saved), jid))
    worker = service(tmp_path, renderer=dual_render)
    assert run_fresh_inapp(worker)["error_code"] == "output_authority_uncertain"


def test_ordinary_only_invalid_history_does_not_enter_identity_write_pass(tmp_path, monkeypatch):
    worker = service(tmp_path)
    seed(worker)
    def no_backfill(*args):
        raise AssertionError("ordinary history entered identity write pass")
    monkeypatch.setattr(jobs.PostRenderJobs, "_backfill_output_identities", no_backfill)
    service(tmp_path)
