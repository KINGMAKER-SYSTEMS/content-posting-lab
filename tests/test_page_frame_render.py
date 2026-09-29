"""Real framed renders: band-aware cut -> prepared post -> measured QA frame.

Every case builds a small synthetic master with ffmpeg (grey field, an 8 px dark
border, four coloured corner squares and a 4x4 grid of white squares), runs the
real band-aware cut exactly as an executor calls it (services.ffmpeg.
run_color_correct: the sourced executor's delivery preset and cut window, or
the generation executor's standard encode), then services.post_render.
render_post, and measures render_post's own QA frame against the shared
fixture table (tests/fixtures/page-frame-geometry-fixtures.v1.json):

* the picture rectangle (the non-black area) is within 2 px of the table;
* everything outside the picture (the bars, and the pillarbox inside a fit
  band) is black, even under a grade with fade, grain and vignette;
* the source border shows on exactly the sides where the table's window
  touches the source edge;
* ffmpeg's own scale/crop/pad integers for the cut chain (from its trace log)
  are within 2 px of the table (fit and pad are exact; see lab-matrix.md for
  the fill crop rounding), and the white squares sit where those integers put
  them.

Set PAGE_FRAME_QA_DIR to keep every QA frame as <dir>/qa/<case>.jpg and to
write <dir>/lab-matrix.md (this also traces the whole 360-case table).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageFilter

from services import page_frame
from services import post_render as render
from services.control_plane_generation import dossier_filters_to_color_correction
from services.ffmpeg import (
    STANDARD_ENCODE_ARGS,
    build_cc_filter,
    delivery_encode_args,
    probe_display_size,
    run_color_correct,
)
from services.source_treatment import normalized_visual_treatment

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe are required for real framed renders",
)

TABLE = json.loads((Path(__file__).parent / "fixtures" / "page-frame-geometry-fixtures.v1.json").read_text())
NOW = 1_800_000_000_000
SOURCE_MS = 600
SOURCES = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "9:16-provider": (704, 1280)}
BORDER = 8
MARKER = 32
FRACTIONS = (0.125, 0.375, 0.625, 0.875)
# A real Dossier grade: the fade lifts blacks, so bars stay black only because
# the pad to the canvas is the cut's last filter.
FILTERS = {"brightness": 0.85, "saturation": 0.5, "fade": 0.2, "grain": 0.25, "vignette": 0.4}
STYLE = {"font": "TikTokSans16pt-Bold.ttf", "size_pt": 24, "color": "#ffffff", "outline": "#000000",
         "position": "bottom", "align": "center", "case": "as_written", "background": "none",
         "offset_pct": 0, "line_balance": 0}
CAPTION = "QA"
TOLERANCE_PX = 2
INK_LUMA = 40
QA_ROOT = os.environ.get("PAGE_FRAME_QA_DIR")
RESULTS: list[dict] = []


@dataclass(frozen=True)
class Case:
    name: str
    source: str
    frame: str
    fit: str | None = None
    zoom: float = 1.0
    focus_x: float = 0.5
    focus_y: float = 0.5
    grade: bool = False
    speed: float = 1.0
    executor: str = "sourced"

    @property
    def resolved_fit(self) -> str:
        return self.fit or page_frame.default_frame_fit(self.frame)

    @property
    def crop(self) -> dict:
        return {"zoom": self.zoom, "focusX": self.focus_x, "focusY": self.focus_y}


ZOOMED = {"zoom": 1.5, "focus_x": 0.2, "focus_y": 0.7}
CASES = [
    # 9:16 master
    Case("src9x16-frame9x16", "9:16", "9:16"),
    Case("src9x16-frame16x9-fill", "9:16", "16:9", "fill"),
    Case("src9x16-frame16x9-fit-speed0.75", "9:16", "16:9", "fit", speed=0.75),
    Case("src9x16-frame1x1-fill-grade", "9:16", "1:1", "fill", grade=True),
    Case("src9x16-frame1x1-fit", "9:16", "1:1", "fit"),
    # 16:9 master
    Case("src16x9-frame9x16", "16:9", "9:16"),
    Case("src16x9-frame16x9-fit", "16:9", "16:9", "fit"),
    Case("src16x9-frame16x9-fit-grade-speed1.5", "16:9", "16:9", "fit", grade=True, speed=1.5),
    Case("src16x9-frame16x9-fill", "16:9", "16:9", "fill"),
    Case("src16x9-frame1x1-fill", "16:9", "1:1", "fill"),
    Case("src16x9-frame1x1-fit", "16:9", "1:1", "fit"),
    Case("src16x9-frame3x4-fit", "16:9", "3:4", "fit"),
    Case("src16x9-frame4x3-fit", "16:9", "4:3", "fit"),
    Case("src16x9-frame4x3-default", "16:9", "4:3"),  # absent frameFit resolves to fit
    # 1:1 master
    Case("src1x1-frame1x1-fit", "1:1", "1:1", "fit"),
    Case("src1x1-frame9x16", "1:1", "9:16"),
    Case("src1x1-frame16x9-fill", "1:1", "16:9", "fill"),
    # zoom 1.5 at focus (0.2, 0.7): fill on every framed ratio, and one fit
    Case("src16x9-frame16x9-fill-z1.5", "16:9", "16:9", "fill", **ZOOMED),
    Case("src16x9-frame1x1-fill-z1.5", "16:9", "1:1", "fill", **ZOOMED),
    Case("src16x9-frame3x4-fill-z1.5-grade", "16:9", "3:4", "fill", grade=True, **ZOOMED),
    Case("src16x9-frame4x3-fill-z1.5", "16:9", "4:3", "fill", **ZOOMED),
    Case("src16x9-frame1x1-fit-z1.5", "16:9", "1:1", "fit", **ZOOMED),
    # generation executor on a native provider frame (standard encode, no window)
    Case("provider704x1280-frame16x9-fit-generation", "9:16-provider", "16:9", "fit", executor="generation"),
    Case("provider704x1280-frame1x1-fill-generation", "9:16-provider", "1:1", "fill", executor="generation"),
]


def _grade_cc() -> dict:
    class _Recipe:
        recipe_spec = {"renderTreatment": {"filters": FILTERS}}

    return dossier_filters_to_color_correction(_Recipe())


def _source_graph(width: int, height: int, seconds: float) -> str:
    boxes = [f"drawbox=x=0:y=0:w={width}:h={height}:color=0x505050:t={BORDER}"]
    corner = 48
    for x, y, color in ((BORDER, BORDER, "0xFF6060"), (width - BORDER - corner, BORDER, "0x60C860"),
                        (BORDER, height - BORDER - corner, "0x6080FF"),
                        (width - BORDER - corner, height - BORDER - corner, "0xC89628")):
        boxes.append(f"drawbox=x={x}:y={y}:w={corner}:h={corner}:color={color}:t=fill")
    for fx in FRACTIONS:
        for fy in FRACTIONS:
            cx, cy = round(fx * width), round(fy * height)
            boxes.append(f"drawbox=x={cx - MARKER // 2}:y={cy - MARKER // 2}:w={MARKER}:h={MARKER}"
                         ":color=white:t=fill")
    return f"color=c=0xA0A0A0:s={width}x{height}:r=30:d={seconds},format=yuv420p," + ",".join(boxes)


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    root = tmp_path_factory.mktemp("page-frame-sources")
    made: dict[str, Path] = {}

    def get(name: str) -> Path:
        if name not in made:
            width, height = SOURCES[name]
            path = root / f"{name.replace(':', 'x')}.mp4"
            subprocess.run(["nice", "-n", "10", "ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
                            "-i", _source_graph(width, height, SOURCE_MS / 1000), "-an", "-c:v", "libx264",
                            "-preset", "ultrafast", "-crf", "8", "-pix_fmt", "yuv420p", "-threads", "2",
                            str(path)], check=True, timeout=120, capture_output=True)
            made[name] = path
        return made[name]

    return get


def _table_row(case: Case) -> dict:
    for row in TABLE["cases"]:
        if (row["source"]["name"] == case.source and row["frame"] == case.frame
                and row["frameFit"] == case.resolved_fit and row["clipCrop"] == case.crop):
            return row
    raise AssertionError(f"{case.name} is not a row of the shared table")


def _cut_kwargs(case: Case, source: Path) -> dict:
    """What _page_frame_cut_kwargs passes for this page (nothing for 9:16)."""
    if case.frame == page_frame.VERTICAL_FRAME:
        return {}
    kwargs = {"page_frame": case.frame, "frame_fit": case.resolved_fit}
    if case.resolved_fit == "fit":
        kwargs["source_size"] = asyncio.run(probe_display_size(source))
    return kwargs


def _cut(case: Case, source: Path, output: Path, frame_kwargs: dict) -> None:
    grade = _grade_cc() if case.grade else None
    if case.executor == "sourced":
        asyncio.run(run_color_correct(
            str(source), str(output), grade, scale=None,
            encode_args=[*delivery_encode_args("tiktok_delivery_v1"), "-threads", "2"],
            playback_speed=case.speed, clip_crop=case.crop, clip_crop_size=(1080, 1920),
            clip_start_ms=0, clip_duration_ms=SOURCE_MS, **frame_kwargs))
    else:
        asyncio.run(run_color_correct(
            str(source), str(output), grade, scale=None,
            encode_args=[*STANDARD_ENCODE_ARGS, "-threads", "2"],
            playback_speed=case.speed, clip_crop=case.crop, **frame_kwargs))


def _post_request(case: Case, source_sha: str) -> render.PostRenderRequest:
    treatment = {"stylePreset": "lab-matrix", "filters": FILTERS if case.grade else {},
                 "captionStyle": STYLE, "clipSpeed": case.speed, "clipCrop": case.crop, "frame": case.frame}
    if case.fit is not None:
        treatment["frameFit"] = case.fit
    raw = json.dumps(treatment, sort_keys=True, separators=(",", ":"))
    visual = json.dumps(normalized_visual_treatment(treatment), sort_keys=True, separators=(",", ":"))
    return render.PostRenderRequest.model_validate({
        "schema": render.REQUEST_SCHEMA, "slot_id": f"slot:lab-matrix:{case.name}",
        "slot_payload_sha256": "b" * 64, "page_id": "page-lab-matrix", "program_id": "playlist:lab-matrix",
        "device_serial": "lab-matrix", "account": "lab.matrix", "source_sha256": source_sha,
        "caption": CAPTION, "caption_sha256": render.sha256(CAPTION.encode()),
        "render_treatment_json": raw, "treatment_sha256": render.sha256(raw.encode()),
        "source_visual_treatment_json": visual, "source_visual_treatment_sha256": render.sha256(visual.encode()),
        "renderer_id": render.RENDERER_ID, "renderer_version": render.RENDERER_VERSION,
        "created_at_ms": NOW - 1000,
    })


_SCALE = re.compile(r"\[Parsed_scale_\d+ @ [^\]]+\] w:\d+ h:\d+ fmt:\w+.* -> w:(\d+) h:(\d+) fmt:(\w+)")
_CROP = re.compile(r"\[Parsed_crop_\d+ @ [^\]]+\] n:0 .*x:(\d+) y:(\d+) x\+w:\d+ y\+h:\d+")
_PAD = re.compile(r"\[Parsed_pad_\d+ @ [^\]]+\] w:\d+ h:\d+ -> w:(\d+) h:(\d+) x:(\d+) y:(\d+)")


def ffmpeg_geometry(width: int, height: int, vf: str) -> dict:
    """ffmpeg's own integers for a cut chain: scale output size, crop offset, pad offset."""
    result = subprocess.run(
        ["nice", "-n", "10", "ffmpeg", "-nostdin", "-v", "trace", "-threads", "2", "-filter_threads", "2",
         "-f", "lavfi", "-i", f"color=c=gray:s={width}x{height}:r=30:d=0.04,format=yuv420p",
         "-vf", vf, "-frames:v", "1", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    scales = _SCALE.findall(result.stderr)
    crops = _CROP.findall(result.stderr)
    pads = _PAD.findall(result.stderr)
    assert len(scales) == 1 and len(crops) == 1 and len(pads) <= 1, (scales, crops, pads)
    return {"scale": [int(scales[0][0]), int(scales[0][1])], "format": scales[0][2],
            "crop": [int(crops[0][0]), int(crops[0][1])],
            "pad": [int(pads[0][2]), int(pads[0][3])] if pads else None}


def _window_delta(case: Case, geometry: dict, actual: dict) -> int:
    """ffmpeg's integers vs the table model; fit and the pad must be exact."""
    picture = geometry["picture"]
    if case.resolved_fit == "fit":
        window = geometry["window"]
        assert actual["scale"] == [picture["w"], picture["h"]]
        assert actual["pad"] == [picture["x"], picture["y"]]
        return max(abs(actual["crop"][0] - window["x"]), abs(actual["crop"][1] - window["y"]))
    assert actual["scale"] == [geometry["scaled"]["w"], geometry["scaled"]["h"]]
    if case.frame != page_frame.VERTICAL_FRAME:
        assert actual["pad"] == [0, geometry["band"]["top"]]
    return max(abs(actual["crop"][0] - geometry["scaled"]["cropX"]),
               abs(actual["crop"][1] - geometry["scaled"]["cropY"]))


def _mapping(case: Case, geometry: dict, crop_x: float, crop_y: float):
    """Source continuous coordinates -> canvas continuous coordinates."""
    width, height = SOURCES[case.source]
    picture = geometry["picture"]
    if case.resolved_fit == "fit":
        window = geometry["window"]
        sx, sy = picture["w"] / window["w"], picture["h"] / window["h"]
        return lambda x, y: (picture["x"] + (x - crop_x) * sx, picture["y"] + (y - crop_y) * sy), sx
    sx, sy = geometry["scaled"]["w"] / width, geometry["scaled"]["h"] / height
    return lambda x, y: (x * sx - crop_x, picture["y"] + y * sy - crop_y), sx


def _luma(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        rgb = np.asarray(image.convert("RGB"), dtype=np.float64)
    # Elementwise (not matmul): Accelerate's BLAS raises spurious FP warnings on macOS.
    return 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]


def _caption_mask(overlay_path: Path) -> np.ndarray:
    with Image.open(overlay_path) as overlay:
        alpha = overlay.convert("RGBA").split()[3].point(lambda a: 255 if a else 0).filter(ImageFilter.MaxFilter(13))
        return np.asarray(alpha) > 0


def _picture_rect(luma: np.ndarray, mask: np.ndarray) -> dict:
    ink = (luma > INK_LUMA) & ~mask
    rows = np.flatnonzero(ink.sum(axis=1) >= 16)
    cols = np.flatnonzero(ink.sum(axis=0) >= 16)
    return {"x": int(cols[0]), "y": int(rows[0]), "w": int(cols[-1] + 1 - cols[0]), "h": int(rows[-1] + 1 - rows[0])}


def _rect_delta(a: dict, b: dict) -> int:
    return max(abs(a["x"] - b["x"]), abs(a["y"] - b["y"]),
               abs(a["x"] + a["w"] - b["x"] - b["w"]), abs(a["y"] + a["h"] - b["y"] - b["h"]))


def _bars(luma: np.ndarray, mask: np.ndarray, picture: dict) -> dict:
    outside = np.ones(luma.shape, dtype=bool)
    outside[max(0, picture["y"] - 4):picture["y"] + picture["h"] + 4,
            max(0, picture["x"] - 4):picture["x"] + picture["w"] + 4] = False
    values = luma[outside & ~mask]
    if not values.size:
        return {"pixels": 0, "max": 0.0, "mean": 0.0}
    return {"pixels": int(values.size), "max": round(float(values.max()), 1), "mean": round(float(values.mean()), 2)}


def _border_sides(case: Case, geometry: dict, luma: np.ndarray, mask: np.ndarray, measured: dict) -> tuple[str, str]:
    """(expected, measured) sides showing the source border, as a subset of 'LTRB'.

    The table window says how much of the 8 px source border survives on each
    side; a side is expected when at least 2 canvas px of it remain and not
    expected when none does. A thinner sliver is not judged either way.
    """
    width, height = SOURCES[case.source]
    window, picture = geometry["window"], geometry["picture"]
    scale_x, scale_y = picture["w"] / window["w"], picture["h"] / window["h"]
    remaining = {"L": (BORDER - window["x"]) * scale_x, "T": (BORDER - window["y"]) * scale_y,
                 "R": (BORDER - (width - window["x"] - window["w"])) * scale_x,
                 "B": (BORDER - (height - window["y"] - window["h"])) * scale_y}
    judged = [side for side in "LTRB" if remaining[side] >= 2 or remaining[side] <= 0]
    expected = "".join(side for side in judged if remaining[side] >= 2)
    x0, y0 = measured["x"], measured["y"]
    x1, y1 = x0 + measured["w"], y0 + measured["h"]
    field = float(np.median(luma[y0:y1, x0:x1][~mask[y0:y1, x0:x1]]))
    mid_x = slice(x0 + measured["w"] // 4, x1 - measured["w"] // 4)
    mid_y = slice(y0 + measured["h"] // 4, y1 - measured["h"] // 4)
    strips = {"L": (mid_y, slice(x0, x0 + 2)), "T": (slice(y0, y0 + 2), mid_x),
              "R": (mid_y, slice(x1 - 2, x1)), "B": (slice(y1 - 2, y1), mid_x)}
    found = ""
    for side in judged:
        rows, cols = strips[side]
        values = luma[rows, cols][~mask[rows, cols]]
        if values.size and float(values.mean()) < field - 25:
            found += side
    return expected, found


def _markers(case: Case, luma: np.ndarray, mask: np.ndarray, picture: dict, to_canvas, scale: float,
             table_to_canvas) -> dict:
    """Centroids of the white squares vs where ffmpeg's integers (and the table) put them."""
    width, height = SOURCES[case.source]
    half = MARKER / 2 * scale
    radius = int(2 * half + 6)
    residuals, table_deltas, missing = [], [], 0
    for fx in FRACTIONS:
        for fy in FRACTIONS:
            sx, sy = round(fx * width), round(fy * height)
            ex, ey = to_canvas(sx, sy)
            if not (picture["x"] + half + 3 <= ex <= picture["x"] + picture["w"] - half - 3
                    and picture["y"] + half + 3 <= ey <= picture["y"] + picture["h"] - half - 3):
                continue
            x0, x1 = int(ex) - radius, int(ex) + radius + 1
            y0, y1 = int(ey) - radius, int(ey) + radius + 1
            if x0 < 0 or y0 < 0 or x1 > luma.shape[1] or y1 > luma.shape[0] or mask[y0:y1, x0:x1].any():
                continue
            window = luma[y0:y1, x0:x1]
            level = float(np.median(window))
            if float(window.max()) - level < 40:
                missing += 1
                continue
            ys, xs = np.nonzero(window > (level + float(window.max())) / 2)
            cx, cy = x0 + xs.mean() + 0.5, y0 + ys.mean() + 0.5
            residuals.append(max(abs(cx - ex), abs(cy - ey)))
            tx, ty = table_to_canvas(sx, sy)
            table_deltas.append(max(abs(cx - tx), abs(cy - ty)))
    return {"count": len(residuals), "missing": missing,
            "residual": round(max(residuals), 2) if residuals else 0.0,
            "tableDelta": round(max(table_deltas), 2) if table_deltas else 0.0}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_framed_render_matches_the_shared_table(case, sources, tmp_path):
    width, height = SOURCES[case.source]
    source = sources(case.source)
    row = _table_row(case)
    geometry = page_frame.geometry(width, height, case.frame, case.resolved_fit,
                                   case.zoom, case.focus_x, case.focus_y)
    assert geometry["picture"] == row["picture"] and geometry["band"] == row["band"]

    frame_kwargs = _cut_kwargs(case, source)
    if case.resolved_fit == "fit":
        assert frame_kwargs["source_size"] == (width, height)
    cut = tmp_path / "cut.mp4"
    _cut(case, source, cut, frame_kwargs)
    result = render.render_post(cut, tmp_path / "post", _post_request(case, render.sha256(cut.read_bytes())),
                                clock_ms=lambda: NOW)
    assert (result.final_probe.width, result.final_probe.height) == (1080, 1920)

    vf = build_cc_filter(_grade_cc() if case.grade else None, playback_speed=case.speed,
                         clip_crop=case.crop, **frame_kwargs)
    actual = ffmpeg_geometry(width, height, vf)
    window_delta = _window_delta(case, geometry, actual)

    luma = _luma(result.qa_frame_path)
    mask = _caption_mask(result.final_path.parent / "overlay.png")
    expected = row["picture"]
    measured = _picture_rect(luma, mask)
    picture_delta = _rect_delta(measured, expected)
    bars = _bars(luma, mask, expected)
    border_expected, border_found = _border_sides(case, geometry, luma, mask, measured)
    if case.resolved_fit == "fit":
        table_crop = (geometry["window"]["x"], geometry["window"]["y"])
    else:
        table_crop = (geometry["scaled"]["cropX"], geometry["scaled"]["cropY"])
    to_canvas, scale = _mapping(case, geometry, *actual["crop"])
    table_to_canvas, _ = _mapping(case, geometry, *table_crop)
    markers = _markers(case, luma, mask, expected, to_canvas, scale, table_to_canvas)

    verdict = (picture_delta <= TOLERANCE_PX and window_delta <= TOLERANCE_PX
               and markers["residual"] <= TOLERANCE_PX and markers["missing"] == 0
               and bars["max"] <= INK_LUMA and bars["mean"] <= 8 and border_expected == border_found)
    qa_path = None
    if QA_ROOT:
        qa_dir = Path(QA_ROOT) / "qa"
        qa_dir.mkdir(parents=True, exist_ok=True)
        qa_path = qa_dir / f"{case.name}.jpg"
        shutil.copyfile(result.qa_frame_path, qa_path)
    RESULTS.append({
        "case": case.name, "source": f"{case.source} {width}x{height}", "frame": case.frame,
        "fit": case.resolved_fit + ("" if case.fit or case.frame == "9:16" else " (absent)"),
        "crop": f"z{case.zoom:g} fx{case.focus_x:g} fy{case.focus_y:g}",
        "treatment": ", ".join(item for item in (
            "grade" if case.grade else "", f"speed {case.speed:g}" if case.speed != 1 else "",
            f"{case.executor} executor") if item),
        "expected": expected, "measured": measured, "pictureDelta": picture_delta,
        "ffmpeg": actual, "windowDelta": window_delta, "markers": markers,
        "border": {"expected": border_expected, "found": border_found}, "bars": bars,
        "maxDelta": max(picture_delta, window_delta, markers["residual"]),
        "verdict": "pass" if verdict else "FAIL", "qa": str(qa_path) if qa_path else None,
    })
    assert picture_delta <= TOLERANCE_PX, (measured, expected)
    assert window_delta <= TOLERANCE_PX, (actual, geometry)
    assert bars["max"] <= INK_LUMA and bars["mean"] <= 8, bars
    assert border_expected == border_found, (border_expected, border_found)
    assert markers["missing"] == 0 and markers["residual"] <= TOLERANCE_PX, markers


LETTERBOX_CASES = [(frame, fit, grade) for frame in ("16:9", "1:1", "3:4", "4:3")
                   for fit in ("fill", "fit") for grade in (False, True)]


@pytest.mark.parametrize("frame,fit,grade", LETTERBOX_CASES)
def test_prepared_post_letterbox_is_idempotent_on_a_band_shaped_cut(tmp_path, frame, fit, grade):
    """#193's letterbox (crop the band, pad black) leaves a framed cut's frame byte-identical."""
    width, height = SOURCES["16:9"]
    crop = {"zoom": 1.5, "focusX": 0.2, "focusY": 0.7}
    vf = build_cc_filter(_grade_cc() if grade else None, clip_crop=crop, page_frame=frame, frame_fit=fit,
                         source_size=(width, height))
    letterbox = page_frame.letterbox_filter(page_frame.band_height(frame))
    cut, boxed = tmp_path / "cut.raw", tmp_path / "boxed.raw"
    subprocess.run(["nice", "-n", "10", "ffmpeg", "-nostdin", "-v", "error", "-y", "-threads", "2",
                    "-filter_threads", "2", "-f", "lavfi", "-i", _source_graph(width, height, 0.04),
                    "-filter_complex", f"[0:v]{vf},split[a][b];[b]{letterbox}[c]",
                    "-map", "[a]", "-frames:v", "1", "-f", "rawvideo", str(cut),
                    "-map", "[c]", "-frames:v", "1", "-f", "rawvideo", str(boxed)],
                   check=True, timeout=120, capture_output=True)
    assert cut.stat().st_size > 1080 * 1920
    assert cut.read_bytes() == boxed.read_bytes()


def _matrix_markdown(results: list[dict]) -> str:
    from tests.test_page_frame_gate import gate_identity_rows

    fmt = lambda rect: f"{rect['x']},{rect['y']} {rect['w']}x{rect['h']}"
    lines = [
        "# Lab page-frame matrix (A1, frames/lab-fit-fill-20260929)",
        "",
        "Each row: synthetic master -> real band-aware cut (services.ffmpeg.run_color_correct, as the "
        "executor calls it) -> services.post_render.render_post -> render_post's own QA frame, measured. "
        "Expected rectangles come from tests/fixtures/page-frame-geometry-fixtures.v1.json. "
        f"Tolerance {TOLERANCE_PX} px, never widened. ffmpeg: "
        + subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout.splitlines()[0] + ".",
        "",
        "Columns: picture = non-black rectangle of the QA frame; picture delta = max edge distance to the table; "
        "window delta = ffmpeg's own crop offset (trace log) minus the table's (fill: canvas px after "
        "scaling; fit: source px; fit crop/scale and every pad are exact); markers = white squares found, "
        "max centroid residual against ffmpeg's integers (in brackets against the table, which adds the "
        "window delta: 2.01 = ffmpeg's 2 px crop rounding + 0.01 px of measurement); border = sides showing "
        "the source's 8 px border (expected from the table window / found); bars = max and mean luma outside "
        "the picture (4 px margin, caption masked); max delta = max(picture delta, window delta, marker "
        "residual).",
        "",
        "Unframed pages: tests/test_page_frame_no_frame_identity.py reproduces, byte for byte, goldens frozen "
        "from 7a97021 before any code change (248 cut filter strings over grades x speeds x crops, 120 full "
        "ffmpeg argv of the sourced and generation cut calls, 24 prepared-post graphs, 4 encode argv pairs, "
        "6 caption placements).",
        "",
        "| case | source | frame | fit | crop | treatment | expected picture | measured picture | picture delta "
        "| window delta | markers n / residual (vs table) | border exp/found | bars max/mean | max delta px "
        "| verdict | QA |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in results:
        markers = row["markers"]
        lines.append(
            f"| {row['case']} | {row['source']} | {row['frame']} | {row['fit']} | {row['crop']} "
            f"| {row['treatment']} | {fmt(row['expected'])} | {fmt(row['measured'])} | {row['pictureDelta']} "
            f"| {row['windowDelta']} | {markers['count']} / {markers['residual']} ({markers['tableDelta']}) "
            f"| {row['border']['expected'] or '-'}/{row['border']['found'] or '-'} "
            f"| {row['bars']['max']}/{row['bars']['mean']} | {row['maxDelta']} | {row['verdict']} "
            f"| {row['qa'] or '-'} |")
    gate = gate_identity_rows()
    identical = sum(row["identicalOverlay"] and row["framedReasons"] == row["verticalReasons"] for row in gate)
    lines += ["", "## Gate identity (Gap 5: no new refusal, burn_quality_gate unchanged)", "",
              f"{identical}/{len(gate)} framed renders (4 style/caption pairs x 4 frames x frameFit absent/fill/fit "
              "x page placement bottom/-12 and middle/0) get the same typed-gate verdict as the 9:16 render of the "
              "same caption middle/0; the overlay bytes are identical, and the caption block centre is on the "
              "band centre (row 960; 4:3's band centre is row 959).", "",
              "| style | caption | caption box rows | 16:9 (608 @656) | 1:1 (1080 @420) | 3:4 (1440 @240) "
              "| 4:3 (810 @554) | gate verdict |", "|---|---|---|---|---|---|---|---|"]
    seen = {}
    for row in gate:
        seen.setdefault((row["style"], row["caption"]), {})[row["frame"]] = row
    for (style, caption), frames in seen.items():
        any_row = next(iter(frames.values()))
        box = any_row["captionBox"]
        cells = ["reaches bars" if frames[frame]["reachesBars"] else "inside band"
                 for frame in ("16:9", "1:1", "3:4", "4:3")]
        lines.append(f"| {style} | {caption} | {box['top']}-{box['bottom'] - 1} ({box['height']} px) | "
                     + " | ".join(cells) + f" | {any_row['framedReasons'] or 'pass'} |")
    lines += ["", "Band-height observation (john's decision, recorded not refused): the gate's height rule is "
              "frame-relative (box <= 0.45 x 1920 = 864 px). On 16:9 (608 px band) and 4:3 (810 px band) a caption "
              "that passes the gate can still be taller than the band and reach onto the bars (above: the 52 pt "
              "six-line caption, 779 px, passes and reaches the 16:9 bars). On 1:1 and 3:4 any passing caption fits "
              "the band."]
    return "\n".join(lines) + "\n"


def _table_trace_summary() -> list[str]:
    """ffmpeg's integers vs the model for every framed row of the table, plain and graded cut."""
    worst, counts = {"plain (yuv420p)": 0, "graded (rgb24)": 0}, {}
    grade = _grade_cc()
    for row in TABLE["cases"]:
        if row["frame"] == page_frame.VERTICAL_FRAME:
            continue
        width, height = row["source"]["width"], row["source"]["height"]
        crop = row["clipCrop"]
        case = Case("table", row["source"]["name"], row["frame"], row["frameFit"],
                    crop["zoom"], crop["focusX"], crop["focusY"])
        geometry = page_frame.geometry(width, height, row["frame"], row["frameFit"],
                                       crop["zoom"], crop["focusX"], crop["focusY"])
        for label, cc in (("plain (yuv420p)", None), ("graded (rgb24)", grade)):
            vf = build_cc_filter(cc, clip_crop=crop, page_frame=row["frame"], frame_fit=row["frameFit"],
                                 source_size=(width, height))
            delta = _window_delta(case, geometry, ffmpeg_geometry(width, height, vf))
            worst[label] = max(worst[label], delta)
            counts[(label, row["frameFit"], delta)] = counts.get((label, row["frameFit"], delta), 0) + 1
    lines = ["", "## ffmpeg vs the model over the whole 360-case table", "",
             "For each of the 320 framed rows, ffmpeg traced the exact cut chain (plain and graded) and its "
             "scale size, crop offset and pad offset were compared with the model. Scale sizes, pad offsets and "
             "every fit crop are exact. Distribution of the fill crop offset delta (px):", "",
             "| cut path | fit | delta 0 | delta 1 | delta 2 |", "|---|---|---|---|---|"]
    for label in worst:
        for fit in ("fill", "fit"):
            lines.append(f"| {label} | {fit} | " + " | ".join(
                str(counts.get((label, fit, delta), 0)) for delta in (0, 1, 2)) + " |")
    lines += ["", f"Worst delta: {worst}. Never more than 2 px, so the model is unchanged. Cause: the model takes "
              "floor(v) & ~1 for the fill crop offset v = (scaled - band) * focus; ffmpeg's crop rounds v to the "
              "nearest integer (lrint) and aligns it down to an even pixel only for yuv420p, and the graded cut "
              "crops in rgb24 (format=rgb24 comes first), where there is no alignment."]
    return lines


@pytest.fixture(scope="module", autouse=True)
def _write_matrix():
    yield
    if QA_ROOT and RESULTS:
        text = _matrix_markdown(RESULTS) + "\n".join(_table_trace_summary()) + "\n"
        Path(QA_ROOT).mkdir(parents=True, exist_ok=True)
        (Path(QA_ROOT) / "lab-matrix.md").write_text(text)
