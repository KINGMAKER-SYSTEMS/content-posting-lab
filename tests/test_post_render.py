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
from services import page_frame
from services.source_treatment import normalized_visual_treatment

NOW = 1_800_000_000_000
STYLE = {"font": "TikTokSans16pt-Bold.ttf", "size_pt": 24, "color": "#ffffff",
         "outline": "#000000", "position": "middle", "align": "center", "case": "as_written",
         "background": "none", "offset_pct": 0, "line_balance": 0}
TREATMENT = {"stylePreset": "treated-source-test", "filters": {"brightness": 0.85, "saturation": 0.5},
             "captionStyle": STYLE, "clipSpeed": 1.5, "clipCrop": {"zoom": 1, "focusX": 0.5, "focusY": 0.5}}


_NO_FRAME = object()


def request(source_sha="a" * 64, frame=_NO_FRAME, **changes):
    slot_treatment = TREATMENT if frame is _NO_FRAME else {**TREATMENT, "frame": frame}
    treatment = json.dumps(slot_treatment, sort_keys=True, separators=(",", ":"))
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
    ("704x1280", "1"),
], ids=["full", "provider-crop", "provider-crop-unspecified-sar", "full-unspecified-sar",
        "wan-near-vertical"])
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
    reference_filter = "scale=1080:1920:flags=lanczos,setsar=1"
    if actual_source.name and result.source_probe.width == 704:
        reference_filter = ("scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,"
                            "crop=1080:1920,setsar=1")
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{result.qa_at_ms / 1000:.3f}",
                    "-i", str(actual_source), "-vf", reference_filter,
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
    (1920, 1080, "1:1", 0), (560, 1080, "1:1", 0),
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


# Page frame: a delivery-only letterbox on the same 1080x1920 canvas.
# A real committed 1080x1920 H.264 portrait clip, not a synthetic pattern.
REAL_PORTRAIT = Path(__file__).parents[1] / "artifacts/visual-admission-evidence/portrait-7s.mp4"
FRAME_BANDS = {"16:9": 608, "1:1": 1080, "3:4": 1440, "4:3": 810}


@pytest.fixture(scope="module")
def real_portrait():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe are required for actual render proof")
    assert REAL_PORTRAIT.is_file()
    return REAL_PORTRAIT


def _luma_rows(image, top, bottom):
    return ImageStat.Stat(image.convert("L").crop((0, top, 1080, bottom)))


@pytest.mark.parametrize("frame", list(FRAME_BANDS))
def test_page_frame_letterboxes_the_centred_band_in_plain_black(real_portrait, tmp_path, frame):
    height = FRAME_BANDS[frame]
    top = ((1920 - height) // 2) & ~1
    bottom = top + height
    result = render.render_post(real_portrait, tmp_path / "render",
                                request(render.sha256(real_portrait.read_bytes()), frame=frame), clock_ms=lambda: NOW)
    receipt = json.loads(result.receipt_json)
    assert (receipt["width"], receipt["height"]) == (1080, 1920)
    assert (result.final_probe.width, result.final_probe.height, result.final_probe.video_codec,
            result.final_probe.pixel_format, result.final_probe.sample_aspect_ratio) == (1080, 1920, "h264", "yuv420p", "1:1")
    assert result.decoded_video_frames > 0
    assert receipt["treatment_sha256"] == render.sha256(json.dumps(
        {**TREATMENT, "frame": frame}, sort_keys=True, separators=(",", ":")).encode())
    # Sample a second decoded frame from the final MP4 besides the QA frame.
    late = tmp_path / "late.png"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", "6.0", "-i", str(result.final_path),
                    "-frames:v", "1", "-update", "1", str(late)], check=True, timeout=30, capture_output=True)
    reference = tmp_path / "reference.png"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{result.qa_at_ms / 1000:.3f}",
                    "-i", str(real_portrait), "-frames:v", "1", "-update", "1", str(reference)],
                   check=True, timeout=30, capture_output=True)
    for path in (result.qa_frame_path, late):
        with Image.open(path) as decoded:
            decoded.load()
            assert decoded.size == (1080, 1920)
            # Margin keeps JPEG ringing and 4:2:0 row alignment out of the samples.
            above, below = _luma_rows(decoded, 0, top - 4), _luma_rows(decoded, bottom + 4, 1920)
            assert above.mean[0] < 16 and below.mean[0] < 16
            assert above.extrema[0][1] < 40 and below.extrema[0][1] < 40
            assert _luma_rows(decoded, top + 4, bottom - 4).mean[0] > 60
    with Image.open(reference) as before, Image.open(result.qa_frame_path) as after:
        # The band holds the source's own centred rows: no scaling, no shift.
        strip = (0, top + 8, 1080, top + 72)
        difference = ImageChops.difference(before.convert("RGB").crop(strip), after.convert("RGB").crop(strip))
        assert max(ImageStat.Stat(difference).mean) < 5
        # The source is not black where the bars now are.
        assert _luma_rows(before, 0, top - 4).mean[0] > 60


