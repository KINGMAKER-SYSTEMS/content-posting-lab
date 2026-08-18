"""Machine contract for the Content Bucket Control Plane.

The control plane worker has had a fully-written, fully-tested client for this
for a while (`control-plane-worker/src/adapters/contentLabClient.js`) and there
was nothing on this side to answer it: `/api/control-plane/v1/capabilities`
resolved to the SPA catch-all and returned index.html, indistinguishable from a
typo'd URL. This is that endpoint.

It answers exactly one question: **which recipes may this page run, at what
version, and how many at a time.** Nothing here accepts or returns a prompt.
The plane never sends prompt text — its job contract rejects any field matching
/(prompt|instruction|message)/i — and recipe authoring stays here, in the Lab,
where the prompts actually live.

A "recipe" is a Lab PROJECT. That is already the unit that owns a prompts.json,
so it is the only unit that can be locked and versioned without inventing a
second vocabulary. Its version is a content hash of that prompts.json: edit the
prompts and the version changes, which is precisely the signal the plane needs
to tell "this page is on the recipe I approved" from "someone changed it
underneath me".

Registration is explicit and lives in `recipes/<project>.json`, not inside the
project directory. `projects/` holds ~96 directories in production and most are
scratch or one-shot batches; offering those to an operator's dropdown would be
worse than offering nothing. It is also gitignored wholesale and full of
renders — an earlier attempt to keep the marker beside the prompts forced git
to walk thousands of video files and `git add` hung outright. A top-level
`recipes/` directory costs nothing to scan, needs no gitignore exception, and
makes registration read as what it is: a reviewable repo-level act.

Registration and content are deliberately separate. A marker only NOMINATES a
project; the endpoint still refuses to offer it unless that project really
exists here with prompts in it. So a marker committed for a project that lives
only on someone's laptop simply does not appear in production — which is how it
should fail, and how it did.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException

from project_manager import PROJECTS_DIR

router = APIRouter()

RESPONSE_SCHEMA = "content-lab.response.v1"
ENGINE = "content_lab"
RECIPES_DIR_NAME = "recipes"
PROMPTS = "prompts.json"

# The client caps the response at 200 entries and rejects anything larger, so
# stay well inside it rather than discovering the ceiling in production.
MAX_CAPABILITIES = 100

# Default ceiling on clips per job. Overridable per recipe in recipe.json; kept
# modest because every unit is real spend on a real provider.
DEFAULT_MAX_QUANTITY = 10

# Page ids arrive in a header and are used only for filtering, never for a
# filesystem path — but bound them anyway so a hostile value cannot be echoed
# unboundedly into a log line.
PAGE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

# Same discipline for the lane header on the roster snapshot.
LANE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

# The ONLY roster fields the snapshot may carry. The roster cache holds
# account credentials (password, signup_email, fwd_address, email aliases,
# drive folder ids) because other parts of the Lab need them; the control
# plane does not, and a fleet-wide machine endpoint that leaked them would be
# a credential dump with a schema version on it. Anything not listed here
# does not cross the boundary.
ROSTER_SNAPSHOT_FIELDS = (
    "pageId", "handle", "group", "groupLabel", "pageType", "accountType",
    "posterName", "status", "project", "tiktokUrl", "notionPageId", "source",
)

MAX_ROSTER_PAGES = 500


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _recipe_version(project_dir: Path) -> str | None:
    """Content hash of the project's prompts, or None if it has none.

    Versioning on content rather than on a hand-typed number means a recipe
    cannot be edited without its version moving. A recipe whose prompts changed
    but whose version did not would let the plane keep asserting an approval
    that no longer describes anything.
    """
    prompts = project_dir / PROMPTS
    if not prompts.is_file():
        return None
    raw = prompts.read_bytes()
    if not raw.strip():
        return None
    return "v" + hashlib.sha256(raw).hexdigest()[:12]


def _recipes_dir() -> Path:
    return PROJECTS_DIR.parent / RECIPES_DIR_NAME


def _registered_recipes() -> list[dict[str, Any]]:
    """Every project explicitly registered as a recipe, with a live version."""
    recipes_dir = _recipes_dir()
    if not recipes_dir.is_dir() or not PROJECTS_DIR.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for marker in sorted(recipes_dir.glob("*.json")):
        meta = _read_json(marker)
        if not isinstance(meta, dict) or meta.get("registered") is not True:
            continue
        name = meta.get("project") or marker.stem
        if not isinstance(name, str) or "/" in name or name in ("", ".", ".."):
            continue
        project_dir = PROJECTS_DIR / name
        if not project_dir.is_dir():
            # Nominated but absent. A marker for a project that exists only on
            # someone's laptop must not become a capability here.
            continue
        version = _recipe_version(project_dir)
        if version is None:
            # Registered but its prompts are gone or empty. Silently offering it
            # would hand the plane a recipe that generates nothing.
            continue
        quantity = meta.get("maxQuantity", DEFAULT_MAX_QUANTITY)
        if not isinstance(quantity, int) or quantity <= 0:
            quantity = DEFAULT_MAX_QUANTITY
        out.append({
            "recipeId": name,
            "engine": ENGINE,
            "recipeVersion": version,
            "maxQuantity": quantity,
            "_pages": meta.get("pages") if isinstance(meta.get("pages"), list) else None,
        })
    return out


@router.get("/v1/capabilities")
def capabilities(
    x_rt_page_id: str | None = Header(default=None),
    x_page_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Recipes this page may run.

    The page id is required — the plane's client always sends it, and answering
    without one would let a caller enumerate every recipe in the Lab from a
    single unscoped request. The header is `X-RT-Page-Id`, which is what
    ContentLabClient#headers actually emits; `X-Page-Id` is accepted as an
    alias so this stays testable by hand with curl.

    A recipe with no `pages` list in its marker is available to any page; one
    with a list is offered only to those pages. That keeps the common case
    (a shared truck recipe) zero-config while still allowing a recipe to be
    pinned to the page it was built for.
    """
    page_id = x_rt_page_id or x_page_id
    if not page_id or not PAGE_ID_RE.match(page_id):
        raise HTTPException(status_code=400, detail="X-RT-Page-Id header is required")

    entries = []
    for recipe in _registered_recipes():
        pages = recipe.pop("_pages")
        if pages is not None and page_id not in pages:
            continue
        entries.append(recipe)
        if len(entries) >= MAX_CAPABILITIES:
            break

    return {"schema": RESPONSE_SCHEMA, "capabilities": entries}


