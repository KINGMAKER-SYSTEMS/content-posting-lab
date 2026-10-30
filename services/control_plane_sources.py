"""Page-scoped immutable-master execution for sourced-video dossiers.

Already-cut outputs are historical evidence, never refillable source DNA. A
sourced recipe becomes executable only when its typed production selection names
one registered master library bound to the exact page and format. The executor
plans varied cut time frames (a new part of the master at a new length each
time) from a recorded per-job seed and records the original-source offset
beside every output.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
import random
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, quote_plus
from pathlib import Path
from typing import Any, Callable

from services.content_engine_registry import resolve_material_profile
from services.content_format_contracts import load_format_contracts
from services.scrambler_originality import ScramblerOriginalityUnavailable
from services.control_plane_generation import (
    dossier_clip_speed,
    render_treatment_capability,
    typed_recipe_spec,
)
from services.source_dna_registry import (
    RETIRED_MASTER_SHAS,
    MasterSource,
    SourceDnaError,
    load_source_dna_library,
)
from services.shipstream_source_manifest import (
    ShipStreamSourceError,
    load_shipstream_source_dna_library,
)
from services.source_controls import source_start_ms


EXECUTOR_PATH = (
    Path(__file__).resolve().parents[1]
    / "recipes/executors/source-dna-recut.v1.json"
)
EXECUTOR_SCHEMA = "content-lab.source-dna-recut-executor.v1"
# Legacy grid (before 2026-09-25). Jobs queued by an older runtime carry
# position ids from this 9-second grid, its +3 s/+6 s re-cut phases and the
# 6-second fixed grid; source_cut_is_planned still verifies them. New plans
# no longer walk this grid: it held only ~(duration / 9 s) x 3 positions per
# master, and every recipe revision walked it again from the same first slot.
CUT_SLOT_STEP_MS = 9_000
RECUT_PHASES_MS = (0, 3_000, 6_000)
RECUT_FIXED_DURATION_MS = 6_000
# Time-frame planning (operator rule 2026-09-25: only the EXACT posted clip may
# never post again; a different time frame of the same master -- another start
# or another length -- is a new video). Every whole-second start on the master
# (plus one start ending on its last frame) times every allowed length is a
# candidate.
# Re-cut variety (operator rule 2026-09-30): each re-cut uses a different part
# of the master and a different length than the cuts before it, and supply
# never stops. plan_source_cuts scores every candidate (fresh footage first,
# then far from recent starts, then a length unlike recent lengths, then the
# least recently used footage, then a seeded random tiebreak recorded on the
# job as cutPlanSeed). When every window has been cut, the oldest is reused
# instead of refusing the job.
CUT_START_STEP_MS = 1_000
CAPABILITY_PLAN_SEED = "capability"
# Sub-second starts (operator 2026-09-30: never render the same bytes twice).
# Once every whole-second start x length window of a master is cut, new
# windows start inside each second, coarse to fine: at half a second, then at
# a quarter and three quarters. Masters carry no verified frame rate, so starts
# stay at least MIN_START_GAP_MS apart: two different starts of one length
# decode different first frames at any frame rate above 4 fps, and a window
# counts as already cut when an earlier cut of the same length starts less
# than MIN_START_GAP_MS away. When every such window is cut the master is
# exhausted (SOURCE_WINDOWS_EXHAUSTED); nothing is ever cut twice.
START_OFFSET_LEVELS_MS = ((0,), (500,), (250, 750))
SUB_SECOND_START_OFFSETS_MS = frozenset(
    offset for level in START_OFFSET_LEVELS_MS for offset in level
)
MIN_START_GAP_MS = 250
# Allowed lengths: 5 s to 9 s in 0.5 s steps (operator 2026-09-30: the
# maximum stays 9 s, the Worker's admission bound in
# control-plane-worker/src/domain/sourceVideoBounds.js).
CUT_LENGTH_MIN_MS = 5_000
CUT_LENGTH_STEP_MS = 500
CUT_LENGTH_MAX_MS = 9_000
# Variety score. Any overlap with already-cut footage (OVERLAP_PENALTY plus up
# to OVERLAP_WEIGHT plus 1 for recency) costs more than the largest distance
# and length penalty (2 x sum(RECENT_WEIGHTS) = 3.8), so fresh footage comes
# first -- except that repeating the master's last length
# (SAME_AS_LAST_LENGTH_PENALTY) costs more than any overlap.
RECENT_CUTS_REMEMBERED = 3
RECENT_WEIGHTS = (1.0, 0.6, 0.3)
NEAR_START_MS = 120_000
OVERLAP_PENALTY = 5.0
# Repeating the master's last length is allowed only when nothing else fits.
SAME_AS_LAST_LENGTH_PENALTY = 100.0
OVERLAP_WEIGHT = 4.0
NO_CUT_TIME = float("-inf")
# Most-different next window (Eric 10-28; MASTER-ITERATION-STRATEGY-1029 §3.2).
# Among eligible windows the planner picks the one most different from the
# page's last few cuts: (1) never within 120 s of the last 3 starts of that
# master while any such window exists, (2) prefer a different master/origin
# than the previous cut (honouring the 1-in-5 backup cap), (3) when a
# per-second dHash file for the master is present, maximise the minimum
# Hamming distance to the last few cuts' fingerprints. Without a fingerprint
# file the planner falls back to (1)+(2) and logs it once.
MOST_DIFFERENT_NEAR_MS = 120_000
MOST_DIFFERENT_RECENT_STARTS = 3
MOST_DIFFERENT_RECENT_CUTS = 5
# Per-second dHash file (schema originality.masters-dhash.v1): each master maps
# to `per_second` and `mirrored_per_second` lists of 16-hex dHashes indexed by
# origin-second offset. The file is read from this env path, else the repo
# path. Missing, unparseable, wrong-schema or larger than the cap files fall
# back to rules (1)+(2), logged once, and never block a plan.
MASTERS_DHASH_SCHEMA = "originality.masters-dhash.v1"
MASTERS_DHASH_ENV = "CONTENT_LAB_ORIGINALITY_MASTERS_DHASH_PATH"
MASTERS_DHASH_REPO_PATH = (
    Path(__file__).resolve().parents[1] / "recipes/originality/masters-dhash.json"
)
MASTERS_DHASH_MAX_BYTES = 8 * 1024 * 1024
_DHASH_HEX_RE = re.compile(r"\A[0-9a-f]{16}\Z")

log = logging.getLogger(__name__)
# Above this many candidate time frames a plan scores a seeded sample per cut
# instead of every candidate (a 24 h master has ~777,000).
FULL_SCAN_CANDIDATES = 5_000
SAMPLED_CANDIDATES_PER_CUT = 256
SAMPLE_ROUNDS = 4
# Why a plan is empty (409 detail at job creation; capability maxQuantity 0).
SOURCE_MASTER_TOO_SHORT = "source_master_too_short"
SOURCE_WINDOWS_EXHAUSTED = "source_windows_exhausted"
SOURCE_WINDOWS_RESERVED_ELSEWHERE = "source_windows_reserved_by_other_pages"
SOURCE_SPEED_DURATION_UNSUPPORTED = "source_speed_duration_unsupported"
# A publication names a registered library that still holds a retired master
# (Rule #1): resolve fails with this named reason instead of silently.
SOURCE_MASTER_RETIRED = "source_master_retired"
# Retired 2026-09-30 (replaced by SOURCE_WINDOWS_EXHAUSTED, which is reached
# only after every sub-second start is cut too). The name stays importable for
# tests/test_capability_job_snapshot_base_import.py, which loads a historical
# routers/control_plane.py that imports it; nothing answers with it any more.
MASTER_WINDOWS_EXHAUSTED = "master_windows_exhausted"
MIN_ORIGINAL_START_MS = 60_000
SHIPSTREAM_PAGE_MASTER_AUTHORITY = "ShipStream source-manifest.v1 exact page master"
SHIPSTREAM_HISTORICAL_AUTHORITY_PREFIX = (
    "ShipStream source-manifest.v1 page-bound historical posted cut;"
)

log = logging.getLogger("content_lab.control_plane_sources")


@dataclass(frozen=True)
class SourceRecipe:
    recipe_id: str
    format_slug: str
    engine: str
    max_quantity: int
    engine_registry_hash: str
    format_contract_version: str
    executor_id: str
    executor_version: str
    source_library_id: str
    source_library_hash: str
    masters: tuple[MasterSource, ...]
    cut_duration_ms: int
    minimum_source_start_ms: int
    output_width: int
    output_height: int
    encode_preset: str
    material_source: str
    asset_type: str
    recipe_spec: dict[str, Any]
    engine_profile_hash: str = ""

@dataclass(frozen=True)
class CutUse:
    """When a time frame was last cut: its job's completedAt or createdAt.

    ``at`` is POSIX seconds (NO_CUT_TIME when the record has no time, which
    counts as the oldest); ``order`` is the cut's position inside its job.
    """
    at: float
    order: int = 0


@dataclass(frozen=True)
class SourceCut:
    master: MasterSource
    start_ms: int
    duration_ms: int
    fixed_length: bool = False

    @property
    def slot_id(self) -> str:
        # A time-frame id names master, start and length: the same start at a
        # different length is a different clip. Speed/crop are deliberately
        # excluded, so a cosmetic re-treatment of a used time frame is not new.
        # Legacy grid positions (fixed_length=False) excluded their length.
        if self.fixed_length:
            return f"{self.master.sha256}:{self.start_ms}:{self.duration_ms}"
        return f"{self.master.sha256}:{self.start_ms}"


def _executor_contract() -> tuple[dict[str, Any], str]:
    raw = EXECUTOR_PATH.read_bytes()
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value) != {
            "schema", "executorId", "source", "selection", "reservation",
            "controls", "output",
        }
        or value.get("schema") != EXECUTOR_SCHEMA
        or value.get("executorId") != "source-dna-recut"
        or value.get("source") != "page_scoped_immutable_master"
        or value.get("selection") != "deterministic_varied_duration_without_replacement"
        or value.get("reservation") != "active_jobs_and_completed_outputs"
    ):
        raise ValueError("source DNA recut executor contract is invalid")
    controls = value.get("controls")
    cut = controls.get("cutDurationMs") if isinstance(controls, dict) else None
    if (
        not isinstance(cut, dict)
        or set(cut) != {"type", "min", "max", "step", "default"}
        or cut.get("type") != "range"
        or (cut.get("min"), cut.get("max"), cut.get("step"), cut.get("default"))
            != (5_000, 9_000, 1_000, 7_000)
    ):
        raise ValueError("source DNA recut controls are invalid")
    output = value.get("output")
    if (
        not isinstance(output, dict)
        or set(output) != {"aspectRatio", "width", "height", "encodePreset"}
        or output.get("aspectRatio") != "9:16"
        or output.get("width") != 1_080
        or output.get("height") != 1_920
        or output.get("encodePreset") != "tiktok_delivery_v1"
    ):
        raise ValueError("source DNA recut output is invalid")
    return value, hashlib.sha256(raw).hexdigest()


def source_treatment_capability() -> dict[str, Any]:
    executor, _ = _executor_contract()
    return {
        "scope": "master_source_window",
        "recutWindow": "deterministic_varied_duration_without_replacement",
        "reservation": executor["reservation"],
        "controls": dict(executor["controls"]),
        "output": dict(executor["output"]),
        **render_treatment_capability(),
    }


def _cut_duration(spec: dict[str, Any], executor: dict[str, Any]) -> int | None:
    production = spec.get("production")
    controls = production.get("controls") if isinstance(production, dict) else None
    if not isinstance(controls, dict):
        return None
    control = executor["controls"]["cutDurationMs"]
    value = controls.get("cutDurationMs", control["default"])
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and not math.isfinite(value))
        or int(value) != value
        or not control["min"] <= int(value) <= control["max"]
        or (int(value) - control["min"]) % control["step"] != 0
    ):
        return None
    return int(value)


def _minimum_source_start(spec: dict[str, Any]) -> int | None:
    """Return the page's durable earliest original-source timestamp.

    The scalar lives in the existing open production-controls map so an
    operator can pin a page away from an unusable lead-in without changing the
    shared executor catalog (and therefore without invalidating every sourced
    page's already-locked catalog version). Absence preserves the established
    raw-library/page-master defaults.
    """
    production = spec.get("production")
    controls = production.get("controls") if isinstance(production, dict) else None
    if not isinstance(controls, dict):
        return None
    return source_start_ms(controls.get("sourceStartMs", 0))


def resolve_source_recipe(
    publication: dict[str, Any],
    *,
    base_recipe_lookup: Callable[[str], dict[str, Any] | None] | None = None,
    refusal_reasons: list[str] | None = None,
) -> SourceRecipe | None:
    del base_recipe_lookup  # old derivative-project lookup is not authority
    if not isinstance(publication, dict):
        return None
    recipe_id = publication.get("recipeId")
    engine = publication.get("engine")
    if not isinstance(recipe_id, str) or engine != "sourced_video":
        return None
    spec = typed_recipe_spec(publication)
    if spec is None or spec.get("schema") not in {
        "dossier.recipe-spec.v3", "dossier.recipe-spec.v4",
    }:
        return None
    profile = resolve_material_profile(publication, spec)
    if (
        profile is None
        or profile.content_engine != engine
        or profile.material_source != "source_library"
        or profile.executor_kind != "source_dna_recut"
        or profile.executor_id is None
        or profile.executor_version is None
        or recipe_id != f"{profile.format_slug}:master"
    ):
        return None
    try:
        executor, executor_hash = _executor_contract()
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if (
        profile.executor_id != executor["executorId"]
        or profile.executor_version != f"sha256:{executor_hash}"
    ):
        return None
    production = spec.get("production")
    source_library_id = (
        production.get("sourceLibraryId") if isinstance(production, dict) else None
    )
    if not isinstance(source_library_id, str) or not source_library_id:
        return None
    master_pages = spec.get("masterPages")
    try:
        library = load_source_dna_library(source_library_id)
    except SourceDnaError:
        try:
            library = load_shipstream_source_dna_library(
                master_pages,
                page_id=str(master_pages.get("pageId") or "") if isinstance(master_pages, dict) else "",
                expected_format=profile.format_slug,
                expected_library_id=source_library_id,
            )
        except ShipStreamSourceError:
            return None
    if any(master.sha256 in RETIRED_MASTER_SHAS for master in library.masters):
        if refusal_reasons is not None:
            refusal_reasons.append(SOURCE_MASTER_RETIRED)
        log.warning(
            "%s library=%s page=%s format=%s: a publication named a library that still holds a retired master",
            SOURCE_MASTER_RETIRED, library.library_id, library.page_id, library.format_slug,
        )
        return None
    try:
        contracts, _ = load_format_contracts()
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    # Local import avoids the ingredient catalog's intentional import of
    # source_treatment_capability from this module.
    from services.dossier_ingredients import (
        is_pinned_legacy_catalog_version,
        selected_dossier_catalog_version,
    )
    supplied_catalog_version = production.get("catalogVersion")
    expected_catalog_version = None
    pinned_legacy_catalog = is_pinned_legacy_catalog_version(
        supplied_catalog_version,
        page_id=str(publication.get("pageId") or ""),
        recipe_id=str(publication.get("recipeId") or ""),
        recipe_version=str(publication.get("recipeVersion") or ""),
        dossier_revision=str(publication.get("dossierRevision") or ""),
        recipe_spec_hash=str(publication.get("recipeSpecHash") or ""),
    )
    if not pinned_legacy_catalog:
        try:
            expected_catalog_version = selected_dossier_catalog_version(
                str(master_pages.get("pageId") or "") if isinstance(master_pages, dict) else "",
                master_pages,
                str(spec.get("masterPagesHash") or ""),
                profile.format_slug,
                production,
                shipstream_library=library,
                load_shipstream=False,
            )
        except (OSError, ValueError, json.JSONDecodeError, KeyError):
            return None
    if (
        library.format_slug != profile.format_slug
        or not isinstance(master_pages, dict)
        or library.page_id != master_pages.get("pageId")
        or (
            not pinned_legacy_catalog
            and supplied_catalog_version != expected_catalog_version
        )
        or contracts.get(profile.format_slug) is None
    ):
        return None
    cut_duration_ms = _cut_duration(spec, executor)
    minimum_source_start_ms = _minimum_source_start(spec)
    if cut_duration_ms is None or minimum_source_start_ms is None:
        return None
    return SourceRecipe(
        recipe_id=recipe_id,
        format_slug=profile.format_slug,
        engine=engine,
        max_quantity=profile.max_quantity,
        engine_registry_hash=profile.registry_hash,
        engine_profile_hash=profile.authority_hash,
        format_contract_version=profile.format_contract_version,
        executor_id=profile.executor_id,
        executor_version=profile.executor_version,
        source_library_id=library.library_id,
        source_library_hash=library.sha256,
        masters=library.masters,
        cut_duration_ms=cut_duration_ms,
        minimum_source_start_ms=minimum_source_start_ms,
        output_width=executor["output"]["width"],
        output_height=executor["output"]["height"],
        encode_preset=executor["output"]["encodePreset"],
        material_source=profile.material_source,
        asset_type=profile.asset_type,
        recipe_spec=spec,
    )


def canonical_source_identity(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        url = urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            return None
        host = url.hostname.lower()
        if host in {"youtu.be", "www.youtube.com", "youtube.com", "m.youtube.com"}:
            video_id = url.path[1:] if host == "youtu.be" else next((val for key, val in parse_qsl(url.query) if key == "v"), None) if url.path == "/watch" else url.path.split("/")[2] if re.match(r"^/(shorts|embed)/", url.path) else None
            return f"https://www.youtube.com/watch?v={video_id}" if isinstance(video_id, str) and re.fullmatch(r"[\w-]{11}", video_id, re.ASCII) else None
        query = [(key, val) for key, val in parse_qsl(url.query, keep_blank_values=True) if not key.startswith("utm_") and key not in {"fbclid", "gclid"}]
        query.sort(key=lambda item: item[0])
        authority = host + (f":{url.port}" if url.port and url.port != 443 else "")
        # Match WHATWG URLSearchParams encoding used by the Worker: star is
        # literal while tilde is escaped; duplicate keys retain stable order.
        def encode(part: str) -> str:
            return quote_plus(part, safe="*").replace("~", "%7E")
        encoded_query = "&".join(f"{encode(key)}={encode(val)}" for key, val in query)
        return urlunsplit(("https", authority, url.path or "/", encoded_query, ""))
    except (ValueError, IndexError):
        return None


def source_window_exclusions(value: Any) -> list[tuple[str, str | None, int, int]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 2000:
        raise ValueError("sourceWindowExclusions must contain at most 2000 windows")
    windows = []
    for entry in value:
        fields = set(entry) if isinstance(entry, dict) else set()
        if fields not in (
            {"sourceIdentity", "startMs", "endMs"},
            {"sourceIdentity", "masterSha256", "startMs", "endMs"},
        ):
            raise ValueError("sourceWindowExclusions fields are invalid")
        identity = canonical_source_identity(entry["sourceIdentity"])
        master_sha256 = entry.get("masterSha256")
        start, end = entry["startMs"], entry["endMs"]
        if (
            identity is None
            or (master_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", master_sha256))
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= 9_007_199_254_740_991
        ):
            raise ValueError("sourceWindowExclusions identity or timeline is invalid")
        windows.append((identity, master_sha256, start, end))
    return windows


def source_cut_durations(recipe: SourceRecipe) -> tuple[int, ...]:
    """Allowed source lengths, ascending: 5 s to 9 s in 0.5 s steps.

    A length is allowed only when the source window AND the delivered clip at
    the saved playback speed (length / speed) both stay inside 5-9 s, which is
    what the Worker admits. POV Club refuses when no legal length satisfies
    both bounds; legacy formats retain their historical fallback vocabulary.
    """
    speed = dossier_clip_speed(recipe)
    lengths = tuple(
        ms for ms in range(CUT_LENGTH_MIN_MS, CUT_LENGTH_MAX_MS + 1, CUT_LENGTH_STEP_MS)
        if CUT_LENGTH_MIN_MS * speed - 1e-6 <= ms <= CUT_LENGTH_MAX_MS * speed + 1e-6
    )
    if lengths:
        return lengths
    if recipe.format_slug == "pov-club":
        return ()
    return _legacy_source_cut_durations(recipe)


def _legacy_source_cut_durations(recipe: SourceRecipe) -> tuple[int, ...]:
    """Lengths planned before 2026-09-30: the page target +/- 2 s, plus 6 s.

    Every value is adjusted by delivery_cut_duration so the delivered clip
    stays 6-11 seconds at the saved playback speed. Jobs queued under this
    vocabulary still verify (source_cut_is_planned).
    """
    values = range(
        max(5_000, recipe.cut_duration_ms - 2_000),
        min(9_000, recipe.cut_duration_ms + 2_000) + 1,
        1_000,
    )
    durations = {delivery_cut_duration(recipe, ms) for ms in values}
    durations.add(delivery_cut_duration(recipe, RECUT_FIXED_DURATION_MS))
    return tuple(sorted(durations))


def _starting_length(recipe: SourceRecipe, durations: tuple[int, ...]) -> int:
    """The allowed length nearest the page's Cut length (ties: the shorter)."""
    return min(durations, key=lambda ms: (abs(ms - recipe.cut_duration_ms), ms))


def cut_use_time(value: Any) -> float:
    """POSIX seconds of an ISO timestamp; NO_CUT_TIME (oldest) when absent."""
    if not isinstance(value, str) or not value:
        return NO_CUT_TIME
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return NO_CUT_TIME
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _first_start_ms(recipe: SourceRecipe, master: MasterSource) -> int:
    """Earliest whole-second library start honoring the original-timeline floor."""
    minimum = minimum_original_start_ms(recipe, master) - master.source_offset_ms
    return max(0, -(-minimum // CUT_START_STEP_MS) * CUT_START_STEP_MS)


def _used_time_frames(
    recipe: SourceRecipe, served_slots: set[str],
) -> dict[str, list[tuple[int, int]]]:
    """Parse served slot ids into (start, duration) time frames per master."""
    masters = {master.sha256: master for master in recipe.masters}
    used: dict[str, set[tuple[int, int]]] = {sha: set() for sha in masters}
    for slot in served_slots:
        parts = slot.split(":")
        master = masters.get(parts[0])
        if master is None or not all(part.isdigit() for part in parts[1:]):
            continue
        if len(parts) == 3:
            used[master.sha256].add((int(parts[1]), int(parts[2])))
        elif len(parts) == 2:
            # A legacy grid position id: recover the length it was cut at.
            try:
                duration = planned_source_cut_duration(recipe, master, int(parts[1]))
            except ValueError:
                continue
            used[master.sha256].add((int(parts[1]), duration))
    return {sha: sorted(frames) for sha, frames in used.items()}


def _merged_reservations(
    master: MasterSource, identity: str | None,
    exclusions: list[tuple[str, str | None, int, int]] | None,
) -> list[tuple[int, int]]:
    """Other pages' reservations of this master on its library timeline, merged."""
    merged: list[list[int]] = []
    for begin, end in sorted(
        (start - master.source_offset_ms, end - master.source_offset_ms)
        for source, master_sha256, start, end in (exclusions or [])
        if source == identity and (master_sha256 is None or master_sha256 == master.sha256)
    ):
        if merged and begin < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([begin, end])
    return [(begin, end) for begin, end in merged]


def _start_runs(
    master: MasterSource, first_ms: int, duration_ms: int,
    reserved: list[tuple[int, int]], *, include_end: bool = True,
) -> list[tuple[int, int, int]]:
    """Candidate starts for one length as ascending ``(first, step, count)`` runs.

    Whole-second starts from ``first_ms``, then one start ending on the last
    frame, minus every start whose window overlaps a reservation. Reserved
    stretches are skipped arithmetically, never enumerated.
    """
    last = master.duration_ms - duration_ms
    if last < first_ms:
        return []

    def free(start: int) -> bool:
        index = bisect_left(reserved, (start + duration_ms, -1))
        return not (index and reserved[index - 1][1] > start)

    runs: list[tuple[int, int, int]] = []
    low = first_ms
    for begin, end in [*reserved, (last + duration_ms, last + duration_ms)]:
        # Starts in [low, begin - duration] do not reach this reservation.
        high = min(last, begin - duration_ms)
        if high >= low:
            k_low = -(-(low - first_ms) // CUT_START_STEP_MS)
            k_high = (high - first_ms) // CUT_START_STEP_MS
            if k_high >= k_low:
                runs.append((first_ms + k_low * CUT_START_STEP_MS, CUT_START_STEP_MS, k_high - k_low + 1))
        low = max(low, end)
        if low > last:
            break
    if include_end and (last - first_ms) % CUT_START_STEP_MS and free(last):
        runs.append((last, CUT_START_STEP_MS, 1))
    return runs


class _Lane:
    """One master's planning state: its candidates and its cut history."""

    def __init__(
        self, recipe: SourceRecipe, master: MasterSource,
        frames: list[tuple[int, int]], history: dict[str, CutUse],
        exclusions: list[tuple[str, str | None, int, int]] | None,
        durations: tuple[int, ...],
    ) -> None:
        self.master = master
        identity = canonical_source_identity(master.provenance.get("sourceUrl"))
        self.reserved = _merged_reservations(master, identity, exclusions)
        self.first = _first_start_ms(recipe, master)
        # levels[k]: [(length, runs)] for starts at START_OFFSET_LEVELS_MS[k]
        # inside each second (level 0 also ends on the last frame).
        self.levels = [
            [
                (duration_ms, [
                    run
                    for offset in offsets
                    for run in _start_runs(
                        master, self.first + offset,
                        duration_ms, self.reserved, include_end=not level,
                    )
                ])
                for duration_ms in durations
            ]
            for level, offsets in enumerate(START_OFFSET_LEVELS_MS)
        ]
        self.counts = [
            sum(count for _, rows in runs for _, _, count in rows)
            for runs in self.levels
        ]
        self.frames = frames
        # Starts already cut, per length (see is_taken).
        self.taken: dict[int, list[int]] = {}
        for start, duration in frames:
            self.taken.setdefault(duration, []).append(start)
        for starts in self.taken.values():
            starts.sort()
        uses = [history.get(f"{master.sha256}:{start}:{duration}") for start, duration in frames]
        self.keys = [
            (use.at, use.order) if use is not None else (NO_CUT_TIME, 0)
            for use in uses
        ]
        self.key_of = dict(zip(frames, self.keys))
        self.distinct_keys = sorted(set(self.keys))
        # Footage already cut, as merged intervals, and the newest cut that
        # touched each second of the master: scoring a candidate costs a few
        # steps however densely the master has been cut.
        merged: list[list[int]] = []
        for start, duration in frames:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], start + duration)
            else:
                merged.append([start, start + duration])
        self.covered = [(begin, end) for begin, end in merged]
        self.newest_by_second: dict[int, tuple[float, int]] = {}
        for (start, duration), key in zip(frames, self.keys):
            for second in range(start // 1_000, (start + duration - 1) // 1_000 + 1):
                known = self.newest_by_second.get(second)
                if known is None or key > known:
                    self.newest_by_second[second] = key
        # Newest first. Only cuts with a known time count as "recent": an old
        # archive entry without one says nothing about what was cut last.
        dated = sorted(
            (key, frame) for key, frame in zip(self.keys, frames)
            if key[0] != NO_CUT_TIME
        )
        self.recent = [frame for _, frame in reversed(dated[-RECENT_CUTS_REMEMBERED:])]
        span = max(0, master.duration_ms - durations[0] - self.first)
        self.near_ms = min(max(span / 3, CUT_START_STEP_MS), NEAR_START_MS)
        self.chosen: list[tuple[int, int]] = []

    def overlaps_chosen(self, start: int, duration: int) -> bool:
        return any(
            other_start < start + duration and start < other_start + other_duration
            for other_start, other_duration in self.chosen
        )

    def reserved_overlap(self, start: int, duration: int) -> bool:
        index = bisect_left(self.reserved, (start + duration, -1))
        return bool(index and self.reserved[index - 1][1] > start)

    def static_score(self, start: int, duration: int) -> tuple[float, tuple[float, int]]:
        """(footage-reuse penalty, newest cut touching it) for one candidate.

        Any overlap with footage this master already cut costs more than any
        distance or length penalty except repeating the last length, so fresh
        footage comes first; more of the window already cut costs more. When
        a window must overlap, footage cut longest ago is preferred: the age
        term is the rank of the newest cut touching the window's seconds, 0
        for the oldest and 1 for the newest.
        """
        end = start + duration
        covered = 0
        index = max(0, bisect_left(self.covered, (start, -1)) - 1)
        while index < len(self.covered) and self.covered[index][0] < end:
            begin, finish = self.covered[index]
            covered += max(0, min(end, finish) - max(start, begin))
            index += 1
        if not covered:
            return 0.0, (NO_CUT_TIME, -1)
        newest = max(
            (self.newest_by_second[second]
             for second in range(start // 1_000, (end - 1) // 1_000 + 1)
             if second in self.newest_by_second),
            default=(NO_CUT_TIME, 0),
        )
        rank = bisect_left(self.distinct_keys, newest) / max(1, len(self.distinct_keys) - 1)
        return OVERLAP_PENALTY + OVERLAP_WEIGHT * covered / duration + rank, newest

    def variety_penalty(self, start: int, duration: int, starting_length: int) -> float:
        """Penalty for sitting near recent starts or repeating recent lengths.

        The first cut on a master (no dated history) uses the page's Cut
        length. After that, a start within ``near_ms`` of a recent start and a
        length equal to (or 0.5 s from) a recent length cost more, newest cut
        weighted most. Repeating the last length costs
        ``SAME_AS_LAST_LENGTH_PENALTY`` (more than any overlap), so a fresh
        length wins at the same start level before a finer start is
        considered; the fingerprint distance then only breaks ties among
        otherwise-equal windows.
        """
        if not self.recent:
            return 0.0 if duration == starting_length else 1.0
        penalty = SAME_AS_LAST_LENGTH_PENALTY if duration == self.recent[0][1] else 0.0
        for weight, (recent_start, recent_duration) in zip(RECENT_WEIGHTS, self.recent):
            distance = abs(start - recent_start)
            if distance < self.near_ms:
                penalty += weight * (1 - distance / self.near_ms)
            if recent_duration == duration:
                penalty += weight
            elif abs(recent_duration - duration) <= CUT_LENGTH_STEP_MS:
                penalty += weight / 2
        return penalty

    def repeats_last_length(self, duration: int) -> bool:
        """True when ``duration`` repeats this master's most recent cut length."""
        return bool(self.recent) and duration == self.recent[0][1]

    def is_taken(self, start: int, duration: int) -> bool:
        """True when a cut of this length starts less than MIN_START_GAP_MS away.

        Starts at least that far apart decode different first frames at any
        frame rate above 4 fps; closer ones may decode the same frames. This
        also catches an end-on-last-frame start a few ms from a grid start.
        """
        starts = self.taken.get(duration)
        if not starts:
            return False
        index = bisect_left(starts, start - MIN_START_GAP_MS + 1)
        return index < len(starts) and starts[index] < start + MIN_START_GAP_MS

    def record(self, start: int, duration: int) -> None:
        self.chosen.append((start, duration))
        starts = self.taken.setdefault(duration, [])
        starts.insert(bisect_left(starts, start), start)
        self.recent = [(start, duration), *self.recent][:RECENT_CUTS_REMEMBERED]


# Per-second dHash fingerprints (originality). Read once per (path, mtime, size)
# and only when the file is present, under 8 MB and schema-valid; every failure
# falls back to rules (1)+(2) and is logged at most once per process so a bad
# file can never block a plan or spam the logs.
_dhash_logged: set[str] = set()
_dhash_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


def _dhash_log_once(key: str, message: str, level: int = logging.WARNING) -> None:
    if key in _dhash_logged:
        return
    _dhash_logged.add(key)
    # The key rides as the first %-argument so the emitted line carries the
    # named condition (tests capture the actual formatted warning).
    log.log(level, "content_lab.control_plane_sources.%s " + message, key)


def _parse_dhash_hex(value: Any) -> int:
    if not isinstance(value, str) or not _DHASH_HEX_RE.match(value):
        raise ValueError("not a 16-hex dHash")
    return int(value, 16)


def _parse_masters_dhash(value: Any) -> dict[str, dict[str, tuple[int, ...]]] | None:
    """Validate ``originality.masters-dhash.v1`` and return int-hash maps."""
    if not isinstance(value, dict) or value.get("schema") != MASTERS_DHASH_SCHEMA:
        return None
    masters = value.get("masters")
    if not isinstance(masters, dict):
        return None
    parsed: dict[str, dict[str, tuple[int, ...]]] = {}
    for sha, entry in masters.items():
        if (
            not isinstance(sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sha)
            or not isinstance(entry, dict)
        ):
            return None
        per = entry.get("per_second")
        mir = entry.get("mirrored_per_second")
        if (
            not isinstance(per, list)
            or not isinstance(mir, list)
            or not per
            or len(per) != len(mir)
        ):
            return None
        try:
            per_ints = tuple(_parse_dhash_hex(item) for item in per)
            mir_ints = tuple(_parse_dhash_hex(item) for item in mir)
        except (TypeError, ValueError):
            return None
        parsed[sha] = {"per_second": per_ints, "mirrored_per_second": mir_ints}
    return parsed


def _masters_dhash_path() -> Path | None:
    configured = os.environ.get(MASTERS_DHASH_ENV)
    if configured:
        return Path(configured)
    if MASTERS_DHASH_REPO_PATH.exists():
        return MASTERS_DHASH_REPO_PATH
    return None


def load_masters_dhash() -> dict[str, dict[str, tuple[int, ...]]]:
    """Return {master sha256: fingerprints} or {} when unavailable."""
    path = _masters_dhash_path()
    if path is None:
        _dhash_log_once(
            "absent",
            "no per-second dHash file (env %s unset and %s missing); most-different "
            "falls back to the 120 s and origin rules" % (MASTERS_DHASH_ENV, MASTERS_DHASH_REPO_PATH),
            logging.INFO,
        )
        return {}
    try:
        stat = path.stat()
    except OSError as exc:
        _dhash_log_once(f"stat:{path}", "cannot stat dHash file %s (%s); falling back" % (path, exc))
        return {}
    key = str(path)
    cached = _dhash_cache.get(key)
    if cached is not None and cached[0] == (stat.st_mtime_ns, stat.st_size):
        return cached[1]
    if stat.st_size > MASTERS_DHASH_MAX_BYTES:
        _dhash_log_once(f"size:{path}", "dHash file %s exceeds 8 MB; falling back" % path)
        return {}
    try:
        with path.open("rb") as handle:
            raw = handle.read(MASTERS_DHASH_MAX_BYTES + 1)
        if len(raw) > MASTERS_DHASH_MAX_BYTES:
            _dhash_log_once(f"size:{path}", "dHash file %s exceeds 8 MB; falling back" % path)
            return {}
    except OSError as exc:
        _dhash_log_once(f"read:{path}", "cannot read dHash file %s (%s); falling back" % (path, exc))
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        _dhash_log_once(f"parse:{path}", "dHash file %s is not valid JSON; falling back" % path)
        return {}
    parsed = _parse_masters_dhash(value)
    if parsed is None:
        _dhash_log_once(f"schema:{path}", "dHash file %s is not schema %s; falling back" % (path, MASTERS_DHASH_SCHEMA))
        return {}
    _dhash_cache[key] = ((stat.st_mtime_ns, stat.st_size), parsed)
    return parsed


def _hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def _fingerprint_at(
    fp: dict[str, tuple[int, ...]] | None, master: MasterSource, start_ms: int,
) -> tuple[int, int] | None:
    """The (normal, mirrored) dHash of a cut's first origin second."""
    if not fp:
        return None
    per, mir = fp.get("per_second"), fp.get("mirrored_per_second")
    if not per or not mir:
        return None
    index = (master.source_offset_ms + start_ms) // 1_000
    if index < 0 or index >= len(per) or index >= len(mir):
        return None
    return (per[index], mir[index])


def _fingerprint_pair_distance(left: tuple[int, int], right: tuple[int, int]) -> int:
    """Min Hamming distance across normal/mirrored pairings (mirror = same frame)."""
    left_norm, left_mirr = left
    right_norm, right_mirr = right
    return min(
        _hamming(left_norm, right_norm),
        _hamming(left_norm, right_mirr),
        _hamming(left_mirr, right_norm),
        _hamming(left_mirr, right_mirr),
    )


def _recent_page_cuts(
    recipe: SourceRecipe, history: dict[str, CutUse],
) -> list[tuple[MasterSource, int, int]]:
    """The page's most recent cuts (newest first) as (master, start_ms, duration).

    The duration rides along so the Scrambler's full-window dHash maximin can
    compare every origin second a recent cut covered, not just its first frame.
    A legacy ``sha:start`` slot (no duration) is skipped: its window is unknown.
    """
    masters = {master.sha256: master for master in recipe.masters}
    ordered = sorted(history.items(), key=lambda item: (item[1].at, item[1].order))
    cuts: list[tuple[MasterSource, int, int]] = []
    for slot, _use in ordered:
        sha, _, rest = slot.partition(":")
        master = masters.get(sha)
        start_text, sep, duration_text = rest.partition(":")
        if master is None or not sep or not start_text.isdigit() or not duration_text.isdigit():
            continue
        cuts.append((master, int(start_text), int(duration_text)))
    return list(reversed(cuts))[:MOST_DIFFERENT_RECENT_CUTS]


@dataclass(frozen=True)
class ScramblerCanonicalStart:
    """Private prior-cut coordinates independent of the active library."""
    origin_kind: str
    origin_id: str
    start_ms: int
    at: float
    order: int
    master_sha256: str
    library_start_ms: int


def _scrambler_row_origin(originality, row):
    """Use immutable private coordinates; verify any surviving producer map."""
    from services.scrambler import ScramblerHistoryCorrupt
    durable = None
    if row.get("originKind") is not None:
        durable = (row["originKind"], row["originId"],
                   row["originStartMs"], row["originEndMs"])
    try:
        mapped = originality.canonical_origin_window(
            row["masterSha256"], row["libraryStartMs"] + row["offsetMs"], row["windowMs"])
    except ScramblerOriginalityUnavailable as error:
        # A rotated-away map cannot erase proven coordinates. A present but
        # malformed/changed map is different: it must still refuse closed.
        if durable is not None and str(error).startswith("origin_mapping_unknown:"):
            return durable
        raise
    if (row.get("originStartMs"), row.get("originEndMs")) != mapped[2:] or (
            durable is not None and durable != mapped):
        raise ScramblerHistoryCorrupt("scrambler_origin_binding_mismatch")
    return durable or mapped


def scrambler_recent_canonical_starts(originality, page_id: str, current_job_id: str):
    """Own-page starts from every durable live row, including removed aliases.

    Posted observations release unposted HOLDs only. They do not erase variety
    history. New rows retain immutable canonical identity when the producer
    rotates the original master map away; legacy rows without that identity
    still require their original map and refuse when it is missing.
    """
    from services.scrambler import live_reservation_rows, ScramblerHistoryCorrupt
    starts = []
    for order, row in enumerate(live_reservation_rows()):
        if row["pageId"] != page_id or row["jobId"] == current_job_id:
            continue
        master = row.get("masterSha256")
        if master is None:
            continue
        kind, oid, start, end = _scrambler_row_origin(originality, row)
        starts.append(ScramblerCanonicalStart(
            kind, oid, row["originStartMs"],
            cut_use_time(row.get("renderedAt") or row["drawnAt"]), order,
            master, row["libraryStartMs"]))
    return tuple(sorted(starts, key=lambda item: (item.at, item.order), reverse=True))


def scrambler_pending_windows(originality, current_job_id: str):
    """Unposted commitments remain held across pages and master aliases.

    Only exact actual-post evidence releases a rendered hold, except a cut its
    job never delivered, released after a grace period. Persist the
    observation before omitting it so feed rotation cannot resurrect the hold.
    """
    from services.scrambler import (
        live_reservation_rows, mark_posting_observed, ScramblerHistoryCorrupt,
    )
    held = []
    for row in live_reservation_rows():
        if row["jobId"] == current_job_id:
            continue
        if row.get("postingObservation") is not None:
            continue
        if row.get("holdRelease") is not None:
            # A rendered cut its job never delivered (services.scrambler
            # release_undelivered_holds): it can never post, so it holds no window.
            continue
        master = row.get("masterSha256")
        if master is None:
            # No source footage has yet been assigned to this draw.
            continue
        window = _scrambler_row_origin(originality, row)
        kind, oid, start, end = window
        asset = row.get("assetSha256")
        observation = (originality.posted_asset_observation(asset, kind, oid, start, end)
                       if asset else None)
        if observation is not None:
            mark_posting_observed(
                row["pageId"], row["jobId"], row["cutIndex"],
                asset_sha256=asset, posted_at=observation["posted_at"],
                page_handle=observation["page"])
            continue
        held.append(window)
    return tuple(held)


@dataclass(frozen=True)
class ScramblerPlan:
    """The Scrambler's plan-time context (only passed when the page is ON).

    ``spans`` holds, per planned cut in order, the exact reserved source window
    (sub-second offset + length x effective speed, ms) the caller already drew
    and reserved atomically in the scrambler ledger. Planning a cut at its
    drawn span is what keeps the reservation truthful: a 9 s output at 1.3x
    reserves an 11.7 s window BEFORE the window is chosen, never a slow-sized
    window that is sped up afterwards.
    ``originality`` is the real-ledger show index (services.scrambler_originality);
    it is REQUIRED on the ON path (the loader itself fails closed when the
    feed is unconfigured or unusable), and ``page_handles`` carries the page's
    real Master Pages handle so the ledger's @handle rows are recognised as
    this page's OWN shows rather than another page's.
    """

    page_id: str
    spans: tuple[int, ...]
    originality: Any = None
    page_handles: frozenset[str] = frozenset()
    pending_windows: tuple[tuple[str, str, int, int], ...] = ()
    prior_canonical_starts: tuple[ScramblerCanonicalStart, ...] = ()


def plan_source_cuts(
    recipe: SourceRecipe,
    quantity: int,
    served_slots: set[str],
    exclusions: list[tuple[str, str | None, int, int]] | None = None,
    *,
    seed: str = CAPABILITY_PLAN_SEED,
    history: dict[str, CutUse] | None = None,
    scrambler: ScramblerPlan | None = None,
) -> list[SourceCut]:
    """Plan up to ``quantity`` never-cut windows, as varied as possible.

    Every cut path goes through here (tests/test_source_cut_path_census.py).
    Without ``scrambler`` this is exactly the base planner (verified
    byte-identical, pick order included, by tests/test_scrambler_executor_behavior.py):
    candidates are every whole-second start on the master's timeline (from
    the page floor; plus one start ending on the last frame) at every allowed
    length (source_cut_durations). Only when every one of those is cut do
    starts move inside the second, coarse to fine (START_OFFSET_LEVELS_MS:
    half a second, then a quarter and three quarters). Each candidate is
    scored, lower is better:

    1. repeating the master's last length (only when nothing else fits),
    2. overlap with footage this master already cut (fresh footage first;
       when everything overlaps, footage cut longest ago wins),
    3. closeness of its start to the last few starts on this master,
    4. the same (or a 0.5 s different) length as the last few cuts,

    then least-recently-used footage, then a seeded random tiebreak, so the
    same seed and inputs always yield the same plan. Every pick updates the
    "recent" state, so one multi-clip plan is itself spread out, and one plan
    never holds two overlapping cuts of a master. The first cut on a master
    uses the page's Cut length. Recency and variety are per master, across
    every page that cut it.

    With ``scrambler`` (the page's flag ON), each cut is planned at its
    already-reserved span instead of the recipe vocabulary, under the
    most-different rules: never within 120 s of the lane's last 3 completed
    starts while a farther window exists (the relaxation is named), a
    deterministic origin switch after a primary (inside the 1-in-5 backup
    cap), and the per-second dHash maximin over the FULL candidate window
    against the page's last 3-5 cuts. Windows whose origin seconds break the
    originality show limits (real ledger, when configured) are never
    candidates.

    ``served_slots`` holds every time frame already cut or reserved (slot ids
    ``sha:start:duration``; legacy ``sha:start`` grid ids resolve to the length
    they were cut at). No window within MIN_START_GAP_MS of one of those (same
    length) is ever planned: a cut never renders the same bytes twice.
    ``history`` maps ``sha:start:duration`` to when it was cut (CutUse); a
    window without one counts as the oldest. Windows overlapping another
    page's reservation (``exclusions``) are never candidates.

    The plan is empty or short only when no further never-cut window fits:
    explain_empty_source_plan names why an empty plan is empty
    (SOURCE_MASTER_TOO_SHORT, SOURCE_WINDOWS_EXHAUSTED or
    SOURCE_WINDOWS_RESERVED_ELSEWHERE). A coarser never-cut window that does
    not fit beside the plan's other cuts ends the plan; the next plan cuts it.

    A start level with more than FULL_SCAN_CANDIDATES candidates scores a
    seeded sample of SAMPLED_CANDIDATES_PER_CUT candidates per cut (up to
    SAMPLE_ROUNDS samples while none is usable), so a 24 h master stays cheap
    on every capability poll; a sample that finds nothing usable is confirmed
    by an exact pass over the level, so no never-cut window is passed over.
    """
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        raise ValueError("quantity must be a positive integer")
    if not isinstance(served_slots, set) or any(
        not isinstance(value, str) for value in served_slots
    ):
        raise ValueError("served_slots must be a string set")
    if not isinstance(seed, str):
        raise ValueError("seed must be a string")
    if history is not None and (
        not isinstance(history, dict)
        or any(not isinstance(use, CutUse) for use in history.values())
    ):
        raise ValueError("history must map time frames to CutUse")
    if scrambler is not None:
        if (
            not isinstance(scrambler, ScramblerPlan)
            or not isinstance(scrambler.page_id, str)
            or not scrambler.page_id
            or not isinstance(scrambler.spans, tuple)
            or len(scrambler.spans) != quantity
            or any(
                isinstance(span, bool) or not isinstance(span, int) or span <= 0
                for span in scrambler.spans
            )
        ):
            raise ValueError("scrambler plan must carry one positive span per cut")
        return _plan_source_cuts_scrambled(
            recipe, quantity, served_slots, exclusions,
            seed=seed, history=history, scrambler=scrambler,
        )
    return _plan_source_cuts_base(
        recipe, quantity, served_slots, exclusions,
        seed=seed, history=history,
    )


def _plan_source_cuts_base(
    recipe: SourceRecipe,
    quantity: int,
    served_slots: set[str],
    exclusions: list[tuple[str, str | None, int, int]] | None,
    *,
    seed: str,
    history: dict[str, CutUse] | None,
) -> list[SourceCut]:
    """The base planner: exactly the pre-Scrambler algorithm, untouched.

    The Scrambler is OFF for every page that reaches here, so this body stays
    byte-identical to main (pick order and rng consumption included). Any
    most-different/scrambler behaviour lives only in
    _plan_source_cuts_scrambled.
    """
    used = _used_time_frames(recipe, served_slots)
    durations = source_cut_durations(recipe)
    if not durations:
        return []
    starting_length = _starting_length(recipe, durations)
    lanes = [
        _Lane(recipe, master, used[master.sha256], history or {}, exclusions, durations)
        for master in recipe.masters
    ]
    rng = random.Random(hashlib.sha256(
        f"{seed}\0{recipe.source_library_hash}".encode("utf-8"),
    ).digest())
    Row = tuple[float, tuple[float, int], float, int, int, int]
    # PRIMARY FIRST (Eric 2026-10-22; source_dna_registry.MASTER_ROLES): the
    # page's primary masters are cut as long as they hold a fresh window. A
    # backup is cut only when they do not, or as a variety cut: at most one in
    # BACKUP_VARIETY_EVERY, drawn at BACKUP_VARIETY_SHARE, never while one of
    # the page's last BACKUP_VARIETY_EVERY - 1 cuts was already a backup. A
    # library without backups plans exactly as before.
    pools = {
        "primary": tuple(index for index, lane in enumerate(lanes) if lane.master.role != "backup"),
        "backup": tuple(index for index, lane in enumerate(lanes) if lane.master.role == "backup"),
    }
    recent_roles = _recent_cut_roles(recipe, history or {}) if pools["backup"] else []
    variety_rng = random.Random(hashlib.sha256(f"{seed}\0backup-variety".encode("utf-8")).digest())
    pool = pools["primary"]
    scanned: dict[tuple[int, tuple[int, ...]], list[Row]] = {}
    sample_indexes: dict[tuple[int, tuple[int, ...]], tuple[list[tuple[int, int, int, int, int]], list[int]]] = {}

    def candidates(level: int):
        for index in pool:
            lane = lanes[index]
            for duration_ms, runs in lane.levels[level]:
                for run_first, step, count in runs:
                    for start_ms in range(run_first, run_first + step * count, step):
                        yield index, start_ms, duration_ms

    def scan(level: int) -> list[Row]:
        rows = []
        for index, start_ms, duration_ms in candidates(level):
            lane = lanes[index]
            if lane.is_taken(start_ms, duration_ms):
                continue
            static, age = lane.static_score(start_ms, duration_ms)
            rows.append((static, age, rng.random(), index, start_ms, duration_ms))
        rows.sort()
        return rows

    def scored(index: int, start_ms: int, duration_ms: int, tiebreak: float) -> Row:
        lane = lanes[index]
        static, age = lane.static_score(start_ms, duration_ms)
        score = static + lane.variety_penalty(start_ms, duration_ms, starting_length)
        return (score, age, tiebreak, index, start_ms, duration_ms)

    def usable(index: int, start_ms: int, duration_ms: int) -> bool:
        lane = lanes[index]
        return not lane.is_taken(start_ms, duration_ms) and not lane.overlaps_chosen(start_ms, duration_ms)

    def best_scanned(rows: list[Row]) -> Row | None:
        # Rows are sorted by their static score, and the variety penalty is
        # never negative: once a row cannot beat the best, no later row can.
        best = None
        for static, age, tiebreak, index, start_ms, duration_ms in rows:
            if best is not None and (static, age, tiebreak) >= best[:3]:
                break
            if not usable(index, start_ms, duration_ms):
                continue
            row = scored(index, start_ms, duration_ms, tiebreak)
            if best is None or row < best:
                best = row
        return best

    def best_sampled(level: int, total: int) -> Row | None:
        if (level, pool) not in sample_indexes:
            index_rows, offset = [], 0
            for index in pool:
                lane = lanes[index]
                for duration_ms, runs in lane.levels[level]:
                    for run_first, step, count in runs:
                        index_rows.append((offset, index, duration_ms, run_first, step))
                        offset += count
            sample_indexes[(level, pool)] = (index_rows, [row[0] for row in index_rows])
        index_rows, offsets = sample_indexes[(level, pool)]
        best = None
        for attempt in range(SAMPLE_ROUNDS * SAMPLED_CANDIDATES_PER_CUT):
            if best is not None and attempt % SAMPLED_CANDIDATES_PER_CUT == 0:
                break
            position = rng.randrange(total)
            run_offset, index, duration_ms, run_first, step = index_rows[
                bisect_left(offsets, position + 1) - 1
            ]
            start_ms = run_first + (position - run_offset) * step
            if usable(index, start_ms, duration_ms):
                row = scored(index, start_ms, duration_ms, rng.random())
                if best is None or row < best:
                    best = row
        if best is None:
            # Never-cut windows are sparse: find them exactly (no sort), and
            # score the first few so the pick still varies.
            found = []
            for index, start_ms, duration_ms in candidates(level):
                if usable(index, start_ms, duration_ms):
                    found.append(scored(index, start_ms, duration_ms, rng.random()))
                    if len(found) >= SAMPLED_CANDIDATES_PER_CUT:
                        break
            best = min(found, default=None)
        return best

    def best_new(level: int) -> Row | None:
        total = sum(lanes[index].counts[level] for index in pool)
        if not total:
            return None
        if total > FULL_SCAN_CANDIDATES:
            return best_sampled(level, total)
        if (level, pool) not in scanned:
            scanned[(level, pool)] = scan(level)
        return best_scanned(scanned[(level, pool)])

    def has_uncut(level: int) -> bool:
        return any(
            not lanes[index].is_taken(start_ms, duration_ms)
            for index, start_ms, duration_ms in candidates(level)
        )

    def repeats_last_length(row: Row) -> bool:
        lane = lanes[row[3]]
        return bool(lane.recent) and row[5] == lane.recent[0][1]

    def pick_from_pool() -> tuple[Row | None, bool]:
        # Whole-second starts first; a finer start only once every window at
        # the coarser level is cut. A coarser window that is uncut but does
        # not fit beside this plan's other cuts ends the plan; the next plan
        # cuts it. The one exception: a window repeating the master's last
        # length is taken only when no finer level offers another length,
        # and only as a plan's first cut.
        # The flag says the pool still holds a fresh window that only this
        # plan's own cuts block: the pool is not out of footage.
        pick = fallback = None
        blocked = False
        for level in range(len(START_OFFSET_LEVELS_MS)):
            candidate = best_new(level)
            if candidate is not None and not repeats_last_length(candidate):
                pick = candidate
                break
            fallback = fallback or candidate
            if candidate is None and has_uncut(level):
                blocked = True
                break
        return pick or (fallback if not chosen else None), blocked

    chosen: list[SourceCut] = []
    for _ in range(quantity):
        order = ("primary", "backup")
        if (pools["backup"] and "backup" not in recent_roles[-(BACKUP_VARIETY_EVERY - 1):]
                and variety_rng.random() < BACKUP_VARIETY_SHARE):
            order = ("backup", "primary")
        pick = None
        for name in order:
            if not pools[name]:
                continue
            pool = pools[name]
            pick, blocked = pick_from_pool()
            # A primary that still has fresh footage is never passed over
            # for a backup: the plan ends and the next plan cuts it.
            if pick is not None or (name == "primary" and blocked):
                break
        if pick is None:
            break
        *_, index, start_ms, duration_ms = pick
        lane = lanes[index]
        lane.record(start_ms, duration_ms)
        chosen.append(SourceCut(lane.master, start_ms, duration_ms, True))
        recent_roles.append(lane.master.role)
    return chosen


def _retired_master_shas() -> frozenset[str]:
    """Master shas retired network-wide, when the registry carries the list.

    source_dna_registry.RETIRED_MASTER_SHAS lands with the retire PR; the
    Scrambler refuses a retired master the moment the constant exists, and
    never invents its own retirement list.
    """
    try:
        from services import source_dna_registry
    except ImportError:
        return frozenset()
    return frozenset(getattr(source_dna_registry, "RETIRED_MASTER_SHAS", ()) or ())


def _plan_source_cuts_scrambled(
    recipe: SourceRecipe,
    quantity: int,
    served_slots: set[str],
    exclusions: list[tuple[str, str | None, int, int]] | None,
    *,
    seed: str,
    history: dict[str, CutUse] | None,
    scrambler: ScramblerPlan,
) -> list[SourceCut]:
    """The Scrambler planner: one already-reserved span per cut, most-different.

    Each cut i is planned at exactly ``scrambler.spans[i]`` ms of source
    window (offset + length x effective speed, reserved BEFORE this call), so
    the reservation bound is truthful to the render and to originality. The
    pick applies the most-different rules (hard 120 s while possible,
    deterministic origin switch, full-window dHash maximin) and never offers a
    window whose origin seconds break the real-ledger originality limits when
    that ledger is configured. Base behaviour (flag OFF) never runs this path.
    """
    retired = _retired_master_shas()
    for master in recipe.masters:
        if master.sha256 in retired:
            raise ValueError("scrambler_retired_master_refused")
    used = _used_time_frames(recipe, served_slots)
    vocabulary = source_cut_durations(recipe)
    if not vocabulary:
        return []
    # Lane levels are precomputed for the spans this plan actually cuts.
    span_set = tuple(sorted(set(scrambler.spans)))
    lanes = [
        _Lane(recipe, master, used[master.sha256], history or {}, exclusions, span_set)
        for master in recipe.masters
    ]
    far_starts = {
        index: tuple(start for start, _duration in lane.recent[:MOST_DIFFERENT_RECENT_STARTS])
        for index, lane in enumerate(lanes)
    }
    rng = random.Random(hashlib.sha256(
        f"{seed}\0{recipe.source_library_hash}\0scrambler".encode("utf-8"),
    ).digest())
    # Row: (fresh, unscored, primary, age, jitter, master index, start, span).
    # Ordering contract: FRESH BEFORE USED is the only static override of the
    # visual score; within each freshness class the full-window fingerprint
    # maximin is PRIMARY (largest min distance first) - timestamps and static
    # tuples never demote a more-different eligible window. Windows without a
    # computable full-coverage distance sort after scored ones, on the named
    # static+variety spacing fallback.
    Row = tuple[int, int, float, tuple[float, int], float, int, int, int]
    pools = {
        "primary": tuple(index for index, lane in enumerate(lanes) if lane.master.role != "backup"),
        "backup": tuple(index for index, lane in enumerate(lanes) if lane.master.role == "backup"),
    }
    recent_roles = _recent_cut_roles(recipe, history or {}) if pools["backup"] else []
    fps = load_masters_dhash()
    recent_page_cuts = _recent_page_cuts(recipe, history or {})
    originality = scrambler.originality
    starting_length = _starting_length(recipe, vocabulary)
    scanned: dict[tuple[int, tuple[int, ...], bool], list[Row]] = {}
    # This call's own picks: the 120 s rule and the fingerprint reference must
    # evolve WITH the plan, not stay frozen at its first pick.
    chosen_starts: dict[int, list[int]] = {}

    canonical_recent: dict[tuple[str, str], list[int]] = {}
    # Durable starts carry the ORIGINAL SHA and actual rendered offset. Their
    # coordinates survive removal of that SHA from the current recipe. Merge
    # legacy/current-library history by event time, suppressing its duplicate
    # reservation-frame entry for every represented durable operation.
    prior = scrambler.prior_canonical_starts
    represented = {(item.master_sha256, item.library_start_ms) for item in prior}
    entries = [(item.at, item.order, item.origin_kind, item.origin_id, item.start_ms)
               for item in prior]
    master_indices = {lane.master.sha256: i for i, lane in enumerate(lanes)}
    for slot, use in ((history or {}).items() if originality is not None else ()):
        parts = slot.split(":")
        if len(parts) < 2 or parts[0] not in master_indices or not parts[1].isdigit():
            continue
        if (parts[0], int(parts[1])) in represented:
            continue
        master = lanes[master_indices[parts[0]]].master
        kind, oid, absolute, _end = originality.canonical_origin_window(
            master.sha256, int(parts[1]), 1)
        entries.append((use.at, use.order, kind, oid, absolute))
    for _at, _order, kind, oid, absolute in sorted(entries, reverse=True):
        recent = canonical_recent.setdefault((kind, oid), [])
        if len(recent) < MOST_DIFFERENT_RECENT_STARTS:
            recent.append(absolute)

    def near_recent(index: int, start_ms: int) -> bool:
        if originality is None:
            recent = tuple(reversed(chosen_starts.get(index, ()))) + far_starts[index]
            return any(abs(start_ms - prior) < MOST_DIFFERENT_NEAR_MS
                       for prior in recent[:MOST_DIFFERENT_RECENT_STARTS])
        window = _canonical(index, start_ms, 1)
        if window is None:
            return True
        kind, oid, absolute, _end = window
        return any(abs(absolute - prior) < MOST_DIFFERENT_NEAR_MS
                   for prior in canonical_recent.get((kind, oid), ())[
                       :MOST_DIFFERENT_RECENT_STARTS])

    def _canonical(index: int, start_ms: int, duration_ms: int):
        """Canonical upstream window of a candidate, or None when unusable.

        KG-A: the candidate is judged in CANONICAL upstream coordinates
        (OriginalityIndex.canonical_origin_window), never through the
        library descriptor's public source offset. Two masters of the same
        upstream video map to the same origin id at DIFFERENT local
        offsets, so only canonical absolute milliseconds make cross-SHA
        overlaps comparable. The descriptor is never mutated to match.
        """
        lane = lanes[index]
        try:
            return originality.canonical_origin_window(
                lane.master.sha256, start_ms, duration_ms)
        except ScramblerOriginalityUnavailable as error:
            if str(error).startswith("candidate_window_invalid"):
                # This one window leaves the mapped upstream extent: it is
                # simply not a usable candidate, like any other refusal.
                return None
            # Unknown/fractional mapping is systematic for the whole master:
            # fail the plan closed (the enqueue wrapper releases the
            # reservations under its source_plan_failed reason).
            raise

    def originality_refused(index: int, start_ms: int, duration_ms: int) -> bool:
        if originality is None:
            return False
        window = _canonical(index, start_ms, duration_ms)
        if window is None:
            return True
        _kind, _oid, origin_start, origin_end = window
        allowed, _reason = originality.window_allowed(
            scrambler.page_id, lanes[index].master.sha256,
            origin_start, origin_end,
            page_handles=scrambler.page_handles,
        )
        return not allowed

    # Canonical windows already held by THIS plan's picks: one job never
    # reserves overlapping canonical origin windows, even when the two
    # copies enter as different master SHAs (library-local disjointness
    # proves nothing about the upstream video).
    chosen_canonical: list[tuple[str, str, int, int]] = list(scrambler.pending_windows)

    def canonical_overlap(index: int, start_ms: int, duration_ms: int) -> bool:
        if originality is None or not chosen_canonical:
            return False
        window = _canonical(index, start_ms, duration_ms)
        if window is None:
            return True
        kind, oid, start, end = window
        return any(
            kind == other_kind and oid == other_oid
            and other_start // 1000 <= (end - 1) // 1000
            and start // 1000 <= (other_end - 1) // 1000
            for other_kind, other_oid, other_start, other_end in chosen_canonical
        )

    def _window_fp_distance(index: int, start_ms: int, duration_ms: int) -> int | None:
        """Min Hamming distance between the FULL candidate window and the
        page's last few cuts' windows (normal+mirrored pairings).

        ``None`` when no full-coverage fingerprints are available; the named
        spacing fallback then decides. Every second of the candidate window
        (master-local, the origin offset never applied here) is compared with
        every second of each recent cut's window, so a transient near-duplicate
        anywhere inside the window loses.
        """
        if not fps:
            return None
        lane = lanes[index]
        candidate_seconds = _window_fingerprint_seconds(
            fps.get(lane.master.sha256), lane.master, start_ms, duration_ms)
        if not candidate_seconds:
            return None
        best: int | None = None
        for other_master, other_start, other_duration in recent_page_cuts:
            other_seconds = _window_fingerprint_seconds(
                fps.get(other_master.sha256), other_master, other_start, other_duration)
            if not other_seconds:
                continue
            distance = min(
                _fingerprint_pair_distance(a, b)
                for a in candidate_seconds for b in other_seconds
            )
            if best is None or distance < best:
                best = distance
        return best

    def _row_for(index: int, start_ms: int, duration_ms: int) -> Row:
        lane = lanes[index]
        static, age = lane.static_score(start_ms, duration_ms)
        fresh = 0 if static <= 0.0 else 1
        distance = _window_fp_distance(index, start_ms, duration_ms)
        if distance is not None:
            return (fresh, 0, -float(distance) - rng.random() * 0.5, age, 0.0,
                    index, start_ms, duration_ms)
        # Named spacing fallback: no full-coverage fingerprints for this
        # window, so static + variety ordering decides among the unscored.
        fallback = static + lane.variety_penalty(start_ms, duration_ms, starting_length)
        return (fresh, 1, fallback, age, rng.random(), index, start_ms, duration_ms)

    def usable(index: int, start_ms: int, duration_ms: int) -> bool:
        lane = lanes[index]
        return (
            not lane.is_taken(start_ms, duration_ms)
            and not lane.overlaps_chosen(start_ms, duration_ms)
            and not canonical_overlap(index, start_ms, duration_ms)
        )

    def span_candidates(span_ms: int, pool: tuple[int, ...]):
        """Whole-second starts (plus one ending on the last frame) at this span."""
        for index in pool:
            lane = lanes[index]
            for duration_ms, runs in lane.levels[0]:
                if duration_ms != span_ms:
                    continue
                for run_first, step, count in runs:
                    for start_ms in range(run_first, run_first + step * count, step):
                        yield index, start_ms, duration_ms

    def scan_span(span_ms: int, pool: tuple[int, ...], require_far: bool) -> list[Row]:
        rows = []
        for index, start_ms, duration_ms in span_candidates(span_ms, pool):
            lane = lanes[index]
            if lane.is_taken(start_ms, duration_ms):
                continue
            if require_far and near_recent(index, start_ms):
                continue
            if originality_refused(index, start_ms, duration_ms):
                continue
            rows.append(_row_for(index, start_ms, duration_ms))
        rows.sort()
        return rows

    def pick_span(span_ms: int, order: tuple[str, ...]) -> Row | None:
        # The FAR pass runs across EVERY allowed pool before the 120 s rule
        # relaxes anywhere: a far window on the backup master beats relaxing
        # on the primary, inside the 1-in-5 backup cap's ordering.
        for require_far in (True, False):
            for name in order:
                pool = pools[name]
                if not pool:
                    continue
                key = (span_ms, pool, require_far)
                if key not in scanned:
                    scanned[key] = scan_span(span_ms, pool, require_far)
                for row in scanned[key]:
                    if usable(row[5], row[6], row[7]):
                        if not require_far:
                            # The hard 120 s rule relaxed: only when no far
                            # window exists for this span in any allowed pool.
                            # Named, never silent.
                            _dhash_log_once(
                                f"far-relaxed:{scrambler.page_id}",
                                "scrambler planner: no window %d ms away from the "
                                "last 3 starts for page %s; relaxing the 120 s rule" % (
                                    MOST_DIFFERENT_NEAR_MS, scrambler.page_id),
                            )
                        return row
        return None

    chosen: list[SourceCut] = []
    for cut_index, span_ms in enumerate(scrambler.spans):
        last_role = recent_roles[-1] if recent_roles else None
        order = ("primary", "backup")
        if (pools["backup"] and "backup" not in recent_roles[-(BACKUP_VARIETY_EVERY - 1):]
                and last_role == "primary"):
            order = ("backup", "primary")
        pick = pick_span(span_ms, order)
        if pick is None:
            break
        *_, index, start_ms, duration_ms = pick
        lane = lanes[index]
        lane.record(start_ms, duration_ms)
        chosen.append(SourceCut(lane.master, start_ms, duration_ms, True))
        if originality is not None:
            window = _canonical(index, start_ms, duration_ms)
            if window is not None:
                chosen_canonical.append(window)
                key = window[:2]
                canonical_recent.setdefault(key, []).insert(0, window[2])
                del canonical_recent[key][MOST_DIFFERENT_RECENT_STARTS:]
        recent_roles.append(lane.master.role)
        chosen_starts.setdefault(index, []).append(start_ms)
        recent_page_cuts.insert(0, (lane.master, start_ms, duration_ms))
        del recent_page_cuts[MOST_DIFFERENT_RECENT_CUTS:]
        # Every later cut of this plan rescans: the 120 s rule now includes
        # this pick, the fingerprint reference includes its window, and its
        # footage is taken - a cached row from before this pick could score a
        # near-duplicate or near-start window as if it were still legal.
        scanned.clear()
    return chosen


def _window_fingerprint_seconds(
    fp: dict[str, tuple[int, ...]] | None, master: MasterSource,
    start_ms: int, duration_ms: int,
) -> list[tuple[int, int]]:
    """The (normal, mirrored) dHashes of every MASTER-LOCAL second a window covers.

    The producer bundle (originality.masters-dhash.v1, the originality lead's
    routine of 10-29) keys each entry by the MASTER FILE's sha256 and indexes
    ``per_second`` on the master file's own timeline (``hashes[0,2,4..]`` at
    t = s.0 of the master) - so a window at library ``start_ms`` reads
    ``start_ms // 1000`` and the master's absolute ORIGIN offset
    (``source_offset_ms``) is NEVER applied here: it is applied exactly once,
    in the originality accounting (originStartMs / window_allowed), never in
    the fingerprint indexing.
    FULL COVERAGE ONLY: when any second the window needs is missing from the
    arrays, the window returns NO seconds at all - a partly covered 11.7 s
    fast window must never claim a full-window distance scored on its prefix.
    """
    if not fp:
        return []
    per, mir = fp.get("per_second"), fp.get("mirrored_per_second")
    if not per or not mir:
        return []
    first = start_ms // 1_000
    last = (start_ms + max(0, duration_ms - 1)) // 1_000
    if not 0 <= first <= last:
        return []
    if last >= len(per) or last >= len(mir):
        return []
    return [(per[index], mir[index]) for index in range(first, last + 1)]


BACKUP_VARIETY_SHARE = 0.2
BACKUP_VARIETY_EVERY = 5


def _recent_cut_roles(recipe: SourceRecipe, history: dict[str, CutUse]) -> list[str]:
    """The pool role of the page's cuts in history, oldest first."""
    roles = {master.sha256: master.role for master in recipe.masters}
    ordered = sorted(history.items(), key=lambda item: (item[1].at, item[1].order))
    return [roles.get(key.split(":", 1)[0], "primary") for key, _ in ordered]


def explain_empty_source_plan(
    recipe: SourceRecipe,
    served_slots: set[str],
    exclusions: list[tuple[str, str | None, int, int]] | None = None,
) -> str:
    """Name why plan_source_cuts returned nothing.

    SOURCE_MASTER_TOO_SHORT: no master fits even the shortest allowed cut
    after the page's start floor (or the library has no master).
    SOURCE_WINDOWS_EXHAUSTED: every start (whole, half and quarter second) x
    length window of every master is already cut; the page needs new footage.
    SOURCE_WINDOWS_RESERVED_ELSEWHERE: never-cut windows remain, but other
    pages' reservations cover all of them.
    """
    durations = source_cut_durations(recipe)
    fits = any(
        _start_runs(master, _first_start_ms(recipe, master), duration_ms, [])
        for master in recipe.masters
        for duration_ms in durations
    )
    if not fits:
        return SOURCE_MASTER_TOO_SHORT
    if not plan_source_cuts(recipe, 1, served_slots):
        return SOURCE_WINDOWS_EXHAUSTED
    return SOURCE_WINDOWS_RESERVED_ELSEWHERE


def source_cut_is_planned(
    recipe: SourceRecipe, master: MasterSource, start_ms: object,
    duration_ms: object, slot_id: object,
    scrambler: dict | None = None,
) -> bool:
    """True only for a cut plan_source_cuts can emit for this master.

    The executor re-derives every queued cut through this one function before
    rendering, so a stored job can never carry a window the planner would not.
    A cut carrying a Scrambler reservation record validates against its drawn
    span instead of the recipe's length vocabulary (the scrambled planner
    reserves ``offset + length x effective speed``), under the same
    whole-second-start and origin-floor rules.
    """
    if scrambler is not None:
        return _scrambler_cut_is_planned(scrambler, recipe, master, start_ms, duration_ms)
    if (
        not isinstance(start_ms, int) or isinstance(start_ms, bool)
        or not isinstance(duration_ms, int) or isinstance(duration_ms, bool)
        or start_ms < 0 or start_ms + duration_ms > master.duration_ms
        or master.source_offset_ms + start_ms < minimum_original_start_ms(recipe, master)
    ):
        return False
    if slot_id == f"{master.sha256}:{start_ms}:{duration_ms}":
        # A time frame: an allowed length at a whole-second start or one of
        # the planner's frame offsets inside a second, or ending on the
        # master's last frame. Lengths planned before 2026-09-30 (every
        # legacy 6 s re-cut included) still verify for jobs queued then.
        legacy_durations = (
            () if recipe.format_slug == "pov-club"
            else _legacy_source_cut_durations(recipe)
        )
        return (
            duration_ms in source_cut_durations(recipe)
            or duration_ms in legacy_durations
        ) and (
            start_ms % CUT_START_STEP_MS in SUB_SECOND_START_OFFSETS_MS
            or start_ms == master.duration_ms - duration_ms
        )
    # Legacy grid position ids from jobs queued before time-frame planning.
    # POV Club was commissioned after that vocabulary was retired, so it has
    # no legitimate queued job with a legacy grid id.
    if recipe.format_slug == "pov-club":
        return False
    if slot_id != f"{master.sha256}:{start_ms}":
        return False
    try:
        return duration_ms == planned_source_cut_duration(recipe, master, start_ms)
    except ValueError:
        return False


def _scrambler_cut_is_planned(
    scrambler: object, recipe: SourceRecipe, master: MasterSource,
    start_ms: object, duration_ms: object,
) -> bool:
    """Validate one stored Scrambler cut record against its reserved span.

    The record is Lab-private job-store data, but a stored job could hold
    tampered or corrupt bytes, so every field is re-derived exactly: schema,
    speed/length/offset/treatment vocabulary membership, the window and span
    arithmetic (``window = length x speed``, ``span = offset + window``), the
    master bounds, the origin floor, and the whole-second start the scrambled
    planner reserves (or a start ending on the master's last frame).
    """
    from services import scrambler as scrambler_rules

    if not isinstance(scrambler, dict):
        return False
    if scrambler.get("schema") != scrambler_rules.SCRAMBLER_CUT_SCHEMA:
        return False
    speed = scrambler.get("speed")
    length_ms = scrambler.get("lengthMs")
    offset_ms = scrambler.get("offsetMs")
    treatment = scrambler.get("treatment")
    mirror = scrambler.get("mirror")
    if (
        isinstance(speed, bool) or not isinstance(speed, (int, float))
        # EXACT vocabulary membership: a corrupt 1.304 must not round into the
        # 1.3 bucket here and then render above Eric's 1.3 cap. The legal
        # speeds are exact literals, so float equality is intentional.
        or float(speed) not in scrambler_rules.SPEEDS
        or isinstance(length_ms, bool) or length_ms not in scrambler_rules.LENGTHS
        or isinstance(offset_ms, bool) or offset_ms not in scrambler_rules.OFFSETS
        or treatment not in scrambler_rules.TREATMENTS
        or not isinstance(mirror, bool)
    ):
        return False
    draw = scrambler_rules.ScramblerDraw(
        treatment=treatment, speed=float(speed), length_ms=length_ms,
        offset_ms=offset_ms, mirror=mirror,
    )
    if scrambler.get("windowMs") != draw.window_ms():
        return False
    if scrambler.get("spanMs") != draw.span_ms():
        return False
    if (
        not isinstance(start_ms, int) or isinstance(start_ms, bool)
        or not isinstance(duration_ms, int) or isinstance(duration_ms, bool)
        or start_ms < 0 or duration_ms != draw.span_ms()
        or start_ms + duration_ms > master.duration_ms
        or master.source_offset_ms + start_ms < minimum_original_start_ms(recipe, master)
    ):
        return False
    # The scrambled planner reserves whole-second starts (level-0 runs) at the
    # drawn span, or a start ending on the master's last frame.
    return start_ms % CUT_START_STEP_MS == 0 or start_ms == master.duration_ms - duration_ms


def minimum_original_start_ms(recipe: SourceRecipe, master: MasterSource) -> int:
    """Return the earliest allowed timestamp on the upstream source timeline."""
    authority = master.provenance.get("authority")
    # ShipStream page manifests describe immutable bytes already extracted for
    # this exact page. They can begin at their recorded upstream origin. The
    # explicit historical recovery form has no trustworthy upstream offset and
    # therefore remains locally anchored at zero. Raw source libraries retain
    # the default one-minute lead-in skip.
    default_minimum_start_ms = (
        0
        if authority == SHIPSTREAM_PAGE_MASTER_AUTHORITY
        or (
            isinstance(authority, str)
            and authority.startswith(SHIPSTREAM_HISTORICAL_AUTHORITY_PREFIX)
        )
        else MIN_ORIGINAL_START_MS
    )
    return max(default_minimum_start_ms, recipe.minimum_source_start_ms)


def delivery_cut_duration(recipe: SourceRecipe, duration_ms: int) -> int:
    """Legacy lengths only: whole source seconds delivering 6-11 s at speed.

    New plans use source_cut_durations; this keeps verifying jobs queued
    under the earlier vocabulary and legacy grid ids.
    """
    speed = dossier_clip_speed(recipe)
    minimum = math.ceil(6 * speed) * 1_000
    maximum = math.floor(11 * speed) * 1_000
    return min(max(duration_ms, minimum), maximum)


def planned_source_cut_duration(
    recipe: SourceRecipe, master: MasterSource, start_ms: int,
) -> int:
    """Return the hash-bound duration of one legacy 9-second-grid position.

    Used only to verify jobs queued before time-frame planning and to resolve a
    legacy ``sha:start`` id to the exact time frame it reserved.
    """
    if (
        not isinstance(start_ms, int)
        or isinstance(start_ms, bool)
        or start_ms < 0
        or start_ms % CUT_SLOT_STEP_MS not in RECUT_PHASES_MS
    ):
        raise ValueError("source start must be a non-negative slot boundary")
    phase = RECUT_PHASES_MS.index(start_ms % CUT_SLOT_STEP_MS)
    duration_values = list(range(
        max(5_000, recipe.cut_duration_ms - 2_000),
        min(9_000, recipe.cut_duration_ms + 2_000) + 1,
        1_000,
    ))
    duration_values = list(dict.fromkeys(delivery_cut_duration(recipe, ms) for ms in duration_values))
    # A curated page master may itself be a finished short-form clip rather
    # than a long source recording. Keep the same deterministic duration
    # vocabulary, but choose only values that fit the immutable bytes instead
    # of rotating onto an 8- or 9-second cut and declaring a 7.5-second master
    # to have no capacity at all.
    fitting_values = [
        duration for duration in duration_values
        if start_ms + duration <= master.duration_ms
    ]
    if fitting_values:
        duration_values = fitting_values
    rotation = int(hashlib.sha256(
        f"{recipe.source_library_hash}\0{master.sha256}".encode("utf-8"),
    ).hexdigest()[:8], 16) % len(duration_values)
    return duration_values[
        (rotation + (start_ms // CUT_SLOT_STEP_MS) + phase) % len(duration_values)
    ]