def test_absent_and_vertical_frame_render_identical_final_bytes(real_portrait, tmp_path):
    sha = render.sha256(real_portrait.read_bytes())
    absent = render.render_post(real_portrait, tmp_path / "absent", request(sha), clock_ms=lambda: NOW)
    vertical = render.render_post(real_portrait, tmp_path / "vertical", request(sha, frame="9:16"), clock_ms=lambda: NOW)
    assert absent.final_path.read_bytes() == vertical.final_path.read_bytes()
    assert json.loads(absent.receipt_json)["treatment_sha256"] != json.loads(vertical.receipt_json)["treatment_sha256"]
    with Image.open(absent.qa_frame_path) as qa:
        assert _luma_rows(qa, 0, 200).mean[0] > 60


@pytest.mark.parametrize("probe,expected", [
    ((1080, 1920, "1:1"), "[0:v:0][1:v:0]overlay=0:0:format=auto[v]"),
    ((1080, 1920, "N/A"), "[0:v:0]setsar=1[delivery];[delivery][1:v:0]overlay=0:0:format=auto[v]"),
    ((606, 1080, "1:1"), "[0:v:0]scale=1080:1920:flags=lanczos,setsar=1[delivery];"
                         "[delivery][1:v:0]overlay=0:0:format=auto[v]"),
    ((704, 1280, "1:1"), "[0:v:0]scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,"
                         "crop=1080:1920,setsar=1[delivery];[delivery][1:v:0]overlay=0:0:format=auto[v]"),
])
@pytest.mark.parametrize("frame", [_NO_FRAME, "9:16"])
def test_no_page_frame_keeps_the_existing_delivery_graph(monkeypatch, tmp_path, probe, expected, frame):
    calls = []
    source_probe = render.MediaProbe(probe[0], probe[1], 1000, "h264", "yuv420p", probe[2], 0)
    monkeypatch.setattr(render, "_run", lambda args, *_a, **_k: calls.append(args) or b"")
    monkeypatch.setattr(render, "_probe", lambda *_: render.MediaProbe(1080, 1920, 1000, "h264", "yuv420p", "1:1", 0))
    (tmp_path / "final.mp4").write_bytes(b"final")
    band = render._frame_band_height(request(frame=frame))
    assert band is None
    render._encode_final(tmp_path / "source", tmp_path / "overlay", tmp_path / "final.mp4", source_probe,
                         render.RenderTools(), band)
    assert calls[0][calls[0].index("-filter_complex") + 1] == expected


@pytest.mark.parametrize("frame,chain", [
    ("16:9", "crop=1080:608:0:656,pad=1080:1920:0:656:black"),
    ("1:1", "crop=1080:1080:0:420,pad=1080:1920:0:420:black"),
    ("3:4", "crop=1080:1440:0:240,pad=1080:1920:0:240:black"),
    ("4:3", "crop=1080:810:0:554,pad=1080:1920:0:554:black"),
])
def test_page_frame_letterbox_follows_delivery_normalization(frame, chain):
    band = render._frame_band_height(request(frame=frame))
    assert page_frame.letterbox_filter(band) == chain
    exact = render.MediaProbe(1080, 1920, 1000, "h264", "yuv420p", "1:1", 0)
    assert render._video_graph(exact, band) == (
        f"[0:v:0]{chain}[delivery];[delivery][1:v:0]overlay=0:0:format=auto[v]")
    near = render.MediaProbe(704, 1280, 1000, "h264", "yuv420p", "1:1", 0)
    assert render._video_graph(near, band) == (
        "[0:v:0]scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,setsar=1,"
        f"{chain}[delivery];[delivery][1:v:0]overlay=0:0:format=auto[v]")


@pytest.mark.parametrize("frame", ["2:3", "9:16 ", "16x9", "", None, 1.7778, ["16:9"], {"ratio": "16:9"}])
def test_unknown_page_frame_is_rejected_at_the_request_boundary(frame):
    with pytest.raises(ValidationError):
        request(frame=frame)


