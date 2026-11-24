"""Score another lane's finished clips against the Lab's own format checks, as they land.

    python tools/blockout/score_lane.py [--watch] [--prefix bake-] [--prefix x-]

ec's blockout lane (~/dev/ec/work/1121-blockout2video) owns the quality work on the shared
ComfyUI; its clips land in ~/ComfyUI/output/ec-blockout/. This reads them (never writes there),
picks the Lab format from the clip's shape and name (portrait Christmas/village/walk -> pov-seasonal,
portrait or wide truck -> truck-scenic, boat/harbor -> boat-lake), and runs the same checks the
blockout pipeline uses: the format's automatic checks, the vision judge, and for wide truck clips
the five 9:16 slices a page posts. It also measures motion (mean frame-to-frame change by quarter),
since near-still clips were the other main complaint.

Results append to /Volumes/OWC-Envoy-Ultra/content-posting-lab/lane-scores.jsonl, one row per clip,
and a clip already scored is skipped. --watch keeps going, checking every two minutes. --deliver
sends each clip that passes every check through tools/blockout/deliver.py into the library.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import pipeline  # noqa: E402
import recipe_lab as rl  # noqa: E402
import deliver  # noqa: E402

LAB_FORMAT = {"truck": "truck-scenic", "truck-xmas": "truck-scenic", "christmas": "pov-seasonal", "boat": "boat-lake"}
# first-frame pictures worth checking before they are animated (ec's reference and edited stills)
STILL_PREFIXES = ("truck-ref-v3", "gas-night-edit", "xmas-cabin-edit", "village-edit", "xmas-truck", "gas-v2")

SOURCE = Path.home() / "ComfyUI/output/ec-blockout"
SCORES = Path("/Volumes/OWC-Envoy-Ultra/content-posting-lab/lane-scores.jsonl")
SPECS = HERE.parent / "recipe_lab_specs"


def shape(path: Path) -> tuple[int, int, int]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,nb_frames", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip().split(",")
    return int(out[0]), int(out[1]), int(out[2]) if out[2].isdigit() else 0


def motion(path: Path) -> list[float]:
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf",
                          "scale=160:-2,format=gray,tblend=all_mode=difference,signalstats,"
                          "metadata=mode=print:key=lavfi.signalstats.YAVG:file=-", "-f", "null", "-"],
                         capture_output=True, text=True).stdout
    vals = [float(line.split("=")[1]) for line in out.splitlines() if "YAVG=" in line]
    n = len(vals)
    return [round(sum(q) / max(len(q), 1), 2) for q in (vals[i * n // 4:(i + 1) * n // 4] for i in range(4))]


def format_for(name: str, width: int, height: int) -> str:
    low = name.lower()
    if any(k in low for k in ("boat", "harbor")):
        return "boat"
    if any(k in low for k in ("ski", "mtb", "bike")):
        return "none"                              # no Lab AI format for these; motion only
    if "cabin" in low or ("xmas" in low and "truck" in low):
        return "truck-xmas"                        # truck shot dressed for Christmas
    if any(k in low for k in ("xmas", "village", "christmas", "walk")):
        return "christmas"
    return "truck"


def score_one(path: Path) -> dict:
    width, height, frames = shape(path)
    fmt = format_for(path.name, width, height)
    row = {"clip": path.name, "format": fmt, "size": f"{width}x{height}", "frames": frames,
           "motion_by_quarter": motion(path), "scored_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if fmt == "none":
        row["failed_checks"] = None
        return row
    spec = json.loads((SPECS / f"{'truck' if fmt == 'truck-xmas' else fmt}.json").read_text())
    try:
        facts, checks = rl.score(path, spec)
        row["failed_checks"] = [k for k, ok in checks.items() if not ok]
    except Exception as exc:  # noqa: BLE001 - keep the row, mark it
        row["failed_checks"] = None
        row["error"] = str(exc)[:200]
    if fmt == "truck" and width > height:
        bad = pipeline.faults(path, spec, slices=True)
        row["slice_faults"] = bad
    if fmt == "truck-xmas" and row["failed_checks"] is not None:
        # a truck shot dressed for Christmas (ec's cabin scene): it has to pass the truck checks
        # (badges, lettering, plates) and also read as Christmas
        try:
            _, xchecks = rl.score(path, json.loads((SPECS / "christmas.json").read_text()))
            row["failed_checks"] += [f"christmas:{k}" for k, ok in xchecks.items() if not ok and k != "no_people"]
        except Exception as exc:  # noqa: BLE001
            row["error"] = str(exc)[:200]
    return row


def score_still(path: Path) -> dict:
    """A first-frame picture: the format's whole-frame checks only (one judge call)."""
    fmt = format_for(path.name, 0, 0)
    row = {"clip": path.name, "format": fmt, "kind": "still", "scored_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if fmt == "none":
        row["failed_checks"] = None
        return row
    spec = json.loads((SPECS / f"{'truck' if fmt == 'truck-xmas' else fmt}.json").read_text())
    found = pipeline.faults(path, spec, slices=False)
    if fmt == "truck-xmas" and found is not None:
        xfound = pipeline.faults(path, json.loads((SPECS / "christmas.json").read_text()), slices=False)
        found = None if xfound is None else found + [f"christmas:{f}" for f in xfound if f != "no_people"]
    row["failed_checks"] = None if found is None else sorted(set(found))
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--prefix", action="append", default=[])
    parser.add_argument("--deliver", action="store_true", help="deliver each clip that passes every check")
    args = parser.parse_args()
    # ec's clip names: bake-*/x-* comparisons, and per-scene drafts such as gas-night-h3i2v-draft-*
    prefixes = tuple(args.prefix or ["bake-", "x-", "gas-night-h3", "xmas-cabin-h3", "village-h3", "truck-road-h3",
                                     "xmas-truck", "gas-v2"])   # xmas-truck-*, xmas-truck2-*, gas-v2-*
    while True:
        done = set()
        if SCORES.exists():
            done = {json.loads(line)["clip"] for line in SCORES.read_text().splitlines() if line.strip()}
        new = sorted((p for p in SOURCE.glob("*.mp4") if p.name.startswith(prefixes) and p.name not in done
                      and time.time() - p.stat().st_mtime > 60), key=lambda p: p.stat().st_mtime)
        stills = sorted((p for p in SOURCE.glob("*.png") if p.name.startswith(STILL_PREFIXES) and p.name not in done
                         and time.time() - p.stat().st_mtime > 60), key=lambda p: p.stat().st_mtime)
        for path in stills:
            row = score_still(path)
            with SCORES.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
            pipeline.say(f"lane-score: still {path.name} [{row['format']}] failed {row['failed_checks']}")
        for path in new:
            row = score_one(path)
            with SCORES.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
            pipeline.say(f"lane-score: {path.name} [{row['format']} {row['size']}] motion {row['motion_by_quarter']} "
                         f"failed {row['failed_checks']}" + (f" slices {row.get('slice_faults')}" if 'slice_faults' in row else ""))
            if args.deliver and row["failed_checks"] == [] and not row.get("slice_faults") \
                    and row["format"] in LAB_FORMAT and not path.name.startswith(("bake-ski", "x-ski")):
                # passed every Lab check: carry it straight into the library in posting form
                entry = deliver.deliver(path, LAB_FORMAT[row["format"]], path.stem.split("_0")[0], "ec",
                                        time.strftime("%m%d-%H%M"), 2.0, False)
                pipeline.say(f"lane-score: delivered {path.name} -> {entry['pieces'][0].rsplit('/', 1)[0]}")
        if not args.watch:
            break
        time.sleep(120)


if __name__ == "__main__":
    main()
