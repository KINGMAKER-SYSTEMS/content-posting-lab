"""The TRIAL similarity gate (services/clip_similarity.py)."""
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services import clip_similarity
from services.clip_transforms import CutTransforms, apply_transforms
from tests.test_clip_transforms import ENCODE


def _moving_clip(path: Path, seconds: float = 6.0) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required")
    # Stripes sliding sideways and an off-centre blob: every frame differs
    # from the next, and the picture is not symmetric left to right.
    subprocess.run([
        "ffmpeg", "-nostdin", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=black:s=1080x1920:r=30:d={seconds},format=gray,"
        r"geq=lum='clip(128+90*sin((X+T*160)/70)+lt(X\,300)*lt(Y\,600)*60\,0\,255)'",
        *ENCODE, "-an", str(path),
    ], check=True, capture_output=True)


def test_distance_is_order_sensitive_and_mirror_blind(tmp_path):
    source = tmp_path / "cut.mp4"
    _moving_clip(source)
    base = clip_similarity.fingerprint(source, 6.0)
    assert len(base) == clip_similarity.FINGERPRINT_FRAMES
    assert clip_similarity.distance(base, base) == 0.0
    mirrored, reversed_clip = tmp_path / "mirror.mp4", tmp_path / "reverse.mp4"
    shutil.copy(source, mirrored)
    shutil.copy(source, reversed_clip)
    apply_transforms(mirrored, CutTransforms(mirror=True), ENCODE)
    apply_transforms(reversed_clip, CutTransforms(reverse=True), ENCODE)
    assert clip_similarity.distance(base, clip_similarity.fingerprint(mirrored, 6.0)) < 0.05, "a mirror is the same footage"
    # In order: a reversed cut reads as new, though its frames read back.
    backwards = clip_similarity.fingerprint(reversed_clip, 6.0)
    read_back = clip_similarity.distance(list(reversed(base)), backwards)
    assert read_back < 0.15
    assert clip_similarity.distance(base, backwards) > read_back + 0.15


def test_judge_names_the_nearest_cut_and_only_records_a_trial_verdict():
    a = [["ffffffffffffffff", "0000000000000000"]] * 8
    b = [["fffffffffffffff0", "0000000000000000"]] * 8
    c = [["0f0f0f0f0f0f0f0f", "f0f0f0f0f0f0f0f0"]] * 8
    record = clip_similarity.judge(a, [("job-1/0", c), ("job-2/1", b)])
    assert record["nearestClip"] == "job-2/1"
    assert record["nearestDistance"] == pytest.approx(4 / 64, abs=1e-4)
    assert record["wouldRecut"] is True and record["mode"] == "trial"
    assert clip_similarity.judge(a, [])["wouldRecut"] is False


def test_recent_fingerprints_cover_the_page_for_thirty_days_only():
    fp = [["0" * 16, "0" * 16]]
    jobs = {
        "fresh": {"pageId": "p", "status": "completed", "completedAt": "2026-10-20T00:00:00+00:00",
                  "clips": [{"similarity": {"fingerprint": fp}}, {}]},
        "old": {"pageId": "p", "status": "completed", "completedAt": "2026-09-01T00:00:00+00:00",
                "clips": [{"similarity": {"fingerprint": fp}}]},
        "other": {"pageId": "q", "status": "completed", "completedAt": "2026-10-21T00:00:00+00:00",
                  "clips": [{"similarity": {"fingerprint": fp}}]},
    }
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    assert clip_similarity.recent_fingerprints(jobs, "p", "running", now) == [("fresh/0", fp)]
