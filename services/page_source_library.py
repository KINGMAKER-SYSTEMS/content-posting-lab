"""Per-page source library: every master the Lab has on record for a page.

A sourced publication binds one source library (a registered manifest or the
page's ShipStream master). When every window of that bound batch is cut, the
page used to stop (``source_windows_exhausted``) until a person brought new
footage. Owner 2026-11-21 (pad #407, part 7): before it stops, the Lab looks
at the page's whole library and cuts the spans not yet used.

The library is derived from durable truth the Lab already holds; nothing new
is registered or persisted:

* every registered manifest in ``recipes/source-dna`` for the same page and
  format (a sibling revision, ``-v1`` beside the bound ``-v2``), and
* every master this page's completed source jobs cut from (the clip's
  ``source.master`` descriptor), e.g. a ShipStream master later replaced.

Every library master is validated through the registry parser, carries a
canonical source identity (so other pages' exclusions still apply), is never
a retired master, and plans as a ``backup``: it is cut only when the bound
primaries hold no fresh window, and a master whose bytes are gone is skipped,
not a failed job. The no-repeat rule is the planner's own span bookkeeping
(``masterSha:startMs:durationMs`` on every queued, running, completed or
archived cut, whatever library it came from): any overlap is refused, so a
recycled body never shares footage with an earlier body of the page, forward
or mirrored (a transform never changes the recorded span). History with
unknown bounds fails closed: that master counts as fully used.

A job that cuts a library master records the whole master row on the job
(``sourceLibraryMasters``) so execution rebuilds exactly the master it was
planned on, never a re-derived one. The bound ``sourceLibraryId`` and hash do
not move. When the library too is used up, the page stays exhausted and the
answer says so in plain words (``NEEDS_NEW_FOOTAGE``, pointing at the footage
fetcher, control-plane-pad #227).
"""

from __future__ import annotations

from dataclasses import replace
import json
import logging
from typing import Any, Iterable

from services import job_store_compaction as compaction
from services import source_dna_registry as registry
from services.control_plane_sources import SourceRecipe, canonical_source_identity
from services.source_dna_registry import (
    MASTER_FIELDS,
    RETIRED_MASTER_SHAS,
    MasterSource,
    SourceDnaError,
    parse_source_dna_manifest,
)

# The job record of the library masters a job's cuts use (manifest rows).
JOB_LIBRARY_MASTERS_KEY = "sourceLibraryMasters"
# Rides beside X-RT-Zero-Reason on the 409; the code itself does not change.
ZERO_REASON_HELP_HEADER = "X-RT-Zero-Reason-Help"
NEEDS_NEW_FOOTAGE = (
    "needs new footage: every span of every source in this page's library is "
    "used; the footage fetcher (control-plane-pad #227) is the next step"
)
_SYNTHETIC_LIBRARY_ID = "page-source-library"

log = logging.getLogger("content_lab.page_source_library")


def recipe_page_id(recipe: SourceRecipe) -> str | None:
    """The page the bound library is tied to (resolve_source_recipe checked it)."""
    spec = getattr(recipe, "recipe_spec", None)
    master_pages = spec.get("masterPages") if isinstance(spec, dict) else None
    page_id = master_pages.get("pageId") if isinstance(master_pages, dict) else None
    return page_id if isinstance(page_id, str) and page_id else None


def master_row(master: MasterSource) -> dict[str, Any]:
    """The registry manifest row for a master (role and grade are not identity)."""
    return {
        "sourceId": master.source_id,
        "sha256": master.sha256,
        "bytes": master.bytes,
        "filename": master.filename,
        "mimeType": master.mime_type,
        "storageKey": master.storage_key,
        "durationMs": master.duration_ms,
        "sourceOffsetMs": master.source_offset_ms,
        "provenance": dict(master.provenance),
        **({"origin": dict(master.origin)} if master.origin else {}),
    }


def _parse_master(row: Any, page_id: str, format_slug: str) -> MasterSource | None:
    """One master row through the registry parser; None unless it passes whole."""
    if not isinstance(row, dict) or set(row) - {"role", "gradeOverride", "origin"} != MASTER_FIELDS:
        return None
    document = {
        "schema": registry.SCHEMA,
        "libraryId": _SYNTHETIC_LIBRARY_ID,
        "format": format_slug,
        "pageId": page_id,
        "masters": [{key: value for key, value in row.items() if key not in ("role", "gradeOverride")}],
    }
    try:
        raw = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        library = parse_source_dna_manifest(raw, _SYNTHETIC_LIBRARY_ID)
    except (SourceDnaError, TypeError, ValueError):
        return None
    (master,) = library.masters
    if master.sha256 in RETIRED_MASTER_SHAS:
        return None
    return replace(master, role="backup", grade_override=None)


def registered_masters(page_id: str, format_slug: str) -> list[MasterSource]:
    """Masters of every registered manifest for this page and format, as backups."""
    masters: list[MasterSource] = []
    for path in sorted(registry.MANIFEST_DIR.glob("*.json")):
        try:
            library = parse_source_dna_manifest(path.read_bytes(), path.stem)
        except (OSError, SourceDnaError):
            continue
        if library.page_id != page_id or library.format_slug != format_slug:
            continue
        masters.extend(
            replace(master, role="backup")
            for master in library.masters
            if master.sha256 not in RETIRED_MASTER_SHAS
        )
    return masters


