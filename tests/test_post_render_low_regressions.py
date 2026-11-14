import json

import pytest

from tests.test_post_render_jobs import service, submission


@pytest.mark.parametrize("stored", [None, "{", "{}"])
def test_legacy_succeeded_without_hash_authority_keeps_legacy_status(tmp_path, stored):
    worker = service(tmp_path)
    job = worker.enqueue(submission(), "request:legacy")
    with worker._db() as db:
        db.execute("UPDATE jobs SET state='succeeded', receipt_sha256='legacy-receipt', "
                   "output_authority_json=? WHERE id=?", (stored, job["id"]))
    status = worker.status(job["id"])
    assert status["state"] == "succeeded"
    assert status["receipt_sha256"] == "legacy-receipt"
    assert "final_sha256" not in status
    assert "qa_frame_sha256" not in status
    assert set(status["artifacts"]) == {"final", "qa", "receipt"}


def test_shared_hash_tombstone_with_invalid_authority_keeps_legacy_status(tmp_path):
    worker = service(tmp_path)
    job = worker.enqueue(submission(), "request:shared")
    assert worker.run_one()
    worker.acknowledge(job["id"])
    worker.retire(job["id"])
    with worker._db() as db:
        db.execute("UPDATE jobs SET final_sha256=NULL, output_authority_json='{}' WHERE id=?", (job["id"],))
    status = worker.status(job["id"])
    assert status["state"] == "succeeded"
    assert status["retired_at_ms"] is not None
    assert "final_sha256" not in status
    assert "qa_frame_sha256" not in status


def test_corrupt_authority_row_does_not_poison_an_ordinary_job(tmp_path, caplog):
    worker = service(tmp_path)
    bad = worker.enqueue(submission(slot_id="slot:corrupt"), "request:corrupt")
    with worker._db() as db:
        db.execute("UPDATE jobs SET state='succeeded', output_authority_json='{' WHERE id=?", (bad["id"],))
    worker = service(tmp_path)  # startup detects and isolates corrupt history
    good = worker.enqueue(submission(slot_id="slot:ordinary"), "request:ordinary")
    assert worker.run_one()
    assert worker.status(good["id"])["state"] == "succeeded"
    assert bad["id"] in caplog.text
