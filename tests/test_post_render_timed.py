import json
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from services import post_render as v1
from services import post_render_timed as rt
from services.source_treatment import normalized_visual_treatment

NOW = 1_800_000_000_000
STYLE = {"font": "TikTokSans16pt-Bold.ttf", "size_pt": 24, "color": "#ffffff",
         "outline": "#000000", "position": "middle", "align": "center", "case": "as_written",
         "background": "none", "offset_pct": 0, "line_balance": 0}
TREATMENT = {"stylePreset": "treated-source-test", "filters": {"brightness": 0.85},
             "captionStyle": STYLE, "clipSpeed": 1, "clipCrop": {"zoom": 1, "focusX": 0.5, "focusY": 0.5}}

SET_ID = "acct:burnt-mill-rd"
PAGE_ID = "acct:burnt-mill-rd"
ACCOUNT = "burnt.mill.rd"
SOUND_ID = "7611482162773035025"
TIMING_SHA = "f" * 64
CANONICAL = "one two three four five six seven"
PHRASES = [
    {"startMs": 0, "endMs": 1000, "text": "one"},
    {"startMs": 2000, "endMs": 3000, "text": "two"},
    {"startMs": 4000, "endMs": 5000, "text": "three"},
    {"startMs": 6000, "endMs": 7000, "text": "four"},
    {"startMs": 8000, "endMs": 9000, "text": "five"},
    {"startMs": 10000, "endMs": 11000, "text": "six"},
    {"startMs": 12000, "endMs": 13000, "text": "seven"},
]

TIMING = {"id": SET_ID, "pageId": PAGE_ID, "account": ACCOUNT, "soundId": SOUND_ID,
          "durationMs": 20_000, "phrases": PHRASES, "canonicalCaption": CANONICAL,
          "sha256": TIMING_SHA, "sourceProgramId": "fixture-program", "sourceProgramSha256": "d" * 64}


def request(source_sha="a" * 64, caption=CANONICAL, **changes):
    treatment = json.dumps(TREATMENT, sort_keys=True, separators=(",", ":"))
    visual_json = json.dumps(normalized_visual_treatment(TREATMENT), sort_keys=True, separators=(",", ":"))
    payload = {"schema": rt.REQUEST_SCHEMA_V2, "slot_id": "slot:page-a:20260908T120000Z",
               "slot_payload_sha256": "b" * 64, "page_id": PAGE_ID, "program_id": "playlist:fixture",
               "device_serial": "fixture-only", "account": ACCOUNT, "source_sha256": source_sha,
               "caption": caption, "caption_sha256": v1.sha256(caption.encode()),
               "render_treatment_json": treatment, "treatment_sha256": v1.sha256(treatment.encode()),
               "source_visual_treatment_json": visual_json,
               "source_visual_treatment_sha256": v1.sha256(visual_json.encode()),
               "renderer_id": rt.RENDERER_ID, "renderer_version": rt.RENDERER_VERSION_V2,
               "lyric_timing_set_id": SET_ID, "lyric_timing_sha256": TIMING_SHA, "sound_id": SOUND_ID,
               "source_program_id": "fixture-program", "source_program_sha256": "d" * 64, "created_at_ms": NOW - 1000}
    payload.update(changes)
    return rt.TimedPostRenderRequest.model_validate(payload)


@pytest.fixture(scope="module")
def actual_source(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe are required for actual render proof")
    root = tmp_path_factory.mktemp("timed-actual")
    path = root / "source.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=0x808080:size=1080x1920:rate=30:duration=20", "-an",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                    "-threads", "2", "-pix_fmt", "yuv420p", "-r", "30", str(path)],
                   check=True, timeout=60, capture_output=True)
    return path


@pytest.fixture(scope="module")
def actual_source_15s(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe are required for actual render proof")
    root = tmp_path_factory.mktemp("timed-actual-15s")
    path = root / "source.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=0x808080:size=1080x1920:rate=30:duration=15", "-an",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                    "-threads", "2", "-pix_fmt", "yuv420p", "-r", "30", str(path)],
                   check=True, timeout=60, capture_output=True)
    return path


def _patch_resolver(monkeypatch, result):
    calls = []
    def resolver(set_id, *, page_id, account, sound_id, expected_sha256):
        calls.append((set_id, page_id, account, sound_id, expected_sha256))
        return result
    monkeypatch.setattr(rt, "resolve_lyric_timing", resolver)
    return calls


