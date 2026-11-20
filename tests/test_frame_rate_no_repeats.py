"""Renders keep the clip's own frame rate: no repeated frames, and an even beat.

A forced 30 fps output repeats frames whenever the clip has fewer frames per
second than that: a 0.7x speed change (30 fps becomes 21 fps of picture, so
about 3 in every 10 output frames were repeats) or a 24p/25p body re-encoded
for delivery (1 in 4 or 1 in 5). Repeats play back as a stutter.

Two checks, both needed:
- repeats: a frame whose average luma change from the previous frame is under
  0.3 (the same near-duplicate check used to measure live bodies);
- evenness: every gap between frame timestamps equals 1/rate within 1 ms. A
  variable-rate output can carry no repeats and still stutter: 21 fps of frames
  placed on a 30 Hz clock land 33 ms and 67 ms apart.

A clip under 20 fps of picture (a 0.55x cut of 30p is 16.5) is delivered with
every frame shown exactly k times (33 fps, k = 2): the repeats are then a fixed
pattern, so the beat stays even, and the upload is never under 20 fps.
"""
import asyncio
import json
import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from services import post_render
from services import slot_source_regeneration as regen
from services.clip_transforms import CutTransforms, apply_transforms
from services.ffmpeg import (
    _probe_input_duration_seconds,
    delivery_encode_args,
    DELIVERY_LENGTH_MIDDLE,
    delivery_frame_rate,
    output_frame_count,
    parse_frame_rate,
    run_color_correct,
    with_frame_rate,
)

DELIVERY = "tiktok_delivery_v1"


# -- the rate rule ----------------------------------------------------------

@pytest.mark.parametrize("native,speed,expected", [
    ("30/1", 0.7, Fraction(21)),
    ("30000/1001", 1.0, Fraction(30000, 1001)),
    ("30000/1001", 0.85, Fraction(25500, 1001)),
    ("25/1", 1.0, Fraction(25)),
    ("24/1", 0.7, Fraction(168, 5)),  # 16.8 -> 33.6
    ("60/1", 1.0, Fraction(30)),     # every 2nd frame
    ("50/1", 1.0, Fraction(25)),
    ("48/1", 1.0, Fraction(24)),
    ("120/1", 1.0, Fraction(30)),    # every 4th frame
    ("60000/1001", 1.0, Fraction(30000, 1001)),
    ("60/1", 0.7, Fraction(21)),     # 42 fps: every 2nd frame
    ("30/1", 1.1, Fraction(33)),     # 20 up to 40 stays as it is
    ("30/1", 1.3, Fraction(39)),
    ("30/1", 0.75, Fraction(45, 2)),
    ("30/1", 0.65, Fraction(39)),    # 19.5 fps: every frame shown twice
    ("30/1", 0.6, Fraction(36)),     # 18 -> 36
    ("30/1", 0.55, Fraction(33)),    # 16.5 -> 33
    ("16/1", 0.7, Fraction(112, 5)),  # an AI clip: 11.2 -> 22.4
    ("8/1", 1.0, Fraction(24)),      # 8 -> 24, every frame shown 3 times
])
def test_one_exact_rate_per_output(native, speed, expected):
    assert delivery_frame_rate(parse_frame_rate(native), speed) == expected


_NATIVES = [Fraction(n) for n in (8, 12, 15, 16, 24, 25, 30, 48, 50, 60, 90, 120)] + [
    Fraction(24000, 1001), Fraction(30000, 1001), Fraction(60000, 1001)]
_SPEEDS = [0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 1.0, 1.06, 1.1, 1.12, 1.2, 1.3]


@pytest.mark.parametrize("native", _NATIVES)
def test_every_rate_lands_between_20_and_40_on_an_even_beat(native):
    for speed in _SPEEDS:
        picture = native * Fraction(f"{speed:.6f}")
        rate = delivery_frame_rate(native, speed)
        assert 20 <= rate < 40, (native, speed, rate)
        # Either every frame shown k times or every k-th frame kept: whole k.
        assert (rate / picture).denominator == 1 or (picture / rate).denominator == 1, (native, speed, rate)
        # A later step that re-reads the cut's rate (the final, a transform) keeps it.
        assert delivery_frame_rate(rate, 1.0) == rate, (native, speed, rate)


