import concurrent.futures
import json
import threading

import pytest

from services import post_render_jobs as jobs
from services.post_render import render_post, sha256
from tests.test_post_render import actual_source
from tests.test_post_render_jobs import NOW, _write_fake_render, service, submission


def dual_render(source, output, request, *, clock_ms, clean=b"shared clean"):
    _write_fake_render(output, request, clock_ms=clock_ms, final=clean)
    _write_fake_render(output / "burned", request, clock_ms=clock_ms,
                       final=b"burned:" + request.caption.encode())
    burned = json.loads((output / "burned/receipt.json").read_text())
    receipt = json.loads((output / "receipt.json").read_text())
    receipt["caption_delivery"] = "in_app"
    receipt["burned_fallback"] = {key: burned[key] for key in (
        "final_sha256", "final_byte_length", "final_object_key", "qa_frame_sha256")}
    receipt["burned_fallback"]["final_object_key"] = f"posting/dual-final/{burned['final_sha256']}.mp4"
    (output / "receipt.json").write_text(json.dumps(receipt))
    decode = json.loads((output / "burned/decode.json").read_text())
    decode["final_probe"].update(width=1080, height=1920, video_codec="h264", pixel_format="yuv420p")
    (output / "burned/decode.json").write_text(json.dumps(decode))


def payload(slot, caption, source=b"source", **kwargs):
    return submission(slot_id=slot, caption=caption, caption_sha256=sha256(caption.encode()),
                      source=source, caption_delivery="in_app", **kwargs)


def test_distinct_captions_share_clean_bytes_and_keep_true_status_through_restart_retire(tmp_path):
    worker = service(tmp_path, renderer=dual_render)
    ids = []
    for index, caption in enumerate(("caption one", "caption two")):
        job = worker.enqueue(payload(f"slot:{index}", caption), f"request:{index}")
        assert worker.run_one()
        assert worker.status(job["id"])["state"] == "succeeded"
        ids.append(job["id"])
    receipts = [json.loads(worker.artifact(job_id, "receipt").read_text()) for job_id in ids]
    assert receipts[0]["final_sha256"] == receipts[1]["final_sha256"]
    assert receipts[0]["burned_fallback"]["final_sha256"] != receipts[1]["burned_fallback"]["final_sha256"]
    worker = service(tmp_path, renderer=dual_render)
    for job_id, receipt in zip(ids, receipts):
        assert worker.status(job_id)["final_sha256"] == receipt["final_sha256"]
        worker.acknowledge(job_id)
        worker.retire(job_id)
        assert worker.status(job_id)["final_sha256"] == receipt["final_sha256"]
    with worker._db() as db:
        rows = db.execute("SELECT final_sha256 FROM jobs").fetchall()
    assert sum(row["final_sha256"] is None for row in rows) == 1


def test_repeated_burned_fallback_is_rejected_even_after_shared_clean_retirement(tmp_path):
    worker = service(tmp_path, renderer=dual_render)
    for index, caption in enumerate(("one", "two", "two")):
        job = worker.enqueue(payload(f"slot:{index}", caption), f"request:{index}")
        assert worker.run_one()
        status = worker.status(job["id"])
        if index < 2:
            assert status["state"] == "succeeded"
            worker.acknowledge(job["id"])
            worker.retire(job["id"])
        else:
            assert status["state"] == "failed"
            assert status["error_code"] == "duplicate_output"


