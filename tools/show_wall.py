"""Tile several clips into one labelled video wall, so a batch can be watched at a glance.

  python tools/show_wall.py out.mp4 --cols 4 --cell 360x640 --frames 121 \\
      "clip1.mp4|Title|note" "clip2.mp4|Title|note" ...
A .png cell is held as a still. Labels are drawn with Pillow because this ffmpeg has no drawtext.
"""
from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


def label(text: str, sub: str, w: int, h: int, dest: Path) -> Path:
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, h - 52, w, h], fill=(0, 0, 0, 170))
    draw.text((10, h - 48), text, font=ImageFont.truetype(FONT, 20), fill=(255, 255, 255, 255))
    draw.text((10, h - 24), sub, font=ImageFont.truetype(FONT, 15), fill=(210, 210, 210, 255))
    img.save(dest)
    return dest


def wall(out: Path, cells: list[tuple[str, str, str]], cols: int, cw: int, ch: int, frames: int) -> Path:
    seconds = frames / 24
    with tempfile.TemporaryDirectory() as tmp:
        inputs: list[str] = []
        filters: list[str] = []
        for i, (path, text, sub) in enumerate(cells):
            inputs += (["-loop", "1", "-t", f"{seconds:.3f}"] if path.endswith(".png") else []) + ["-i", path]
            inputs += ["-i", str(label(text, sub, cw, ch, Path(tmp) / f"{i}.png"))]
            filters.append(
                f"[{2 * i}:v]fps=24,trim=end_frame={frames},setpts=PTS-STARTPTS,"
                f"scale={cw}:{ch}:force_original_aspect_ratio=increase,crop={cw}:{ch}[v{i}];"
                f"[v{i}][{2 * i + 1}:v]overlay=0:0[c{i}]")
        layout = "|".join(f"{(i % cols) * cw}_{(i // cols) * ch}" for i in range(len(cells)))
        filters.append("".join(f"[c{i}]" for i in range(len(cells))) + f"xstack=inputs={len(cells)}:layout={layout}[out]")
        subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(filters), "-map", "[out]",
                        "-c:v", "libx264", "-crf", "21", "-pix_fmt", "yuv420p", "-r", "24", str(out)], check=True)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out", type=Path)
    parser.add_argument("cells", nargs="+", help='"path|title|note"')
    parser.add_argument("--cols", type=int, default=4)
    parser.add_argument("--cell", default="360x640")
    parser.add_argument("--frames", type=int, default=73)
    args = parser.parse_args()
    cw, ch = (int(x) for x in args.cell.split("x"))
    cells = [tuple((c.split("|") + ["", ""])[:3]) for c in args.cells]
    print(wall(args.out, cells, args.cols, cw, ch, args.frames))


if __name__ == "__main__":
    main()
