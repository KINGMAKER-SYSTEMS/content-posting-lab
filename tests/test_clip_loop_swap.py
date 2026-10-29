"""Loop-swap transform acceptance tests (services/clip_transforms.py).

Eric's recipe (2026-10-28): "slow it and then cut it to 9 secs or less n then
split it in half n move the second half to the front so it has a loop" + reverse.

`loop_swap` re-orders the finished cut: the second half (split at the frame
nearest the midpoint) plays first, then the first half. This makes the clip a
perfect loop — the last frame and the first frame are adjacent frames of the
original, so playback wraps without a jump. Reversing a perfect loop is still
a perfect loop, which is why reverse + loop_swap stays seamless.

loop_swap is opt-in per page (services/clip_transforms.LOOP_SWAP_PAGES, default
empty): it is OFF for every page unless the page is on its own allow-list,
which is distinct from services/clip_variation.CLIP_VARIATION_PAGES. With it
off, the draw and the mirror/motion/reverse render order are byte-identical to
the pre-loop-swap build.
"""
import hashlib
import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

import services.clip_transforms as ct
from services.clip_transforms import (
    CutTransforms,
    LOOP_SWAP_SHARE,
    apply_transforms,
    cut_transforms,
    loop_swap_enabled,
    transforms_from_manifest,
)
from services.clip_variation import (
    FAST_SPEEDS,
    OUTPUT_MIN_MS,
    SLOW_SPEEDS,
    allowed_speeds,
    cut_variant,
    varies_clips,
)

BTL = "acct:between-the-lines66"
CROP = {"zoom": 1.63, "focusX": 0.5, "focusY": 0.5}
MASTER = "a" * 64

# Lossless video so a decoded frame hash is byte-identical before and after the
# re-order (no compression drift): the loop-swap proof compares pixels.
LOSSLESS = [
    "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0",
    "-pix_fmt", "yuv420p", "-r", "30", "-movflags", "+faststart",
    "-c:a", "aac", "-b:a", "64k",
]


def _no_rate(encode_args):
    """The same encode args without a forced output frame rate."""
    result = []
    skip = False
    for argument in encode_args:
        if skip:
            skip = False
            continue
        if argument == "-r":
            skip = True
            continue
        result.append(argument)
    return result