def test_valid_timed_render_is_silent_20s_with_phrase_evidence(actual_source, tmp_path, monkeypatch):
    _patch_resolver(monkeypatch, TIMING)
    out = tmp_path / "render"
    req = request(source_sha=v1.sha256(actual_source.read_bytes()))
    result = rt.render_timed_post(actual_source, out, req, clock_ms=lambda: NOW)
    receipt = json.loads(result.receipt_json)
    assert result.receipt_path.read_bytes() == result.receipt_json
    assert result.receipt_sha256 == v1.sha256(result.receipt_json)
    assert receipt["schema"] == rt.RECEIPT_SCHEMA_V2
    assert receipt["renderer_version"] == "2"
    assert receipt["lyric_timing_set_id"] == SET_ID
    assert receipt["lyric_timing_sha256"] == TIMING_SHA
    assert receipt["sound_id"] == SOUND_ID
    assert receipt["final_sha256"] == v1.sha256(result.final_path.read_bytes())
    assert receipt["qa_frame_sha256"] == v1.sha256(result.qa_frame_path.read_bytes())
    assert receipt["final_sha256"] != receipt["source_sha256"]
    assert receipt["final_byte_length"] == result.final_path.stat().st_size <= rt.MAX_FINAL_BYTES
    assert (receipt["width"], receipt["height"]) == (1080, 1920)
    assert result.final_probe.video_codec == "h264"
    assert result.final_probe.pixel_format == "yuv420p"
    assert result.final_probe.sample_aspect_ratio == "1:1"
    assert abs(result.final_probe.duration_ms - 20_000) <= 100
    assert result.decoded_video_frames > 0
    assert len(receipt["phrase_frame_evidence"]) == 4
    assert all(e["maxMeanGlyphError"] <= 35 for e in receipt["phrase_frame_evidence"])
    assert result.decoded_video_frames == 600
    assert len(receipt["phrases"]) == 7
    for index, phrase in enumerate(receipt["phrases"], start=1):
        png = out / f"phrase-{index}.png"
        assert png.is_file()
        assert phrase["pngSha256"] == v1.sha256(png.read_bytes())
        assert phrase["text"] == PHRASES[index - 1]["text"]
    assert not (out / "source.mp4").exists()
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json",
                            str(result.final_path)], check=True, capture_output=True, text=True, timeout=30)
    assert all(stream.get("codec_type") != "audio" for stream in json.loads(probe.stdout)["streams"])


def test_non_20s_duration_derives_final_duration_and_frames(actual_source_15s, tmp_path, monkeypatch):
    # MED-1 guard: the renderer must derive -t / bitrate / decoded-frame count
    # from timing["durationMs"], not a compiled-in 20s/600 shape.
    duration_ms = 15_000
    phrases = [
        {"startMs": 0, "endMs": 3000, "text": "one"},
        {"startMs": 4000, "endMs": 7000, "text": "two"},
        {"startMs": 8000, "endMs": 11000, "text": "three"},
        {"startMs": 12000, "endMs": 15000, "text": "four"},
    ]
    caption = "one two three four"
    _patch_resolver(monkeypatch, {**TIMING, "durationMs": duration_ms,
                                  "phrases": phrases, "canonicalCaption": caption})
    out = tmp_path / "render"
    req = request(source_sha=v1.sha256(actual_source_15s.read_bytes()), caption=caption)
    result = rt.render_timed_post(actual_source_15s, out, req, clock_ms=lambda: NOW)
    assert abs(result.final_probe.duration_ms - duration_ms) <= 100
    assert result.decoded_video_frames == duration_ms * 30 // 1000
    assert result.decoded_video_frames != 600



@pytest.mark.parametrize("overrides,code", [
    ({"id": "other-set"}, "lyric_timing_set_mismatch"),
    ({"pageId": "other-page"}, "lyric_timing_page_mismatch"),
    ({"account": "other.account"}, "lyric_timing_account_mismatch"),
    ({"soundId": "999"}, "lyric_timing_sound_mismatch"),
    ({"sha256": "e" * 64}, "lyric_timing_sha256_mismatch"),
    ({"canonicalCaption": "different caption"}, "lyric_caption_mismatch"),
])
def test_binding_mismatches_fail_before_encoding(tmp_path, monkeypatch, overrides, code):
    _patch_resolver(monkeypatch, {**TIMING, **overrides})
    output = tmp_path / "render"
    with pytest.raises(rt.PostRenderError) as error:
        rt.render_timed_post(tmp_path / "missing", output, request(), clock_ms=lambda: NOW)
    assert error.value.code == code
    assert not output.exists()


