"""Output endpoint policy: an exact final frame count inside an approved interval.

The ordinary delivery encode resamples to 30 fps with no end bound, so a
531-frame 60000/1001 source (8.858850 s) becomes 267 frames / 8.900 s, which the
80 ms source-duration tolerance accepts. A slot whose sound has a measured quiet
interval ending at 8.875 s needs the one 30 fps boundary inside it: 266 frames,
8.866667 s. These tests pin that contract with real ffmpeg.
"""
import copy
import dataclasses
import json
import shutil
import subprocess

import pytest
from pydantic import ValidationError

from services import post_render as render
from services import post_render_jobs as jobs
from tests.test_post_render import NOW
from tests.test_post_render_jobs import submission as ordinary_submission
from tests.test_post_render_lyric_source import lyric_payload, rebind_slot


ENDPOINT_SCHEMA = "content-lab.post-render-output-endpoint.v1"
THO_ENDPOINT = {
    "schema": ENDPOINT_SCHEMA,
    "frame_rate": "30/1",
    "frames": 266,
    "interval_start_ms": 8830,
    "interval_end_ms": 8875,
    "evidence_sha256": "c9" * 32,
}
SHORT_ENDPOINT = {**THO_ENDPOINT, "frames": 26, "interval_start_ms": 860, "interval_end_ms": 870}


def _synthetic(path, *, frames, rate="60000/1001", audio=False):
    args = ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
            f"color=c=navy:size=1080x1920:rate={rate}"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    args += ["-vf", "drawbox=x=160:y=500:w=760:h=160:color=white:t=fill", "-frames:v", str(frames)]
    args += ["-c:a", "aac", "-shortest"] if audio else ["-an"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-threads", "2",
             "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(args, check=True, timeout=120)
    return path


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.fail("ffmpeg and ffprobe required for output endpoint evidence")
    root = tmp_path_factory.mktemp("output-endpoint")
    return {
        # The retained Tho shape: 531 frames at 60000/1001 = 8.858850 s, no audio.
        "tho": _synthetic(root / "tho531.mp4", frames=531),
        # 264 frames at 30 fps = 8.800 s: inside the 80 ms window of 8.866667 s,
        # yet two frames short of the 266 the policy demands.
        "two_short": _synthetic(root / "two-short.mp4", frames=264, rate="30"),
        # 53 frames at 60000/1001 = 0.884217 s for the quick caption-path runs.
        "short": _synthetic(root / "short53.mp4", frames=53),
        "audio": _synthetic(root / "audio531.mp4", frames=531, audio=True),
    }


def endpoint_payload(source=None, endpoint=THO_ENDPOINT, *, lyric=True, delivery=None):
    """A Worker-shaped submission whose immutable slot carries the endpoint policy."""
    if lyric:
        body = lyric_payload(delivery=delivery)
    else:
        treatment = {} if delivery is None else {"caption_delivery": delivery}
        body = ordinary_submission(**treatment).model_dump(by_alias=True)
    if source is not None:
        source_sha = render.sha256(source.read_bytes())
        body["request"]["source_sha256"] = source_sha
        body["source_provenance"]["source_sha256"] = source_sha
        rebind_slot(body, lambda slot: slot["asset"].update(sha256=source_sha))
    if endpoint is not None:
        rebind_slot(body, lambda slot: slot.update(output_endpoint=copy.deepcopy(endpoint)))
        body["request"]["output_endpoint"] = copy.deepcopy(endpoint)
    return body


def _render(source, tmp_path, body):
    accepted = jobs.RenderJobSubmission.model_validate(body)
    return render.render_post(source, tmp_path / "prepared", accepted.request, clock_ms=lambda: NOW)


# --- the cause, and the contract that corrects it ------------------------------------------

def test_ordinary_request_still_overshoots_the_tho_source_to_267_frames(sources, tmp_path):
    """Characterises the defect and pins that an absent policy keeps today's output."""
    result = _render(sources["tho"], tmp_path, endpoint_payload(sources["tho"], endpoint=None))
    assert result.source_probe.duration_ms == 8859
    assert result.decoded_video_frames == 267
    assert result.final_probe.duration_ms == 8900
    assert "output_endpoint_sha256" not in json.loads(result.receipt_json)


def test_endpoint_policy_renders_exactly_266_frames_inside_the_quiet_interval(sources, tmp_path):
    body = endpoint_payload(sources["tho"])
    result = _render(sources["tho"], tmp_path, body)
    assert result.source_probe.duration_ms == 8859
    assert result.decoded_video_frames == 266
    # 266 frames exactly; the container stores 8.866016 s or 8.866667 s.
    assert result.final_probe.duration_ms in (8866, 8867)
    raw = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
         "stream=nb_read_frames,r_frame_rate,duration", "-of", "json", str(result.final_path)],
        check=True, capture_output=True, timeout=60).stdout
    stream = json.loads(raw)["streams"][0]
    assert stream["nb_read_frames"] == "266" and stream["r_frame_rate"] == "30/1"
    assert abs(float(stream["duration"]) - 266 / 30) < 0.001
    packets = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,duration_time",
         "-of", "json", str(result.final_path)], check=True, capture_output=True, timeout=60).stdout
    ends = [float(p["pts_time"]) + float(p["duration_time"]) for p in json.loads(packets)["packets"]]
    # The last frame is shown until exactly 266/30 s, inside the quiet interval.
    assert len(ends) == 266 and abs(max(ends) - 266 / 30) < 1e-5 and 8.830 <= max(ends) <= 8.875
    receipt = json.loads(result.receipt_json)
    assert receipt["duration_ms"] == result.final_probe.duration_ms
    from services.output_endpoint import endpoint_digest
    assert receipt["output_endpoint_sha256"] == endpoint_digest(THO_ENDPOINT)
    stored = json.loads((tmp_path / "prepared/request.json").read_text())
    assert stored["output_endpoint"] == THO_ENDPOINT


