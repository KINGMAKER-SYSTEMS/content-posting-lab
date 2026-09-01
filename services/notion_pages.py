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
    load_roster,
    mutate_roster,
    set_page,
)

NOTION_API_KEY = os.getenv("NOTION_API_KEY", "")
NOTION_PAGES_DB = os.getenv("NOTION_PAGES_DB", "")
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Reads that touch the database itself need the newer API version: Notion
# migrated databases to a multi-data-source model, and the 2022-06-28
# version refuses to query any database that has more than one data source
# ("Databases with multiple data sources are not supported in this API
# version"). Under 2025-09-03 a database is queried through its data
# sources: GET /databases/{id} lists them, POST /data_sources/{id}/query
# pages each one. Page-shaped results are unchanged, so parse_page and all
# property extractors work exactly as before. Writes (page create/update)
# stay on the pinned version — they never hit this failure and re-verifying
# them against a new version is its own change.
NOTION_VERSION_DATA_SOURCES = "2025-09-03"


def _headers(version: str = NOTION_VERSION) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": version,
        "Content-Type": "application/json",
    }


async def _data_source_ids(client: httpx.AsyncClient) -> list[str]:
    """Every data source the Master Pages database currently has.

    A single-source database returns one id and behaves exactly like the old
    database query; a multi-source one returns several and every one is
    queried, so a second source added in Notion silently splits no rows off
    the roster.
    """
    resp = await client.get(
        f"{NOTION_API_BASE}/databases/{NOTION_PAGES_DB}",
        headers=_headers(NOTION_VERSION_DATA_SOURCES),
    )
    resp.raise_for_status()
    sources = resp.json().get("data_sources") or []
    return [source["id"] for source in sources if source.get("id")]


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

def _multi_select(prop: dict) -> list[str]:
    if not isinstance(prop, dict):
        return []
    values = prop.get("multi_select")
    if not isinstance(values, list):
        return []
    return [str(value.get("name") or "").strip() for value in values if str(value.get("name") or "").strip()]


def _text_or_multi_select(prop: dict) -> str:
    """Read a human text field across the live and legacy Notion types."""
    text = _rich_text(prop)
    if text:
        return text
    return ", ".join(_multi_select(prop))


def _checkbox(prop: dict) -> bool:
    return bool(prop.get("checkbox")) if isinstance(prop, dict) else False


def _external_file_url(prop: dict) -> str:
    """First external file URL only; uploaded Notion files are expiring URLs."""
    files = prop.get("files") if isinstance(prop, dict) else None
    if not isinstance(files, list):
        return ""
    for item in files:
        external = item.get("external") if isinstance(item, dict) else None
        if isinstance(external, dict) and external.get("url"):
            return str(external["url"]).strip()
    return ""

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


# ``Account Group`` is the operator-maintained assignment in Master Pages.
# ``Label`` is an older broad taxonomy column and is routinely stale after a
# page moves between Warner and the internal fleet.  These reserved account
# groups therefore own the broad lane projection; Label remains a fallback for
# client/artist groups that have not yet been migrated to the closed mapping.
ACCOUNT_GROUP_TO_GROUP = {
    "Internal Page": "INTERNAL",
    "Warner Test UGC": "INTERNAL",
    "Warner UGC": "WARNER",
}


def group_from_account_group(group_label: str, legacy_group: str) -> str:
    return ACCOUNT_GROUP_TO_GROUP.get(group_label, legacy_group)


# ── Parse a single Notion page ───────────────────────────────────────────────