def _snapshot_page(page: dict[str, Any]) -> dict[str, Any] | None:
    """One roster row, reduced to exactly the ontology the plane reconciles.

    Every field is explicit — a null is a true statement ("Notion does not
    say"), never a dropped key, so the plane can tell "unknown" from
    "absent" and raise a blocker instead of guessing a lane or page type.
    """
    page_id = str(page.get("integration_id") or "").strip()
    handle = str(page.get("name") or "").strip()
    if not page_id or not handle:
        # A row with no stable identity cannot be reconciled to anything —
        # including it would give the plane a page it can never address.
        return None
    return {
        "pageId": page_id,
        "handle": handle,
        "group": page.get("group") or None,
        "groupLabel": page.get("group_label") or None,
        "pageType": page.get("page_type") or None,
        "accountType": page.get("account_type") or None,
        "posterName": page.get("poster_name") or None,
        "status": page.get("status") or None,
        # The page -> recipe link. `project` IS a recipe id when the project
        # is registered (see the capabilities contract above); the plane
        # treats anything unregistered as "no recipe attached", never as a
        # recipe that might exist.
        "project": page.get("project") or None,
        "tiktokUrl": page.get("tiktok_url") or None,
        "notionPageId": page.get("notion_page_id") or None,
        # "notion" for CRM-synced rows; null for legacy rows that predate the
        # Notion sync. The plane needs to know whose ontology a row speaks.
        "source": page.get("source") or None,
    }


@router.get("/v1/roster")
def roster_snapshot(
    x_rt_lane: str | None = Header(default=None),
) -> dict[str, Any]:
    """Versioned snapshot of the Notion-informed roster, for plane reconcile.

    This is the ONLY door the Master Pages ontology walks through on its way
    to the control plane — the plane runs no Notion client of its own, by
    design. Notion syncs into the roster cache (services/notion_pages.py),
    and this answers with what that cache currently says, content-hashed so
    the plane can tell "the fleet changed" from "it did not" without
    diffing 100+ rows.

    The snapshot speaks the Lab's own vocabulary (group: WARNER / ATLANTIC /
    INTERNAL; project = recipe id). Mapping groups to the plane's lanes is
    the plane's business — a contract that answered in the consumer's
    vocabulary would couple every other consumer to it too.

    `capturedAt` is the roster cache's own mtime: the honest answer to "how
    fresh is this" is "when the last Notion sync landed", not "when you
    asked". X-RT-Lane is required for the same reason capabilities requires
    a page id — these endpoints answer scoped machine callers, not bare
    crawlers.
    """
    if not x_rt_lane or not LANE_RE.match(x_rt_lane):
        raise HTTPException(status_code=400, detail="X-RT-Lane header is required")

    # Imported here so importing this router never touches the roster cache
    # for a caller that only wanted capabilities.
    from services.roster import ROSTER_PATH, list_all_pages

    pages: list[dict[str, Any]] = []
    for raw in list_all_pages():
        page = _snapshot_page(raw)
        if page is not None:
            pages.append(page)
        if len(pages) >= MAX_ROSTER_PAGES:
            break
    pages.sort(key=lambda page: page["pageId"])

    canonical = json.dumps(pages, sort_keys=True, separators=(",", ":"))
    version = "r" + hashlib.sha256(canonical.encode()).hexdigest()[:12]

    captured_at = None
    try:
        captured_at = datetime.fromtimestamp(
            ROSTER_PATH.stat().st_mtime, timezone.utc
        ).isoformat()
    except OSError:
        # No cache, no freshness claim. The snapshot is empty in that case
        # anyway; the plane's reconcile treats an empty snapshot as "say
        # nothing", never as "the fleet is gone".
        captured_at = None

    return {
        "schema": RESPONSE_SCHEMA,
        "snapshotVersion": version,
        "capturedAt": captured_at,
        "pages": pages,
    }
