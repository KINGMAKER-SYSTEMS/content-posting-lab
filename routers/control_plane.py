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

import asyncio
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException

from project_manager import PROJECTS_DIR
from providers.base import generate_one
from routers.control_plane_recipes import (
    list_registered_recipes,
    load_registered_recipe,
    require_control_plane_bearer,
)
from services.control_plane_generation import (
    MAX_CAPABILITY_QUANTITY,
    compose_prompt,
    dossier_filters_to_color_correction,
    generation_options,
    resolve_generation_recipe,
)
from services.ffmpeg import run_color_correct

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
    "contentNiche", "contentEngine", "automationMode", "vaultUrl", "pipeline",
    "soundsReference", "archived",
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
        pages = recipe.get("_pages")
        if pages is not None and page_id not in pages:
            continue
        entries.append({key: value for key, value in recipe.items() if key != "_pages"})
        if len(entries) >= MAX_CAPABILITIES:
            break

    # A dossier version is executable only when its server-owned base prompt
    # family, exact provider model, runtime credential, and typed treatment are
    # all available. Registration alone never becomes a capability.
    for publication in list_registered_recipes(page_id):
        recipe = resolve_generation_recipe(publication)
        if recipe is None:
            continue
        entries.append({
            "recipeId": publication["recipeId"],
            "engine": publication["engine"],
            "recipeVersion": publication["recipeVersion"],
            "maxQuantity": MAX_CAPABILITY_QUANTITY,
        })
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
        # The page's niche from Master Pages — the routing vocabulary the
        # plane's caption themes and campaign routing intersect with.
        "contentNiche": page.get("content_niche") or None,
        "accountType": page.get("account_type") or None,
        # Master Pages declares which existing Content Lab backend creates new
        # clips and where that page's output belongs. These are ontology, not
        # Railway-volume media pointers.
        "contentEngine": page.get("content_engine") or None,
        "automationMode": page.get("automation_mode") or None,
        "vaultUrl": page.get("vault_url") or None,
        "pipeline": page.get("pipeline") or None,
        "soundsReference": page.get("sounds_reference") or None,
        "archived": bool(page.get("archived")),
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


