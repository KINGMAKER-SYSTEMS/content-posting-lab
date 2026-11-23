"""Trim a tall dashboard off the bottom of a from-the-car clip, leaving a thin strip.

    python tools/blockout/trim_dash.py IN.mp4 OUT.mp4 [--keep 0.05]

The Lab's drive prompts ask for "a narrow dark dashboard edge at the bottom", but every look
still drew a full dashboard over the bottom quarter to third of the frame, and three different
wordings of the still prompt did not change that (2026-11-23). So the fix is in post: measure how
tall the dark band at the bottom is on a middle frame, crop it down to --keep of the frame
height, keep the picture's shape by trimming the sides equally, and scale back to the original
size. Prints what it did; copies the clip unchanged when the band is already small.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess

import numpy as np


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("src")
    parser.add_argument("dest")
    parser.add_argument("--keep", type=float, default=0.05, help="fraction of the frame height left as dashboard")
    # the houses are on the right in the Lab's drive scenes; trimming both sides equally cut their
    # lights out of the last frames of three clips, which then failed the Christmas-lights check
    parser.add_argument("--side", choices=["centre", "right", "left"], default="right",
                        help="which side of the picture to keep when the width is trimmed")
    # 78, not something near black: on the exposure-held clips the dashboard rows average 20-75 and
    # the road above them 75-125 (measured in 5% bands on three clips)
    parser.add_argument("--dark", type=float, default=78.0, help="a row darker than this (0-255) counts as dashboard")
    a = parser.parse_args()
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,nb_frames", "-of", "csv=p=0", a.src],
                         capture_output=True, text=True, check=True).stdout.strip().split(",")
    width, height, frames = int(out[0]), int(out[1]), int(out[2])
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", a.src, "-vf", f"select=eq(n\\,{frames // 2})", "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    rows = np.frombuffer(raw, dtype=np.uint8).reshape(height, width).astype(np.float32).mean(axis=1)
    band, bright_run = 0, 0
    for value in rows[::-1]:                              # walk up from the bottom edge
        if value < a.dark:
            band += 1 + bright_run
            bright_run = 0
        else:
            bright_run += 1
            if bright_run > height * 0.04:                # a real change, not a reflection on the dash
                break
    share = band / height
    if share <= a.keep + 0.04:
        shutil.copy(a.src, a.dest)
        print(f"dashboard is {share:.0%} of the frame: left as it is")
        return
    crop_h = int(height * (1 - share + a.keep)) // 2 * 2
    crop_w = int(width * crop_h / height) // 2 * 2
    x = {"centre": (width - crop_w) // 2, "right": width - crop_w, "left": 0}[a.side]
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", a.src, "-vf",
                    f"crop={crop_w}:{crop_h}:{x}:0,scale={width}:{height}:flags=lanczos",
                    "-an", "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", a.dest], check=True)
    print(f"dashboard was {share:.0%} of the frame, now {a.keep:.0%}; kept {crop_w}x{crop_h} of {width}x{height}")


if __name__ == "__main__":
    main()
