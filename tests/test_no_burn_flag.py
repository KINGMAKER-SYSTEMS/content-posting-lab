"""CONTENT_LAB_REFUSE_CAPTION_BURN: Lab-side no-burn backstop (card F-NO-BURN-FLEETWIDE-1103).

Eric 11-03 22:3x standing rule: "Please don't do burn text on any of the pages in
this system." The Worker is getting its own no-burn default; this flag is the Lab
backstop so a mistaken Worker plan can never make the Lab burn caption text.

Default OFF == today's behaviour byte-identical. When ON the Lab never produces
burned caption pixels, through two doors:

  * enqueue + _execute refuse a burned (no ``captionDelivery``) v1 request with
    the stable reason ``caption_burn_refused`` (HTTP 409 on submit; terminal
    ``failed`` for any job that was queued while OFF and only rendered after the
    flip — retry, automatic requeue, provide_provenance, restart recovery).
  * ``render_prepared_post`` renders a ``captionDelivery: "in_app"`` request as
    the clean asset only (no ``burned_fallback`` copy or receipt key), which is
    the exact 3-artifact shape the Worker already admits.

Timed-lyric renders (lyric text IS the format) are exempt. The manual operator
burn tool (routers/burn.py) is out of scope per the brief.
"""
import json
from pathlib import Path

import pytest

from services import post_render as render
from services import post_render_jobs as jobs
from services import safety_controls
from services.post_render import PostRenderError, _caption_delivery
from services.post_render_timed import TimedPostRenderRequest
from tests.test_post_render_jobs import NOW, client_for, service, submission
from tests.test_post_render_jobs import Crash, _write_fake_render
from tests.test_post_render_timed_jobs import submission as timed_submission


def _burned_submission():
    """An ordinary v1 prepared-post submission with NO captionDelivery (burned)."""
    return submission()


def _in_app_submission():
    """A v1 prepared-post submission whose caption is bound but not drawn."""
    return submission(caption_delivery="in_app")


def _clean_render_files(output, request, *, clock_ms):
    """Write a valid clean in_app render (no burned/ dir, no burned_fallback)."""
    output.mkdir()
    final = b"clean-final:" + request.slot_id.encode()
    qa = b"clean-qa"
    (output / "final.mp4").write_bytes(final)
    (output / "qa.jpg").write_bytes(qa)
    receipt = {key: getattr(request, key) for key in (
        "slot_id", "slot_payload_sha256", "page_id", "program_id", "device_serial",
        "account", "source_sha256", "caption_sha256", "treatment_sha256",
        "renderer_id", "renderer_version")}
    receipt.update(schema="posting-prepared-artifact/v1", final_sha256=render.sha256(final),
                   final_byte_length=len(final), qa_frame_sha256=render.sha256(qa),
                   final_object_key=f"posting/final/{render.sha256(final)}.mp4", mime_type="video/mp4",
                   width=1080, height=1920, duration_ms=1000, completed_at_ms=clock_ms(),
                   caption_delivery="in_app")
    (output / "receipt.json").write_text(json.dumps(receipt, separators=(",", ":")))
    (output / "decode.json").write_text(json.dumps({
        "final_sha256": render.sha256(final), "qa_frame_sha256": render.sha256(qa),
        "decoded_video_frames": 30, "final_probe": {"duration_ms": 1000}}))
    return receipt


# --- flag OFF is byte-identical -------------------------------------------------

def test_flag_defaults_off_and_burned_submission_queues(tmp_path):
    assert jobs.refuse_caption_burn() is False
    worker = service(tmp_path)
    job = worker.enqueue(_burned_submission(), "no-burn-default-off")
    assert job["state"] == "queued"
    assert job["error_code"] is None


def test_safety_controls_off_has_no_flag_and_on_adds_it(monkeypatch):
    monkeypatch.delenv(jobs.CONTENT_LAB_REFUSE_CAPTION_BURN, raising=False)
    off = safety_controls.snapshot(None)
    # OFF keeps the base surface byte-identical: the key is absent, not False.
    assert "refuse_caption_burn" not in off["render"]
    monkeypatch.setenv(jobs.CONTENT_LAB_REFUSE_CAPTION_BURN, "1")
    assert safety_controls.snapshot(None)["render"]["refuse_caption_burn"] is True


# --- classifier -----------------------------------------------------------------

def test_caption_burn_classifier(tmp_path):
    assert jobs._caption_burn_requested(_burned_submission().request) is True
    assert jobs._caption_burn_requested(_in_app_submission().request) is False
    timed = jobs.RenderJobSubmission.model_validate(timed_submission()).request
    assert isinstance(timed, TimedPostRenderRequest)
    assert jobs._caption_burn_requested(timed) is False
    invalid = submission(caption_delivery="burned").request
    with pytest.raises(PostRenderError) as error:
        _caption_delivery(invalid)
    assert error.value.code == "caption_delivery_invalid"
    assert jobs._caption_burn_requested(invalid) is False


