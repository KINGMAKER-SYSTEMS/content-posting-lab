"""
Notion Master Pages sync — pulls account roster from the Notion DB.
Notion is the canonical source of truth for accounts: username, email, password,
forwarding address, poster assignment, group, account type.

Roster JSON is a local cache that mirrors Notion data plus app-only fields
(drive_folder_id, project assignment).

Env vars:
  NOTION_API_KEY     — same key used by services/notion.py
  NOTION_PAGES_DB    — Master Pages database ID (e.g. 3271465bb829805db21ed6656edcfada)
"""

import os
import re
from typing import Any

import httpx

from services.roster import (
    list_all_pages,
    load_roster,
    set_page,
)

NOTION_API_KEY = os.getenv("NOTION_API_KEY", "")
NOTION_PAGES_DB = os.getenv("NOTION_PAGES_DB", "")
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def is_configured() -> bool:
    return bool(NOTION_API_KEY) and bool(NOTION_PAGES_DB)


# ── Property extractors ──────────────────────────────────────────────────────


def _title(prop: dict) -> str:
    items = prop.get("title", []) if isinstance(prop, dict) else []
    return "".join(p.get("plain_text", "") for p in items).strip()


def _rich_text(prop: dict) -> str:
    items = prop.get("rich_text", []) if isinstance(prop, dict) else []
    return "".join(p.get("plain_text", "") for p in items).strip()


def _email(prop: dict) -> str:
    if not isinstance(prop, dict):
        return ""
    return (prop.get("email") or "").strip()


def _url(prop: dict) -> str:
    if not isinstance(prop, dict):
        return ""
    return (prop.get("url") or "").strip()


def _select(prop: dict) -> str:
    if not isinstance(prop, dict):
        return ""
    sel = prop.get("select")
    return sel.get("name", "") if isinstance(sel, dict) else ""


def _date(prop: dict) -> str:
    if not isinstance(prop, dict):
        return ""
    d = prop.get("date")
    if isinstance(d, dict):
        return d.get("start", "") or ""
    return ""


# ── ID minting ───────────────────────────────────────────────────────────────


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(s: str) -> str:
    s = s.lower().strip()
    s = _SLUG_RE.sub("-", s).strip("-")
    return s or "unknown"


def mint_integration_id(username: str) -> str:
    """Stable, deterministic ID for a Notion-sourced account."""
    return f"acct:{slugify(username)}"


# ── Parse a single Notion page ───────────────────────────────────────────────


def parse_page(notion_page: dict) -> dict[str, Any] | None:
    """Parse a Notion DB row into our roster format. Returns None if no username."""
    props = notion_page.get("properties", {}) or {}

    username = _title(props.get("Account Username", {}))
    if not username:
        return None

    # The DB has both "Group" and "Group " (with trailing space) — different cols
    group = _select(props.get("Group", {}))
    group_label = _select(props.get("Group ", {}))  # trailing space intentional

    return {
        "integration_id": mint_integration_id(username),
        "name": username,
        "provider": "tiktok",  # all rows in this DB are TikTok
        "tiktok_url": _url(props.get("Page URL", {})),
        "signup_email": _email(props.get("email", {})),
        "fwd_address": _rich_text(props.get("fwd address", {})),
        "password": _rich_text(props.get("Password", {})),
        "poster_name": _rich_text(props.get("Poster", {})),
        "group": group,                  # ATLANTIC / WARNER / INTERNAL
        "group_label": group_label,      # "Warner UGC", "Sam Barber (Atlantic)", etc.
        "account_type": _select(props.get("Account Type", {})),
        "notes": _rich_text(props.get("Notes", {})),
        "notion_page_id": notion_page.get("id", ""),
        "source": "notion",
        # Pipeline columns (added in plan §"Notion schema additions")
        "status": _select(props.get("Status", {})),
        "pipeline": _select(props.get("Pipeline", {})),
        "page_type": _select(props.get("Page Type", {})),
        "sounds_reference": _url(props.get("Sounds Reference", {})),
        "go_live_date": _date(props.get("Go-Live Date", {})),
        "drive_folder_url": _url(props.get("Drive Folder URL", {})),
    }


