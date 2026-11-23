"""Save the kept local renders, by Lab content format, in one place on the drive, with a page to watch them.

    python tools/blockout/save_library.py

Copies (never moves) the chosen clip for every scene into
/Volumes/OWC-Envoy-Ultra/content-posting-lab/library/<format>/, writes manifest.json (scene, take,
source file, route, the Lab checks it last failed) and index.html (one section per format). Safe
to run again: a file is copied only when it is missing or its source is newer, and nothing in the
library is deleted. Add a clip by adding a line to KEEP below.

The page is served by the "recipe-lab" preview server through the link
projects/recipe-lab/library -> the drive folder: http://localhost:8011/library/index.html
"""
from __future__ import annotations

import html
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import post_slices  # noqa: E402  (BEST_HD: the 1080p truck clip kept for each scene)

DRIVE = Path("/Volumes/OWC-Envoy-Ultra/content-posting-lab")
TRUCKS = DRIVE / "blockout-trucks"
VIDEOS = TRUCKS / "videos"
SHOW = HERE.parent.parent / "projects" / "recipe-lab" / "_show"
LIBRARY = DRIVE / "library"
BLENDER = "Blender blockout: depth pass, look still, depth-guided LTX 2.3"

# format -> (title, note, [(scene, take label, source path, route)])
KEEP: dict[str, tuple[str, str, list[tuple[str, str, Path, str]]]] = {}

KEEP["truck-scenic"] = (
    "Truck scenic", "One 1920x1088 render per scene; the five 9:16 slices a page posts are in slices/.",
    [(scene, "1080p", VIDEOS / name, BLENDER + ", emblems cleaned with H3-Image")
     for scene, name in post_slices.BEST_HD.items()])

KEEP["boat-lake"] = (
    "Boat / city harbor", "Reversed reveal: the move is blocked backwards and both ends are pinned.",
    [("harbor-night (Shanghai prompt)", "b8", VIDEOS / "harbor-night-rev__b8.mp4", BLENDER),
     ("harbor-dusk (Singapore prompt)", "b7", VIDEOS / "harbor-dusk-rev__b7.mp4", BLENDER),
     ("harbor-newyork", "c1", VIDEOS / "harbor-newyork-rev__c1.mp4", BLENDER),
     ("harbor-dubai", "c1", VIDEOS / "harbor-dubai-rev__c1.mp4", BLENDER),
     ("harbor-hongkong", "c1", VIDEOS / "harbor-hongkong-rev__c1.mp4", BLENDER),
     # round c2: each city opens on its own water picture (tools/blockout/city_water.py)
     ("harbor-newyork", "c2", VIDEOS / "harbor-newyork-rev__c2.mp4", BLENDER),
     ("harbor-dubai", "c2", VIDEOS / "harbor-dubai-rev__c2.mp4", BLENDER),
     ("harbor-hongkong", "c2", VIDEOS / "harbor-hongkong-rev__c2.mp4", BLENDER)])

RELIT = BLENDER + ", ending relit with H3-Image"
KEEP["pov-seasonal"] = (
    "Christmas POV", "Eight-second 768x1344 walks, drives and overlooks: the catalog's fifteen scenes, two takes each.",
    [("xmas-lane", "take 1", SHOW / "xmas-lane__x1-held.mp4", BLENDER),
     ("xmas-market", "take 1", VIDEOS / "xmas-market__x2.mp4", BLENDER),
     ("xmas-bridge", "take 1", VIDEOS / "xmas-bridge__x2.mp4", BLENDER),
     ("xmas-bakery", "take 1", SHOW / "xmas-bakery__x1e.mp4", RELIT),
     ("xmas-chapel", "take 1", VIDEOS / "xmas-chapel__x1.mp4", BLENDER),
     ("xmas-mews", "take 1", VIDEOS / "xmas-mews__x1.mp4", BLENDER),
     ("xmas-drive-candy", "take 1", SHOW / "xmas-drive-candy__x1-trim.mp4", BLENDER),
     ("xmas-drive-icicle", "take 1", SHOW / "xmas-drive-icicle__x1-trim.mp4", BLENDER),
     ("xmas-drive-victorian", "take 1", SHOW / "xmas-drive-victorian__x1e.mp4", RELIT),
     ("xmas-drive-reindeer", "take 1", SHOW / "xmas-drive-reindeer__x1e.mp4", RELIT),
     ("xmas-drive-lake", "take 1", SHOW / "xmas-drive-lake__x1e.mp4", RELIT),
     ("xmas-alpine-a", "take 1", VIDEOS / "xmas-alpine-a__x1.mp4", BLENDER),
     ("xmas-alpine-b", "take 1", VIDEOS / "xmas-alpine-b__x1.mp4", BLENDER),
     ("xmas-alpine-c", "take 1", VIDEOS / "xmas-alpine-c__x2.mp4", BLENDER),
     ("xmas-alpine-d", "take 1", VIDEOS / "xmas-alpine-d__x2.mp4", BLENDER),
     *[(scene, "take 2", VIDEOS / f"{scene}__y1.mp4", BLENDER)
       for scene in ("xmas-lane", "xmas-market", "xmas-bridge", "xmas-bakery", "xmas-chapel", "xmas-mews",
                     "xmas-drive-candy", "xmas-drive-icicle", "xmas-drive-reindeer",
                     "xmas-alpine-a", "xmas-alpine-b", "xmas-alpine-c", "xmas-alpine-d")],
     ("xmas-drive-victorian", "take 2", VIDEOS / "xmas-drive-victorian__y1m.mp4", RELIT + ", mid frame pinned"),
     ("xmas-drive-lake", "take 2", VIDEOS / "xmas-drive-lake__y2m.mp4", RELIT + ", mid frame pinned")])

