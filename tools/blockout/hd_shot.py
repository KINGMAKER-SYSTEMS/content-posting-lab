"""One blockout scene at 1920x1088 instead of 1344x768, from a look still already chosen.

    python tools/blockout/hd_shot.py cabin-lights:s3:81 [tree-farm:s3:81 ...]

The Lab's paid truck renders are 1080p, so its five tall slices are about 608x1080; the
1344x768 route gives 432x768. This re-exports the depth pass at 1920x1088 into <scene>-hd,
redraws the picked still at full working size (about 4 megapixels, same seed as the cleaning
pass, so the same picture), and renders the depth-guided LTX video at that size. It logs how
long each step took. First run (cabin-lights, 2026-11-23): depth 26 s, still 253 s, video 361 s,
both with a model load; the 768p video took 121 s.

Needs the scene's .blend and <tag>-<scene>_state.json from the normal pipeline.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))

import h3_image  # noqa: E402
import pipeline  # noqa: E402
import recipe_lab as rl  # noqa: E402

ROOT = pipeline.ROOT
EC = pipeline.EC


async def big_still(src: Path, dest: Path, seed: int) -> None:
    width, height = h3_image._size(src)
    width, height = width // 32 * 32, height // 32 * 32
    async with httpx.AsyncClient() as client:
        graph = h3_image.edit_graph(await h3_image.upload(client, src), h3_image.TRUCK_CLEAN, width, height, seed,
                                    full_size=True)
        await rl.prepare_server(client, graph)
        await h3_image.run_image(client, graph, dest)


def one_still(scene: str, tag: str, seed: int, size: tuple[int, int], retry: int = 0) -> dict:
    """Depth pass at the large size and the picked still redrawn at full size. Skips what exists.

    retry N redraws the still with another seed and keeps it beside the first (…_hd_still-rN.png):
    for a scene whose first redraw kept or grew a mark (salt flat's door, forest road's grille).
    """
    name = f"{tag}-{scene}"
    suffix = f"-r{retry}" if retry else ""
    state = json.loads((ROOT / scene / "hero" / f"{name}_state.json").read_text())
    shot = ROOT / f"{scene}-hd"
    timings = {}
    passes = shot / "passes.json"
    if not (passes.exists() and json.loads(passes.read_text()).get("width") == size[0]):
        t = time.monotonic()
        pipeline.say(f"{scene} hd: depth pass at {size[0]}x{size[1]}")
        pipeline.sh(["blender", "-b", str(ROOT / f"{scene}.blend"), "-P", str(EC.parent / "blender/export_passes.py"),
                     "--", "--out", str(shot), "--preset", "ltx", "--width", str(size[0]), "--height", str(size[1]),
                     "--clay", "--near", "5", "--far", "300"])
        timings["depth"] = round(time.monotonic() - t)
    still = shot / f"{name}_hd_still{suffix}.png"
    if not still.exists():
        t = time.monotonic()
        pipeline.say(f"{scene} hd: redrawing the picked still at full size")
        with pipeline.warm("clean"):
            asyncio.run(big_still(Path(state["pick"]), still, seed + 1000 * retry))
        timings["still"] = round(time.monotonic() - t)
        pipeline.say(f"{scene} hd: still is {h3_image._size(still)}")
    return timings


def one_video(scene: str, tag: str, seed: int, timings: dict, retry: int = 0, video_seed: int = 0) -> None:
    name = f"{tag}-{scene}"
    suffix = f"-r{retry}" if retry else ""
    still_suffix = suffix
    if video_seed:
        # same still, another video seed: for a clip whose truck crept against the background on
        # its own (snowy barn, found by tools/blockout/subject_drift.py and checked by eye)
        suffix += f"-v{video_seed}"
    shot = ROOT / f"{scene}-hd"
    still = shot / f"{name}_hd_still{still_suffix}.png"
    t = time.monotonic()
    pipeline.say(f"{scene} hd: depth-guided video on LTX")
    with pipeline.warm("video"):
        out = pipeline.sh(["python3", str(EC / "run_shot.py"), str(shot), "--model", "ltx", "--ref", str(still),
                           "--name", f"{name}-hd{suffix}", "--seed", str(video_seed or seed + 100),
                           "--prompt", pipeline.SCENES[scene]["video"]])
    timings["video"] = round(time.monotonic() - t)
    match = re.search(r"^output (.+\.mp4)\s*$", out, re.M)
    if not match:
        raise RuntimeError(f"video step reported no file: {out[-300:]}")
    rendered = Path(match.group(1))
    spec = json.loads((HERE.parent / "recipe_lab_specs" / "truck.json").read_text())
    row = {"scene": scene, "tag": f"{tag}-hd{suffix}", "video": f"{scene}__{tag}-hd{suffix}.mp4", "seconds": timings}
    try:
        facts, checks = rl.score(rendered, spec)
        row.update(checks=checks, usable_slices=facts.get("usable_crops"),
                   slices=[c["faults"] for c in facts.get("crops", [])],
                   brightness=facts["brightness"], motion=facts["motion"])
    except Exception as exc:  # noqa: BLE001
        row["judge_error"] = str(exc)[:200]
    shutil.copy(rendered, ROOT / "videos" / row["video"])
    with (ROOT / "results.jsonl").open("a") as f:
        f.write(json.dumps(row) + "\n")
    pipeline.say(f"{scene} hd: done, seconds {timings}, usable slices {row.get('usable_slices', 'unjudged')}/5, "
                 f"clip {rendered}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("shots", nargs="+", help="scene:tag:seed[:retry], e.g. cabin-lights:s3:81 or salt-flat:v3:52:1")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1088)
    parser.add_argument("--video-seed", type=int, default=0, help="render the video with this seed (same still)")
    args = parser.parse_args()
    shots = []
    for item in args.shots:
        parts = item.split(":")
        shots.append((parts[0], parts[1], int(parts[2]), int(parts[3]) if len(parts) > 3 else 0))
    timings: dict[str, dict] = {}
    # every still first, then every video: one change of model for the whole batch
    for scene, tag, seed, retry in shots:
        try:
            timings[scene] = one_still(scene, tag, seed, (args.width, args.height), retry)
        except Exception as exc:  # noqa: BLE001
            pipeline.say(f"{scene} hd: FAILED at the still: {(str(exc) or repr(exc))[:300]}")
    for scene, tag, seed, retry in shots:
        if scene not in timings:
            continue
        try:
            one_video(scene, tag, seed, timings[scene], retry, args.video_seed)
        except Exception as exc:  # noqa: BLE001
            pipeline.say(f"{scene} hd: FAILED at the video: {(str(exc) or repr(exc))[:300]}")
    pipeline.say("hd batch finished")


if __name__ == "__main__":
    main()
