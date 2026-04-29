"""
Page roster router.
Manages assignment of Postiz integrations (pages) to projects,
and linking of Google Drive folders to pages.
"""

import os
import re

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.roster import (
    get_page,
    list_all_pages,
    list_pages_for_project,
    load_roster,
    parse_drive_folder_id,
    remove_page,
    save_roster,
    set_page,
)

# Regex to strip emoji and other non-alphanumeric/space characters for dedup matching
_EMOJI_RE = re.compile(
    r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    r"\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U000024C2-\U0001F251"
    r"\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
    r"\U00002600-\U000026FF\U0000FE0F\U0000200D]+",
    re.UNICODE,
)


def _normalize_name(name: str) -> str:
    """Normalize a page name for dedup: lowercase, strip emoji, collapse whitespace."""
    name = _EMOJI_RE.sub("", name)
    return re.sub(r"\s+", " ", name).lower().strip()

router = APIRouter()


# ── Roster CRUD ────────────────────────────────────────────────────────────


class UpdatePageRequest(BaseModel):
    project: str | None = None
    drive_folder_url: str | None = None


@router.get("/")
async def list_roster():
    """List all pages in the roster."""
    return {"pages": list_all_pages()}


@router.get("/project/{project_name}")
async def get_project_pages(project_name: str):
    """List pages assigned to a specific project, enriched with staging topic status."""
    from services.telegram import get_staging_group

    pages = list_pages_for_project(project_name)
    staging = get_staging_group()
    topics = staging.get("topics", {}) if staging else {}

    enriched = []
    for page in pages:
        iid = page.get("integration_id", "")
        topic_info = topics.get(iid)
        enriched.append({
            **page,
            "has_staging_topic": bool(topic_info and topic_info.get("topic_id")),
            "staging_topic_name": topic_info.get("topic_name") if topic_info else None,
        })

    return {"pages": enriched}


@router.put("/{integration_id}")
async def update_page(integration_id: str, req: UpdatePageRequest):
    """Assign or update a page in the roster."""
    data: dict = {}
    if req.project is not None:
        data["project"] = req.project
    if req.drive_folder_url is not None:
        data["drive_folder_url"] = req.drive_folder_url
        folder_id = parse_drive_folder_id(req.drive_folder_url)
        if req.drive_folder_url and not folder_id:
            raise HTTPException(
                status_code=400,
                detail="Could not parse Google Drive folder ID from URL",
            )
        data["drive_folder_id"] = folder_id
    entry = set_page(integration_id, data)
    return {"page": entry}