def recorded_masters(store: dict[str, Any], page_id: str, recipe: SourceRecipe) -> list[MasterSource]:
    """Masters this page's completed source jobs cut from (clip descriptors)."""
    found: dict[str, MasterSource] = {}
    for job in store.get("jobs", {}).values():
        if (
            not isinstance(job, dict)
            or job.get("pageId") != page_id
            or job.get("sourceKind") != "dossier_source_dna"
            or job.get("status") != "completed"
        ):
            continue
        clips = job.get("clips")
        if not isinstance(clips, list):
            continue
        for clip in clips:
            source = clip.get("source") if isinstance(clip, dict) else None
            if not isinstance(source, dict) or source.get("recipeId") != recipe.recipe_id:
                continue
            master = _parse_master(source.get("master"), page_id, recipe.format_slug)
            if master is not None and master.sha256 not in found:
                found[master.sha256] = master
    return [found[sha] for sha in sorted(found)]


def prior_cut_rows(store: dict[str, Any]) -> list[dict[str, Any]]:
    """Every durable cut row: queued/running/completed jobs and the archive."""
    rows: list[dict[str, Any]] = []
    for job in store.get("jobs", {}).values():
        if (
            not isinstance(job, dict)
            or job.get("sourceKind") != "dossier_source_dna"
            or job.get("status") not in compaction.SOURCE_DNA_UNAVAILABLE_STATUSES
        ):
            continue
        cuts = job.get("sourceCuts")
        if isinstance(cuts, list):
            rows.extend(cut for cut in cuts if isinstance(cut, dict))
    rows.extend(
        cut for cut in compaction.read_archive_index(store).get("sourceDnaCuts", [])
        if isinstance(cut, dict)
    )
    return rows


def recyclable_masters(
    masters: Iterable[MasterSource], rows: Iterable[dict[str, Any]],
) -> tuple[MasterSource, ...]:
    """Drop every master with a cut row whose span is unknown (fail closed)."""
    by_sha: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        sha = row.get("masterSha256")
        if isinstance(sha, str):
            by_sha.setdefault(sha, []).append(row)
    kept: list[MasterSource] = []
    for master in masters:
        unknown = any(
            type(row.get("startMs")) is not int
            or type(row.get("durationMs")) is not int
            or row["startMs"] < 0
            or row["durationMs"] <= 0
            or row["startMs"] + row["durationMs"] > master.duration_ms
            for row in by_sha.get(master.sha256, ())
        )
        if unknown:
            log.warning(
                "page_source_library master=%s counts as used: a cut row has unknown bounds",
                master.sha256,
            )
            continue
        kept.append(master)
    return tuple(kept)


def library_recipe(recipe: SourceRecipe, store: dict[str, Any]) -> SourceRecipe | None:
    """The recipe widened to the page's library, or None when there is none.

    The bound masters stay first and unchanged (library id and hash do not
    move). Library masters come after, as backups, deduplicated by bytes and
    by original source (a second encoding of a bound or kept source is not
    fresh footage), never retired, each with a canonical source identity,
    and only when no cut row on that master has unknown bounds.
    """
    page_id = recipe_page_id(recipe)
    if page_id is None:
        return None
    seen = {master.sha256 for master in recipe.masters}
    # The cut ledger is keyed by master bytes. Two masters of the same
    # original source (one re-encoded or re-trimmed, so a new sha) share
    # footage the ledger cannot compare, so a same-source master counts as
    # used (fail closed) rather than as fresh footage.
    identities = {
        canonical_source_identity(master.provenance.get("sourceUrl"))
        for master in recipe.masters
    }
    extra: list[MasterSource] = []
    for master in [
        *registered_masters(page_id, recipe.format_slug),
        *recorded_masters(store, page_id, recipe),
    ]:
        identity = canonical_source_identity(master.provenance.get("sourceUrl"))
        if (
            master.sha256 in seen
            or master.sha256 in RETIRED_MASTER_SHAS
            or identity is None
            or identity in identities
        ):
            continue
        seen.add(master.sha256)
        identities.add(identity)
        extra.append(master)
    recyclable = recyclable_masters(extra, prior_cut_rows(store))
    if not recyclable:
        return None
    return replace(recipe, masters=(*recipe.masters, *recyclable))


def library_masters_used(recipe: SourceRecipe, pooled: SourceRecipe, cuts: Iterable[Any]) -> list[dict[str, Any]]:
    """The job record: every library master the plan's cuts use, once each."""
    bound = {master.sha256 for master in recipe.masters}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cut in cuts:
        master = cut.master
        if master.sha256 in bound or master.sha256 in seen:
            continue
        seen.add(master.sha256)
        rows.append(master_row(master))
    del pooled  # the record is taken from the cuts themselves
    return rows


def masters_from_job(job: dict[str, Any]) -> tuple[MasterSource, ...]:
    """Rebuild a job's recorded library masters; () unless the record is whole."""
    rows = job.get(JOB_LIBRARY_MASTERS_KEY)
    if not isinstance(rows, list) or not rows:
        return ()
    page_id = job.get("pageId")
    if not isinstance(page_id, str) or not page_id:
        page_id = _SYNTHETIC_LIBRARY_ID
    masters: list[MasterSource] = []
    for row in rows:
        master = _parse_master(row, page_id, _SYNTHETIC_LIBRARY_ID)
        if master is None:
            return ()
        masters.append(master)
    if len({master.sha256 for master in masters}) != len(masters):
        return ()
    return tuple(masters)


def with_job_masters(recipe: SourceRecipe, job: dict[str, Any]) -> SourceRecipe:
    """The execution recipe: bound masters plus the job's own recorded ones."""
    bound = {master.sha256 for master in recipe.masters}
    extra = tuple(master for master in masters_from_job(job) if master.sha256 not in bound)
    return replace(recipe, masters=(*recipe.masters, *extra)) if extra else recipe
