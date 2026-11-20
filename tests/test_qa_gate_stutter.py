"""The lead's stutter test inside the QA gate, and the enforce-only heal.

Reference: the lead's stutter.py v4 (seeno-claude/scouts-1120/stutter.py, md5
261855824b7c796215a5801f5fb7dfea). On 114 real bodies the gate's verdict
matched the script on every body (control-plane-pad#237 agreement run).
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from services import qa_gate
from tests import qa_gate_clips as clips

pytestmark = pytest.mark.usefixtures("_ffmpeg")


@pytest.fixture
def _ffmpeg():
    clips.require_ffmpeg()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rate_and_frames(path: Path) -> tuple[str, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate,nb_read_frames", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    rate, frames = out.split(",")[:2]
    return rate, int(frames)


def _padded(path: Path, native: int, seconds: float = 6.0) -> Path:
    """A clip filmed at ``native`` fps and padded up to 30 by repeating frames
    (the Lab's -r 30 delivery fault, #209/#239)."""
    native_file = clips.clean(path.with_name(path.stem + f".{native}.mp4"), seconds, fps=native)
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-y", "-v", "error", "-i", str(native_file),
                    "-vf", "fps=30", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(path)], check=True, capture_output=True)
    return path


VIDEO_6S = qa_gate.QaPolicy(expected_frames=180)


# ── the verdict, rule by rule (pure function, the lead's thresholds) ─────────

def _ydif(pattern: list[float], frames: int) -> list[float]:
    return [0.0] + [pattern[i % len(pattern)] for i in range(frames - 1)]


def test_a_repeat_every_sixth_frame_is_periodic():
    # 25-in-30 padding: a repeated frame every 6th frame.
    verdict = qa_gate.stutter_verdict(_ydif([5, 5, 5, 5, 5, 0.0], 180), 30.0)
    assert verdict["verdict"] == "PERIODIC" and verdict["nearDups"] == 29


def test_every_frame_shown_twice_is_a_smooth_doubled_rate():
    verdict = qa_gate.stutter_verdict(_ydif([5, 0.0], 180), 30.0)
    assert verdict["verdict"] == "DOUBLED-OK"


def test_a_repeat_every_third_frame_is_judder():
    # 20-in-30: one fixed gap of 3.
    verdict = qa_gate.stutter_verdict(_ydif([5, 5, 0.0], 180), 30.0)
    assert verdict["verdict"] == "PERIODIC"


def test_long_runs_of_repeats_are_a_still_held_on_purpose():
    pattern = [5.0] * 4 + [0.0] * 8
    assert qa_gate.stutter_verdict(_ydif(pattern, 180), 30.0)["verdict"] == "ok-heldstills"


def test_scattered_repeats_in_a_moving_shot_are_irregular_but_not_in_a_still_shot():
    pattern = [5, 5, 0.0, 5, 5, 5, 5, 0.0, 5, 0.0, 5, 5, 5, 5, 5, 5, 0.0]
    assert qa_gate.stutter_verdict(_ydif(pattern, 180), 30.0)["verdict"] == "IRREGULAR"
    calm = [0.8 if value else 0.0 for value in pattern]
    assert qa_gate.stutter_verdict(_ydif(calm, 180), 30.0)["verdict"] == "ok-lowmotion"


def test_a_clean_clip_and_an_empty_read_are_ok():
    assert qa_gate.stutter_verdict(_ydif([5.0], 180), 30.0)["verdict"] == "ok"
    assert qa_gate.stutter_verdict([], 30.0)["verdict"] == "ok"


# ── on real encodes ─────────────────────────────────────────────────────────

def test_a_native_30_body_has_no_stutter(tmp_path):
    verdict = qa_gate.validate_clip(clips.clean(tmp_path / "clean.mp4"), VIDEO_6S)
    assert verdict.findings == () and verdict.extra["stutter"]["verdict"] == "ok"
    assert verdict.manifest()["stutter"]["verdict"] == "ok"


def test_a_25_padded_to_30_body_warns_and_never_blocks(tmp_path):
    body = _padded(tmp_path / "pad25.mp4", 25)
    verdict = qa_gate.validate_clip(body, VIDEO_6S)
    assert verdict.ok and [item.code for item in verdict.findings] == ["qa_stutter"]
    assert verdict.extra["stutter"]["verdict"] == "PERIODIC" and verdict.score == 60


def test_shadow_records_stutter_and_changes_nothing(tmp_path):
    body = _padded(tmp_path / "pad25.mp4", 25)
    before = _sha(body)
    verdict = qa_gate.run_gate(body, VIDEO_6S, qa_gate.MODE_SHADOW)
    assert verdict.ok and verdict.healed == () and _sha(body) == before