@pytest.mark.parametrize("lyric,delivery", [(False, None), (False, "in_app"), (True, "in_app")])
def test_endpoint_bounds_every_caption_path_including_the_in_app_pair(sources, tmp_path, lyric, delivery):
    body = endpoint_payload(sources["short"], SHORT_ENDPOINT, lyric=lyric, delivery=delivery)
    result = _render(sources["short"], tmp_path, body)
    assert result.decoded_video_frames == 26 and result.final_probe.duration_ms in (866, 867)
    if delivery == "in_app" and not lyric:
        burned = json.loads((tmp_path / "prepared/burned/decode.json").read_text())
        assert burned["decoded_video_frames"] == 26
        assert burned["final_probe"]["duration_ms"] in (866, 867)


def test_byte_budget_fallback_keeps_the_same_frame_bound(monkeypatch, tmp_path):
    calls = []
    final = tmp_path / "final.mp4"
    source_probe = render.MediaProbe(1080, 1920, 8859, "h264", "yuv420p", "1:1", 0)
    final_probe = dataclasses.replace(source_probe, duration_ms=8867)

    def run(args, _tools, **kwargs):
        calls.append(args)
        final.write_bytes(b"final")
        if len(calls) == 1:
            raise render.PostRenderError("artifact_too_large", "injected first encode overflow")
        return b""
    monkeypatch.setattr(render, "_run", run)
    monkeypatch.setattr(render, "_probe", lambda *_: final_probe)
    assert render._encode_final(tmp_path / "source", None, final, source_probe, render.RenderTools(),
                                in_app=True, output_frames=266) == final_probe
    assert len(calls) == 2 and "-b:v" in calls[1]
    for call in calls:
        assert call[call.index("-frames:v") + 1] == "266"
    # Without a policy the encoder command carries no frame bound at all.
    calls.clear()
    render._encode_final(tmp_path / "source", None, final, source_probe, render.RenderTools(), in_app=True)
    assert all("-frames:v" not in call for call in calls)