def test_a_whole_frame_ending_stays_inside_the_worker_length_window():
    from services.control_plane_sources import CUT_LENGTH_MAX_MS, CUT_LENGTH_MIN_MS
    assert DELIVERY_LENGTH_MIDDLE == Fraction(CUT_LENGTH_MIN_MS + CUT_LENGTH_MAX_MS, 2000)
    rates = {delivery_frame_rate(Fraction(n), s) for n in (24, 25, 30, 50, 60)
             for s in (0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.3, 1.5)}
    edges = [*range(5_000, 5_060), *range(6_990, 7_010), *range(8_941, 9_001)]
    for rate in rates:
        for ms in edges:
            frames = output_frame_count(Fraction(ms, 1000), rate)
            assert 5 <= frames / rate <= 9, (rate, ms)
            assert abs(frames / rate - Fraction(ms, 1000)) < 1 / rate


@pytest.mark.parametrize("value", ["0/0", "30/0", "0/1", "90000/1", "N/A", "", None, "30"])
def test_unreadable_or_container_rates_are_not_rates(value):
    assert parse_frame_rate(value) is None


def test_preset_keeps_30_only_as_the_unknown_rate_fallback():
    args = delivery_encode_args(DELIVERY)
    assert args[args.index("-r") + 1] == "30"
    assert "-fps_mode" not in args and "-vsync" not in args
    swapped = with_frame_rate(args, Fraction(30000, 1001))
    assert swapped[swapped.index("-r") + 1] == "30000/1001" and swapped.count("-r") == 1
    assert with_frame_rate(["-c:v", "libx264"], Fraction(21))[-2:] == ["-r", "21"]
    with pytest.raises(ValueError):  # a bare trailing -r is refused, never doubled
        with_frame_rate(["-c:v", "libx264", "-r"], Fraction(21))


# -- media helpers ----------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def ffmpeg_tools():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe are required for the frame-rate proof")


def _ffmpeg(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", *args],
                          check=True, timeout=180, capture_output=True, text=True)


def _moving_source(root: Path, rate: str, seconds: float = 3, size: str = "540x960") -> Path:
    # testsrc2 changes every frame, so any repeated frame reads as YDIF ~0.
    path = root / f"source-{rate.replace('/', '_')}p.mp4"
    _ffmpeg("-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}", "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", "2",
            "-pix_fmt", "yuv420p", str(path))
    return path


def _repeat_flags(path: Path) -> list[bool]:
    """For every frame after the first: is it a repeat of the frame before?"""
    result = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-v", "info", "-i", str(path), "-an",
                             "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YDIF",
                             "-f", "null", "-"], check=True, timeout=180, capture_output=True, text=True)
    ydif = [float(value) for value in re.findall(r"YDIF=([0-9.]+)", result.stderr)][1:]
    assert ydif, "no frames decoded"
    return [value < 0.3 for value in ydif]


def _repeated_frames(path: Path) -> tuple[int, int]:
    flags = _repeat_flags(path)
    return sum(flags), len(flags) + 1


def _frame_gaps(path: Path) -> list[float]:
    raw = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "packet=pts_time", "-of", "json", str(path)],
                         check=True, timeout=120, capture_output=True, text=True).stdout
    times = sorted(float(row["pts_time"]) for row in json.loads(raw)["packets"])
    return [later - earlier for earlier, later in zip(times, times[1:])]