@pytest.mark.parametrize("frame", ["9:16", *FRAME_BANDS])
def test_page_frame_is_not_source_treatment_and_keeps_existing_source_reusable(frame):
    framed = request(frame=frame)
    assert render.source_visual_matches(framed)
    assert framed.treatment_sha256 != request().treatment_sha256
    assert framed.source_visual_treatment_json == request().source_visual_treatment_json


def _request_with_caption_style(frame, **style):
    treatment = {**TREATMENT, "captionStyle": {**STYLE, **style}}
    if frame is not _NO_FRAME:
        treatment["frame"] = frame
    encoded = json.dumps(treatment, sort_keys=True, separators=(",", ":"))
    return request(render_treatment_json=encoded, treatment_sha256=render.sha256(encoded.encode()))


@pytest.mark.parametrize("frame", list(FRAME_BANDS))
@pytest.mark.parametrize("position,offset", [("top", 0), ("bottom", 0), ("bottom", -12), ("middle", 20)])
def test_framed_page_draws_its_caption_in_the_very_middle(frame, position, offset):
    style = render._caption_request(_request_with_caption_style(frame, position=position, offset_pct=offset)).style
    assert (style.position, style.offset_pct) == ("middle", 0)
    # Only the placement moves: everything else the page chose is kept.
    kept = render._caption_request(_request_with_caption_style(_NO_FRAME, position=position, offset_pct=offset)).style
    assert style.model_dump(exclude={"position", "offset_pct"}) == kept.model_dump(exclude={"position", "offset_pct"})


@pytest.mark.parametrize("frame", [_NO_FRAME, "9:16"])
def test_full_screen_page_keeps_its_caption_placement(frame):
    style = render._caption_request(_request_with_caption_style(frame, position="bottom", offset_pct=-12)).style
    assert (style.position, style.offset_pct) == ("bottom", -12)


def test_framed_bottom_caption_is_burned_over_the_picture_not_the_bar(real_portrait, tmp_path):
    sha = render.sha256(real_portrait.read_bytes())
    result = render.render_post(real_portrait, tmp_path / "render",
                                _request_with_caption_style("16:9", position="bottom").model_copy(
                                    update={"source_sha256": sha}), clock_ms=lambda: NOW)
    with Image.open(result.qa_frame_path) as qa:
        qa.load()
        # 16:9 keeps rows 656-1263. A bottom caption would be centred near
        # row 1632, on the lower bar; it must not be there.
        lower_bar = _luma_rows(qa, 1264 + 4, 1920)
        assert lower_bar.mean[0] < 16 and lower_bar.extrema[0][1] < 40


TALL_LINES = ["when you", "finally", "see the", "light", "again", "at last",
              "and it", "all ends", "so well", "tonight"]
# Inclusive caption fit areas: the picture's rows, 44 px in from each side.
SAFE_AREAS = {"9:16": (44, 0, 1035, 1919), "16:9": (44, 656, 1035, 1263), "4:3": (44, 554, 1035, 1363)}
FRAME_ROWS = {"16:9": (656, 1263), "4:3": (554, 1363)}


def _tall_caption_request(frame, sha, line_count, size_pt=60):
    lines = TALL_LINES[:line_count]
    caption = " ".join(lines)
    treatment = {**TREATMENT, "frame": frame, "captionStyle": {**STYLE, "size_pt": size_pt, "line_breaks": lines}}
    encoded = json.dumps(treatment, sort_keys=True, separators=(",", ":"))
    return request(sha, render_treatment_json=encoded, treatment_sha256=render.sha256(encoded.encode()),
                   caption=caption, caption_sha256=render.sha256(caption.encode()))


def _overlay_ink_inside(directory, area):
    with Image.open(directory / "overlay.png") as overlay:
        box = overlay.getchannel("A").getbbox()
    return area[0] <= box[0] and area[1] <= box[1] and box[2] - 1 <= area[2] and box[3] - 1 <= area[3]


