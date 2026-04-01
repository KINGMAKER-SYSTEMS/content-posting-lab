"""
Page roster data access layer.
Manages the page_roster.json file that maps Postiz integrations to projects
and Google Drive folders.
"""

import json
import re
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
_DATA_DIR = Path("/data") if Path("/data").exists() else BASE_DIR
ROSTER_PATH = _DATA_DIR / "page_roster.json"

_DRIVE_FOLDER_RE = re.compile(r"folders/([a-zA-Z0-9_-]+)")


def _empty_roster() -> dict:
    return {"version": 1, "pages": {}}


def load_roster() -> dict:
    """Load roster from disk. Returns empty structure if file missing or corrupt."""
    if not ROSTER_PATH.exists():
        return _empty_roster()
    try:
        data = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "pages" not in data:
            return _empty_roster()
        return data
    except (json.JSONDecodeError, OSError):
        return _empty_roster()


def save_roster(data: dict) -> None:
    """Atomic write: write to tmp file then rename."""
    tmp = ROSTER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.rename(ROSTER_PATH)


def get_page(integration_id: str) -> dict | None:
    """Get a single page entry or None."""
    roster = load_roster()
    return roster["pages"].get(integration_id)


def set_page(integration_id: str, data: dict) -> dict:
    """Create or update a page entry. Returns the saved entry."""
    roster = load_roster()
    existing = roster["pages"].get(integration_id, {})
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

    entry = {
        "integration_id": integration_id,
        "name": data.get("name", existing.get("name", "")),
        "provider": data.get("provider", existing.get("provider", "")),
        "picture": data.get("picture", existing.get("picture")),
        "project": data.get("project", existing.get("project")),
        "drive_folder_url": data.get("drive_folder_url", existing.get("drive_folder_url")),
        "drive_folder_id": data.get("drive_folder_id", existing.get("drive_folder_id")),
        "email_alias": data.get("email_alias", existing.get("email_alias")),
        "email_rule_id": data.get("email_rule_id", existing.get("email_rule_id")),
        "fwd_destination": data.get("fwd_destination", existing.get("fwd_destination")),
        "added_at": existing.get("added_at", now),
        "updated_at": now,
    }

    # Auto-parse drive folder ID from URL if URL changed
    url = entry.get("drive_folder_url")
    if url and url != existing.get("drive_folder_url"):
        folder_id = parse_drive_folder_id(url)
        if folder_id:
            entry["drive_folder_id"] = folder_id

    roster["pages"][integration_id] = entry
    save_roster(roster)
    return entry


def remove_page(integration_id: str) -> bool:
    """Remove a page from the roster. Returns True if it existed."""
    roster = load_roster()
    if integration_id not in roster["pages"]:
        return False
    del roster["pages"][integration_id]
    save_roster(roster)
    return True


def list_pages_for_project(project_name: str) -> list[dict]:
    """Get all pages assigned to a project."""
    roster = load_roster()
    return [
        page for page in roster["pages"].values()
        if page.get("project") == project_name
    ]


def list_all_pages() -> list[dict]:
    """Get all pages in the roster."""
    roster = load_roster()
    return list(roster["pages"].values())


def parse_drive_folder_id(url: str) -> str | None:
    """Extract Google Drive folder ID from a URL.

    Handles formats like:
    - https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp
    - https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp?usp=sharing
    - https://drive.google.com/drive/u/0/folders/1AbCdEfGhIjKlMnOp
    """
    match = _DRIVE_FOLDER_RE.search(url)
    return match.group(1) if match else None
