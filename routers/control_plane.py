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
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import anyio

from fastapi import APIRouter, Depends, Header, HTTPException

from project_manager import PROJECTS_DIR
from providers import PROVIDERS
from providers.base import (
    classify_provider_error,
    generate_one,
    multi_crop_vertical,
    provider_error_code,
)
from routers.control_plane_recipes import (
    LANE as CONTROL_PLANE_LANE,
    list_registered_recipe_bindings,
    list_registered_recipes,
    load_registered_recipe,
    load_registered_recipe_binding,
    publication_matches_master_pages,
    require_control_plane_bearer,
)
from services.control_plane_generation import (
    CATALOG_PATH,
    MAX_CAPABILITY_QUANTITY,
    compose_prompt_combination,
    dossier_clip_crop,
    dossier_clip_speed,
    dossier_filters_to_color_correction,
    generation_options,
    load_generation_anchor,
    plan_prompt_combinations,
    prompt_combination_space,
    prompt_sha256,
    resolve_generation_recipe,
    typed_recipe_spec,
)
from services.control_plane_sources import (
    CAPABILITY_PLAN_SEED,
    MASTER_WINDOWS_EXHAUSTED,
    canonical_source_identity,
    plan_source_cuts,
    source_cut_is_planned,
    resolve_source_recipe,
    source_window_exclusions,
)
from services.control_plane_slideshows import (
    SyzygyError,
    download_syzygy_artifact,
    load_recipe_library,
    load_syzygy_library,
    observe_library_media,
    plan_slideshows,
    poll_syzygy_render,
    resolve_slideshow_recipe,
    submit_syzygy_render,
    syzygy_revision,
)
from services.control_plane_source_imports import (
    MAX_CONCURRENT_SOURCE_IMPORTS,
    SOURCE_IMPORT_SCHEMA,
    SOURCE_PROVENANCE_SCHEMA,
    SourceImportError,
    SourceImportUnavailable,
    download_source_video,
    source_import_slot,
    validate_source_url,
)
from services.content_engine_registry import load_engine_registry, resolve_material_profile
from services.content_format_contracts import CONTRACTS_PATH, load_format_contracts
from services.ffmpeg import delivery_encode_args, probe_display_size, run_color_correct
from services.master_pages_contract import SCHEMA as MASTER_PAGES_SCHEMA, canonical_intent, exact_intent, intent_hash
from services.page_frame import VERTICAL_FRAME, resolved_frame
from services import moderation_retry
from services.roster_public import require_roster_auth
from services.source_treatment import (
    derived_source_treatment,
    recovery_treatment_matches,
    source_treatment_receipt,
)
from services.source_dna_registry import source_dna_master_hashes

router = APIRouter()
log = logging.getLogger("control_plane")

RESPONSE_SCHEMA = "content-lab.response.v1"
ENGINE = "content_lab"
RECIPES_DIR_NAME = "recipes"
PROMPTS = "prompts.json"

# The client caps the response at 200 entries and rejects anything larger, so
# stay well inside it rather than discovering the ceiling in production.
MAX_CAPABILITIES = 100
SOURCE_URL_RESOLVE_SECONDS = 10

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


# Reader hot-path cache (Tides review of PR #185, round 2, defect 3 — writer
# starvation under a zero-gap read burst): capabilities() is deterministic
# in everything it reads — the job snapshot, the resolved Master Pages
# intent, the engine registry, and the registered bindings — so many
# reader threads asking for the SAME page between the same two writes were
# each redoing the identical, non-trivial reservation scan
# (_generated_unavailable_prompts / _truck_master_candidates / etc.) from
# scratch. Under a genuinely concurrent, zero-gap burst that meant many
# CPU-bound Python threads competing for the GIL against the writer's own
# read-modify-write, which is what starved it (profiled: ~60us of Python
# work per capabilities() call, dominated by the two reservation scans —
# cheap once, but multiplied by thousands of redundant calls/second under
# a tight-loop burst).
#
# The cache key folds in every input capabilities() reads that has a cheap
# version signal: the page, the job-snapshot generation (so a write is
# ALWAYS a hard, immediate miss — C2's read-after-write bound is unaffected,
# it never depends on the TTL below), the resolved intent hash, the
# engine-registry file hash, a collision-safe stat signature for the
# format-contracts file (MaterialProfile fields are parsed FROM the matching
# contract, not just the registry — services/content_engine_registry.py's
# load_engine_registry passes contracts[format_slug] into _parse_profile —
# so registry_hash alone does not capture a contract-only edit), the exact
# content of every registered binding (recipeSpecHash is registration-time
# bound to recipeSpecCanonical, rejected at POST /v1/recipes if they do not
# match — routers/control_plane_recipes.py :435-436 — so it stands in for
# the full spec bytes, the only other publication field the resolvers read
# besides recipeId/engine), the SET of engines among those bindings (belt
# and suspenders alongside the per-binding tuples — see
# _capabilities_binding_engines), and a collision-safe stat signature for
# every prompt-catalog file resolve_generation_recipe can read, when any
# registered binding is ai_video (its content is not implied by anything
# else in the key).
#
# Signature format, everywhere in this cache (Tides review of PR #185,
# round 3): (path, st_ino, st_mtime_ns, st_size, st_ctime_ns) — the SAME
# four fields T1 uses for the job-store signature, not just mtime_ns+size.
# mtime_ns+size alone collides on a same-second, same-length in-place
# rewrite; ino distinguishes a replace (new inode) and ctime catches an
# in-place rewrite that lands within the same mtime tick. A plain content
# hash (as an earlier version of this used for both contracts and the
# catalog) is collision-proof too but pays a full read+parse(+hash) on
# every request; a first version that did this for the prompt catalog alone
# measured a real p95 regression (~20ms to ~206ms under the C5 load test —
# it reads and re-parses up to three files). A stat is one syscall.
#
# Audited and NOT cacheable at all: a registered slideshow binding
# (sourced_slideshow / lyrics_slideshows) resolves through
# load_syzygy_library, a LIVE networked fetch with no cheap pre-fetch
# version signal at all — not even a stat, since there is no local file.
# capabilities() never caches (reads or stores) a response for a page with
# any slideshow binding registered; it always computes fresh for those,
# same as base did for every page. The binding-engine set is ALSO in the
# key (not just this bypass) so that a page whose bindings change from
# ai_video to slideshow between two calls at the SAME job generation can
# never be served the earlier, now-stale ai_video response: the bypass
# alone would already prevent it (cacheable is re-evaluated fresh every
# call from the CURRENT registered_bindings), but the key changing too
# means this holds even if the bypass logic is ever refactored to be less
# strict for some other binding kind later.
#
# Audited and found NOT a factor: no time/wall-clock read (datetime.now(),
# a lease or expiry window) feeds any reservation or planning function
# capabilities() calls (_generated_unavailable_prompts,
# _truck_master_candidates, _slideshow_unavailable_signatures,
# _source_dna_unavailable_slots, plan_prompt_combinations, plan_source_cuts,
# plan_slideshows, resolve_generation_recipe, _dossier_source_recipe,
# resolve_slideshow_recipe, resolve_material_profile) — the one wall-clock
# read in this file that gates on elapsed time
# (_source_import_active_deadline_expired) is reachable only from
# GET /v1/jobs/{id}, never from capabilities(). No request header or param
# beyond X-RT-Page-Id/X-Page-Id (folded into page_id) is read either.
#
# The remaining short TTL is a defensive backstop only, not the mechanism
# this relies on for the inputs actually in the key above — those make a
# write, an intent change, a registry edit, a contract edit, or a catalog
# edit for a bound format ALL hard, immediate misses.
_capabilities_cache_lock = Lock()
_capabilities_cache: dict[tuple[Any, ...], tuple[float, list[dict[str, Any]]]] = {}
_CAPABILITIES_CACHE_TTL_SECONDS = 2.0
_CAPABILITIES_CACHE_MAX_ENTRIES = 4096

_SLIDESHOW_ENGINES = frozenset({"sourced_slideshow", "lyrics_slideshows"})


def _file_stat_signature(path: Path) -> tuple[str, int, int, int, int]:
    """Collision-safe (ino, mtime_ns, size, ctime_ns) freshness signature
    for one file, keyed by its path — the same four fields T1 uses for the
    job-store signature. -1s if the file does not exist (still a valid,
    stable signature: present -> absent is itself a real change)."""
    try:
        stat = path.stat()
    except OSError:
        return (str(path), -1, -1, -1, -1)
    return (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns)


def _capabilities_cache_key(
    page_id: str,
    generation: int,
    master_pages_hash: str,
    registry_hash: str | None,
    contracts_signature: tuple[str, int, int, int, int],
    registered_bindings: list[tuple[Any, dict[str, Any]]],
    binding_engines: tuple[str, ...],
    catalog_signature: tuple[tuple[str, int, int, int, int], ...],
) -> tuple[Any, ...]:
    bindings_key = tuple(
        (
            publication.get("recipeId"), publication.get("engine"),
            publication.get("recipeVersion"), publication.get("recipeSpecHash"),
        )
        for _, publication in registered_bindings
    )
    return (
        page_id, generation, master_pages_hash, registry_hash,
        contracts_signature, bindings_key, binding_engines, catalog_signature,
    )


def _capabilities_binding_engines(
    registered_bindings: list[tuple[Any, dict[str, Any]]],
) -> tuple[str, ...]:
    return tuple(sorted({
        str(publication.get("engine"))
        for _, publication in registered_bindings
    }))


def _capabilities_binding_has_slideshow(
    registered_bindings: list[tuple[Any, dict[str, Any]]],
) -> bool:
    return any(
        publication.get("engine") in _SLIDESHOW_ENGINES
        for _, publication in registered_bindings
    )


def _contracts_freshness_signature() -> tuple[str, int, int, int, int]:
    """Collision-safe stat signature for the ONE file load_format_contracts
    reads (services/content_format_contracts.py's CONTRACTS_PATH, or its
    CONTENT_LAB_FORMAT_CONTRACTS env override)."""
    configured = os.environ.get("CONTENT_LAB_FORMAT_CONTRACTS", "").strip()
    path = Path(configured).resolve() if configured else CONTRACTS_PATH
    return _file_stat_signature(path)


def _prompt_catalog_freshness_signature() -> tuple[tuple[str, int, int, int, int], ...]:
    """Collision-safe stat signature for EVERY file load_prompt_catalog can
    read for ANY format_slug: the base catalog, plus (when using the
    default, non-overridden catalog path) the silhouette and boat overlays
    it unconditionally reads and merges in regardless of which format_slug
    was asked for (services/control_plane_generation.py's
    load_prompt_catalog). Not per-format-slug precise — it always includes
    all three files, not just the one(s) a given format_slug's resolution
    would actually touch — which can only over-invalidate, never under-.
    """
    configured = os.environ.get("CONTENT_LAB_PROMPT_CATALOG", "").strip()
    base = Path(configured).resolve() if configured else CATALOG_PATH
    paths = [base]
    if not configured:
        paths.append(base.with_name("silhouette_stills.v1.json"))
        paths.append(base.with_name("boat_minimax.v1.json"))
    return tuple(_file_stat_signature(path) for path in paths)


def _capabilities_catalog_signature(
    registered_bindings: list[tuple[Any, dict[str, Any]]],
) -> tuple[tuple[str, int, int, int, int], ...]:
    """The prompt-catalog freshness signature, included in the cache key
    only when at least one registered binding is ai_video (the only engine
    that reads the prompt catalog) — no ai_video binding, no cost, same as
    the slideshow-only-when-present gate above."""
    if any(
        publication.get("engine") == "ai_video"
        for _, publication in registered_bindings
    ):
        return _prompt_catalog_freshness_signature()
    return ()


def _capabilities_cache_lookup(key: tuple[Any, ...]) -> list[dict[str, Any]] | None:
    with _capabilities_cache_lock:
        hit = _capabilities_cache.get(key)
    if hit is None:
        return None
    stored_at, entries = hit
    if time.monotonic() - stored_at > _CAPABILITIES_CACHE_TTL_SECONDS:
        return None
    return entries


def _capabilities_cache_store(key: tuple[Any, ...], entries: list[dict[str, Any]]) -> None:
    with _capabilities_cache_lock:
        if len(_capabilities_cache) >= _CAPABILITIES_CACHE_MAX_ENTRIES:
            # Simple, bounded eviction: a stale-key pileup across many
            # distinct pages/generations is rare (writes clear their own
            # generation's keys implicitly by changing the key), so a full
            # clear is cheap and keeps this O(1)-to-reason-about rather
            # than needing real LRU bookkeeping on the hot path.
            _capabilities_cache.clear()
        _capabilities_cache[key] = (time.monotonic(), entries)


def _generation_related_jobs_view(
    jobs_snapshot: "_JobsSnapshot", page_jobs_view: dict[str, Any], page_id: str,
) -> dict[str, Any]:
    """The narrowed view capabilities() hands the ai_video (generated)
    reservation helpers: this page's own jobs, unioned with every
    truck_master_recovery job (the only genuinely cross-page reservation
    those helpers need — see the comment above page_jobs_view in
    capabilities()). Factored out (Tides review of PR #185, round 2,
    defect 2) so a test can call the SAME function capabilities() calls,
    instead of rebuilding an equivalent-looking dict inline — a copy that
    would keep passing even if this line's real union were broken."""
    return {
        "jobs": {
            **page_jobs_view["jobs"],
            **jobs_snapshot.by_source_kind.get("truck_master_recovery", {}),
        },
        compaction.ARCHIVE_INDEX_KEY: _snapshot_archive_index(jobs_snapshot),
    }


def _snapshot_archive_index(jobs_snapshot: "_JobsSnapshot") -> dict[str, Any]:
    """The compacted store's archiveIndex (empty before any compaction).
    Every view capabilities() hands a reservation helper must carry it:
    those helpers read the archived no-repeat facts from the dict they are
    given, so a view without it would silently forget every archived job."""
    return jobs_snapshot.data.get(compaction.ARCHIVE_INDEX_KEY, {}) or {}


