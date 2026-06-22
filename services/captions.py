"""Shared caption-loading service.

Used by both the burn router and the slideshow router to access
project caption banks (scraped TikTok caption CSVs).
"""

import csv
from pathlib import Path

import project_manager
from project_manager import get_project_caption_dir


# Canonical caption-row primitive. Every caption — scraped or TOS-imported —
# is one row described by these dimensions, so the burn bank can sort/filter by
# creator, campaign sound, or sentiment (mood) without relying on folder names.
CAPTION_FIELDS = ("text", "video_id", "video_url", "creator", "sound_id", "song", "mood", "views")


def load_captions(csv_path: Path) -> list[dict]:
    """Load captions from a CSV file as the canonical caption-row primitive.

    Returns dicts with keys: text, video_id, video_url, creator, sound_id,
    song, mood, views. Older CSVs that only have text/video_id/video_url/mood
    still load — the extra columns default to empty/0. Filters empty text.
    """
    captions = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            text = (row.get("caption") or "").strip()
            if not text:
                continue
            try:
                views = int(row.get("views") or 0)
            except (TypeError, ValueError):
                views = 0
            captions.append(
                {
                    "text": text,
                    "video_id": row.get("video_id", ""),
                    "video_url": row.get("video_url", ""),
                    "creator": (row.get("creator") or "").lstrip("@"),
                    "sound_id": row.get("sound_id", ""),
                    "song": row.get("song", ""),
                    "mood": row.get("mood", ""),
                    "views": views,
                }
            )
    return captions


def scan_project_captions(project: str) -> list[dict]:
    """Find all caption CSVs in projects/{name}/captions/.

    Returns list of caption sources, each with:
    - username: folder name
    - csv_path: relative path to CSV
    - count: number of captions
    - captions: list of caption dicts (canonical primitive)
    """
    sources = []
    caption_dir = get_project_caption_dir(project)
    if not caption_dir.exists():
        return sources
    for user_dir in sorted(caption_dir.iterdir()):
        if not user_dir.is_dir() or user_dir.name.startswith("."):
            continue
        csv_path = user_dir / "captions.csv"
        if csv_path.exists():
            caps = load_captions(csv_path)
            sources.append(
                {
                    "username": user_dir.name,
                    "csv_path": str(csv_path.relative_to(project_manager.BASE_DIR)),
                    "count": len(caps),
                    "captions": caps,
                }
            )
    return sources


def caption_facets(sources: list[dict]) -> dict:
    """Distinct filter values across all caption sources in a project.

    Lets the burn UI offer sort/filter by creator, campaign sound, or mood.
    Returns {creators: [...], sounds: [{sound_id, song}], moods: [...]}.
    """
    creators: set[str] = set()
    sounds: dict[str, str] = {}
    moods: set[str] = set()
    for src in sources:
        for c in src.get("captions", []):
            if c.get("creator"):
                creators.add(c["creator"])
            if c.get("sound_id"):
                sounds.setdefault(c["sound_id"], c.get("song", ""))
            if c.get("mood"):
                moods.add(c["mood"])
    return {
        "creators": sorted(creators),
        "sounds": [{"sound_id": sid, "song": song} for sid, song in sorted(sounds.items(), key=lambda kv: kv[1])],
        "moods": sorted(moods),
    }
