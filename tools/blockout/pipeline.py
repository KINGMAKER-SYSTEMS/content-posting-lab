"""Truck clips by the Blender blockout route, one scene end to end.

    python tools/blockout/pipeline.py lakeside [mountain-pullout ...] [--tag v2] [--no-clean]

For each scene:
  1. build the blockout in Blender and export its depth pass (make_shot.sh)
  2. four look stills held to that depth (ec's lookdev.py), keep the one the judge faults least
  3. clean emblems, badges, lettering and plates off it (MiniMax H3-Image; tools/h3_image.py)
  4. depth-guided video on LTX with the cleaned still as frame one (ec's run_shot.py)
  5. score the clip as the Lab would post it: five tall slices, each judged on its own

Scenes, stills, videos and results.jsonl live on the drive under
/Volumes/OWC-Envoy-Ultra/content-posting-lab/blockout-trucks (team storage rule).
The depth, look-still and video scripts belong to ec's 1121-blockout2video lane and are
run as they are; nothing in that folder is changed.
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))

import h3_image  # noqa: E402
import recipe_lab as rl  # noqa: E402

ROOT = Path("/Volumes/OWC-Envoy-Ultra/content-posting-lab/blockout-trucks")
EC = Path.home() / "dev/ec/work/1121-blockout2video/comfy"

TRUCK = ("a real dark navy-black full-size crew-cab pickup truck with glossy factory paint, a plain dark grille, "
         "plain dark alloy wheels and clear empty windows, completely unmarked bodywork")
LOOK = "moody low-exposure phone photograph, muted natural colour, deep blacks, photorealistic"
STILL = "A dark navy-black full-size crew-cab pickup truck parked completely still, its wheels not turning"
FOOTAGE = "moody low-exposure phone footage with deep blacks and muted natural colour"

# still = the look-still prompt (what the scene is); video = what moves and how the camera travels.
SCENES = {
    "lakeside": {
        "still": f"Extreme wide low wheel-height photograph of {TRUCK}, parked on a gravel shoulder beside a still pond, "
                 f"dormant tan grass on a low hillside across the water, dark soft-overcast light, {LOOK}",
        "video": f"{STILL} on a gravel shoulder beside a still pond, dormant tan grass stirring slightly in a light "
                 f"breeze, faint ripples on the water, dark soft-overcast light, {FOOTAGE}, slow steady camera push-in",
        "strength": 0.7},
    "mountain-pullout": {
        "still": f"Extreme wide low wheel-height rear three-quarter photograph of {TRUCK}, seen from behind, parked on a "
                 f"gravel mountain pullout, a dense forest of tall natural pine trees close behind and grey hazy peaks "
                 f"beyond, dark soft-overcast light, {LOOK}",
        "video": f"{STILL} on a gravel mountain pullout, tall dark pines behind swaying very slightly, thin grey haze "
                 f"drifting across the peaks, dark soft-overcast light, {FOOTAGE}, slow steady camera drift",
        # 1.0: with no trees blocked the still has little else to hold, and at 0.8 the truck was
        # drawn about twice its blocked size.
        "strength": 1.0},
    "open-field": {
        "still": f"Extreme wide low wheel-height side profile photograph of {TRUCK}, parked on a dirt farm track across an "
                 f"open field of dry grass, a weathered wooden fence line behind and a distant dark tree line, heavy dark "
                 f"overcast sky at dusk, {LOOK}",
        "video": f"{STILL} on a dirt farm track in an open field, dry grass in the foreground moving gently in the wind, "
                 f"a wooden fence line behind, heavy dark overcast sky at dusk, {FOOTAGE}, slow sideways camera drift",
        "strength": 0.8},
    "desert-road": {
        "still": f"Extreme wide low wheel-height front three-quarter photograph of {TRUCK}, parked on an empty two-lane "
                 f"desert road, low scrub on both sides and flat-topped mesas on the far horizon, blue-hour dusk light, {LOOK}",
        "video": f"{STILL} on an empty two-lane desert road, low scrub trembling in a light breeze, flat-topped mesas on "
                 f"the far horizon, blue-hour dusk light, {FOOTAGE}, slow steady camera push-in",
        "strength": 0.8},
    "hilltop": {
        "still": f"Extreme wide low wheel-height rear three-quarter photograph of {TRUCK}, seen from behind, parked on a "
                 f"grassy rise with rolling hills falling away into haze, dark soft-overcast light before rain, {LOOK}",
        "video": f"{STILL} on a grassy rise, long grass moving in the wind, rolling hills fading into haze, dark "
                 f"soft-overcast light before rain, {FOOTAGE}, slow steady camera drift",
        "strength": 0.8},
    "salt-flat": {
        "still": f"Extreme wide low wheel-height side profile photograph of {TRUCK}, parked on a vast empty flat plain of "
                 f"pale cracked earth, a thin dark mountain range on the far horizon, deep dusk light, {LOOK}",
        "video": f"{STILL} on a vast empty flat plain, a thin veil of dust drifting low across the ground, a dark mountain "
                 f"range on the far horizon, deep dusk light, {FOOTAGE}, slow steady camera push-in",
        "strength": 0.8},
    "snow-field": {
        "still": f"Extreme wide low wheel-height front three-quarter photograph of {TRUCK}, parked in a snow-covered "
                 f"field in deep winter, smooth untouched snow all around, low drifts and pale hills fading into grey "
                 f"haze, flat dark overcast light at dusk, {LOOK}",
        "video": f"{STILL} in a snow-covered field in deep winter, light snow falling slowly, fine powder blowing low "
                 f"across the drifts, pale hills in grey haze, flat dark overcast light at dusk, {FOOTAGE}, slow steady "
                 f"camera push-in",
        "strength": 0.8},
    "beach": {
        "still": f"Extreme wide low wheel-height photograph of {TRUCK}, parked on a wide empty beach of flat wet sand at "
                 f"low tide, grey sea to the horizon and a dark headland far off, heavy overcast sky at dusk, {LOOK}",
        "video": f"{STILL} on a wide empty beach of flat wet sand at low tide, small waves rolling in far behind, thin "
                 f"sea mist drifting, heavy overcast sky at dusk, {FOOTAGE}, slow sideways camera drift",
        "strength": 0.8},
    "barn": {
        "still": f"Extreme wide low wheel-height side profile photograph of {TRUCK}, parked in a gravel farmyard, a large "
                 f"weathered plain wooden barn and a metal grain silo behind, a wooden fence along the yard, dark "
                 f"soft-overcast light at dusk, {LOOK}",
        "video": f"{STILL} in a gravel farmyard, a weathered wooden barn and a grain silo behind, dry weeds along the "
                 f"fence moving in a light wind, dark soft-overcast light at dusk, {FOOTAGE}, slow steady camera push-in",
        "strength": 0.8},
}


# Model files each stage loads. ComfyUI maps them into memory, and from the external drive a
# mapped file that is not already in the Mac's file cache pages in at tens of MB/s: one look-still
# load took 33 minutes on 2026-11-22 while a plain read of the same drive ran at about 6 GB/s.
# Reading the files through once first puts them in the cache, and the load is then quick.
MODEL_ROOTS = [Path.home() / "ComfyUI/models", Path("/Volumes/OWC-Envoy-Ultra/smaths/models/comfyui-lookdev")]
STAGE_FILES = {
    "look": ["z_image_turbo_bf16.safetensors", "qwen_3_4b.safetensors", "ae.safetensors",
             "Z-Image-Turbo-Fun-Controlnet-Union.safetensors"],
    "clean": ["minimax_h3_image_pruned_bf16.safetensors", "minimax_h3_image_vae_fp16.safetensors",
              "qwen3vl_32b_minimax_h3_bf16.safetensors"],
    "video": ["ltx-2.3-22b-dev.safetensors", "gemma_3_12B_it.safetensors",
              "ltx-2.3-22b-ic-lora-union-control-ref0.5.safetensors",
              "ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors",
              "ltx-2.3-spatial-upscaler-x2-1.1.safetensors"],
}


COMFY_LOG = Path.home() / "ComfyUI/logs/comfyui.log"


def stage_files(stage: str) -> list[str]:
    found = []
    for name in STAGE_FILES[stage]:
        for root in MODEL_ROOTS:
            hits = glob.glob(str(root / "*" / name))
            if hits:
                found.append(hits[0])
                break
    return found


class warm:
    """Keep a stage's model files in the file cache while ComfyUI loads them.

        with warm("video"):
            ...queue the job and wait...

    One read-through is not enough: the cache was seen to drop most of a 75 GB pre-read within
    half an hour, before the load that needed it. So a thread re-reads the files (a pass over
    cached files costs seconds) until ComfyUI's log shows a sampler progress bar, which means
    the weights are in, or six minutes have gone, or the stage ends.
    """

    def __init__(self, stage: str):
        self.stage, self.files, self.stop = stage, stage_files(stage), threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def sampling(self, offset: int) -> bool:
        try:
            with COMFY_LOG.open("rb") as log:
                log.seek(offset)
                return b"it/s]" in (tail := log.read()) or b"s/it]" in tail
        except OSError:
            return False

    def run(self) -> None:
        offset = COMFY_LOG.stat().st_size if COMFY_LOG.exists() else 0
        started, passes = time.monotonic(), 0
        while self.files and not self.stop.is_set() and time.monotonic() - started < 360:
            with open("/dev/null", "wb") as sink:
                subprocess.run(["/bin/cat", *self.files], stdout=sink, check=False)
            passes += 1
            if self.sampling(offset):
                break
            self.stop.wait(5)
        say(f"  kept {len(self.files)}/{len(STAGE_FILES[self.stage])} {self.stage} model files cached: "
            f"{passes} passes in {time.monotonic() - started:.0f} s")

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=120)


def say(text: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {text}", flush=True)


def sh(cmd: list[str]) -> str:
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} {Path(cmd[1]).name if len(cmd) > 1 else ''} failed: "
                           f"{(done.stdout + done.stderr)[-400:]}")
    return done.stdout


def faults(path: Path, spec: dict, slices: bool = True) -> list[str] | None:
    """Faults of a still, on the whole frame and inside each of the five posted slices.

    A small door badge can pass the whole-frame look and still fail the slice it sits in
    (it cost the hilltop clip two slices), so the still that goes on is checked on the
    stricter slice view. None when the judge cannot answer. slices=False asks about the
    whole frame only: one judge call instead of six, for ranking candidates (the judge is a
    shared model and took 40-60 s a call when it was busy on 2026-11-22).
    """
    try:
        found = list(rl.still_faults(path, spec))
        if not slices:
            return found
        for piece in rl.crop_facts(path, 1, rl.vision_config(spec), spec.get("crops") or "both"):
            found += [f for f in piece["faults"] if f not in (spec.get("advisory") or []) and f != "truck_in_view"]
        return found
    except Exception as exc:  # noqa: BLE001 - the pipeline goes on without a judge
        say(f"  judge unavailable: {str(exc)[:80]}")
        return None


async def clean(src: Path, dest: Path, seed: int) -> None:
    width, height = h3_image._size(src)
    width, height = width // 32 * 32, height // 32 * 32          # the edit works on a x32 grid
    async with httpx.AsyncClient() as client:
        graph = h3_image.edit_graph(await h3_image.upload(client, src), h3_image.TRUCK_CLEAN, width, height, seed)
        await rl.prepare_server(client, graph)
        await h3_image.run_image(client, graph, dest)


def run_scene(scene: str, tag: str, do_clean: bool, spec: dict, seed: int) -> dict:
    cfg = SCENES[scene]
    shot = ROOT / scene
    row = {"scene": scene, "tag": tag, "started": f"{datetime.now():%F %T}"}
    started = time.monotonic()

    say(f"{scene}: blockout and depth pass")
    sh([str(HERE / "make_shot.sh"), scene])

    say(f"{scene}: look stills")
    name = f"{tag}-{scene}"
    pattern = str(shot / "hero" / f"{name}_seed{seed}_*.png")
    if glob.glob(pattern):
        say(f"{scene}: reusing the look stills already made for {name} seed {seed}")   # a re-run after a lost render
    else:
        with warm("look"):
            sh(["python3", str(EC / "lookdev.py"), str(shot), "--prompt", cfg["still"], "--strength",
                str(cfg["strength"]), "--seed", str(seed), "--name", name])
    candidates = sorted(Path(p) for p in glob.glob(pattern))
    if not candidates:
        raise RuntimeError("look-still step wrote no pictures")
    # rank the candidates on the whole frame, then look inside the slices of the best one only
    judged = sorted(((c, faults(c, spec, slices=False)) for c in candidates),
                    key=lambda item: len(item[1]) if item[1] is not None else 99)
    # Then look inside the slices, best-ranked first, and stop at the first clean one: five judge
    # calls for another candidate cost less than a cleaning pass (hilltop v4: the first-ranked
    # still had a badge in two slices while the fourth had none).
    pick, pick_faults = judged[0][0], None
    for candidate, whole in judged:
        if whole is None:
            break
        found = faults(candidate, spec)
        if found is not None and (pick_faults is None or len(found) < len(pick_faults)):
            pick, pick_faults = candidate, found
        if found == []:
            break
    row["look_still"], row["look_faults"] = pick.name, pick_faults
    say(f"{scene}: picked {pick.name} ("
        f"{', '.join(sorted(set(pick_faults))) if pick_faults else 'no faults' if pick_faults == [] else 'unjudged'})")

    frame = pick
    # Only clean a still the judge faulted. On one that was already clean the edit added small
    # round ornaments to the bumpers, so an unneeded pass costs four minutes and can do harm.
    marked = pick_faults is None or any(f in ("no_emblem", "no_lettering") for f in pick_faults)
    if do_clean and marked:
        say(f"{scene}: cleaning emblems and lettering off the still")
        cleaned = shot / "hero" / f"{name}_clean.png"
        with warm("clean"):
            asyncio.run(clean(pick, cleaned, seed))
        clean_faults = faults(cleaned, spec)
        row["clean_still"], row["clean_faults"] = cleaned.name, clean_faults
        # keep the edit unless the judge finds it worse than what went in
        if pick_faults is None or clean_faults is None or len(clean_faults) <= len(pick_faults):
            frame = cleaned
        else:
            say(f"{scene}: cleaning made it worse ({sorted(set(clean_faults))}), keeping the original still")

    say(f"{scene}: depth-guided video on LTX")
    with warm("video"):
        out = sh(["python3", str(EC / "run_shot.py"), str(shot), "--model", "ltx", "--ref", str(frame),
                  "--name", name, "--seed", str(seed + 100), "--prompt", cfg["video"]])
    match = re.search(r"^output (.+\.mp4)\s*$", out, re.M)
    if not match:
        raise RuntimeError(f"video step reported no file: {out[-300:]}")
    video = ROOT / "videos" / f"{scene}__{tag}.mp4"
    video.parent.mkdir(exist_ok=True)
    shutil.copy(match.group(1), video)
    row["video"] = video.name
    try:
        facts, checks = rl.score(video, spec)
        row["checks"] = checks
        row["usable_slices"] = facts.get("usable_crops")
        row["slices"] = [c["faults"] for c in facts.get("crops", [])]
        row["brightness"], row["motion"] = facts["brightness"], facts["motion"]
    except Exception as exc:  # noqa: BLE001 - keep the clip unjudged
        row["judge_error"] = str(exc)[:200]
    row["seconds"] = round(time.monotonic() - started)
    say(f"{scene}: done in {row['seconds']} s, usable slices {row.get('usable_slices', 'unjudged')}/5, "
        f"failed checks {[k for k, v in row.get('checks', {}).items() if not v]}")
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenes", nargs="+", choices=sorted(SCENES))
    parser.add_argument("--tag", default="v2")
    parser.add_argument("--no-clean", action="store_true")
    parser.add_argument("--seed", type=int, default=40)
    args = parser.parse_args()
    spec = json.loads((HERE.parent / "recipe_lab_specs" / "truck.json").read_text())
    for scene in args.scenes:
        try:
            row = run_scene(scene, args.tag, not args.no_clean, spec, args.seed)
        except Exception as exc:  # noqa: BLE001 - one scene failing must not stop the rest
            row = {"scene": scene, "tag": args.tag, "error": (str(exc) or repr(exc))[:400]}
            say(f"{scene}: FAILED: {row['error']}")
        with (ROOT / "results.jsonl").open("a") as f:
            f.write(json.dumps(row) + "\n")
    say("blockout pipeline finished")


if __name__ == "__main__":
    main()
