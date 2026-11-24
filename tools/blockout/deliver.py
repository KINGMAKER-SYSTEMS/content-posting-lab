"""Turn one finished clip, from any lane, into the files a Lab page posts, and file it in the library.

    python tools/blockout/deliver.py CLIP --format truck-scenic|pov-seasonal|boat-lake --scene NAME
        [--lane ec] [--take T] [--factor 2] [--night]

ec's blockout lane owns quality on the shared ComfyUI (split agreed 2026-11-23); this is the other
half: whatever clip wins there goes through the same last steps every time.

  wide clips (wider than tall): slowed by --factor with motion interpolation, then cut into the
      Lab's five 9:16 slices (recipe_lab.crop_windows = providers.base.multi_crop_vertical)
  portrait clips: used whole
  then every piece goes through ec's phone finish at 1080x1920 (scale, light sharpen, grain, one
  platform-safe encode; ~/dev/ec/work/1121-blockout2video/comfy/phone_finish.py, called unchanged)

The source clip is scored first with the Lab's checks (tools/blockout/score_lane.py); the result is
recorded but nothing is dropped automatically, since the judge can be wrong both ways.

Output: library/<format>/drafts/<scene>__<take>/piece_N.mp4 (or delivered/ with --approved), plus a
row in library/delivered.jsonl that tools/blockout/save_library.py lists. tools/blockout/send_to_lab.py
sends only approved rows with no failed check.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import recipe_lab as rl  # noqa: E402
import score_lane  # noqa: E402

LIBRARY = Path("/Volumes/OWC-Envoy-Ultra/content-posting-lab/library")
DELIVERED = LIBRARY / "delivered.jsonl"
EC_FINISH = Path.home() / "dev/ec/work/1121-blockout2video/comfy/phone_finish.py"


def size(path: Path) -> tuple[int, int]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout.strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


def fps(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout.strip()
    num, den = (int(v) for v in out.split("/"))
    return num / den


def finish(src: Path, dest: Path, night: bool) -> None:
    cmd = [sys.executable, str(EC_FINISH), str(src), str(dest), "--size", "1080x1920"]
    if night:
        cmd.append("--night")
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError(f"phone_finish failed: {(out.stdout + out.stderr)[-400:]}")


def deliver(clip: Path, fmt: str, scene: str, lane: str, take: str, factor: float, night: bool,
            approved: bool = False) -> dict:
    # Nothing is approved by default: ec's lane asked (2026-11-23) that its clips stay unapproved
    # drafts until Eric approves them, even when every check passes. Drafts never go to the Lab.
    folder = LIBRARY / fmt / ("delivered" if approved else "drafts") / f"{scene}__{take}"
    work = folder / "_work"
    work.mkdir(parents=True, exist_ok=True)
    shutil.copy2(clip, folder / f"source{clip.suffix}")
    row = score_lane.score_one(clip)
    pieces: list[Path] = []
    width, height = size(clip)
    if width > height:                                   # a wide render: the Lab slices it
        slow = work / "slow.mp4"
        rl.slow_motion(clip, slow, factor)
        width, height = size(slow)
        for i, (x, crop_w) in enumerate(rl.crop_windows(width, height, "both")):
            cut = work / f"cut{i}.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(slow), "-vf", f"crop={crop_w}:ih:{x}:0", "-an",
                            "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", str(cut)], check=True)
            pieces.append(cut)
    elif fps(clip) < 23:
        # ec's phone_finish refuses clips under 23 fps (platform minimum); a 16 fps Wan clip is
        # brought to 24 fps here with motion interpolation, same length
        smooth = work / "24fps.mp4"
        rl.slow_motion(clip, smooth, 1.0)
        pieces.append(smooth)
    else:
        pieces.append(clip)
    out = []
    for i, piece in enumerate(pieces):
        dest = folder / f"piece_{i}.mp4"
        finish(piece, dest, night)
        out.append(str(dest.relative_to(LIBRARY)))
    shutil.rmtree(work, ignore_errors=True)
    entry = {"format": fmt, "scene": scene, "take": take, "lane": lane, "source": str(clip), "approved": approved,
             "pieces": out, "failed_checks": row.get("failed_checks"), "slice_faults": row.get("slice_faults"),
             "motion_by_quarter": row.get("motion_by_quarter"), "delivered_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with DELIVERED.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("clip", type=Path)
    parser.add_argument("--format", required=True, choices=["truck-scenic", "pov-seasonal", "boat-lake"])
    parser.add_argument("--scene", required=True)
    parser.add_argument("--lane", default="ec")
    parser.add_argument("--take", default=time.strftime("%m%d-%H%M"))
    parser.add_argument("--factor", type=float, default=2.0, help="slow-motion factor for wide formats")
    parser.add_argument("--night", action="store_true", help="phone finish with night grain")
    parser.add_argument("--approved", action="store_true",
                        help="the clip is approved for posting (by Eric for ec's lane); otherwise it is filed as a draft")
    args = parser.parse_args()
    entry = deliver(args.clip, args.format, args.scene, args.lane, args.take, args.factor, args.night, args.approved)
    print(json.dumps(entry, indent=1))


if __name__ == "__main__":
    main()
