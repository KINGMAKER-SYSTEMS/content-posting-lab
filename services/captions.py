"""Shared caption-loading service.

Used by both the burn router and the slideshow router to access
project caption banks (scraped TikTok caption CSVs).
"""

import csv
from pathlib import Path

import project_manager
from project_manager import get_project_caption_dir


def load_captions(csv_path: Path) -> list[dict]:
    """Load captions from a CSV file.

    Returns list of dicts with keys: text, video_id, video_url, mood.
    Filters out rows with empty caption text.
    """
    captions = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            text = (row.get("caption") or "").strip()
            if text:
                captions.append(
                    {
                        "text": text,
                        "video_id": row.get("video_id", ""),
                        "video_url": row.get("video_url", ""),
                        "mood": row.get("mood", ""),
                    }
                )
    return captions


def scan_project_captions(project: str) -> list[dict]:
    """Find all caption CSVs in projects/{name}/captions/.

    Returns list of caption sources, each with:
    - username: folder name
    - csv_path: relative path to CSV
    - count: number of captions
    - captions: list of caption dicts
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
