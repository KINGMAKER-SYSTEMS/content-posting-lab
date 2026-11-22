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
or mirrored (a transform never changes the recorded span). That ledger is
keyed by master bytes, so the library also compares masters by the ORIGINAL
footage they were cut from (``original_source_keys``): a second encoding or
extraction of a bound or kept original is not fresh footage, and a master
whose original is unknown (only the vault's own asset address, no origin id)
cannot be told apart from anything, so it counts as used. History with
unknown bounds, or a completed job whose clips name a master its cut rows do
not, fails closed the same way: that master counts as fully used.

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
from urllib.parse import urlsplit

from services import job_store_compaction as compaction
from services import source_dna_registry as registry
from services.control_plane_sources import SourceRecipe, canonical_source_identity
from services.shipstream_source_manifest import ShipStreamNotConfigured, shipstream_origin
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


def recorded_masters(
    store: dict[str, Any], page_id: str, recipe: SourceRecipe,
) -> tuple[list[MasterSource], set[str]]:
    """Masters this page's completed source jobs cut from (clip descriptors).

    Returns ``(masters, tainted)``: ``tainted`` holds the sha of every master
    a job's clips name without a well-formed cut row for it in that same job
    (a cut container that is not a list, a row that is not a mapping, or no
    row for that master). The clips prove the footage was posted; the ledger
    cannot say which span, so such a master counts as fully used.
    """
    found: dict[str, MasterSource] = {}
    tainted: set[str] = set()
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
        cuts = job.get("sourceCuts")
        rows_whole = isinstance(cuts, list) and all(isinstance(cut, dict) for cut in cuts)
        recorded = {cut.get("masterSha256") for cut in cuts} if rows_whole else set()
        for clip in clips:
            source = clip.get("source") if isinstance(clip, dict) else None
            if not isinstance(source, dict) or source.get("recipeId") != recipe.recipe_id:
                continue
            master = _parse_master(source.get("master"), page_id, recipe.format_slug)
            if master is None:
                continue
            if master.sha256 not in recorded:
                tainted.add(master.sha256)
            found.setdefault(master.sha256, master)
    return [found[sha] for sha in sorted(found)], tainted


def _vault_asset_url(url: Any) -> bool:
    """True when a source URL is only the ShipStream vault's address for the bytes."""
    if not isinstance(url, str):
        return True
    parsed = urlsplit(url)
    if not parsed.path.startswith("/assets/"):
        return False
    try:
        return f"{parsed.scheme}://{parsed.netloc}".lower() == shipstream_origin()
    except ShipStreamNotConfigured:
        return True  # cannot tell the vault apart: treat the address as the vault's


def original_source_keys(master: MasterSource) -> frozenset[tuple[str, str]]:
    """The original footage a master was cut from, as comparable keys.

    The canonical source identity of its ``sourceUrl``, unless that URL is
    only the vault's own asset address (ShipStream writes one when the
    upstream URL is unknown, so it names the bytes, not the footage), plus
    the upstream ``origin.originSourceId`` when the master carries one. An
    empty set means the original is unknown.
    """
    keys: set[tuple[str, str]] = set()
    url = master.provenance.get("sourceUrl")
    identity = canonical_source_identity(url)
    if identity is not None and not _vault_asset_url(url):
        keys.add(("source", identity))
    origin_id = (master.origin or {}).get("originSourceId")
    if isinstance(origin_id, str) and origin_id.strip():
        keys.add(("origin", origin_id.strip()))
    return frozenset(keys)


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
    by original footage (a second encoding of a bound or kept original is not
    fresh footage; an unknown original counts as used), never retired, each
    with a canonical source identity, never named by a completed job's clips
    without a cut row, and only when no cut row on that master has unknown
    bounds.
    """
    page_id = recipe_page_id(recipe)
    if page_id is None:
        return None
    seen = {master.sha256 for master in recipe.masters}
    # The cut ledger is keyed by master bytes. Two masters of the same
    # original footage (one re-encoded or re-extracted, so a new sha) share
    # footage the ledger cannot compare, so a same-original master counts as
    # used (fail closed) rather than as fresh footage. A bound master whose
    # original is unknown cannot be told apart from anything: no library.
    originals: set[tuple[str, str]] = set()
    for master in recipe.masters:
        keys = original_source_keys(master)
        if not keys:
            return None
        originals |= keys
    recorded, tainted = recorded_masters(store, page_id, recipe)
    candidates: list[MasterSource] = []
    for master in [*registered_masters(page_id, recipe.format_slug), *recorded]:
        if master.sha256 in seen:
            continue
        seen.add(master.sha256)
        candidates.append(master)
    # An original shared by two masters of different bytes cannot have its
    # spans compared between them, so every master of that original is out:
    # neither the one sharing a bound master's original nor any pair among
    # the candidates themselves.
    shared = set(originals)
    counted: set[tuple[str, str]] = set()
    for master in candidates:
        keys = original_source_keys(master)
        shared |= keys & counted
        counted |= keys
    extra: list[MasterSource] = []
    for master in candidates:
        keys = original_source_keys(master)
        if (
            master.sha256 in RETIRED_MASTER_SHAS
            or master.sha256 in tainted
            or canonical_source_identity(master.provenance.get("sourceUrl")) is None
            or not keys
            or keys & shared
        ):
            continue
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
