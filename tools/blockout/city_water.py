"""Give each city harbor scene its own opening water picture.

    python tools/blockout/city_water.py [scene ...] [--seed 7]

The reversed harbor clips open on the picture pinned to their last rendered frame. The first five
scenes all pinned the same one (water_end_sharp.png: a wake behind a red-striped stern, trees on
the far shore), so every clip opened identically and the stern did not match the white bow seen a
second later (round c1, 2026-11-23). This has MiniMax H3-Image redraw that picture once per city:
plain white stern, that city's lights on the far shore and in the water. The result is written
to <scene>/hero/water_end_city.png, which the scene names as its end_still.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pipeline  # noqa: E402
import relight_end  # noqa: E402

SOURCE = pipeline.ROOT / "harbor-dusk-rev" / "hero" / "water_end_sharp.png"
KEEP = ("Keep the wake, the white foam, the waves, the camera angle and the sharpness exactly the same. "
        "No people, no other boats, no text.")
CITIES = {
    "harbor-newyork-rev": "In <Picture 1>, repaint the boat's stern at the bottom plain glossy white with no red, "
                          "replace the line of trees on the far shore with a distant low row of city buildings "
                          "with small warm window lights, and tint the open water with soft orange and violet "
                          "dusk reflections. " + KEEP,
    "harbor-dubai-rev": "In <Picture 1>, repaint the boat's stern at the bottom plain glossy white with no red, "
                        "replace the line of trees on the far shore with a distant row of tall slender towers "
                        "with small lights, make the open water calmer and tint it with golden and pale pink "
                        "dusk reflections. " + KEEP,
    "harbor-hongkong-rev": "In <Picture 1>, repaint the boat's stern at the bottom plain glossy white with no red, "
                           "replace the line of trees on the far shore with a distant dense row of lit high-rise "
                           "buildings below dark hills, and tint the open water with magenta, teal and gold "
                           "neon reflections under a darker dusk sky. " + KEEP,
    "harbor-singapore-rev": "In <Picture 1>, repaint the boat's stern at the bottom plain glossy white with no red, "
                            "replace the line of trees on the far shore with a distant row of lit glass office "
                            "towers, and tint the open water with teal and soft gold dusk reflections under a "
                            "pastel sky. " + KEEP,
    "harbor-shanghai-rev": "In <Picture 1>, repaint the boat's stern at the bottom plain glossy white with no red, "
                           "make it night with a dark hazy sky, replace the line of trees on the far shore with a "
                           "distant dense row of brightly lit skyscrapers, and make the open water black with "
                           "blue, pink and white light reflections. " + KEEP,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenes", nargs="*", default=list(CITIES))
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    with pipeline.warm("clean"):
        for scene in args.scenes:
            dest = pipeline.ROOT / scene / "hero" / "water_end_city.png"
            pipeline.say(f"{scene}: drawing its own opening water picture")
            asyncio.run(relight_end.edit(SOURCE, dest, CITIES[scene], args.seed))
            pipeline.say(f"{scene}: wrote {dest.name}")


if __name__ == "__main__":
    main()