def parse_page(notion_page: dict) -> dict[str, Any] | None:
    """Parse a Notion DB row into our roster format. Returns None if no username."""
    props = notion_page.get("properties", {}) or {}

    username = _title(props.get("Account Username", {}))
    if not username:
        return None

    # Account Group is the live assignment authority. Label is retained as a
    # fallback only for account groups outside the closed reserved mapping.
    # This prevents a stale Label=WARNER from keeping an Internal Page in the
    # Warner fleet after the operator changes its Account Group in Notion.
    legacy_group = _select(props.get("Label", props.get("Group", {})))
    group_label = _select(props.get("Account Group", props.get("Group ", {})))
    group = group_from_account_group(group_label, legacy_group)
    # The page's content niche — the Master Pages half of the routing
    # vocabulary (posting-documentation/SUBMISSION_FORM_NICHE_ALIGNMENT.md).
    # The live column name carries a trailing space ("Content Niche ").
    content_niche = _select(props.get("Content Niche ", props.get("Content Niche", {})))

    # These names and types are read from the live Master Pages data source.
    # They are the page ontology the Dossier consumes: ContentEngine selects
    # the existing generation backend; Vault Link is the declared page bucket.
    account_status = _select(props.get("Account Status", props.get("Status", {})))
    vault_url = _url(props.get("Vault Link", {})) or _external_file_url(props.get("Files & media", {}))

    return {
        "integration_id": mint_integration_id(username),
        "name": username,
        "provider": "tiktok",
        "tiktok_url": _url(props.get("Page URL", {})),
        "signup_email": _email(props.get("email", {})),
        "fwd_address": _text_or_multi_select(props.get("fwd address", {})),
        "password": _rich_text(props.get("Password", {})),
        "poster_name": _text_or_multi_select(props.get("Poster", {})),
        "group": group,
        "group_label": group_label,
        "account_type": _select(props.get("Account Type", {})),
        "notes": _rich_text(props.get("Notes", {})),
        "notion_page_id": notion_page.get("id", ""),
        "source": "notion",
        "status": account_status,
        "account_status": account_status,
        "pipeline": _select(props.get("Pipeline", {})),
        "page_type": _select(props.get("Page Type", {})),
        "content_niche": content_niche,
        "content_engine": _select(props.get("ContentEngine", {})),
        "automation_mode": _select(props.get("Automation vs Operator", {})),
        "vault_url": vault_url,
        "archived": _checkbox(props.get("Archived", {})),
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

    async with httpx.AsyncClient(timeout=30) as client:
        source_ids = await _data_source_ids(client)
        for source_id in source_ids:
            has_more = True
            cursor: str | None = None
            while has_more:
                body: dict[str, Any] = {"page_size": 100}
                if cursor:
                    body["start_cursor"] = cursor

                resp = await client.post(
                    f"{NOTION_API_BASE}/data_sources/{source_id}/query",
                    headers=_headers(NOTION_VERSION_DATA_SOURCES),
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
      - Existing non-Notion rows keyed by Postiz integration_id are NOT touched.
      - A prior Notion row absent from the complete successful fetch is pruned;
        otherwise username changes and removed rows survive as false identities.
      - Duplicate usernames resolve only when exactly one row is unarchived.
        Two active rows are ambiguous and remain unchanged instead of being
        selected by Notion query order.

    Returns: {added, updated, total_in_notion, errors}
    """
    fetched_rows = await fetch_all_pages()

    rows_by_id: dict[str, dict[str, Any]] = {}
    ambiguous_ids: set[str] = set()
    errors: list[str] = []
    for row in fetched_rows:
        iid = row["integration_id"]
        if iid in ambiguous_ids:
            continue
        prior = rows_by_id.get(iid)
        if prior is None:
            rows_by_id[iid] = row
            continue
        active = [candidate for candidate in (prior, row) if not candidate.get("archived")]
        if len(active) == 1:
            rows_by_id[iid] = active[0]
        elif not active:
            rows_by_id[iid] = min(
                (prior, row), key=lambda candidate: str(candidate.get("notion_page_id") or ""),
            )
        else:
            rows_by_id.pop(iid, None)
            ambiguous_ids.add(iid)
            errors.append(f"{row.get('name', '?')}: duplicate active Master Pages identity")

    rows = [rows_by_id[iid] for iid in sorted(rows_by_id)]

    roster = load_roster()
    existing_pages = roster["pages"]

    added = 0
    updated = 0
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
                "content_niche": row.get("content_niche"),
                "sounds_reference": row.get("sounds_reference"),
                "go_live_date": row.get("go_live_date"),
                "account_status": row.get("account_status"),
                "content_engine": row.get("content_engine"),
                "automation_mode": row.get("automation_mode"),
                "vault_url": row.get("vault_url"),
                "archived": bool(row.get("archived")),
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

    authoritative_ids = set(rows_by_id) | ambiguous_ids
    with mutate_roster() as current:
        for iid, page in list(current["pages"].items()):
            if page.get("source") == "notion" and iid not in authoritative_ids:
                del current["pages"][iid]
        pages = list(current["pages"].values())

    return {
        "added": added,
        "updated": updated,
        "total_in_notion": len(fetched_rows),
        "errors": errors,
        "pages": pages,
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


async def update_intake_page(
    notion_page_id: str,
    *,
    account_username: str | None = None,
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
) -> dict[str, Any]:
    """Patch an in-flight intake row with the rest of the form details.

    Used in step 2 of the intake flow: step 1 creates the row with just the
    email + placeholder username, step 2 fills in the actual TikTok handle
    and page details after the user finishes signup.
    """
    props: dict[str, Any] = {}
    if account_username and account_username.strip():
        props["Account Username"] = {
            "title": [{"type": "text", "text": {"content": account_username.strip()}}]
        }
    if label_artist:
        props["Label / Artist"] = {"rich_text": [{"type": "text", "text": {"content": label_artist}}]}
    if pipeline_choice:
        props["Pipeline"] = {"select": {"name": pipeline_choice}}
    if page_type:
        props["Page Type"] = {"select": {"name": page_type}}
    if sounds_reference:
        props["Sounds Reference"] = {"url": sounds_reference}
    if notes:
        props["Notes"] = {"rich_text": [{"type": "text", "text": {"content": notes}}]}
    if poster:
        props["Poster"] = {"rich_text": [{"type": "text", "text": {"content": poster}}]}
    if go_live_date:
        props["Go-Live Date"] = {"date": {"start": go_live_date}}
    if group:
        props["Group"] = {"select": {"name": group}}
    if group_label:
        props["Group "] = {"select": {"name": group_label}}
    if account_type:
        props["Account Type"] = {"select": {"name": account_type}}
    if not props:
        return {}
    return await _patch_page(notion_page_id, props)