@router.post("/v1/roster/refresh")
async def refresh_roster_snapshot(
    x_rt_lane: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Refresh the Notion cache without returning credential-bearing rows.

    The control-plane cron calls this immediately before reading the sanitized
    snapshot above. The existing operator sync response includes the full
    internal roster, so it is intentionally not reused as a machine contract.
    """
    if not x_rt_lane or not LANE_RE.match(x_rt_lane):
        raise HTTPException(status_code=400, detail="X-RT-Lane header is required")
    expected_token = os.getenv("CONTROL_PLANE_TOKEN", "")
    supplied_token = (
        authorization.removeprefix("Bearer ").strip()
        if isinstance(authorization, str) else ""
    )
    if not expected_token:
        raise HTTPException(status_code=503, detail="Control-plane refresh is not configured")
    if not hmac.compare_digest(supplied_token, expected_token):
        raise HTTPException(status_code=401, detail="Invalid control-plane token")

    from services.notion_pages import is_configured, sync_into_roster

    if not is_configured():
        raise HTTPException(status_code=503, detail="Notion roster is not configured")
    try:
        result = await sync_into_roster()
    except Exception:
        raise HTTPException(status_code=502, detail="Notion roster refresh failed")

    errors = result.get("errors") if isinstance(result, dict) else None
    return {
        "schema": RESPONSE_SCHEMA,
        "added": int(result.get("added", 0)),
        "updated": int(result.get("updated", 0)),
        "totalInNotion": int(result.get("total_in_notion", 0)),
        "errorCount": len(errors) if isinstance(errors, list) else 0,
    }


# ── Jobs: new generation or approved-library selection, never both ──────────
#
# Legacy registered projects remain approved footage libraries: a job selects
# `quantity` clips the page has never been served and records that serving.
# A page-scoped dossier publication instead resolves the hash-pinned prompt
# catalog and creates new media in an isolated job root. A dry library never
# falls through to spend, and an unavailable generation executor never falls
# back to library footage.
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
GENERATION_ACTIVE_STATUSES = {"queued", "running"}
_GENERATION_RUNTIME_ID = _secrets.token_hex(16)

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


def _generation_root() -> Path:
    configured = os.environ.get("CONTENT_LAB_GENERATION_ROOT", "").strip()
    if configured:
        root = Path(configured).resolve()
    else:
        from services.roster import ROSTER_PATH
        root = (ROSTER_PATH.parent / "control_plane_generated").resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _update_job(job_id: str, **fields: Any) -> dict[str, Any] | None:
    with lock_for(_jobs_path()):
        store = _load_jobs()
        job = store["jobs"].get(job_id)
        if job is None:
            return None
        job.update(fields)
        atomic_save(_jobs_path(), store)
        return dict(job)


def _generated_manifest(job_root: Path, path: Path) -> dict[str, Any]:
    root = job_root.resolve()
    full = path.resolve()
    if root not in full.parents or not full.is_file():
        raise RuntimeError("generated artifact escaped its job root")
    size = full.stat().st_size
    if size <= 0:
        raise RuntimeError("generated artifact is empty")
    return {
        "path": str(full.relative_to(root)),
        "name": full.name,
        "sha256": _sha256(full),
        "bytes": size,
    }


async def _run_dossier_generation(job_id: str) -> None:
    job = _get_job_or_404(job_id)
    publication = load_registered_recipe(
        job["pageId"], job["recipeId"], job["engine"], job["recipeVersion"],
    )
    recipe = resolve_generation_recipe(publication) if publication else None
    if recipe is None:
        _update_job(
            job_id,
            status="failed",
            error="recipe_executor_unavailable",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return

    job_root = Path(job["artifactRoot"]).resolve()
    render_root = job_root / "renders"
    treated_root = job_root / "treated"
    render_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    options = generation_options(recipe)
    duration = int(options.pop("duration", 6))
    resolution = str(options.pop("resolution", "1080p"))
    aspect_ratio = str(options.pop("aspect_ratio", "9:16"))
    calls = int(job["providerCallsPlanned"])
    color_correction = dossier_filters_to_color_correction(recipe)
    manifests: list[dict[str, Any]] = []
    _update_job(job_id, status="running", progress=0, providerCallsCompleted=0)

    try:
        for call_index in range(calls):
            provider_job_id = f"{job_id}-g{call_index:02d}"
            provider_jobs = {
                provider_job_id: {
                    "videos": [{"index": 0, "status": "queued"}],
                }
            }
            prompt, slots = compose_prompt(recipe, job["idempotencyKey"], call_index)
            await generate_one(
                provider_job_id,
                0,
                recipe.engine,
                prompt,
                aspect_ratio,
                resolution,
                duration,
                None,
                provider_jobs,
                render_root,
                "",
                **options,
            )
            entry = provider_jobs[provider_job_id]["videos"][0]
            if entry.get("status") != "done":
                raise RuntimeError("provider_generation_failed")
            candidates = entry.get("crops") or [{"file": entry.get("file")}]
            for candidate_index, candidate in enumerate(candidates):
                rel_path = candidate.get("file") if isinstance(candidate, dict) else None
                if not isinstance(rel_path, str) or not rel_path:
                    raise RuntimeError("provider_artifact_missing")
                source = (render_root / rel_path).resolve()
                if render_root.resolve() not in source.parents or not source.is_file():
                    raise RuntimeError("provider_artifact_invalid")
                artifact = source
                if color_correction:
                    treated_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                    artifact = treated_root / f"g{call_index:02d}-c{candidate_index:02d}.mp4"
                    await run_color_correct(
                        str(source), str(artifact), color_correction, scale=None,
                    )
                manifest = _generated_manifest(job_root, artifact)
                manifest["generationIndex"] = call_index
                manifest["promptSlots"] = slots
                manifests.append(manifest)
            _update_job(
                job_id,
                progress=int(((call_index + 1) / calls) * 100),
                providerCallsCompleted=call_index + 1,
            )
    except Exception as error:  # provider and ffmpeg failures are job state
        _update_job(
            job_id,
            status="failed",
            error=str(error)[:300],
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return

    _update_job(
        job_id,
        status="completed",
        progress=100,
        clips=manifests,
        completedAt=datetime.now(timezone.utc).isoformat(),
    )


_generation_tasks: dict[str, asyncio.Task] = {}


def _start_dossier_generation(job_id: str) -> None:
    task = asyncio.create_task(_run_dossier_generation(job_id))
    _generation_tasks[job_id] = task
    task.add_done_callback(lambda _: _generation_tasks.pop(job_id, None))


@router.post("/v1/jobs")
async def create_job(
    request: Request,
    x_rt_page_id: str | None = Header(default=None),
    x_rt_lane: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    body: dict[str, Any] = Body(default=None),
) -> dict[str, Any]:
    """Execute an exact recipe version under one durable idempotency key.

    Legacy `content_lab` project recipes continue to select approved library
    bytes. A page-scoped dossier version uses only the new-media executor and
    never scans that library.
    """
    require_control_plane_bearer(authorization)
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
    if body.get("lane") != x_rt_lane:
        raise HTTPException(status_code=400, detail="body lane must match X-RT-Lane")
    engine = str(body.get("engine") or "").strip()
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

    publication = load_registered_recipe(page_id, recipe_id, engine, recipe_version)
    generation_recipe = resolve_generation_recipe(publication) if publication else None
    legacy_recipe = None
    if publication is not None:
        if generation_recipe is None:
            raise HTTPException(status_code=409, detail="recipe_executor_unavailable")
        if quantity > MAX_CAPABILITY_QUANTITY:
            raise HTTPException(
                status_code=400,
                detail=f"quantity exceeds recipe ceiling {MAX_CAPABILITY_QUANTITY}",
            )
        try:
            provider_calls = generation_recipe.planned_provider_calls(quantity)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    else:
        if engine != ENGINE:
            raise HTTPException(status_code=404, detail="recipe not registered")
        legacy_recipe = _registered_recipe(recipe_id)
        if legacy_recipe is None:
            raise HTTPException(status_code=404, detail="recipe not registered")
        if quantity > legacy_recipe["maxQuantity"]:
            raise HTTPException(
                status_code=400,
                detail=f"quantity exceeds recipe ceiling {legacy_recipe['maxQuantity']}",
            )
        current_version = legacy_recipe["recipeVersion"]
        if recipe_version != current_version:
            raise HTTPException(
                status_code=409,
                detail=f"recipe_version_mismatch: current is {current_version}",
            )

    start_generation = False
    with lock_for(_jobs_path()):
        store = _load_jobs()
        existing_id = store["byIdempotency"].get(idempotency_key)
        if existing_id and existing_id in store["jobs"]:
            existing = store["jobs"][existing_id]
            if (
                existing.get("sourceKind") == "generated"
                and existing.get("status") in GENERATION_ACTIVE_STATUSES
                and existing.get("runtimeId") != _GENERATION_RUNTIME_ID
            ):
                existing.update({
                    "status": "failed",
                    "error": "generation_runtime_restarted",
                    "completedAt": datetime.now(timezone.utc).isoformat(),
                })
                atomic_save(_jobs_path(), store)
            return {"schema": RESPONSE_SCHEMA, "jobId": existing["jobId"], "status": existing["status"]}

        job_id = JOB_ID_PREFIX + _secrets.token_hex(8)
        common = {
            "jobId": job_id, "idempotencyKey": idempotency_key,
            "pageId": page_id, "lane": str(body.get("lane") or x_rt_lane),
            "engine": engine, "recipeId": recipe_id,
            "recipeVersion": recipe_version, "policyHash": policy_hash,
            "sourceIsolation": body.get("sourceIsolation") or None,
            "constraints": constraints or {}, "quantityRequested": quantity,
            "token": _secrets.token_urlsafe(JOB_TOKEN_BYTES),
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        if generation_recipe is not None:
            job_root = (
                _generation_root() / page_id / recipe_version / job_id
            ).resolve()
            job_root.mkdir(parents=True, exist_ok=False, mode=0o700)
            job = {
                **common,
                "sourceKind": "generated",
                "status": "queued",
                "progress": 0,
                "clips": [],
                "artifactRoot": str(job_root),
                "dossierRevision": publication["dossierRevision"],
                "recipeSpecHash": publication["recipeSpecHash"],
                "promptCatalogHash": generation_recipe.prompt_catalog_hash,
                "family": generation_recipe.family_name,
                "providerModel": generation_recipe.provider_model,
                "providerCallsPlanned": provider_calls,
                "providerCallsCompleted": 0,
                "runtimeId": _GENERATION_RUNTIME_ID,
            }
            start_generation = True
        else:
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
                raise HTTPException(status_code=409, detail="insufficient_inventory")
            job = {
                **common,
                "sourceKind": "approved_library",
                "status": "completed",
                "progress": 100,
                "clips": picks,
            }
            served = store["served"].setdefault(recipe_id, {}).setdefault(page_id, [])
            served.extend(pick["path"] for pick in picks)
        store["jobs"][job_id] = job
        store["byIdempotency"][idempotency_key] = job_id
        atomic_save(_jobs_path(), store)

    if start_generation:
        _start_dossier_generation(job_id)
    return {"schema": RESPONSE_SCHEMA, "jobId": job_id, "status": job["status"]}


def _get_job_or_404(job_id: str) -> dict[str, Any]:
    if not re.match(rf"^{JOB_ID_PREFIX}[0-9a-f]{{16}}$", job_id or ""):
        raise HTTPException(status_code=404, detail="job not found")
    job = _load_jobs()["jobs"].get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/v1/jobs/{job_id}")
def job_status(
    job_id: str,
    x_rt_page_id: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_control_plane_bearer(authorization)
    if not x_rt_page_id or not PAGE_ID_RE.match(x_rt_page_id):
        raise HTTPException(status_code=400, detail="X-RT-Page-Id header is required")
    job = _get_job_or_404(job_id)
    if (
        job.get("sourceKind") == "generated"
        and job.get("status") in GENERATION_ACTIVE_STATUSES
        and job.get("runtimeId") != _GENERATION_RUNTIME_ID
    ):
        _update_job(
            job_id,
            status="failed",
            error="generation_runtime_restarted",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        job = _get_job_or_404(job_id)
    if job["pageId"] != x_rt_page_id:
        # A job answers only to the page it belongs to — cross-page status
        # reads would leak what other pages were served.
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "schema": RESPONSE_SCHEMA,
        "jobId": job["jobId"],
        "status": job["status"],
        "progress": int(job.get("progress") or 0),
    }


@router.get("/v1/jobs/{job_id}/artifacts")
def job_artifacts(
    job_id: str,
    request: Request,
    x_rt_page_id: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_control_plane_bearer(authorization)
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
    if job.get("sourceKind") == "generated":
        video_root = Path(job["artifactRoot"]).resolve()
        full = (video_root / rel_path).resolve()
    else:
        video_root = (PROJECTS_DIR / job["recipeId"] / "videos").resolve()
        full = (video_root / rel_path).resolve()
    if video_root not in full.parents or not full.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(full, media_type="video/mp4", filename=job["clips"][index]["name"])