def _capabilities_job_views(
    jobs_snapshot: "_JobsSnapshot", page_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(page_jobs_view, dossier_source_dna_view) exactly as capabilities()
    builds them, each carrying the archiveIndex (see
    _snapshot_archive_index); factored out so a test calls the same code."""
    archive_index = _snapshot_archive_index(jobs_snapshot)
    return (
        {"jobs": jobs_snapshot.by_page.get(page_id, {}),
         compaction.ARCHIVE_INDEX_KEY: archive_index},
        {"jobs": jobs_snapshot.by_source_kind.get("dossier_source_dna", {}),
         compaction.ARCHIVE_INDEX_KEY: archive_index},
    )


@router.get("/v1/capabilities")
def capabilities(
    x_rt_page_id: str | None = Header(default=None),
    x_page_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Exact dossier recipes this page may run.

    The page id is required — the plane's client always sends it, and answering
    without one would let a caller enumerate every recipe in the Lab from a
    single unscoped request. The header is `X-RT-Page-Id`, which is what
    ContentLabClient#headers actually emits; `X-Page-Id` is accepted as an
    alias so this stays testable by hand with curl.

    Legacy project markers are executor inventory, not page capabilities. They
    are deliberately omitted here: advertising every shared project to every
    page is how a Coffee or POV page appears eligible for truck media. A row is
    returned only after the page's hash-bound dossier publication resolves
    through the closed Master Pages content-engine registry.
    """
    page_id = x_rt_page_id or x_page_id
    if not page_id or not PAGE_ID_RE.match(page_id):
        raise HTTPException(status_code=400, detail="X-RT-Page-Id header is required")

    current = _current_intent_for_capabilities(page_id)
    if current is None:
        return {"schema": RESPONSE_SCHEMA, "capabilities": []}
    master_pages, master_pages_hash = current

    entries = []
    try:
        jobs_snapshot = _read_jobs_snapshot_object()
    except _JobsDecodeFailed as error:
        # Fail closed: never answer from a snapshot whose signature no
        # longer matches the file (T3). The Worker already treats a 503 the
        # same as its own client timeout, so this degrades exactly like
        # today's behaviour instead of ever over-admitting maxQuantity.
        raise HTTPException(
            status_code=503, detail="job store temporarily unavailable",
        ) from error

    # Fetched before the job-store views below because they all feed the
    # cache key (see the comment above _capabilities_cache_lock): a cache
    # hit skips everything from page_jobs_view down through the whole
    # registered_bindings loop.
    registered_bindings = list_registered_recipe_bindings(
        page_id, master_pages, master_pages_hash,
    )
    try:
        profiles, registry_hash = load_engine_registry()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        # Fail closed: a malformed/drifted registry must surface as a service
        # failure, not an HTTP 200 with an empty (and cacheable) capability
        # list that silently stops all replenishment. The loader already
        # rejects empty/drifted registries (content_engine_registry.py), so
        # reaching here means the commissioned bindings cannot be resolved.
        log.error("content engine registry unavailable: %s", error)
        raise HTTPException(
            status_code=503, detail="engine_registry_unavailable",
        ) from error

    # A registered slideshow binding resolves through a LIVE networked
    # fetch with no cheap pre-fetch version signal at all (see the comment
    # above _capabilities_cache_lock) — never cached, never read from
    # cache. Checked before paying for the contracts/catalog stats below.
    cacheable = not _capabilities_binding_has_slideshow(registered_bindings)
    cache_key = None
    if cacheable:
        contracts_signature = _contracts_freshness_signature()
        catalog_signature = _capabilities_catalog_signature(registered_bindings)
        binding_engines = _capabilities_binding_engines(registered_bindings)
        cache_key = _capabilities_cache_key(
            page_id, jobs_snapshot.generation, master_pages_hash, registry_hash,
            contracts_signature, registered_bindings, binding_engines,
            catalog_signature,
        )
        cached_entries = _capabilities_cache_lookup(cache_key)
        if cached_entries is not None:
            return {"schema": RESPONSE_SCHEMA, "capabilities": list(cached_entries)}

    # capabilities() for one page only ever needs that page's own jobs, plus
    # (for the two async source-recipe kinds below) the jobs of the specific
    # sourceKinds those checks scope by. Slicing through the snapshot's
    # indices here means every helper below touches only its relevant slice
    # of the store instead of a full scan (C3) — none of their own bodies
    # change, since each already re-checks pageId/sourceKind/status itself,
    # so a narrower input can only ever produce the identical result.
    # All three views are O(page) / O(1): they union existing index buckets,
    # never copy or scan the whole store.
    #
    # generation_related_jobs_view used to be `by_source_kind["generated"] |
    # by_source_kind["truck_master_recovery"]` — store-wide over the
    # dominant, ever-growing "generated" kind, and the actual C3 violation a
    # Tides review caught (PR #185): the perf test's bindings=[] patch meant
    # nothing ever measured it. `_generated_unavailable_prompts` and the
    # second (candidate) loop of `_truck_master_candidates` both already
    # require pageId == page_id, so this page's own jobs are exactly the
    # "generated" jobs either function can ever match — `by_page[page_id]`
    # is the equivalent, page-scoped replacement. Only
    # `_truck_master_candidates`'s FIRST loop (the cross-page
    # truck_master_recovery reservation set) genuinely needs every page's
    # jobs of that one kind, so that bucket alone stays store-wide.
    page_jobs_view, dossier_source_dna_view = _capabilities_job_views(jobs_snapshot, page_id)
    archive_index = page_jobs_view[compaction.ARCHIVE_INDEX_KEY]
    generation_related_jobs_view: dict[str, Any] | None = None
    completed_import_identities = sorted({
        identity
        for job in page_jobs_view["jobs"].values()
        if isinstance(job, dict)
        and job.get("sourceKind") == "page_source_import"
        and job.get("status") == "completed"
        for identity in [canonical_source_identity(job.get("sourceUrl"))]
        if identity is not None
    } | set((archive_index.get("sourceIdentities") or {}).get(page_id) or []))
    # registered_bindings and profiles were already fetched above (they
    # feed the cache key). A page-bound intent is itself enough to expose
    # the commissioned format route before its first dossier publication.
    # The publication list below remains authoritative for versions already
    # registered; this bootstrap entry lets a new page reach the Dossier
    # editor instead of becoming an empty-capability dead end.
    intent_niche = master_pages.get("contentNiche")
    intent_engine = master_pages.get("contentEngine")
    bootstrap = next((profile for profile in profiles.values()
                      if profile.content_niche == intent_niche
                      and profile.content_engine == intent_engine
                      and profile.execution_status == "commissioned"), None)
    if bootstrap is not None and not registered_bindings:
        entries.append({
            "recipeId": f"{bootstrap.format_slug}:master",
            "engine": bootstrap.content_engine,
            "recipeVersion": bootstrap.format_contract_version,
            "maxQuantity": bootstrap.max_quantity,
            **({"sourceIdentities": completed_import_identities}
               if bootstrap.content_engine == "sourced_video" and completed_import_identities
               else {}),
        })
    # Sourced-video capacity is consumable inventory, not a static executor
    # ceiling. Load the durable job store lazily and only once for this request
    # so every sourced capability advertises the number of unique windows that
    # can actually be reserved right now. Without this, a 10-per-job executor
    # can advertise 10 after six of a ten-window master have already crossed the
    # API boundary; Control Plane then asks for six and the exact-quantity job
    # contract rejects the whole refill even though four safe windows remain.
    # The store was loaded above so completed page-source imports can contribute
    # their canonical identities to both bootstrap and registered capabilities.
    # A dossier version is executable only when its server-owned base prompt
    # family, exact provider model, runtime credential, and typed treatment are
    # all available. Registration alone never becomes a capability.
    for _, publication in registered_bindings:
        publication_engine = publication.get("engine")
        generation_recipe = (
            resolve_generation_recipe(publication)
            if publication_engine == "ai_video"
            else None
        )
        source_recipe = (
            _dossier_source_recipe(publication)
            if publication_engine == "sourced_video"
            else None
        )
        slideshow_recipe = (
            resolve_slideshow_recipe(publication)
            if publication_engine in {"sourced_slideshow", "lyrics_slideshows"}
            else None
        )
        if (
            generation_recipe is None
            and source_recipe is None
            and slideshow_recipe is None
        ):
            continue
        if slideshow_recipe is not None:
            try:
                library = load_syzygy_library(slideshow_recipe_profile := (
                    resolve_material_profile(publication, slideshow_recipe.recipe_spec)
                ), master_pages)
            except (SyzygyError, AttributeError):
                continue
            if slideshow_recipe_profile is None:
                continue
            unavailable = _slideshow_unavailable_signatures(
                page_jobs_view, slideshow_recipe, page_id,
            )
            max_quantity = len(plan_slideshows(
                slideshow_recipe, library, slideshow_recipe.max_quantity,
                unavailable,
                f"capability:{page_id}:{slideshow_recipe.executor_version}",
            ))
        elif source_recipe is not None:
            unavailable_slots = _source_dna_unavailable_slots(
                dossier_source_dna_view, source_recipe, publication["recipeVersion"],
            )
            max_quantity = len(plan_source_cuts(
                source_recipe, source_recipe.max_quantity, unavailable_slots,
            ))
        else:
            if generation_related_jobs_view is None:
                generation_related_jobs_view = _generation_related_jobs_view(
                    jobs_snapshot, page_jobs_view, page_id,
                )
            max_quantity = _generated_capability_quantity(
                generation_related_jobs_view, generation_recipe, page_id, master_pages,
                publication["recipeSpecHash"],
            )
        source_identities = None
        if source_recipe is not None:
            identities = [canonical_source_identity(master.provenance.get("sourceUrl")) for master in source_recipe.masters]
            identities.extend(completed_import_identities)
            if not identities or any(identity is None for identity in identities):
                continue
            source_identities = sorted(set(identities))
        registered_entry = {
            **({"sourceIdentities": source_identities} if source_identities is not None else {}),
            "recipeId": publication["recipeId"],
            "engine": publication["engine"],
            "recipeVersion": publication["recipeVersion"],
            "maxQuantity": max_quantity,
        }
        if not any(entry["recipeId"] == registered_entry["recipeId"]
                   and entry["engine"] == registered_entry["engine"]
                   and entry["recipeVersion"] == registered_entry["recipeVersion"]
                   for entry in entries):
            entries.append(registered_entry)
        if len(entries) >= MAX_CAPABILITIES:
            break

    if cacheable:
        _capabilities_cache_store(cache_key, entries)
    return {"schema": RESPONSE_SCHEMA, "capabilities": entries}


# Registry reads must not queue behind network-heavy capability/catalog work
# in Starlette's shared synchronous endpoint pool. Keep disk IO off the loop.
_FORMAT_READ_LIMITER = anyio.CapacityLimiter(2)


@router.get("/v1/format-contracts")
async def format_contract_status(
    x_rt_lane: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Machine-readable creative definition and commissioning status.

    The response contains no prompt text or secrets. It is service-authenticated
    because it is a fleet-wide read model for the control plane and dossier UI,
    not a public recipe catalog. Incomplete formats remain present with exact
    missing dimensions so the UI never substitutes a similarly named engine.
    """
    if x_rt_lane != CONTROL_PLANE_LANE:
        raise HTTPException(status_code=400, detail="X-RT-Lane header is invalid")
    require_control_plane_bearer(authorization)
    return await anyio.to_thread.run_sync(
        _format_contract_snapshot, limiter=_FORMAT_READ_LIMITER,
    )


def _format_contract_snapshot() -> dict[str, Any]:
    try:
        contracts, contracts_hash = load_format_contracts()
        profiles, registry_hash = load_engine_registry()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise HTTPException(
            status_code=503, detail="content format contracts are unavailable",
        ) from error

    formats = []
    for format_slug in sorted(contracts):
        contract = contracts[format_slug]
        profile = profiles[format_slug]
        formats.append({
            "formatSlug": format_slug,
            "contentNiche": contract.content_niche,
            "contentEngine": contract.content_engine,
            "materialSource": contract.material_source,
            "assetType": contract.asset_type,
            "definitionStatus": contract.definition_status,
            "executionStatus": profile.execution_status,
            "creativeAuthority": (
                {
                    "kind": contract.creative_authority.kind,
                    "id": contract.creative_authority.authority_id,
                    "version": contract.creative_authority.version,
                }
                if contract.creative_authority is not None else None
            ),
            "dimensions": contract.dimensions,
            "output": contract.output,
            "reviewAuthority": contract.review_authority,
            "reviewGates": list(contract.review_gates),
            "distribution": contract.distribution,
            "definitionGaps": list(contract.definition_gaps),
            "formatContractVersion": profile.format_contract_version,
            "executor": (
                {
                    "kind": profile.executor_kind,
                    "id": profile.executor_id,
                    "version": profile.executor_version,
                    "maxQuantity": profile.max_quantity,
                }
                if profile.execution_status == "commissioned" else None
            ),
        })
    return {
        "schema": "content-lab.format-contract-status.v1",
        "contractsRegistryVersion": "sha256:" + contracts_hash,
        "engineRegistryVersion": "sha256:" + registry_hash,
        "formats": formats,
    }


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
        # Historical Content Lab project linkage. It is roster context, never
        # an execution grant: only a hash-bound dossier publication resolved
        # through the content-engine registry becomes a capability.
        "project": page.get("project") or None,
        "tiktokUrl": page.get("tiktok_url") or None,
        "notionPageId": page.get("notion_page_id") or None,
        # "notion" for CRM-synced rows; null for legacy rows that predate the
        # Notion sync. The plane needs to know whose ontology a row speaks.
        "source": page.get("source") or None,
    }


def _roster_projection() -> tuple[dict[str, Any], bool]:
    """Build the one canonical Master Pages projection shared by both routes.

    Refresh returns only its count and content hash, while GET returns the rows.
    A consumer can therefore prove the snapshot it read is the exact projection
    produced by the completed refresh without comparing it to raw Notion rows.
    """
    from services.roster import ROSTER_PATH, list_all_pages

    pages: list[dict[str, Any]] = []
    for raw in list_all_pages():
        if raw.get("source") != "notion":
            continue
        page = _snapshot_page(raw)
        if page is not None:
            pages.append(page)
    pages.sort(key=lambda page: page["pageId"])
    projection_complete = len(pages) <= MAX_ROSTER_PAGES
    pages = pages[:MAX_ROSTER_PAGES]

    canonical = json.dumps(pages, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    version = "r" + digest[:12]

    captured_at = None
    try:
        captured_at = datetime.fromtimestamp(
            ROSTER_PATH.stat().st_mtime, timezone.utc
        ).isoformat()
    except OSError:
        # No cache, no freshness claim. The snapshot is empty in that case
        # anyway; the plane's reconcile treats an empty snapshot as "say
        # nothing", never as "the fleet is gone".
        pass

    return (
        {
            "schema": RESPONSE_SCHEMA,
            "snapshotVersion": version,
            "projectionHash": "sha256:" + digest,
            "capturedAt": captured_at,
            "pages": pages,
        },
        projection_complete,
    )


def _current_master_pages_intent(
    page_id: str,
    asserted: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str] | None:
    """Resolve current Notion intent onto one operational control-plane id.

    Content Lab's roster cache mints a stable local integration id, while the
    posting Rail already owns the durable page id used by policies, slots and
    buckets. When those ids differ, the immutable Notion page id plus handle
    are the source identity; the caller's page id is only the operational
    binding. All other Master Pages fields must still match exactly.
    """
    from services.roster import list_all_pages

    matches = []
    for raw in list_all_pages():
        snapshot = _snapshot_page(raw)
        if snapshot is None:
            continue
        exact_id = str(raw.get("integration_id") or "").strip() == page_id
        asserted_identity = (
            isinstance(asserted, dict)
            and isinstance(asserted.get("notionPageId"), str)
            and bool(asserted["notionPageId"].strip())
            and snapshot.get("notionPageId") == asserted.get("notionPageId")
            and str(snapshot.get("handle") or "").casefold()
                == str(asserted.get("handle") or "").casefold()
        )
        if not exact_id and not asserted_identity:
            continue
        candidate = {"schema": MASTER_PAGES_SCHEMA, **snapshot}
        candidate["pageId"] = page_id
        matches.append(candidate)
    if len(matches) != 1:
        return None
    canonical = canonical_intent(matches[0], expected_page_id=page_id)
    if canonical is None:
        return None
    return canonical, intent_hash(canonical)


def _current_intent_for_capabilities(
    page_id: str,
) -> tuple[dict[str, Any], str] | None:
    """Resolve canonical and pre-canonical operational capability callers.

    Canonical ids resolve directly from the roster.  An older operational id
    has no standalone roster row, so its own registered publication supplies
    only the immutable Notion identity needed to ask the same roster resolver.
    The returned intent is still rebuilt from the current roster and stale
    publication fields remain unable to pass the later binding check.
    """
    direct = _current_master_pages_intent(page_id)
    if direct is not None:
        return direct
    candidates: dict[str, tuple[dict[str, Any], str]] = {}
    for publication in list_registered_recipes(page_id):
        spec = typed_recipe_spec(publication)
        asserted = spec.get("masterPages") if isinstance(spec, dict) else None
        resolved = _current_master_pages_intent(page_id, asserted)
        if resolved is not None:
            candidates[resolved[1]] = resolved
    return next(iter(candidates.values())) if len(candidates) == 1 else None


@router.get("/v1/roster", dependencies=[Depends(require_roster_auth)])
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

    projection, _ = _roster_projection()
    return projection


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
    error_count = len(errors) if isinstance(errors, list) else 0
    projection, projection_complete = _roster_projection()
    return {
        "schema": RESPONSE_SCHEMA,
        "added": int(result.get("added", 0)),
        "updated": int(result.get("updated", 0)),
        "totalInNotion": int(result.get("total_in_notion", 0)),
        "projectedCount": len(projection["pages"]),
        "snapshotVersion": projection["snapshotVersion"],
        "projectionHash": projection["projectionHash"],
        "complete": error_count == 0 and projection_complete,
        "errorCount": error_count,
    }


# ── Jobs: new generation or approved-library selection, never both ──────────
#
# Legacy registered projects remain approved footage libraries. Source-DNA
# windows are reserved while a job is queued/running and become permanently
# unavailable only when the job completes; a failed executor releases its
# unrendered positions so transport/runtime failures cannot drain the master.
# A page-scoped dossier publication resolves either the hash-pinned generation
# catalog or one closed, exact-version approved-library binding. Both execute
# in an isolated job root. A dry library never falls through to spend, and an
# unavailable executor never falls back to a different source.
#
# The plane's job contract rejects free-form prompt fields; this side
# mirrors that rejection so a crafted body dies on whichever side it hits
# first. Job records are DURABLE (a JSON store under a lock), unlike the
# in-memory dicts the interactive /api/video routes use — a Railway
# restart must not forget what was served, or a page gets duplicates.

import secrets as _secrets

from fastapi import Body, Request
from fastapi.responses import FileResponse

from services.json_store import atomic_load, atomic_save
from services.generation_recovery import PredictionCheckpoint, runner_lock, store_lock as lock_for
from services import job_store_compaction as compaction

JOBS_STORE_NAME = "control_plane_jobs.json"
JOB_ID_PREFIX = "cpl-"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".m4v"}
MAX_JOB_QUANTITY = 100
MAX_JOB_BODY_BYTES = 16_384
MAX_GENERATION_JOB_BODY_BYTES = 512_000
# Long creator masters are deliberately retained for recurring replenishment.
# Their bounded download plus normalization can legitimately outlast the old
# twenty-minute one-clip deadline, so do not recycle a healthy import midway.
SOURCE_IMPORT_ACTIVE_DEADLINE_SECONDS = 6 * 60 * 60
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,200}$")
JOB_TOKEN_BYTES = 24
GENERATION_ACTIVE_STATUSES = {"queued", "running"}
SOURCE_DNA_UNAVAILABLE_STATUSES = compaction.SOURCE_DNA_UNAVAILABLE_STATUSES
ASYNC_SOURCE_KINDS = {
    "generated", "dossier_source_dna", "truck_master_recovery",
    "page_source_import", "syzygy_slideshow",
}
TRUCK_RECIPE_ID = "truck-scenic:master"
TRUCK_CROP_MODE = "both"
TRUCK_CROP_COUNT = 5
_GENERATION_RUNTIME_ID = _secrets.token_hex(16)
_source_cache_locks: dict[str, asyncio.Lock] = {}
# Persistent source-master cache (_generation_root()/_source_dna) byte ceiling.
# Every previously-unseen master is os.replace()d in forever; without a budget
# the Railway volume fills monotonically. Two 20 GB masters plus headroom is a
# safe default; operators may lower it to match a smaller volume.
_DEFAULT_SOURCE_DNA_CACHE_BYTES = 50 * 1024 * 1024 * 1024

# Defense-in-depth mirror of the plane's assertNoFreeFormPrompt: no field
# anywhere in the job body may look like prompt text. Recipe authoring
# stays in the Lab; the plane picks recipes, never words.
PROMPT_FIELD_RE = re.compile(r"(prompt|instruction|message)", re.IGNORECASE)

JOB_FIELDS = {
    "pageId", "lane", "engine", "lockedRecipeId", "recipeVersion",
    "quantity", "constraints", "sourceIsolation", "policyHash",
    "masterPages", "masterPagesHash",
}
SOURCE_IMPORT_FIELDS = {
    "schema", "pageId", "format", "sourceUrl", "masterPages",
    "masterPagesHash",
}


def _jobs_path() -> Path:
    # Same data dir as the roster cache (Railway volume in production).
    from services.roster import ROSTER_PATH
    return ROSTER_PATH.parent / JOBS_STORE_NAME


def _empty_jobs() -> dict[str, Any]:
    return {"version": 1, "jobs": {}, "byIdempotency": {}, "served": {}}


def _idempotency_job_id(store: dict[str, Any], key: str) -> Any:
    """Return a live job id, or refuse a key whose job was archived."""
    existing_id = store["byIdempotency"].get(key)
    if existing_id is not None and existing_id not in store["jobs"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "idempotency_key_archived",
                "jobId": existing_id,
            },
        )
    return existing_id


# Writer-intent signal (Tides review of PR #185, round 2, defect 3): a
# plain int, read/written without a lock. Under the GIL a simple int
# increment/decrement/comparison cannot tear, and this is advisory only —
# readers use it to voluntarily yield, never to gate correctness. Covers
# _load_jobs (the full json.loads of a growing file) and _save_jobs (the
# full json.dumps + fsync); the short, pure-Python mutation a caller does
# between those two calls is not covered; and it's set from EVERY writer
# call site since capabilities() never calls _load_jobs itself (only
# writers do — enforced by test_capabilities_use_snapshot_not_mutable_
# transaction_load), so this can safely live inside those two functions
# rather than needing every one of the 11 write call sites touched.
_jobs_writer_active_count = 0


def _jobs_writer_enter() -> None:
    global _jobs_writer_active_count
    _jobs_writer_active_count += 1


def _jobs_writer_exit() -> None:
    global _jobs_writer_active_count
    _jobs_writer_active_count -= 1


def _load_jobs() -> dict[str, Any]:
    _jobs_writer_enter()
    try:
        data = atomic_load(_jobs_path(), default=None)
    finally:
        _jobs_writer_exit()
    if not isinstance(data, dict) or "jobs" not in data:
        return _empty_jobs()
    return data


class _JobsDecodeFailed(Exception):
    """The job-history file changed but could not be safely decoded.

    Raised instead of ever answering from a snapshot whose signature no
    longer matches the file on disk (fail closed: under-admitting an empty
    read is safe, silently serving stale/mismatched data is not).
    """


class _PendingJobsDecode:
    """Single-flight ticket: every concurrent reader of the same file
    signature shares the one thread actually doing the decode."""

    __slots__ = ("event", "snapshot", "error")

    def __init__(self) -> None:
        self.event = Event()
        self.snapshot: "_JobsSnapshot | None" = None
        self.error: BaseException | None = None


class _JobsSnapshot:
    """One immutable, fully-decoded view of the job-history file.

    ``by_page`` and ``by_source_kind`` are jobId->job sub-dicts pointing at
    the SAME job objects held in ``data["jobs"]`` (no copies) so a caller
    that needs "every job for this page" or "every job of this sourceKind"
    touches only that slice instead of the whole store.
    """

    __slots__ = ("signature", "generation", "data", "by_page", "by_source_kind")

    def __init__(
        self,
        signature: tuple[int, int, int, int],
        generation: int,
        data: dict[str, Any],
        by_page: dict[str, dict[str, Any]],
        by_source_kind: dict[str, dict[str, Any]],
    ) -> None:
        self.signature = signature
        self.generation = generation
        self.data = data
        self.by_page = by_page
        self.by_source_kind = by_source_kind


# _jobs_snapshot_lock guards ONLY the swap of the _jobs_snapshot reference
# (an in-memory pointer assignment) — never a decode and never disk IO. A
# reader can therefore never block behind another thread's file read for
# longer than that swap (C4).
_jobs_snapshot_lock = Lock()
_jobs_snapshot: "_JobsSnapshot | None" = None

# Monotonic, process-local, drawn once per *observed* file version — at
# write-publish time, or the moment a reader first notices a new on-disk
# signature (never at decode completion). That ordering is what makes the
# CAS publish below correct: a decode that started against an older file
# version can never stomp a newer snapshot, no matter how long it takes to
# finish, because the newer snapshot already drew a larger generation number
# before that slow decode's result comes back (T2).
_jobs_generation_lock = Lock()
_jobs_generation_counter = 0

# Coalesces concurrent decodes of the same file signature into one (C4).
# Entries are removed the instant their decode finishes, so this dict never
# grows past "distinct file versions currently mid-decode" (C7).
_jobs_decode_dispatch_lock = Lock()
_jobs_decode_pending: dict[tuple[int, int, int, int], _PendingJobsDecode] = {}


def _jobs_signature(stat: os.stat_result) -> tuple[int, int, int, int]:
    # (inode, mtime, size, ctime): the CACHE-KEY signature, compared against
    # a fresh path.stat() on every single request — no TTL, no "every N
    # seconds" staleness window (T1). ctime is kept (base compared it too)
    # so an in-place rewrite that preserves both size and mtime (`cp -p`,
    # `touch -r`, a restore tool) is still caught — ctime moves on any
    # inode metadata change, including a content rewrite that leaves mtime
    # alone. Do NOT reuse this for the mid-read consistency check inside
    # _decode_jobs_snapshot — see _jobs_read_consistency_signature.
    return (stat.st_ino, stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns)


def _jobs_read_consistency_signature(stat: os.stat_result) -> tuple[int, int, int]:
    # Deliberately excludes ctime. This is fstat'd on the SAME open file
    # descriptor before and after json.load(), to catch "the bytes I am
    # reading changed under me mid-read" — not "is my cached snapshot still
    # current" (that's _jobs_signature, above). An external atomic_save
    # unlinks whatever was previously at this path as part of its
    # os.replace; if we already hold that old inode open via this fd, the
    # unlink alone bumps ITS ctime (nlink dropping to 0 is an inode
    # metadata change) even though our fd's mtime/size/content are
    # completely untouched and what we already read remains fully valid.
    # Including ctime here produced exactly that false positive — a
    # perfectly good decode raising _JobsDecodeFailed — under any external
    # replace that lands mid-read.
    return (stat.st_ino, stat.st_mtime_ns, stat.st_size)


def _next_jobs_generation() -> int:
    global _jobs_generation_counter
    with _jobs_generation_lock:
        _jobs_generation_counter += 1
        return _jobs_generation_counter


def _build_jobs_indices(
    data: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Per-page and per-sourceKind jobId->job indices, built in one pass.

    Built once per snapshot (on decode, or on a write's direct publish) —
    never per capabilities request (C3).
    """
    by_page: dict[str, dict[str, Any]] = {}
    by_source_kind: dict[str, dict[str, Any]] = {}
    for job_id, job in data.get("jobs", {}).items():
        if not isinstance(job, dict):
            continue
        page_id = job.get("pageId")
        if isinstance(page_id, str) and page_id:
            by_page.setdefault(page_id, {})[job_id] = job
        source_kind = job.get("sourceKind")
        if isinstance(source_kind, str) and source_kind:
            by_source_kind.setdefault(source_kind, {})[job_id] = job
    return by_page, by_source_kind


def _publish_jobs_snapshot(snapshot: "_JobsSnapshot") -> "_JobsSnapshot":
    """Install ``snapshot`` only if it is not older than the one live now
    (compare-and-swap on ``generation``, T2). Returns whichever snapshot is
    live after the attempt, so a stale caller still gets a consistent read.
    """
    global _jobs_snapshot
    with _jobs_snapshot_lock:
        current = _jobs_snapshot
        if current is None or snapshot.generation > current.generation:
            _jobs_snapshot = snapshot
            return snapshot
        return current


def _save_jobs(store: dict[str, Any]) -> None:
    """Persist ``store`` and publish it straight to the read cache (C1).

    Every writer already holds ``lock_for(_jobs_path())`` and already owns
    the exact dict that is about to become durable truth — decoding it back
    off disk just to answer the next capabilities read would be pure waste.
    Build the snapshot + index directly from this in-memory dict instead.
    `_load_jobs` (a fresh decode) remains what every writer calls to start
    its own read-modify-write transaction; only the published READ cache is
    short-circuited here. Mutations stay the source of truth (C6) — this
    function does not change what gets written, only what gets cached after.
    """
    path = _jobs_path()
    _jobs_writer_enter()
    try:
        atomic_save(path, store)
        try:
            signature = _jobs_signature(path.stat())
        except OSError:
            # Publish is best-effort: the next reader's own stat()+decode
            # will resync regardless.
            return
        generation = _next_jobs_generation()
        by_page, by_source_kind = _build_jobs_indices(store)
        _publish_jobs_snapshot(_JobsSnapshot(signature, generation, store, by_page, by_source_kind))
    finally:
        _jobs_writer_exit()


def _decode_jobs_snapshot(generation: int) -> "_JobsSnapshot":
    """Decode the file off the fast path. Never called under
    ``_jobs_snapshot_lock`` (C4) — only the final CAS publish takes that
    lock, and only for the reference swap itself.
    """
    path = _jobs_path()
    try:
        with path.open("r", encoding="utf-8") as handle:
            before_stat = os.fstat(handle.fileno())
            before = _jobs_read_consistency_signature(before_stat)
            data = json.load(handle)
            fd_stat = os.fstat(handle.fileno())
            after = _jobs_read_consistency_signature(fd_stat)
    except FileNotFoundError:
        # Vanished between our stat() and open(): legitimately empty, not a
        # decode failure.
        return _JobsSnapshot((0, 0, 0, 0), generation, _empty_jobs(), {}, {})
    except (OSError, ValueError) as error:
        raise _JobsDecodeFailed(str(error)) from error
    # A same-(ino,mtime,size) in-place rewrite (`cp -p`, `touch -r`, a
    # restore tool that preserves mtime) passes the check above but still
    # touches this inode's ctime — UNLESS it was replaced out from under us
    # (os.replace unlinks the old name; nlink drops to 0 on our still-open
    # fd, and THAT alone moves ctime too, which is fine and already
    # tolerated: the bytes we read are one complete, valid old version).
    # Reject only when the inode is STILL linked and ctime moved: that
    # combination means someone rewrote the content in place while we were
    # reading it, and letting it through would publish stale bytes keyed to
    # exactly the signature the file now reports (T1; a Tides review of PR
    # #185, round 2, caught this — serving stale content persistently after
    # the file settled).
    in_place_rewrite = (
        fd_stat.st_nlink > 0 and fd_stat.st_ctime_ns != before_stat.st_ctime_ns
    )
    if (
        before != after
        or in_place_rewrite
        or not isinstance(data, dict)
        or "jobs" not in data
    ):
        raise _JobsDecodeFailed("job-history file changed mid-read or is malformed")
    # The full (ctime-including) signature is what gets published/compared
    # as this snapshot's cache key — computed from the same final fstat,
    # just with the extra T1 discriminator the read-consistency check must
    # not use (see _jobs_read_consistency_signature).
    signature = _jobs_signature(fd_stat)
    by_page, by_source_kind = _build_jobs_indices(data)
    return _JobsSnapshot(signature, generation, data, by_page, by_source_kind)


def _read_jobs_snapshot_object() -> "_JobsSnapshot":
    """Read-only capability input; mutations always use fresh _load_jobs().

    Every call does a cheap ``stat()`` — no TTL. If the signature matches
    what is already published, return it with no lock and no decode. If not,
    a single reader decodes (outside any lock) while the rest wait on that
    one in-flight decode (C4); the result is published only if it is not
    older than whatever is live by the time it finishes (C2/T2). A decode
    failure never poisons the cache and never resolves to an answer keyed by
    a signature that does not match the file (T3) — it raises instead, and
    the caller (capabilities()) turns that into a 503.
    """
    if _jobs_writer_active_count > 0:
        # Writer priority (Tides review of PR #185, round 2, defect 3):
        # a bare GIL yield, not a real wait — this thread stays runnable,
        # it just gives a currently-mid-flight writer's own json
        # encode/decode a fairer shot at the interpreter instead of losing
        # every reacquisition race to however many reader threads are
        # spinning through this exact call under a zero-gap burst. Cheap
        # (no lock, no syscall beyond what sleep(0) already is) and never
        # affects correctness — a stale read here is impossible either way,
        # the signature check right below still runs every time.
        time.sleep(0.002)
    path = _jobs_path()
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        # Not published to _jobs_snapshot (there is nothing to key it by),
        # but the generation must still be freshly drawn, not a fixed
        # sentinel: capabilities()'s own response cache keys on this
        # generation too, and two distinct callers observing "no file yet"
        # are two distinct observations, not the same one. A fixed 0 here
        # made every "file doesn't exist yet" answer, on any page, collide
        # in that cache — found via cross-test pollution in the full suite
        # after the capabilities()-level cache was added (Tides review of
        # PR #185, round 2).
        return _JobsSnapshot((0, 0, 0, 0), _next_jobs_generation(), _empty_jobs(), {}, {})
    except OSError as error:
        raise _JobsDecodeFailed(str(error)) from error

    signature = _jobs_signature(stat_result)
    current = _jobs_snapshot
    if current is not None and current.signature == signature:
        return current

    with _jobs_decode_dispatch_lock:
        current = _jobs_snapshot
        if current is not None and current.signature == signature:
            return current
        pending = _jobs_decode_pending.get(signature)
        if pending is None:
            pending = _jobs_decode_pending[signature] = _PendingJobsDecode()
            is_leader = True
        else:
            is_leader = False

    if not is_leader:
        # Bounded wait: the leader's own try/finally below already
        # guarantees the event fires for every normal exception. This
        # timeout is a defensive backstop only, against something outside
        # ordinary Python exception handling (a forcibly killed thread, a
        # process-level crash) — fail closed with a 503-mapped error rather
        # than pin this server thread on `.wait()` forever.
        if not pending.event.wait(timeout=5):
            raise _JobsDecodeFailed(
                f"timed out waiting for an in-flight decode of {signature}",
            )
        if pending.error is not None:
            raise pending.error
        return pending.snapshot

    try:
        # Drawn inside the try (Tides review of PR #185, round 2, INFO
        # note): _next_jobs_generation() cannot practically raise (a Lock
        # plus an int increment), but if it ever did, this keeps the same
        # try/finally guarantee — the ticket is still released — rather
        # than leaving one line of the leader's critical section outside
        # the safety net the rest of this block relies on.
        generation = _next_jobs_generation()
        snapshot = _decode_jobs_snapshot(generation)
        published = _publish_jobs_snapshot(snapshot)
    except _JobsDecodeFailed as error:
        pending.error = error
        raise
    except BaseException as error:
        # Any other failure (MemoryError on a huge store, a bug in
        # _build_jobs_indices, ...) must still release every follower
        # waiting on this ticket — T3's "never leave a reader blocked
        # behind a decode" applies to a decode that fails unexpectedly, not
        # only to a clean _JobsDecodeFailed. Normalize it to the same
        # fail-closed 503 contract rather than let an exotic exception type
        # escape uncaught by capabilities().
        wrapped = _JobsDecodeFailed(f"unexpected decode failure: {error!r}")
        pending.error = wrapped
        raise wrapped from error
    else:
        pending.snapshot = published
        return published
    finally:
        # Always — success, _JobsDecodeFailed, or anything else — clear the
        # ticket and wake every follower. A leaked ticket here is exactly
        # the regression this fixes: every later reader of this signature
        # would otherwise block on pending.event.wait() forever (base's
        # `with`-locked design released on any exception; this must too).
        with _jobs_decode_dispatch_lock:
            _jobs_decode_pending.pop(signature, None)
        pending.event.set()


def _read_jobs_snapshot() -> dict[str, Any]:
    """Back-compat accessor: the decoded store only, no index. Prefer
    ``_read_jobs_snapshot_object()`` when the per-page/per-sourceKind index
    is needed too (capabilities() does)."""
    return _read_jobs_snapshot_object().data


# ── job-store compaction (bounds the growing file, inside the writer lock) ──
# See services/job_store_compaction.py for the pure transform + index rules.
# This side is the IO/scheduling glue: it runs under lock_for(_jobs_path()),
# appends the archive idempotently, then atomic-replaces the compacted store.
# A crash between the archive append and the store commit is safe: the retry
# skips already-archived ids (idempotent archive) and still commits the
# compacted store. A crash during atomic_save leaves old or new store valid.

_COMPACTION_THREAD: threading.Thread | None = None
_COMPACTION_STOP = threading.Event()
_COMPACTION_INTERVAL_SECONDS = 6 * 60 * 60  # re-check at least every 6 h
_COMPACTION_TMP_RE = re.compile(r"\.(\d+)\.\d+\.tmp$")


def _sweep_stale_compaction_tmps(path: Path) -> int:
    """Reap *.tmp sidecars older than 1 h that aren't the live writer's own."""
    now = time.time()
    try:
        siblings = list(path.parent.glob(f"{path.name}.*.tmp"))
    except OSError:
        return 0
    removed = 0
    for tmp in siblings:
        m = _COMPACTION_TMP_RE.search(tmp.name)
        if not m or int(m.group(1)) == os.getpid():
            continue
        try:
            if now - tmp.stat().st_mtime < 3600:
                continue
            tmp.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def run_compaction_once(
    path: Path,
    now: datetime | None = None,
    *,
    retention_days: int = compaction.DEFAULT_RETENTION_DAYS,
    batch_size: int = compaction.DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    """Compact one bounded batch under the writer lock. Returns a summary."""
    expected_path = _jobs_path()
    assert path == expected_path, "compaction path must be the canonical jobs store"
    now = now or datetime.now(timezone.utc)
    with lock_for(path):
        store = _load_jobs()
        before = len(store.get("jobs", {}))
        try:
            if compaction.ARCHIVE_INDEX_KEY not in store:
                archives = [
                    candidate
                    for candidate in path.parent.glob(f"{path.name}.archive-*.jsonl")
                    if candidate.is_file()
                ]
                if archives:
                    raise compaction.ArchiveIndexUnreadable(
                        f"missing_with_archives count={len(archives)}",
                    )
            new_store, archived = compaction.compact_job_store(
                store, now, retention_days=retention_days, batch_size=batch_size,
            )
        except compaction.ArchiveIndexUnreadable as error:
            log.error(
                "ALERT-LABJOBSTORE index_unreadable reason=%s",
                error.reason,
            )
            raise
        if not archived:
            return {"archived": 0, "jobs_before": before, "jobs_after": before,
                    "tmp_swept": _sweep_stale_compaction_tmps(path)}
        compaction.append_archive_records(path, archived, now)
        _save_jobs(new_store)  # #185 C1: publish the compacted snapshot too
        return {
            "archived": len(archived),
            "jobs_before": before,
            "jobs_after": len(new_store.get("jobs", {})),
            "tmp_swept": _sweep_stale_compaction_tmps(path),
        }


def maybe_compact_jobs(now: datetime | None = None, *, force: bool = False) -> dict[str, Any]:
    """Run compaction when the file exceeds the size bound (or when forced)."""
    now = now or datetime.now(timezone.utc)
    path = _jobs_path()
    try:
        size = path.stat().st_size
    except OSError:
        return {"archived": 0, "jobs_before": 0, "jobs_after": 0, "tmp_swept": 0}
    if not force and size < compaction.DEFAULT_SIZE_THRESHOLD_BYTES:
        # Keep the tmp sidecars tidy even when the store is small.
        return {"archived": 0, "jobs_before": 0, "jobs_after": 0,
                "tmp_swept": _sweep_stale_compaction_tmps(path)}
    return run_compaction_once(path, now)


def _compaction_loop() -> None:
    # First pass is forced so an interrupted prior compaction self-heals even
    # when the file is already under the threshold; later passes only fire on
    # size, so a healthy small store does no extra decode work.
    first = True
    while True:
        try:
            if first:
                maybe_compact_jobs(force=True)
                first = False
            else:
                maybe_compact_jobs()
        except Exception:  # compaction must never take the process down
            log.exception("job-store compaction pass failed")
        if _COMPACTION_STOP.wait(_COMPACTION_INTERVAL_SECONDS):
            break


def start_compaction_scheduler() -> None:
    global _COMPACTION_THREAD
    if _COMPACTION_THREAD is not None and _COMPACTION_THREAD.is_alive():
        return
    _COMPACTION_STOP.clear()
    _COMPACTION_THREAD = threading.Thread(
        target=_compaction_loop, name="content-lab-job-compaction", daemon=True,
    )
    _COMPACTION_THREAD.start()


def stop_compaction_scheduler() -> None:
    _COMPACTION_STOP.set()


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


def _source_dna_unavailable_slots(
    store: dict[str, Any], source_recipe: Any, recipe_version: str,
) -> set[str]:
    """Derive reservations from durable job truth, never a write-only ledger.

    Queued/running jobs reserve their exact windows across recipe revisions so
    concurrent work cannot cut the same source position. Every queued, running
    or completed cut also reserves its exact time frame
    (``masterSha:startMs:durationMs``) for every recipe revision and library
    version that contains the same master bytes: a new recipe re-treats fresh
    time frames instead of re-cutting the ones already delivered. Failed jobs
    release their windows.
    """
    master_shas = {master.sha256 for master in source_recipe.masters}
    slots: set[str] = set()
    for job in store.get("jobs", {}).values():
        if (
            not isinstance(job, dict)
            or job.get("sourceKind") != "dossier_source_dna"
            or job.get("status") not in SOURCE_DNA_UNAVAILABLE_STATUSES
        ):
            continue
        same_library_slot = (
            job.get("sourceLibraryId") == source_recipe.source_library_id
            and job.get("sourceLibraryHash") == source_recipe.source_library_hash
            and (
                job.get("status") != "completed"
                or job.get("recipeVersion") == recipe_version
            )
        )
        for cut in job.get("sourceCuts", []):
            if not isinstance(cut, dict):
                continue
            slot_id = cut.get("slotId")
            if same_library_slot and isinstance(slot_id, str) and slot_id:
                slots.add(slot_id)
            master_sha = cut.get("masterSha256")
            start_ms, duration_ms = cut.get("startMs"), cut.get("durationMs")
            if (
                master_sha in master_shas
                and type(start_ms) is int and type(duration_ms) is int
            ):
                slots.add(f"{master_sha}:{start_ms}:{duration_ms}")
    # Archived completed source cuts keep their permanent reservations. The
    # exact time frame is reserved forever; the library slot id stays reserved
    # only across the same recipe revision (mirroring the live rule).
    for cut in (store.get(compaction.ARCHIVE_INDEX_KEY, {}) or {}).get("sourceDnaCuts", []):
        if not isinstance(cut, dict):
            continue
        same_library_slot = (
            cut.get("sourceLibraryId") == source_recipe.source_library_id
            and cut.get("sourceLibraryHash") == source_recipe.source_library_hash
            and cut.get("recipeVersion") == recipe_version
        )
        slot_id = cut.get("slotId")
        if same_library_slot and isinstance(slot_id, str) and slot_id:
            slots.add(slot_id)
        master_sha = cut.get("masterSha256")
        start_ms, duration_ms = cut.get("startMs"), cut.get("durationMs")
        if (
            master_sha in master_shas
            and type(start_ms) is int and type(duration_ms) is int
        ):
            slots.add(f"{master_sha}:{start_ms}:{duration_ms}")
    return slots


def _slideshow_unavailable_signatures(
    store: dict[str, Any], recipe: Any, page_id: str,
) -> set[str]:
    """Reserve every slideshow plan that may already have crossed the API."""
    signatures: set[str] = set()
    for job in store.get("jobs", {}).values():
        if (
            not isinstance(job, dict)
            or job.get("sourceKind") != "syzygy_slideshow"
            or job.get("pageId") != page_id
            or job.get("sourceLibraryId") != recipe.library_id
            or job.get("status") not in SOURCE_DNA_UNAVAILABLE_STATUSES
        ):
            continue
        for plan in job.get("slideshowPlan", []):
            signature = plan.get("signature") if isinstance(plan, dict) else None
            if isinstance(signature, str) and re.fullmatch(r"[0-9a-f]{64}", signature):
                signatures.add(signature)
    # Archived slideshow plans keep their permanent reservations.
    for entry in (store.get(compaction.ARCHIVE_INDEX_KEY, {}) or {}).get("slideshow", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("pageId") == page_id and entry.get("sourceLibraryId") == recipe.library_id:
            signature = entry.get("signature")
            if isinstance(signature, str) and re.fullmatch(r"[0-9a-f]{64}", signature):
                signatures.add(signature)
    return signatures


def _generated_unavailable_prompts(
    store: dict[str, Any], recipe: Any, page_id: str,
) -> tuple[set[str], set[str]]:
    """Reserve approved prompts only while generation is in flight.

    A completed prompt may generate fresh media again; prompt text is not a
    delivered asset. Existing job idempotency and output-byte admission still
    prevent replaying completed work as a new delivery. Active hashes remain
    reserved across recipe revisions, while legacy slot identities are scoped
    to their original catalog family.
    """
    hashes: set[str] = set()
    slot_signatures: set[str] = set()
    for job in store.get("jobs", {}).values():
        if (
            not isinstance(job, dict)
            or job.get("sourceKind") != "generated"
            or job.get("pageId") != page_id
            or job.get("status") not in GENERATION_ACTIVE_STATUSES
        ):
            continue
        for item in job.get("promptPlan", []):
            digest = item.get("promptHash") if isinstance(item, dict) else None
            if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
                hashes.add(digest)
        # Older jobs carried selected slot values on their clips. Preserve
        # those reservations while the job is active without reconstructing
        # or persisting raw prompt text.
        for clip in job.get("clips", []):
            if not isinstance(clip, dict):
                continue
            digest = clip.get("promptHash")
            if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
                hashes.add(digest)
            if (
                job.get("promptCatalogHash") == recipe.prompt_catalog_hash
                and job.get("family") == recipe.family_name
                and job.get("providerModel") == recipe.provider_model
            ):
                slots = clip.get("promptSlots")
                if isinstance(slots, dict) and all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in slots.items()
                ):
                    slot_signatures.add(json.dumps(
                        slots, sort_keys=True, separators=(",", ":"),
                    ))
    return hashes, slot_signatures


def _generated_capability_quantity(
    store: dict[str, Any], recipe: Any, page_id: str,
    master_pages: dict[str, Any], recipe_spec_hash: str,
) -> int:
    """Advertise only fresh output the current recipe can reserve now."""
    unavailable_hashes, unavailable_slots = _generated_unavailable_prompts(
        store, recipe, page_id,
    )
    available_calls = len(plan_prompt_combinations(
        recipe,
        f"capability:{page_id}:{recipe.prompt_catalog_hash}:{recipe.family_name}",
        recipe.planned_provider_calls(MAX_CAPABILITY_QUANTITY),
        unavailable_hashes,
        unavailable_slots,
    ))
    fresh_capacity = min(
        MAX_CAPABILITY_QUANTITY,
        available_calls * recipe.clips_per_generation,
    )
    recovery_capacity = 0
    if (
        recipe.recipe_id == TRUCK_RECIPE_ID
        and str(master_pages.get("contentNiche") or "").strip().upper() == "TRUCK"
    ):
        recovery_limit = recipe.planned_provider_calls(MAX_CAPABILITY_QUANTITY)
        recovery_capacity = min(
            MAX_CAPABILITY_QUANTITY,
            len(_truck_master_candidates(
                store, page_id, recovery_limit,
                content_engine=recipe.engine,
                recipe_id=recipe.recipe_id,
                generation_recipe=recipe,
                current_recipe_spec_hash=recipe_spec_hash,
            )) * recipe.clips_per_generation,
        )
    return max(fresh_capacity, recovery_capacity)


def _truck_master_candidates(
    store: dict[str, Any], page_id: str, limit: int, *,
    content_engine: str, recipe_id: str, generation_recipe: Any,
    current_recipe_spec_hash: str,
) -> list[dict[str, Any]]:
    """Return durable, unused, current-authority truck masters for re-cropping.

    A completed recovery reserves its exact masters permanently. Active
    recoveries reserve them against concurrent replenishment. Failed recovery
    jobs release them because no new delivery bytes crossed the API boundary.
    A landscape file alone is not enough: the producing job must match the
    current recipe, engine registry, prompt catalog, executor and provider
    model. This prevents old-model or old-prompt renders from silently becoming
    new five-crop deliveries after the page's creative authority changes. The
    source files are checked again, byte-for-byte, by the recovery runner.
    Applied-video evidence must also match the current recipe hash, requested
    grade, speed and crop; otherwise fresh generation produces new evidence.
    """
    reserved: set[str] = set()
    jobs = store.get("jobs", {})
    archive_index = store.get(compaction.ARCHIVE_INDEX_KEY, {}) or {}
    for job in jobs.values():
        if (
            not isinstance(job, dict)
            or job.get("sourceKind") != "truck_master_recovery"
            or job.get("status") not in SOURCE_DNA_UNAVAILABLE_STATUSES
        ):
            continue
        for master in job.get("recoveryMasters", []):
            sha256 = master.get("sha256") if isinstance(master, dict) else None
            if isinstance(sha256, str) and re.fullmatch(r"[0-9a-f]{64}", sha256):
                reserved.add(sha256)
    # Archived completed recoveries reserve their masters permanently.
    for sha256 in archive_index.get("truckRecoveryMasters", []):
        if isinstance(sha256, str) and re.fullmatch(r"[0-9a-f]{64}", sha256):
            reserved.add(sha256)

    candidates: list[dict[str, Any]] = []
    seen = set(reserved)
    # Archived completed generated truck renders remain live re-crop candidates
    # (full manifests preserved in the index); include them in the ordered scan.
    ordered_jobs = sorted(
        [job for job in jobs.values() if isinstance(job, dict)]
        + [job for job in archive_index.get("truckCandidateJobs", []) if isinstance(job, dict)],
        key=lambda job: (str(job.get("createdAt") or ""), str(job.get("jobId") or "")),
    )
    for job in ordered_jobs:
        if (
            job.get("pageId") != page_id
            or job.get("sourceKind") != "generated"
            or job.get("status") != "completed"
            or job.get("engine") != content_engine
            or job.get("recipeId") != recipe_id
            or job.get("engineRegistryHash") != generation_recipe.engine_registry_hash
            or job.get("formatContractVersion") != generation_recipe.format_contract_version
            or job.get("executorVersion") != generation_recipe.executor_version
            or job.get("promptCatalogHash") != generation_recipe.prompt_catalog_hash
            or job.get("providerModel") != generation_recipe.provider_model
            or not isinstance(job.get("artifactRoot"), str)
        ):
            continue
        root = Path(job["artifactRoot"]).resolve()
        for clip in job.get("clips", []):
            if not isinstance(clip, dict) or isinstance(clip.get("delivery"), dict):
                continue
            rel_path = clip.get("path")
            sha256 = clip.get("sha256")
            byte_count = clip.get("bytes")
            source = clip.get("source")
            if (
                not isinstance(rel_path, str)
                or re.search(r"_crop[0-9]+\.mp4$", rel_path)
                or not isinstance(sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", sha256)
                or sha256 in seen
                or not isinstance(byte_count, int)
                or byte_count <= 0
                or not isinstance(source, dict)
                or source.get("pageId") != page_id
                or source.get("recipeId") != recipe_id
                or str(source.get("contentNiche") or "").strip().upper() != "TRUCK"
                or source.get("contentEngine") != content_engine
                or not isinstance(clip.get("sourceTreatment"), dict)
                or clip["sourceTreatment"].get("recipeSpecHash")
                    != current_recipe_spec_hash
                or not recovery_treatment_matches(
                    clip["sourceTreatment"], job, sha256,
                    generation_recipe.recipe_spec["renderTreatment"],
                )
            ):
                continue
            full = (root / rel_path).resolve()
            if (
                root not in full.parents
                or not full.is_file()
                or full.stat().st_size != byte_count
                or not _is_exact_16x9_video(full)
            ):
                continue
            candidates.append({
                "sourceJobId": job.get("jobId"),
                "artifactRoot": str(root),
                "path": rel_path,
                "sha256": sha256,
                "bytes": byte_count,
                "source": source,
                "sourceTreatment": clip["sourceTreatment"],
            })
            seen.add(sha256)
            if len(candidates) >= limit:
                return candidates
    return candidates


def _is_exact_16x9_video(path: Path) -> bool:
    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=p=0",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        parts = completed.stdout.strip().split(",")
        if completed.returncode != 0 or len(parts) != 2:
            return False
        width, height = int(parts[0]), int(parts[1])
        return width > height > 0 and abs((width / height) - (16 / 9)) <= 0.05
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


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


def _dossier_source_recipe(publication: dict[str, Any] | None):
    if publication is None:
        return None
    return resolve_source_recipe(
        publication,
        base_recipe_lookup=_registered_recipe,
    )


def _clip_manifest(project: str, rel_path: str) -> dict[str, Any] | None:
    root = (PROJECTS_DIR / project / "videos").resolve()
    full = (root / rel_path).resolve()
    if root not in full.parents:
        return None
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
    """Merge ``fields`` onto the job and publish straight to the read cache.

    `_save_jobs` publishes this exact call's dict/list values by reference,
    not a decoded copy (that is the whole point of C1 — no re-decode after
    our own write). A caller that keeps mutating a list/dict it just passed
    in `fields` (e.g. appending to it across loop iterations) would silently
    mutate the published snapshot too. Pass a copy (`list(x)`, `dict(x)`) of
    anything you intend to keep changing after this call returns.
    """
    with lock_for(_jobs_path()):
        store = _load_jobs()
        job = store["jobs"].get(job_id)
        if job is None:
            return None
        job.update(fields)
        _save_jobs(store)
        return dict(job)


def _checkpointed_generation(job: dict[str, Any]) -> bool:
    return job.get("sourceKind") == "generated" and job.get("generationCheckpointVersion") == 1


def _needs_generation_resume(job: dict[str, Any]) -> bool:
    return (_checkpointed_generation(job) and job.get("status") in GENERATION_ACTIVE_STATUSES
            and (job.get("runtimeId") != _GENERATION_RUNTIME_ID or job.get("resumePending") is True))


def _save_prediction_checkpoint(job_id: str, key: int | str, record: dict) -> None:
    # Attempt 0 of call i keeps the original key "i"; a moderation retry uses
    # its own "i:k" key, so it never resumes or overwrites the refused prediction.
    with lock_for(_jobs_path()):
        store = _load_jobs()
        job = store["jobs"][job_id]
        if job.get("status") not in GENERATION_ACTIVE_STATUSES:
            raise RuntimeError("generation_checkpoint_job_not_active")
        job.setdefault("providerCheckpoints", {})[str(key)] = record
        _save_jobs(store)


def _prediction_checkpoint_key(call_index: int, attempt: int) -> str:
    return str(call_index) if attempt == 0 else f"{call_index}:{attempt}"


def _reserve_moderation_retry(
    job_id: str, call_index: int, attempts: dict[str, list[dict[str, Any]]],
    entry: dict[str, Any],
) -> bool:
    """Atomically reserve one retry against the page's UTC-day budget.

    The reservation is the durable attempt row itself, written in the same
    transaction that counts every job's retries for the page, so concurrent
    jobs and restarts share one budget. First attempts never pass through here.
    """
    budget = moderation_retry.daily_budget()
    with lock_for(_jobs_path()):
        store = _load_jobs()
        job = store["jobs"][job_id]
        if job.get("status") not in GENERATION_ACTIVE_STATUSES:
            raise RuntimeError("generation_checkpoint_job_not_active")
        used = moderation_retry.retries_used(store, job.get("pageId"), entry["at"][:10])
        if used >= budget:
            return False
        key = str(call_index)
        updated = json.loads(json.dumps(attempts))
        updated.setdefault(key, []).append(dict(entry))
        job["generationAttempts"] = updated
        atomic_save(_jobs_path(), store)
    attempts.setdefault(key, []).append(dict(entry))
    return True


def _verify_preserved_generation(job_root: Path, clips: list) -> None:
    for clip in clips:
        for artifact in [clip, clip.get("source"), clip.get("thumbnail"), clip.get("generatedStill")]:
            if artifact is None:
                continue
            if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
                raise RuntimeError("generation_checkpoint_artifact_invalid")
            path = (job_root / artifact["path"]).resolve()
            if (job_root not in path.parents or not path.is_file()
                    or path.stat().st_size != artifact.get("bytes")
                    or _sha256(path) != artifact.get("sha256")):
                raise RuntimeError("generation_checkpoint_artifact_mismatch")


def _claim_unique_generated_clip(
    job_id: str, manifest: dict[str, Any],
) -> None:
    """Reject reused bytes, active prompt collisions, and within-job repeats."""
    digest = manifest.get("sha256")
    prompt_hash = manifest.get("promptHash")
    generation_index = manifest.get("generationIndex")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeError("generated_artifact_identity_invalid")
    if (
        not isinstance(prompt_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", prompt_hash)
        or not isinstance(generation_index, int)
        or isinstance(generation_index, bool)
        or generation_index < 0
    ):
        raise RuntimeError("generated_prompt_identity_invalid")
    with lock_for(_jobs_path()):
        store = _load_jobs()
        job = store.get("jobs", {}).get(job_id)
        if not isinstance(job, dict) or job.get("status") not in GENERATION_ACTIVE_STATUSES:
            raise RuntimeError("generated_job_not_active")
        page_id = job.get("pageId")
        archive_index = store.get(compaction.ARCHIVE_INDEX_KEY, {}) or {}
        if digest in (archive_index.get("usedClipSha256") or {}).get(page_id, []):
            # Delivered bytes never expire: an archived completed job still
            # reserves its exact sha256 against replay as a new delivery.
            raise RuntimeError("duplicate_generated_artifact")
        for other in store.get("jobs", {}).values():
            if (
                not isinstance(other, dict)
                or other.get("pageId") != page_id
                or other.get("status") not in SOURCE_DNA_UNAVAILABLE_STATUSES
            ):
                continue
            for clip in other.get("clips", []):
                if not isinstance(clip, dict):
                    continue
                if clip.get("sha256") == digest:
                    raise RuntimeError("duplicate_generated_artifact")
                if (
                    other.get("jobId") != job_id
                    and other.get("status") in GENERATION_ACTIVE_STATUSES
                    and clip.get("promptHash") == prompt_hash
                ):
                    raise RuntimeError("duplicate_generated_prompt")
                if (
                    other.get("jobId") == job_id
                    and clip.get("promptHash") == prompt_hash
                    and clip.get("generationIndex") != generation_index
                ):
                    raise RuntimeError("duplicate_generated_prompt")
        job.setdefault("clips", []).append(manifest)
        _save_jobs(store)


def _claim_unique_slideshow_clip(
    job_id: str, manifest: dict[str, Any], plan_signature: str,
) -> None:
    """Atomically admit one fresh render for one reserved slideshow plan."""
    digest = manifest.get("sha256")
    if (
        not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or not isinstance(plan_signature, str)
        or not re.fullmatch(r"[0-9a-f]{64}", plan_signature)
    ):
        raise RuntimeError("slideshow_artifact_identity_invalid")
    with lock_for(_jobs_path()):
        store = _load_jobs()
        job = store.get("jobs", {}).get(job_id)
        if not isinstance(job, dict) or job.get("status") not in GENERATION_ACTIVE_STATUSES:
            raise RuntimeError("slideshow_job_not_active")
        page_id = job.get("pageId")
        archive_index = store.get(compaction.ARCHIVE_INDEX_KEY, {}) or {}
        if digest in (archive_index.get("usedClipSha256") or {}).get(page_id, []):
            raise RuntimeError("duplicate_slideshow_artifact")
        for entry in archive_index.get("slideshow", []):
            if (
                isinstance(entry, dict)
                and entry.get("pageId") == page_id
                and entry.get("signature") == plan_signature
            ):
                raise RuntimeError("duplicate_slideshow_plan")
        for other in store.get("jobs", {}).values():
            if (
                not isinstance(other, dict)
                or other.get("pageId") != page_id
                or other.get("status") not in SOURCE_DNA_UNAVAILABLE_STATUSES
            ):
                continue
            for clip in other.get("clips", []):
                if not isinstance(clip, dict):
                    continue
                source = clip.get("source")
                if clip.get("sha256") == digest:
                    raise RuntimeError("duplicate_slideshow_artifact")
                if (
                    other.get("jobId") != job_id
                    and isinstance(source, dict)
                    and source.get("planSignature") == plan_signature
                ):
                    raise RuntimeError("duplicate_slideshow_plan")
        job.setdefault("clips", []).append(manifest)
        _save_jobs(store)


def _job_matches_current_master_pages(job: dict[str, Any]) -> bool:
    """Recheck the job's exact page strategy immediately before media work."""
    page_id = job.get("pageId")
    if not isinstance(page_id, str):
        return False
    intent = exact_intent(
        job.get("masterPages"), job.get("masterPagesHash"),
        expected_page_id=page_id,
    )
    if intent is None:
        return False
    current = _current_master_pages_intent(page_id, intent)
    return current == (intent, job.get("masterPagesHash"))


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


def _source_media_origin() -> str:
    value = os.environ.get("CONTENT_LAB_CONTROL_PLANE_ORIGIN", "").strip().rstrip("/")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("source_dna_control_plane_origin_unavailable")
    return value


def _source_artifact_origin() -> str:
    value = os.environ.get("CONTENT_LAB_PUBLIC_ORIGIN", "").strip().rstrip("/")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("source_import_public_origin_unavailable")
    return value


def _source_dna_cache_budget() -> int:
    """Byte ceiling for ``_source_dna/``. Env override, clamped to positive."""
    raw = os.environ.get("CONTENT_LAB_SOURCE_DNA_CACHE_BYTES", "").strip()
    if raw:
        try:
            budget = int(raw)
        except ValueError:
            budget = _DEFAULT_SOURCE_DNA_CACHE_BYTES
        if budget > 0:
            return budget
    return _DEFAULT_SOURCE_DNA_CACHE_BYTES


def _source_dna_active_master_hashes() -> set[str]:
    """Masters still referenced by queued/running source jobs (may yet be cut).

    A master with no active job and no local manifest is obsolete: it can only
    be re-created by re-downloading, so it is the eviction candidate set.
    """
    hashes: set[str] = set()
    try:
        store = _load_jobs()
    except Exception:
        return hashes
    for job in store.get("jobs", {}).values():
        if not isinstance(job, dict):
            continue
        if job.get("sourceKind") != "dossier_source_dna":
            continue
        if job.get("status") not in GENERATION_ACTIVE_STATUSES:
            continue
        for cut in job.get("sourceCuts") or []:
            if isinstance(cut, dict) and isinstance(cut.get("masterSha256"), str):
                hashes.add(cut["masterSha256"])
    return hashes


def _evict_source_dna_cache(
    cache_root: Path,
    pinned: set[str],
    *,
    reserve_bytes: int = 0,
) -> None:
    """Atomically evict oldest unreferenced masters until the cache fits budget.

    Reference-aware: files whose name (a SHA256) is pinned are never removed.
    Deletion is oldest-first by mtime so a just-installed master (which is also
    pinned by its active job) is doubly safe. Best-effort per file: one locked
    or unreadable entry must not stop the rest of the sweep.
    """
    budget = _source_dna_cache_budget()
    entries: list[tuple[int, int, Path]] = []
    for path in cache_root.glob("*.mp4"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.is_file():
            entries.append((stat.st_mtime_ns, stat.st_size, path))
    entries.sort()
    total = sum(size for _mtime, size, _path in entries)
    for _mtime, size, path in entries:
        if total + reserve_bytes <= budget:
            break
        if path.name[:-4] in pinned:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            total -= size
        except OSError:
            continue
        else:
            total -= size


async def _cached_source_master(page_id: str, master: Any, job_id: str) -> Path:
    cache_root = (_generation_root() / "_source_dna").resolve()
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = cache_root / f"{master.sha256}.mp4"
    lock = _source_cache_locks.setdefault(master.sha256, asyncio.Lock())
    async with lock:
        pinned = _source_dna_active_master_hashes() | source_dna_master_hashes() | {master.sha256}
        if (
            target.is_file()
            and target.stat().st_size == master.bytes
            and await asyncio.to_thread(_sha256, target) == master.sha256
        ):
            _evict_source_dna_cache(cache_root, pinned)
            return target
        _evict_source_dna_cache(cache_root, pinned, reserve_bytes=master.bytes)
        partial = cache_root / f".{master.sha256}.{job_id}.part"
        partial.unlink(missing_ok=True)
        url = (
            f"{_source_media_origin()}/api/control-plane/v1/pages/"
            f"{quote(page_id, safe='')}/dossier-ingredient-media/source/{master.sha256}"
        )
        digest = hashlib.sha256()
        byte_count = 0
        try:
            timeout = httpx.Timeout(connect=10, read=120, write=30, pool=10)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        raise RuntimeError("source_dna_master_unavailable")
                    content_length = response.headers.get("content-length")
                    if content_length is not None and content_length != str(master.bytes):
                        raise RuntimeError("source_dna_master_size_mismatch")
                    with partial.open("xb") as handle:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            byte_count += len(chunk)
                            if byte_count > master.bytes:
                                raise RuntimeError("source_dna_master_size_mismatch")
                            digest.update(chunk)
                            handle.write(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())
            if byte_count != master.bytes or digest.hexdigest() != master.sha256:
                raise RuntimeError("source_dna_master_hash_mismatch")
            os.replace(partial, target)
            _evict_source_dna_cache(cache_root, pinned)
            return target
        finally:
            partial.unlink(missing_ok=True)


async def _thumbnail_manifest(job_root: Path, video: Path, index: int) -> dict[str, Any]:
    thumbnail_root = job_root / "thumbnails"
    thumbnail_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = thumbnail_root / f"{index:04d}.jpg"
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", "-ss", "0.25", "-i", str(video),
        "-frames:v", "1", "-vf", "scale=360:-2", str(target),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    if process.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
        raise RuntimeError(f"thumbnail_generation_failed:{stderr.decode(errors='replace')[-120:]}")
    return _generated_manifest(job_root, target)


def _source_provenance(job: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    master = job["masterPages"]
    return {
        **base,
        "pageId": job["pageId"],
        "masterPagesHash": job["masterPagesHash"],
        "contentNiche": master["contentNiche"],
        "contentEngine": master["contentEngine"],
        "vaultUrl": master["vaultUrl"],
    }


async def _validated_source_url(value: Any) -> str:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(validate_source_url, value),
            timeout=SOURCE_URL_RESOLVE_SECONDS,
        )
    except TimeoutError as error:
        raise SourceImportUnavailable("source import host resolution timed out") from error


def _validated_provider_candidates(entry: dict[str, Any], crop_mode: Any) -> list[dict[str, Any]]:
    """A multi-crop provider call must fulfill its complete commissioned set."""
    candidates = entry.get("crops") or [{"file": entry.get("file")}]
    counts = {"dual": 2, "triptych": 3, "both": 5}
    expected = counts.get(crop_mode)
    if expected is not None:
        if not isinstance(candidates, list) or len(candidates) != expected or any(
            not isinstance(candidate, dict)
            or candidate.get("cropMode") != crop_mode
            or type(candidate.get("cropCount")) is not int
            or candidate["cropCount"] != expected
            or type(candidate.get("cropIndex")) is not int
            for candidate in candidates
        ) or {candidate["cropIndex"] for candidate in candidates} != set(range(expected)):
            raise RuntimeError("provider_crop_set_invalid")
        candidates = sorted(candidates, key=lambda candidate: candidate["cropIndex"])
    elif any(isinstance(candidate, dict) and candidate.get("cropMode") in counts for candidate in candidates):
        raise RuntimeError("provider_crop_mode_uncommissioned")
    return candidates


async def _run_dossier_generation(job_id: str) -> None:
    _get_job_or_404(job_id)  # Validate the id before deriving a lock filename.
    with runner_lock(_jobs_path(), job_id) as acquired:
        if not acquired:
            return  # Another live runtime still owns this exact provider work.
        job = _get_job_or_404(job_id)
        if job.get("status") not in GENERATION_ACTIVE_STATUSES:
            return
        await asyncio.to_thread(_update_job, job_id, runtimeId=_GENERATION_RUNTIME_ID)
        await _run_owned_dossier_generation(job_id)


async def _run_owned_dossier_generation(job_id: str) -> None:
    job = _get_job_or_404(job_id)
    if not _job_matches_current_master_pages(job):
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error="master_pages_strategy_changed",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return
    publication = load_registered_recipe(
        job.get("recipePublicationPageId") or job["pageId"],
        job["recipeId"], job["engine"], job["recipeVersion"],
    )
    recipe = resolve_generation_recipe(publication) if publication else None
    if (
        recipe is None
        or job.get("engineRegistryHash") != recipe.engine_registry_hash
        or job.get("formatContractVersion") != recipe.format_contract_version
        or job.get("promptCatalogHash") != recipe.prompt_catalog_hash
        or job.get("executorVersion") != recipe.executor_version
        or job.get("family") != recipe.family_name
        or job.get("providerModel") != recipe.provider_model
    ):
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error="recipe_executor_unavailable",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return

    job_root = Path(job["artifactRoot"]).resolve()
    durable = _checkpointed_generation(job)
    resuming = durable and bool(job.get("providerCheckpoints") or job.get("clips"))
    # Never rewrite bytes already referenced by a preserved manifest when an
    # interrupted crop/treatment is rebuilt from the same paid prediction.
    render_root = job_root / ("renders-resume-" + _secrets.token_hex(6) if resuming else "renders")
    treated_root = job_root / "treated"
    render_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    options = generation_options(recipe)
    duration = int(options.pop("duration", 6))
    resolution = str(options.pop("resolution", "1080p"))
    aspect_ratio = str(options.pop("aspect_ratio", "9:16"))
    calls = int(job["providerCallsPlanned"])
    color_correction = dossier_filters_to_color_correction(recipe)
    clip_speed = dossier_clip_speed(recipe)
    clip_crop = dossier_clip_crop(recipe)
    # A framed page's candidate is always cut into its band, even when the
    # recipe has no grade, speed or crop of its own.
    framed = resolved_frame(recipe.recipe_spec["renderTreatment"])[0] != VERTICAL_FRAME
    manifests: list[dict[str, Any]] = list(job.get("clips", [])) if durable else []
    provider_failures: list[dict[str, Any]] = list(job.get("providerFailures", [])) if durable else []
    completed_calls = list(job.get("completedGenerationCalls", [])) if durable else []
    attempts: Any = json.loads(json.dumps(job.get("generationAttempts") or {})) if durable else {}
    provider_calls_completed = len(completed_calls)
    prompt_plan = job.get("promptPlan")
    if (
        not isinstance(prompt_plan, list)
        or len(prompt_plan) != calls
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("combinationId"), int)
            or not isinstance(item.get("promptHash"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["promptHash"])
            for item in prompt_plan
        )
    ):
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error="prompt_plan_invalid",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return
    await asyncio.to_thread(_update_job,job_id, status="running", resumePending=False,
                           providerCallsCompleted=provider_calls_completed)

    try:
        if (any(type(index) is not int or index < 0 or index >= calls for index in completed_calls)
                or len(completed_calls) != len(set(completed_calls))):
            raise RuntimeError("generation_checkpoint_invalid")
        if not isinstance(attempts, dict):
            raise RuntimeError("generation_checkpoint_invalid")

        def prompt_provenance_ok(clip: dict[str, Any], index: int) -> bool:
            # The plan's prompt, or a variant whose exact hash a durable
            # succeeded attempt row for this same call recorded.
            plan_hash = prompt_plan[index]["promptHash"]
            variant = clip.get("promptVariant")
            if variant is None:
                return (clip.get("promptHash") == plan_hash
                        and clip.get("basePromptHash", plan_hash) == plan_hash)
            return clip.get("basePromptHash") == plan_hash and any(
                isinstance(row, dict) and row.get("outcome") == "succeeded"
                and type(row.get("attempt")) is int and row["attempt"] >= 1
                and row.get("promptVariant") == variant
                and row.get("promptHash") == clip.get("promptHash")
                for row in (attempts.get(str(index)) or [])
            )

        if manifests:
            await asyncio.to_thread(_verify_preserved_generation, job_root, manifests)
        expected_crops = {"dual": 2, "triptych": 3, "both": 5}.get(options.get("crop_mode"), 1)
        seen_candidates = set()
        for clip in manifests:
            index = clip.get("generationIndex")
            crop = clip.get("delivery", {}).get("crop", {})
            candidate_index = crop.get("index", 0)
            if (type(index) is not int or not 0 <= index < calls
                    or not prompt_provenance_ok(clip, index)
                    or clip.get("promptCombinationId") != prompt_plan[index]["combinationId"]
                    or type(candidate_index) is not int or not 0 <= candidate_index < expected_crops
                    or (index, candidate_index) in seen_candidates
                    or (expected_crops > 1 and crop.get("count") != expected_crops)
                    or clip.get("sourceTreatment") != source_treatment_receipt(
                        job, recipe.recipe_spec["renderTreatment"], clip["sha256"],
                        clip_speed=clip_speed, clip_crop=clip_crop)):
                raise RuntimeError("generation_checkpoint_provenance_mismatch")
            seen_candidates.add((index, candidate_index))
        for index in range(calls):
            group = [clip for clip in manifests if clip.get("generationIndex") == index]
            complete = (len(group) == expected_crops
                        and {clip.get("delivery", {}).get("crop", {}).get("index", 0) for clip in group}
                        == set(range(expected_crops))
                        and all(prompt_provenance_ok(clip, index) for clip in group))
            if complete and index not in completed_calls:
                # The last claim may have been committed immediately before
                # death, without the subsequent completed-call counter write.
                completed_calls.append(index)
            elif index in completed_calls and not complete:
                raise RuntimeError("generation_checkpoint_incomplete_call")
        provider_calls_completed = len(completed_calls)
        for call_index in range(calls):
            if not _job_matches_current_master_pages(job):
                raise RuntimeError("master_pages_strategy_changed")
            if call_index in completed_calls:
                continue
            prior_failure = next((failure for failure in provider_failures
                                  if failure.get("generationIndex") == call_index), None)
            if prior_failure is not None:
                if (prior_failure.get("class") == "moderation"
                        and isinstance(prior_failure.get("providerRequestId"), str)
                        and prior_failure["providerRequestId"].strip()
                        and str(prior_failure.get("detail", "")).startswith("Replicate failed:")):
                    continue
                raise RuntimeError("provider_generation_failed")
            prompt_entry = prompt_plan[call_index]
            base_prompt, slots = compose_prompt_combination(
                recipe, prompt_entry["combinationId"],
            )
            if prompt_sha256(base_prompt) != prompt_entry["promptHash"]:
                raise RuntimeError("prompt_plan_authority_mismatch")
            anchor = await load_generation_anchor(
                recipe, job["idempotencyKey"], call_index,
            )
            image_data_uri = anchor[0] if anchor else None
            anchor_metadata = anchor[1] if anchor else None
            call_key = str(call_index)
            rows = attempts.get(call_key, [])
            if not isinstance(rows, list) or any(
                not isinstance(row, dict) or row.get("attempt") != number
                for number, row in enumerate(rows)
            ):
                raise RuntimeError("generation_checkpoint_invalid")
            if rows and rows[-1].get("outcome") == "refused":
                # The refusal is recorded; decide its retry exactly once more.
                attempt, reserve = rows[-1]["attempt"] + 1, True
            elif rows:
                # Resume the same paid attempt through its own checkpoint.
                attempt, reserve = rows[-1]["attempt"], False
            else:
                attempt, reserve = 0, False
            attempt_cost = moderation_retry.attempt_cost_usd(recipe.provider_model)
            terminal = None
            succeeded = False
            while True:
                if reserve:
                    reserve = False
                    if attempt > moderation_retry.RETRIES_PER_CALL:
                        terminal = moderation_retry.RETRIES_EXHAUSTED
                        break
                    if not _job_matches_current_master_pages(job):
                        raise RuntimeError("master_pages_strategy_changed")
                    retry_prompt, retry_variant = moderation_retry.variant_prompt(base_prompt, attempt)
                    reserved = await asyncio.to_thread(
                        _reserve_moderation_retry, job_id, call_index, attempts, {
                            "attempt": attempt,
                            "promptHash": prompt_sha256(retry_prompt),
                            "promptVariant": retry_variant,
                            "providerRequestId": None,
                            "class": None,
                            "errorDetail": None,
                            "costUsd": None,
                            "outcome": "pending",
                            "at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    if not reserved:
                        terminal = moderation_retry.BUDGET_EXHAUSTED
                        break
                prompt, prompt_variant = moderation_retry.variant_prompt(base_prompt, attempt)
                used_prompt_hash = prompt_sha256(prompt)
                rows = attempts.setdefault(call_key, [])
                if attempt == len(rows):
                    rows.append({"attempt": attempt, "promptHash": used_prompt_hash,
                                 "promptVariant": prompt_variant,
                                 "at": datetime.now(timezone.utc).isoformat()})
                row = rows[attempt]
                if row.get("promptHash") != used_prompt_hash or row.get("promptVariant") != prompt_variant:
                    raise RuntimeError("prompt_plan_authority_mismatch")
                provider_job_id = f"{job_id}-g{call_index:02d}" + (f"-r{attempt}" if attempt else "")
                provider_jobs = {
                    provider_job_id: {
                        "videos": [{"index": 0, "status": "queued"}],
                    }
                }
                if durable:
                    checkpoint_key = _prediction_checkpoint_key(call_index, attempt)
                    def persist(record, key=checkpoint_key):
                        _save_prediction_checkpoint(job_id, key, record)
                    provider_jobs[provider_job_id]["videos"][0]["_prediction_checkpoint"] = PredictionCheckpoint(
                        job.get("providerCheckpoints", {}).get(checkpoint_key), persist,
                    )
                await generate_one(
                    provider_job_id,
                    0,
                    recipe.engine,
                    prompt,
                    aspect_ratio,
                    resolution,
                    duration,
                    image_data_uri,
                    provider_jobs,
                    render_root,
                    "",
                    model_id=recipe.provider_model,
                    **options,
                )
                entry = provider_jobs[provider_job_id]["videos"][0]
                request_id = entry.get("provider_request_id")
                if entry.get("status") == "done":
                    # A successful prediction is billed; record its estimate.
                    row.update({"providerRequestId": request_id, "class": None,
                                "errorDetail": None, "costUsd": attempt_cost,
                                "outcome": "succeeded"})
                    succeeded = True
                    if attempt:
                        # Recovery accepts a variant prompt hash only from a
                        # durable succeeded row, so persist it before claims.
                        await asyncio.to_thread(_update_job, job_id,
                            generationAttempts=attempts,
                            moderationRetryCostUsd=moderation_retry.retry_cost_total(attempts))
                    break
                provider_error = str(entry.get("error") or "")
                error_class = classify_provider_error(provider_error)
                refused = moderation_retry.confirmed_refusal(error_class, request_id, provider_error)
                # A refused Replicate prediction is billed like a successful
                # one; no prediction id means nothing was created or billed.
                row.update({"providerRequestId": request_id, "class": error_class,
                            "errorDetail": provider_error_code(provider_error),
                            "detail": provider_error[:300],
                            "costUsd": attempt_cost if request_id else None,
                            "outcome": "refused" if refused else "failed"})
                if not refused:
                    break
                terminal = moderation_retry.retry_blocked(provider_error)
                if terminal is not None:
                    break
                await asyncio.to_thread(_update_job, job_id,
                    generationAttempts=attempts,
                    moderationRetryCostUsd=moderation_retry.retry_cost_total(attempts))
                log.info("job=%s generation=%d attempt=%d moderation refusal; considering varied retry",
                         job_id, call_index, attempt)
                attempt += 1
                reserve = True
            if not succeeded:
                # Keep the terminal error code stable and persist why the
                # provider failed: Railway logs rotate, the job store does not,
                # and credit exhaustion needs a different response than a
                # transient provider fault. The status route returns the safe
                # class/code only for a terminal provider failure (see
                # _job_status_failure_cause). A refusal that was retried is
                # not a failure until its retries or budget end.
                last = attempts[call_key][-1]
                provider_error = str(last.get("detail") or "")
                error_class = last.get("class")
                error_detail = last.get("errorDetail")
                failure = {
                    "class": error_class,
                    "provider": recipe.engine,
                    "model": recipe.provider_model,
                    "generationIndex": call_index,
                    "providerRequestId": last.get("providerRequestId"),
                    "detail": provider_error,
                    "at": datetime.now(timezone.utc).isoformat(),
                }
                if terminal is not None:
                    failure["terminal"] = terminal
                    failure["attempts"] = len(attempts[call_key])
                provider_failures.append(failure)
                # errorClass/errorDetail (latest failure) are logged and stored
                # on the job. GET /v1/jobs/{id} returns them only once the job
                # ends failed with provider_generation_failed, sanitised to the
                # Worker's tolerant status validator (opus/lab-errorclass),
                # which must be live before this Lab change is deployed.
                log.warning(
                    "job=%s generation=%d provider=%s errorClass=%s errorDetail=%s terminal=%s",
                    job_id, call_index, recipe.engine, error_class, error_detail, terminal,
                )
                await asyncio.to_thread(_update_job,
                    job_id,
                    errorClass=error_class,
                    errorDetail=error_detail,
                    providerFailure=failure,
                    # list(...): the same aliasing hazard _update_job's
                    # docstring warns about — this loop keeps appending to
                    # provider_failures on a later failed call, after an
                    # earlier call already published this exact list object
                    # into the cache (Tides review of PR #185, round 2,
                    # minor finding: SERVED 2 / ON_DISK 1 without this).
                    providerFailures=list(provider_failures),
                    generationAttempts=attempts,
                    moderationRetryCostUsd=moderation_retry.retry_cost_total(attempts),
                )
                if last.get("outcome") == "refused":
                    # A confirmed refused prediction whose bounded varied
                    # retries (or the page's daily retry budget) are spent
                    # stays refused. Continue only to the next distinct
                    # candidate already in this job's immutable plan.
                    await asyncio.to_thread(_update_job, job_id,
                        progress=int(((call_index + 1) / calls) * 100))
                    continue
                raise RuntimeError("provider_generation_failed")
            candidates = _validated_provider_candidates(entry, options.get("crop_mode"))
            for candidate_index, candidate in enumerate(candidates):
                if any(clip.get("generationIndex") == call_index
                       and clip.get("delivery", {}).get("crop", {}).get("index", 0) == candidate_index
                       for clip in manifests):
                    continue  # Exact preserved bytes were checked above.
                rel_path = candidate.get("file") if isinstance(candidate, dict) else None
                if not isinstance(rel_path, str) or not rel_path:
                    raise RuntimeError("provider_artifact_missing")
                source = (render_root / rel_path).resolve()
                if render_root.resolve() not in source.parents or not source.is_file():
                    raise RuntimeError("provider_artifact_invalid")
                artifact = source
                if color_correction or clip_speed != 1.0 or clip_crop is not None or framed:
                    treated_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                    artifact = treated_root / f"g{call_index:02d}-c{candidate_index:02d}.mp4"
                    frame_cut = await _page_frame_cut_kwargs(
                        recipe.recipe_spec["renderTreatment"], source,
                    )
                    await run_color_correct(
                        str(source), str(artifact), color_correction, scale=None,
                        playback_speed=clip_speed,
                        clip_crop=clip_crop,
                        **frame_cut,
                    )
                manifest = _generated_manifest(job_root, artifact)
                manifest["generationIndex"] = call_index
                manifest["promptCombinationId"] = prompt_entry["combinationId"]
                # promptHash is the prompt actually sent; the immutable plan's
                # hash stays alongside it, with the deterministic variant id
                # (None for the plan's own prompt).
                manifest["promptHash"] = used_prompt_hash
                manifest["basePromptHash"] = prompt_entry["promptHash"]
                manifest["promptVariant"] = prompt_variant
                manifest["promptSlots"] = slots
                manifest["clipSpeed"] = clip_speed
                manifest["clipCrop"] = clip_crop
                manifest["sourceTreatment"] = source_treatment_receipt(
                    job,
                    recipe.recipe_spec["renderTreatment"],
                    manifest["sha256"],
                    clip_speed=clip_speed,
                    clip_crop=clip_crop,
                )
                provider_source = source
                provider_image_rel_path = entry.get("provider_image_file")
                provider_image = None
                if provider_image_rel_path is not None:
                    if not isinstance(provider_image_rel_path, str) or not provider_image_rel_path:
                        raise RuntimeError("provider_image_missing")
                    provider_image = (render_root / provider_image_rel_path).resolve()
                    if render_root.resolve() not in provider_image.parents or not provider_image.is_file():
                        raise RuntimeError("provider_image_invalid")
                delivery = None
                if isinstance(candidate, dict) and candidate.get("cropMode") in {
                    "dual", "triptych", "both",
                }:
                    master_rel_path = entry.get("provider_master_file")
                    if not isinstance(master_rel_path, str) or not master_rel_path:
                        raise RuntimeError("provider_master_missing")
                    provider_source = (render_root / master_rel_path).resolve()
                    if render_root.resolve() not in provider_source.parents or not provider_source.is_file():
                        raise RuntimeError("provider_master_invalid")
                    crop_index = candidate.get("cropIndex")
                    crop_count = candidate.get("cropCount")
                    crop_width = candidate.get("width")
                    crop_height = candidate.get("height")
                    if (
                        not isinstance(crop_index, int)
                        or not isinstance(crop_count, int)
                        or crop_index < 0
                        or crop_index >= crop_count
                        or not isinstance(crop_width, int)
                        or crop_width <= 0
                        or not isinstance(crop_height, int)
                        or crop_height <= 0
                    ):
                        raise RuntimeError("provider_crop_geometry_invalid")
                    provider_master = _generated_manifest(job_root, provider_source)
                    delivery = {
                        "aspectRatio": "9:16",
                        "width": crop_width,
                        "height": crop_height,
                        "crop": {
                            "mode": candidate["cropMode"],
                            "index": crop_index,
                            "count": crop_count,
                            "groupId": f"sha256:{provider_master['sha256']}",
                            "sourceSha256": provider_master["sha256"],
                        },
                    }
                    manifest["delivery"] = delivery
                source_manifest = _generated_manifest(job_root, provider_source)
                manifest["source"] = _source_provenance(job, {
                    "recipeId": recipe.recipe_id,
                    "recipeVersion": job["recipeVersion"],
                    "path": source_manifest["path"],
                    "sha256": source_manifest["sha256"],
                    "bytes": source_manifest["bytes"],
                })
                manifest["thumbnail"] = await _thumbnail_manifest(
                    job_root, artifact, len(manifests),
                )
                if anchor_metadata is not None:
                    manifest["anchor"] = anchor_metadata
                if provider_image is not None:
                    manifest["generatedStill"] = _generated_manifest(job_root, provider_image)
                if not _job_matches_current_master_pages(job):
                    raise RuntimeError("master_pages_strategy_changed")
                _claim_unique_generated_clip(job_id, manifest)
                manifests.append(manifest)
            provider_calls_completed += 1
            completed_calls.append(call_index)
            # `_update_job` now publishes the saved dict straight to the
            # read cache (_save_jobs). A caller-owned list handed to it is
            # therefore aliased into that cache, not copied — this loop
            # keeps appending to `completed_calls` on later iterations, so
            # passing the live reference would let the published snapshot's
            # job silently grow past what was actually published for it.
            # `list(...)` freezes what this write actually saw.
            await asyncio.to_thread(_update_job,
                job_id,
                progress=int(((call_index + 1) / calls) * 100),
                providerCallsCompleted=provider_calls_completed,
                completedGenerationCalls=list(completed_calls),
                generationAttempts=attempts,
                moderationRetryCostUsd=moderation_retry.retry_cost_total(attempts),
            )
        if provider_failures:
            # Retain the same truthful partial/zero-output terminal handling,
            # including the error, after all independent planned candidates.
            raise RuntimeError("provider_generation_failed")
    except asyncio.CancelledError as cancelled:
        if durable and cancelled.args == ("generation_runtime_shutdown",):
            # Only application shutdown pauses work. An explicit cancellation
            # or changed strategy retains its existing terminal behavior.
            await asyncio.to_thread(_update_job, job_id, status="queued", resumePending=True)
            raise
        shutil.rmtree(job_root, ignore_errors=True)
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error="generation_cancelled",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        raise
    except Exception as error:  # provider and ffmpeg failures are job state
        complete_manifests = [clip for clip in manifests
                              if not durable or clip.get("generationIndex") in completed_calls]
        if (str(error) == "provider_generation_failed" and complete_manifests
                and _job_matches_current_master_pages(job)):
            # Earlier calls already produced fully treated, claimed clips. A
            # later provider failure ends this batch, not those paid outputs.
            # Preserve requested/completed counts and the error; the consumer
            # admits the actual artifacts and plans the remaining deficit.
            await asyncio.to_thread(_update_job,
                job_id,
                status="completed",
                progress=100,
                clips=complete_manifests,
                uncompletedGenerationClips=[clip for clip in manifests if clip not in complete_manifests],
                providerCallsCompleted=len(completed_calls),
                completedGenerationCalls=list(completed_calls),
                error=str(error),
                completedAt=datetime.now(timezone.utc).isoformat(),
            )
            return
        if not resuming:
            shutil.rmtree(job_root, ignore_errors=True)
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error=str(error)[:300],
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return

    if not _job_matches_current_master_pages(job):
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error="master_pages_strategy_changed",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return
    await asyncio.to_thread(_update_job,
        job_id,
        status="completed",
        progress=100,
        clips=manifests,
        providerCallsCompleted=len(completed_calls),
        completedGenerationCalls=list(completed_calls),
        completedAt=datetime.now(timezone.utc).isoformat(),
    )


async def _page_frame_cut_kwargs(render_treatment: dict[str, Any], source: Path) -> dict[str, Any]:
    """Band-aware cut arguments for a framed page; none at all for a 9:16 page.

    The frame and frameFit come from the locked recipe's render treatment.
    fit contains the source window, so it needs the upright size the cut's
    filters will see; fill uses ffmpeg's own scale arithmetic and needs no probe.
    A 9:16 page passes nothing, so its cut call is exactly today's.
    """
    frame, fit = resolved_frame(render_treatment)
    if frame == VERTICAL_FRAME:
        return {}
    kwargs: dict[str, Any] = {"page_frame": frame, "frame_fit": fit}
    if fit == "fit":
        kwargs["source_size"] = await probe_display_size(source)
    return kwargs


async def _video_geometry(path: Path) -> tuple[int, int]:
    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0",
        str(path), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    parts = stdout.decode().strip().split(",")
    if process.returncode != 0 or len(parts) != 2:
        raise RuntimeError("truck_master_geometry_unavailable")
    width, height = int(parts[0]), int(parts[1])
    if width <= height or abs((width / height) - (16 / 9)) > 0.05:
        raise RuntimeError("truck_master_not_16x9")
    return width, height


async def _run_truck_master_recovery(job_id: str) -> None:
    """Turn preserved paid truck masters into five exact portrait deliveries.

    This path never calls a model. It copies and re-hashes the previously
    completed output, proves it is landscape, then uses the same five-way crop
    executor as new truck generations. The new job's normal artifact endpoint
    lets the existing Control Plane replenishment transaction download, hash,
    admit, scan and approve the results without a parallel write path.
    """
    job = _get_job_or_404(job_id)
    if not _job_matches_current_master_pages(job):
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error="master_pages_strategy_changed",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return
    job_root = Path(job["artifactRoot"]).resolve()
    master_root = job_root / "masters"
    master_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    masters = job.get("recoveryMasters")
    if not isinstance(masters, list) or not masters:
        await asyncio.to_thread(_update_job,
            job_id, status="failed", error="truck_master_recovery_empty",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return
    manifests: list[dict[str, Any]] = []
    await asyncio.to_thread(_update_job,job_id, status="running", progress=0)
    try:
        for master_index, candidate in enumerate(masters):
            if not _job_matches_current_master_pages(job):
                raise RuntimeError("master_pages_strategy_changed")
            source_root = Path(candidate["artifactRoot"]).resolve()
            source = (source_root / candidate["path"]).resolve()
            if (
                source_root not in source.parents
                or not source.is_file()
                or source.stat().st_size != candidate["bytes"]
                or _sha256(source) != candidate["sha256"]
            ):
                raise RuntimeError("truck_master_bytes_changed")
            await _video_geometry(source)
            copied = master_root / f"{candidate['sha256']}.mp4"
            if not copied.is_file():
                shutil.copyfile(source, copied)
            copied_manifest = _generated_manifest(job_root, copied)
            if (
                copied_manifest["sha256"] != candidate["sha256"]
                or copied_manifest["bytes"] != candidate["bytes"]
            ):
                raise RuntimeError("truck_master_copy_mismatch")
            crops = await multi_crop_vertical(copied, TRUCK_CROP_MODE)
            if len(crops) != TRUCK_CROP_COUNT:
                raise RuntimeError("truck_master_crop_count_invalid")
            crop_width, crop_height = await _video_geometry_for_delivery(crops[0])
            for crop_index, crop in enumerate(crops):
                geometry = await _video_geometry_for_delivery(crop)
                if geometry != (crop_width, crop_height):
                    raise RuntimeError("truck_master_crop_geometry_mismatch")
                manifest = _generated_manifest(job_root, crop)
                inherited_treatment = derived_source_treatment(
                    candidate.get("sourceTreatment"), candidate["sha256"],
                    manifest["sha256"], job["jobId"],
                )
                if inherited_treatment is not None:
                    manifest["sourceTreatment"] = inherited_treatment
                source_authority = candidate["source"]
                manifest["source"] = _source_provenance(job, {
                    "recipeId": source_authority.get("recipeId") or job["recipeId"],
                    "recipeVersion": source_authority.get("recipeVersion") or job["recipeVersion"],
                    "path": copied_manifest["path"],
                    "sha256": copied_manifest["sha256"],
                    "bytes": copied_manifest["bytes"],
                })
                manifest["delivery"] = {
                    "aspectRatio": "9:16",
                    "width": crop_width,
                    "height": crop_height,
                    "crop": {
                        "mode": TRUCK_CROP_MODE,
                        "index": crop_index,
                        "count": TRUCK_CROP_COUNT,
                        "groupId": f"sha256:{copied_manifest['sha256']}",
                        "sourceSha256": copied_manifest["sha256"],
                    },
                }
                manifest["thumbnail"] = await _thumbnail_manifest(
                    job_root, crop, len(manifests),
                )
                manifests.append(manifest)
            await asyncio.to_thread(_update_job,
                job_id,
                progress=int(((master_index + 1) / len(masters)) * 100),
            )
    except asyncio.CancelledError:
        shutil.rmtree(job_root, ignore_errors=True)
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error="generation_cancelled",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        raise
    except Exception as error:
        shutil.rmtree(job_root, ignore_errors=True)
        await asyncio.to_thread(_update_job,
            job_id, status="failed", error=str(error)[:300],
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return
    if not _job_matches_current_master_pages(job):
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error="master_pages_strategy_changed",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return
    await asyncio.to_thread(_update_job,
        job_id, status="completed", progress=100, clips=manifests,
        completedAt=datetime.now(timezone.utc).isoformat(),
    )


async def _video_geometry_for_delivery(path: Path) -> tuple[int, int]:
    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0",
        str(path), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    parts = stdout.decode().strip().split(",")
    if process.returncode != 0 or len(parts) != 2:
        raise RuntimeError("truck_crop_geometry_unavailable")
    width, height = int(parts[0]), int(parts[1])
    if width <= 0 or height <= 0 or abs((width / height) - (9 / 16)) > 0.003:
        raise RuntimeError("truck_crop_not_9x16")
    return width, height


async def _run_dossier_source(job_id: str) -> None:
    """Cut unique windows from one page's immutable source DNA."""
    job = _get_job_or_404(job_id)
    if not _job_matches_current_master_pages(job):
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error="master_pages_strategy_changed",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return
    publication = load_registered_recipe(
        job.get("recipePublicationPageId") or job["pageId"],
        job["recipeId"], job["engine"], job["recipeVersion"],
    )
    recipe = await asyncio.to_thread(_dossier_source_recipe, publication)
    if (
        recipe is None
        or job.get("sourceLibraryId") != recipe.source_library_id
        or job.get("sourceLibraryHash") != recipe.source_library_hash
        or job.get("engineRegistryHash") != recipe.engine_registry_hash
        or job.get("formatContractVersion") != recipe.format_contract_version
        or job.get("executorVersion") != recipe.executor_version
    ):
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error="source_recipe_executor_unavailable",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return

    job_root = Path(job["artifactRoot"]).resolve()
    treated_root = job_root / "treated"
    treated_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    color_correction = dossier_filters_to_color_correction(recipe)
    clip_speed = dossier_clip_speed(recipe)
    # The sourced format contract always requires 9:16 delivery. A missing
    # custom crop means the neutral centered crop, not source-aspect output.
    clip_crop = dossier_clip_crop(recipe) or {
        "zoom": 1.0, "focusX": 0.5, "focusY": 0.5,
    }
    source_cuts = job.get("sourceCuts")
    if not isinstance(source_cuts, list) or not source_cuts:
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error="source_recipe_selection_missing",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return
    manifests: list[dict[str, Any]] = []
    await asyncio.to_thread(_update_job,job_id, status="running", progress=0)
    try:
        masters = {master.sha256: master for master in recipe.masters}
        for index, source_cut in enumerate(source_cuts):
            if not _job_matches_current_master_pages(job):
                raise RuntimeError("master_pages_strategy_changed")
            if not isinstance(source_cut, dict):
                raise RuntimeError("source_recipe_manifest_invalid")
            master = masters.get(source_cut.get("masterSha256"))
            start_ms = source_cut.get("startMs")
            duration_ms = source_cut.get("durationMs")
            if (
                master is None
                or start_ms != source_cut.get("libraryStartMs")
                or not source_cut_is_planned(
                    recipe, master, start_ms, duration_ms, source_cut.get("slotId"),
                )
            ):
                raise RuntimeError("source_recipe_cut_invalid")
            source = await _cached_source_master(job["pageId"], master, job_id)
            destination = treated_root / f"source-{index:04d}.mp4"
            frame_cut = await _page_frame_cut_kwargs(
                recipe.recipe_spec["renderTreatment"], source,
            )
            await run_color_correct(
                str(source), str(destination), color_correction, scale=None,
                encode_args=delivery_encode_args(recipe.encode_preset),
                playback_speed=clip_speed,
                clip_crop=clip_crop,
                clip_crop_size=(recipe.output_width, recipe.output_height),
                clip_start_ms=start_ms,
                clip_duration_ms=duration_ms,
                **frame_cut,
            )
            manifest = _generated_manifest(job_root, destination)
            manifest["clipSpeed"] = clip_speed
            manifest["clipCrop"] = clip_crop
            manifest["sourceTreatment"] = source_treatment_receipt(
                job,
                recipe.recipe_spec["renderTreatment"],
                manifest["sha256"],
                clip_speed=clip_speed,
                clip_crop=clip_crop,
            )
            manifest["source"] = _source_provenance(job, {
                "recipeId": recipe.recipe_id,
                "sourceLibraryId": recipe.source_library_id,
                "sourceLibraryVersion": f"sha256:{recipe.source_library_hash}",
                "master": {
                    "sourceId": master.source_id,
                    "sha256": master.sha256,
                    "bytes": master.bytes,
                    "filename": master.filename,
                    "mimeType": master.mime_type,
                    "storageKey": master.storage_key,
                    "durationMs": master.duration_ms,
                    "sourceOffsetMs": master.source_offset_ms,
                    "provenance": master.provenance,
                },
                "cutWindow": {
                    "libraryStartMs": start_ms,
                    "libraryEndMs": start_ms + duration_ms,
                    "durationMs": duration_ms,
                    "originalStartMs": master.source_offset_ms + start_ms,
                    "originalEndMs": master.source_offset_ms + start_ms + duration_ms,
                },
            })
            manifest["thumbnail"] = await _thumbnail_manifest(
                job_root, destination, len(manifests),
            )
            manifests.append(manifest)
            await asyncio.to_thread(_update_job,
                job_id,
                progress=int(((index + 1) / len(source_cuts)) * 100),
            )
    except asyncio.CancelledError:
        shutil.rmtree(job_root, ignore_errors=True)
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error="generation_cancelled",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        raise
    except Exception as error:
        shutil.rmtree(job_root, ignore_errors=True)
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error=str(error)[:300],
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return
    if not _job_matches_current_master_pages(job):
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error="master_pages_strategy_changed",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return
    await asyncio.to_thread(_update_job,
        job_id,
        status="completed",
        progress=100,
        clips=manifests,
        completedAt=datetime.now(timezone.utc).isoformat(),
    )


async def _run_syzygy_slideshow(job_id: str) -> None:
    """Render reserved Syzygy plans under one page's locked recipe."""
    job = _get_job_or_404(job_id)
    if not _job_matches_current_master_pages(job):
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error="master_pages_strategy_changed",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return
    publication = load_registered_recipe(
        job.get("recipePublicationPageId") or job["pageId"],
        job["recipeId"], job["engine"], job["recipeVersion"],
    )
    recipe = (
        resolve_slideshow_recipe(publication)
        if isinstance(publication, dict)
        else None
    )
    if (
        recipe is None
        or job.get("sourceLibraryId") != recipe.library_id
        or job.get("engineRegistryHash") != recipe.engine_registry_hash
        or job.get("formatContractVersion") != recipe.format_contract_version
        or job.get("executorVersion") != recipe.executor_version
    ):
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error="slideshow_recipe_executor_unavailable",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return

    job_root = Path(job["artifactRoot"]).resolve()
    treated_root = job_root / "treated"
    treated_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    renders_root = job_root / "renders"
    renders_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    color_correction = dossier_filters_to_color_correction(recipe)
    clip_speed = dossier_clip_speed(recipe)
    # The slideshow executor always delivers 9:16. A missing custom crop
    # means the neutral centered crop, never a renderer-default frame.
    clip_crop = dossier_clip_crop(recipe) or {
        "zoom": 1.0, "focusX": 0.5, "focusY": 0.5,
    }
    plans = job.get("slideshowPlan")
    if not isinstance(plans, list) or not plans:
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error="slideshow_recipe_selection_missing",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return
    templates = {template.slug: template for template in recipe.templates}
    manifests: list[dict[str, Any]] = []
    await asyncio.to_thread(_update_job,job_id, status="running", progress=0)
    try:
        library = await asyncio.to_thread(load_recipe_library, recipe)
        if library.library_id != recipe.library_id:
            raise RuntimeError("slideshow_library_binding_changed")
        bytes_by_key = {item.key: item.bytes for item in library.objects}
        for index, plan in enumerate(plans):
            if not _job_matches_current_master_pages(job):
                raise RuntimeError("master_pages_strategy_changed")
            if not isinstance(plan, dict):
                raise RuntimeError("slideshow_plan_invalid")
            template_slug = plan.get("templateSlug")
            media_keys = plan.get("mediaKeys")
            signature = plan.get("signature")
            snapshot_hash = plan.get("librarySnapshotHash")
            planned_media = plan.get("media")
            template = (
                templates.get(template_slug)
                if isinstance(template_slug, str)
                else None
            )
            if (
                template is None
                or not isinstance(media_keys, list)
                or len(media_keys) != template.media_count
                or any(not isinstance(key, str) for key in media_keys)
                or not isinstance(signature, str)
                or not re.fullmatch(r"[0-9a-f]{64}", signature)
                or signature != hashlib.sha256(json.dumps(
                    {"templateSlug": template_slug, "mediaKeys": media_keys},
                    sort_keys=True, separators=(",", ":"),
                ).encode()).hexdigest()
                or not isinstance(snapshot_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", snapshot_hash)
                or not isinstance(planned_media, list)
                or [
                    item.get("objectKey") if isinstance(item, dict) else None
                    for item in planned_media
                ] != media_keys
                or any(
                    not isinstance(item.get("bytes"), int)
                    or isinstance(item.get("bytes"), bool)
                    or item.get("bytes") <= 0
                    for item in planned_media
                    if isinstance(item, dict)
                )
            ):
                raise RuntimeError("slideshow_plan_invalid")
            for item in planned_media:
                if bytes_by_key.get(item["objectKey"]) != item["bytes"]:
                    raise RuntimeError("slideshow_library_object_changed")
            before = await observe_library_media(library, media_keys)
            # The renderer may roll between plans; each artifact records the
            # exact revision observed immediately before its own submission.
            renderer_revision = await syzygy_revision()
            render_job_id, _quality = await submit_syzygy_render(
                plan,
                page_id=job["pageId"],
                recipe_version=job["recipeVersion"],
            )
            play_url = await poll_syzygy_render(render_job_id)
            raw_render = renders_root / f"render-{index:04d}.mp4"
            await download_syzygy_artifact(play_url, raw_render)
            after = await observe_library_media(library, media_keys)
            if after != before:
                raise RuntimeError("slideshow_source_mutated_during_render")
            destination = treated_root / f"slideshow-{index:04d}.mp4"
            await run_color_correct(
                str(raw_render), str(destination), color_correction,
                scale=None,
                encode_args=delivery_encode_args(recipe.encode_preset),
                playback_speed=clip_speed,
                clip_crop=clip_crop,
                clip_crop_size=(recipe.output_width, recipe.output_height),
            )
            manifest = _generated_manifest(job_root, destination)
            manifest["clipSpeed"] = clip_speed
            manifest["clipCrop"] = clip_crop
            manifest["sourceTreatment"] = source_treatment_receipt(
                job,
                recipe.recipe_spec["renderTreatment"],
                manifest["sha256"],
                clip_speed=clip_speed,
                clip_crop=clip_crop,
            )
            manifest["source"] = _source_provenance(job, {
                "schema": "content-lab.syzygy-slideshow-source.v1",
                "kind": "syzygy_slideshow",
                "recipeId": recipe.recipe_id,
                "recipeVersion": job["recipeVersion"],
                "sourceLibraryId": recipe.library_id,
                "librarySnapshotHash": snapshot_hash,
                "templateSlug": template_slug,
                "planSignature": signature,
                "rendererRevision": renderer_revision,
                "renderJobId": render_job_id,
                "media": after,
            })
            manifest["thumbnail"] = await _thumbnail_manifest(
                job_root, destination, len(manifests),
            )
            _claim_unique_slideshow_clip(job_id, manifest, signature)
            manifests.append(manifest)
            await asyncio.to_thread(_update_job,
                job_id,
                progress=int(((index + 1) / len(plans)) * 100),
            )
    except asyncio.CancelledError:
        # A cancelled runner must never leave the job stuck in "running"
        # with partial renders; the reservation releases with the failure.
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error="slideshow_render_cancelled",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        raise
    except Exception as error:
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error=str(error)[:300],
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return
    if not _job_matches_current_master_pages(job):
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error="master_pages_strategy_changed",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return
    await asyncio.to_thread(_update_job,
        job_id,
        status="completed",
        progress=100,
        clips=manifests,
        completedAt=datetime.now(timezone.utc).isoformat(),
    )


async def _run_page_source_import(job_id: str) -> None:
    job = _get_job_or_404(job_id)
    if not _job_matches_current_master_pages(job):
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error="master_pages_strategy_changed",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return
    job_root = Path(job["artifactRoot"]).resolve()
    source_root = job_root / "source"
    destination = source_root / "source.mp4"
    await asyncio.to_thread(_update_job,job_id, status="running", progress=5)
    try:
        async with source_import_slot():
            if not _job_matches_current_master_pages(job):
                raise RuntimeError("master_pages_strategy_changed")
            # Resolve the host again immediately before the network boundary so
            # a queued job cannot rely only on the address observed at admission.
            source_url = await _validated_source_url(job["sourceUrl"])
            imported = await download_source_video(source_url, destination)
            manifest = _generated_manifest(job_root, imported.path)
            if manifest["sha256"] != imported.sha256 or manifest["bytes"] != imported.bytes:
                raise RuntimeError("source_import_artifact_identity_mismatch")
            manifest["source"] = _source_provenance(job, {
                "schema": SOURCE_PROVENANCE_SCHEMA,
                "kind": "page_source_import",
                "format": job["format"],
                "sourceUrl": source_url,
                "sha256": imported.sha256,
                "bytes": imported.bytes,
                "mimeType": "video/mp4",
                "media": imported.media.wire(),
                "original": {
                    "sha256": imported.original_sha256,
                    "bytes": imported.original_bytes,
                    "mimeType": "video/mp4",
                    "media": imported.original_media.wire(),
                },
            })
            manifest["thumbnail"] = await _thumbnail_manifest(
                job_root, imported.path, 0,
            )
            if not _job_matches_current_master_pages(job):
                raise RuntimeError("master_pages_strategy_changed")
    except asyncio.CancelledError:
        shutil.rmtree(job_root, ignore_errors=True)
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error="source_import_cancelled",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        raise
    except Exception as error:
        shutil.rmtree(job_root, ignore_errors=True)
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error=str(error)[:300],
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return
    if not _job_matches_current_master_pages(job):
        shutil.rmtree(job_root, ignore_errors=True)
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error="master_pages_strategy_changed",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )
        return
    await asyncio.to_thread(_update_job,
        job_id,
        status="completed",
        progress=100,
        clips=[manifest],
        completedAt=datetime.now(timezone.utc).isoformat(),
    )


_generation_tasks: dict[str, asyncio.Task] = {}
_source_tasks: dict[str, asyncio.Task] = {}
_truck_recovery_tasks: dict[str, asyncio.Task] = {}
_source_import_tasks: dict[str, asyncio.Task] = {}
_syzygy_slideshow_tasks: dict[str, asyncio.Task] = {}


async def _guarded_job_runner(runner, job_id: str, setup_error: str) -> None:
    """Terminalize a runner whose setup raised before its own try block.

    The generation/source runners validate and persist their terminal states
    inside their own try/except once past setup, but their setup (recipe
    resolution, mkdir, option parsing, manifest decode) runs before that try.
    An exception there would otherwise escape the task, its done-callback
    would drop the only live task reference, and the durable row would stay
    queued/running with the current runtime id — which same-runtime expiry
    (job_status) cannot reclaim. Consume it here so the job always reaches a
    terminal state instead of becoming a permanent ghost.
    """
    try:
        await runner(job_id)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        log.exception("%s runner failed outside its guarded body", setup_error)
        await asyncio.to_thread(_update_job,
            job_id,
            status="failed",
            error=f"{setup_error}:{str(error)[:300]}",
            completedAt=datetime.now(timezone.utc).isoformat(),
        )


def _start_dossier_generation(job_id: str) -> None:
    current = _generation_tasks.get(job_id)
    if current is not None and not current.done():
        return
    task = asyncio.create_task(
        _guarded_job_runner(_run_dossier_generation, job_id, "generation_setup_failed"),
    )
    _generation_tasks[job_id] = task
    task.add_done_callback(lambda completed: _generation_tasks.pop(job_id, None)
                           if _generation_tasks.get(job_id) is completed else None)


async def shutdown_dossier_generation() -> None:
    """Checkpoint before the event loop's undifferentiated task cancellation."""
    tasks = list(_generation_tasks.values())
    for task in tasks:
        task.cancel("generation_runtime_shutdown")
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _start_dossier_source(job_id: str) -> None:
    task = asyncio.create_task(
        _guarded_job_runner(_run_dossier_source, job_id, "source_setup_failed"),
    )
    _source_tasks[job_id] = task
    task.add_done_callback(lambda _: _source_tasks.pop(job_id, None))


def _start_truck_master_recovery(job_id: str) -> None:
    task = asyncio.create_task(_run_truck_master_recovery(job_id))
    _truck_recovery_tasks[job_id] = task
    task.add_done_callback(lambda _: _truck_recovery_tasks.pop(job_id, None))


def _start_page_source_import(job_id: str) -> None:
    task = asyncio.create_task(_run_page_source_import(job_id))
    _source_import_tasks[job_id] = task
    task.add_done_callback(
        lambda completed: (
            _source_import_tasks.pop(job_id, None)
            if _source_import_tasks.get(job_id) is completed
            else None
        ),
    )


async def _cancel_page_source_import(job_id: str) -> bool:
    """Cancel and join the owned runner before its artifact root can be reused."""
    task = _source_import_tasks.get(job_id)
    if task is None:
        return False
    cancel_requested = task.cancel() if not task.done() else False
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        if _source_import_tasks.get(job_id) is task:
            _source_import_tasks.pop(job_id, None)
    return cancel_requested


def _start_syzygy_slideshow(job_id: str) -> None:
    task = asyncio.create_task(_run_syzygy_slideshow(job_id))
    _syzygy_slideshow_tasks[job_id] = task
    task.add_done_callback(
        lambda _: _syzygy_slideshow_tasks.pop(job_id, None),
    )


@router.post("/v1/source-imports")
async def create_source_import(
    x_rt_page_id: str | None = Header(default=None),
    x_rt_lane: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    body: dict[str, Any] = Body(default=None),
) -> dict[str, Any]:
    """Import one exact URL as a page-bound source artifact.

    This does not admit bytes to ShipStream or alter a source manifest. It
    produces the same durable job/status/artifact envelope replenishment
    already consumes, leaving the authoritative vault mutation downstream.
    """
    require_control_plane_bearer(authorization)
    if not x_rt_page_id or not PAGE_ID_RE.match(x_rt_page_id):
        raise HTTPException(status_code=400, detail="X-RT-Page-Id header is required")
    if x_rt_lane != CONTROL_PLANE_LANE:
        raise HTTPException(status_code=400, detail="X-RT-Lane must be content-bucket-control-plane")
    if not idempotency_key or not IDEMPOTENCY_KEY_RE.match(idempotency_key):
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required (8-200 token chars)")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="source import body must be a JSON object")
    unknown = sorted(set(body) - SOURCE_IMPORT_FIELDS)
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown source import fields: {', '.join(unknown)}")
    if set(body) != SOURCE_IMPORT_FIELDS or body.get("schema") != SOURCE_IMPORT_SCHEMA:
        raise HTTPException(status_code=400, detail="source import fields or schema are invalid")
    if len(json.dumps(body, ensure_ascii=False)) > MAX_JOB_BODY_BYTES:
        raise HTTPException(status_code=400, detail="source import body too large")
    _reject_prompt_fields(body)

    page_id = body.get("pageId")
    if page_id != x_rt_page_id:
        raise HTTPException(status_code=400, detail="body pageId must match X-RT-Page-Id")
    format_slug = body.get("format")
    if not isinstance(format_slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", format_slug):
        raise HTTPException(status_code=400, detail="format must be an exact bounded slug")
    try:
        source_url = await _validated_source_url(body.get("sourceUrl"))
    except SourceImportError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except SourceImportUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    master_pages = exact_intent(
        body.get("masterPages"), body.get("masterPagesHash"),
        expected_page_id=page_id,
    )
    if master_pages is None or master_pages.get("contentEngine") != "sourced_video":
        raise HTTPException(status_code=409, detail="source import Master Pages intent is missing, stale, or not sourced_video")
    current_master_pages = _current_master_pages_intent(page_id, master_pages)
    if current_master_pages is None or current_master_pages != (
        master_pages, body["masterPagesHash"],
    ):
        raise HTTPException(status_code=409, detail="source import Master Pages intent does not match the current roster")
    contracts, _ = load_format_contracts()
    profiles, _ = load_engine_registry()
    contract = contracts.get(format_slug)
    profile = profiles.get(format_slug)
    if (
        contract is None
        or contract.definition_status != "complete"
        or contract.content_niche != master_pages.get("contentNiche")
        or contract.content_engine != "sourced_video"
        or contract.material_source != "source_library"
        or contract.asset_type != "video/mp4"
        or profile is None
        or profile.execution_status != "commissioned"
        or profile.content_niche != contract.content_niche
        or profile.content_engine != contract.content_engine
        or profile.material_source != contract.material_source
        or profile.asset_type != contract.asset_type
        or profile.format_contract_version != f"sha256:{contract.contract_hash}"
    ):
        raise HTTPException(status_code=409, detail="source import format is not complete and commissioned for Master Pages")

    canonical_body = {**body, "sourceUrl": source_url}
    fingerprint = "sha256:" + hashlib.sha256(json.dumps(
        canonical_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    restart_existing = False
    with lock_for(_jobs_path()):
        store = _load_jobs()
        existing_id = _idempotency_job_id(store, idempotency_key)
        if existing_id and existing_id in store["jobs"]:
            existing = store["jobs"][existing_id]
            if (
                existing.get("sourceKind") != "page_source_import"
                or existing.get("sourceImportRequestHash") != fingerprint
            ):
                raise HTTPException(status_code=409, detail="idempotency key belongs to a different request")
            if (
                existing.get("status") in GENERATION_ACTIVE_STATUSES
                and existing.get("runtimeId") != _GENERATION_RUNTIME_ID
            ):
                existing.update({
                    "status": "failed",
                    "error": "source_import_runtime_restarted",
                    "completedAt": datetime.now(timezone.utc).isoformat(),
                })
            if (
                existing.get("status") == "failed"
                and existing.get("error") == "source_import_runtime_restarted"
            ):
                active_others = [
                    row for row_id, row in store["jobs"].items()
                    if row_id != existing_id
                    and isinstance(row, dict)
                    and row.get("sourceKind") == "page_source_import"
                    and row.get("status") in GENERATION_ACTIVE_STATUSES
                    and row.get("runtimeId") == _GENERATION_RUNTIME_ID
                ]
                if any(row.get("pageId") == page_id for row in active_others):
                    raise HTTPException(status_code=409, detail="source import is already active for this page")
                if len(active_others) >= MAX_CONCURRENT_SOURCE_IMPORTS:
                    raise HTTPException(status_code=409, detail="source import capacity is currently full")
                job_id = existing["jobId"]
                job_root = (
                    _generation_root() / page_id / "source-imports" / job_id
                ).resolve()
                if Path(existing.get("artifactRoot", "")).resolve() != job_root:
                    raise HTTPException(status_code=409, detail="source import artifact root is invalid")
                shutil.rmtree(job_root, ignore_errors=True)
                job_root.mkdir(parents=True, exist_ok=False, mode=0o700)
                existing.update({
                    "status": "queued",
                    "progress": 0,
                    "clips": [],
                    "artifactRoot": str(job_root),
                    "runtimeId": _GENERATION_RUNTIME_ID,
                    "token": _secrets.token_urlsafe(JOB_TOKEN_BYTES),
                    "restartedAt": datetime.now(timezone.utc).isoformat(),
                })
                existing.pop("error", None)
                existing.pop("completedAt", None)
                _save_jobs(store)
                restart_existing = True
            else:
                return {
                    "schema": RESPONSE_SCHEMA,
                    "jobId": existing["jobId"],
                    "status": existing["status"],
                }

        if not restart_existing:
            active_imports = [
                row for row in store["jobs"].values()
                if isinstance(row, dict)
                and row.get("sourceKind") == "page_source_import"
                and row.get("status") in GENERATION_ACTIVE_STATUSES
                and row.get("runtimeId") == _GENERATION_RUNTIME_ID
            ]
            if any(row.get("pageId") == page_id for row in active_imports):
                raise HTTPException(status_code=409, detail="source import is already active for this page")
            if len(active_imports) >= MAX_CONCURRENT_SOURCE_IMPORTS:
                raise HTTPException(status_code=409, detail="source import capacity is currently full")

            job_id = JOB_ID_PREFIX + _secrets.token_hex(8)
            job_root = (
                _generation_root() / page_id / "source-imports" / job_id
            ).resolve()
            job_root.mkdir(parents=True, exist_ok=False, mode=0o700)
            now = datetime.now(timezone.utc).isoformat()
            job = {
                "jobId": job_id,
                "idempotencyKey": idempotency_key,
                "sourceImportRequestHash": fingerprint,
                "pageId": page_id,
                "lane": x_rt_lane,
                "engine": "sourced_video",
                "format": format_slug,
                "sourceUrl": source_url,
                "sourceKind": "page_source_import",
                "status": "queued",
                "progress": 0,
                "clips": [],
                "artifactRoot": str(job_root),
                "quantityRequested": 1,
                "token": _secrets.token_urlsafe(JOB_TOKEN_BYTES),
                "masterPages": master_pages,
                "masterPagesHash": body["masterPagesHash"],
                "runtimeId": _GENERATION_RUNTIME_ID,
                "createdAt": now,
            }
            store["jobs"][job_id] = job
            store["byIdempotency"][idempotency_key] = job_id
            _save_jobs(store)

    _start_page_source_import(job_id)
    return {"schema": RESPONSE_SCHEMA, "jobId": job_id, "status": "queued"}


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
    bytes directly. A page-scoped dossier version uses only an explicitly
    registered generated or version-bound sourced executor.
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
    if len(json.dumps(body)) > MAX_GENERATION_JOB_BODY_BYTES:
        raise HTTPException(status_code=400, detail="job body too large")
    try:
        excluded_windows = source_window_exclusions((constraints or {}).get("sourceWindowExclusions"))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    master_pages = exact_intent(
        body.get("masterPages"), body.get("masterPagesHash"),
        expected_page_id=page_id,
    )
    if master_pages is None or master_pages["contentEngine"] != engine:
        raise HTTPException(status_code=409, detail="job Master Pages intent is missing, stale, or engine-mismatched")
    current_master_pages = _current_master_pages_intent(page_id, master_pages)
    if current_master_pages is None or current_master_pages != (master_pages, body["masterPagesHash"]):
        raise HTTPException(status_code=409, detail="job Master Pages intent does not match the current roster")

    publication_binding = load_registered_recipe_binding(
        page_id, recipe_id, engine, recipe_version,
        master_pages, body["masterPagesHash"],
    )
    if publication_binding is None:
        raise HTTPException(status_code=409, detail="hash-bound dossier publication is required")
    publication_page_id, publication = publication_binding
    if not publication_matches_master_pages(
        publication, master_pages, body["masterPagesHash"],
    ):
        raise HTTPException(status_code=409, detail="job intent does not match the registered dossier publication")
    publication_engine = publication.get("engine") if publication else None
    generation_recipe = (
        resolve_generation_recipe(publication)
        if publication_engine == "ai_video"
        else None
    )
    source_recipe = (
        await asyncio.to_thread(_dossier_source_recipe, publication)
        if publication_engine == "sourced_video"
        else None
    )
    slideshow_recipe = (
        resolve_slideshow_recipe(publication)
        if publication_engine in {"sourced_slideshow", "lyrics_slideshows"}
        else None
    )
    if (
        generation_recipe is None
        and source_recipe is None
        and slideshow_recipe is None
    ):
        raise HTTPException(status_code=409, detail="recipe_executor_unavailable")
    capability_ceiling = (
        MAX_CAPABILITY_QUANTITY
        if generation_recipe is not None
        else (source_recipe or slideshow_recipe).max_quantity
    )
    if quantity > capability_ceiling:
        raise HTTPException(
            status_code=400,
            detail=f"quantity exceeds recipe ceiling {capability_ceiling}",
        )
    provider_calls = None
    if generation_recipe is not None:
        try:
            provider_calls = generation_recipe.planned_provider_calls(quantity)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    slideshow_library = None
    if slideshow_recipe is not None:
        # The live library read is network I/O and must happen before the
        # durable jobs-store lock is held; the reservation itself is planned
        # and persisted atomically inside the lock below.
        try:
            slideshow_library = await asyncio.to_thread(
                load_recipe_library, slideshow_recipe,
            )
        except SyzygyError as error:
            raise HTTPException(
                status_code=409, detail="recipe_executor_unavailable",
            ) from error

    # Fingerprint only the validated, immutable job contract.  Jobs written by
    # older runtimes have no hash; those legacy rows remain replay-compatible.
    job_request_body = {
        "pageId": page_id,
        "lane": str(body.get("lane") or x_rt_lane),
        "engine": engine,
        "lockedRecipeId": recipe_id,
        "recipeVersion": recipe_version,
        "quantity": quantity,
        "constraints": constraints or {},
        "sourceIsolation": body.get("sourceIsolation") or None,
        "policyHash": policy_hash,
        "masterPages": master_pages,
        "masterPagesHash": body["masterPagesHash"],
    }
    job_request_hash = "sha256:" + hashlib.sha256(json.dumps(
        job_request_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()

    start_generation = False
    start_source = False
    start_truck_recovery = False
    start_slideshow = False
    with lock_for(_jobs_path()):
        store = _load_jobs()
        existing_id = _idempotency_job_id(store, idempotency_key)
        if existing_id and existing_id in store["jobs"]:
            existing = store["jobs"][existing_id]
            # Legacy rows predate request fingerprints; preserve their replay
            # behavior, but never let a hashed row be reused for new bytes.
            if existing.get("jobRequestHash") not in (None, job_request_hash):
                raise HTTPException(status_code=409, detail="idempotency_key_reused_with_different_request")
            if (_checkpointed_generation(existing)
                    and existing.get("status") in GENERATION_ACTIVE_STATUSES):
                if _needs_generation_resume(existing):
                    _start_dossier_generation(existing["jobId"])
                return {"schema": RESPONSE_SCHEMA, "jobId": existing["jobId"], "status": existing["status"]}
            if (
                existing.get("sourceKind") in ASYNC_SOURCE_KINDS
                and existing.get("status") in GENERATION_ACTIVE_STATUSES
                and existing.get("runtimeId") != _GENERATION_RUNTIME_ID
            ):
                existing.update({
                    "status": "failed",
                    "error": "generation_runtime_restarted",
                    "completedAt": datetime.now(timezone.utc).isoformat(),
                })
                _save_jobs(store)
            return {"schema": RESPONSE_SCHEMA, "jobId": existing["jobId"], "status": existing["status"]}

        job_id = JOB_ID_PREFIX + _secrets.token_hex(8)
        common = {
            "jobId": job_id, "idempotencyKey": idempotency_key,
            "jobRequestHash": job_request_hash,
            "pageId": page_id, "lane": str(body.get("lane") or x_rt_lane),
            "engine": engine, "recipeId": recipe_id,
            "recipeVersion": recipe_version, "policyHash": policy_hash,
            "recipePublicationPageId": publication_page_id,
            "masterPages": master_pages,
            "masterPagesHash": body["masterPagesHash"],
            "sourceIsolation": body.get("sourceIsolation") or None,
            "constraints": constraints or {}, "quantityRequested": quantity,
            "token": _secrets.token_urlsafe(JOB_TOKEN_BYTES),
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        recovery_masters = (
            _truck_master_candidates(
                store, page_id, provider_calls,
                content_engine=engine,
                recipe_id=recipe_id,
                generation_recipe=generation_recipe,
                current_recipe_spec_hash=publication["recipeSpecHash"],
            )
            if generation_recipe is not None
            and recipe_id == TRUCK_RECIPE_ID
            and master_pages["contentNiche"].strip().upper() == "TRUCK"
            else []
        )
        if len(recovery_masters) != provider_calls:
            # Recovery is one complete executor path, not a partial prelude to
            # a fresh provider call. An undersized candidate set cannot satisfy
            # this exact-quantity job, so fall through to fresh prompt planning.
            recovery_masters = []
        if generation_recipe is not None and recovery_masters:
            job_root = (
                _generation_root() / page_id / recipe_version / job_id
            ).resolve()
            job_root.mkdir(parents=True, exist_ok=False, mode=0o700)
            job = {
                **common,
                "sourceKind": "truck_master_recovery",
                "status": "queued",
                "progress": 0,
                "clips": [],
                "artifactRoot": str(job_root),
                "recoveryMasters": recovery_masters,
                "dossierRevision": publication["dossierRevision"],
                "recipeSpecHash": publication["recipeSpecHash"],
                "engineRegistryHash": generation_recipe.engine_registry_hash,
                "formatContractVersion": generation_recipe.format_contract_version,
                "materialSource": generation_recipe.material_source,
                "assetType": generation_recipe.asset_type,
                "executorVersion": generation_recipe.executor_version,
                "promptCatalogHash": generation_recipe.prompt_catalog_hash,
                "family": generation_recipe.family_name,
                "providerModel": generation_recipe.provider_model,
                "providerCallsPlanned": 0,
                "providerCallsCompleted": 0,
                "runtimeId": _GENERATION_RUNTIME_ID,
            }
            start_truck_recovery = True
        elif generation_recipe is not None:
            unavailable_hashes, unavailable_slots = _generated_unavailable_prompts(
                store, generation_recipe, page_id,
            )
            prompt_plan = plan_prompt_combinations(
                generation_recipe,
                idempotency_key,
                provider_calls,
                unavailable_hashes,
                unavailable_slots,
            )
            if len(prompt_plan) != provider_calls:
                raise HTTPException(status_code=409, detail="prompt_inventory_exhausted")
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
                "engineRegistryHash": generation_recipe.engine_registry_hash,
                "formatContractVersion": generation_recipe.format_contract_version,
                "materialSource": generation_recipe.material_source,
                "assetType": generation_recipe.asset_type,
                "executorVersion": generation_recipe.executor_version,
                "promptCatalogHash": generation_recipe.prompt_catalog_hash,
                "family": generation_recipe.family_name,
                "providerModel": generation_recipe.provider_model,
                "providerCallsPlanned": provider_calls,
                "providerCallsCompleted": 0,
                "promptPlan": prompt_plan,
                "generationCheckpointVersion": 1 if PROVIDERS[generation_recipe.engine]["key_id"] == "replicate" else None,
                "providerCheckpoints": {},
                "completedGenerationCalls": [],
                "runtimeId": _GENERATION_RUNTIME_ID,
            }
            start_generation = True
        elif source_recipe is not None:
            served_slots = _source_dna_unavailable_slots(
                store, source_recipe, publication["recipeVersion"],
            )
            # A per-job seed varies the time frames each run cuts; it is
            # recorded so the plan is reproducible. Capability counted with
            # the capability seed, so fall back to it rather than refuse a
            # quantity that seed can still fill near exhaustion.
            cut_plan_seed = hashlib.sha256(
                f"{idempotency_key}\0{job_id}".encode(),
            ).hexdigest()[:16]
            cuts = plan_source_cuts(
                source_recipe, quantity, served_slots, excluded_windows,
                seed=cut_plan_seed,
            )
            if len(cuts) != quantity:
                cut_plan_seed = CAPABILITY_PLAN_SEED
                cuts = plan_source_cuts(
                    source_recipe, quantity, served_slots, excluded_windows,
                    seed=cut_plan_seed,
                )
            if not cuts:
                raise HTTPException(status_code=409, detail=MASTER_WINDOWS_EXHAUSTED)
            if len(cuts) != quantity:
                raise HTTPException(status_code=409, detail="insufficient_inventory")
            job_root = (
                _generation_root() / page_id / recipe_version / job_id
            ).resolve()
            job_root.mkdir(parents=True, exist_ok=False, mode=0o700)
            job = {
                **common,
                "sourceKind": "dossier_source_dna",
                "status": "queued",
                "progress": 0,
                "clips": [],
                "artifactRoot": str(job_root),
                "sourceCuts": [{
                    "slotId": cut.slot_id,
                    "masterSha256": cut.master.sha256,
                    "libraryStartMs": cut.start_ms,
                    "startMs": cut.start_ms,
                    "durationMs": cut.duration_ms,
                } for cut in cuts],
                "cutPlanSeed": cut_plan_seed,
                "sourceLibraryId": source_recipe.source_library_id,
                "sourceLibraryHash": source_recipe.source_library_hash,
                "engineRegistryHash": source_recipe.engine_registry_hash,
                "formatContractVersion": source_recipe.format_contract_version,
                "executorVersion": source_recipe.executor_version,
                "materialSource": source_recipe.material_source,
                "assetType": source_recipe.asset_type,
                "dossierRevision": publication["dossierRevision"],
                "recipeSpecHash": publication["recipeSpecHash"],
                "runtimeId": _GENERATION_RUNTIME_ID,
            }
            start_source = True
        elif slideshow_recipe is not None:
            unavailable_signatures = _slideshow_unavailable_signatures(
                store, slideshow_recipe, page_id,
            )
            plans = plan_slideshows(
                slideshow_recipe, slideshow_library, quantity,
                unavailable_signatures, job_id,
            )
            if len(plans) != quantity:
                raise HTTPException(status_code=409, detail="slideshow_inventory_exhausted")
            # Pin the exact byte count each planned key carried at admission
            # so the runner can prove the library still holds those bytes.
            bytes_by_key = {
                item.key: item.bytes for item in slideshow_library.objects
            }
            for plan in plans:
                plan["media"] = [
                    {"objectKey": key, "bytes": bytes_by_key[key]}
                    for key in plan["mediaKeys"]
                ]
            job_root = (
                _generation_root() / page_id / recipe_version / job_id
            ).resolve()
            job_root.mkdir(parents=True, exist_ok=False, mode=0o700)
            job = {
                **common,
                "sourceKind": "syzygy_slideshow",
                "status": "queued",
                "progress": 0,
                "clips": [],
                "artifactRoot": str(job_root),
                "slideshowPlan": plans,
                "sourceLibraryId": slideshow_recipe.library_id,
                "librarySnapshotHash": slideshow_library.snapshot_hash,
                "engineRegistryHash": slideshow_recipe.engine_registry_hash,
                "formatContractVersion": slideshow_recipe.format_contract_version,
                "executorVersion": slideshow_recipe.executor_version,
                "materialSource": slideshow_recipe.material_source,
                "assetType": slideshow_recipe.asset_type,
                "dossierRevision": publication["dossierRevision"],
                "recipeSpecHash": publication["recipeSpecHash"],
                "runtimeId": _GENERATION_RUNTIME_ID,
            }
            start_slideshow = True
        store["jobs"][job_id] = job
        store["byIdempotency"][idempotency_key] = job_id
        _save_jobs(store)

    if start_generation:
        _start_dossier_generation(job_id)
    elif start_source:
        _start_dossier_source(job_id)
    elif start_truck_recovery:
        _start_truck_master_recovery(job_id)
    elif start_slideshow:
        _start_syzygy_slideshow(job_id)
    return {"schema": RESPONSE_SCHEMA, "jobId": job_id, "status": job["status"]}


def _get_job_or_404(job_id: str) -> dict[str, Any]:
    if not re.match(rf"^{JOB_ID_PREFIX}[0-9a-f]{{16}}$", job_id or ""):
        raise HTTPException(status_code=404, detail="job not found")
    job = _load_jobs()["jobs"].get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


def _source_import_active_deadline_expired(job: dict[str, Any]) -> bool:
    if (
        job.get("sourceKind") != "page_source_import"
        or job.get("status") not in GENERATION_ACTIVE_STATUSES
    ):
        return False
    try:
        active_since = datetime.fromisoformat(
            job.get("restartedAt") or job["createdAt"],
        )
        if active_since.tzinfo is None:
            active_since = active_since.replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return True
    return (
        datetime.now(timezone.utc) - active_since
    ).total_seconds() >= SOURCE_IMPORT_ACTIVE_DEADLINE_SECONDS


@router.get("/v1/jobs/{job_id}")
async def job_status(
    job_id: str,
    x_rt_page_id: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_control_plane_bearer(authorization)
    if not x_rt_page_id or not PAGE_ID_RE.match(x_rt_page_id):
        raise HTTPException(status_code=400, detail="X-RT-Page-Id header is required")
    job = _get_job_or_404(job_id)
    if job["pageId"] != x_rt_page_id:
        raise HTTPException(status_code=404, detail="job not found")
    if _needs_generation_resume(job):
        _start_dossier_generation(job_id)
    should_expire = (
        job.get("sourceKind") in ASYNC_SOURCE_KINDS
        and not _checkpointed_generation(job)
        and job.get("status") in GENERATION_ACTIVE_STATUSES
        and (
            job.get("runtimeId") != _GENERATION_RUNTIME_ID
            or _source_import_active_deadline_expired(job)
        )
    )
    if should_expire:
        cancelled_local_runner = False
        if job.get("sourceKind") == "page_source_import":
            cancelled_local_runner = await _cancel_page_source_import(job_id)
        current = _get_job_or_404(job_id)
        if cancelled_local_runner or current.get("status") in GENERATION_ACTIVE_STATUSES:
            _update_job(
                job_id,
                status="failed",
                error=(
                    "source_import_runtime_restarted"
                    if job.get("sourceKind") == "page_source_import"
                    else "generation_runtime_restarted"
                ),
                completedAt=datetime.now(timezone.utc).isoformat(),
            )
        job = _get_job_or_404(job_id)
    if job["pageId"] != x_rt_page_id:
        # A job answers only to the page it belongs to — cross-page status
        # reads would leak what other pages were served.
        raise HTTPException(status_code=404, detail="job not found")
    response = {
        "schema": RESPONSE_SCHEMA,
        "jobId": job["jobId"],
        "status": job["status"],
        "progress": int(job.get("progress") or 0),
    }
    if job["status"] == "failed" and isinstance(job.get("error"), str):
        # The executor already persists a bounded terminal reason. Keep it on
        # the page-scoped, authenticated status contract so the control plane
        # can report and recover the real seam instead of collapsing every
        # terminal failure to a generic label.
        response["error"] = job["error"][:300]
    response.update(_job_status_failure_cause(job))
    return response


# Provider failure cause on the status contract. These patterns mirror the
# Control Plane Worker's tolerant status validator (isLabFailureClass /
# isLabFailureDetail in contentLabClient.js) exactly; a value that does not
# match is dropped, never truncated or rewritten.
_STATUS_FAILURE_CLASS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")
_STATUS_FAILURE_DETAIL = re.compile(r"[A-Za-z0-9 _.:,;/()#=+-]{1,64}")
_STATUS_FAILURE_TERMINAL = frozenset({"failed", "error", "cancelled"})


def _job_status_failure_cause(job: dict[str, Any]) -> dict[str, str]:
    """Return the safe errorClass/errorDetail pair for a terminal provider failure.

    Only a job that ended in a terminal failure *because of the provider*
    carries the cause, so a class recorded by an isolated refusal can never be
    attached to an unrelated later failure (ffmpeg, cancellation, restart).
    The values are the short classifier label and provider code the executor
    stored, never the provider message; URL-shaped details are dropped too.
    """
    if (
        job.get("status") not in _STATUS_FAILURE_TERMINAL
        or job.get("error") != "provider_generation_failed"
    ):
        return {}
    cause: dict[str, str] = {}
    error_class = job.get("errorClass")
    if isinstance(error_class, str) and _STATUS_FAILURE_CLASS.fullmatch(error_class):
        cause["errorClass"] = error_class
    detail = job.get("errorDetail")
    if (
        isinstance(detail, str)
        and detail.strip(" ") == detail
        and _STATUS_FAILURE_DETAIL.fullmatch(detail)
        and "://" not in detail
    ):
        cause["errorDetail"] = detail
    return cause


def _artifact_source_treatment(job: dict[str, Any], clip: dict[str, Any]) -> dict[str, Any] | None:
    """Return producer evidence, including for completed jobs written before it was serialized.

    The legacy recovery is deliberately narrow: the persisted clip must carry
    the exact applied speed and crop, and the exact registered publication must
    still resolve to the recipe hash bound into the job. This re-exposes facts
    already persisted by the executor; it never fills treatment from a caller.
    """
    stored = clip.get("sourceTreatment")
    if isinstance(stored, dict):
        return stored
    if (
        job.get("status") != "completed"
        or job.get("sourceKind") not in {"generated", "dossier_source_dna", "syzygy_slideshow"}
        or "clipSpeed" not in clip
        or "clipCrop" not in clip
    ):
        return None
    publication = load_registered_recipe(
        job.get("recipePublicationPageId") or job["pageId"],
        job["recipeId"], job["engine"], job["recipeVersion"],
    )
    recipe_spec = typed_recipe_spec(publication) if publication else None
    if (
        recipe_spec is None
        or publication.get("recipeSpecHash") != job.get("recipeSpecHash")
    ):
        return None
    try:
        return source_treatment_receipt(
            job,
            recipe_spec["renderTreatment"],
            clip["sha256"],
            clip_speed=clip["clipSpeed"],
            clip_crop=clip["clipCrop"],
        )
    except (KeyError, TypeError, ValueError):
        return None


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
    if job.get("sourceKind") == "page_source_import":
        # The URL carries the per-job download credential. Never derive its
        # origin from caller-controlled forwarded headers or from the separate
        # Control Plane origin used to retrieve page-vault source masters.
        base = _source_artifact_origin()
    else:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host", request.url.netloc)
        base = f"{proto}://{host}"
    # Old queued jobs may have persisted a provider-call count from the former
    # keeper-yield planner. Never let that historical over-generation flood a
    # page vault: transport at most the number of complete five-crop groups
    # needed for the requested truck delivery count. Any commissioned crop
    # group crossing the quantity boundary is transported complete.
    requested = int(job.get("quantityRequested") or len(job["clips"]))
    artifact_limit = requested
    if (
        job.get("recipeId") == TRUCK_RECIPE_ID
        and job.get("sourceKind") in {"generated", "truck_master_recovery"}
    ):
        artifact_limit = math.ceil(requested / TRUCK_CROP_COUNT) * TRUCK_CROP_COUNT
    if 0 < artifact_limit < len(job["clips"]):
        boundary = job["clips"][artifact_limit - 1].get("delivery", {}).get("crop", {})
        count = {"dual": 2, "triptych": 3, "both": 5}.get(boundary.get("mode"))
        index = boundary.get("index")
        if count is not None and type(index) is int and 0 <= index < count:
            artifact_limit += count - index - 1
    artifacts = []
    for index, clip in enumerate(job["clips"][:artifact_limit]):
        artifact = {
            "url": f"{base}/api/control-plane/v1/jobs/{job_id}/download/{index}?token={job['token']}",
            "type": "video",
            # These are claims about the exact bytes behind the signed URL.
            # ShipStream re-hashes the download before admission, so a stale
            # or substituted response fails closed instead of becoming D1/R2
            # supply under provenance that no longer matches its bytes.
            "sha256": clip["sha256"],
            "bytes": clip["bytes"],
            "source": clip["source"],
            "thumbnail": {
                "url": f"{base}/api/control-plane/v1/jobs/{job_id}/thumbnail/{index}?token={job['token']}",
                "type": "image/jpeg",
                "sha256": clip["thumbnail"]["sha256"],
                "bytes": clip["thumbnail"]["bytes"],
            },
        }
        if isinstance(clip.get("delivery"), dict):
            artifact["delivery"] = clip["delivery"]
        source_treatment = _artifact_source_treatment(job, clip)
        if source_treatment is not None:
            artifact["sourceTreatment"] = source_treatment
        artifacts.append(artifact)
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
    if job.get("artifactRoot"):
        video_root = Path(job["artifactRoot"]).resolve()
        full = (video_root / rel_path).resolve()
    else:
        video_root = (PROJECTS_DIR / job["recipeId"] / "videos").resolve()
        full = (video_root / rel_path).resolve()
    if video_root not in full.parents or not full.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return FileResponse(full, media_type="video/mp4", filename=job["clips"][index]["name"])


@router.get("/v1/jobs/{job_id}/thumbnail/{index}")
def job_thumbnail(job_id: str, index: int, token: str = "") -> FileResponse:
    job = _get_job_or_404(job_id)
    if not token or not _secrets.compare_digest(token, job["token"]):
        raise HTTPException(status_code=403, detail="invalid download token")
    if index < 0 or index >= len(job["clips"]):
        raise HTTPException(status_code=404, detail="thumbnail not found")
    thumbnail = job["clips"][index].get("thumbnail")
    if not isinstance(thumbnail, dict) or not job.get("artifactRoot"):
        raise HTTPException(status_code=404, detail="thumbnail not found")
    root = Path(job["artifactRoot"]).resolve()
    full = (root / str(thumbnail.get("path") or "")).resolve()
    if root not in full.parents or not full.is_file():
        raise HTTPException(status_code=404, detail="thumbnail not found")
    return FileResponse(full, media_type="image/jpeg", filename=thumbnail["name"])


_VISUAL_RUNTIME = _secrets.token_hex(16)
_VISUAL_SWEEP_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="content-lab-visual-admission",
)
_VISUAL_SWEEP_BACKOFF_SECONDS = 30.0
_VISUAL_SWEEP_MAX_BACKOFF_SECONDS = 30.0 * 60
# The first failed scan may be followed by this many fresh sweep attempts for
# the same exact output. Only closed, deterministic failures tied to those
# exact bytes consume this terminal budget. Provider and unknown exceptions
# remain retryable because terminalizing paid supply during an outage is worse
# than retaining it behind bounded backoff.
_VISUAL_SWEEP_MAX_RETRIES = 5
_VISUAL_SWEEP_PERSISTENT_ALERT = "visual_admission_persistent_failure"
_VISUAL_OUTPUT_FAILURE_REASONS = frozenset({
    "artifact_identity_mismatch",
    "artifact_schema_invalid",
    "frame_budget_exceeded",
    "frame_count_mismatch",
    "incomplete_decode",
    "incomplete_or_changed_artifact",
    "ocr_response_invalid",
    "vision_input_budget_exceeded",
})


def _visual_sweep_item_key(index: int, clip: dict[str, Any]) -> str:
    return f"{index}:{clip.get('sha256')}:{clip.get('bytes')}"


def _visual_sweep_failure_is_output_specific(error: Exception) -> bool:
    """Return true only for a closed set of deterministic byte-bound faults.

    Provider, transport, timeout, rate-limit, HTTP 5xx and unknown exceptions
    deliberately default to transient. New exception shapes cannot silently
    acquire authority to discard paid output.
    """
    return str(error) in _VISUAL_OUTPUT_FAILURE_REASONS


def _visual_sweep_backoff_seconds(failure_count: int) -> float:
    exponent = min(max(failure_count - 1, 0), 6)
    return min(
        _VISUAL_SWEEP_MAX_BACKOFF_SECONDS,
        _VISUAL_SWEEP_BACKOFF_SECONDS * (2 ** exponent),
    )


def _observe_visual_sweep_failure(completed, job_id: str, sweep_id: str) -> None:
    """Consume a finished sweep's result so no exception sits unobserved."""
    try:
        completed.result()
    except Exception as error:
        log.exception("visual admission sweep failed job=%s sweep=%s", job_id, sweep_id)
        _record_visual_sweep_failure(job_id, sweep_id, error)


def _record_visual_sweep_failure(job_id: str, sweep_id: str, error: Exception) -> None:
    """Durably mark a failed sweep so the next poll backs off instead of
    immediately and silently resubmitting the same failing operation."""
    with lock_for(_jobs_path()):
        store = _load_jobs()
        job = store["jobs"].get(job_id)
        if job is None or job.get("visualAdmissionSweep", {}).get("id") != sweep_id:
            return
        sweep = dict(job.get("visualAdmissionSweep") or {})
        item_key = sweep.get("itemKey")
        index = sweep.get("outputIndex")
        if not isinstance(item_key, str) or not isinstance(index, int):
            return
        clips = job.get("clips", [])
        if not 0 <= index < min(100, len(clips)):
            return
        clip = clips[index]
        if item_key != _visual_sweep_item_key(index, clip):
            return
        prior_decision = job.get("visualAdmission", {}).get(str(index), {})
        if (
            prior_decision.get("reason") == "scan_failure_retry_exhausted"
            and prior_decision.get("sha256") == clip.get("sha256")
            and prior_decision.get("bytes") == clip.get("bytes")
        ):
            return
        failures = dict(job.get("visualAdmissionFailures") or {})
        previous = failures.get(item_key)
        count = int(previous.get("count") or 0) + 1 if isinstance(previous, dict) else 1
        output_specific = _visual_sweep_failure_is_output_specific(error)
        terminal_count = (
            int(previous.get("terminalFailureCount") or 0)
            if isinstance(previous, dict)
            else 0
        ) + int(output_specific)
        failed_at = datetime.now(timezone.utc).isoformat()
        failures[item_key] = {
            "count": count,
            "terminalFailureCount": terminal_count,
            "failureClass": "output_specific" if output_specific else "transient",
            "firstFailedAt": (
                previous.get("firstFailedAt")
                if isinstance(previous, dict) and previous.get("firstFailedAt")
                else failed_at
            ),
            "lastFailedAt": failed_at,
            "lastError": str(error)[:300],
        }
        job["visualAdmissionFailures"] = failures
        sweep.update({
            "id": sweep_id,
            "runtime": _VISUAL_RUNTIME,
            "running": False,
            "scanFailures": count,
            "terminalScanFailures": terminal_count,
            "scanError": str(error)[:300],
            "scanFailureClass": "output_specific" if output_specific else "transient",
            "scanFailedAt": failed_at,
            "scanBackoffSeconds": _visual_sweep_backoff_seconds(count),
        })
        if count > _VISUAL_SWEEP_MAX_RETRIES:
            log.error(
                "%s job=%s output=%s failures=%s class=%s",
                _VISUAL_SWEEP_PERSISTENT_ALERT,
                job_id,
                index,
                count,
                sweep["scanFailureClass"],
            )
        if terminal_count > _VISUAL_SWEEP_MAX_RETRIES:
            from services.visual_admission import pending_decision

            decision = pending_decision(
                page_id=job["pageId"],
                job_id=job_id,
                index=index,
                sha256=clip["sha256"],
                byte_count=clip["bytes"],
            )
            decision["reason"] = "scan_failure_retry_exhausted"
            decision["model"]["reason"] = "scanner_exception_retry_exhausted"
            job.setdefault("visualAdmission", {})[str(index)] = decision
            sweep["terminalReason"] = "scan_failure_retry_exhausted"
            log.error(
                "visual admission scan terminal job=%s output=%s failures=%s total_failures=%s",
                job_id,
                index,
                terminal_count,
                count,
            )
        job["visualAdmissionSweep"] = sweep
        _save_jobs(store)


def _visual_sweep_backed_off(sweep: dict, now: datetime) -> bool:
    try:
        failed_at = datetime.fromisoformat(sweep["scanFailedAt"])
    except (KeyError, TypeError, ValueError):
        return False
    failure_count = sweep.get("scanFailures")
    backoff = sweep.get("scanBackoffSeconds")
    if not isinstance(backoff, (int, float)) or backoff < 0:
        backoff = _visual_sweep_backoff_seconds(
            failure_count if isinstance(failure_count, int) else 1,
        )
    return (now - failed_at).total_seconds() < backoff


def _submit_visual_sweep(job_id: str, sweep_id: str):
    """Queue sweeps behind the one process-wide scanner instead of racing it.

    The returned Future is observed by a done-callback that consumes its
    result, so a parser/manifest/storage exception can never disappear into an
    unobserved Future and silently resubmit the same failing operation forever.
    """
    future = _VISUAL_SWEEP_EXECUTOR.submit(_finish_visual_sweep, job_id, sweep_id)
    future.add_done_callback(
        lambda completed: _observe_visual_sweep_failure(completed, job_id, sweep_id),
    )
    return future


def _mark_visual_sweep(
    job_id: str,
    sweep_id: str,
    running: bool,
    *,
    item_key: str | None = None,
    output_index: int | None = None,
) -> bool:
    with lock_for(_jobs_path()):
        store = _load_jobs()
        job = store["jobs"].get(job_id)
        if job is None or job.get("visualAdmissionSweep", {}).get("id") != sweep_id:
            return False
        sweep = dict(job.get("visualAdmissionSweep") or {})
        sweep.update({
            "id": sweep_id,
            "runtime": _VISUAL_RUNTIME,
            "running": running,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        })
        if item_key is not None:
            sweep["itemKey"] = item_key
        if output_index is not None:
            sweep["outputIndex"] = output_index
        job["visualAdmissionSweep"] = sweep
        _save_jobs(store)
        return True


def _finish_visual_sweep(job_id: str, sweep_id: str) -> None:
    """Scan one paid output, then requeue at the tail for fleet fairness."""
    from services.visual_admission import ALGORITHM, SCHEMA, is_final_decision, pending_decision, scan_artifact
    requeued = False

    def final_for(job, index, clip):
        previous = job.get("visualAdmission", {}).get(str(index), {})
        return (
            previous.get("schema") == SCHEMA
            and all(previous.get(key) == value for key, value in {
                "pageId": job["pageId"], "jobId": job_id,
                "outputIndex": index, "sha256": clip.get("sha256"),
                "bytes": clip.get("bytes"),
            }.items())
            and previous.get("sampling", {}).get("algorithm") == ALGORITHM
            and is_final_decision(previous)
        )

    try:
        job = _get_job_or_404(job_id)
        root = Path(job["artifactRoot"]).resolve()
        selected = None
        for index, clip in enumerate(job.get("clips", [])[:100]):
            if final_for(job, index, clip):
                continue
            selected = (index, clip)
            break
        if selected is None:
            return
        index, clip = selected
        item_key = _visual_sweep_item_key(index, clip)
        path = (root / clip["path"]).resolve()
        if not _mark_visual_sweep(
            job_id,
            sweep_id,
            True,
            item_key=item_key,
            output_index=index,
        ):
            return
        if root not in path.parents or not path.is_file():
            decision = pending_decision(page_id=job["pageId"], job_id=job_id, index=index,
                                        sha256=clip["sha256"], byte_count=clip["bytes"])
            decision["reason"] = "artifact_missing"
        else:
            decision = scan_artifact(path, page_id=job["pageId"], job_id=job_id, index=index,
                                     sha256=clip["sha256"], byte_count=clip["bytes"])
        with lock_for(_jobs_path()):
            store = _load_jobs()
            current = store["jobs"].get(job_id)
            if current is None:
                return
            current.setdefault("visualAdmission", {})[str(index)] = decision
            failures = current.get("visualAdmissionFailures")
            if isinstance(failures, dict) and item_key in failures:
                failures = dict(failures)
                failures.pop(item_key, None)
                if failures:
                    current["visualAdmissionFailures"] = failures
                else:
                    current.pop("visualAdmissionFailures", None)
            _save_jobs(store)
        if decision["reason"] != "scan_pending" and any(
            not final_for(current, candidate_index, candidate)
            for candidate_index, candidate in enumerate(current.get("clips", [])[:100])
        ):
            requeued = True
            try:
                _submit_visual_sweep(job_id, sweep_id)
            except Exception:
                requeued = False
                raise
    finally:
        if not requeued:
            _mark_visual_sweep(job_id, sweep_id, False)


@router.post("/v1/jobs/{job_id}/visual-admission/{index}")
def job_visual_admission(
    job_id: str, index: int, body: dict[str, Any],
    x_rt_page_id: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Enqueue once, then serve only byte-verified server-owned visual decisions."""
    require_control_plane_bearer(authorization)
    if not x_rt_page_id or not PAGE_ID_RE.fullmatch(x_rt_page_id):
        raise HTTPException(status_code=400, detail="X-RT-Page-Id header is required")
    job = _get_job_or_404(job_id)
    if job["pageId"] != x_rt_page_id:
        raise HTTPException(status_code=404, detail="job not found")
    if set(body) != {"sha256", "bytes"} or not isinstance(body.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", body["sha256"]) or type(body.get("bytes")) is not int:
        raise HTTPException(status_code=400, detail="exact artifact SHA and bytes required")
    if index < 0 or index >= min(100, len(job.get("clips", []))) or not job.get("artifactRoot"):
        raise HTTPException(status_code=404, detail="artifact not found")
    clip = job["clips"][index]
    if body["sha256"] != clip.get("sha256") or body["bytes"] != clip.get("bytes"):
        raise HTTPException(status_code=409, detail="artifact identity mismatch")
    root = Path(job["artifactRoot"]).resolve()
    path = (root / clip["path"]).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    from services.visual_admission import ALGORITHM, MAX_BYTES, SCHEMA, _hash, is_final_decision, pending_decision
    decision = pending_decision(page_id=x_rt_page_id, job_id=job_id, index=index,
                                sha256=body["sha256"], byte_count=body["bytes"])
    if not 1 <= body["bytes"] <= MAX_BYTES or path.stat().st_size != body["bytes"] or _hash(path) != body["sha256"]:
        decision["reason"] = "artifact_identity_mismatch"
        return decision
    queued_sweep = None
    with lock_for(_jobs_path()):
        store = _load_jobs()
        current = store["jobs"].get(job_id)
        if current is None:
            raise HTTPException(status_code=409, detail="job disappeared during scan")
        prior = current.get("visualAdmission", {}).get(str(index), {})
        same = prior.get("schema") == SCHEMA and all(prior.get(k) == decision[k] for k in ("pageId", "jobId", "outputIndex", "sha256", "bytes")) and prior.get("sampling", {}).get("algorithm") == ALGORITHM
        if same and is_final_decision(prior):
            return prior
        now = datetime.now(timezone.utc)
        if same:
            try:
                if (now - datetime.fromisoformat(prior["scannedAt"])).total_seconds() < 30:
                    return prior
            except (ValueError, KeyError, TypeError):
                pass
        sweep = current.get("visualAdmissionSweep", {})
        active = sweep.get("runtime") == _VISUAL_RUNTIME and sweep.get("running") is True
        if not active:
            if _visual_sweep_backed_off(sweep, now):
                return decision
            sweep_id = _secrets.token_hex(16)
            current["visualAdmissionSweep"] = {"id": sweep_id, "runtime": _VISUAL_RUNTIME, "running": True, "updatedAt": now.isoformat()}
            _save_jobs(store)
            queued_sweep = (job_id, sweep_id)
    if queued_sweep is not None:
        _submit_visual_sweep(*queued_sweep)
    return decision
