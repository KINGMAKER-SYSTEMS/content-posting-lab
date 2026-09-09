import dataclasses
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageStat
from pydantic import ValidationError

from services import post_render as render
from services.source_treatment import normalized_visual_treatment

NOW = 1_800_000_000_000
STYLE = {"font": "TikTokSans16pt-Bold.ttf", "size_pt": 24, "color": "#ffffff",
         "outline": "#000000", "position": "middle", "align": "center", "case": "as_written",
         "background": "none", "offset_pct": 0, "line_balance": 0}
TREATMENT = {"stylePreset": "treated-source-test", "filters": {"brightness": 0.85, "saturation": 0.5},
             "captionStyle": STYLE, "clipSpeed": 1.5, "clipCrop": {"zoom": 1, "focusX": 0.5, "focusY": 0.5}}


def request(source_sha="a" * 64, **changes):
    treatment = json.dumps(TREATMENT, sort_keys=True, separators=(",", ":"))
    visual_json = json.dumps(normalized_visual_treatment(TREATMENT), sort_keys=True, separators=(",", ":"))
    payload = {"schema": render.REQUEST_SCHEMA, "slot_id": "slot:page-a:20260908T120000Z",
               "slot_payload_sha256": "b" * 64, "page_id": "page-a", "program_id": "playlist:fixture",
               "device_serial": "fixture-only", "account": "fixture.account", "source_sha256": source_sha,
               "caption": "already treated", "caption_sha256": render.sha256(b"already treated"),
               "render_treatment_json": treatment, "treatment_sha256": render.sha256(treatment.encode()),
               "source_visual_treatment_json": visual_json,
               "source_visual_treatment_sha256": render.sha256(visual_json.encode()),
               "renderer_id": render.RENDERER_ID, "renderer_version": render.RENDERER_VERSION,
               "created_at_ms": NOW - 1000}
    payload.update(changes)
    return render.PostRenderRequest.model_validate(payload)


@pytest.fixture(scope="module", params=[
    ("1080x1920", "1"), ("606x1080", "1"), ("606x1080", "0"), ("1080x1920", "0"),
], ids=["full", "provider-crop", "provider-crop-unspecified-sar", "full-unspecified-sar"])
def actual_source(tmp_path_factory, request):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe are required for actual render proof")
    root = tmp_path_factory.mktemp("post-render-actual")
    path = root / "source.mp4"
    # The source already has nonneutral grade and 1.5x speed; renderer must not repeat either.
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    f"testsrc2=size={request.param[0]}:rate=30:duration=1.2", "-vf",
                    f"eq=brightness=-0.15:saturation=0.5,setpts=PTS/1.5,setsar={request.param[1]}", "-an", "-c:v", "libx264",
                    "-preset", "ultrafast", "-crf", "20", "-threads", "2", "-pix_fmt", "yuv420p",
                    "-r", "30", str(path)], check=True, timeout=30, capture_output=True)
    return path


def test_real_caption_delivery_decodes_and_preserves_already_applied_treatment(actual_source, tmp_path):
    result = render.render_post(actual_source, tmp_path / "render", request(render.sha256(actual_source.read_bytes())), clock_ms=lambda: NOW)
    receipt = json.loads(result.receipt_json)
    assert result.receipt_path.read_bytes() == result.receipt_json
    assert result.receipt_sha256 == render.sha256(result.receipt_json)
    assert receipt["final_sha256"] == render.sha256(result.final_path.read_bytes())
    assert receipt["qa_frame_sha256"] == render.sha256(result.qa_frame_path.read_bytes())
    assert receipt["final_sha256"] != receipt["source_sha256"]
    assert receipt["final_byte_length"] == result.final_path.stat().st_size <= render.MAX_FINAL_BYTES
    assert result.qa_frame_path.stat().st_size <= render.MAX_QA_BYTES
    assert (receipt["width"], receipt["height"]) == (1080, 1920)
    assert result.decoded_video_frames > 0
    assert abs(result.source_probe.duration_ms - result.final_probe.duration_ms) <= 80
    assert result.final_probe.video_codec == "h264"
    assert result.final_probe.sample_aspect_ratio == "1:1"
    assert not (result.final_path.parent / "source.mp4").exists()
    with Image.open(result.qa_frame_path) as qa:
        qa.load()
        assert qa.size == (1080, 1920)
    # Compare the same source frame outside the caption area. Reapplying grade would shift these pixels.
    reference = tmp_path / "reference.png"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{result.qa_at_ms / 1000:.3f}",
                    "-i", str(actual_source), "-vf", "scale=1080:1920:flags=lanczos,setsar=1",
                    "-frames:v", "1", "-update", "1", str(reference)],
                   check=True, timeout=20, capture_output=True)
    with Image.open(reference) as before, Image.open(result.qa_frame_path) as after:
        difference = ImageChops.difference(before.convert("RGB").crop((150, 100, 930, 300)),
                                           after.convert("RGB").crop((150, 100, 930, 300)))
        assert max(ImageStat.Stat(difference).mean) < 5
        # The actual video has visible caption pixels, independently of the receipt.
        caption_difference = ImageChops.difference(before.convert("RGB").crop((250, 900, 830, 1030)),
                                                   after.convert("RGB").crop((250, 900, 830, 1030)))
        assert max(ImageStat.Stat(caption_difference).mean) > 6