def test_an_overshooting_encode_is_refused_not_retried_or_accepted(monkeypatch, tmp_path):
    calls = []
    final = tmp_path / "final.mp4"
    source_probe = render.MediaProbe(1080, 1920, 8859, "h264", "yuv420p", "1:1", 0)

    def run(args, _tools, **kwargs):
        calls.append(args)
        final.write_bytes(b"final")
        return b""
    monkeypatch.setattr(render, "_run", run)
    # 8900 ms is inside the ordinary 80 ms tolerance but is not the bound endpoint.
    monkeypatch.setattr(render, "_probe", lambda *_: dataclasses.replace(source_probe, duration_ms=8900))
    with pytest.raises(render.PostRenderError) as error:
        render._encode_final(tmp_path / "source", None, final, source_probe, render.RenderTools(),
                             in_app=True, output_frames=266)
    assert error.value.code == "output_endpoint_unmet"
    assert len(calls) == 1 and not final.exists()


@pytest.mark.parametrize("probed_ms,met", [(8866, True), (8867, True), (8868, True), (8865, False), (8869, False),
                                           (8833, False), (8900, False)])
def test_only_the_container_millisecond_rounding_of_the_endpoint_is_accepted(monkeypatch, tmp_path, probed_ms, met):
    """A real 266-frame final is stored as 8.866016 s (8866) or 8.866667 s (8867)."""
    final = tmp_path / "final.mp4"
    source_probe = render.MediaProbe(1080, 1920, 8859, "h264", "yuv420p", "1:1", 0)
    monkeypatch.setattr(render, "_run", lambda *a, **k: final.write_bytes(b"final") and b"")
    monkeypatch.setattr(render, "_probe", lambda *_: dataclasses.replace(source_probe, duration_ms=probed_ms))
    encode = lambda: render._encode_final(tmp_path / "source", None, final, source_probe, render.RenderTools(),
                                          in_app=True, output_frames=266)
    if met:
        assert encode().duration_ms == probed_ms
    else:
        with pytest.raises(render.PostRenderError) as error:
            encode()
        assert error.value.code == "output_endpoint_unmet"


# --- refusals ------------------------------------------------------------------------------

def test_source_that_cannot_supply_the_frames_is_refused_without_padding(sources, tmp_path):
    with pytest.raises(render.PostRenderError) as error:
        _render(sources["two_short"], tmp_path, endpoint_payload(sources["two_short"]))
    assert error.value.code == "output_endpoint_unmet"
    assert not (tmp_path / "prepared").exists()


def test_endpoint_far_from_the_source_duration_is_not_a_cut_command(sources, tmp_path):
    cut = {**THO_ENDPOINT, "frames": 200, "interval_start_ms": 6600, "interval_end_ms": 6700}
    with pytest.raises(render.PostRenderError) as error:
        _render(sources["tho"], tmp_path, endpoint_payload(sources["tho"], cut))
    assert error.value.code == "output_endpoint_outside_source"
    assert not (tmp_path / "prepared").exists()


def test_source_with_audio_is_refused_rather_than_left_with_a_longer_track(sources, tmp_path):
    with pytest.raises(render.PostRenderError) as error:
        _render(sources["audio"], tmp_path, endpoint_payload(sources["audio"]))
    assert error.value.code == "output_endpoint_audio_unsupported"
    assert not (tmp_path / "prepared").exists()


@pytest.mark.parametrize("change", [
    {"schema": "content-lab.post-render-output-endpoint.v2"},
    {"frame_rate": "60/1"}, {"frame_rate": 30},
    {"frames": 264}, {"frames": 267}, {"frames": 0}, {"frames": True}, {"frames": 266.0}, {"frames": "266"},
    {"interval_start_ms": 8876}, {"interval_end_ms": 8829}, {"interval_start_ms": -1},
    {"interval_start_ms": 8830.0}, {"interval_end_ms": 600_001},
    {"evidence_sha256": "C9" * 32}, {"evidence_sha256": "c9" * 31}, {"evidence_sha256": None},
    {"extra": 1},
])
def test_malformed_or_out_of_interval_policy_is_rejected(change):
    endpoint = {**THO_ENDPOINT, **change}
    with pytest.raises(ValidationError):
        jobs.RenderJobSubmission.model_validate(endpoint_payload(endpoint=endpoint))


