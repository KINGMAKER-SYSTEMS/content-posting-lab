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


# ── Jobs: library selection under an idempotent, durable contract ────────────
#
# What a replenish job IS here: the registered recipes are footage LIBRARIES
# (trucks ≈ 2,986 clips, silhoutte ≈ 1,183, …), so a job selects `quantity`
# clips the page has never been served, manifests them with checksums, and
# records the serving so no page is ever handed the same clip twice. No
# generation, no provider spend — generation for a dry library is a later
# phase with its own arming decision, and a library that cannot fill a
# request fails 409 insufficient_inventory rather than quietly generating.
#
# The plane's job contract rejects free-form prompt fields; this side
# mirrors that rejection so a crafted body dies on whichever side it hits
# first. Job records are DURABLE (a JSON store under a lock), unlike the
# in-memory dicts the interactive /api/video routes use — a Railway
# restart must not forget what was served, or a page gets duplicates.

import secrets as _secrets

from fastapi import Body, Request
from fastapi.responses import FileResponse

from services.json_store import atomic_load, atomic_save, lock_for

JOBS_STORE_NAME = "control_plane_jobs.json"
JOB_ID_PREFIX = "cpl-"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".m4v"}
MAX_JOB_QUANTITY = 100
MAX_JOB_BODY_BYTES = 16_384
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,200}$")
JOB_TOKEN_BYTES = 24

# Defense-in-depth mirror of the plane's assertNoFreeFormPrompt: no field
# anywhere in the job body may look like prompt text. Recipe authoring
# stays in the Lab; the plane picks recipes, never words.
PROMPT_FIELD_RE = re.compile(r"(prompt|instruction|message)", re.IGNORECASE)

JOB_FIELDS = {
    "pageId", "lane", "engine", "lockedRecipeId", "recipeVersion",
    "quantity", "constraints", "sourceIsolation", "policyHash",
}


def _jobs_path() -> Path:
    # Same data dir as the roster cache (Railway volume in production).
    from services.roster import ROSTER_PATH
    return ROSTER_PATH.parent / JOBS_STORE_NAME


def _empty_jobs() -> dict[str, Any]:
    return {"version": 1, "jobs": {}, "byIdempotency": {}, "served": {}}


def _load_jobs() -> dict[str, Any]:
    data = atomic_load(_jobs_path(), default=None)
    if not isinstance(data, dict) or "jobs" not in data:
        return _empty_jobs()
    return data


