"""Cut the best blockout-route truck clips into what a page posts: slowed, then five 9:16 slices.

    python tools/blockout/post_slices.py [--factor 2] [--only scene ...]

This is the Lab's own truck recipe applied to these clips: one wide render, slowed in post,
cut into five tall slices (same offsets as providers.base.multi_crop_vertical, through
recipe_lab.crop_windows). A slice the judge faulted in results.jsonl is still cut, but it is
named ".fault" and listed as such in post/manifest.json, so nothing faulted is posted by accident.

Output: /Volumes/OWC-Envoy-Ultra/content-posting-lab/blockout-trucks/post/<scene>/ (team storage rule).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))

import recipe_lab as rl  # noqa: E402

ROOT = Path("/Volumes/OWC-Envoy-Ultra/content-posting-lab/blockout-trucks")

# The clip to keep for each scene (2026-11-22/23), chosen from the judge's slice scores and from
# looking at first and last frames.
BEST = {
    "lakeside": "lakeside.mp4", "salt-flat": "salt-flat__v3.mp4", "mountain-pullout": "mountain-pullout__v6.mp4",
    "desert-road": "desert-road__v4.mp4", "hilltop": "hilltop__v4.mp4", "open-field": "open-field__v4.mp4",
    "snow-field": "snow-field__v4.mp4", "beach": "beach__v4.mp4", "barn": "barn__v5.mp4",
    "forest-road": "forest-road__s3.mp4", "canyon-rim": "canyon-rim__s2.mp4", "hay-field": "hay-field__s1.mp4",
    "cabin-snow": "cabin-snow__s1.mp4", "lake-dock": "lake-dock__s1.mp4", "night-lot": "night-lot__s2.mp4",
    "tree-farm": "tree-farm__s3.mp4", "covered-bridge": "covered-bridge__s3.mp4", "frozen-lake": "frozen-lake__s3.mp4",
    "cabin-lights": "cabin-lights__s3.mp4", "barn-snow": "barn-snow__s3.mp4", "pass-snow": "pass-snow__s3.mp4",
}

# Scenes re-rendered at 1920x1088 (tools/blockout/hd_shot.py): slices come out about 612x1088.
BEST_HD = {
    "cabin-lights": "cabin-lights__s3-hd-r1.mp4", "tree-farm": "tree-farm__s3-hd.mp4",
    "covered-bridge": "covered-bridge__s3-hd-r1.mp4", "frozen-lake": "frozen-lake__s3-hd.mp4",
    "barn-snow": "barn-snow__s3-hd.mp4", "pass-snow": "pass-snow__s3-hd.mp4", "cabin-snow": "cabin-snow__s1-hd.mp4",
    "canyon-rim": "canyon-rim__s2-hd.mp4", "night-lot": "night-lot__s2-hd.mp4", "lake-dock": "lake-dock__s1-hd.mp4",
    "hay-field": "hay-field__s1-hd.mp4", "forest-road": "forest-road__s3-hd-r1.mp4", "desert-road": "desert-road__v4-hd.mp4",
    "mountain-pullout": "mountain-pullout__v6-hd.mp4", "beach": "beach__v4-hd.mp4", "barn": "barn__v5-hd.mp4",
    "hilltop": "hilltop__v4-hd.mp4", "open-field": "open-field__v4-hd.mp4", "salt-flat": "salt-flat__v3-hd-r1.mp4",
    "snow-field": "snow-field__v4-hd.mp4", "lakeside": "lakeside__v1-hd.mp4",
}


def judged_slices() -> dict[str, list[list[str]]]:
    """Latest per-slice faults for each video name in results.jsonl."""
    found: dict[str, list[list[str]]] = {}
    for line in (ROOT / "results.jsonl").read_text().splitlines():
        row = json.loads(line)
        if row.get("video") and row.get("slices") is not None:
            found[row["video"]] = row["slices"]
    return found


def size(path: Path) -> tuple[int, int]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout.strip()
    width, height = out.split(",")[:2]
    return int(width), int(height)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--factor", type=float, default=2.0, help="slow-motion factor (2 = a 4 s clip becomes 8 s)")
    parser.add_argument("--only", nargs="*", default=[])
    parser.add_argument("--hd", action="store_true", help="cut the 1920x1088 re-renders into post-hd/ instead")
    args = parser.parse_args()
    faults = judged_slices()
    manifest = []
    post = ROOT / ("post-hd" if args.hd else "post")
    for scene, name in (BEST_HD if args.hd else BEST).items():
        if args.only and scene not in args.only:
            continue
        src = ROOT / "videos" / name
        out = post / scene
        out.mkdir(parents=True, exist_ok=True)
        # a scene whose best clip changed is cut again: its old slow copy and slices are replaced
        marker = out / "source.txt"
        if marker.exists() and marker.read_text().strip() != name:
            for stale in [out / "slow.mp4", *out.glob("slice*.mp4")]:
                stale.unlink(missing_ok=True)
        marker.write_text(name + "\n")
        slow = out / "slow.mp4"
        if not slow.exists():
            rl.slow_motion(src, slow, args.factor)
        width, height = size(slow)
        per_slice = faults.get(name)
        entry = {"scene": scene, "source": name, "factor": args.factor, "slices": []}
        for i, (x, crop_w) in enumerate(rl.crop_windows(width, height, "both")):
            bad = per_slice[i] if per_slice and i < len(per_slice) else None
            # a slice with only "no truck here" is landscape, not a fault in the picture
            real = [f for f in (bad or []) if f != "truck_in_view"]
            piece = out / (f"slice{i}.fault.mp4" if real else f"slice{i}.mp4")
            if not piece.exists():
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(slow), "-vf", f"crop={crop_w}:ih:{x}:0",
                                "-an", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", str(piece)], check=True)
            entry["slices"].append({"file": piece.name, "x": x, "width": crop_w, "height": height,
                                    "judge": "unjudged" if bad is None else (bad or "ok")})
        manifest.append(entry)
        usable = sum(1 for s in entry["slices"] if ".fault" not in s["file"])
        print(f"{scene}: {usable}/{len(entry['slices'])} slices to post from {name}", flush=True)
    if args.only and (post / "manifest.json").exists():
        # a partial run updates its scenes in the manifest and keeps the rest
        done = {e["scene"] for e in manifest}
        manifest = [e for e in json.loads((post / "manifest.json").read_text()) if e["scene"] not in done] + manifest
    (post / "manifest.json").write_text(json.dumps(manifest, indent=1))
    total = sum(1 for e in manifest for s in e["slices"] if ".fault" not in s["file"])
    print(f"{total} postable slices from {len(manifest)} scenes; manifest at {post / 'manifest.json'}")


if __name__ == "__main__":
    main()
