"""
Global caption bank router.
Manages a JSON-based caption bank with manual categories,
independent of any project. Supports CRUD for categories and
captions with mood/sentiment tags, plus importing from project-scoped scraped CSVs.
"""

import csv
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from project_manager import BASE_DIR, get_project_caption_dir

router = APIRouter()

BANK_FILE = BASE_DIR / "caption_bank.json"


# ── Models ──────────────────────────────────────────────────────────


class CreateCategoryBody(BaseModel):
    name: str


class RenameCategoryBody(BaseModel):
    name: str


class CaptionsBody(BaseModel):
    captions: list[str]


class CaptionWithMoodBody(BaseModel):
    captions: list[dict]  # [{"text": "...", "mood": "..."}]


class ImportBody(BaseModel):
    project: str
    username: str
    categoryId: str


class AnalyzeMoodsBody(BaseModel):
    categoryId: str


# ── Helpers ─────────────────────────────────────────────────────────


def _load_bank() -> dict[str, Any]:
    import json

    if BANK_FILE.exists():
        with open(BANK_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"categories": []}


def _save_bank(data: dict[str, Any]) -> None:
    import json

    with open(BANK_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _find_category(bank: dict, cat_id: str) -> dict | None:
    for cat in bank["categories"]:
        if cat["id"] == cat_id:
            return cat
    return None


def _normalize_caption(c) -> dict:
    """Ensure a caption entry is an object with text and mood fields.

    Handles backward compatibility: plain strings become {"text": str, "mood": null}.
    """
    if isinstance(c, str):
        return {"text": c, "mood": None}
    if isinstance(c, dict):
        return {"text": c.get("text", ""), "mood": c.get("mood")}
    return {"text": str(c), "mood": None}


def _normalize_captions(cat: dict) -> list[dict]:
    """Return all captions in a category as normalized objects."""
    return [_normalize_caption(c) for c in cat.get("captions", [])]


def _caption_text(c) -> str:
    """Extract text from a caption entry (string or object)."""
    if isinstance(c, str):
        return c
    if isinstance(c, dict):
        return c.get("text", "")
    return str(c)


# ── Endpoints ───────────────────────────────────────────────────────


@router.get("/")
async def list_categories(mood: str | None = Query(default=None)):
    bank = _load_bank()
    cats = []
    for c in bank["categories"]:
        captions = _normalize_captions(c)
        if mood:
            captions = [cap for cap in captions if cap.get("mood") == mood]
        cats.append(
            {
                "id": c["id"],
                "name": c["name"],
                "captions": captions,
                "count": len(captions),
            }
        )
    return {"categories": cats}


@router.get("/moods")
async def list_moods():
    """Return all mood tags currently in use across the caption bank."""
    from scraper.sentiment_analyzer import MOOD_TAGS

    bank = _load_bank()
    used: dict[str, int] = {}
    for cat in bank["categories"]:
        for c in _normalize_captions(cat):
            m = c.get("mood")
            if m:
                used[m] = used.get(m, 0) + 1
    return {"available": MOOD_TAGS, "used": used}


@router.post("/categories")
async def create_category(body: CreateCategoryBody):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Category name is required")
    bank = _load_bank()
    cat = {"id": uuid.uuid4().hex[:12], "name": name, "captions": []}
    bank["categories"].append(cat)
    _save_bank(bank)
    return {"category": {**cat, "count": 0}}


@router.put("/categories/{cat_id}")
async def rename_category(cat_id: str, body: RenameCategoryBody):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Category name is required")
    bank = _load_bank()
    cat = _find_category(bank, cat_id)
    if not cat:
        raise HTTPException(404, "Category not found")
    cat["name"] = name
    _save_bank(bank)
    return {"category": {**cat, "count": len(cat["captions"])}}


@router.delete("/categories/{cat_id}")
async def delete_category(cat_id: str):
    bank = _load_bank()
    before = len(bank["categories"])
    bank["categories"] = [c for c in bank["categories"] if c["id"] != cat_id]
    if len(bank["categories"]) == before:
        raise HTTPException(404, "Category not found")
    _save_bank(bank)
    return {"deleted": True}


@router.post("/categories/{cat_id}/captions")
async def add_captions(cat_id: str, body: CaptionsBody):
    bank = _load_bank()
    cat = _find_category(bank, cat_id)
    if not cat:
        raise HTTPException(404, "Category not found")
    new = [c.strip() for c in body.captions if c.strip()]
    existing = set(_caption_text(c) for c in cat["captions"])
    added = [{"text": c, "mood": None} for c in new if c not in existing]
    cat["captions"].extend(added)
    _save_bank(bank)
    return {"added": len(added), "total": len(cat["captions"])}


@router.delete("/categories/{cat_id}/captions")
async def remove_captions(cat_id: str, body: CaptionsBody):
    bank = _load_bank()
    cat = _find_category(bank, cat_id)
    if not cat:
        raise HTTPException(404, "Category not found")
    to_remove = set(c.strip() for c in body.captions)
    before = len(cat["captions"])
    cat["captions"] = [c for c in cat["captions"] if _caption_text(c) not in to_remove]
    _save_bank(bank)
    return {"removed": before - len(cat["captions"]), "total": len(cat["captions"])}


@router.post("/import")
async def import_from_scraped(body: ImportBody):
    bank = _load_bank()
    cat = _find_category(bank, body.categoryId)
    if not cat:
        raise HTTPException(404, "Category not found")

    caption_dir = get_project_caption_dir(body.project)
    csv_path = caption_dir / body.username / "captions.csv"
    if not csv_path.exists():
        raise HTTPException(404, f"No scraped captions found for @{body.username}")

    captions: list[dict] = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            text = (row.get("caption") or "").strip()
            if text:
                mood = (row.get("mood") or "").strip() or None
                captions.append({"text": text, "mood": mood})

    existing = set(_caption_text(c) for c in cat["captions"])
    added = [c for c in captions if c["text"] not in existing]
    cat["captions"].extend(added)
    _save_bank(bank)
    return {"added": len(added), "total": len(cat["captions"])}


@router.post("/analyze-moods")
async def analyze_category_moods(body: AnalyzeMoodsBody):
    """Run mood/sentiment analysis on all captions in a category that lack mood tags.

    This is useful for retroactively tagging existing captions.
    """
    from scraper.sentiment_analyzer import analyze_moods_batch

    bank = _load_bank()
    cat = _find_category(bank, body.categoryId)
    if not cat:
        raise HTTPException(404, "Category not found")

    # Normalize all captions to object format first
    cat["captions"] = _normalize_captions(cat)

    # Find captions missing mood tags
    to_analyze: list[tuple[int, str]] = []
    for i, c in enumerate(cat["captions"]):
        if not c.get("mood") and c.get("text", "").strip():
            to_analyze.append((i, c["text"]))

    if not to_analyze:
        _save_bank(bank)
        return {"analyzed": 0, "total": len(cat["captions"])}

    # Batch analyze in chunks of 20
    CHUNK = 20
    analyzed_count = 0
    for start in range(0, len(to_analyze), CHUNK):
        chunk = to_analyze[start : start + CHUNK]
        texts = [t for _, t in chunk]
        moods = await analyze_moods_batch(texts)
        for (idx, _), mood in zip(chunk, moods):
            cat["captions"][idx]["mood"] = mood
            analyzed_count += 1

    _save_bank(bank)
    return {"analyzed": analyzed_count, "total": len(cat["captions"])}