def _assert_clean(path: Path, rate: Fraction, label: str, shown: int = 1) -> None:
    """No repeats (or, with ``shown`` = k, every frame shown exactly k times) and an even beat."""
    flags = _repeat_flags(path)
    gaps = _frame_gaps(path)
    step = 1 / float(rate)
    uneven = [gap for gap in gaps if abs(gap - step) > 0.001]
    if shown == 1:
        assert not any(flags), f"{label}: {sum(flags)}/{len(flags) + 1} repeated frames"
    else:
        # Frame i (i >= 1) is new exactly when i is a multiple of k.
        off = [i for i, repeat in enumerate(flags, start=1) if repeat != (i % shown != 0)]
        assert not off, (f"{label}: {len(off)}/{len(flags) + 1} frames break the shown-{shown}-times "
                         f"pattern (first at frame {off[0]}; {sum(flags)} repeats)")
    assert not uneven, (f"{label}: {len(uneven)}/{len(gaps)} gaps are not {step * 1000:.2f} ms "
                        f"(min {min(gaps) * 1000:.2f}, max {max(gaps) * 1000:.2f})")


def _cut(source: Path, out: Path, speed: float, encode_args=None) -> Path:
    asyncio.run(run_color_correct(str(source), str(out), None, scale=None,
                                  encode_args=encode_args, playback_speed=speed))
    return out


# -- the cut ----------------------------------------------------------------

@pytest.mark.parametrize("rate,speed,expected", [
    ("30", 0.7, Fraction(21)),
    ("24", 0.7, Fraction(168, 5)),
    ("25", 0.85, Fraction(85, 4)),
    ("30000/1001", 0.7, Fraction(21000, 1001)),
    ("25", 1.0, Fraction(25)),
    ("60", 1.0, Fraction(30)),
    ("30", 0.55, Fraction(33)),       # 16.5 fps of picture, each frame shown twice
    ("16", 0.7, Fraction(112, 5)),    # a 16 fps AI clip slowed: 11.2 -> 22.4
])
def test_cut_has_no_repeats_and_an_even_beat(tmp_path, rate, speed, expected):
    source = _moving_source(tmp_path, rate)
    cut = _cut(source, tmp_path / "cut.mp4", speed, delivery_encode_args(DELIVERY))
    native = parse_frame_rate(rate if "/" in rate else f"{rate}/1")
    picture = native * Fraction(f"{speed:.6f}")
    shown = int(expected / picture) if expected > picture else 1
    _assert_clean(cut, expected, f"{rate}p x{speed}", shown)
    if expected >= picture:  # nothing decimated: every source frame is still there
        assert _repeated_frames(cut)[1] // shown >= int(3 * native) - 2


def test_generated_clip_slowed_on_the_standard_encode_is_clean(tmp_path):
    source = _moving_source(tmp_path, "24")
    _assert_clean(_cut(source, tmp_path / "cut.mp4", 0.7), Fraction(168, 5), "standard 24p x0.7", shown=2)


# -- the cut window (the path every Scrambler and Layer B cut takes) ----------

def _long_source(root: Path) -> Path:
    path = root / "long-30p.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30:duration=12",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", "2",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-shortest", str(path))
    return path


def _stream_seconds(path: Path, stream: str) -> float:
    raw = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream,
                          "-show_entries", "stream=duration", "-of", "json", str(path)],
                         check=True, timeout=60, capture_output=True, text=True).stdout
    return float(json.loads(raw)["streams"][0]["duration"])


@pytest.mark.parametrize("speed,expected", [
    (0.6, Fraction(36)), (0.65, Fraction(39)), (0.7, Fraction(21)),
    (0.75, Fraction(45, 2)), (0.8, Fraction(24)), (1.1, Fraction(33)),
])
def test_a_9_second_window_cut_never_runs_past_9_seconds(tmp_path, speed, expected):
    # The Scrambler draws an origin window of length x speed (scrambler.window_ms)
    # and the router refuses a delivered cut over 9.0 s.
    out = tmp_path / "cut.mp4"
    asyncio.run(run_color_correct(str(_long_source(tmp_path)), str(out), None, scale=None,
                                  encode_args=delivery_encode_args(DELIVERY), playback_speed=speed,
                                  clip_start_ms=500, clip_duration_ms=round(9_000 * speed)))
    delivered = _probe_input_duration_seconds(str(out))  # the router's own probe
    frame = 1 / float(expected)
    assert delivered is not None and 9.0 - frame - 0.001 < delivered <= 9.0, f"x{speed}: {delivered}"
    video, audio = _stream_seconds(out, "v:0"), _stream_seconds(out, "a:0")
    assert video <= 9.0 and abs(video - audio) < frame, f"x{speed}: video {video}, audio {audio}"
    picture = Fraction(30) * Fraction(f"{speed:.6f}")
    _assert_clean(out, expected, f"9 s window x{speed}", int(expected / picture) if expected > picture else 1)