def test_both_30fps_boundaries_inside_the_interval_are_valid_and_distinct_policies():
    """8.830..8.875 s holds two 30 fps boundaries: 265 (8.833333 s) and 266 (8.866667 s).

    The Lab accepts whichever one the trusted slot names; it never picks.
    """
    from services.output_endpoint import endpoint_digest, endpoint_duration_ms
    earlier = {**THO_ENDPOINT, "frames": 265}
    for endpoint, ms in ((earlier, 8833), (THO_ENDPOINT, 8867)):
        accepted = jobs.RenderJobSubmission.model_validate(endpoint_payload(endpoint=endpoint))
        assert endpoint_duration_ms(accepted.request.output_endpoint) == ms
    assert endpoint_digest(earlier) != endpoint_digest(THO_ENDPOINT)


@pytest.mark.parametrize("missing", list(THO_ENDPOINT))
def test_policy_with_a_missing_field_is_rejected(missing):
    jobs.RenderJobSubmission.model_validate(endpoint_payload())  # the complete policy is accepted
    endpoint = {key: value for key, value in THO_ENDPOINT.items() if key != missing}
    with pytest.raises(ValidationError):
        jobs.RenderJobSubmission.model_validate(endpoint_payload(endpoint=endpoint))


@pytest.mark.parametrize("mutate", [
    # A request marker alone is not authority: the immutable slot must carry it.
    lambda body: rebind_slot(body, lambda slot: slot.pop("output_endpoint")),
    # A slot that pins an endpoint cannot be rendered by an unbounded request.
    lambda body: body["request"].pop("output_endpoint"),
    lambda body: body["request"]["output_endpoint"].update(evidence_sha256="ab" * 32),
    lambda body: rebind_slot(body, lambda slot: slot["output_endpoint"].update(frames=266.0)),
    lambda body: rebind_slot(body, lambda slot: slot.update(output_endpoint=None)),
])
def test_request_and_immutable_slot_must_carry_the_same_policy(mutate):
    body = endpoint_payload()
    mutate(body)
    with pytest.raises(ValidationError):
        jobs.RenderJobSubmission.model_validate(body)


def test_policy_cannot_ride_with_a_duration_repair_or_a_timed_request():
    from tests.test_source_duration_repair import good_command
    request = endpoint_payload()["request"]
    # The repair command is valid by itself; only the combination is refused.
    render.PostRenderRequest.model_validate(
        {key: value for key, value in request.items() if key != "output_endpoint"}
        | {"source_duration_repair": good_command()})
    with pytest.raises(ValidationError, match="cannot be combined"):
        render.PostRenderRequest.model_validate({**request, "source_duration_repair": good_command()})
    from tests.test_post_render_timed_jobs import submission as timed_submission
    timed = timed_submission()
    rebind_slot(timed, lambda slot: slot.update(output_endpoint=copy.deepcopy(THO_ENDPOINT)))
    timed["request"]["output_endpoint"] = copy.deepcopy(THO_ENDPOINT)
    with pytest.raises(ValidationError, match="output endpoint"):
        jobs.RenderJobSubmission.model_validate(timed)


# --- identity ------------------------------------------------------------------------------

def test_absent_policy_leaves_ordinary_request_bytes_and_identity_unchanged():
    body = lyric_payload()
    accepted = jobs.RenderJobSubmission.model_validate(body)
    assert "output_endpoint" not in accepted.request.model_dump(by_alias=True)
    assert "output_endpoint" not in json.loads(accepted.request.model_dump_json(by_alias=True))
    assert accepted.model_dump(by_alias=True) == body