# --- H1: flag ON, in_app renders clean only ------------------------------------

def test_flag_on_refuses_burned_caption_before_any_work(tmp_path, monkeypatch):
    monkeypatch.setenv(jobs.CONTENT_LAB_REFUSE_CAPTION_BURN, "1")
    fetched, rendered = [], []

    def fetch(_request, path):
        fetched.append(path)
        path.write_bytes(b"source")

    def fake_renderer(source, output, request, **kwargs):
        rendered.append(request)
        raise AssertionError("renderer must not run when a caption burn is refused")

    worker = service(tmp_path, fetcher=fetch, renderer=fake_renderer)
    with pytest.raises(jobs.RenderJobError) as error:
        worker.enqueue(_burned_submission(), "no-burn-on")
    assert error.value.code == "caption_burn_refused"
    assert fetched == [] and rendered == []
    assert list((worker.root / "attempts").iterdir()) == []
    with pytest.raises(jobs.RenderJobError) as not_found:
        worker.status("render_" + "0" * 32)
    assert not_found.value.code == "job_not_found"


def test_flag_on_http_submit_returns_409_with_stable_code(tmp_path, monkeypatch):
    monkeypatch.setenv(jobs.CONTENT_LAB_REFUSE_CAPTION_BURN, "on")
    worker = service(tmp_path)
    client = client_for(worker, monkeypatch)
    headers = {"Authorization": "Bearer only-this-test-service-token",
               "X-RT-Page-Id": "page-a", "Idempotency-Key": "no-burn-http"}
    response = client.post("/api/control-plane/v1/post-renders",
                           json=_burned_submission().model_dump(by_alias=True), headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"] == "caption_burn_refused"


def test_render_post_allow_burned_fallback_false_skips_fallback(tmp_path, monkeypatch):
    calls = []

    def fake_single(source_path, output_directory, request, *,
                    font_dir=None, tools=None, clock_ms=None, burned_fallback=False):
        calls.append(burned_fallback)
        receipt = b'{"clean":true}'
        return render.RenderedPost(
            final_path=Path(output_directory) / "final.mp4",
            qa_frame_path=Path(output_directory) / "qa.jpg",
            receipt_path=Path(output_directory) / "receipt.json",
            receipt_json=receipt, receipt_sha256=render.sha256(receipt),
            source_probe=render.MediaProbe(1080, 1920, 1000, "h264", "yuv420p", "1:1", 0),
            final_probe=render.MediaProbe(1080, 1920, 1000, "h264", "yuv420p", "1:1", 0),
            decoded_video_frames=30, qa_at_ms=500)

    monkeypatch.setattr(render, "_render_post_single", fake_single)
    out = tmp_path / "render"
    result = render.render_post(tmp_path / "src.mp4", out, _in_app_submission().request,
                                allow_burned_fallback=False, clock_ms=lambda: NOW)
    assert calls == [False]
    assert "burned_fallback" not in json.loads(result.receipt_json)
    assert not (out / "burned").exists()


def test_flag_on_durable_in_app_render_is_clean_only(tmp_path, monkeypatch):
    """H1 end-to-end: render_prepared_post passes allow_burned_fallback=False,
    the durable job succeeds, and status exposes exactly 3 artifacts."""
    monkeypatch.setenv(jobs.CONTENT_LAB_REFUSE_CAPTION_BURN, "1")
    calls = []

    def fake_single(source_path, output_directory, request, *,
                    font_dir=None, tools=None, clock_ms=None, burned_fallback=False):
        calls.append(burned_fallback)
        assert burned_fallback is False
        receipt = _clean_render_files(output_directory, request, clock_ms=clock_ms)
        final = (output_directory / "final.mp4").read_bytes()
        qa = (output_directory / "qa.jpg").read_bytes()
        receipt_bytes = json.dumps(receipt, separators=(",", ":")).encode()
        return render.RenderedPost(
            final_path=output_directory / "final.mp4",
            qa_frame_path=output_directory / "qa.jpg",
            receipt_path=output_directory / "receipt.json",
            receipt_json=receipt_bytes, receipt_sha256=render.sha256(receipt_bytes),
            source_probe=render.MediaProbe(1080, 1920, 1000, "h264", "yuv420p", "1:1", 0),
            final_probe=render.MediaProbe(1080, 1920, 1000, "h264", "yuv420p", "1:1", 0),
            decoded_video_frames=30, qa_at_ms=500)

    monkeypatch.setattr(render, "_render_post_single", fake_single)
    worker = service(tmp_path, renderer=jobs.render_prepared_post)
    queued = worker.enqueue(_in_app_submission(), "no-burn-one-armed")
    assert queued["state"] == "queued"
    assert worker.run_one()
    status = worker.status(queued["id"])
    assert status["state"] == "succeeded"
    assert set(status["artifacts"]) == {"final", "qa", "receipt"}
    assert calls == [False]


# --- H2: render-time guard (job queued while OFF, rendered after the flip) ------

def test_flag_on_render_time_guard_refuses_queued_before_flip(tmp_path, monkeypatch):
    worker = service(tmp_path)
    job = worker.enqueue(_burned_submission(), "queued-before-flip")  # OFF
    assert worker.status(job["id"])["state"] == "queued"
    monkeypatch.setenv(jobs.CONTENT_LAB_REFUSE_CAPTION_BURN, "1")
    assert worker.run_one()
    status = worker.status(job["id"])
    assert status["state"] == "failed"
    assert status["error_code"] == "caption_burn_refused"
    assert status["retryable"] is False
    assert not (worker.root / "attempts" / job["id"]).exists()


def test_flag_on_render_time_guard_refuses_worker_retry(tmp_path, monkeypatch):
    def boom(source, output, request, **kwargs):
        raise jobs.RenderJobError("render_failed")

    worker = service(tmp_path, renderer=boom)
    job = worker.enqueue(_burned_submission(), "retry-path")  # OFF
    assert worker.run_one()
    assert worker.status(job["id"])["state"] == "failed"
    monkeypatch.setenv(jobs.CONTENT_LAB_REFUSE_CAPTION_BURN, "1")
    assert worker.retry(job["id"])["state"] == "queued"
    assert worker.run_one()
    status = worker.status(job["id"])
    assert status["state"] == "failed"
    assert status["error_code"] == "caption_burn_refused"
    assert status["retryable"] is False


def test_flag_on_render_time_guard_refuses_automatic_requeue(tmp_path, monkeypatch):
    def transient(source, output, request, **kwargs):
        raise jobs.RenderJobError("source_unavailable")

    now = [NOW]
    worker = service(tmp_path, renderer=transient, clock=lambda: now[0])
    job = worker.enqueue(_burned_submission(), "requeue-path")  # OFF
    assert worker.run_one()
    assert worker.status(job["id"])["state"] == "queued"  # transient -> requeued
    monkeypatch.setenv(jobs.CONTENT_LAB_REFUSE_CAPTION_BURN, "1")
    now[0] += 5000 * 3  # clear the transient backoff
    assert worker.run_one()
    status = worker.status(job["id"])
    assert status["state"] == "failed"
    assert status["error_code"] == "caption_burn_refused"
    assert status["retryable"] is False


def test_flag_on_render_time_guard_refuses_provide_provenance(tmp_path, monkeypatch):
    worker = service(tmp_path)
    full = _burned_submission()
    body = full.model_dump(by_alias=True)
    del body["source_provenance"]
    # Queued while OFF: a burned request without provenance lands in
    # regeneration_needed, not refused (enqueue's door is OFF).
    queued = worker.enqueue(jobs.RenderJobSubmission.model_validate(body), "prov-path")
    assert worker.status(queued["id"])["state"] == "regeneration_needed"
    monkeypatch.setenv(jobs.CONTENT_LAB_REFUSE_CAPTION_BURN, "1")
    # provide_provenance requeues it after the flip; the render-time guard
    # must still refuse before any fetch/render.
    assert worker.provide_provenance(queued["id"], full)["state"] == "queued"
    assert worker.run_one()
    status = worker.status(queued["id"])
    assert status["state"] == "failed"
    assert status["error_code"] == "caption_burn_refused"


def test_flag_on_render_time_guard_refuses_restart_recovery(tmp_path, monkeypatch):
    def crash_before_output(source, output, request, **kwargs):
        raise Crash()

    worker = service(tmp_path, renderer=crash_before_output)
    job = worker.enqueue(_burned_submission(), "restart-path")  # OFF
    with pytest.raises(Crash):
        worker.run_one()
    assert worker.status(job["id"])["state"] == "running"
    monkeypatch.setenv(jobs.CONTENT_LAB_REFUSE_CAPTION_BURN, "1")
    assert worker.run_one()  # restart recovery reaches _execute -> refused
    status = worker.status(job["id"])
    assert status["state"] == "failed"
    assert status["error_code"] == "caption_burn_refused"
    assert status["retryable"] is False


# --- timed lyric render stays working when ON ----------------------------------

def test_flag_on_lyric_timed_render_still_queues(tmp_path, monkeypatch):
    monkeypatch.setenv(jobs.CONTENT_LAB_REFUSE_CAPTION_BURN, "1")
    worker = service(tmp_path)
    body = jobs.RenderJobSubmission.model_validate(timed_submission())
    job = worker.enqueue(body, "no-burn-timed")
    assert job["state"] in {"queued", "regeneration_needed"}