# -- the final --------------------------------------------------------------

def _final(source: Path, out: Path, paired: bool = True) -> post_render.MediaProbe:
    tools = post_render.RenderTools()
    probe = post_render._probe(source, tools)
    final = post_render._encode_final(source, None, out, probe, tools, in_app=True,
                                      normalize_pair_timing=paired)
    assert abs(final.duration_ms - probe.duration_ms) <= 80  # the length guard still holds
    return final


@pytest.mark.parametrize("paired", [True, False])
@pytest.mark.parametrize("rate", ["25", "24"])
def test_final_keeps_a_native_rate_body_clean(tmp_path, rate, paired):
    source = _moving_source(tmp_path, rate)
    _final(source, tmp_path / "final.mp4", paired)
    _assert_clean(tmp_path / "final.mp4", Fraction(int(rate)), f"{rate}p final")


def test_final_of_a_60p_body_drops_every_second_frame_evenly(tmp_path):
    source = _moving_source(tmp_path, "60")
    _final(source, tmp_path / "final.mp4")
    _assert_clean(tmp_path / "final.mp4", Fraction(30), "60p final")


def test_slowed_cut_through_the_final_stays_clean(tmp_path):
    source = _moving_source(tmp_path, "30")
    cut = _cut(source, tmp_path / "cut.mp4", 0.7, delivery_encode_args(DELIVERY))
    _final(cut, tmp_path / "final.mp4")
    _assert_clean(tmp_path / "final.mp4", Fraction(21), "0.7x cut + final")


def test_a_24p_cut_at_0_6_clears_the_final_length_guard(tmp_path):
    # 14.4 fps of picture is 69 ms a frame, close to the final's 80 ms length
    # guard. Lifted to 28.8 fps (each frame shown twice) a frame is 35 ms.
    source = _moving_source(tmp_path, "24")
    cut = _cut(source, tmp_path / "cut.mp4", 0.6, delivery_encode_args(DELIVERY))
    _final(cut, tmp_path / "final.mp4")  # asserts the 80 ms guard
    _assert_clean(tmp_path / "final.mp4", Fraction(144, 5), "24p x0.6 cut + final", shown=2)


def test_a_shown_twice_cut_keeps_its_pattern_through_the_final(tmp_path):
    source = _moving_source(tmp_path, "30")
    cut = _cut(source, tmp_path / "cut.mp4", 0.55, delivery_encode_args(DELIVERY))
    _final(cut, tmp_path / "final.mp4")
    _assert_clean(tmp_path / "final.mp4", Fraction(33), "0.55x cut + final", shown=2)


def test_a_paired_final_fails_closed_when_its_rate_cannot_be_read(tmp_path, monkeypatch):
    # The clean and captioned twins must come out at one rate; a twin that
    # cannot read the rate must not quietly fall back to 30.
    source = _moving_source(tmp_path, "25", seconds=1)
    monkeypatch.setattr(post_render, "probe_frame_rate", lambda *_a, **_k: None)
    with pytest.raises(post_render.PostRenderError) as error:
        _final(source, tmp_path / "final.mp4", paired=True)
    assert error.value.code == "frame_rate_unreadable"
    _final(source, tmp_path / "lone.mp4", paired=False)  # a lone final keeps 30 as before
    assert _frame_gaps(tmp_path / "lone.mp4")[0] == pytest.approx(1 / 30, abs=0.001)


# -- the final: rate changes and duration repairs (#209 step 2) -------------