def _reject_prompt_fields(value: Any, path: str = "job") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if PROMPT_FIELD_RE.search(str(key)):
                raise HTTPException(
                    status_code=400,
                    detail=f"free-form prompt fields are not accepted ({path}.{key})",
                )
            _reject_prompt_fields(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_prompt_fields(item, f"{path}[{index}]")


def _scan_library(project: str) -> list[str]:
    """Every clip in the recipe's library, as sorted project-relative paths.

    Sorted so selection is deterministic: the same library state always
    yields the same picks for the same served-set, which is what makes a
    retried job idempotent in practice, not just in key.
    """
    video_dir = PROJECTS_DIR / project / "videos"
    if not video_dir.is_dir():
        return []
    out: list[str] = []
    for path in video_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
            out.append(str(path.relative_to(video_dir)))
    return sorted(out)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _registered_recipe(project: str) -> dict[str, Any] | None:
    for recipe in _registered_recipes():
        if recipe["recipeId"] == project:
            return recipe
    return None


def _clip_manifest(project: str, rel_path: str) -> dict[str, Any] | None:
    full = PROJECTS_DIR / project / "videos" / rel_path
    try:
        size = full.stat().st_size
    except OSError:
        return None
    if size <= 0:
        # A zero-byte clip is not inventory; handing it out would plant a
        # corrupt asset in the page's bucket.
        return None
    return {
        "path": rel_path,
        "name": Path(rel_path).name,
        "sha256": _sha256(full),
        "bytes": size,
    }


@router.post("/v1/jobs")
def create_job(
    request: Request,
    x_rt_page_id: str | None = Header(default=None),
    x_rt_lane: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
    body: dict[str, Any] = Body(default=None),
) -> dict[str, Any]:
    """Select `quantity` never-served clips from a registered recipe's library.

    Idempotent by header: the same Idempotency-Key returns the original job
    without selecting again — a retried create can never double-serve.
    """
    if not x_rt_page_id or not PAGE_ID_RE.match(x_rt_page_id):
        raise HTTPException(status_code=400, detail="X-RT-Page-Id header is required")
    if not x_rt_lane or not LANE_RE.match(x_rt_lane):
        raise HTTPException(status_code=400, detail="X-RT-Lane header is required")
    if not idempotency_key or not IDEMPOTENCY_KEY_RE.match(idempotency_key):
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required (8-200 token chars)")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="job body must be a JSON object")

    unknown = sorted(set(body.keys()) - JOB_FIELDS)
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown job fields: {', '.join(unknown)}")
    _reject_prompt_fields(body)

    page_id = str(body.get("pageId") or "")
    if page_id != x_rt_page_id:
        raise HTTPException(status_code=400, detail="body pageId must match X-RT-Page-Id")
    if body.get("engine") != ENGINE:
        raise HTTPException(status_code=400, detail=f"engine must be {ENGINE}")
    recipe_id = str(body.get("lockedRecipeId") or "").strip()
    recipe_version = str(body.get("recipeVersion") or "").strip()
    policy_hash = str(body.get("policyHash") or "").strip()
    if not recipe_id or not recipe_version or not policy_hash:
        raise HTTPException(status_code=400, detail="lockedRecipeId, recipeVersion and policyHash are required")
    quantity = body.get("quantity")
    if not isinstance(quantity, int) or quantity <= 0 or quantity > MAX_JOB_QUANTITY:
        raise HTTPException(status_code=400, detail=f"quantity must be an integer 1..{MAX_JOB_QUANTITY}")
    constraints = body.get("constraints")
    if constraints is not None and not isinstance(constraints, dict):
        raise HTTPException(status_code=400, detail="constraints must be an object")
    if len(json.dumps(body)) > MAX_JOB_BODY_BYTES:
        raise HTTPException(status_code=400, detail="job body too large")

    recipe = _registered_recipe(recipe_id)
    if recipe is None:
        raise HTTPException(status_code=404, detail="recipe not registered")
    if quantity > recipe["maxQuantity"]:
        raise HTTPException(status_code=400, detail=f"quantity exceeds recipe ceiling {recipe['maxQuantity']}")
    current_version = recipe["recipeVersion"]
    if recipe_version != current_version:
        # The plane pinned a version the recipe has moved away from. Serving
        # anyway would fill a bucket with content the operator never approved.
        raise HTTPException(status_code=409, detail=f"recipe_version_mismatch: current is {current_version}")

    with lock_for(_jobs_path()):
        store = _load_jobs()
        existing_id = store["byIdempotency"].get(idempotency_key)
        if existing_id and existing_id in store["jobs"]:
            existing = store["jobs"][existing_id]
            return {"schema": RESPONSE_SCHEMA, "jobId": existing["jobId"], "status": existing["status"]}

        served_for_page = set(store["served"].get(recipe_id, {}).get(page_id, []))
        picks: list[dict[str, Any]] = []
        for rel_path in _scan_library(recipe_id):
            if rel_path in served_for_page:
                continue
            manifest = _clip_manifest(recipe_id, rel_path)
            if manifest is None:
                continue
            picks.append(manifest)
            if len(picks) >= quantity:
                break
        if not picks:
            # The library cannot fill this page right now. A dry library is
            # an inventory problem for the operator, never a reason to
            # generate spend nobody armed.
            raise HTTPException(status_code=409, detail="insufficient_inventory")

        job_id = JOB_ID_PREFIX + _secrets.token_hex(8)
        job = {
            "jobId": job_id,
            "idempotencyKey": idempotency_key,
            "pageId": page_id,
            "lane": str(body.get("lane") or x_rt_lane),
            "engine": ENGINE,
            "recipeId": recipe_id,
            "recipeVersion": recipe_version,
            "policyHash": policy_hash,
            "sourceIsolation": body.get("sourceIsolation") or None,
            "constraints": constraints or {},
            "quantityRequested": quantity,
            "status": "completed",
            "token": _secrets.token_urlsafe(JOB_TOKEN_BYTES),
            "clips": picks,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        store["jobs"][job_id] = job
        store["byIdempotency"][idempotency_key] = job_id
        served = store["served"].setdefault(recipe_id, {}).setdefault(page_id, [])
        served.extend(pick["path"] for pick in picks)
        atomic_save(_jobs_path(), store)

    return {"schema": RESPONSE_SCHEMA, "jobId": job_id, "status": "completed"}


def _get_job_or_404(job_id: str) -> dict[str, Any]:
    if not re.match(rf"^{JOB_ID_PREFIX}[0-9a-f]{{16}}$", job_id or ""):
        raise HTTPException(status_code=404, detail="job not found")
    job = _load_jobs()["jobs"].get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/v1/jobs/{job_id}")
def job_status(job_id: str, x_rt_page_id: str | None = Header(default=None)) -> dict[str, Any]:
    if not x_rt_page_id or not PAGE_ID_RE.match(x_rt_page_id):
        raise HTTPException(status_code=400, detail="X-RT-Page-Id header is required")
    job = _get_job_or_404(job_id)
    if job["pageId"] != x_rt_page_id:
        # A job answers only to the page it belongs to — cross-page status
        # reads would leak what other pages were served.
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "schema": RESPONSE_SCHEMA,
        "jobId": job["jobId"],
        "status": job["status"],
        "progress": 100 if job["status"] == "completed" else 0,
    }


@router.get("/v1/jobs/{job_id}/artifacts")
def job_artifacts(
    job_id: str,
    request: Request,
    x_rt_page_id: str | None = Header(default=None),
) -> dict[str, Any]:
    if not x_rt_page_id or not PAGE_ID_RE.match(x_rt_page_id):
        raise HTTPException(status_code=400, detail="X-RT-Page-Id header is required")
    job = _get_job_or_404(job_id)
    if job["pageId"] != x_rt_page_id:
        raise HTTPException(status_code=404, detail="job not found")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    base = f"{proto}://{host}"
    artifacts = [
        {
            "url": f"{base}/api/control-plane/v1/jobs/{job_id}/download/{index}?token={job['token']}",
            "type": "video",
        }
        for index in range(len(job["clips"]))
    ]
    return {"schema": RESPONSE_SCHEMA, "jobId": job_id, "artifacts": artifacts}


@router.get("/v1/jobs/{job_id}/download/{index}")
def job_download(job_id: str, index: int, token: str = "") -> FileResponse:
    job = _get_job_or_404(job_id)
    # The token is the download credential: unguessable, per-job, and the
    # only thing standing between a clip URL and the open internet.
    if not token or not _secrets.compare_digest(token, job["token"]):
        raise HTTPException(status_code=403, detail="invalid download token")
    if index < 0 or index >= len(job["clips"]):
        raise HTTPException(status_code=404, detail="artifact not found")
    rel_path = job["clips"][index]["path"]
    full = (PROJECTS_DIR / job["recipeId"] / "videos" / rel_path).resolve()
    video_root = (PROJECTS_DIR / job["recipeId"] / "videos").resolve()
    if video_root not in full.parents or not full.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(full, media_type="video/mp4", filename=job["clips"][index]["name"])
