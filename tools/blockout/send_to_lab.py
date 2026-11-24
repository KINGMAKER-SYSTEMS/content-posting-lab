"""Send finished 9:16 clips from the drive library into a Content Lab project, through the Lab's own clipper routes.

    python tools/blockout/send_to_lab.py --format truck-scenic --project trucks-local [--lab http://127.0.0.1:8000]
        [--scene lakeside ...] [--limit N] [--dry-run] [--remote-ok]

Why this route (2026-11-23 survey of the Lab's code): no Lab route writes a pre-made file into a
project's videos/ folder except the source-library commission, which is hard-wired to the dirt-bike
pipeline and needs a deploy per batch. The clipper's two routes need no new Lab code:
POST /api/clipper/upload-batch stages files, POST /api/clipper/trim-batch with each file's full
duration writes projects/<project>/clips/<job>/clip_NNN.mp4, and that is what the poster content
scan (services/poster_content.py, via the burn scan) offers each page whose roster project matches.

What goes up: for truck-scenic, the postable 9:16 slices in library/truck-scenic/slices/ (".fault"
slices are never in the library); for pov-seasonal, the portrait clips. Clips the library marks as
having failed a Lab check are skipped. A ledger next to the library (lab-sent.jsonl) records each
clip's sha256, the Lab URL and project, so a clip is never sent twice to the same Lab and project.

The Lab address defaults to a local copy. Anything else is the live Lab and is refused unless
--remote-ok is passed; the API key is read from the LAB_API_KEY environment variable, never typed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

LIBRARY = Path("/Volumes/OWC-Envoy-Ultra/content-posting-lab/library")
LEDGER = LIBRARY / "lab-sent.jsonl"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "lab.test"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def candidates(fmt: str, scenes: list[str]) -> list[Path]:
    """Postable 9:16 files for a format, from the library manifest."""
    manifest = json.loads((LIBRARY / "manifest.json").read_text())
    picked: list[Path] = []
    for entry in manifest:
        if entry["format"] != fmt or (scenes and entry["scene"] not in scenes):
            continue
        if entry.get("failed_checks"):
            continue                                   # failed a Lab check when it was scored
        if entry.get("delivered"):
            if entry.get("approved"):
                picked.append(LIBRARY / entry["file"])     # already 9:16 at 1080x1920
            continue                                       # an unapproved draft never goes to the Lab
        elif fmt == "truck-scenic":
            picked += [LIBRARY / s for s in entry.get("slices", [])]
        elif entry["file"].endswith(".mp4"):
            picked.append(LIBRARY / entry["file"])
    return picked


def already_sent(lab: str, project: str) -> set[str]:
    if not LEDGER.exists():
        return set()
    rows = [json.loads(line) for line in LEDGER.read_text().splitlines() if line.strip()]
    return {r["sha256"] for r in rows if r["lab"] == lab and r["project"] == project and r.get("ok")}


def send(client: httpx.Client, lab: str, project: str, files: list[Path], batch_size: int = 10) -> list[dict]:
    """Upload and keep each file whole. Returns one ledger row per file."""
    rows: list[dict] = []
    for start in range(0, len(files), batch_size):
        chunk = files[start:start + batch_size]
        handles = [("files", (f.name, f.open("rb"), "video/mp4")) for f in chunk]
        try:
            resp = client.post("/api/clipper/upload-batch", data={"project": project}, files=handles)
        finally:
            for _, (_, fh, _) in handles:
                fh.close()
        resp.raise_for_status()
        staged = resp.json()
        if staged.get("errors"):
            raise RuntimeError(f"upload refused some files: {staged['errors']}")
        trims = [{"path": item["path"], "start": 0.0, "end": duration(src), "original_name": src.name}
                 for item, src in zip(staged["files"], chunk)]
        resp = client.post("/api/clipper/trim-batch", json={"project": project, "batch_id": staged["batch_id"],
                                                             "trims": trims})
        resp.raise_for_status()
        result = resp.json()
        for src, clip in zip(chunk, result["clips"]):
            rows.append({"source": str(src), "sha256": sha256(src), "lab": lab, "project": project,
                         "job_id": result["job_id"], "url": clip.get("url"), "ok": bool(clip.get("ok")),
                         "error": clip.get("error")})
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--format", required=True, choices=["truck-scenic", "pov-seasonal", "boat-lake"])
    parser.add_argument("--project", required=True)
    parser.add_argument("--lab", default="http://127.0.0.1:8000")
    parser.add_argument("--scene", nargs="*", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--remote-ok", action="store_true", help="allow a Lab that is not on this machine")
    args = parser.parse_args()

    host = urlparse(args.lab).hostname or ""
    if host not in LOCAL_HOSTS and not args.remote_ok:
        sys.exit(f"{args.lab} is not a local Lab; sending to the live Lab needs --remote-ok and the user's OK")
    sent = already_sent(args.lab, args.project)
    files = [f for f in candidates(args.format, args.scene) if sha256(f) not in sent]
    if args.limit:
        files = files[:args.limit]
    print(f"{len(files)} clips to send to {args.lab} project {args.project} ({len(sent)} already there)")
    if args.dry_run or not files:
        for f in files:
            print("  ", f.relative_to(LIBRARY))
        return
    headers = {"x-api-key": os.environ["LAB_API_KEY"]} if os.environ.get("LAB_API_KEY") else {}
    with httpx.Client(base_url=args.lab, headers=headers, timeout=600) as client:
        rows = send(client, args.lab, args.project, files)
    with LEDGER.open("a") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    print(f"{sum(r['ok'] for r in rows)}/{len(rows)} clips now in {args.project}")


if __name__ == "__main__":
    main()
