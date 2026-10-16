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


def request(source_sha="a" * 64, frame=_NO_FRAME, frame_fit=_NO_FRAME, **changes):
    slot_treatment = TREATMENT if frame is _NO_FRAME else {**TREATMENT, "frame": frame}
    if frame_fit is not _NO_FRAME:
        slot_treatment = {**slot_treatment, "frameFit": frame_fit}
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


@pytest.mark.parametrize("frame", list(FRAME_BANDS))
@pytest.mark.parametrize("fit", ["fill", "fit"])
def test_frame_fit_is_accepted_with_a_frame_and_keeps_sources_reusable(frame, fit):
    framed = request(frame=frame, frame_fit=fit)
    # Lenient by decision: frameFit is not source treatment, so no new refusal.
    assert render.source_visual_matches(framed)
    assert framed.source_visual_treatment_json == request().source_visual_treatment_json
    assert framed.treatment_sha256 != request(frame=frame).treatment_sha256
    assert render._frame_band_height(framed) == FRAME_BANDS[frame]
    style = render._caption_request(framed).style
    assert (style.position, style.offset_pct) == ("middle", 0)
    # The prepared-post graph stays #193's letterbox whichever fit cut the clip.
    for probe in (render.MediaProbe(1080, 1920, 1000, "h264", "yuv420p", "1:1", 0),
                  render.MediaProbe(704, 1280, 1000, "h264", "yuv420p", "1:1", 0)):
        assert render._video_graph(probe, render._frame_band_height(framed)) == render._video_graph(
            probe, render._frame_band_height(request(frame=frame)))


@pytest.mark.parametrize("frame,fit", [
    (_NO_FRAME, "fill"), (_NO_FRAME, "fit"), ("9:16", "fill"), ("9:16", "fit"),
    ("16:9", "stretch"), ("16:9", None), ("1:1", ""), ("4:3", ["fit"]),
])
def test_frame_fit_without_a_frame_or_unknown_is_rejected_at_the_request_boundary(frame, fit):
    with pytest.raises(ValidationError):
        request(frame=frame, frame_fit=fit)


# --- Ink check: the burned frame must show the words (CAPTION_NOT_DRAWN / CAPTION_LOW_CONTRAST) -----------------------
# The check measures CHANGE against the picture around the words: the same source frame, through the same delivery
# graph, with the caption's box or highlight drawn but its words left out. A box alone cannot pass, matching
# colours alone prove nothing (white on white), and white words on a dark box over a white clip pass because they
# are measured against the box. Numbers: docs/caption-ink-calibration.md and tests/fixtures/caption_ink_calibration/.

CALIBRATION_DIR = Path(__file__).parent / "fixtures" / "caption_ink_calibration"


def test_the_terminal_codes_are_not_retried():
    # No ffmpeg needed: a terminal refusal that started retrying would loop on a caption that cannot change.
    from services.post_render_jobs import RETRY_CODES
    assert {"CAPTION_GLYPH_MISSING", "CAPTION_NO_TEXT", "CAPTION_NOT_DRAWN",
            "CAPTION_LOW_CONTRAST"}.isdisjoint(RETRY_CODES)


def _decode(directory):
    return json.loads((directory / "decode.json").read_text())


def _solid_colour_clip(path, colour):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    f"color=c={colour}:size=1080x1920:rate=30:duration=1.2,setsar=1", "-an", "-c:v", "libx264",
                    "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30", str(path)],
                   check=True, timeout=30, capture_output=True)
    return path


def _noise_clip(path):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    "nullsrc=size=1080x1920:rate=30:duration=1.2,geq=random(1)*255:128:128,setsar=1", "-an", "-c:v",
                    "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30", str(path)],
                   check=True, timeout=60, capture_output=True)
    return path


def _transparent(overlay_png, background_png):
    import io
    out = io.BytesIO()
    Image.new("RGBA", (1080, 1920), (0, 0, 0, 0)).save(out, format="PNG")
    return out.getvalue()


def _background_only(overlay_png, background_png):
    # A box drawn but no words (a partial composite). A style with no box has nothing to leave behind.
    return background_png if background_png is not None else _transparent(overlay_png, background_png)


