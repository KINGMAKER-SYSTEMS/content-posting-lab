"""Side-by-side quality test: the same shot from the same first-frame still, through each video model.

    python tools/blockout/quality_test.py [--only truck|xmas|boat ...] [--variants wan,ltx,h3]

The user judged the blockout clips "super low quality" and "AI-made" (2026-11-23). The blockout
route renders with LTX 2.3 at 8 distilled steps under a depth guide, at 1344x768 or 768x1344
for most formats. This renders three representative shots again from the same still with:

  wan   Wan 2.2 14B image-to-video, full 20 steps with CFG (no speed LoRA), 720p, FILM to 32 fps
  ltx   LTX 2.3 two-stage image-to-video at 1920x1088 (no depth guide)
  h3    MiniMax H3 first/last-frame-to-video through the Lab's own provider code, 20 steps

and puts every result, plus the existing clip, through ec's phone finish (scale to the posting
size, light sharpen, sensor grain, one platform-safe encode). ec's scripts are called unchanged.

Output: /Volumes/OWC-Envoy-Ultra/content-posting-lab/quality-test/<shot>/<variant>[_phone].mp4
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(REPO))

import pipeline  # noqa: E402
import recipe_lab as rl  # noqa: E402
from providers import comfyui  # noqa: E402

EC = Path.home() / "dev/ec/work/1121-blockout2video/comfy"
DRIVE = Path("/Volumes/OWC-Envoy-Ultra/content-posting-lab")
TRUCKS = DRIVE / "blockout-trucks"
OUT = DRIVE / "quality-test"
NO_PEOPLE = " No people in frame, no text, no logos."

SHOTS = {
    "truck": {
        "still": TRUCKS / "mountain-pullout-hd" / "v6-mountain-pullout_hd_still.png",
        "now": TRUCKS / "videos" / "mountain-pullout__v6-hd.mp4",
        "wide": True,
        "prompt": "Handheld phone video walking slowly toward a dark navy-black full-size crew-cab pickup truck "
                  "parked still on a gravel mountain pullout, the truck stays parked and its wheels do not turn, "
                  "tall dark pines behind swaying slightly, thin grey haze drifting across the peaks, dark "
                  "overcast light, real phone footage with natural sway." + NO_PEOPLE,
    },
    "xmas": {
        "still": TRUCKS / "xmas-lane" / "hero" / "y1-xmas-lane_seed71_00001_.png",
        "now": TRUCKS / "videos" / "xmas-lane__y1.mp4",
        "wide": False,
        "prompt": pipeline.SCENES["xmas-lane"]["video"] + NO_PEOPLE,
    },
    "boat": {
        "still": TRUCKS / "harbor-newyork-rev" / "hero" / "c1-harbor-newyork-rev_seed61_00001_.png",
        "opening": TRUCKS / "harbor-newyork-rev" / "hero" / "water_end_city.png",
        "now": TRUCKS / "videos" / "harbor-newyork-rev__c2.mp4",
        "wide": True,
        "prompt": "POV phone video from the bow of a speedboat cruising fast across New York Harbor at dusk toward "
                  "the Lower Manhattan skyline, the white bow rising and falling on small waves, spray, the "
                  "skyline slowly coming closer, glowing tower lights reflecting on the water, semi-shaky "
                  "handheld." + NO_PEOPLE,
        # H3 pins both ends natively, so it renders the Lab's own reveal forwards: water, then skyline
        "h3_prompt": rl.catalog_prompt("boat-lake", "boat", 3)[0],
    },
    # The Lab's own truck reference picture (projects/recipe-lab/_anchors/scenic-v2.jpg) as the first
    # frame: the look the pages post, to see how much of the AI look comes from our stills. It still
    # carries a Ford badge, so this is a comparison only, not a clip to post.
    "anchor": {
        "still": REPO / "projects" / "recipe-lab" / "_anchors" / "scenic-v2.jpg",
        "now": None,
        "wide": False,
        "prompt": "Handheld phone video moving slowly around the front of a black pickup truck parked on sand "
                  "beside an old wooden fence at golden hour, long warm shadows across the sand, the truck "
                  "stays parked and its wheels do not turn, natural hand sway." + NO_PEOPLE,
    },
}


def say(msg: str) -> None:
    pipeline.say(f"quality-test: {msg}")


def run(cmd: list[str]) -> str:
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError(f"{Path(cmd[1]).name} failed: {(out.stdout + out.stderr)[-600:]}")
    return out.stdout


def shot_dir(name: str, frames: int, fps: int) -> Path:
    """ec's run_shot reads length and frame rate from a passes.json in the shot folder."""
    d = OUT / name / f"shot-{fps}fps"
    d.mkdir(parents=True, exist_ok=True)
    (d / "passes.json").write_text(json.dumps({"width": 1280, "height": 720, "fps": fps, "frames": frames}))
    return d


