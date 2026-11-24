"""First pass of every AI format in the Lab through the local pipeline.

The truck format got a full recipe (tools/recipe_lab.py). This runs the other
generated formats through the same steps, as far as each one needs them, so
there is one page showing where every format stands on local models:

  still   one frame from the H3 video model, from the Lab's own prompt
  clean   MiniMax H3-Image removes lettering, logos and plates (where the format forbids them)
  animate LTX 2.3 turns the picture into a 3 s clip (several times faster than H3)
  text    LTX 2.3 from the prompt alone, for comparison, on text-to-video formats

Passes run model by model (all stills, then all cleaning, then all clips) so
the big models are swapped twice, not once per clip.

  python tools/format_coverage.py --run-dir projects/recipe-lab/formats-v1 [--scenes 2] [--only boat,coffee]
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import html
import json
import sys
from datetime import datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import h3_image  # noqa: E402
import recipe_lab as rl  # noqa: E402

SIZES = {"landscape": (1344, 768), "vertical": (768, 1344)}
SPECS = Path(__file__).resolve().parent / "recipe_lab_specs"
ANCHORS = rl.LAB_DIR / "_anchors"            # the Lab's reference photos, copied from the content agent's repo

TEXT_CLEAN = (
    "In <Picture 1>, remove every piece of readable lettering: words on signs, shop fronts, banners, vehicles and "
    "boats, brand names, logos, numbers and licence plates, leaving plain surfaces in the same colours and light. "
    "Keep everything else exactly the same: the scene, the camera angle, the light and every object in it.")


# POV dirt bike is "sourced video" in the Lab: real helmet-cam footage recut, no prompts.
# These are ours, written to the format contract: first-person dirt-bike riding only.
DIRT_BIKE_PROMPTS = [
    "First-person helmet-camera video riding a dirt bike fast along a narrow forest single-track at golden hour. "
    "The bike's handlebars, front fender and the rider's gloved hands on the grips fill the bottom of the frame and "
    "shake with the terrain. Pine trunks rush past on both sides, the trail bends ahead, loose dirt and small rocks "
    "blur under the front wheel, dust hangs in shafts of low sun. Raw action-camera footage, wide lens, natural "
    "motion blur, vertical 9:16.",
    "First-person helmet-camera video riding a dirt bike across open desert dunes in late afternoon. Handlebars, "
    "number-plate-free front fender and gloved hands at the bottom of the frame. The bike climbs a sand ridge, the "
    "horizon tilts as it crests, sand sprays from the front tyre, long shadows stretch across rippled dunes under a "
    "clear sky. Raw action-camera footage, wide lens, natural motion blur, vertical 9:16.",
    "First-person helmet-camera video riding a dirt bike down a muddy farm track after rain at dusk. Handlebars and "
    "gloved hands at the bottom of the frame, mud flecks on the front fender. Puddles splash as the front wheel "
    "cuts through them, wet grass and a wooden fence line pass on the right, dark clouds glow orange at the "
    "horizon. Raw action-camera footage, wide lens, natural motion blur, vertical 9:16.",
    "First-person helmet-camera video riding a dirt bike along a rocky mountain ridge trail in cool morning light. "
    "Handlebars and gloved hands at the bottom of the frame. The trail drops away on the left to a wide valley with "
    "mist, the bike weaves between boulders, the front suspension compresses over rocks, small stones scatter. Raw "
    "action-camera footage, wide lens, natural motion blur, vertical 9:16.",
]


# A walking point-of-view prompt makes the still model draw a walker seen from
# behind. The shot is meant to be through the walker's own eyes, so the picture
# is emptied of people before it is animated.
POV_CLEAN = (
    "In <Picture 1>, remove every person so the path ahead is completely empty, as if seen through the eyes of "
    "someone walking along it, and fill the space they occupied with the ground, buildings and lights behind them. "
    "Also remove every piece of readable lettering on signs and shop fronts, leaving plain surfaces. Keep everything "
    "else exactly the same: the place, the decorations, the lights, the camera angle and the time of day.")


def _question(scene: str, subject: str) -> str:
    return (
        f"You are inspecting one frame of {scene}. Reply with one line of JSON and nothing else, using exactly these "
        'keys: {"readable_text_or_logos": true or false, "where": "short phrase, or empty", "people_visible": true or '
        'false, "overlay_graphics": true or false, "subject_visible": true or false}. readable_text_or_logos is true '
        "if any legible words, brand names, logos or number plates appear anywhere. people_visible is true if any "
        "person, face, hand or body part is visible. overlay_graphics is true if there are subtitles, watermarks or "
        f"app interface elements. subject_visible is true if the picture clearly shows {subject}.")


def _spec_vision(name: str) -> dict:
    return json.loads((SPECS / f"{name}.json").read_text())["vision"]


# What each format is, as the Lab's catalog defines it, and what its judge may fail it for.
FORMATS: dict[str, dict] = {
    "boat": {
        "format": "boat-lake", "family": "boat", "shape": "landscape", "crops": "both", "method": "t2v",
        "clean": TEXT_CLEAN, "vision": lambda: _spec_vision("boat"), "combinations": [0, 1, 2, 3]},
    "christmas-pov": {
        # The Lab's prompt is written for one unbroken eight-second shot, so render eight.
        "format": "pov-seasonal", "family": "christmas-pov", "shape": "vertical", "method": "t2v", "seconds": 8,
        "clean": POV_CLEAN, "vision": lambda: _spec_vision("christmas"), "combinations": [0, 167, 41, 233, 97, 300, 60, 205]},
    "ugc": {
        # This format wants FORD on the grille, so lettering is not a fault and nothing is cleaned.
        "format": "truck-scenic", "family": "ugc", "shape": "landscape", "crops": "dual", "method": "t2v",
        "clean": None, "combinations": [0, 13, 29, 41],
        "vision": lambda: {"question": _question("a phone video of a parked lifted pickup truck", "a pickup truck"),
                           "must_be_false": {"no_people": "people_visible", "no_overlay": "overlay_graphics"},
                           "must_be_true": {"subject_in_view": "subject_visible"}}},
    "scenic": {
        # The Lab starts this format from one reference photo of the truck and asks the
        # image model to put that same truck in a new place: an edit, not a fresh picture.
        "format": "truck-scenic", "family": "scenic", "shape": "vertical", "method": "i2v",
        "clean": None, "anchor": "scenic-v2.jpg", "anchor_use": "reframe", "combinations": [0, 13, 29, 41],
        "vision": lambda: {"question": _question("a phone video of a parked black pickup truck", "a pickup truck"),
                           "must_be_false": {"no_text": "readable_text_or_logos", "no_people": "people_visible",
                                             "no_overlay": "overlay_graphics"},
                           "must_be_true": {"subject_in_view": "subject_visible"}}},
    "scenic-ref": {
        # Same format, shorter route: H3's reference-to-video model is shown the Lab's truck
        # photo and asked for the new scene directly, with no separate re-staging edit.
        "format": "truck-scenic", "family": "scenic", "shape": "vertical", "method": "i2v",
        "clean": None, "anchor": "scenic-v2.jpg", "anchor_use": "reference", "animate": "h3-ref",
        "combinations": [0, 13, 29, 41],
        "vision": lambda: {"question": _question("a phone video of a parked black pickup truck", "a pickup truck"),
                           "must_be_false": {"no_text": "readable_text_or_logos", "no_people": "people_visible",
                                             "no_overlay": "overlay_graphics"},
                           "must_be_true": {"subject_in_view": "subject_visible"}}},
    "coffee": {
        # The Lab animates one fixed photo of the cup; only the coffee surface may move.
        "format": "coffee-tok", "family": "coffee", "shape": "vertical", "method": "i2v",
        # LTX zoomed and slid the cup; this format needs a locked camera, so H3 animates it
        # with the photo pinned at four points along the clip (only the coffee can move).
        "clean": None, "anchor": "coffee-crema-ribbon-anchor.png", "anchor_use": "frame", "combinations": [0, 3, 5, 7],
        # H3 with the photo pinned (four pins, or first and last only) holds the cup perfectly
        # but leaves the crema frozen: both measured the same near-zero movement. Wan 2.2
        # image-to-video is what the Lab pays for on this format, so the local copy is next.
        "animate": "wan-i2v", "black_background": True,
        # With the Lab's motion prompt, local Wan moved the crema well but a pitcher and a hand
        # came into frame in the last second of both clips: the prompt names them ("no pitcher,
        # no hand ...") and the fast 4-step setting ignores negatives. So: say only what should
        # be there, and render four seconds instead of five.
        "motion_prompt": ("Only the coffee surface moves: the tan crema drifts slowly and gently clockwise like real "
                          "liquid under surface tension, fine micro-foam wisps stretch and relax, tiny rim bubbles "
                          "hold their size. The white porcelain cup, its rim, its handle and the saucer stay perfectly "
                          "fixed and still. Locked overhead camera, pure black background, the frame around the cup "
                          "stays empty and unchanged for the whole shot, photorealistic, gentle seamless loop."),
        "num_frames": 65,
        "vision": lambda: {"question": _question("an overhead video of a cup of coffee on a black background",
                                                 "exactly one cup of coffee seen from directly above"),
                           "must_be_false": {"no_text": "readable_text_or_logos", "no_people": "people_visible",
                                             "no_overlay": "overlay_graphics"},
                           "must_be_true": {"subject_in_view": "subject_visible"}}},
    "dirt-bike": {
        # The rider's own hands and arms belong in the shot, so people are not a fault here.
        # Every still drew garbled lettering on the handlebar crossbar pad, so that is cleaned off.
        "prompts": DIRT_BIKE_PROMPTS, "shape": "vertical", "method": "t2v", "seconds": 5,
        "clean": ("In <Picture 1>, remove all lettering, logos, numbers and stickers from the dirt bike's handlebar "
                  "pad, front fender, number plate and hand guards, leaving plain surfaces in the same colours. Keep "
                  "everything else exactly the same: the handlebars, the trail, the landscape, the light and the "
                  "motion blur."),
        "combinations": [0, 1, 2, 3],
        "vision": lambda: {"question": _question("a first-person helmet-camera video from a dirt bike",
                                                 "a dirt bike's handlebars seen from the rider's position"),
                           "must_be_false": {"no_text": "readable_text_or_logos", "no_overlay": "overlay_graphics"},
                           "must_be_true": {"subject_in_view": "subject_visible"}}},
    "silhouette": {
        # A still, not a clip: two silhouetted people are the subject, so people are expected.
        "format": "silhouette-truck", "family": "silhouette", "shape": "vertical", "method": "t2i",
        "clean": None, "combinations": [0, 5, 11, 17],
        "vision": lambda: {"question": _question("a dusk photograph of two silhouetted people beside a pickup truck",
                                                 "two dark human silhouettes and a pickup truck"),
                           "must_be_false": {"no_text": "readable_text_or_logos", "no_overlay": "overlay_graphics"},
                           "must_be_true": {"subject_in_view": "subject_visible"}}},
}


def split_prompt(prompt: str) -> tuple[str, str]:
    """(picture prompt, motion prompt). Image-to-video formats carry both, joined by ' Motion: '."""
    picture, _, motion = prompt.partition(" Motion: ")
    picture = picture.replace("Reframe the same ", "A ").replace(" as a ", ", seen in a ", 1)
    return picture.strip(), (motion.strip() or prompt)


def say(text: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {text}", flush=True)


def fit(src: Path, dest: Path, width: int, height: int) -> Path:
    """Scale and centre-crop a picture to exactly the frame size."""
    import subprocess
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf",
                    f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height}",
                    str(dest)], check=True)
    return dest


def judge_file(path: Path, config: dict, crops: str | None) -> dict:
    """Faults for one picture or clip, whole frame and (for sliced formats) per posted slice."""
    facts = rl.measure(path)
    facts["vision"] = rl.vision_facts(path, facts["frames"], config)
    checks = rl.judge(facts, {}, config)
    out = {"faults": [name for name, ok in checks.items() if not ok], "where": facts["vision"].get("where", ""),
           "motion": facts["motion"], "brightness": facts["brightness"], "black_bars": facts.get("black_bars", 0.0)}
    if crops and facts["frames"] > 1:
        pieces = rl.crop_facts(path, facts["frames"], config, crops)
        out["slices"] = [c["faults"] for c in pieces]
        out["usable_slices"] = sum(1 for c in pieces if not c["faults"])
    return out


async def run(run_dir: Path, scenes: int, only: list[str] | None, kinds: set[str] | None = None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    results_path = run_dir / "results.jsonl"
    done = {(r["format"], r["scene"], r["kind"]) for r in load(run_dir)}
    lock = (rl.LAB_DIR / ".round.lock").open("w")
    await asyncio.to_thread(fcntl.flock, lock, fcntl.LOCK_EX)          # wait for any sweep that is mid-round

    jobs = []
    for name, cfg in FORMATS.items():
        if only and name not in only:
            continue
        for scene in range(scenes):
            combination = cfg["combinations"][scene % len(cfg["combinations"])]
            if cfg.get("prompts"):
                prompt = cfg["prompts"][combination % len(cfg["prompts"])]
            else:
                prompt, _ = rl.catalog_prompt(cfg["format"], cfg["family"], combination)
            picture, motion = split_prompt(prompt) if cfg["method"] == "i2v" else (prompt, prompt)
            jobs.append({"format": name, "cfg": cfg, "scene": scene, "combination": combination,
                         "picture_prompt": picture, "motion_prompt": cfg.get("motion_prompt") or motion,
                         "seed": 7000 + 10 * scene,
                         "vision": cfg["vision"](), "size": SIZES[cfg["shape"]]})

    def record(job: dict, kind: str, path: Path, seconds: float | None, error: str | None = None) -> None:
        row = {"format": job["format"], "scene": job["scene"], "kind": kind, "file": path.name,
               "seconds": seconds, "combination": job["combination"]}
        if error:
            row["error"] = error[:400]
        else:
            try:
                row.update(judge_file(path, job["vision"], job["cfg"].get("crops")))
                if job["cfg"].get("black_background"):
                    row["black_bars"] = 0.0          # the black surround is the design, not letterboxing
                    row["faults"] = [f for f in row["faults"] if f != "no_black_bars"]
            except Exception as exc:  # noqa: BLE001 - keep the clip even if the judge is unavailable
                row["judge_error"] = (str(exc) or repr(exc))[:300]
        with results_path.open("a") as f:
            f.write(json.dumps(row) + "\n")
        verdict = error or row.get("judge_error") or (", ".join(row.get("faults", [])) or "clean")
        extra = f", usable slices {row['usable_slices']}/{len(row['slices'])}" if "slices" in row else ""
        say(f"{job['format']} scene {job['scene']} {kind}: {seconds}s, {verdict}{extra}")
        write_page(run_dir)

    async def step(job: dict, kind: str, path: Path, make) -> bool:
        """Run one render unless it is already on disk; returns whether the file exists afterwards."""
        if kinds and kind not in kinds:
            return path.exists()
        if (job["format"], job["scene"], kind) in done and path.exists():
            return True
        try:
            seconds = round(await make(), 1) if not path.exists() else None
            record(job, kind, path, seconds)
        except Exception as exc:  # noqa: BLE001 - one bad render must not stop the pass
            record(job, kind, path, None, (str(exc) or repr(exc)))
        return path.exists()

    async with httpx.AsyncClient() as client:
        # Pass 1: one still per scene, on the H3 video model. Formats that start from
        # the Lab's own reference photo use that photo instead.
        for job in jobs:
            anchor = ANCHORS / job["cfg"]["anchor"] if job["cfg"].get("anchor") else None
            job["anchor"] = anchor if anchor and anchor.exists() else None
            if job["anchor"]:
                job["frame"] = job["anchor"] if job["cfg"]["anchor_use"] in ("frame", "reference") else None
                continue
            w, h = job["size"]
            still = run_dir / f"{job['format']}__s{job['scene']}_still.png"
            graph = h3_image.still_graph(job["picture_prompt"], w, h, job["seed"])
            async def make(graph=graph, still=still):
                await rl.prepare_server(client, graph)
                return await h3_image.run_image(client, graph, still)
            job["frame"] = still if await step(job, "still", still, make) else None
        # Pass 2: the image model. It re-stages the reference truck for the scenic
        # format, and cleans the stills of formats that forbid lettering.
        for job in jobs:
            if job.get("anchor") and job["cfg"]["anchor_use"] == "reframe":
                w, h = job["size"]
                staged = run_dir / f"{job['format']}__s{job['scene']}_clean.png"
                order = ("Take the truck in <Picture 1> and show that exact same truck in a new photograph: "
                         + job["picture_prompt"] + " The truck's shape, colour, wheels and unmarked surfaces stay "
                         "identical to <Picture 1>.")
                async def make(job=job, staged=staged, order=order, w=w, h=h):
                    graph = h3_image.edit_graph(await h3_image.upload(client, job["anchor"]), order, w, h, job["seed"])
                    await rl.prepare_server(client, graph)
                    return await h3_image.run_image(client, graph, staged)
                if await step(job, "clean", staged, make):
                    job["frame"] = staged
                continue
            if not job["cfg"]["clean"] or not job["frame"]:
                continue
            w, h = job["size"]
            cleaned = run_dir / f"{job['format']}__s{job['scene']}_clean.png"
            async def make(job=job, cleaned=cleaned, w=w, h=h):
                graph = h3_image.edit_graph(await h3_image.upload(client, job["frame"]), job["cfg"]["clean"], w, h, job["seed"])
                await rl.prepare_server(client, graph)
                return await h3_image.run_image(client, graph, cleaned)
            if await step(job, "clean", cleaned, make):
                job["frame"] = cleaned
        # Pass 3: LTX animates each picture; text-to-video formats also get a prompt-only clip.
        for job in jobs:
            if job["cfg"]["method"] == "t2i":
                continue
            w, h = job["size"]
            base = {"width": w, "height": h, "duration": int(job["cfg"].get("seconds", 3))}
            if job["frame"] and not job["cfg"].get("animate"):       # LTX unless the format names another engine
                clip = run_dir / f"{job['format']}__s{job['scene']}_animate.mp4"
                async def make(job=job, clip=clip, base=base):
                    graph = rl.ltx_with_first_frame(
                        rl.build_ltx23_graph(job["motion_prompt"], base, {}, job["seed"]),
                        await h3_image.upload(client, job["frame"]))
                    return await rl.render(client, graph, clip)
                await step(job, "animate", clip, make)
            if job["cfg"]["method"] == "t2v":
                clip = run_dir / f"{job['format']}__s{job['scene']}_text.mp4"
                async def make(job=job, clip=clip, base=base):
                    return await rl.render(client, rl.build_ltx23_graph(job["motion_prompt"], base, {}, job["seed"]), clip)
                await step(job, "text", clip, make)
        # Pass 4: formats that need a locked camera, on H3 with the picture pinned along the clip.
        for job in jobs:
            if job["cfg"].get("animate") != "h3-pinned" or not job["frame"]:
                continue
            w, h = job["size"]
            clip = run_dir / f"{job['format']}__s{job['scene']}_animate.mp4"
            async def make(job=job, clip=clip, w=w, h=h):
                frame = fit(job["frame"], run_dir / f"{job['format']}__s{job['scene']}_frame.png", w, h)
                graph = rl.build_graph(job["motion_prompt"], {
                    "model_id": rl.comfyui.H3_FL2VA, "variant": "vertical" if h > w else "landscape",
                    "duration": 3, "go_fast": True}, {}, job["seed"])
                h3_image.with_anchors(graph, await h3_image.upload(client, frame),
                                      fractions=tuple(job["cfg"].get("pins") or (1 / 3, 2 / 3, 1.0)))
                return await rl.render(client, graph, clip)
            await step(job, "animate", clip, make)
        # Pass 4b: H3 reference-to-video. The picture is a reference for the subject, not a first frame.
        for job in jobs:
            if job["cfg"].get("animate") != "h3-ref" or not job["frame"]:
                continue
            w, h = job["size"]
            clip = run_dir / f"{job['format']}__s{job['scene']}_animate.mp4"
            async def make(job=job, clip=clip, w=w, h=h):
                prompt = (f"The pickup truck from the reference picture, unchanged. {job['picture_prompt']} "
                          f"{job['motion_prompt']}")
                graph = rl.build_graph(prompt, {
                    "model_id": rl.comfyui.H3_FL2VA, "variant": "vertical" if h > w else "landscape",
                    "duration": 3, "go_fast": True}, {}, job["seed"])
                graph["unet"]["inputs"]["unet_name"] = "minimax_h3_ref2va_bf16.safetensors"
                graph["lora"]["inputs"]["lora_name"] = "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"
                graph["sigmas"]["inputs"]["steps"] = 4
                graph["ref"] = {"class_type": "LoadImage", "inputs": {"image": await h3_image.upload(client, job["frame"])}}
                old_inputs = graph["h3"]["inputs"]
                graph["h3"] = {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
                    "clip": old_inputs["clip"], "vae": old_inputs["vae"], "prompt": prompt,
                    "width": old_inputs["width"], "height": old_inputs["height"], "length": old_inputs["length"],
                    "ref_image_size": "match", "ref_images.ref_image_1": ["ref", 0]}}
                return await rl.render(client, graph, clip)
            await step(job, "animate", clip, make)
        # Pass 5: formats the Lab itself runs on Wan image-to-video, on the local copy of it.
        for job in jobs:
            if job["cfg"].get("animate") != "wan-i2v" or not job["frame"]:
                continue
            w, h = job["size"]
            clip = run_dir / f"{job['format']}__s{job['scene']}_animate.mp4"
            async def make(job=job, clip=clip, w=w, h=h):
                frame = fit(job["frame"], run_dir / f"{job['format']}__s{job['scene']}_frame.png", w, h)
                graph = rl.copy.deepcopy(rl.comfyui.build_workflow(job["motion_prompt"], {
                    "model_id": rl.comfyui.WAN_I2V, "resolution": "720p", "go_fast": True, "seed": job["seed"],
                    "num_frames": int(job["cfg"].get("num_frames", 81)),
                    "image_data_uri": "data:image/png;base64,AAAA", "image_size": (w, h),
                    "image_name": await h3_image.upload(client, frame)}))
                for node in graph.values():
                    if "filename_prefix" in node.get("inputs", {}):
                        node["inputs"]["filename_prefix"] = "content-lab/recipe-lab"
                return await rl.render(client, graph, clip)
            await step(job, "animate", clip, make)
    write_page(run_dir)
    say("format coverage finished")


def load(run_dir: Path) -> list[dict]:
    path = run_dir / "results.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


_KINDS = {"still": "H3 still", "clean": "cleaned or re-staged still", "animate": "animated from the picture", "text": "LTX from the prompt"}


def write_page(run_dir: Path) -> Path:
    rows = load(run_dir)
    out = [f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<meta http-equiv=refresh content=90><title>All formats, local</title><style>{rl._SHEET_CSS}"
           ".grid{grid-template-columns:repeat(auto-fill,minmax(220px,1fr))}.clip video,.clip img{max-height:420px;"
           "object-fit:contain;background:#000}</style>",
           "<h1>Every generated format on local models</h1>",
           f"<p class=note>First pass: the Lab's own prompts, no per-format tuning yet. Each format goes through the "
           f"steps it needs (still, clean, animate). A red label is what the judge faulted. "
           f"Updated {datetime.now():%Y-%m-%d %H:%M:%S}.</p>",
           "<table><tr><th>Format</th><th>Step</th><th>Passed the judge</th><th>Usable slices</th><th>Avg time</th></tr>"]
    for name in FORMATS:
        for kind, label in _KINDS.items():
            mine = [r for r in rows if r["format"] == name and r["kind"] == kind]
            if not mine:
                continue
            good = [r for r in mine if "error" not in r and "judge_error" not in r]
            clean = sum(1 for r in good if not r.get("faults") and r.get("black_bars", 0) < 0.04)
            usable = sum(r.get("usable_slices", 0) for r in good)
            total = sum(len(r.get("slices", [])) for r in good)
            times = [r["seconds"] for r in good if r.get("seconds")]
            failed = len(mine) - len(good)
            out.append(f"<tr><td><b>{html.escape(name)}</b></td><td>{label}</td><td>{clean}/{len(good)}"
                       f"{' + ' + str(failed) + ' failed' if failed else ''}</td>"
                       f"<td>{f'{usable}/{total}' if total else '-'}</td>"
                       f"<td>{f'{sum(times) / len(times):.0f} s' if times else '-'}</td></tr>")
    out.append("</table>")
    for name in FORMATS:
        mine = [r for r in rows if r["format"] == name]
        if not mine:
            continue
        out.append(f"<h2>{html.escape(name)}</h2><div class=grid>")
        for r in sorted(mine, key=lambda r: (r["scene"], list(_KINDS).index(r["kind"]))):
            title = f"scene {r['scene'] + 1} · {_KINDS[r['kind']]}"
            if "error" in r:
                out.append(f"<div class=clip><b>{title}</b><div class='meta bad'>{html.escape(r['error'])}</div></div>")
                continue
            src = html.escape(r["file"])
            media = (f"<video src='{src}' muted loop playsinline controls preload=metadata></video>"
                     if r["file"].endswith(".mp4") else f"<a href='{src}'><img src='{src}' alt=''></a>")
            faults = r.get("faults", []) + (["black_bars"] if r.get("black_bars", 0) >= 0.04 else [])
            verdict = (f"<span class=bad>{html.escape(', '.join(faults))}</span>" if faults else
                       "<span class=bad>not judged</span>" if "judge_error" in r else "<span class=ok>passed</span>")
            slices = f" · {r['usable_slices']}/{len(r['slices'])} slices" if "slices" in r else ""
            out.append(f"<div class=clip><b>{title}</b>{media}<div class=meta>{verdict}{slices}"
                       f"{' · ' + str(r['seconds']) + ' s' if r.get('seconds') else ''}</div></div>")
        out.append("</div>")
    page = run_dir / "index.html"
    page.write_text("\n".join(out))
    return page


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--scenes", type=int, default=2)
    parser.add_argument("--only", help="comma-separated format names")
    parser.add_argument("--kinds", help="comma-separated steps to run: still, clean, animate, text (default all)")
    args = parser.parse_args()
    asyncio.run(run(args.run_dir, args.scenes, args.only.split(",") if args.only else None,
                    set(args.kinds.split(",")) if args.kinds else None))


if __name__ == "__main__":
    main()