@pytest.mark.parametrize("same_caption", [False, True])
def test_concurrent_workers_serialize_dual_identity_admission(tmp_path, same_caption):
    rendezvous = threading.Barrier(2)
    def renderer(*args, **kwargs):
        dual_render(*args, **kwargs)
        rendezvous.wait(timeout=10)
    worker = service(tmp_path, renderer=renderer)
    ids = [worker.enqueue(payload(f"slot:{i}", "same" if same_caption else f"caption {i}"),
                          f"request:{i}")["id"] for i in range(2)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert all(pool.map(lambda _: worker.run_one(), range(2)))
    states = [worker.status(job_id) for job_id in ids]
    assert sum(row["state"] == "succeeded" for row in states) == (1 if same_caption else 2)
    assert all(row["state"] == "succeeded" or row["error_code"] == "duplicate_output" for row in states)


def test_real_shared_source_two_captions_produce_two_admitted_pairs(tmp_path, actual_source):
    source = actual_source.read_bytes()
    worker = service(tmp_path, renderer=render_post,
                     fetcher=lambda _request, path: path.write_bytes(source))
    receipts = []
    for index, caption in enumerate(("first caption", "second caption")):
        job = worker.enqueue(payload(f"slot:{index}", caption, source), f"request:{index}")
        assert worker.run_one()
        assert worker.status(job["id"])["state"] == "succeeded"
        receipts.append(json.loads(worker.artifact(job["id"], "receipt").read_text()))
    assert receipts[0]["final_sha256"] == receipts[1]["final_sha256"]
    assert receipts[0]["burned_fallback"]["final_sha256"] != receipts[1]["burned_fallback"]["final_sha256"]


def test_shared_clean_confirmation_never_retires_unacknowledged_distinct_fallback(tmp_path):
    clock = [NOW]
    worker = service(tmp_path, renderer=dual_render, clock=lambda: clock[0])
    ids = []
    for index in range(2):
        job = worker.enqueue(payload(f"slot:{index}", f"caption {index}"), f"request:{index}")
        assert worker.run_one()
        ids.append(job["id"])
    worker.acknowledge(ids[0])
    worker.confirm_final = lambda *_: True
    clock[0] += jobs.DELIVERED_RETIRE_AFTER_MS["normal"] + 1
    result = worker._age_out_delivered()
    assert result["retired"] == 1
    assert result["unconfirmed"] == 1
    assert worker.status(ids[1])["retired_at_ms"] is None
    assert worker.artifact(ids[1], "burned_final").exists()


def test_same_burned_bytes_still_duplicate_when_clean_bytes_differ(tmp_path):
    def renderer(source, output, request, **kwargs):
        dual_render(source, output, request, clean=request.slot_id.encode(), **kwargs)
    worker = service(tmp_path, renderer=renderer)
    for index in range(2):
        job = worker.enqueue(payload(f"slot:{index}", "same caption"), f"request:{index}")
        worker.run_one()
        assert worker.status(job["id"])["state"] == ("succeeded" if index == 0 else "failed")
    assert worker.status(job["id"])["error_code"] == "duplicate_output"


@pytest.mark.parametrize("dual_first", [False, True])
def test_ordinary_and_dual_burned_identity_cannot_duplicate_each_other(tmp_path, dual_first):
    def renderer(source, output, request, **kwargs):
        if request.slot_id == "slot:dual":
            dual_render(source, output, request, **kwargs)
        else:
            _write_fake_render(output, request, final=b"burned:caption", **kwargs)
    worker = service(tmp_path, renderer=renderer)
    requests = [payload("slot:dual", "caption"), submission(slot_id="slot:ordinary")]
    if not dual_first:
        requests.reverse()
    for index, request in enumerate(requests):
        job = worker.enqueue(request, f"request:{index}")
        worker.run_one()
        assert worker.status(job["id"])["state"] == ("succeeded" if index == 0 else "failed")
    assert worker.status(job["id"])["error_code"] == "duplicate_output"


def test_restart_recovers_shared_clean_pair_without_rerender_after_finish_crash(tmp_path, monkeypatch):
    from tests.test_post_render_jobs import Crash
    worker = service(tmp_path, renderer=dual_render)
    first = worker.enqueue(payload("slot:first", "first"), "request:first")
    assert worker.run_one()
    second = worker.enqueue(payload("slot:second", "second"), "request:second")
    monkeypatch.setattr(worker, "_finish", lambda *_: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        worker.run_one()
    def no_render(*_args, **_kwargs):
        raise AssertionError("complete retained pair must recover without another encode")
    resumed = service(tmp_path, renderer=no_render)
    assert resumed.run_one()
    assert resumed.status(second["id"])["state"] == "succeeded"
    assert resumed.status(first["id"])["final_sha256"] == resumed.status(second["id"])["final_sha256"]
    assert resumed._row(second["id"])["final_sha256"] is None
