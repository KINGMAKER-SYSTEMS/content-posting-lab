#!/usr/bin/env python3
"""Import Slingshot TOS captions into a posting-lab project's caption bank.

Pulls per-post text-on-screen captions from the Slingshot TOS database
(Supabase tos_captions table) and writes them as caption source CSVs under
projects/<project>/captions/tos-<song>/captions.csv, using the canonical
caption-row primitive so the burn UI can sort/filter by creator, campaign
sound, or sentiment (mood):

    caption, video_id, video_url, creator, sound_id, song, mood, views

Mood is classified with the lab's existing scraper.sentiment_analyzer
(reuses OPENAI_API_KEY from .env). Pass --no-mood to skip it.

Usage:
    SUPABASE_URL=https://imxflhdsfxkfmvsufwvr.supabase.co \
    SUPABASE_KEY=sb_publishable_... \
    python3 import_tos_captions.py --project my-project --all
    python3 import_tos_captions.py --project my-project --sound-id 7649157011083134977
    python3 import_tos_captions.py --project my-project --campaign summer_davis_new_guy
"""
import argparse
import asyncio
import csv
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

import project_manager

load_dotenv()

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = (os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY") or "").strip()
TABLE = "tos_captions"


def _sb_get(params: dict) -> list[dict]:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{TABLE}",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
        params=params,
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def _video_id(url: str) -> str:
    m = re.search(r"/video/(\d+)", url or "")
    return m.group(1) if m else ""


def _sanitize_label(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "tos"


def fetch_rows(sound_id: str | None, campaign: str | None) -> list[dict]:
    params = {
        "select": "text_raw,username,video_url,views,sound_id,song_title,campaign_slug",
        "order": "views.desc",
    }
    if sound_id:
        params["sound_id"] = f"eq.{sound_id}"
    if campaign:
        params["campaign_slug"] = f"eq.{campaign}"
    return [r for r in _sb_get(params) if (r.get("text_raw") or "").strip()]


def classify_moods(texts: list[str]) -> dict[str, str]:
    """Classify distinct caption texts into mood tags; returns {text: mood}."""
    from scraper.sentiment_analyzer import analyze_moods_batch

    distinct = sorted({t for t in texts if t.strip()})
    if not distinct:
        return {}
    moods = asyncio.run(analyze_moods_batch(distinct))
    return dict(zip(distinct, moods))


def write_source(project: str, label: str, rows: list[dict], moods: dict[str, str]) -> Path:
    caption_dir = project_manager.get_project_caption_dir(project)
    src_dir = caption_dir / f"tos-{_sanitize_label(label)}"
    src_dir.mkdir(parents=True, exist_ok=True)
    csv_path = src_dir / "captions.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["video_id", "video_url", "caption", "creator", "sound_id", "song", "mood", "views"],
        )
        w.writeheader()
        for row in rows:
            text = (row.get("text_raw") or "").strip()
            if not text:
                continue
            w.writerow({
                "video_id": _video_id(row.get("video_url")),
                "video_url": row.get("video_url") or "",
                "caption": text,
                "creator": (row.get("username") or "").lstrip("@"),
                "sound_id": row.get("sound_id") or "",
                "song": row.get("song_title") or "",
                "mood": moods.get(text, ""),
                "views": int(row.get("views") or 0),
            })
    return csv_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--sound-id")
    ap.add_argument("--campaign")
    ap.add_argument("--all", action="store_true", help="import every sound as its own source")
    ap.add_argument("--no-mood", action="store_true", help="skip mood classification")
    args = ap.parse_args()

    if not (SUPABASE_URL and SUPABASE_KEY):
        print("SUPABASE_URL and SUPABASE_KEY (or SUPABASE_SERVICE_KEY) are required", file=sys.stderr)
        return 1
    if not (args.sound_id or args.campaign or args.all):
        print("pass one of --sound-id, --campaign, or --all", file=sys.stderr)
        return 1

    rows = fetch_rows(args.sound_id, args.campaign)
    if not rows:
        print("[warn] no captions found for that filter", file=sys.stderr)
        return 0

    moods: dict[str, str] = {}
    if not args.no_mood:
        print(f"[info] classifying mood for {len(rows)} captions...")
        try:
            moods = classify_moods([r.get("text_raw") or "" for r in rows])
        except Exception as e:
            print(f"[warn] mood classification failed ({e}); writing without mood", file=sys.stderr)

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r.get("campaign_slug") or r.get("sound_id") or "tos", []).append(r)

    for label, grp in groups.items():
        grp.sort(key=lambda r: int(r.get("views") or 0), reverse=True)
        path = write_source(args.project, label, grp, moods)
        song = grp[0].get("song_title") or label
        print(f"[done] {song}: {len(grp)} captions -> {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