# ── Fetch all rows ───────────────────────────────────────────────────────────


async def fetch_all_pages() -> list[dict[str, Any]]:
    """Query the Notion Master Pages DB and return parsed rows."""
    if not is_configured():
        raise RuntimeError("NOTION_API_KEY and NOTION_PAGES_DB must be set")

    parsed: list[dict[str, Any]] = []
    has_more = True
    cursor: str | None = None

    async with httpx.AsyncClient(timeout=30) as client:
        while has_more:
            body: dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor

            resp = await client.post(
                f"{NOTION_API_BASE}/databases/{NOTION_PAGES_DB}/query",
                headers=_headers(),
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

            for page in data.get("results", []):
                row = parse_page(page)
                if row:
                    parsed.append(row)

            has_more = data.get("has_more", False)
            cursor = data.get("next_cursor")

    return parsed


# ── Sync into roster JSON ────────────────────────────────────────────────────


async def sync_into_roster() -> dict[str, Any]:
    """Pull Notion Master Pages and merge into local roster JSON.

    Strategy:
      - Notion is canonical for: username, email, password, fwd, poster_name,
        group, account_type, tiktok_url, notes, notion_page_id.
      - Roster JSON is canonical for: project, drive_folder_url/id, email_alias,
        email_rule_id, fwd_destination (CF Email Routing fields).
      - Existing roster pages keyed by Postiz integration_id are NOT touched —
        Notion-sourced rows use `acct:{username}` keys, so they coexist.

    Returns: {added, updated, total_in_notion, errors}
    """
    rows = await fetch_all_pages()

    roster = load_roster()
    existing_pages = roster["pages"]

    added = 0
    updated = 0
    errors: list[str] = []

    for row in rows:
        try:
            iid = row["integration_id"]
            existing = existing_pages.get(iid, {})

            # Merge: Notion fields overwrite, app-only fields preserved
            merged = {
                "name": row["name"],
                "provider": row["provider"],
                # Notion-canonical fields:
                "tiktok_url": row["tiktok_url"],
                "signup_email": row["signup_email"],
                "fwd_address": row["fwd_address"],
                "password": row["password"],
                "poster_name": row["poster_name"],
                "group": row["group"],
                "group_label": row["group_label"],
                "account_type": row["account_type"],
                "notes": row["notes"],
                "notion_page_id": row["notion_page_id"],
                "source": "notion",
                # Pipeline-canonical (Notion):
                "status": row.get("status"),
                "pipeline": row.get("pipeline"),
                "page_type": row.get("page_type"),
                "sounds_reference": row.get("sounds_reference"),
                "go_live_date": row.get("go_live_date"),
                # drive_folder_url: prefer Notion if set, else existing
                "drive_folder_url": row.get("drive_folder_url") or existing.get("drive_folder_url"),
                # App-only fields preserved from existing entry:
                "project": existing.get("project"),
                "drive_folder_id": existing.get("drive_folder_id"),
                "email_alias": existing.get("email_alias"),
                "email_rule_id": existing.get("email_rule_id"),
                "fwd_destination": existing.get("fwd_destination"),
            }

            set_page(iid, merged)
            if existing:
                updated += 1
            else:
                added += 1
        except Exception as exc:
            errors.append(f"{row.get('name', '?')}: {exc}")

    return {
        "added": added,
        "updated": updated,
        "total_in_notion": len(rows),
        "errors": errors,
        "pages": list_all_pages(),
    }


# ── Write-back helpers (round-trip to Notion) ────────────────────────────────


async def _patch_page(notion_page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
    """PATCH a Notion page's properties. Internal helper."""
    if not is_configured():
        raise RuntimeError("Notion not configured")
    if not notion_page_id:
        raise ValueError("notion_page_id is required")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.patch(
            f"{NOTION_API_BASE}/pages/{notion_page_id}",
            headers=_headers(),
            json={"properties": properties},
        )
        resp.raise_for_status()
        return resp.json()


async def update_page_status(notion_page_id: str, status: str) -> dict[str, Any]:
    """Set the Status select property on a Notion page."""
    return await _patch_page(
        notion_page_id,
        {"Status": {"select": {"name": status}}},
    )


async def update_page_drive_folder(notion_page_id: str, url: str) -> dict[str, Any]:
    """Set the Drive Folder URL on a Notion page."""
    return await _patch_page(
        notion_page_id,
        {"Drive Folder URL": {"url": url}},
    )


async def create_intake_page(
    *,
    account_username: str,
    label_artist: str | None = None,
    pipeline_choice: str | None = None,
    page_type: str | None = None,
    sounds_reference: str | None = None,
    notes: str | None = None,
    poster: str | None = None,
    go_live_date: str | None = None,
    group: str | None = None,
    group_label: str | None = None,
    account_type: str | None = None,
    email: str | None = None,
    fwd_address: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    """Create a new row in Master Pages with Status = 'New — Pending Setup'.

    Returns the created Notion page object including its `id`.
    """
    if not is_configured():
        raise RuntimeError("Notion not configured")
    if not account_username.strip():
        raise ValueError("account_username is required")

    properties: dict[str, Any] = {
        "Account Username": {"title": [{"type": "text", "text": {"content": account_username.strip()}}]},
        "Status": {"select": {"name": "New — Pending Setup"}},
    }

    if label_artist:
        properties["Label / Artist"] = {"rich_text": [{"type": "text", "text": {"content": label_artist}}]}
    if pipeline_choice:
        properties["Pipeline"] = {"select": {"name": pipeline_choice}}
    if page_type:
        properties["Page Type"] = {"select": {"name": page_type}}
    if sounds_reference:
        properties["Sounds Reference"] = {"url": sounds_reference}
    if notes:
        properties["Notes"] = {"rich_text": [{"type": "text", "text": {"content": notes}}]}
    if poster:
        properties["Poster"] = {"rich_text": [{"type": "text", "text": {"content": poster}}]}
    if go_live_date:
        properties["Go-Live Date"] = {"date": {"start": go_live_date}}
    if group:
        properties["Group"] = {"select": {"name": group}}
    if group_label:
        # Notion column name has a trailing space — preserve it
        properties["Group "] = {"select": {"name": group_label}}
    if account_type:
        properties["Account Type"] = {"select": {"name": account_type}}
    if email:
        properties["email"] = {"email": email}
    if fwd_address:
        properties["fwd address"] = {
            "rich_text": [{"type": "text", "text": {"content": fwd_address}}]
        }
    if password:
        properties["Password"] = {
            "rich_text": [{"type": "text", "text": {"content": password}}]
        }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{NOTION_API_BASE}/pages",
            headers=_headers(),
            json={
                "parent": {"database_id": NOTION_PAGES_DB},
                "properties": properties,
            },
        )
        resp.raise_for_status()
        return resp.json()


async def update_page_email_fields(
    notion_page_id: str,
    email: str | None = None,
    fwd_address: str | None = None,
) -> dict[str, Any]:
    """Set the email and/or fwd address fields on a Notion page.

    The Notion `email` column is type=email, `fwd address` is type=rich_text.
    """
    props: dict[str, Any] = {}
    if email is not None:
        props["email"] = {"email": email}
    if fwd_address is not None:
        props["fwd address"] = {
            "rich_text": [{"type": "text", "text": {"content": fwd_address}}]
        }
    if not props:
        return {}
    return await _patch_page(notion_page_id, props)
