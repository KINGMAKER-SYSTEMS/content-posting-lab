"""FIX ROUND 2 regression tests for the Lab no-burn backstop (F-NO-BURN-FLEETWIDE-1103).

These pin the three HELD findings plus the OFF byte-identical gap:

  H1  OFF->ON restart must not quarantine already-succeeded in-app pairs, and a
      brand-new in-app job must still render clean-only.
  H2  flag ON: two in-app slots with the same source+look must not collide as
      duplicate_output (their clean bytes are identical; their slots differ).
  H3  jobs that already succeeded as clean+burned pairs must keep serving the
      clean asset after the flip (stable status, no HTTP-409 class error).
  GAP flag OFF must stay byte-identical: the burned fallback copy must still be
      produced when OFF (real ffmpeg).

They are expected to FAIL on the held ee02370d builder tree.
"""
import json

import pytest

from services import post_render as render
from services import post_render_jobs as jobs
from services.post_render import sha256
from tests.test_post_render import actual_source, request as render_request
from tests.test_post_render_jobs import NOW, service, submission


def _clean_render(source, output, request, *, clock_ms, final):
    """A clean-only in_app render: identical bytes for a shared source+look."""
    output.mkdir()
    qa = b"clean-qa"
    (output / "final.mp4").write_bytes(final)
    (output / "qa.jpg").write_bytes(qa)
    receipt = {key: getattr(request, key) for key in (
        "slot_id", "slot_payload_sha256", "page_id", "program_id", "device_serial",
        "account", "source_sha256", "caption_sha256", "treatment_sha256",
        "renderer_id", "renderer_version")}
    receipt.update(schema="posting-prepared-artifact/v1", final_sha256=sha256(final),
                   final_byte_length=len(final), qa_frame_sha256=sha256(qa),
                   final_object_key=f"posting/final/{sha256(final)}.mp4", mime_type="video/mp4",
                   width=1080, height=1920, duration_ms=1000, completed_at_ms=clock_ms(),
                   caption_delivery="in_app")
    (output / "receipt.json").write_text(json.dumps(receipt, separators=(",", ":")))
    (output / "decode.json").write_text(json.dumps({
        "final_sha256": sha256(final), "qa_frame_sha256": sha256(qa),
        "decoded_video_frames": 30, "final_probe": {"duration_ms": 1000}}))


def dual_render(source, output, request, *, clock_ms):
    """OFF-era paired in_app render: shared clean + per-slot burned fallback."""
    from tests.test_post_render_dual_dedup import dual_render as _dual
    _dual(source, output, request, clock_ms=clock_ms)


def _in_app(source=b"source", **kwargs):
    return submission(source=source, caption_delivery="in_app", **kwargs)


# --- H1: OFF->ON restart does not quarantine and new in-app jobs still run -----

def test_off_on_restart_keeps_paired_history_and_new_inapp_renders_clean(tmp_path, monkeypatch):
    worker = service(tmp_path, renderer=dual_render)
    paired = worker.enqueue(_in_app(), "h1-paired-off")
    assert worker.run_one()
    assert worker.status(paired["id"])["state"] == "succeeded"

    monkeypatch.setenv(jobs.CONTENT_LAB_REFUSE_CAPTION_BURN, "1")
    # Restart applies the flag: PostRenderJobs.__init__ rebuilds identities.
    def unique_clean(source, output, request, **kwargs):
        _clean_render(source, output, request, final=b"clean:" + request.source_sha256.encode(), **kwargs)

    restarted = service(tmp_path, renderer=unique_clean)
    # The OFF-era pair must not become an uncertainty marker.
    assert restarted.status(paired["id"])["state"] == "succeeded"
    fresh = restarted.enqueue(_in_app(source=b"fresh-source", slot_id="slot:new"), "h1-fresh-on")
    assert restarted.run_one()
    status = restarted.status(fresh["id"])
    assert status["state"] == "succeeded", status
    assert set(status["artifacts"]) == {"final", "qa", "receipt"}


# --- H2: shared clean bytes across distinct slots are not duplicate_output -----

def test_flag_on_shared_source_look_two_slots_both_succeed(tmp_path, monkeypatch):
    monkeypatch.setenv(jobs.CONTENT_LAB_REFUSE_CAPTION_BURN, "1")

    def shared_clean(source, output, request, **kwargs):
        _clean_render(source, output, request, final=b"clean:shared-bytes", **kwargs)

    worker = service(tmp_path, renderer=shared_clean)
    ids = []
    for index in range(2):
        caption = f"caption {index}"
        job = worker.enqueue(_in_app(slot_id=f"slot:shared:{index}",
                                     caption=caption, caption_sha256=sha256(caption.encode())),
                             f"h2-request:{index}")
        assert worker.run_one()
        status = worker.status(job["id"])
        assert status["state"] == "succeeded", (index, status)
        ids.append(job["id"])
    receipts = [json.loads(worker.artifact(job_id, "receipt").read_text()) for job_id in ids]
    assert receipts[0]["final_sha256"] == receipts[1]["final_sha256"]
    assert receipts[0]["slot_id"] != receipts[1]["slot_id"]


# --- H3: already-succeeded OFF-era pairs keep serving clean after the flip ------

def test_flag_on_succeeded_pair_still_serves_clean_asset(tmp_path, monkeypatch):
    worker = service(tmp_path, renderer=dual_render)
    job = worker.enqueue(_in_app(), "h3-pair-off")
    assert worker.run_one()
    assert worker.status(job["id"])["state"] == "succeeded"
    clean = json.loads(worker.artifact(job["id"], "receipt").read_text())["final_sha256"]

    monkeypatch.setenv(jobs.CONTENT_LAB_REFUSE_CAPTION_BURN, "1")
    restarted = service(tmp_path, renderer=dual_render)
    status = restarted.status(job["id"])
    assert status["state"] == "succeeded", status
    assert status["final_sha256"] == clean
    # Serving the clean asset stays available; the pair is not a 409.
    assert restarted.artifact(job["id"], "final").read_bytes()


# --- GAP: flag OFF stays byte-identical (burned fallback still produced) --------

def test_flag_off_real_render_still_produces_burned_fallback(tmp_path, actual_source, monkeypatch):
    monkeypatch.delenv(jobs.CONTENT_LAB_REFUSE_CAPTION_BURN, raising=False)
    source_bytes = actual_source.read_bytes()
    request = render_request(sha256(source_bytes), caption_delivery="in_app")
    output = tmp_path / "render"
    result = jobs.render_prepared_post(actual_source, output, request, clock_ms=lambda: NOW)
    receipt = json.loads(result.receipt_json)
    assert "burned_fallback" in receipt
    assert (output / "burned" / "final.mp4").is_file()
    assert receipt["burned_fallback"]["final_sha256"] != receipt["final_sha256"]