def test_missing_or_overlapping_phrases_fail_before_encoding(tmp_path, monkeypatch):
    _patch_resolver(monkeypatch, {**TIMING, "phrases": []})
    with pytest.raises(rt.PostRenderError) as error:
        rt.render_timed_post(tmp_path / "missing", tmp_path / "empty", request(), clock_ms=lambda: NOW)
    assert error.value.code == "lyric_phrases_invalid"
    assert not (tmp_path / "empty").exists()

    bad = [dict(p) for p in PHRASES]
    bad[1]["startMs"] = 500
    _patch_resolver(monkeypatch, {**TIMING, "phrases": bad})
    with pytest.raises(rt.PostRenderError) as error:
        rt.render_timed_post(tmp_path / "missing", tmp_path / "overlap", request(), clock_ms=lambda: NOW)
    assert error.value.code == "lyric_phrases_overlap"
    assert not (tmp_path / "overlap").exists()

    _patch_resolver(monkeypatch, None)
    with pytest.raises(rt.PostRenderError) as error:
        rt.render_timed_post(tmp_path / "missing", tmp_path / "missing", request(), clock_ms=lambda: NOW)
    assert error.value.code == "lyric_timing_invalid"
    assert not (tmp_path / "missing").exists()


def test_unknown_or_different_treatment_requires_regeneration_before_resolver(tmp_path, monkeypatch):
    calls = _patch_resolver(monkeypatch, TIMING)
    output = tmp_path / "render"
    payload = request(source_visual_treatment_json=None, source_visual_treatment_sha256=None)
    with pytest.raises(rt.PostRenderError) as error:
        rt.render_timed_post(tmp_path / "missing", output, payload, clock_ms=lambda: NOW)
    assert error.value.code == "regeneration_required"
    assert not calls
    assert not output.exists()


def test_wrong_source_hash_cleans_only_new_output(tmp_path, monkeypatch):
    _patch_resolver(monkeypatch, TIMING)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"wrong source")
    with pytest.raises(rt.PostRenderError) as error:
        rt.render_timed_post(source, tmp_path / "render", request(), clock_ms=lambda: NOW)
    assert error.value.code == "source_sha256_mismatch"
    assert source.read_bytes() == b"wrong source"
    assert not (tmp_path / "render").exists()


def test_existing_output_is_not_overwritten(tmp_path, monkeypatch):
    _patch_resolver(monkeypatch, TIMING)
    output = tmp_path / "render"
    output.mkdir()
    (output / "receipt.json").write_text("existing")
    with pytest.raises(FileExistsError):
        rt.render_timed_post(tmp_path / "missing", output, request(), clock_ms=lambda: NOW)
    assert (output / "receipt.json").read_text() == "existing"


def test_timed_request_is_frozen_and_rejects_v1_and_unknown_fields():
    with pytest.raises(ValidationError):
        request(unknown_field=True)
    with pytest.raises(ValidationError):
        request(renderer_version="1")
    with pytest.raises(ValidationError):
        request(sound_id=" ")
    with pytest.raises(ValidationError):
        request(lyric_timing_sha256="A" * 64)
    payload = request()
    with pytest.raises(ValidationError):
        payload.sound_id = "other"


def test_unsupported_frame_treatment_is_refused_before_encoding(tmp_path, monkeypatch):
    _patch_resolver(monkeypatch, TIMING)
    treatment = {**TREATMENT, "frame": "16:9"}
    raw = json.dumps(treatment, sort_keys=True, separators=(",", ":"))
    with pytest.raises(ValidationError):
        request(render_treatment_json=raw, treatment_sha256=v1.sha256(raw.encode()))
    # Explicit vertical full-bleed stays legal.
    raw_vertical = json.dumps({**TREATMENT, "frame": "9:16"}, sort_keys=True, separators=(",", ":"))
    request(render_treatment_json=raw_vertical, treatment_sha256=v1.sha256(raw_vertical.encode()))


def test_missing_actual_overlay_is_rejected_by_decoded_cue_evidence(actual_source, tmp_path, monkeypatch):
    _patch_resolver(monkeypatch, TIMING)
    def omit_overlay(source, pngs, phrases, final, probe, tools, target_duration_ms):
        shutil.copyfile(source, final)
        return probe
    monkeypatch.setattr(rt, "_encode_timed_final", omit_overlay)
    request_value = request(source_sha=v1.sha256(actual_source.read_bytes()))
    with pytest.raises(rt.PostRenderError) as error:
        rt.render_timed_post(actual_source, tmp_path / "missing-overlay", request_value, clock_ms=lambda: NOW)
    assert error.value.code == "phrase_frame_evidence_invalid"
    assert not (tmp_path / "missing-overlay").exists()


def test_in_app_timed_render_is_refused_before_the_resolver(tmp_path, monkeypatch):
    calls = _patch_resolver(monkeypatch, TIMING)
    treatment = {**TREATMENT, "captionDelivery": "in_app"}
    raw = json.dumps(treatment, sort_keys=True, separators=(",", ":"))
    req = request(render_treatment_json=raw, treatment_sha256=v1.sha256(raw.encode()))
    output = tmp_path / "render"
    with pytest.raises(rt.PostRenderError) as error:
        rt.render_timed_post(tmp_path / "missing", output, req, clock_ms=lambda: NOW)
    assert error.value.code == "in_app_timed_unsupported"
    assert not calls
    assert not output.exists()