@pytest.mark.parametrize("applied", [None, "different"])
def test_unknown_or_different_treatment_requires_regeneration_before_any_io(tmp_path, applied):
    output = tmp_path / "render"
    actual = None if applied is None else json.dumps(normalized_visual_treatment({**TREATMENT, "clipSpeed": 2}))
    payload = request(source_visual_treatment_json=actual,
                      source_visual_treatment_sha256=None if actual is None else render.sha256(actual.encode()))
    with pytest.raises(render.PostRenderError) as error:
        render.render_post(tmp_path / "missing", output, payload, clock_ms=lambda: NOW)
    assert error.value.code == "regeneration_required"
    assert not output.exists()


def test_wrong_source_hash_cleans_only_new_output(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"wrong source")
    with pytest.raises(render.PostRenderError) as error:
        render.render_post(source, tmp_path / "render", request(), clock_ms=lambda: NOW)
    assert error.value.code == "source_sha256_mismatch"
    assert source.read_bytes() == b"wrong source"
    assert not (tmp_path / "render").exists()


def test_existing_output_is_not_overwritten(tmp_path):
    output = tmp_path / "render"
    output.mkdir()
    (output / "receipt.json").write_text("existing")
    with pytest.raises(FileExistsError):
        render.render_post(tmp_path / "missing", output, request(), clock_ms=lambda: NOW)
    assert (output / "receipt.json").read_text() == "existing"


@pytest.mark.parametrize("field,value", [("unknown", True), ("caption_sha256", "c" * 64),
    ("treatment_sha256", "d" * 64), ("created_at_ms", True), ("source_sha256", "A" * 64),
    ("account", " "), ("renderer_version", "other")])
def test_request_rejects_bad_binding_and_unknown_fields(field, value):
    with pytest.raises(ValidationError):
        request(**{field: value})


def test_request_is_immutable_and_rejects_treatment_schema_drift():
    payload = request()
    with pytest.raises(ValidationError):
        payload.caption = "mutated"
    treatment = json.dumps({**TREATMENT, "unknown": True})
    with pytest.raises(ValidationError):
        request(render_treatment_json=treatment, treatment_sha256=render.sha256(treatment.encode()))


def test_future_request_does_not_create_output(tmp_path):
    with pytest.raises(render.PostRenderError) as error:
        render.render_post(tmp_path / "missing", tmp_path / "render", request(created_at_ms=NOW + 1), clock_ms=lambda: NOW)
    assert error.value.code == "request_future_dated"
    assert not (tmp_path / "render").exists()


def test_subprocess_timeout_and_inherited_pipe_are_bounded():
    started = time.monotonic()
    code = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])"
    with pytest.raises(render.PostRenderError) as error:
        render._run([sys.executable, "-c", code], render.RenderTools(process_timeout_s=0.15))
    assert error.value.code == "process_timeout"
    assert time.monotonic() - started < 2


def test_subprocess_output_overflow_and_nonzero_exit_have_distinct_failures():
    with pytest.raises(render.PostRenderError) as error:
        render._run([sys.executable, "-c", "import sys;sys.stderr.write('x'*1000000)"], render.RenderTools(process_output_limit=2000))
    assert error.value.code == "process_output_limit"
    with pytest.raises(render.PostRenderError) as error:
        render._run([sys.executable, "-c", "raise SystemExit(7)"], render.RenderTools())
    assert error.value.code == "process_failed"