def _quarter_strength(overlay_png, background_png):
    import io
    with Image.open(io.BytesIO(overlay_png)) as overlay:
        overlay = overlay.convert("RGBA")
    alpha = overlay.getchannel("A").point(lambda a: a // 4)
    overlay.putalpha(alpha)
    out = io.BytesIO()
    overlay.save(out, format="PNG")
    return out.getvalue()


def _deliver(monkeypatch, transform):
    """Encode the final video with ``transform(overlay_png, background_png)`` instead of the real overlay.

    The overlay evidence the check holds (the real overlay, and the background it renders) is untouched, so this
    models a renderer that composited the wrong thing into the video.
    """
    captured = {}
    real_render = render.render_caption_with_background

    def spy(*args, **kwargs):
        result, background = real_render(*args, **kwargs)
        captured["background"] = background
        return result, background
    real_encode = render._encode_final

    def encode(source, overlay, final, source_probe, tools, frame_height=None):
        overlay.write_bytes(transform(overlay.read_bytes(), captured["background"]))
        return real_encode(source, overlay, final, source_probe, tools, frame_height)
    monkeypatch.setattr(render, "render_caption_with_background", spy)
    monkeypatch.setattr(render, "_encode_final", encode)


def _look_request(clip, **style):
    sha = render.sha256(clip.read_bytes())
    return _request_with_caption_style(_NO_FRAME, **style).model_copy(update={"source_sha256": sha})


def _refused(clip, tmp_path, look):
    with pytest.raises(render.PostRenderError) as error:
        render.render_post(clip, tmp_path / "refused", look, clock_ms=lambda: NOW)
    assert not (tmp_path / "refused").exists()
    return error.value


def test_a_normal_render_passes_and_records_how_much_caption_ink_it_shows(real_portrait, tmp_path):
    result = render.render_post(real_portrait, tmp_path / "render",
                                request(render.sha256(real_portrait.read_bytes())), clock_ms=lambda: NOW)
    decoded = _decode(tmp_path / "render")
    assert decoded["caption_ink_share"] >= 0.8
    assert decoded["caption_ink_pixels"] >= render.CAPTION_INK_MIN_PIXELS
    for name in ("reference.jpg", "background.png", "bare.jpg"):
        assert not (tmp_path / "render" / name).exists()
    # The receipt is unchanged: the share lives in decode.json only.
    assert "caption_ink_share" not in json.loads(result.receipt_json)


def test_a_render_whose_overlay_was_not_applied_is_refused_as_not_drawn(real_portrait, monkeypatch, tmp_path):
    _deliver(monkeypatch, _transparent)
    error = _refused(real_portrait, tmp_path, request(render.sha256(real_portrait.read_bytes())))
    assert error.code == "CAPTION_NOT_DRAWN"


@pytest.mark.parametrize("frame", ["16:9", "4:3"])
def test_a_framed_page_render_passes_which_proves_the_overlay_lines_up_with_the_band(real_portrait, tmp_path, frame):
    # The overlay is laid on the full 1080x1920 frame at 0:0, band and all, so it needs no offset, and the reference
    # goes through the same letterbox. A check that compared the wrong rows would see no caption here.
    render.render_post(real_portrait, tmp_path / "render",
                       request(render.sha256(real_portrait.read_bytes()), frame=frame), clock_ms=lambda: NOW)
    assert _decode(tmp_path / "render")["caption_ink_share"] >= 0.8


def test_a_framed_page_render_without_its_overlay_is_refused_too(real_portrait, monkeypatch, tmp_path):
    _deliver(monkeypatch, _transparent)
    error = _refused(real_portrait, tmp_path,
                     request(render.sha256(real_portrait.read_bytes()), frame="16:9"))
    assert error.code == "CAPTION_NOT_DRAWN"


@pytest.mark.parametrize("frame", ["16:9", "4:3"])
def test_a_framed_page_box_look_passes_and_is_refused_with_only_the_box(real_portrait, monkeypatch, tmp_path, frame):
    # The box reference goes through the same band as the delivered frame.
    sha = render.sha256(real_portrait.read_bytes())
    treatment = {**TREATMENT, "frame": frame, "captionStyle": {**STYLE, "background": "box", "background_color": "#202020"}}
    encoded = json.dumps(treatment, sort_keys=True, separators=(",", ":"))
    look = request(sha, render_treatment_json=encoded, treatment_sha256=render.sha256(encoded.encode()))
    render.render_post(real_portrait, tmp_path / "render", look, clock_ms=lambda: NOW)
    assert _decode(tmp_path / "render")["caption_ink_share"] >= 0.8
    _deliver(monkeypatch, _background_only)
    assert _refused(real_portrait, tmp_path, look).code == "CAPTION_NOT_DRAWN"


OTHER_LOOKS = [
    {"color": "#ff3b30", "outline": "#ffffff"},
    {"color": "#ffd60a", "outline": "#000000"},
    {"color": "#0a84ff", "outline": None},
    {"color": "#000000", "outline": None, "background": "highlight", "background_color": "#ffd60a"},
    {"background": "box", "background_color": "#000000"},
    {"color": "#ffffff", "outline": None, "background": "box", "background_color": "#808080"},
    {"size_pt": 12},
    {"font": "TikTokSans12pt-Light.ttf", "size_pt": 12, "outline": None},
    {"font": "TikTokSans16pt-Regular.ttf", "size_pt": 12, "color": "#0a84ff", "outline": None},
]
OTHER_LOOK_IDS = ["red", "yellow", "blue-no-outline", "highlight", "black-box", "gray-box", "smallest-size",
                  "thin-font", "regular-blue"]


@pytest.mark.parametrize("style", OTHER_LOOKS, ids=OTHER_LOOK_IDS)
def test_other_looks_pass_on_real_footage_and_are_refused_without_the_overlay(real_portrait, monkeypatch, tmp_path, style):
    look = _look_request(real_portrait, **style)
    render.render_post(real_portrait, tmp_path / "drawn", look, clock_ms=lambda: NOW)
    assert _decode(tmp_path / "drawn")["caption_ink_share"] >= 0.5
    _deliver(monkeypatch, _transparent)
    assert _refused(real_portrait, tmp_path, look).code == "CAPTION_NOT_DRAWN"


@pytest.mark.parametrize("style", [s for s in OTHER_LOOKS if "background" in s],
                         ids=[i for i, s in zip(OTHER_LOOK_IDS, OTHER_LOOKS) if "background" in s])
def test_a_box_with_no_words_in_the_video_is_refused_as_not_drawn(real_portrait, monkeypatch, tmp_path, style):
    # Codex r3 F1: the box was drawn, the words were not. Credit for the box must not prove the words.
    _deliver(monkeypatch, _background_only)
    assert _refused(real_portrait, tmp_path, _look_request(real_portrait, **style)).code == "CAPTION_NOT_DRAWN"


def test_a_gray_box_over_black_cannot_stand_in_for_white_words(tmp_path, monkeypatch):
    # The exact counterexample: black clip, white words, no outline, #808080 box. A box-only video scored 0.50
    # against the bare source (over the old 0.40 gate). Against the box-only reference it scores 0.
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required for actual render proof")
    clip = _solid_colour_clip(tmp_path / "black.mp4", "black")
    look = _look_request(clip, color="#ffffff", outline=None, background="box", background_color="#808080")
    render.render_post(clip, tmp_path / "ok", look, clock_ms=lambda: NOW)
    assert _decode(tmp_path / "ok")["caption_ink_share"] >= 0.8
    _deliver(monkeypatch, _background_only)
    assert _refused(clip, tmp_path, look).code == "CAPTION_NOT_DRAWN"


@pytest.mark.parametrize("clip_colour,style", [
    ("white", {"color": "#ffffff", "outline": None, "background": "box", "background_color": "#000000"}),
    ("white", {"color": "#ffffff", "outline": None, "background": "highlight", "background_color": "#000000"}),
    ("black", {"color": "#000000", "outline": None, "background": "box", "background_color": "#ffffff"}),
], ids=["white-on-black-box-over-white", "white-on-black-highlight-over-white", "black-on-white-box-over-black"])
def test_readable_words_on_a_contrasting_box_pass_even_when_they_match_the_clip(tmp_path, monkeypatch, clip_colour, style):
    # Codex r4 F1: the glyph pixels equal the clip (white on white) but sit on a black box, so the post is plainly
    # readable and must not be refused. Missing overlay or a box with no words must still be refused.
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required for actual render proof")
    clip = _solid_colour_clip(tmp_path / "clip.mp4", clip_colour)
    look = _look_request(clip, **style)
    render.render_post(clip, tmp_path / "ok", look, clock_ms=lambda: NOW)
    assert _decode(tmp_path / "ok")["caption_ink_share"] >= 0.8
    for name, transform in (("bare", _transparent), ("box-only", _background_only)):
        with monkeypatch.context() as patch:
            _deliver(patch, transform)
            assert _refused(clip, tmp_path, look).code == "CAPTION_NOT_DRAWN", name


def test_a_quarter_strength_caption_is_refused_so_the_threshold_cannot_be_lowered_unnoticed(real_portrait, monkeypatch, tmp_path):
    # A half-composited caption moves the frame about a quarter of the way: far from zero, still not drawn.
    # A threshold lowered under 0.25 would let it through.
    _deliver(monkeypatch, _quarter_strength)
    with pytest.raises(render.PostRenderError) as error:
        render.render_post(real_portrait, tmp_path / "render", request(render.sha256(real_portrait.read_bytes())),
                           clock_ms=lambda: NOW)
    assert error.value.code == "CAPTION_NOT_DRAWN"
    assert render.CAPTION_INK_MIN_SHARE >= 0.3


def test_a_thin_coloured_caption_on_noisy_footage_still_passes(tmp_path):
    # The worst case in the calibration: a 30 px Light blue caption on random noise (about 0.5 of its contrast
    # reaches the frame). The older colour-match check scored 0.03 here and would have refused a good post.
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required for actual render proof")
    clip = _noise_clip(tmp_path / "noise.mp4")
    render.render_post(clip, tmp_path / "render",
                       _look_request(clip, font="TikTokSans12pt-Light.ttf", size_pt=12, color="#0a84ff", outline=None),
                       clock_ms=lambda: NOW)
    assert _decode(tmp_path / "render")["caption_ink_share"] >= render.CAPTION_INK_MIN_SHARE + 0.05


def test_white_text_with_an_outline_on_a_white_clip_passes(tmp_path):
    # The black outline changes the picture, so the caption is measurable and visible.
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required for actual render proof")
    clip = _solid_colour_clip(tmp_path / "white.mp4", "white")
    render.render_post(clip, tmp_path / "render", request(render.sha256(clip.read_bytes())), clock_ms=lambda: NOW)
    assert _decode(tmp_path / "render")["caption_ink_share"] >= 0.8


@pytest.mark.parametrize("clip_colour", ["white", "0xf2f2f2"])
@pytest.mark.parametrize("applied", [True, False], ids=["overlay-applied", "overlay-missing"])
def test_white_text_with_no_outline_on_a_white_clip_is_refused_as_low_contrast(tmp_path, monkeypatch, clip_colour, applied):
    # It used to pass whether or not the overlay was applied (white matches white). Words that are the colour of
    # what is directly behind them (no box) cannot be seen, so the post would carry no readable caption. That is a
    # fact about this clip and look (CAPTION_LOW_CONTRAST), the same whether or not the overlay was applied.
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required for actual render proof")
    clip = _solid_colour_clip(tmp_path / "bright.mp4", clip_colour)
    if not applied:
        _deliver(monkeypatch, _transparent)
    error = _refused(clip, tmp_path, _look_request(clip, outline=None))
    assert error.code == "CAPTION_LOW_CONTRAST"
    assert "cannot be told apart" in str(error)


@pytest.mark.parametrize("applied", [True, False], ids=["overlay-applied", "overlay-missing"])
def test_black_words_in_a_white_box_on_a_white_clip(tmp_path, monkeypatch, applied):
    # The box is the clip's own colour; the black words are measured against it. Drawn passes; missing is NOT_DRAWN.
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required for actual render proof")
    clip = _solid_colour_clip(tmp_path / "white.mp4", "white")
    look = _look_request(clip, color="#000000", outline=None, background="box", background_color="#ffffff")
    if applied:
        render.render_post(clip, tmp_path / "render", look, clock_ms=lambda: NOW)
        assert _decode(tmp_path / "render")["caption_ink_share"] >= 0.8
        return
    _deliver(monkeypatch, _transparent)
    assert _refused(clip, tmp_path, look).code == "CAPTION_NOT_DRAWN"


def test_text_in_a_box_of_its_own_colour_cannot_be_told_apart(real_portrait, tmp_path):
    # White text, no outline, in a white box over dark footage: the box is plainly visible, but the words are
    # the box's colour and cannot be read. Against the box-only reference they change nothing: LOW_CONTRAST.
    error = _refused(real_portrait, tmp_path,
                     _look_request(real_portrait, color="#ffffff", outline=None, background="box",
                                   background_color="#ffffff"))
    assert error.code == "CAPTION_LOW_CONTRAST"
    assert "cannot be told apart" in str(error)


def test_the_two_refusals_are_different_terminal_codes(real_portrait, monkeypatch, tmp_path):
    # CAPTION_LOW_CONTRAST: the words cannot be seen against what is behind them (a fact about the clip and look).
    # CAPTION_NOT_DRAWN: contrast was expected, the frame did not change (the renderer did not draw it).
    low = _refused(real_portrait, tmp_path,
                   _look_request(real_portrait, color="#ffffff", outline=None, background="box", background_color="#ffffff"))
    _deliver(monkeypatch, _transparent)
    not_drawn = _refused(real_portrait, tmp_path, request(render.sha256(real_portrait.read_bytes())))
    assert (low.code, not_drawn.code) == ("CAPTION_LOW_CONTRAST", "CAPTION_NOT_DRAWN")


def test_numpy_is_a_declared_requirement_imported_when_the_module_loads():
    # A missing install must fail at build or import, not at the first render. Floor only: an exact pin would fight
    # numba and librosa.
    requirements = (Path(__file__).parents[1] / "requirements.txt").read_text().splitlines()
    numpy_lines = [line for line in requirements if line.lower().startswith("numpy")]
    assert len(numpy_lines) == 1 and numpy_lines[0].startswith("numpy>=") and "==" not in numpy_lines[0]
    assert render.np.__name__ == "numpy"


def _overlay_for(style_overrides=None, caption="share check"):
    import base64
    from services.caption_render import CaptionRenderRequest, render_caption_with_background
    caption_request = CaptionRenderRequest.model_validate({
        "schema": "content-lab.caption-render-request.v1", "caption": caption,
        "style": {**STYLE, "size_pt": 32, "line_balance": 0, **(style_overrides or {})}})
    fonts = Path(render.__file__).parents[1] / "fonts"
    result, background = render_caption_with_background(caption_request, font_dir=fonts)
    return base64.b64decode(result.overlay.base64), background


def _composite(base, png):
    import io
    with Image.open(io.BytesIO(png)) as layer:
        return Image.alpha_composite(base.convert("RGBA"), layer.convert("RGBA")).convert("RGB")


def test_the_ink_measure_on_synthetic_frames():
    import io
    png, background = _overlay_for()
    assert background is None
    source = Image.new("RGB", (1080, 1920), (120, 120, 120))
    burned = _composite(source, png)
    share, pixels, box_match = render._caption_ink(png, burned, source)
    assert share >= 0.99 and pixels > 1000 and box_match is None
    # Nothing burned: the frame is the reference.
    share, pixels, _ = render._caption_ink(png, source, source)
    assert abs(share) < 0.01 and pixels > 1000
    # A quarter of the way: far from zero, under the gate, over a lowered one.
    quarter = Image.blend(source, burned, 0.25)
    share = render._caption_ink(png, quarter, source)[0]
    assert 0.2 < share < render.CAPTION_INK_MIN_SHARE
    # A frame that moved the wrong way scores below zero.
    assert render._caption_ink(png, Image.eval(burned, lambda v: 255 - v), source)[0] < 0
    # An overlay with no solid pixel proves nothing.
    empty = io.BytesIO()
    Image.new("RGBA", (1080, 1920), (255, 255, 255, 0)).save(empty, format="PNG")
    assert render._caption_ink(empty.getvalue(), burned, source) == (0.0, 0, None)


def test_the_ink_measure_scores_the_words_against_their_box_not_the_bare_source():
    black = Image.new("RGB", (1080, 1920), (0, 0, 0))
    png, background = _overlay_for({"color": "#ffffff", "outline": None, "background": "box", "background_color": "#808080"})
    box_only = _composite(black, background)
    full = _composite(black, png)
    # Words drawn: moved all the way from the box to white.
    share, pixels, box_match = render._caption_ink(png, full, box_only, background, black)
    assert share >= 0.99 and pixels > 1000 and box_match >= 0.99
    # A frame with the box and no words: nothing moved from the box-only reference; the box itself is there.
    share, pixels, box_match = render._caption_ink(png, box_only, box_only, background, black)
    assert abs(share) < 0.01 and pixels > 1000 and box_match >= 0.99
    # No box and no words in the frame: the box is missing too.
    assert render._caption_ink(png, black, box_only, background, black)[2] < 0.05
    # Scored against the bare source instead, the box alone would have looked half-drawn (the old hole).
    assert render._caption_ink(png, box_only, black, background, black)[0] > 0.45


def test_words_on_a_contrasting_box_have_enough_contrast_even_when_they_match_the_clip():
    white = Image.new("RGB", (1080, 1920), (255, 255, 255))
    png, background = _overlay_for({"color": "#ffffff", "outline": None, "background": "box", "background_color": "#000000"})
    box_only = _composite(white, background)
    share, pixels, _ = render._caption_ink(png, _composite(white, png), box_only, background, white)
    assert share >= 0.99 and pixels > 1000
    # The same words with no box have nothing to show against the white clip.
    png, background = _overlay_for({"color": "#ffffff", "outline": None})
    assert render._caption_ink(png, _composite(white, png), white)[1] < render.CAPTION_INK_MIN_PIXELS


def _verdict(row):
    """The verdict the current thresholds give a recorded measurement."""
    pixels = int(row["pixels"])
    if pixels < render.CAPTION_INK_MIN_PIXELS:
        return "LOW_CONTRAST"
    if float(row["share"]) < render.CAPTION_INK_MIN_SHARE:
        return "NOT_DRAWN"
    if row["box_match"] and float(row["box_match"]) < render.CAPTION_BOX_MIN_MATCH:
        return "NOT_DRAWN"
    return "PASS"


def test_the_calibration_evidence_in_the_repo_supports_the_thresholds():
    # tests/fixtures/caption_ink_calibration/real-render-shares.csv holds 3,102 real renders on seeno (11 clips, 3 fonts,
    # 18 looks incl. boxes, highlights and mid-grey boxes, 12 and 24 pt; each drawn, with no overlay in the video, and for box looks
    # with only the box in the video). See docs/caption-ink-calibration.md. The thresholds in post_render.py must
    # still give every recorded measurement its recorded verdict, and keep a clear gap on both sides.
    import csv
    rows = list(csv.DictReader((CALIBRATION_DIR / "real-render-shares.csv").open()))
    assert len(rows) == 3102
    for row in rows:
        assert _verdict(row) == row["verdict"], row
    drawn_visible = [r for r in rows if r["mode"] == "drawn" and r["expected_invisible"] == "no"]
    missing = [r for r in rows if r["mode"] != "drawn"]
    # Every drawn caption that can be seen passes; every one that cannot (same colour as what is behind it) is low contrast.
    assert drawn_visible and all(r["verdict"] == "PASS" for r in drawn_visible)
    assert all(r["verdict"] == "LOW_CONTRAST" for r in rows if r["mode"] == "drawn" and r["expected_invisible"] == "yes")
    # Nothing missing from the video ever passes.
    assert missing and all(r["verdict"] != "PASS" for r in missing)
    # Gaps: words share of drawn captions vs words share when the overlay is missing (no box), box match likewise.
    plain = [r for r in drawn_visible if not r["box_match"]]
    boxed = [r for r in drawn_visible if r["box_match"]]
    assert min(float(r["share"]) for r in plain) > render.CAPTION_INK_MIN_SHARE + 0.1
    assert min(float(r["share"]) for r in boxed) > render.CAPTION_INK_MIN_SHARE + 0.4
    assert min(float(r["box_match"]) for r in boxed) > render.CAPTION_BOX_MIN_MATCH + 0.25
    bare_plain = [r for r in missing if r["mode"] == "bare" and not r["box_match"] and r["share"]]
    assert max(float(r["share"]) for r in bare_plain if r["clip"] != "noise") < 0.1
    assert max(float(r["share"]) for r in bare_plain if r["clip"] == "noise") < render.CAPTION_INK_MIN_SHARE - 0.05
    # A box look with no overlay can score well on the words (white words on a black box over a white clip match the
    # white clip); the box itself is what is missing.
    bare_boxed = [r for r in missing if r["mode"] == "bare" and r["box_match"] and r["verdict"] != "LOW_CONTRAST"
                  and float(r["share"]) >= render.CAPTION_INK_MIN_SHARE]
    assert bare_boxed and max(float(r["box_match"]) for r in bare_boxed) < render.CAPTION_BOX_MIN_MATCH - 0.1


def test_a_box_needs_the_bare_source_frame_to_be_told_from_the_burned_frame():
    png, background = _overlay_for({"color": "#ffffff", "outline": None, "background": "box", "background_color": "#808080"})
    frame = Image.new("RGB", (1080, 1920), (0, 0, 0))
    with pytest.raises(ValueError):
        render._caption_ink(png, frame, frame, background)


def _grey(value):
    return Image.new("RGB", (1080, 1920), (value, value, value))


@pytest.mark.parametrize("source_grey,box,words", [
    (0xF0, "#c0c0c0", "#ffffff"),   # Codex r7 F1: light-grey footage, a #c0c0c0 box, white words
    (0xE0, "#b0b0b0", "#ffffff"),
    (0xD8, "#a8a8a8", "#ffffff"),
    (0x90, "#606060", "#ffffff"),
    (0xA0, "#d8d8d8", "#000000"),
    (0x70, "#a0a0a0", "#000000"),
], ids=["codex-r7-f1", "e0-vs-b0", "d8-vs-a8", "90-vs-60", "a0-vs-d8-black-words", "70-vs-a0-black-words"])
def test_a_mid_grey_box_over_matching_grey_footage_does_not_count_as_shown_when_nothing_was_burned(source_grey, box, words):
    # The footage sits within the old 64-level colour tolerance of the box colour, so a plain colour match called the
    # box "shown" with no overlay in the video, and the words (measured against the box) passed too. A box pixel now
    # counts only when the burned colour is nearer the box colour than the bare source is.
    png, background = _overlay_for({"color": words, "outline": None, "background": "box", "background_color": box})
    source = _grey(source_grey)
    box_only = _composite(source, background)
    full = _composite(source, png)
    # Nothing burned: the frame is the bare source.
    share, pixels, box_match = render._caption_ink(png, source, box_only, background, source)
    assert box_match < 0.05
    assert box_match < render.CAPTION_BOX_MIN_MATCH - 0.4
    # The caption burned in: the box is shown and the words pass.
    share, pixels, box_match = render._caption_ink(png, full, box_only, background, source)
    assert box_match >= 0.99 and share >= 0.99 and pixels > 1000


@pytest.mark.parametrize("source_grey,box,words", [
    (0xF0, "#c0c0c0", "#ffffff"),
    (0xE0, "#b0b0b0", "#ffffff"),
    (0x90, "#606060", "#ffffff"),
    (0x70, "#a0a0a0", "#000000"),
], ids=["codex-r7-f1", "e0-vs-b0", "90-vs-60", "70-vs-a0-black-words"])
def test_a_mid_grey_box_render_passes_with_the_overlay_and_is_refused_without_it(tmp_path, monkeypatch, source_grey, box, words):
    # The exact Codex r7 look (and variants) through the real render: overlay in the video passes; no overlay is
    # NOT_DRAWN (it passed both checks before); a box with no words is NOT_DRAWN too.
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required for actual render proof")
    clip = _solid_colour_clip(tmp_path / "grey.mp4", "0x{0:02x}{0:02x}{0:02x}".format(source_grey))
    look = _look_request(clip, color=words, outline=None, background="box", background_color=box)
    render.render_post(clip, tmp_path / "ok", look, clock_ms=lambda: NOW)
    decoded = _decode(tmp_path / "ok")
    assert decoded["caption_box_match"] >= 0.9 and decoded["caption_ink_share"] >= 0.8
    for name, transform in (("bare", _transparent), ("box-only", _background_only)):
        with monkeypatch.context() as patch:
            _deliver(patch, transform)
            assert _refused(clip, tmp_path, look).code == "CAPTION_NOT_DRAWN", name
