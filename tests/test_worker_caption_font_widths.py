"""The control-plane Worker sizes mirror captions with a copy of THIS repo's font metrics.

The Worker decides whether a mirror post's exact source lines will be drawn big enough to read
(control-plane-worker/src/domain/captionLineFit.js). It cannot open font files, so it carries a
table of every TikTok Sans font's widths here, measured with Pillow the way
services/caption_render.py draws (ImageFont.truetype at 1000 px: round(getlength) for the advance,
getbbox for the ink edges; a space's edges are its pen positions).

tests/fixtures/worker_caption_font_widths.json is that table. These tests fail when a font file is
added, removed or changed, or when a Pillow upgrade moves a width, so the Worker is never left
judging captions on numbers this Lab no longer draws. When they fail, rebuild the Worker table
from this repo's fonts and copy it here in the same change.
"""

import hashlib
import json
from pathlib import Path

import pytest
from PIL import ImageFont

ROOT = Path(__file__).parents[1]
TABLE = json.loads((ROOT / "tests" / "fixtures" / "worker_caption_font_widths.json").read_text(encoding="utf-8"))
FONTS = sorted(path for path in (ROOT / "fonts").glob("TikTokSans*.ttf") if "Italic" not in path.name)


def test_every_drawable_font_is_in_the_worker_table_and_nothing_else():
    assert FONTS, "no TikTok Sans fonts found"
    assert sorted(path.name for path in FONTS) == sorted(TABLE["fonts"]), (
        "the Lab's drawable TikTok Sans fonts and the Worker table differ: rebuild the table")


@pytest.mark.parametrize("path", FONTS, ids=lambda path: path.name)
def test_font_file_is_the_one_the_worker_table_was_measured_on(path):
    assert hashlib.sha256(path.read_bytes()).hexdigest() == TABLE["fonts"][path.name]["sha256"], (
        f"{path.name} changed since the Worker table was built: rebuild the table")


@pytest.mark.parametrize("path", FONTS, ids=lambda path: path.name)
def test_pillow_still_measures_the_worker_table_widths(path):
    units = TABLE["unitsPerPx"]
    font = ImageFont.truetype(str(path), units)
    row = TABLE["fonts"][path.name]
    advance, ink_left, ink_right = (row[key].split(",") for key in ("advance", "inkLeft", "inkRight"))
    moved = []
    for index, char in enumerate(TABLE["charset"]):
        width = round(font.getlength(char))
        box = font.getbbox(char)
        left, right = (0, width) if char == " " else (box[0], box[2])
        for name, want, got in (("advance", advance[index], width), ("inkLeft", ink_left[index], left),
                                ("inkRight", ink_right[index], right)):
            if want != str(got):
                moved.append(f"{char!r} {name}: table {want}, Pillow {got}")
    assert not moved, f"{path.name}: {len(moved)} widths moved (first: {moved[:3]}): rebuild the Worker table"
