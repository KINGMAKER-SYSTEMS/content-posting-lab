"""DRAFT executor: montage bodies built from planned source windows.

The module is not wired into jobs; these tests pin its own contract.
"""
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from services.control_plane_sources import plan_source_cuts
from services.ffmpeg import delivery_encode_args
from services.source_montage import (
    MONTAGE_FPS,
    montage_ffmpeg_args,
    montage_is_planned,
    plan_montage,
    render_montage,
)
from tests.test_control_plane_source_execution import (  # noqa: F401 (autouse fixture)
    offline_shipstream_manifest,
)
from tests.test_source_time_frames import _long_page_master_recipe
from tests.test_source_time_frames import _isolated_source_dna  # noqa: F401 (fixture)


def _overlaps(a, b):
    return (a.master.sha256 == b.master.sha256
            and a.start_ms < b.start_ms + b.duration_ms
            and b.start_ms < a.start_ms + a.duration_ms)


def test_same_seed_same_montage_and_slices_sit_inside_their_windows():
    recipe, _ = _long_page_master_recipe()
    first = plan_montage(recipe, set(), seed="job-1", cuts=3, total_ms=7_500)
    again = plan_montage(recipe, set(), seed="job-1", cuts=3, total_ms=7_500)
    assert first == again
    assert first.total_frames == 225
    assert montage_is_planned(recipe, first)
    for item in first.slices:
        assert 0 <= item.offset_ms
        assert item.offset_ms + item.length_ms <= item.window.duration_ms


def test_windows_are_disjoint_and_never_reused_across_jobs():
    recipe, _ = _long_page_master_recipe()
    served: set[str] = set()
    windows = []
    for n in range(5):
        plan = plan_montage(recipe, served, seed=f"job-{n}", cuts=3)
        assert plan is not None
        served |= set(plan.reserved_slot_ids)
        windows += [s.window for s in plan.slices]
    for i, a in enumerate(windows):
        for b in windows[i + 1:]:
            assert not _overlaps(a, b)
    # A later recut plan also refuses every window the montages reserved.
    for cut in plan_source_cuts(recipe, 10, served, seed="after"):
        assert all(not _overlaps(cut, w) for w in windows)


def test_bounds_are_enforced():
    recipe, _ = _long_page_master_recipe()
    for bad in (dict(cuts=1), dict(cuts=5), dict(total_ms=4_000), dict(total_ms=10_000),
                dict(cuts=4, total_ms=5_000)):
        with pytest.raises(ValueError):
            plan_montage(recipe, set(), seed="x", **bad)


def test_exhausted_page_returns_none():
    recipe, master = _long_page_master_recipe(duration_ms=230_000, floor_ms=210_000)
    served: set[str] = set()
    results = []
    for n in range(20):
        plan = plan_montage(recipe, served, seed=f"j{n}", cuts=3)
        results.append(plan)
        if plan is None:
            break
        served |= set(plan.reserved_slot_ids)
    assert results[-1] is None


def test_args_are_silent_and_keep_the_delivery_video_settings(tmp_path):
    recipe, master = _long_page_master_recipe()
    plan = plan_montage(recipe, set(), seed="args", cuts=3)
    args = montage_ffmpeg_args(plan, {master.sha256: tmp_path / "m.mp4"}, tmp_path / "o.mp4",
                               delivery_encode_args("tiktok_delivery_v1"))
    assert "-an" in args and "-c:a" not in args and "-b:a" not in args
    assert args[args.index("-c:v") + 1] == "libx264"
    assert args[args.index("-pix_fmt") + 1] == "yuv420p"
    assert args[args.index("-frames:v") + 1] == str(plan.total_frames)


@pytest.mark.skipif(subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0,
                    reason="ffmpeg not installed")
async def test_render_produces_a_body_the_finished_clip_route_accepts(tmp_path):
    recipe, master = _long_page_master_recipe(duration_ms=260_000, floor_ms=210_000)
    src = tmp_path / "master.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=s=1280x720:r=30:d=260", "-f", "lavfi", "-i", "sine=d=260",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(src)],
                   check=True)
    plan = plan_montage(recipe, set(), seed="render", cuts=3, total_ms=7_500)
    out = tmp_path / "body.mp4"
    result = await render_montage(recipe, plan, {master.sha256: src}, out,
                                  delivery_encode_args("tiktok_delivery_v1"))
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format",
         "-of", "json", str(out)], capture_output=True, text=True, check=True).stdout)
    video = [s for s in probe["streams"] if s["codec_type"] == "video"]
    audio = [s for s in probe["streams"] if s["codec_type"] == "audio"]
    assert len(video) == 1 and not audio
    v = video[0]
    assert (v["codec_name"], v["width"], v["height"], v["pix_fmt"]) == ("h264", 1080, 1920, "yuv420p")
    assert v["r_frame_rate"] == f"{MONTAGE_FPS}/1"
    assert int(v["nb_read_frames"]) == plan.total_frames == result["frames"] == 225
    assert 5.0 <= float(probe["format"]["duration"]) <= 9.0
    assert Path(result["output"]).stat().st_size <= 32 * 1024 * 1024


def test_tampered_plan_is_refused():
    recipe, _ = _long_page_master_recipe()
    plan = plan_montage(recipe, set(), seed="t", cuts=3)
    moved = replace(plan.slices[0], window=replace(plan.slices[0].window, start_ms=1))
    assert not montage_is_planned(recipe, replace(plan, slices=(moved, *plan.slices[1:])))
