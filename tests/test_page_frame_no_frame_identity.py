"""No-frame byte identity: an unframed page renders exactly what dca377d5d1a0f5b30e0d4e2048b31652d788c3f7 rendered.

tests/fixtures/page-frame-no-frame-goldens.v1.json was produced by
``compute_goldens()`` below while services/ffmpeg.py and services/post_render.py
were still the dca377d5d1a0f5b30e0d4e2048b31652d788c3f7 (#193 merge) code, and committed before the band-aware
cut changed either file. Every later change must reproduce these bytes: the
cut filter strings, the full ffmpeg argv of both executors' cut call, and the
prepared-post graph and argv for pages without a frame (absent or "9:16").
The reference is generated on an untouched current-main worktree before integrating framed cuts; do not regenerate it from the implementation under test.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from services import ffmpeg as ffmpeg_module
from services import post_render as render
from services.control_plane_generation import dossier_filters_to_color_correction

GOLDENS = Path(__file__).parent / "fixtures" / "page-frame-no-frame-goldens.v1.json"

# Dossier filter dials as saved by real pages, mapped the way both executors
# map them (float arithmetic included, e.g. 0.94 -> -6.000000000000005).
_DOSSIER_FILTERS = [
    {},
    {"brightness": 0.94, "contrast": 1.08},
    {"brightness": 0.85, "saturation": 0.5},
    {"warmth": 0.3, "fade": 0.2, "grain": 0.25, "vignette": 0.4},
    {"brightness": 1.1, "contrast": 0.9, "saturation": 1.25, "warmth": -0.35},
]
_RAW_GRADES = [
    None,
    {"brightness": 20},
    {"temperature": 1},  # dead-band: collapses to the no-grade fast path
    {"sharpness": 25, "tint": 10},
    {"temperature": -40, "shadow": 12},
]
_SPEEDS = [1.0, 0.75, 1.5, 2.0]
_CROPS = [
    None,
    {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5},
    {"zoom": 1.5, "focusX": 0.2, "focusY": 0.7},
    {"zoom": 2.0, "focusX": 0.8, "focusY": 0.3},
    {"zoom": 3.0, "focusX": 1.0, "focusY": 1.0},
    {"zoom": 1.0, "focusX": 0.0, "focusY": 0.5},
]


def _grades():
    class _Recipe:
        def __init__(self, filters):
            self.recipe_spec = {"renderTreatment": {"filters": filters}}

    grades = [("raw", grade) for grade in _RAW_GRADES]
    grades += [("dossier", dossier_filters_to_color_correction(_Recipe(filters)))
               for filters in _DOSSIER_FILTERS]
    return grades


class _Stderr:
    async def read(self, _size=-1):
        return b""


class _Process:
    returncode = 0
    stderr = _Stderr()

    async def wait(self):
        return 0


def _cut_argv(monkeypatch, **kwargs) -> list[str]:
    captured: list[str] = []

    async def fake_exec(*args, **_kwargs):
        captured.extend(args)
        return _Process()

    monkeypatch.setattr(ffmpeg_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(ffmpeg_module, "_probe_input_duration_seconds", lambda _path: 7.0)
    asyncio.run(ffmpeg_module.run_color_correct("in.mp4", "out.mp4", **kwargs))
    return captured


def _slot_request(frame=None):
    style = {"font": "TikTokSans16pt-Bold.ttf", "size_pt": 24, "color": "#ffffff",
             "outline": "#000000", "position": "bottom", "align": "center", "case": "as_written",
             "background": "none", "offset_pct": -12, "line_balance": 0}
    treatment = {"stylePreset": "golden", "filters": {"brightness": 0.85, "saturation": 0.5},
                 "captionStyle": style, "clipSpeed": 1.5,
                 "clipCrop": {"zoom": 1.5, "focusX": 0.2, "focusY": 0.7}}
    if frame is not None:
        treatment["frame"] = frame
    raw = json.dumps(treatment, sort_keys=True, separators=(",", ":"))
    visual = json.dumps(render.normalized_visual_treatment(treatment), sort_keys=True, separators=(",", ":"))
    return render.PostRenderRequest.model_validate({
        "schema": render.REQUEST_SCHEMA, "slot_id": "slot:golden", "slot_payload_sha256": "b" * 64,
        "page_id": "page-golden", "program_id": "playlist:golden", "device_serial": "golden",
        "account": "golden.account", "source_sha256": "a" * 64, "caption": "golden caption",
        "caption_sha256": render.sha256(b"golden caption"), "render_treatment_json": raw,
        "treatment_sha256": render.sha256(raw.encode()), "source_visual_treatment_json": visual,
        "source_visual_treatment_sha256": render.sha256(visual.encode()),
        "renderer_id": render.RENDERER_ID, "renderer_version": render.RENDERER_VERSION,
        "created_at_ms": 1_800_000_000_000,
    })


def _encode_argv(monkeypatch, probe, band) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(args, _tools, **kwargs):
        calls.append(list(args))
        Path(args[-1]).write_bytes(b"final")
        if len(calls) == 1:
            raise render.PostRenderError("artifact_too_large", "force the bounded retry argv too")
        return b""

    monkeypatch.setattr(render, "_run", fake_run)
    monkeypatch.setattr(render, "_probe", lambda *_: probe)
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        final = Path(directory) / "final.mp4"
        render._encode_final(Path("/golden/source.mp4"), Path("/golden/overlay.png"), final,
                             probe, render.RenderTools(), band)
        return [[arg if arg != str(final) else "<final>" for arg in call] for call in calls]


def compute_goldens(monkeypatch) -> dict:
    filters = []
    for kind, grade in _grades():
        for speed in _SPEEDS:
            for crop in _CROPS:
                filters.append({"grade": [kind, grade], "speed": speed, "crop": crop, "size": None,
                                "scale": None,
                                "vf": ffmpeg_module.build_cc_filter(grade, playback_speed=speed, clip_crop=crop)})
    for crop in _CROPS[1:3]:
        for kind, grade in _grades()[:2]:
            filters.append({"grade": [kind, grade], "speed": 1.0, "crop": crop, "size": [720, 1280],
                            "scale": None,
                            "vf": ffmpeg_module.build_cc_filter(grade, clip_crop=crop, clip_crop_size=(720, 1280))})
            filters.append({"grade": [kind, grade], "speed": 1.0, "crop": crop, "size": None,
                            "scale": "1080:1920",
                            "vf": ffmpeg_module.build_cc_filter(grade, scale="1080:1920", clip_crop=crop)})

    grades = _grades()
    cuts = []
    # Sourced executor: delivery preset, typed 1080x1920 crop size, a cut window.
    for kind, grade in (grades[0], grades[1], grades[6], grades[8]):
        for speed in (1.0, 0.75, 2.0):
            for crop in (_CROPS[1], _CROPS[2], _CROPS[4]):
                for window in ((0, 6_000), (8_500, 7_000)):
                    kwargs = {"scale": None,
                              "encode_args": ffmpeg_module.delivery_encode_args("tiktok_delivery_v1"),
                              "playback_speed": speed, "clip_crop": crop, "clip_crop_size": (1080, 1920),
                              "clip_start_ms": window[0], "clip_duration_ms": window[1]}
                    cuts.append({"executor": "sourced", "grade": [kind, grade], "speed": speed,
                                 "crop": crop, "window": list(window),
                                 "argv": _cut_argv(monkeypatch, cc=grade, **kwargs)})
    # Generation executor: standard encode, default crop size, no window.
    for kind, grade in (grades[0], grades[1], grades[5], grades[6]):
        for speed in (1.0, 0.75, 1.5):
            for crop in (None, _CROPS[1], _CROPS[2], _CROPS[3]):
                kwargs = {"scale": None, "playback_speed": speed, "clip_crop": crop}
                cuts.append({"executor": "generation", "grade": [kind, grade], "speed": speed,
                             "crop": crop, "window": None,
                             "argv": _cut_argv(monkeypatch, cc=grade, **kwargs)})

    graphs = []
    probes = [(1080, 1920, "1:1"), (1080, 1920, "N/A"), (606, 1080, "1:1"), (704, 1280, "1:1")]
    for width, height, sar in probes:
        probe = render.MediaProbe(width, height, 8000, "h264", "yuv420p", sar, 0)
        for frame in (None, "9:16", "16:9", "1:1", "3:4", "4:3"):
            band = render._frame_band_height(_slot_request(frame))
            graphs.append({"probe": [width, height, sar], "frame": frame,
                           "graph": render._video_graph(probe, band)})
    encodes = []
    for frame in (None, "9:16", "16:9", "4:3"):
        probe = render.MediaProbe(1080, 1920, 8000, "h264", "yuv420p", "1:1", 0)
        encodes.append({"frame": frame, "argv": _encode_argv(
            monkeypatch, probe, render._frame_band_height(_slot_request(frame)))})
    captions = [{"frame": frame, "style": render._caption_request(_slot_request(frame)).style.model_dump()}
                for frame in (None, "9:16", "16:9", "1:1", "3:4", "4:3")]
    return {"schema": "page-frame-no-frame-goldens.v1", "source": "dca377d5d1a0f5b30e0d4e2048b31652d788c3f7",
            "filters": filters, "cuts": cuts, "postRenderGraphs": graphs,
            "postRenderEncodes": encodes, "captionStyles": captions}


def _json_roundtrip(value):
    return json.loads(json.dumps(value))


def test_no_frame_filter_strings_argv_and_post_render_graphs_are_byte_identical(monkeypatch):
    expected = json.loads(GOLDENS.read_text())
    actual = _json_roundtrip(compute_goldens(monkeypatch))
    assert expected["source"] == "dca377d5d1a0f5b30e0d4e2048b31652d788c3f7"
    for key in ("filters", "cuts", "postRenderGraphs", "postRenderEncodes", "captionStyles"):
        assert len(actual[key]) == len(expected[key]), key
        for index, (got, want) in enumerate(zip(actual[key], expected[key])):
            assert got == want, f"{key}[{index}] drifted from dca377d5d1a0f5b30e0d4e2048b31652d788c3f7"


def test_golden_grid_covers_grades_speeds_crops_and_both_executors():
    expected = json.loads(GOLDENS.read_text())
    assert len(expected["filters"]) >= 200
    assert {row["executor"] for row in expected["cuts"]} == {"sourced", "generation"}
    assert any("colorchannelmixer" in row["vf"] for row in expected["filters"])
    assert any("vignette=" in row["vf"] and "noise=" in row["vf"] for row in expected["filters"])
    assert {row["speed"] for row in expected["filters"]} == set(_SPEEDS)