def _sync_source_at(root: Path, rate: str) -> Path:
    """A clip at ``rate`` with one white frame at 0.6 s and a 1 kHz beep at the same 0.6 s."""
    path = root / f"sync-{rate}.mp4"
    _ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=540x960:rate={rate}:duration=4",
            "-f", "lavfi", "-i",
            "aevalsrc='if(between(t,0.6,0.6333),0.8*sin(2*PI*1000*t),0)':s=48000:d=4",
            "-vf", "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='between(t,0.595,0.605)'",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", "2",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-shortest", str(path))
    return path


def test_a_lone_final_of_a_sub_20_body_shows_every_frame_twice_from_the_first(tmp_path):
    # A 16 fps body (an AI clip delivered without a speed cut) goes out at 32
    # fps with every frame shown exactly twice. (main forced 30 fps: uneven
    # repeats.)
    source = _moving_source(tmp_path, "16")
    _final(source, tmp_path / "final.mp4", paired=False)
    _assert_clean(tmp_path / "final.mp4", Fraction(32), "16p lone final", shown=2)


@pytest.mark.parametrize("rate,expected", [("50", Fraction(25)), ("60", Fraction(30))])
def test_a_lone_final_that_drops_frames_keeps_picture_and_sound_together(tmp_path, rate, expected):
    source = _sync_source_at(tmp_path, rate)
    before = _flash_time(source) - _beep_time(source)
    _final(source, tmp_path / "final.mp4", paired=False)
    final = tmp_path / "final.mp4"
    _assert_clean(final, expected, f"{rate}p lone final")
    # The white frame sits on the final's own grid, with the beep. Keeping the
    # frames between grid points (the encoder's -r) drops it and shows every
    # kept frame up to one source frame off the sound.
    try:
        flash = _flash_time(final)
    except AssertionError:
        pytest.fail(f"{rate}p final dropped the frame that lines up with the beep: its frames are off the sound's clock")
    drift = (flash - _beep_time(final)) - before
    assert abs(drift) < 1 / float(expected) / 2, f"{rate}p final moved the picture {drift * 1000:+.1f} ms off the sound"


def _packet_times(path: Path) -> list[float]:
    raw = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "packet=pts_time", "-of", "json", str(path)],
                         check=True, timeout=120, capture_output=True, text=True).stdout
    return sorted(float(row["pts_time"]) for row in json.loads(raw)["packets"])


def _variable_rate_source(root: Path) -> Path:
    """A healed-style body: 30p with every 6th frame cut out, the rest at their own times (25 fps mean)."""
    path = root / "variable.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=540x960:rate=30:duration=3",
            "-vf", "select='not(eq(mod(n\\,6)\\,5))'", "-fps_mode", "passthrough", "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", "2",
            "-pix_fmt", "yuv420p", str(path))
    return path


@pytest.mark.parametrize("paired", [False, True])
def test_a_variable_rate_heal_keeps_its_own_frame_times_through_the_final(tmp_path, paired):
    # A heal cuts a body's repeated frames out and keeps the rest at their own
    # times. Made 30 fps again at delivery, every gap is filled with a repeat
    # (a Josie heal went from 188 frames back to 222). The final keeps them.
    source = _variable_rate_source(tmp_path)
    assert 24 < post_render.probe_variable_frame_rate(source) < 26  # read as variable rate
    _final(source, tmp_path / "final.mp4", paired)
    final = tmp_path / "final.mp4"
    assert not any(_repeat_flags(final)), f"{sum(_repeat_flags(final))} repeated frames in the final"
    before, after = _packet_times(source), _packet_times(final)
    assert len(after) == len(before), f"{len(before)} frames became {len(after)}"
    assert max(abs(a - b) for a, b in zip(before, after)) < 0.001, "frame times moved"


def _repaired_final(source: Path, out: Path) -> dict:
    from services.source_duration_repair import encode_shorter_final

    return encode_shorter_final(source, out)