def test_policy_is_a_new_job_and_cannot_relabel_the_ready_unbounded_artifact(sources, tmp_path):
    source = sources["short"]
    old = jobs.RenderJobSubmission.model_validate(endpoint_payload(source, endpoint=None))
    new = jobs.RenderJobSubmission.model_validate(endpoint_payload(source, SHORT_ENDPOINT))
    assert old.request.slot_id == new.request.slot_id
    assert old.request.slot_payload_sha256 != new.request.slot_payload_sha256
    service = jobs.PostRenderJobs(tmp_path / "jobs", clock_ms=lambda: NOW,
                                  fetcher=lambda _request, target: shutil.copyfile(source, target))
    old_job = service.enqueue(old, "endpoint-old-key")
    assert service.run_one() and service.status(old_job["id"])["state"] == "succeeded"
    old_receipt = service.artifact(old_job["id"], "receipt").read_bytes()
    # Unbounded, the 0.884 s source runs on to 28 frames.
    assert json.loads(old_receipt)["duration_ms"] == 933
    with pytest.raises(jobs.RenderJobError) as error:
        service.enqueue(new, "endpoint-old-key")
    assert error.value.code == "idempotency_conflict"
    new_job = service.enqueue(new, "endpoint-new-key")
    assert new_job["id"] != old_job["id"] and new_job["state"] == "queued"
    assert service.run_one() and service.status(new_job["id"])["state"] == "succeeded"
    receipt = json.loads(service.artifact(new_job["id"], "receipt").read_bytes())
    assert receipt["duration_ms"] in (866, 867)
    assert receipt["final_sha256"] != json.loads(old_receipt)["final_sha256"]
    # The earlier artifact is untouched and still served under its own job.
    assert service.artifact(old_job["id"], "receipt").read_bytes() == old_receipt


def test_durable_authority_refuses_a_receipt_or_frame_count_that_drops_the_policy(sources, tmp_path):
    source = sources["short"]
    accepted = jobs.RenderJobSubmission.model_validate(endpoint_payload(source, SHORT_ENDPOINT))
    service = jobs.PostRenderJobs(tmp_path / "jobs", clock_ms=lambda: NOW,
                                  fetcher=lambda _request, target: shutil.copyfile(source, target))
    job = service.enqueue(accepted, "endpoint-authority")
    assert service.run_one() and service.status(job["id"])["state"] == "succeeded"
    row = service._row(job["id"])
    authority = service._load_output_authority(row)
    service._validate_output_authority(row, copy.deepcopy(authority))

    def refused(mutate):
        changed = copy.deepcopy(authority)
        mutate(changed)
        with pytest.raises(jobs.RenderJobError) as error:
            service._validate_output_authority(row, changed)
        return error.value.code
    assert refused(lambda a: a["receipt"].pop("output_endpoint_sha256")) == "receipt_binding_mismatch"
    assert refused(lambda a: a["receipt"].update(output_endpoint_sha256="0" * 64)) == "receipt_binding_mismatch"
    assert refused(lambda a: a["decode"].update(decoded_video_frames=27)) == "receipt_binding_mismatch"

    def overshoot(a):
        a["receipt"]["duration_ms"] = 933
        a["decode"]["final_probe"]["duration_ms"] = 933
    assert refused(overshoot) == "receipt_binding_mismatch"

    def two_ms_early(a):
        a["receipt"]["duration_ms"] = 865
        a["decode"]["final_probe"]["duration_ms"] = 865
    assert refused(two_ms_early) == "receipt_binding_mismatch"

    # And an ordinary job's receipt may not claim a policy it never had.
    plain = jobs.RenderJobSubmission.model_validate(
        endpoint_payload(source, endpoint=None, delivery=None))
    plain_job = service.enqueue(plain, "endpoint-plain")
    assert service.run_one() and service.status(plain_job["id"])["state"] == "succeeded"
    plain_row = service._row(plain_job["id"])
    forged = service._load_output_authority(plain_row)
    forged["receipt"]["output_endpoint_sha256"] = "0" * 64
    with pytest.raises(jobs.RenderJobError) as error:
        service._validate_output_authority(plain_row, forged)
    assert error.value.code == "receipt_binding_mismatch"