@pytest.mark.parametrize("raw", [b"not JSON", b'{"streams":[],"format":{}}',
    b'{"streams":[{"codec_type":"video"}],"format":{"duration":"NaN"}}'])
def test_malformed_media_probe_cannot_become_evidence(monkeypatch, tmp_path, raw):
    monkeypatch.setattr(render, "_run", lambda *_args, **_kwargs: raw)
    with pytest.raises(render.PostRenderError) as error:
        render._probe(tmp_path / "final", render.RenderTools())
    assert error.value.code == "media_probe_invalid"


def test_size_retry_changes_encoding_only_and_is_bounded_to_two_attempts(monkeypatch, tmp_path):
    calls = []
    final = tmp_path / "final.mp4"
    probe = render.MediaProbe(1080, 1920, 8000, "h264", "yuv420p", "1:1", 0)
    def run(args, _tools, **kwargs):
        calls.append(args)
        final.write_bytes(b"final")
        if len(calls) == 1:
            raise render.PostRenderError("artifact_too_large", "injected first encode overflow")
        return b""
    monkeypatch.setattr(render, "_run", run)
    monkeypatch.setattr(render, "_probe", lambda *_: probe)
    assert render._encode_final(tmp_path / "source", tmp_path / "overlay", final, probe, render.RenderTools()) == probe
    assert len(calls) == 2
    assert "-crf" in calls[0] and "-crf" not in calls[1]
    assert "-b:v" in calls[1]
    assert calls[0][calls[0].index("-filter_complex") + 1] == calls[1][calls[1].index("-filter_complex") + 1]
    calls.clear()
    monkeypatch.setattr(render, "_probe", lambda *_: dataclasses.replace(probe, duration_ms=4000))
    with pytest.raises(render.PostRenderError) as error:
        render._encode_final(tmp_path / "source", tmp_path / "overlay", final, probe, render.RenderTools())
    assert error.value.code == "delivery_budget_exceeded"
    assert len(calls) == 2
    assert not final.exists()


@pytest.mark.parametrize("geometry", [
    (1920, 1080, "1:1", 0), (600, 1080, "1:1", 0),
    (606, 1080, "2:1", 0), (1080, 1920, "1:1", 90),
    (304, 540, "1:1", 0),
])
def test_wrong_source_geometry_fails_without_compositing(monkeypatch, tmp_path, geometry):
    source = tmp_path / "source"
    source.write_bytes(b"verified fixture")
    monkeypatch.setattr(render, "_probe", lambda *_: render.MediaProbe(geometry[0], geometry[1], 1000, "h264", "yuv420p", geometry[2], geometry[3]))
    with pytest.raises(render.PostRenderError) as error:
        render.render_post(source, tmp_path / "render", request(render.sha256(source.read_bytes())), clock_ms=lambda: NOW)
    assert error.value.code == "regeneration_required"
    assert not (tmp_path / "render").exists()


def test_failed_complete_decode_cannot_leave_a_ready_receipt(actual_source, monkeypatch, tmp_path):
    original_run = render._run
    def run(args, tools, **kwargs):
        if "-progress" in args:
            return b"frame=0\nprogress=continue\n"
        return original_run(args, tools, **kwargs)
    monkeypatch.setattr(render, "_run", run)
    with pytest.raises(render.PostRenderError) as error:
        render.render_post(actual_source, tmp_path / "render", request(render.sha256(actual_source.read_bytes())), clock_ms=lambda: NOW)
    assert error.value.code == "final_decode_failed"
    assert not (tmp_path / "render").exists()
    assert actual_source.is_file()


def test_caption_only_change_reuses_source_video_treatment():
    original = request()
    changed = json.loads(original.render_treatment_json)
    changed["captionStyle"]["position"] = "top"
    raw = json.dumps(changed, sort_keys=True, separators=(",", ":"))
    new_request = request(render_treatment_json=raw, treatment_sha256=render.sha256(raw.encode()))
    assert original.treatment_sha256 != new_request.treatment_sha256
    assert render.source_visual_matches(new_request)