@pytest.mark.parametrize("rate", ["25", "24", "21"])
def test_a_repaired_final_under_30_keeps_its_own_rate(tmp_path, rate):
    source = _moving_source(tmp_path, rate)
    _repaired_final(source, tmp_path / "final.mp4")
    _assert_clean(tmp_path / "final.mp4", Fraction(int(rate)), f"{rate}p repaired final")


def _repeat_map(path: Path) -> str:
    return "".join("R" if repeat else "." for repeat in _repeat_flags(path))


@pytest.mark.parametrize("rate,frames,expected", [
    ("33", 99, Fraction(33, 2)),   # a 1.1x cut of 30p: every second frame, 16.5 fps
    ("33", 101, Fraction(33, 2)),  # an odd frame count must not end past the body
    ("36", 108, Fraction(18)),
])
def test_a_repaired_final_faster_than_30_keeps_every_second_frame(tmp_path, rate, frames, expected):
    # The Worker counts a repair's expected_output_frames at 30 fps and the Lab
    # refuses more frames than that, or a final that ends past the commanded cut.
    source = tmp_path / f"body-{rate}.mp4"
    _ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=540x960:rate={rate}", "-frames:v", str(frames), "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", "2",
            "-pix_fmt", "yuv420p", str(source))
    facts = _repaired_final(source, tmp_path / "final.mp4")
    assert Fraction(facts["fps_num"], facts["fps_den"]) == expected
    _assert_clean(tmp_path / "final.mp4", expected, f"{rate} fps repaired final")
    body_seconds = Fraction(frames) / Fraction(rate)
    count = len(_frame_gaps(tmp_path / "final.mp4")) + 1
    assert count <= body_seconds * 30, f"{count} frames is over the Worker's 30 fps count"
    assert Fraction(count) / expected <= body_seconds, "the repaired final ends past the body"


def test_a_repaired_final_of_a_shown_twice_body_gets_its_own_frames_back(tmp_path):
    # 0.65x of 30p is 19.5 fps of picture, cut at 39 fps with every frame shown
    # twice. Resampling that straight to 30 held frames for 1 or 2 slots
    # unevenly (the stutter); keeping every second frame gives back the 19.5.
    source = _moving_source(tmp_path, "30")
    cut = _cut(source, tmp_path / "cut.mp4", 0.65, delivery_encode_args(DELIVERY))
    facts = _repaired_final(cut, tmp_path / "final.mp4")
    assert Fraction(facts["fps_num"], facts["fps_den"]) == Fraction(39, 2)
    assert not any(_repeat_flags(tmp_path / "final.mp4")), _repeat_map(tmp_path / "final.mp4")


# -- the cut's transforms ---------------------------------------------------

def test_camera_move_on_a_25p_cut_keeps_its_rate(tmp_path):
    source = _moving_source(tmp_path, "25", seconds=2)
    cut = _cut(source, tmp_path / "cut.mp4", 1.0, delivery_encode_args(DELIVERY))
    apply_transforms(cut, CutTransforms(motion="push_in"), delivery_encode_args(DELIVERY))
    _assert_clean(cut, Fraction(25), "25p push_in")


def _sync_source(root: Path) -> Path:
    """A 30p clip with one white frame at 0.6 s and a 1 kHz beep at the same 0.6 s."""
    path = root / "sync.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30:duration=4",
            "-f", "lavfi", "-i",
            "aevalsrc='if(between(t,0.6,0.6333),0.8*sin(2*PI*1000*t),0)':s=48000:d=4",
            "-vf", "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='between(t,0.59,0.61)'",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", "2",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-shortest", str(path))
    return path


def _flash_time(path: Path) -> float:
    result = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-v", "info", "-i", str(path), "-an",
                             "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG",
                             "-f", "null", "-"], check=True, timeout=120, capture_output=True, text=True)
    rows = re.findall(r"pts_time:([0-9.]+)\s*\n.*?YAVG=([0-9.]+)", result.stderr)
    flashes = [float(time) for time, luma in rows if float(luma) > 200]
    assert flashes, "white frame not found"
    return flashes[0]


