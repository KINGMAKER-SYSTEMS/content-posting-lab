"""Register a control-plane page that is not in the Notion Master Pages roster.

Source imports, clip jobs and capabilities all resolve a page through
``routers.control_plane._current_master_pages_intent``, which needs exactly one
roster row whose ``integration_id`` is the page id. Rising Tides' own pages get
that row from the Notion sync. A page whose Master Pages intent is owned
elsewhere (North Star workspaces send ``source: "northstar"``) has no Notion
row, so every one of those routes answered 409 or an empty list for it.

This is the door for those pages. The control plane PUTs the page's complete
canonical intent and Content Lab stores it as a non-Notion roster row through
the roster store's ``set_page``. The row stays out of Notion's way by
construction:

* ``acct:`` ids are refused. Notion sync and pipeline intake mint only
  ``acct:<handle>`` ids, so sync can never write over an external row.
* Sync prunes only ``source == "notion"`` rows, so it never removes one.
* The fleet roster projection publishes only ``source == "notion"`` rows, so
  an external page never enters the Rising Tides fleet snapshot.
* An id or handle another roster row already holds is refused, and DELETE
  removes only rows whose source this door owns.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException

from routers.control_plane import (
    PAGE_ID_RE,
    RESPONSE_SCHEMA,
    _snapshot_page,
)
from routers.control_plane_recipes import LANE, require_control_plane_bearer
from services import roster
from services.json_store import lock_for
from services.master_pages_contract import (
    EXTERNAL_PAGE_SOURCES,
    REQUIRED_MATERIAL_FIELDS,
    SCHEMA as MASTER_PAGES_SCHEMA,
    canonical_intent,
    intent_hash,
)


router = APIRouter()

# ``EXTERNAL_PAGE_SOURCES`` (services/master_pages_contract.py) is the closed
# set of sources whose rows this door may write and delete. The ShipStream
# manifest parser reads the same set, so a page is an external page in exactly
# one place.
# Notion sync and pipeline intake mint ``acct:<handle>``. Keeping external ids
# out of that namespace is what guarantees a later Notion row can never land on
# (and overwrite) an external page.
NOTION_ID_PREFIX = "acct:"
MAX_INTENT_BODY_BYTES = 16_384

# Intent field -> roster row key, exactly as ``_snapshot_page`` reads it back.
_ROW_FIELDS = {
    "handle": "name",
    "group": "group",
    "groupLabel": "group_label",
    "pageType": "page_type",
    "contentNiche": "content_niche",
    "accountType": "account_type",
    "contentEngine": "content_engine",
    "automationMode": "automation_mode",
    "vaultUrl": "vault_url",
    "pipeline": "pipeline",
    "soundsReference": "sounds_reference",
    "archived": "archived",
    "posterName": "poster_name",
    "status": "status",
    "project": "project",
    "tiktokUrl": "tiktok_url",
    "notionPageId": "notion_page_id",
    "source": "source",
}


def _require_machine(authorization: str | None, lane: str | None, page_id: str) -> None:
    require_control_plane_bearer(authorization)
    if lane != LANE:
        raise HTTPException(400, f"X-RT-Lane must be {LANE}")
    if not PAGE_ID_RE.match(page_id):
        raise HTTPException(400, "pageId must be a bounded page token")


def _resolved_intent(page_id: str, row: dict[str, Any]) -> dict[str, Any] | None:
    """The intent ``_current_master_pages_intent`` would build from ``row``."""
    snapshot = _snapshot_page({**row, "integration_id": page_id})
    if snapshot is None:
        return None
    return canonical_intent(
        {"schema": MASTER_PAGES_SCHEMA, **snapshot, "pageId": page_id},
        expected_page_id=page_id,
    )


def _validated_intent(page_id: str, body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a Master Pages intent object")
    if len(json.dumps(body, ensure_ascii=False)) > MAX_INTENT_BODY_BYTES:
        raise HTTPException(400, "Master Pages intent is too large")
    intent = canonical_intent(body, expected_page_id=page_id)
    if intent is None:
        raise HTTPException(
            400,
            f"body must be the complete {MASTER_PAGES_SCHEMA} intent for this pageId",
        )
    if intent["source"] not in EXTERNAL_PAGE_SOURCES:
        raise HTTPException(
            400,
            "source must name an external Master Pages owner: "
            + ", ".join(sorted(EXTERNAL_PAGE_SOURCES)),
        )
    if intent["notionPageId"] is not None:
        raise HTTPException(400, "an external page has no Notion page id")
    missing = [field for field in REQUIRED_MATERIAL_FIELDS if intent[field] is None]
    if missing:
        raise HTTPException(400, f"intent is missing {', '.join(missing)}")
    return intent


@router.put("/v1/external-pages/{page_id}")
def register_external_page(
    page_id: str,
    authorization: str | None = Header(default=None),
    x_rt_lane: str | None = Header(default=None),
    body: Any = Body(default=None),
) -> dict[str, Any]:
    """Create or replace one non-Notion page's Master Pages intent.

    The body is the complete ``master-pages.page-intent.v1`` intent. The
    response repeats the intent and hash that source imports, clip jobs and
    capabilities will now resolve for this page, so the caller can confirm
    they equal the ones it stored. A repeat of the same intent is
    ``unchanged`` and writes nothing.
    """
    _require_machine(authorization, x_rt_lane, page_id)
    if page_id.lower().startswith(NOTION_ID_PREFIX):
        raise HTTPException(400, f"{NOTION_ID_PREFIX} page ids are reserved for Notion Master Pages rows")
    intent = _validated_intent(page_id, body)
    row = {key: intent[field] for field, key in _ROW_FIELDS.items()}
    if _resolved_intent(page_id, row) != intent:
        # Content Lab reads handles back in TikTok's lowercase spelling.
        raise HTTPException(400, "intent does not round-trip through the roster; send the handle in lowercase")

    handle = intent["handle"].casefold()
    with lock_for(roster.ROSTER_PATH):
        pages = roster.load_roster()["pages"]
        existing = pages.get(page_id)
        if existing is not None and existing.get("source") not in EXTERNAL_PAGE_SOURCES:
            if existing.get("source") == "notion":
                raise HTTPException(409, "page id is held by a Notion Master Pages row")
            raise HTTPException(409, "page id is held by a roster row this route does not own")
        for other_id, other in pages.items():
            if other_id == page_id:
                continue
            if str(other.get("name") or "").strip().casefold() == handle:
                if other.get("source") == "notion":
                    raise HTTPException(409, "handle is held by a Notion Master Pages row")
                raise HTTPException(409, "handle is held by another roster page")
        if existing is not None and _resolved_intent(page_id, existing) == intent:
            status = "unchanged"
        else:
            roster.set_page(page_id, row)
            status = "updated" if existing is not None else "created"

    return {
        "schema": RESPONSE_SCHEMA,
        "pageId": page_id,
        "status": status,
        "masterPages": intent,
        "masterPagesHash": intent_hash(intent),
    }


@router.delete("/v1/external-pages/{page_id}")
def remove_external_page(
    page_id: str,
    authorization: str | None = Header(default=None),
    x_rt_lane: str | None = Header(default=None),
) -> dict[str, Any]:
    """Remove one page this door registered. Notion and legacy rows refuse.

    Removing an already-absent page answers ``absent`` so offboarding can
    retry safely. Jobs and artifacts already created for the page are kept.
    """
    _require_machine(authorization, x_rt_lane, page_id)
    with lock_for(roster.ROSTER_PATH):
        existing = roster.get_page(page_id)
        if existing is None:
            status = "absent"
        elif existing.get("source") not in EXTERNAL_PAGE_SOURCES:
            if existing.get("source") == "notion":
                raise HTTPException(409, "Notion Master Pages rows are removed only by Notion sync")
            raise HTTPException(409, "page id is held by a roster row this route does not own")
        else:
            roster.remove_page(page_id)
            status = "deleted"
    return {"schema": RESPONSE_SCHEMA, "pageId": page_id, "status": status}
