"""Similarity gate for clip-varying pages, TRIAL mode (log only; Eric 2026-10-22).

Every finished varied cut is fingerprinted: eight evenly spaced frames, each
reduced to a 9x8 grey image and a 64-bit dHash, kept with the hash of the same
frame mirrored. Two cuts are compared frame by frame IN ORDER, so a reversed
cut reads as different footage, while each frame takes the closer of its plain
and mirrored hash, so a mirrored copy still reads as the same footage. The
distance is the mean Hamming distance over the aligned frames, 0..1
(unrelated footage sits near 0.5).

TRIAL: the gate never re-cuts and never blocks. It records the fingerprint and
the nearest distance on the clip manifest and logs one line per cut with the
cut's transforms, so the threshold can be chosen from what really reads as new.
In ENFORCE mode (not built) a cut under the threshold must take a NEW window:
on the same window, a different treatment alone measured 0.20-0.33, below it.

Threshold 0.38, measured 2026-10-22 on a real sourced master (thingsleftunsaid52,
600 s, 8 s windows): the same window at another speed/crop/mirror scored
0.20-0.33; the window moved by 2 s scored 0.44; different windows 0.43-0.49.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

FINGERPRINT_FRAMES = 8
SIMILARITY_THRESHOLD = 0.38
SIMILARITY_WINDOW_DAYS = 30
MODE = "trial"


def _hashes(frame: bytes) -> tuple[int, int]:
    """dHash of one 9x8 grey frame, and of the same frame mirrored."""
    plain = mirrored = 0
    for y in range(8):
        row = frame[y * 9:(y + 1) * 9]
        flipped = row[::-1]
        for x in range(8):
            plain = (plain << 1) | (row[x] < row[x + 1])
            mirrored = (mirrored << 1) | (flipped[x] < flipped[x + 1])
    return plain, mirrored


def fingerprint(path: Path, duration_s: float) -> list[list[str]]:
    """Eight evenly spaced frames as [plain, mirrored] 16-hex-digit dHashes."""
    rate = FINGERPRINT_FRAMES / max(float(duration_s), 0.5)
    raw = subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
        "-vf", f"fps={rate:.6f},scale=9:8:flags=area,format=gray",
        "-frames:v", str(FINGERPRINT_FRAMES), "-f", "rawvideo", "pipe:1",
    ], check=True, capture_output=True, timeout=120).stdout
    return [[f"{value:016x}" for value in _hashes(raw[i:i + 72])] for i in range(0, len(raw) - 71, 72)]


def distance(left, right) -> float:
    """Mean per-frame distance in order (0..1); a mirrored copy counts as the same."""
    pairs = list(zip(left or [], right or []))
    if not pairs:
        return 1.0
    total = 0
    for a, b in pairs:
        plain = int(a[0], 16)
        total += min(bin(plain ^ int(b[0], 16)).count("1"), bin(plain ^ int(b[1], 16)).count("1"))
    return total / (64 * len(pairs))


def recent_fingerprints(jobs: dict, page_id: str, job_id: str, now: datetime) -> list[tuple[str, list]]:
    """(clip id, fingerprint) for the page's cuts of the last SIMILARITY_WINDOW_DAYS."""
    cutoff = (now - timedelta(days=SIMILARITY_WINDOW_DAYS)).isoformat()
    found = []
    for other_id, other in jobs.items():
        if (other_id == job_id or other.get("pageId") != page_id or other.get("status") != "completed"
                or str(other.get("completedAt") or "") < cutoff):
            continue
        for index, clip in enumerate(other.get("clips") or []):
            value = (clip.get("similarity") or {}).get("fingerprint")
            if isinstance(value, list) and value:
                found.append((f"{other_id}/{index}", value))
    return found


def judge(candidate: list, others: list[tuple[str, list]]) -> dict:
    """The manifest record for one cut: nearest earlier cut and the trial verdict."""
    nearest_id, nearest = None, 1.0
    for other_id, other in others:
        value = distance(candidate, other)
        if value < nearest:
            nearest_id, nearest = other_id, value
    return {"mode": MODE, "fingerprint": candidate, "nearestDistance": round(nearest, 4),
            "nearestClip": nearest_id, "threshold": SIMILARITY_THRESHOLD,
            "wouldRecut": nearest < SIMILARITY_THRESHOLD}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