def _beep_time(path: Path) -> float:
    result = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-v", "info", "-i", str(path), "-vn",
                             "-af", "silencedetect=n=-30dB:d=0.01", "-f", "null", "-"],
                            check=True, timeout=120, capture_output=True, text=True)
    ends = [float(value) for value in re.findall(r"silence_end: ([0-9.]+)", result.stderr)]
    assert ends, "beep not found"
    return ends[0]


def test_loop_swap_of_a_slowed_cut_keeps_sound_and_picture_together(tmp_path):
    cut = _cut(_sync_source(tmp_path), tmp_path / "cut.mp4", 0.7, delivery_encode_args(DELIVERY))
    before = _flash_time(cut) - _beep_time(cut)
    apply_transforms(cut, CutTransforms(loop_swap=True), delivery_encode_args(DELIVERY))
    flash, beep = _flash_time(cut), _beep_time(cut)
    assert flash > 2.5, "the white frame moved to the second half"
    detail = f"picture {flash:.3f}s vs sound {beep:.3f}s (before the swap {before:+.3f}s)"
    assert abs(flash - beep) < 1 / 21, detail                 # under one 21 fps frame apart
    assert abs((flash - beep) - before) < 1 / 21 / 2, detail  # and the swap itself moved them < half a frame
    _assert_clean(cut, Fraction(21), "loop-swapped 0.7x cut")


# -- the source-regeneration gate -------------------------------------------

def _regenerate(tmp_path: Path, monkeypatch, master: Path, speed: float) -> dict:
    command = {
        "schema": regen.COMMAND_SCHEMA, "submissionSha256": "a" * 64,
        "parentGenerationJobId": "cpl-rate", "parentAuthorityJobId": "cpl-rate",
        "cutSlotId": "f" * 64 + ":0:4000", "pageId": "page-rate", "slotId": "slot:rate",
        "slotPayloadSha256": "b" * 64, "parentSourceSha256": "c" * 64,
        "parentRecipeId": "pov-dirt-bike:master", "parentRecipeVersion": "v",
        "sourceLibraryId": "lib", "sourceLibraryVersion": "sha256:" + "d" * 64,
        "requestedVisualJson": "{}", "requestedVisualSha256": "a" * 64,
        "requestedSourceVisualSha256": "e" * 64,
        "master": {"sha256": "f" * 64, "bytes": master.stat().st_size, "storageKey": "k",
                   "filename": "m.mp4", "mimeType": "video/mp4", "durationMs": 5000, "sourceOffsetMs": 0},
        "cut": {"start_ms": 0, "end_ms": 4000, "duration_ms": 4000},
        "render": {"playbackSpeed": speed, "clipCrop": {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5},
                   "filters": {}, "encodePreset": DELIVERY,
                   "outputWidth": 1080, "outputHeight": 1920, "frame": None},
        "createdAtMs": 0,
    }
    monkeypatch.setattr(regen, "fetch_master",
                        lambda _master, destination, *, page_id: shutil.copy(master, destination) and destination)
    monkeypatch.setattr(regen, "_reload_planned_cut", lambda _command: (object(), object()))
    monkeypatch.setattr("services.control_plane_sources.source_cut_is_planned", lambda *a, **k: True)
    return regen.regenerate(command, tmp_path / "work", job_id="render_rate", attempt_id="0" * 32)


@pytest.mark.parametrize("rate,speed,expected", [("25", 1.0, 25.0), ("30", 0.7, 21.0), ("30", 0.55, 33.0)])
def test_regeneration_gate_accepts_the_cut_at_its_own_rate(tmp_path, monkeypatch, rate, speed, expected):
    master = _moving_source(tmp_path, rate, seconds=5, size="1080x1920")
    envelope = _regenerate(tmp_path, monkeypatch, master, speed)
    assert envelope["schema"] == regen.ENVELOPE_SCHEMA
    artifact = regen._probe_artifact(tmp_path / "work" / regen.ARTIFACT_NAME)
    assert artifact["frame_rate"] == pytest.approx(expected, abs=0.01)
