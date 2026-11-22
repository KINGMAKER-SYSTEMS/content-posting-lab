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

Evidence of posted footage is read from EVERY job of the page that has
clips, whatever its recipe, format or status: the raw ``source.master``
descriptor's sha and original keys count even when the descriptor no longer
parses, and a master named without a well-formed cut row in its job counts as
used. Once compaction has archived a page's jobs, the archive keeps the page's
clip digests and its cut rows: more clips than attributable rows means footage
was posted whose spans are gone, and the page gets no library at all.

A job that cuts a library master records the whole master row on the job
(``sourceLibraryMasters``), with the manifest it came from, so execution
rebuilds exactly the master it was planned on, never a re-derived one, and the
clip receipt names that manifest. The bound ``sourceLibraryId`` and hash do
not move. When the library too is used up, the page stays exhausted and the
answer says so in plain words (``NEEDS_NEW_FOOTAGE``, pointing at the footage
fetcher, control-plane-pad #227).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
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
    SourceDnaLibrary,
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
    if not isinstance(row, dict) or set(row) - _ROW_EXTRAS != MASTER_FIELDS:
        return None
    document = {
        "schema": registry.SCHEMA,
        "libraryId": _SYNTHETIC_LIBRARY_ID,
        "format": format_slug,
        "pageId": page_id,
        "masters": [{key: value for key, value in row.items() if key not in _ROW_META}],
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


# Keys a job's master row may carry beyond the registry's master fields.
_ROW_META = frozenset({"role", "gradeOverride", "libraryId", "libraryVersion"})
_ROW_EXTRAS = _ROW_META | {"origin"}


def registered_libraries(page_id: str, format_slug: str) -> list[SourceDnaLibrary]:
    """Every registered manifest for this page and format, by library id."""
    libraries: list[SourceDnaLibrary] = []
    for path in sorted(registry.MANIFEST_DIR.glob("*.json")):
        try:
            library = parse_source_dna_manifest(path.read_bytes(), path.stem)
        except (OSError, SourceDnaError):
            continue
        if library.page_id == page_id and library.format_slug == format_slug:
            libraries.append(library)
    return libraries


def registered_masters(page_id: str, format_slug: str) -> list[MasterSource]:
    """Masters of every registered manifest for this page and format, as backups."""
    return [
        replace(master, role="backup")
        for library in registered_libraries(page_id, format_slug)
        for master in library.masters
        if master.sha256 not in RETIRED_MASTER_SHAS
    ]


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


def _keys_from(url: Any, origin_id: Any) -> frozenset[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    identity = canonical_source_identity(url)
    if identity is not None and not _vault_asset_url(url):
        keys.add(("source", identity))
    if isinstance(origin_id, str) and origin_id.strip():
        keys.add(("origin", origin_id.strip()))
    return frozenset(keys)


def original_source_keys(master: MasterSource) -> frozenset[tuple[str, str]]:
    """The original footage a master was cut from, as comparable keys.

    The canonical source identity of its ``sourceUrl``, unless that URL is
    only the vault's own asset address (ShipStream writes one when the
    upstream URL is unknown, so it names the bytes, not the footage), plus
    the upstream ``origin.originSourceId`` when the master carries one. An
    empty set means the original is unknown.
    """
    return _keys_from(master.provenance.get("sourceUrl"), (master.origin or {}).get("originSourceId"))


def _raw_keys(descriptor: dict[str, Any]) -> frozenset[tuple[str, str]]:
    """Original keys straight from a clip's master descriptor, parsed or not."""
    provenance = descriptor.get("provenance")
    origin = descriptor.get("origin")
    return _keys_from(
        provenance.get("sourceUrl") if isinstance(provenance, dict) else None,
        origin.get("originSourceId") if isinstance(origin, dict) else None,
    )


@dataclass
class PageEvidence:
    """What the page's own jobs prove about footage it has posted."""

    masters: list[MasterSource] = field(default_factory=list)  # recyclable candidates
    tainted: set[str] = field(default_factory=set)             # shas that count as used
    originals: dict[tuple[str, str], set[str]] = field(default_factory=dict)  # key -> shas
    unknown: bool = False  # a source job posted clips whose master cannot be named


def page_evidence(store: dict[str, Any], page_id: str, recipe: SourceRecipe) -> PageEvidence:
    """Read every job of the page that has clips, whatever its recipe or status.

    Each clip's raw ``source.master`` descriptor gives a sha and the keys of
    its original footage, even when it no longer parses. A master is tainted
    (counts as fully used) unless its descriptor parses whole AND the same job
    holds a well-formed cut row for it. Only a parsed, untainted master cut
    under this recipe by a completed job is a recycling candidate.
    """
    evidence = PageEvidence()
    found: dict[str, MasterSource] = {}
    for job in store.get("jobs", {}).values():
        if not isinstance(job, dict) or job.get("pageId") != page_id:
            continue
        clips = job.get("clips")
        source_job = job.get("sourceKind") == "dossier_source_dna"
        if not isinstance(clips, list):
            if source_job and clips is not None:
                evidence.unknown = True
            continue
        cuts = job.get("sourceCuts")
        rows_whole = isinstance(cuts, list) and all(isinstance(cut, dict) for cut in cuts)
        recorded = {cut.get("masterSha256") for cut in cuts} if rows_whole else set()
        for clip in clips:
            source = clip.get("source") if isinstance(clip, dict) else None
            descriptor = source.get("master") if isinstance(source, dict) else None
            if not isinstance(descriptor, dict):
                if source_job:
                    evidence.unknown = True
                continue
            sha = descriptor.get("sha256")
            if not isinstance(sha, str) or not sha:
                if source_job:
                    evidence.unknown = True
                else:
                    # Another job kind posted footage it cannot name by bytes:
                    # its original is spoken for (owned by no master), but it
                    # cannot taint the whole library.
                    for key in _raw_keys(descriptor):
                        evidence.originals.setdefault(key, set()).add("")
                continue
            for key in _raw_keys(descriptor):
                evidence.originals.setdefault(key, set()).add(sha)
            master = _parse_master(descriptor, page_id, recipe.format_slug)
            if master is None or master.sha256 != sha or sha not in recorded:
                evidence.tainted.add(sha)
                continue
            if source.get("recipeId") == recipe.recipe_id and job.get("status") == "completed":
                found.setdefault(sha, master)
    evidence.masters = [found[sha] for sha in sorted(found)]
    return evidence


def archive_evidence_gap(store: dict[str, Any], page_id: str) -> bool:
    """True when an archived source clip of the page has no cut rows of its own job.

    Compaction keeps, per page, every source-job clip digest with the number
    of cut rows ITS job carried (``sourceDnaClips``), so the check is clip by
    clip: one job's rows never cover another's, and clips of other job kinds
    (renders, slideshows, truck recoveries) never count. A page whose clips
    were archived before that record existed (None, or an index without the
    key) has unknown span history, which counts as used.
    """
    index = compaction.read_archive_index(store)
    digests = (index.get("usedClipSha256") or {}).get(page_id)
    if digests is None:
        return False
    if not isinstance(digests, list):
        return True
    clips_by_page = index.get("sourceDnaClips")
    if not isinstance(clips_by_page, dict):
        return True
    clips = clips_by_page.get(page_id, [])
    if not isinstance(clips, list):
        return True
    return any(
        not isinstance(clip, dict)
        or type(clip.get("cutRows")) is not int
        or clip["cutRows"] < 1
        for clip in clips
    )


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


def library_recipe(
    recipe: SourceRecipe, store: dict[str, Any], *, cut_rows_store: dict[str, Any] | None = None,
) -> SourceRecipe | None:
    """The recipe widened to the page's library, or None when there is none.

    ``store`` gives the page's own jobs (evidence of posted footage) and the
    archive index; ``cut_rows_store`` (default ``store``) gives every page's
    source jobs, whose cut rows on a library master must all have known
    bounds. A caller holding a page-only view passes the all-pages view here.

    The bound masters stay first and unchanged (library id and hash do not
    move). Library masters come after, as backups, deduplicated by bytes and
    by original footage (a second encoding of a bound, kept or ever-posted
    original is not fresh footage; an unknown original counts as used), never
    retired, each with a canonical source identity, never tainted by the
    page's clip evidence, and only when no cut row on that master has unknown
    bounds. No library at all when a source job's clips cannot name their
    master or the archive holds posted clips without their cut rows.
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
    # owners: every original key -> the shas of the masters cut from it.
    owners: dict[tuple[str, str], set[str]] = {}
    for master in recipe.masters:
        keys = original_source_keys(master)
        if not keys:
            return None
        for key in keys:
            owners.setdefault(key, set()).add(master.sha256)
    evidence = page_evidence(store, page_id, recipe)
    if evidence.unknown:
        return None
    if archive_evidence_gap(store, page_id):
        return None
    libraries = registered_libraries(page_id, recipe.format_slug)
    for key, shas in evidence.originals.items():
        owners.setdefault(key, set()).update(shas)
    candidates: list[MasterSource] = []
    registered = [
        replace(master, role="backup")
        for library in libraries
        for master in library.masters
    ]
    for master in [*registered, *evidence.masters]:
        if master.sha256 in seen:
            continue
        seen.add(master.sha256)
        candidates.append(master)
    # An original shared by two masters of different bytes cannot have its
    # spans compared between them, so every master of that original is out:
    # one sharing a bound or posted master's original as much as any pair
    # among the candidates themselves. A master's own key never counts
    # against it (the ledger compares its spans by its sha).
    for master in candidates:
        for key in original_source_keys(master):
            owners.setdefault(key, set()).add(master.sha256)
    extra: list[MasterSource] = []
    for master in candidates:
        keys = original_source_keys(master)
        if (
            master.sha256 in RETIRED_MASTER_SHAS
            or master.sha256 in evidence.tainted
            or canonical_source_identity(master.provenance.get("sourceUrl")) is None
            or not keys
            or any(owners[key] - {master.sha256} for key in keys)
        ):
            continue
        extra.append(master)
    recyclable = recyclable_masters(extra, prior_cut_rows(cut_rows_store if cut_rows_store is not None else store))
    if not recyclable:
        return None
    return replace(recipe, masters=(*recipe.masters, *recyclable))


def _library_of(
    master: MasterSource, libraries: Iterable[SourceDnaLibrary], store: dict[str, Any], page_id: str,
) -> tuple[str, str]:
    """The manifest a library master came from: a registered one, else the clip's."""
    for library in libraries:
        if any(item.sha256 == master.sha256 for item in library.masters):
            return library.library_id, f"sha256:{library.sha256}"
    for job in store.get("jobs", {}).values():
        if not isinstance(job, dict) or job.get("pageId") != page_id:
            continue
        for clip in job.get("clips") or []:
            source = clip.get("source") if isinstance(clip, dict) else None
            descriptor = source.get("master") if isinstance(source, dict) else None
            if (
                isinstance(descriptor, dict)
                and descriptor.get("sha256") == master.sha256
                and isinstance(source.get("sourceLibraryId"), str)
                and isinstance(source.get("sourceLibraryVersion"), str)
            ):
                return source["sourceLibraryId"], source["sourceLibraryVersion"]
    return _SYNTHETIC_LIBRARY_ID, f"sha256:{master.sha256}"


def library_masters_used(
    recipe: SourceRecipe, pooled: SourceRecipe, cuts: Iterable[Any], store: dict[str, Any],
) -> list[dict[str, Any]]:
    """The job record: every library master the plan's cuts use, once each,
    with the manifest it came from (``libraryId``, ``libraryVersion``)."""
    bound = {master.sha256 for master in recipe.masters}
    page_id = recipe_page_id(pooled) or _SYNTHETIC_LIBRARY_ID
    libraries = registered_libraries(page_id, pooled.format_slug)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cut in cuts:
        master = cut.master
        if master.sha256 in bound or master.sha256 in seen:
            continue
        seen.add(master.sha256)
        library_id, library_version = _library_of(master, libraries, store, page_id)
        rows.append({**master_row(master), "libraryId": library_id, "libraryVersion": library_version})
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
        if master is None or not all(
            isinstance(row.get(key), str) and row[key].strip() for key in ("libraryId", "libraryVersion")
        ):
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


def receipt_library(recipe: SourceRecipe, job: dict[str, Any], master: MasterSource) -> tuple[str, str]:
    """The manifest a cut's receipt names: the bound one, or the recorded
    library master's own (``masters_from_job`` already proved the record whole)."""
    if master.sha256 not in {item.sha256 for item in masters_from_job(job)}:
        return recipe.source_library_id, f"sha256:{recipe.source_library_hash}"
    row = next(row for row in job[JOB_LIBRARY_MASTERS_KEY] if row["sha256"] == master.sha256)
    return row["libraryId"], row["libraryVersion"]
