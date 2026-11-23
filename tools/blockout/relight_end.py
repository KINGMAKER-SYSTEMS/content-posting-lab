"""Re-render a clip with its ending fixed: edit the last frame, then pin both ends.

    python tools/blockout/relight_end.py SCENE --tag x1 --seed 51 --instruction "In <Picture 1>, ..."

Some Christmas clips end weakly: a building that was small and far away in the first frame
arrives with its lights dark (bakery street, three of the drives, 2026-11-23). The first frame
cannot fix that, since it is the only thing pinned. This takes the last frame of the clip that
was already rendered, has MiniMax H3-Image redraw it to the instruction (same geometry, lights
added), and renders the shot again with the original still on the first frame and the edited
picture on the last (tools/blockout/two_ends.py). Exposure hold and dashboard trim are applied
as the scene's settings say. Prints the Lab's checks for the new clip.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))

import h3_image  # noqa: E402
import pipeline  # noqa: E402
import recipe_lab as rl  # noqa: E402

OUT = Path.home() / "ComfyUI/output/ec-blockout"


async def edit(src: Path, dest: Path, instruction: str, seed: int) -> None:
    width, height = h3_image._size(src)
    width, height = width // 32 * 32, height // 32 * 32
    async with httpx.AsyncClient() as client:
        graph = h3_image.edit_graph(await h3_image.upload(client, src), instruction, width, height, seed)
        await rl.prepare_server(client, graph)
        await h3_image.run_image(client, graph, dest)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scene", choices=sorted(pipeline.SCENES))
    parser.add_argument("--tag", required=True, help="the tag of the clip already rendered")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--instruction", required=True)
    parser.add_argument("--new-tag", default="", help="tag for the new clip (default: <tag>e)")
    parser.add_argument("--keep-frame", type=int, default=0,
                        help="also pin this frame of the rendered clip, so what it shows mid-clip is kept")
    parser.add_argument("--reuse-end", action="store_true", help="use the end picture already drawn for this tag")
    args = parser.parse_args()
    scene, cfg = args.scene, pipeline.SCENES[args.scene]
    shot = pipeline.ROOT / scene
    name, new = f"{args.tag}-{scene}", f"{args.new_tag or args.tag + 'e'}-{scene}"
    state = json.loads((shot / "hero" / f"{name}_state.json").read_text())
    # the last frame of what was rendered, at the exposure the clip opens with if a held copy exists
    rendered = next(p for p in (OUT / f"{name}_00001__held.mp4", OUT / f"{name}_00001_.mp4") if p.exists())
    last = shot / "hero" / f"{name}_last.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-sseof", "-0.2", "-i", str(rendered), "-frames:v", "1",
                    "-update", "1", str(last)], check=True)
    end = shot / "hero" / f"{new}_end.png"
    drawn = [p for p in (end, shot / "hero" / f"{args.tag}e-{scene}_end.png") if p.exists()]
    if args.reuse_end and drawn:
        end = drawn[0]
        pipeline.say(f"{scene}: reusing {end.name}")
    else:
        pipeline.say(f"{scene}: redrawing the last frame to the instruction")
        with pipeline.warm("clean"):
            asyncio.run(edit(last, end, args.instruction, args.seed))
    keep = []
    if args.keep_frame:
        mid = shot / "hero" / f"{name}_f{args.keep_frame}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(rendered), "-vf", f"select=eq(n\\,{args.keep_frame})",
                        "-frames:v", "1", "-update", "1", str(mid)], check=True)
        keep = ["--mid", str(mid), "--mid-frame", str(args.keep_frame)]
    pipeline.say(f"{scene}: video with both ends pinned")
    guide = ["--strength", str(cfg["guide"])] if cfg.get("guide") else []
    with pipeline.warm("video"):
        out = pipeline.sh(["python3", str(HERE / "two_ends.py"), str(shot), "--ref", state["frame"], "--end", str(end),
                           "--name", new, "--seed", str(args.seed + 100), "--prompt", cfg["video"], *guide, *keep])
    clip = Path(re.search(r"^output (.+\.mp4)\s*$", out, re.M).group(1))
    for flag, tool, suffix in (("hold_exposure", "hold_exposure.py", "_held"), ("trim_dash", "trim_dash.py", "_trim")):
        if cfg.get(flag):
            nxt = clip.with_name(clip.stem + f"{suffix}.mp4")
            pipeline.sh([sys.executable, str(HERE / tool), str(clip), str(nxt)])
            clip = nxt
    spec = json.loads((HERE.parent / "recipe_lab_specs" / f"{cfg.get('spec', 'truck')}.json").read_text())
    facts, checks = rl.score(clip, spec)
    pipeline.say(f"{scene}: relit clip {clip}, failed checks {[k for k, v in checks.items() if not v]}")


if __name__ == "__main__":
    main()