@pytest.mark.parametrize("frame,line_count", [("16:9", 4), ("4:3", 6)])
def test_framed_tall_caption_shrinks_inside_the_picture_not_over_the_bars(real_portrait, tmp_path, frame, line_count):
    # Owner rule 2026-10-04: at the look's full 60 pt these lines are taller
    # than the band; the caption must scale down to fit inside the picture.
    sha = render.sha256(real_portrait.read_bytes())
    top, bottom = FRAME_ROWS[frame]
    result = render.render_post(real_portrait, tmp_path / "render", _tall_caption_request(frame, sha, line_count),
                                clock_ms=lambda: NOW)
    assert _overlay_ink_inside(tmp_path / "render", SAFE_AREAS[frame])
    plan = json.loads((tmp_path / "render" / "caption-render.json").read_text())["plan"]
    assert plan["font_size_px"] == 150 and plan["fitted_font_size_px"] < 150
    with Image.open(result.qa_frame_path) as qa:
        qa.load()
        for bar in (_luma_rows(qa, 0, top - 4), _luma_rows(qa, bottom + 1 + 4, 1920)):
            assert bar.mean[0] < 16 and bar.extrema[0][1] < 40


def test_full_screen_big_caption_inside_the_screen_posts_as_styled(real_portrait, tmp_path):
    # Ten 60 pt lines are 84% of the screen tall. That used to refuse
    # (caption_geometry_invalid, over 45%); it is inside the screen, so it
    # posts at its full size.
    sha = render.sha256(real_portrait.read_bytes())
    render.render_post(real_portrait, tmp_path / "render", _tall_caption_request("9:16", sha, 10),
                       clock_ms=lambda: NOW)
    plan = json.loads((tmp_path / "render" / "caption-render.json").read_text())["plan"]
    assert plan["font_size_px"] == 150 and plan["line_height_px"] == 162
    assert "fitted_font_size_px" not in plan
    assert _overlay_ink_inside(tmp_path / "render", SAFE_AREAS["9:16"])


def test_full_screen_caption_taller_than_the_screen_shrinks_and_posts(real_portrait, tmp_path):
    # Ten 96 pt lines would run off the screen: that used to refuse with
    # CAPTION_OUT_OF_FRAME. Now it shrinks to fit and the post goes ahead.
    sha = render.sha256(real_portrait.read_bytes())
    render.render_post(real_portrait, tmp_path / "render", _tall_caption_request("9:16", sha, 10, size_pt=96),
                       clock_ms=lambda: NOW)
    plan = json.loads((tmp_path / "render" / "caption-render.json").read_text())["plan"]
    assert plan["font_size_px"] == 240 and plan["fitted_font_size_px"] < 240
    assert _overlay_ink_inside(tmp_path / "render", SAFE_AREAS["9:16"])


def test_page_frame_letterboxes_a_scaled_provider_source_the_same_way(real_portrait, tmp_path):
    # A 704x1280 provider frame goes through scale+crop before the letterbox.
    scaled = tmp_path / "provider-704x1280.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(real_portrait), "-vf", "scale=704:1280",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(scaled)],
                   check=True, timeout=120, capture_output=True)
    result = render.render_post(scaled, tmp_path / "render",
                                request(render.sha256(scaled.read_bytes()), frame="4:3"), clock_ms=lambda: NOW)
    assert (result.final_probe.width, result.final_probe.height) == (1080, 1920)
    with Image.open(result.qa_frame_path) as qa:
        qa.load()
        above, below = _luma_rows(qa, 0, 554 - 4), _luma_rows(qa, 1364 + 4, 1920)
        assert above.mean[0] < 16 and below.mean[0] < 16
        assert _luma_rows(qa, 554 + 4, 1364 - 4).mean[0] > 60



def test_page_frame_band_edges_land_on_the_even_rows_for_a_scaled_source(tmp_path):
    # A white 606x1080 provider-shaped source makes the band's edge rows exact:
    # 4:3 keeps rows 554-1363 whether or not the source was scaled first.
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe are required for actual render proof")
    source = tmp_path / "white-606x1080.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=white:s=606x1080:d=2:r=30",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(source)],
                   check=True, timeout=120, capture_output=True)
    result = render.render_post(source, tmp_path / "render",
                                request(render.sha256(source.read_bytes()), frame="4:3"), clock_ms=lambda: NOW)
    frame_png = tmp_path / "final.png"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(result.final_path), "-frames:v", "1",
                    "-update", "1", str(frame_png)], check=True, timeout=30, capture_output=True)
    with Image.open(frame_png) as decoded:
        luma = decoded.convert("L")
        # Sample the left edge, clear of the centred caption.
        row = lambda y: ImageStat.Stat(luma.crop((20, y, 120, y + 1))).mean[0]
        assert row(553) < 40 and row(1364) < 40
        assert row(554) > 200 and row(1363) > 200