@router.delete("/{integration_id}")
async def delete_page(integration_id: str):
    """Remove a page from the roster."""
    removed = remove_page(integration_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Page not found in roster")
    return {"deleted": True}


# ── Dedup ────────────────────────────────────────────────────────────────


@router.get("/duplicates")
async def find_duplicates():
    """Audit roster for duplicate page names.

    For each duplicate set, shows which integration_ids have:
    - staging topics (with topic_id)
    - inventory items (staged/pending/forwarded)
    So the user can decide which to keep.
    """
    from collections import defaultdict

    roster = load_roster()
    pages = roster["pages"]

    # Get staging topics and inventory data
    try:
        from services.telegram import get_staging_group, get_inventory
        staging = get_staging_group()
        staging_topics = staging.get("topics", {})
    except Exception:
        staging_topics = {}

    try:
        from services.telegram import load_config as load_tg_config
        tg_config = load_tg_config()
        all_inventory = tg_config.get("inventory", {})
    except Exception:
        all_inventory = {}

    # Group by normalized name (strips emoji, lowercases, collapses whitespace)
    by_name: dict[str, list[str]] = defaultdict(list)
    for ig_id, page in pages.items():
        key = _normalize_name(page.get("name") or ig_id)
        by_name[key].append(ig_id)

    duplicates = []
    for name, ids in sorted(by_name.items()):
        if len(ids) <= 1:
            continue

        entries = []
        for ig_id in ids:
            page = pages[ig_id]
            topic = staging_topics.get(ig_id, {})
            inv = all_inventory.get(ig_id, [])
            inv_pending = sum(1 for i in inv if not i.get("forwarded"))
            inv_forwarded = sum(1 for i in inv if i.get("forwarded"))

            entries.append({
                "integration_id": ig_id,
                "name": page.get("name", ""),
                "provider": page.get("provider", ""),
                "has_topic": bool(topic.get("topic_id")),
                "topic_id": topic.get("topic_id"),
                "topic_name": topic.get("topic_name", ""),
                "inventory_total": len(inv),
                "inventory_pending": inv_pending,
                "inventory_forwarded": inv_forwarded,
                "has_project": bool(page.get("project")),
                "has_drive": bool(page.get("drive_folder_id")),
            })

        duplicates.append({"name": name, "count": len(ids), "entries": entries})

    return {
        "total_pages": len(pages),
        "duplicate_names": len(duplicates),
        "duplicates": duplicates,
    }


@router.post("/dedup")
async def dedup_roster():
    """Remove duplicate roster entries that share the same name.

    For each duplicate set, keeps the entry with the most inventory/data.
    Merges inventory from removed entries into the kept one.
    Also cleans up duplicate staging topics.
    """
    from collections import defaultdict

    roster = load_roster()
    pages = roster["pages"]

    try:
        from services.telegram import (
            get_staging_group,
            remove_staging_topic,
            load_config as load_tg_config,
            save_config as save_tg_config,
        )
        staging = get_staging_group()
        staging_topics = staging.get("topics", {})
        tg_config = load_tg_config()
        all_inventory = tg_config.get("inventory", {})
    except Exception:
        staging_topics = {}
        tg_config = None
        all_inventory = {}

    by_name: dict[str, list[str]] = defaultdict(list)
    for ig_id, page in pages.items():
        key = _normalize_name(page.get("name") or ig_id)
        by_name[key].append(ig_id)

    removed_ids: list[str] = []
    removed_names: list[str] = []
    inventory_merged = 0

    for name, ids in by_name.items():
        if len(ids) <= 1:
            continue

        # Score: prefer entries with inventory > topic > project > drive
        def score(ig_id: str) -> int:
            s = 0
            inv = all_inventory.get(ig_id, [])
            s += len(inv) * 100  # inventory items matter most
            if ig_id in staging_topics:
                s += 50
            p = pages[ig_id]
            if p.get("project"):
                s += 10
            if p.get("drive_folder_id"):
                s += 5
            return s

        ids_sorted = sorted(ids, key=score, reverse=True)
        keep_id = ids_sorted[0]

        for dupe_id in ids_sorted[1:]:
            # Merge inventory from dupe into keeper
            dupe_inv = all_inventory.get(dupe_id, [])
            if dupe_inv and tg_config is not None:
                keep_inv = tg_config.get("inventory", {}).setdefault(keep_id, [])
                keep_inv.extend(dupe_inv)
                inventory_merged += len(dupe_inv)
                # Remove dupe inventory
                if dupe_id in tg_config.get("inventory", {}):
                    del tg_config["inventory"][dupe_id]

            removed_ids.append(dupe_id)
            removed_names.append(pages[dupe_id].get("name", dupe_id))
            remove_page(dupe_id)

    # Save merged inventory
    if tg_config is not None and inventory_merged > 0:
        save_tg_config(tg_config)

    # Clean up staging topics for removed IDs
    topic_removed = 0
    for rid in removed_ids:
        if rid in staging_topics:
            try:
                remove_staging_topic(rid)
                topic_removed += 1
            except Exception:
                pass

    return {
        "removed": len(removed_ids),
        "removed_names": removed_names,
        "inventory_merged": inventory_merged,
        "topics_cleaned": topic_removed,
        "remaining": len(list_all_pages()),
    }


# ── Sync with Postiz ──────────────────────────────────────────────────────


@router.get("/sync-notion/status")
async def notion_sync_status():
    """Check if Notion Master Pages sync is configured."""
    from services.notion_pages import is_configured
    return {"configured": is_configured()}


@router.post("/sync-notion")
async def sync_from_notion():
    """Pull account roster from Notion Master Pages DB.

    Notion is the source of truth for: username, signup email, password,
    forwarding address, poster name, group, account type, TikTok URL.
    Roster JSON keeps app-only fields (project, drive folder, CF email alias).
    """
    from services.notion_pages import is_configured, sync_into_roster

    if not is_configured():
        raise HTTPException(
            status_code=503,
            detail="Notion not configured — set NOTION_API_KEY and NOTION_PAGES_DB",
        )

    try:
        return await sync_into_roster()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"Notion API error: {exc.response.text[:500]}",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Notion sync failed: {exc}")


@router.post("/sync")
async def sync_integrations():
    """[DEPRECATED] Postiz sync — kept as alias to /sync-notion for backwards compat.

    Notion is now the source of truth. The legacy 'Sync from Postiz' button
    transparently routes here, which redirects to the Notion sync.
    """
    from services.notion_pages import is_configured, sync_into_roster

    if not is_configured():
        raise HTTPException(
            status_code=503,
            detail="Notion not configured — set NOTION_API_KEY and NOTION_PAGES_DB",
        )

    try:
        result = await sync_into_roster()
        # Match legacy response shape so old callers don't break
        return {
            "added": result.get("added", 0),
            "removed": 0,  # Notion-driven — we don't auto-remove anything
            "updated": result.get("updated", 0),
            "pages": result.get("pages", []),
        }
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"Notion API error: {exc.response.text[:500]}",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Notion sync failed: {exc}")
