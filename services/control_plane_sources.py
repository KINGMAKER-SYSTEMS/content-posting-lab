"""Page-scoped immutable-master execution for sourced-video dossiers.

Already-cut outputs are historical evidence, never refillable source DNA. A
sourced recipe becomes executable only when its typed production selection names
one registered master library bound to the exact page and format. The executor
plans unique cut time frames from a recorded per-job seed and records the
original-source offset beside every output.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
import hashlib
import json
import math
import random
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, quote_plus
from pathlib import Path
from typing import Any, Callable

from services.content_engine_registry import resolve_material_profile
from services.content_format_contracts import load_format_contracts
from services.control_plane_generation import (
    dossier_clip_speed,
    render_treatment_capability,
    typed_recipe_spec,
)
from services.source_dna_registry import (
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
# candidate. A time frame already cut from this master, under any recipe
# revision, is never planned again. Fresh footage is preferred: candidates that
# overlap previously cut footage least come first, and ties are broken by a
# seed recorded on the job, so each run cuts different time frames and every
# plan is reproducible from its seed.
CUT_START_STEP_MS = 1_000
CAPABILITY_PLAN_SEED = "capability"
MASTER_WINDOWS_EXHAUSTED = "master_windows_exhausted"
MIN_ORIGINAL_START_MS = 60_000
SHIPSTREAM_PAGE_MASTER_AUTHORITY = "ShipStream source-manifest.v1 exact page master"
SHIPSTREAM_HISTORICAL_AUTHORITY_PREFIX = (
    "ShipStream source-manifest.v1 page-bound historical posted cut;"
)


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
    """Allowed source lengths: the page target +/- 2 s, plus the 6 s re-cut.

    Every value is adjusted by delivery_cut_duration so the delivered clip
    stays 6-11 seconds at the saved playback speed.
    """
    values = range(
        max(5_000, recipe.cut_duration_ms - 2_000),
        min(9_000, recipe.cut_duration_ms + 2_000) + 1,
        1_000,
    )
    durations = {delivery_cut_duration(recipe, ms) for ms in values}
    durations.add(delivery_cut_duration(recipe, RECUT_FIXED_DURATION_MS))
    return tuple(sorted(durations))


def _time_frame_starts(master: MasterSource, first_ms: int, duration_ms: int) -> list[int]:
    """Whole-second starts from ``first_ms``, plus one ending on the last frame."""
    last = master.duration_ms - duration_ms
    if last < first_ms:
        return []
    starts = list(range(first_ms, last + 1, CUT_START_STEP_MS))
    if starts[-1] != last:
        starts.append(last)
    return starts


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


def _overlap_with_used(frames: list[tuple[int, int]], longest: int, start: int, end: int) -> int:
    """Largest overlap (ms) between [start, end) and any used time frame."""
    best = 0
    index = bisect_left(frames, (start - longest, -1))
    while index < len(frames) and frames[index][0] < end:
        used_start, used_duration = frames[index]
        overlap = min(end, used_start + used_duration) - max(start, used_start)
        if overlap > best:
            best = overlap
        index += 1
    return best


def plan_source_cuts(
    recipe: SourceRecipe,
    quantity: int,
    served_slots: set[str],
    exclusions: list[tuple[str, int, int]] | None = None,
    *,
    seed: str = CAPABILITY_PLAN_SEED,
) -> list[SourceCut]:
    """Plan ``quantity`` never-cut time frames, freshest footage first.

    ``served_slots`` holds every time frame already cut or reserved (slot ids
    ``sha:start:duration``; legacy ``sha:start`` grid ids are resolved to the
    length they were cut at). Those exact time frames are never returned.
    Windows overlapping another page's reservation (``exclusions``) and windows
    before the page's original-timeline floor are never candidates. One plan
    never holds two overlapping cuts of the same master. Among the remaining
    candidates, the least overlap with already-cut footage wins; ties are
    ordered by ``seed``, so the same seed and inputs always yield the same plan.
    Fewer than ``quantity`` cuts means the master's unique time frames are
    exhausted (see MASTER_WINDOWS_EXHAUSTED).
    """
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        raise ValueError("quantity must be a positive integer")
    if not isinstance(served_slots, set) or any(
        not isinstance(value, str) for value in served_slots
    ):
        raise ValueError("served_slots must be a string set")
    if not isinstance(seed, str):
        raise ValueError("seed must be a string")
    used = _used_time_frames(recipe, served_slots)
    durations = source_cut_durations(recipe)
    candidates: list[tuple[int, float, SourceCut]] = []
    rng = random.Random(hashlib.sha256(
        f"{seed}\0{recipe.source_library_hash}".encode("utf-8"),
    ).digest())
    for master in recipe.masters:
        identity = canonical_source_identity(master.provenance.get("sourceUrl"))
        reserved = sorted(
            (start, end)
            for source, master_sha256, start, end in (exclusions or [])
            if source == identity and (master_sha256 is None or master_sha256 == master.sha256)
        )
        frames = used[master.sha256]
        taken = set(frames)
        longest = max((duration for _, duration in frames), default=0)
        first = _first_start_ms(recipe, master)
        for duration_ms in durations:
            for start_ms in _time_frame_starts(master, first, duration_ms):
                if (start_ms, duration_ms) in taken:
                    continue
                original_start = master.source_offset_ms + start_ms
                original_end = original_start + duration_ms
                if any(begin < original_end and end > original_start for begin, end in reserved):
                    continue
                cut = SourceCut(master, start_ms, duration_ms, True)
                if cut.slot_id in served_slots:
                    continue
                overlap = _overlap_with_used(frames, longest, start_ms, start_ms + duration_ms)
                candidates.append((overlap, rng.random(), cut))
    candidates.sort(key=lambda row: (row[0], row[1]))
    chosen: list[SourceCut] = []
    for _, _, cut in candidates:
        if any(
            other.master.sha256 == cut.master.sha256
            and other.start_ms < cut.start_ms + cut.duration_ms
            and cut.start_ms < other.start_ms + other.duration_ms
            for other in chosen
        ):
            continue
        chosen.append(cut)
        if len(chosen) >= quantity:
            break
    return chosen


def source_cut_is_planned(
    recipe: SourceRecipe, master: MasterSource, start_ms: object,
    duration_ms: object, slot_id: object,
) -> bool:
    """True only for a cut plan_source_cuts can emit for this master.

    The executor re-derives every queued cut through this one function before
    rendering, so a stored job can never carry a window the planner would not.
    """
    if (
        not isinstance(start_ms, int) or isinstance(start_ms, bool)
        or not isinstance(duration_ms, int) or isinstance(duration_ms, bool)
        or start_ms < 0 or start_ms + duration_ms > master.duration_ms
        or master.source_offset_ms + start_ms < minimum_original_start_ms(recipe, master)
    ):
        return False
    if slot_id == f"{master.sha256}:{start_ms}:{duration_ms}":
        # A time frame: an allowed length at a whole-second start, or ending on
        # the master's last frame. This also covers every legacy 6 s re-cut.
        return duration_ms in source_cut_durations(recipe) and (
            start_ms % CUT_START_STEP_MS == 0
            or start_ms == master.duration_ms - duration_ms
        )
    # Legacy grid position ids from jobs queued before time-frame planning.
    if slot_id != f"{master.sha256}:{start_ms}":
        return False
    try:
        return duration_ms == planned_source_cut_duration(recipe, master, start_ms)
    except ValueError:
        return False


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
    """Select whole source seconds that deliver 6-11 seconds at saved speed."""
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
