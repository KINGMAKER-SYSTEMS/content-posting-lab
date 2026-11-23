"""Depth-guided LTX video with BOTH ends pinned: a first-frame still and a last-frame still.

    python tools/blockout/two_ends.py SHOT --ref FIRST.png --end LAST.png --prompt "..." \
        [--seed 1] [--strength 0.6] [--name NAME]

ec's run_shot.py (~/dev/ec/work/1121-blockout2video/comfy) pins only the first frame. For a
reveal shot that leaves the other end of the clip to chance: in the boat format the water view
was invented each time, and came back as a second boat with people, a deck lit with neon strips,
or smear (harbor rounds b2-b5, 2026-11-23). This builds the same graph through ec's own functions
(imported, not changed) and adds one more LTXVAddGuide that pins a picture on the last frame.

Prints "output <path>" like run_shot.py, so tools/blockout/pipeline.py can call either.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

EC = Path.home() / "dev/ec/work/1121-blockout2video/comfy"
sys.path.insert(0, str(EC))

import run_shot as rs  # noqa: E402  (ec's script: staging, graph and wait logic are reused as they are)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("shot")
    parser.add_argument("--ref", required=True, help="picture pinned on the first frame")
    parser.add_argument("--end", required=True, help="picture pinned on the last frame")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--strength", type=float, default=1.0, help="how hard the depth video is held")
    parser.add_argument("--end-strength", type=float, default=1.0, help="how hard the last-frame picture is held")
    parser.add_argument("--distill", type=float, default=0.5)
    parser.add_argument("--name")
    parser.add_argument("--dry-run", action="store_true")
    a = parser.parse_args()

    m = json.load(open(os.path.join(a.shot, "passes.json")))
    if (m["frames"] - 1) % 8 or m["width"] % 64 or m["height"] % 64:
        sys.exit(f"ltx needs 8n+1 frames and sizes in multiples of 64; passes.json has {m}")
    tag = a.name or f"{os.path.basename(os.path.abspath(a.shot))}-ltx2"
    live = not a.dry_run
    depth = rs.stage(os.path.join(a.shot, "depth.mp4"), tag, live)
    ref = rs.stage(a.ref, tag, live)
    end = rs.stage(a.end, tag + "-end", live)

    g = rs.ltx_graph(SimpleNamespace(prompt=a.prompt, seed=a.seed, strength=a.strength, distill=a.distill), m, depth, ref)
    # the extra guide: one picture on the last frame, after the depth guide in the chain
    g["end"] = ("LoadImage", {"image": end})
    g["end_fit"] = ("ImageScale", {"image": ["end", 0], "upscale_method": "lanczos",
                                   "width": m["width"], "height": m["height"], "crop": "center"})
    g["end_guide"] = ("LTXVAddGuide", {"positive": ["guide", 0], "negative": ["guide", 1], "vae": ["ckpt", 2],
                                       "latent": ["guide", 2], "image": ["end_fit", 0], "frame_idx": -1,
                                       "strength": a.end_strength})
    g["av"][1]["video_latent"] = ["end_guide", 2]
    for node in ("sample", "crop"):
        g[node][1]["positive"], g[node][1]["negative"] = ["end_guide", 0], ["end_guide", 1]
    g["save"] = ("SaveVideo", {"video": ["video", 0], "filename_prefix": f"ec-blockout/{tag}",
                               "format": "auto", "codec": "auto"})
    prompt = {k: {"class_type": c, "inputs": i} for k, (c, i) in g.items()}
    if a.dry_run:
        print(json.dumps(prompt, indent=1, ensure_ascii=False))
        return
    rs.submit(prompt)


if __name__ == "__main__":
    main()