def test_enforce_heals_a_padded_body_back_to_its_native_rate(tmp_path):
    body = _padded(tmp_path / "pad25.mp4", 25)
    verdict = qa_gate.run_gate(body, VIDEO_6S, qa_gate.MODE_ENFORCE)
    assert verdict.ok and verdict.healed == ("stutter",)
    assert verdict.findings == () and verdict.score == 100
    assert verdict.extra["stutterBefore"]["verdict"] == "PERIODIC"
    assert verdict.extra["stutter"]["verdict"] == "ok"
    assert verdict.manifest()["healed"] == ["stutter"] and verdict.manifest()["healedFps"] == "25/1"
    # Same 6 s, 150 real pictures at 25 fps, every repeat gone.
    assert _rate_and_frames(body) == ("25/1", 150)
    assert verdict.path == body


def test_enforce_heals_20_in_30_judder(tmp_path):
    body = _padded(tmp_path / "pad20.mp4", 20)
    verdict = qa_gate.run_gate(body, VIDEO_6S, qa_gate.MODE_ENFORCE)
    assert verdict.healed == ("stutter",) and _rate_and_frames(body) == ("20/1", 120)


def test_a_doubled_rate_body_is_left_alone(tmp_path):
    body = _padded(tmp_path / "dbl15.mp4", 15)
    before = _sha(body)
    verdict = qa_gate.run_gate(body, VIDEO_6S, qa_gate.MODE_ENFORCE)
    assert verdict.extra["stutter"]["verdict"] == "DOUBLED-OK"
    assert verdict.healed == () and _sha(body) == before


def test_a_paid_clip_is_healed_into_the_treated_copy_never_in_place(tmp_path):
    paid = _padded(tmp_path / "provider.mp4", 25)
    before = _sha(paid)
    treated = tmp_path / "treated" / "g00-c00.mp4"
    policy = qa_gate.QaPolicy(width=None, height=None, expected_frames=None)
    verdict = qa_gate.run_gate(paid, policy, qa_gate.MODE_ENFORCE, repaired_destination=treated)
    assert verdict.healed == ("stutter",) and verdict.path == treated and treated.exists()
    assert _sha(paid) == before


def test_a_heal_that_would_not_come_back_clean_keeps_the_original(tmp_path, monkeypatch):
    body = _padded(tmp_path / "pad25.mp4", 25)
    before = _sha(body)

    def broken_heal(source, destination, dups, rate):
        raise RuntimeError("stutter_heal_failed: simulated")

    monkeypatch.setattr(qa_gate, "heal_stutter", broken_heal)
    verdict = qa_gate.run_gate(body, VIDEO_6S, qa_gate.MODE_ENFORCE)
    assert verdict.ok and verdict.healed == () and _sha(body) == before
    assert [item.code for item in verdict.findings] == ["qa_stutter"]
    assert not list(tmp_path.glob("*.heal.mp4"))


def test_a_heal_that_times_out_keeps_the_bodys_own_verdict(tmp_path, monkeypatch):
    # GLM review: a timeout must not fall through to run_gate's fail-open
    # tool pass (which drops every finding); the heal just does not happen.
    body = _padded(tmp_path / "pad25.mp4", 25)
    before = _sha(body)

    def slow_heal(source, destination, dups, rate):
        raise subprocess.TimeoutExpired("ffmpeg", 1)

    monkeypatch.setattr(qa_gate, "heal_stutter", slow_heal)
    verdict = qa_gate.run_gate(body, VIDEO_6S, qa_gate.MODE_ENFORCE)
    assert verdict.extra.get("tool_error") is None
    assert [item.code for item in verdict.findings] == ["qa_stutter"] and _sha(body) == before


def test_native_rate_keeps_the_duration_and_never_snaps_away_from_it():
    assert qa_gate.native_rate(180, 30, 30.0) == 25
    assert qa_gate.native_rate(180, 36, 30.0) == 24
    # 222 frames with 31 repeats: 191 pictures over 7.4 s; 25 fps would end
    # 0.04 s early (more than a frame), so the exact rate is used.
    assert qa_gate.native_rate(222, 31, 30.0) == pytest.approx(191 / 7.4, abs=0.001)
    assert qa_gate.native_rate(180, 0, 30.0) is None
    assert qa_gate.native_rate(180, 150, 30.0) is None   # under 12 fps left


def test_a_dark_truck_scene_is_not_a_black_run(tmp_path):
    # Geoff's truck clips grade down to brightness .5-.7: dim, never black.
    body = tmp_path / "dark.mp4"
    clips._encode(body, "testsrc2=size=1080x1920:rate=30:duration=6,eq=brightness=-0.35:contrast=0.6",
                  frames=180)
    verdict = qa_gate.validate_clip(body, VIDEO_6S)
    assert not [item for item in verdict.findings if item.code == "qa_black_run"]
