"""Does the parked vehicle move against its surroundings? A check the vision judge cannot make.

    python tools/blockout/subject_drift.py CLIP [CLIP ...] [--json]

Two failures the user and ec's lane both saw on 2026-11-23: a camera move the video model turned
into the truck moving. The tree-farm clip (camera tracked sideways while aimed at the truck: the
truck held its place while the trees streamed past, reading as the truck sliding down the road)
and ec's cabin clip (a pedestal-down that came out as the truck driving toward the cabin with tyre
spray). Mean frame difference flags the jump but cannot say whether the camera or the subject moved.

Method, per pair of frames 4 apart, on a 480-px-wide grey copy:
  1. dense optical flow (Farneback)
  2. the camera's motion = a homography fitted with RANSAC to flow sampled over the whole frame
     (the background is most of the picture, so it dominates the fit)
  3. residual = flow minus what the camera motion predicts, at every pixel
  4. the subject region = the largest connected blob of residual above a threshold
Reported: for each quarter of the clip, the share of the frame that moves on its own and its mean
residual speed (px per frame at 480 px wide), plus a verdict: "drift" when a sizeable region
(over 4% of the frame) keeps moving on its own (over 0.6 px/frame) for at least a third of the
clip, or when any quarter has over 10% of the frame moving on its own faster than 1 px/frame.
Water, snow, smoke and trees in wind also move on their own; read the verdict with the clip.
LIMITS (found 2026-11-23, unresolved): a single homography cannot model depth parallax, so a
camera push-in can read as "drift" (the re-rendered tree farm push-in is still flagged), and
counting only sideways shift then missed ec's cabin truck driving toward the camera. Treat a
"drift" verdict as "look at this clip", never as proof; frames by eye decide. It did correctly
flag the sideways-tracked tree farm, open field and hay field. Its snowy barn flag was push-in
parallax, not creep (the truck grows evenly about its centre while the barn holds), which I first
misread by eye as well: check that the subject grows symmetrically before calling it a slide.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

STEP = 4            # compare frame i with frame i+STEP
WIDTH = 480
MIN_AREA = 0.04     # share of the frame
MIN_SPEED = 0.6     # residual px per frame at 480 px wide
BURST_AREA = 0.10   # any quarter with this much of the frame moving on its own ...
BURST_SPEED = 1.0   # ... at this speed


def frames(path: Path) -> list[np.ndarray]:
    cap = cv2.VideoCapture(str(path))
    out = []
    while True:
        ok, img = cap.read()
        if not ok:
            break
        h, w = img.shape[:2]
        small = cv2.resize(img, (WIDTH, int(h * WIDTH / w)), interpolation=cv2.INTER_AREA)
        out.append(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
    cap.release()
    return out


def own_motion(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """(share of frame moving on its own, speed of that region's shift as a whole) between frames STEP apart."""
    flow = cv2.calcOpticalFlowFarneback(a, b, None, 0.5, 3, 21, 3, 5, 1.2, 0)
    h, w = a.shape
    ys, xs = np.mgrid[0:h:8, 0:w:8]
    src = np.stack([xs.ravel(), ys.ravel()], 1).astype(np.float32)
    dst = src + flow[ys.ravel(), xs.ravel()]
    hom, _ = cv2.findHomography(src, dst, cv2.RANSAC, 1.5)
    if hom is None:
        return 0.0, 0.0
    gy, gx = np.mgrid[0:h, 0:w]
    grid = np.stack([gx, gy], -1).reshape(-1, 1, 2).astype(np.float32)
    predicted = cv2.perspectiveTransform(grid, hom).reshape(h, w, 2) - np.stack([gx, gy], -1)
    residual = np.linalg.norm(flow - predicted, axis=2) / STEP
    mask = (residual > MIN_SPEED).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if count <= 1:
        return 0.0, 0.0
    biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    region = labels == biggest
    # Only a shift of the region as a whole counts. A push-in makes a near truck grow faster than
    # far trees (depth parallax a single homography cannot model): its residual points outward in
    # all directions and averages out. A slide points one way.
    # Sideways only: the near ground under a push-in also streams outward and down as a block
    # (more parallax), while a parked truck that slides or drives goes sideways.
    res_vec = (flow - predicted)[region] / STEP
    shift = float(abs(res_vec[:, 0].mean()))
    return float(region.mean()), shift


def check(path: Path) -> dict:
    fr = frames(path)
    pairs = [own_motion(fr[i], fr[i + STEP]) for i in range(0, len(fr) - STEP, STEP)]
    n = len(pairs)
    quarters = [pairs[i * n // 4:(i + 1) * n // 4] for i in range(4)]
    by_quarter = [{"area": round(float(np.mean([p[0] for p in q])), 3) if q else 0.0,
                   "speed": round(float(np.mean([p[1] for p in q])), 2) if q else 0.0} for q in quarters]
    moving = sum(1 for area, speed in pairs if area > MIN_AREA and speed > MIN_SPEED)
    # a short hard burst also counts: ec's cabin clip held still, then the truck drove for about a
    # quarter of the clip (23% of the frame moving on its own at 1.7 px/frame in that quarter)
    burst = any(q["area"] > BURST_AREA and q["speed"] > BURST_SPEED for q in by_quarter)
    return {"clip": path.name, "pairs": n, "own_motion_by_quarter": by_quarter,
            "moving_share": round(moving / max(n, 1), 2),
            "verdict": "drift" if moving >= n / 3 or burst else "steady"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("clips", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    for clip in args.clips:
        row = check(clip)
        if args.json:
            print(json.dumps(row))
        else:
            q = " ".join(f"{x['area']:.2f}@{x['speed']:.1f}" for x in row["own_motion_by_quarter"])
            print(f"{row['verdict']:6s} moving {row['moving_share']:.2f}  quarters(area@speed) {q}  {row['clip']}")


if __name__ == "__main__":
    sys.exit(main())
