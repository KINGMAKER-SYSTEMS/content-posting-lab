"""Measure Studio vision agreement on known-answer frames before it may decide.

Local vision output stays a hint (providers/backend_order.json,
``vision.studio.authority: hint``) until this run passes; its result's
``agreement`` object is what the order file records to allow ``verdict``.

Known answers are built from clips: every sampled frame as-is is ``clean`` (use
clips that carry no added caption or watermark), and the same frame with a
caption burned in is ``text``. With ``--reference`` the current hosted model
(GLM, CONTENT_LAB_VISION_API_KEY) answers the same frames, so the report also
gives Studio-vs-current agreement.

A shared machine comes first. ``run`` refuses to start, and stops early, when
the 1-minute load average is at or above ``--max-load``, when the model is not
already loaded (this script never loads a model), or when a ComfyUI render is
running. Requests go strictly one at a time.

    python scripts/vision_agreement.py build --clips a.mp4 b.mp4 --per-clip 25 --out DIR
    python scripts/vision_agreement.py run --manifest DIR/manifest.jsonl --out DIR/result.json
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import visual_admission as gate  # noqa: E402

DEFAULT_URL = "http://127.0.0.1:11434/v1/chat/completions"
DEFAULT_MODEL = "mimo-v2.6:9b-nvfp4"
DEFAULT_COMFYUI = "http://127.0.0.1:8188"
CAPTIONS = ("wait for the drop", "POV: you finally found it", "day 3 of posting this",
            "nobody talks about this", "save this for later")
_RECHECK_EVERY = 10


def _duration(clip: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(clip)], capture_output=True, text=True, check=True, timeout=60)
    return float(out.stdout.strip())


def _frame(clip: Path, at: float) -> Image.Image:
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{at:.3f}", "-i", str(clip),
                          "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                         capture_output=True, check=True, timeout=120)
    return Image.open(io.BytesIO(out.stdout)).convert("RGB")


def _font(size: int) -> ImageFont.ImageFont:
    for path in sorted((Path(__file__).resolve().parent.parent / "fonts").rglob("*.ttf")):
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _with_caption(image: Image.Image, text: str) -> Image.Image:
    captioned = image.copy()
    draw = ImageDraw.Draw(captioned)
    size = max(18, captioned.width // 14)
    font = _font(size)
    box = draw.textbbox((0, 0), text, font=font, stroke_width=max(2, size // 12))
    x = (captioned.width - (box[2] - box[0])) // 2
    y = int(captioned.height * 0.62)
    draw.text((x, y), text, font=font, fill="white", stroke_width=max(2, size // 12), stroke_fill="black")
    return captioned


def build(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for clip_number, clip in enumerate(map(Path, args.clips)):
        duration = _duration(clip)
        for i in range(args.per_clip):
            at = duration * (i + 0.5) / args.per_clip
            base = _frame(clip, at)
            stem = f"c{clip_number:02d}f{i:03d}"
            base.save(out / f"{stem}-clean.jpg", quality=gate.VISION_JPEG_QUALITY)
            caption = CAPTIONS[(clip_number + i) % len(CAPTIONS)]
            _with_caption(base, caption).save(out / f"{stem}-text.jpg", quality=gate.VISION_JPEG_QUALITY)
            rows.append({"path": f"{stem}-clean.jpg", "answer": "clean", "clip": clip.name, "at": round(at, 3)})
            rows.append({"path": f"{stem}-text.jpg", "answer": "text", "clip": clip.name, "at": round(at, 3),
                         "caption": caption})
    (out / "manifest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"{len(rows)} known-answer frames -> {out / 'manifest.jsonl'}")
    return 0


def preflight(url: str, model: str, max_load: float, comfyui: str) -> str | None:
    """Why the shared machine cannot take this run now, or None."""
    load1 = os.getloadavg()[0]
    if load1 >= max_load:
        return f"load1 {load1:.1f} >= {max_load:g}"
    if not gate._studio_resident(url, model, time.monotonic() + 5):
        return f"{model} is not loaded (this script never loads a model)"
    try:
        body = httpx.get(f"{comfyui.rstrip('/')}/queue", timeout=5).json()
    except (httpx.HTTPError, ValueError):
        return None  # no ComfyUI here: nothing to collide with
    if body.get("queue_running") or body.get("queue_pending"):
        return "a ComfyUI render is running"
    return None


def _ask(image_path: Path, *, url: str, model: str, provider: str, key: str = "") -> str:
    sample = base64.b64encode(image_path.read_bytes()).decode()
    try:
        return gate._vision_request([sample], time.monotonic() + 120, url=url, model=model,
                                    provider=provider, key=key)["verdict"]
    except (httpx.HTTPError, RuntimeError) as error:
        return f"error:{type(error).__name__}"


def run(args) -> int:
    manifest = Path(args.manifest)
    rows = [json.loads(line) for line in manifest.read_text().splitlines() if line.strip()]
    reason = preflight(args.url, args.model, args.max_load, args.comfyui)
    if reason:
        print(f"REFUSED: {reason}")
        return 3
    key = os.environ.get("CONTENT_LAB_VISION_API_KEY", "").strip() if args.reference else ""
    if args.reference and not key:
        print("note: --reference needs CONTENT_LAB_VISION_API_KEY; comparing with known answers only")
    results, stopped = [], None
    for number, row in enumerate(rows):
        if number and number % _RECHECK_EVERY == 0:
            stopped = preflight(args.url, args.model, args.max_load, args.comfyui)
            if stopped:
                break
        path = manifest.parent / row["path"]
        verdict = _ask(path, url=args.url, model=args.model, provider="studio")
        reference = _ask(path, url=gate.VISION_URL, model=gate.MODEL, provider=gate.VISION_PROVIDER,
                         key=key) if key else None
        results.append({**row, "studio": verdict, "reference": reference})
    answered = [r for r in results if not r["studio"].startswith("error:")]
    correct = sum(r["studio"] == r["answer"] for r in results)
    confusion: dict[str, dict[str, int]] = {}
    for r in results:
        confusion.setdefault(r["answer"], {}).setdefault(r["studio"], 0)
        confusion[r["answer"]][r["studio"]] += 1
    summary = {
        "model": args.model,
        "frames": len(results),
        "answered": len(answered),
        "rate": round(correct / len(results), 4) if results else None,
        "confusion": confusion,
        "stopped": stopped,
        "measuredAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with_reference = [r for r in results if r["reference"] is not None]
    if with_reference:
        summary["reference"] = {
            "model": gate.MODEL,
            "frames": len(with_reference),
            "accuracy": round(sum(r["reference"] == r["answer"] for r in with_reference) / len(with_reference), 4),
            "agreementWithStudio": round(sum(r["reference"] == r["studio"] for r in with_reference)
                                         / len(with_reference), 4),
        }
        # The gate figure is the weaker of "matches the answer" and "matches today's model".
        summary["rate"] = min(summary["rate"], summary["reference"]["agreementWithStudio"])
    out = Path(args.out)
    out.write_text(json.dumps({"agreement": summary, "frames": results}, indent=2) + "\n")
    print(json.dumps(summary))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="make known-answer frames from clips")
    b.add_argument("--clips", nargs="+", required=True)
    b.add_argument("--per-clip", type=int, default=25)
    b.add_argument("--out", required=True)
    r = sub.add_parser("run", help="ask the Studio model about every known-answer frame")
    r.add_argument("--manifest", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--url", default=DEFAULT_URL)
    r.add_argument("--model", default=DEFAULT_MODEL)
    r.add_argument("--max-load", type=float, default=12.0)
    r.add_argument("--comfyui", default=DEFAULT_COMFYUI)
    r.add_argument("--reference", action="store_true", help="also ask the current hosted model")
    args = parser.parse_args(argv)
    return build(args) if args.command == "build" else run(args)


if __name__ == "__main__":
    raise SystemExit(main())