KEEP["silhouette-truck"] = (
    "Silhouette truck", "Stills only, as in the Lab: two scenes, four look stills each.",
    [(scene, f"still {i}", TRUCKS / scene / "hero" / f"h1-{scene}_seed23_0000{i}_.png",
      "Blender blockout: depth pass, Z-Image look still")
     for scene in ("silhouette-dusk", "silhouette-wheat") for i in (1, 2, 3, 4)])

KEEP["other-formats"] = (
    "Other formats (earlier local tests, not the Blender route)",
    "Kept for reference: coffee needs the cup photo pinned; dirt bike in the Lab is recut real footage.",
    [("coffee-tok", "Wan i2v, cup pinned", SHOW / "formats" / "coffee_wan_positive_prompt_OK.mp4", "Wan 2.2 image-to-video"),
     ("pov-dirt-bike", "cleaned", SHOW / "array_dirtbike_cleaned.mp4", "local remake wall"),
     ("christmas-pov", "LTX from prompt", SHOW / "formats" / "christmas_pov_ltx_from_prompt.mp4", "LTX 2.3 from the prompt alone")])


def copy(src: Path, dest: Path) -> bool:
    if not src.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists() or src.stat().st_mtime > dest.stat().st_mtime:
        shutil.copy2(src, dest)
    return True


def failed_checks() -> dict[str, list[str]]:
    """Checks each rendered clip last failed, keyed by its file name in videos/."""
    found: dict[str, list[str]] = {}
    for line in (TRUCKS / "results.jsonl").read_text().splitlines():
        row = json.loads(line)
        if row.get("video"):
            found[row["video"]] = [k for k, ok in (row.get("checks") or {}).items() if not ok]
    return found


def main() -> None:
    failed = failed_checks()
    manifest, sections, missing = [], [], []
    hd = json.loads((TRUCKS / "post-hd" / "manifest.json").read_text())
    slices = {e["scene"]: [s["file"] for s in e["slices"] if ".fault" not in s["file"]] for e in hd}
    for fmt, (title, note, items) in KEEP.items():
        cells = []
        for scene, take, src, route in items:
            safe = f"{scene.split(' ')[0]}__{take.replace(' ', '-').replace(',', '')}{src.suffix}"
            if not copy(src, LIBRARY / fmt / safe):
                missing.append(str(src))
                continue
            entry = {"format": fmt, "scene": scene, "take": take, "file": f"{fmt}/{safe}", "source": str(src),
                     "route": route, "failed_checks": failed.get(src.name)}
            if fmt == "truck-scenic":
                kept = []
                for name in slices.get(scene, []):
                    if copy(TRUCKS / "post-hd" / scene / name, LIBRARY / fmt / "slices" / f"{scene}_{name}"):
                        kept.append(f"{fmt}/slices/{scene}_{name}")
                entry["slices"] = kept
            manifest.append(entry)
            label = f"{html.escape(scene)} <span>{html.escape(take)}</span>"
            if entry.get("slices") is not None:
                label += f" <span>{len(entry['slices'])}/5 slices</span>"
            if entry["failed_checks"]:
                label += f" <span class=bad>failed: {html.escape(', '.join(entry['failed_checks']))}</span>"
            media = (f'<img loading=lazy src="{entry["file"]}">' if src.suffix == ".png" else
                     f'<video controls muted loop playsinline preload=metadata src="{entry["file"]}"></video>')
            cells.append(f"<figure>{media}<figcaption>{label}</figcaption></figure>")
        wide = fmt in ("truck-scenic", "boat-lake")
        sections.append(f'<section><h2>{html.escape(title)} <small>{len(cells)}</small></h2><p>{html.escape(note)}</p>'
                        f'<div class="grid {"wide" if wide else "tall"}">{"".join(cells)}</div></section>')
    (LIBRARY / "manifest.json").write_text(json.dumps(manifest, indent=1))
    (LIBRARY / "index.html").write_text(PAGE.replace("@@", "".join(sections)))
    print(f"{len(manifest)} items saved under {LIBRARY}")
    for fmt in KEEP:
        print(f"  {fmt}: {sum(1 for e in manifest if e['format'] == fmt)}")
    if missing:
        print("not there yet (skipped):", *missing, sep="\n  ")


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Local render library</title>
<style>
:root{--bg:#f6f5f2;--fg:#1c1b19;--dim:#6b6860;--card:#fff;--line:#dedbd3;--bad:#a8321e}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#eceae4;--dim:#9a968c;--card:#1f1f1c;--line:#33322e;--bad:#ef8a73}}
body{margin:0;padding:24px 16px 64px;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,system-ui,sans-serif}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:36px 0 2px}h2 small{color:var(--dim);font-weight:400}
p{margin:0 0 12px;color:var(--dim);max-width:70ch}
.grid{display:grid;gap:12px}.wide{grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
.tall{grid-template-columns:repeat(auto-fill,minmax(170px,1fr))}
figure{margin:0;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
video,img{display:block;width:100%;height:auto;background:#000}
figcaption{padding:6px 8px;font-size:12.5px}figcaption span{color:var(--dim);margin-left:6px}
figcaption span.bad{color:var(--bad)}
</style>
<h1>Local render library</h1>
<p>Clips kept from the studio Mac's local renders, by Lab content format. Files and manifest.json are in this folder.</p>
@@
"""

if __name__ == "__main__":
    main()