def ec_render(name: str, shot: dict, model: str, size: str, extra: list[str], dest: Path) -> None:
    fps, frames = (16, 81) if model == "wan-i2v" else (24, 121)
    out = run([sys.executable, str(EC / "run_shot.py"), str(shot_dir(name, frames, fps)), "--model", model,
               "--ref", str(shot["still"]), "--prompt", shot["prompt"], "--size", size, "--seed", "7",
               "--name", f"qt-{name}-{model}", *extra])
    made = [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("output ")]
    shutil.copy2(made[-1], dest)


def data_uri(path: Path) -> str:
    kind = "jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "png"
    return f"data:image/{kind};base64," + base64.b64encode(path.read_bytes()).decode()


async def h3_render(name: str, shot: dict, dest: Path) -> None:
    params = {"model_id": comfyui.H3_FL2VA, "variant": "landscape" if shot["wide"] else "vertical",
              "duration": 5, "go_fast": False, "sample_steps": 20, "seed": 7, "entry": {}}
    if shot.get("opening"):
        params["image_data_uri"], params["last_image_data_uri"] = data_uri(shot["opening"]), data_uri(shot["still"])
    else:
        params["image_data_uri"] = data_uri(shot["still"])
    async with httpx.AsyncClient(timeout=120) as client:
        url = await comfyui.run_on_server("http://127.0.0.1:8188", shot.get("h3_prompt") or shot["prompt"],
                                          params, client)
        resp = await client.get(url, timeout=600)
        resp.raise_for_status()
        dest.write_bytes(resp.content)


def finish(src: Path, wide: bool) -> Path:
    dest = src.with_name(src.stem + "_phone.mp4")
    if not dest.exists():
        run([sys.executable, str(EC / "phone_finish.py"), str(src), str(dest),
             "--size", "1920x1080" if wide else "1080x1920"])
    return dest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="*", default=list(SHOTS))
    parser.add_argument("--variants", default="wan,ltx,h3")
    args = parser.parse_args()
    variants = args.variants.split(",")
    for name in args.only:
        shot = SHOTS[name]
        folder = OUT / name
        folder.mkdir(parents=True, exist_ok=True)
        if shot["now"]:
            now = folder / "now.mp4"
            if not now.exists():
                shutil.copy2(shot["now"], now)
            finish(now, shot["wide"])
        for v in variants:
            dest = folder / f"{v}.mp4"
            if dest.exists():
                say(f"{name}/{v} already rendered")
            else:
                say(f"{name}/{v} rendering")
                t0 = time.time()
                try:
                    if v == "wan":
                        ec_render(name, shot, "wan-i2v", "1280x720" if shot["wide"] else "720x1280",
                                  ["--quality", "--interp"], dest)
                    elif v == "ltx":
                        ec_render(name, shot, "ltx-i2v", "1920x1088" if shot["wide"] else "1088x1920", [], dest)
                    elif v == "h3":
                        asyncio.run(h3_render(name, shot, dest))
                except Exception as exc:  # noqa: BLE001 - one failed variant should not stop the rest
                    say(f"{name}/{v} FAILED: {str(exc)[:300]}")
                    continue
                say(f"{name}/{v} done in {time.time() - t0:.0f} s")
            finish(dest, shot["wide"])
    say("finished")


if __name__ == "__main__":
    main()