def _counter_clip(path: Path, seconds: float = 6.0, rate=30) -> None:
    """A clip whose frame number is burned in via geq luma: frame N is solid
    grey ``(N*16) % 256``. Lossless H.264 keeps the decoded luma byte-identical
    before and after re-ordering, so a frame hash identifies the index exactly."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required")
    subprocess.run([
        "ffmpeg", "-nostdin", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=black:s=320x240:r={rate}:d={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:d={seconds}",
        "-vf", "geq=lum='mod(N,256)':cb='128':cr='128'",
        "-shortest", *LOSSLESS, str(path),
    ], check=True, capture_output=True)


def _frame_hash(path: Path, index: int) -> str:
    """SHA-256 of decoded luma for one frame, identified by 0-based index."""
    raw = subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
        "-vf", f"trim=start_frame={index}:end_frame={index + 1}",
        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ], check=True, capture_output=True).stdout
    return hashlib.sha256(raw).hexdigest()


def _duration(path: Path) -> float:
    raw = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nokey=1:noprint_wrappers=1", str(path),
    ], check=True, capture_output=True, text=True).stdout
    return float(raw.strip())


# ── L1: second half first, split at the frame nearest the midpoint ─────────
def test_loop_swap_puts_the_second_half_first(tmp_path):
    source = tmp_path / "counter.mp4"
    _counter_clip(source, seconds=5.5)
    n = 165  # 5.5 s * 30 fps (odd, so ceil/floor midpoint differ)
    k = (n + 1) // 2  # ceil(n/2) == 83
    target = tmp_path / "out.mp4"
    shutil.copy(source, target)
    apply_transforms(target, CutTransforms(loop_swap=True), LOSSLESS)
    # Output frame 0 is input frame ceil(n/2); the last output frame is the
    # one just before it, so the two are adjacent and the clip loops.
    assert _frame_hash(target, 0) == _frame_hash(source, k)
    assert _frame_hash(target, n - 1) == _frame_hash(source, k - 1)
    assert _duration(target) == pytest.approx(5.5, abs=0.1)
    assert not list(tmp_path.glob(".*transforms")), "work files are removed"


# ── L2: a loop-swapped cut is always 5.2..9.0 s ────────────────────────────
def test_loop_swap_speed_choice_keeps_the_cut_at_or_under_nine_seconds(monkeypatch):
    # When a loop_swap is drawn, the cut's speed is chosen so the delivered
    # duration stays <= 9.0 s (and >= 5.2 s). Slow speeds stay in the pool —
    # they are only excluded for windows where they would exceed 9 s.
    monkeypatch.setattr(ct, "LOOP_SWAP_PAGES", frozenset({BTL}))
    assert all(s in allowed_speeds(SLOW_SPEEDS, 7_000, 9_000) for s in SLOW_SPEEDS)
    windows = range(5_000, 9_001, 500)
    drawn = 0
    previous = None
    for index, start in enumerate(range(0, 200 * 8_000, 8_000)):
        duration_ms = windows[index % len(windows)]
        slot = f"{MASTER}:{start}:{duration_ms}"
        transforms = cut_transforms(BTL, slot, previous)
        previous = transforms
        if not transforms.loop_swap:
            continue
        drawn += 1
        speed, _ = cut_variant(BTL, slot, 1.0, CROP, duration_ms=duration_ms,
                               max_output_ms=9_000)
        delivered = duration_ms / speed
        assert OUTPUT_MIN_MS <= delivered <= 9_000, (duration_ms, speed, delivered)
    assert drawn > 40, "loop_swap is actually drawn over 200 cuts"


# ── L3: fixed order, and reverse + loop_swap stays seamless ────────────────
def test_loop_swap_then_reverse_keeps_a_seamless_loop(tmp_path):
    # loop_swap first (second half -> first half), then the whole clip is
    # reversed. Reversing a perfect loop is still a perfect loop: the seam is
    # the same two adjacent source frames, in reverse order. An ODD frame
    # count is essential: for even n, swap and reverse commute frame-for-frame
    # (n-1-k == k-1), so only an odd n makes a clean swap/reverse order mutant
    # fail the endpoints.
    source = tmp_path / "counter.mp4"
    _counter_clip(source, seconds=5.5)
    n = 165
    k = (n + 1) // 2  # 83
    target = tmp_path / "out.mp4"
    shutil.copy(source, target)
    apply_transforms(target, CutTransforms(loop_swap=True, reverse=True), LOSSLESS)
    # reversed(swapped) frame 0 == swapped frame n-1 == source frame k-1
    # reversed(swapped) frame n-1 == swapped frame 0 == source frame k
    assert _frame_hash(target, 0) == _frame_hash(source, k - 1)
    assert _frame_hash(target, n - 1) == _frame_hash(source, k)
    # The seam (last frame k -> first frame k-1) is one adjacent frame step.
    assert abs(k - (k - 1)) == 1
    assert _duration(target) == pytest.approx(5.5, abs=0.1)


# ── L4: grade bands and the receipt are untouched by loop_swap/reverse ─────
def test_loop_swap_and_reverse_never_regrade_or_rewrite_the_receipt(tmp_path):
    # Really run loop_swap on a clip, then prove the receipt (built from the
    # grade, never from the transforms) is unchanged and carries no loop_swap.
    from services.clip_variation import cut_grade
    from services.source_treatment import source_treatment_receipt
    source = tmp_path / "counter.mp4"
    _counter_clip(source, seconds=2.0)
    target = tmp_path / "out.mp4"
    shutil.copy(source, target)
    apply_transforms(target, CutTransforms(loop_swap=True), LOSSLESS)
    # The swap really ran: output frame 0 is no longer source frame 0.
    assert _frame_hash(target, 0) != _frame_hash(source, 0)
    recipe = {"filters": {"brightness": 0.5, "contrast": 1.05}, "clipSpeed": 1.0}
    slot = f"{MASTER}:1000:6000"
    filters = cut_grade(BTL, slot, recipe["filters"])
    job = {"jobId": "generation-1", "recipeSpecHash": "sha256:" + "a" * 64}
    base = source_treatment_receipt(job, recipe, "b" * 64, clip_speed=0.9,
                                    clip_crop=None, filters=filters)
    again = source_treatment_receipt(job, recipe, "b" * 64, clip_speed=0.9,
                                     clip_crop=None, filters=filters)
    assert base == again
    assert "loop_swap" not in json.dumps(base, sort_keys=True)
    assert "reverse" not in json.dumps(base, sort_keys=True)
    assert filters["brightness"] <= 0.5, "brightness never above the page look"


# ── L5 / A1: gated by its own allow-list, OFF by default ───────────────────
def test_loop_swap_is_off_unless_the_page_is_allow_listed(monkeypatch):
    # Default: LOOP_SWAP_PAGES is empty, so even a CLIP_VARIATION page (BTL)
    # never draws loop_swap. Gating on CLIP_VARIATION_PAGES instead would make
    # loop_swap_enabled(BTL) true and fail this test (the A1 mutant).
    assert ct.LOOP_SWAP_PAGES == frozenset()
    assert not loop_swap_enabled(BTL)
    assert not loop_swap_enabled("acct:codyjames6-7")
    previous = None
    for start in range(0, 400 * 2_000, 2_000):
        drawn = cut_transforms(BTL, f"{MASTER}:{start}:6000", previous)
        assert not drawn.loop_swap
        previous = drawn
    # Opting a page in turns loop_swap on for that page only.
    monkeypatch.setattr(ct, "LOOP_SWAP_PAGES", frozenset({BTL}))
    assert loop_swap_enabled(BTL)
    assert not loop_swap_enabled("acct:codyjames6-7")
    previous = None
    drawn_count = 0
    for start in range(0, 400 * 2_000, 2_000):
        drawn = cut_transforms(BTL, f"{MASTER}:{start}:6000", previous)
        drawn_count += drawn.loop_swap
        previous = drawn
    assert drawn_count > 0, "loop_swap is drawn once the page is allow-listed"


def test_all_off_transforms_are_a_strict_no_op(tmp_path):
    # apply_transforms with the all-off combination is a strict no-op: the
    # file bytes are untouched and no work dir is created. The caller's gate
    # (varies_clips / transforms_enabled / LOOP_SWAP_PAGES / TRANSFORMS_ALL)
    # decides whether a transform is drawn; this test pins only that an
    # all-off draw never touches the delivered bytes.
    assert not varies_clips("acct:codyjames6-7")
    assert not loop_swap_enabled("acct:codyjames6-7")
    source = tmp_path / "plain.mp4"
    _counter_clip(source, seconds=1.0)
    target = tmp_path / "out.mp4"
    shutil.copy(source, target)
    before = target.read_bytes()
    apply_transforms(target, CutTransforms(), LOSSLESS)
    assert target.read_bytes() == before
    assert not list(tmp_path.glob(".*transforms")), "no work dir is created for a no-op"


# ── L6: draw share and determinism ─────────────────────────────────────────
def test_loop_swap_share_is_a_third_and_deterministic_per_cut(monkeypatch):
    monkeypatch.setattr(ct, "LOOP_SWAP_PAGES", frozenset({BTL}))
    assert LOOP_SWAP_SHARE == 1 / 3
    previous = None
    count = 0
    n = 600
    for start in range(0, n * 2_000, 2_000):
        slot = f"{MASTER}:{start}:6000"
        drawn = cut_transforms(BTL, slot, previous)
        assert drawn == cut_transforms(BTL, slot, previous), "same seed -> same transforms"
        count += drawn.loop_swap
        previous = drawn
    assert 0.25 < count / n < 0.42


def test_manifest_roundtrip_includes_loop_swap():
    transforms = CutTransforms(loop_swap=True, reverse=True)
    assert transforms.manifest() == {
        "mirror": False, "motion": None, "reverse": True, "loop_swap": True,
    }
    assert transforms_from_manifest(transforms.manifest()) == transforms
    # An older manifest without loop_swap still parses as loop_swap off.
    assert transforms_from_manifest({"mirror": True}) == CutTransforms(mirror=True)


# ── L7: bounded memory (streamed, never a whole-clip decode into RAM) ──────
def test_loop_swap_streams_frames_instead_of_decoding_the_whole_clip():
    import inspect

    source = inspect.getsource(ct._loop_swap)
    # trim/concat select each half by frame index and stream it out; reverse's
    # whole-clip frame buffer (a `reverse` filter) is what we avoid here.
    assert "trim=start_frame=" in source and "concat=n=2:v=1:a=0" in source
    assert "[0:v]reverse" not in source and ",reverse" not in source
    # The one-op subprocess timeout is the same bounded gate every transform uses.
    assert ct.FFMPEG_TIMEOUT_S == 600


# ── A1 (off-path): non-loop-swapped cuts keep main's combined order ─────────
def _asymmetric_counter_clip(path: Path, seconds: float = 6.0, rate=30) -> None:
    """A counter clip whose halves differ spatially (left counts up, right
    counts down), so a horizontal pan or a mirror visibly moves content: an
    order change between motion, mirror and reverse is detectable in pixels."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required")
    subprocess.run([
        "ffmpeg", "-nostdin", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=black:s=320x240:r={rate}:d={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:d={seconds}",
        "-vf", "geq=lum='if(lt(X,W/2),mod(N,256),255-mod(N,256))':cb='128':cr='128'",
        "-shortest", *LOSSLESS, str(path),
    ], check=True, capture_output=True)


def test_non_loop_swapped_cut_keeps_mains_mirror_motion_then_reverse_order(tmp_path):
    # With loop_swap off (the default), a cut combining reverse + mirror +
    # camera move must re-encode exactly as main did: mirror and the move are
    # folded into ONE graph (motion then mirror), then reverse. This is a
    # rendered contract: the combo equals the transforms applied stepwise in
    # main's order, and any of the b88f073 order changes (reverse first, or
    # mirror/motion split into separate encodes) renders different bytes.
    source = tmp_path / "asymmetric.mp4"
    _asymmetric_counter_clip(source, seconds=6.0)
    combo = tmp_path / "combo.mp4"
    shutil.copy(source, combo)
    apply_transforms(combo, CutTransforms(reverse=True, mirror=True, motion="pan_right"),
                     LOSSLESS)
    # main's order: mirror+motion in one call, then reverse.
    stepwise = tmp_path / "stepwise.mp4"
    shutil.copy(source, stepwise)
    apply_transforms(stepwise, CutTransforms(mirror=True, motion="pan_right"), LOSSLESS)
    apply_transforms(stepwise, CutTransforms(reverse=True), LOSSLESS)
    for index in (0, 90, 179):
        assert _frame_hash(combo, index) == _frame_hash(stepwise, index), index


# ── MED-2 (A/V sync): audio swaps with the picture, and the seam is exact ───
def _two_tone_clip(path: Path, seconds: float = 6.0, rate=30) -> None:
    """A counter clip whose audio marks its halves: 440 Hz for the first half,
    880 Hz for the second. After a loop_swap the audio must swap with the
    picture, so the tone at any moment identifies which half is playing."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required")
    half = seconds / 2
    subprocess.run([
        "ffmpeg", "-nostdin", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=black:s=320x240:r={rate}:d={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:d={half}",
        "-f", "lavfi", "-i", f"sine=frequency=880:sample_rate=48000:d={half}",
        "-filter_complex",
        "[0:v]geq=lum='mod(N,256)':cb='128':cr='128'[v];[1][2]concat=n=2:v=0:a=1[a]",
        "-map", "[v]", "-map", "[a]", "-shortest", *LOSSLESS, str(path),
    ], check=True, capture_output=True)


def _tone_hz(path: Path, start_s: float, seconds: float = 0.5) -> float:
    """The dominant frequency of one audio window, by zero-crossing rate."""
    raw = subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
        "-ss", f"{start_s}", "-t", f"{seconds}", "-vn",
        "-f", "s16le", "-ac", "1", "-ar", "48000", "pipe:1",
    ], check=True, capture_output=True).stdout
    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    crossings = sum(1 for a, b in zip(samples, samples[1:]) if (a < 0) != (b < 0))
    return crossings / 2 / (len(samples) / 48_000)


def test_loop_swap_keeps_audio_in_sync_with_the_swapped_picture(tmp_path):
    # The picture's halves swap at 3.0 s (frame 90 of 180); the audio must swap
    # at the same boundary: the second half's 880 Hz plays first and the first
    # half's 440 Hz plays last. Audio left in the original order (mutant T1), or
    # split 100 ms early (mutant T2), plays the wrong tone against the picture.
    source = tmp_path / "two-tone.mp4"
    _two_tone_clip(source)
    target = tmp_path / "out.mp4"
    shutil.copy(source, target)
    apply_transforms(target, CutTransforms(loop_swap=True), LOSSLESS)
    assert _duration(target) == pytest.approx(6.0, abs=0.1)
    assert _frame_hash(target, 0) == _frame_hash(source, 90), "picture swapped at 3.0 s"
    assert _tone_hz(target, 0.2) == pytest.approx(880, abs=60)
    assert _tone_hz(target, 5.3) == pytest.approx(440, abs=40)
    # Seam-adjacent: the swap boundary is at 3.0 s. A 100 ms early audio split
    # (mutant T2) moves the 880->440 transition to ~3.1 s, so a short window
    # just after the true seam reads 440 here and 880 on the mutant.
    assert _tone_hz(target, 3.06, seconds=0.04) == pytest.approx(440, abs=80)
    # The source itself is the control: 440 first, 880 last.
    assert _tone_hz(source, 0.2) == pytest.approx(440, abs=40)
    assert _tone_hz(source, 5.3) == pytest.approx(880, abs=60)


# ── A4 (LOW-2): the swap re-encode never pushes a capped cut over 9.0 s ────
def test_loop_swap_keeps_a_capped_cut_at_or_under_nine_seconds_at_common_rates(tmp_path):
    # Encoder frame rounding at the 9.0 s boundary differs by rate; a cut made
    # just under 9.0 s (the probe's 8974 ms worst case) must not be pushed over
    # 9.0 s by the swap re-encode at 24 / 30 / 29.97 / 60 fps.
    for rate in (24, 30, 60, "30000/1001"):
        source = tmp_path / f"c-{str(rate).replace('/', 'p')}.mp4"
        _counter_clip(source, seconds=8.97, rate=rate)
        target = tmp_path / f"out-{str(rate).replace('/', 'p')}.mp4"
        shutil.copy(source, target)
        apply_transforms(target, CutTransforms(loop_swap=True), _no_rate(LOSSLESS))
        assert _duration(target) <= 9.0, (rate, _duration(target))
