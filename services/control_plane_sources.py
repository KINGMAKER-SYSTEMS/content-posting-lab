"""Page-scoped immutable-master execution for sourced-video dossiers.

Already-cut outputs are historical evidence, never refillable source DNA. A
sourced recipe becomes executable only when its typed production selection names
one registered master library bound to the exact page and format. The executor
plans unique cut slots deterministically and records the original-source offset
beside every output.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, quote_plus
from pathlib import Path
from typing import Any, Callable

from services.content_engine_registry import resolve_material_profile
from services.content_format_contracts import load_format_contracts
from services.control_plane_generation import (
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


EXECUTOR_PATH = (
    Path(__file__).resolve().parents[1]
    / "recipes/executors/source-dna-recut.v1.json"
)
EXECUTOR_SCHEMA = "content-lab.source-dna-recut-executor.v1"
CUT_SLOT_STEP_MS = 9_000
# Re-cut phases. The 0 ms grid is cut first; once a page has used every clean
# grid slot of its footage, the same footage is cut again from grid slots
# shifted by 3 s and then 6 s (operator decision 2026-09-18: re-cutting the
# same source at different points is fine). A 90-second page master yields 10
# cuts on the plain grid and ~29 across the three phases. Each start position
# is still used once per recipe version (slot_id), and cuts are never shared
# across pages.
RECUT_PHASES_MS = (0, 3_000, 6_000)
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

    @property
    def slot_id(self) -> str:
        # Deliberately excludes duration/speed/crop. Once a source position is
        # used, a cosmetically altered near-duplicate may not re-enter a job.
        # A re-cut phase is a different position (start_ms), so it has its own id.
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
        or int(value) != value
        or not control["min"] <= int(value) <= control["max"]
        or (int(value) - control["min"]) % control["step"] != 0
    ):
        return None
    return int(value)


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
    if cut_duration_ms is None:
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


def plan_source_cuts(
    recipe: SourceRecipe,
    quantity: int,
    served_slots: set[str],
    exclusions: list[tuple[str, int, int]] | None = None,
) -> list[SourceCut]:
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        raise ValueError("quantity must be a positive integer")
    if not isinstance(served_slots, set) or any(
        not isinstance(value, str) for value in served_slots
    ):
        raise ValueError("served_slots must be a string set")
    candidates: list[SourceCut] = []
    lanes = []
    for master in recipe.masters:
        identity = canonical_source_identity(master.provenance.get("sourceUrl"))
        authority = master.provenance.get("authority")
        # ShipStream page manifests describe immutable bytes that have already
        # been extracted for this exact page. Applying the original-source
        # one-minute skip to those bytes makes every short imported page master
        # advertise zero capacity. The 60-second floor remains intact for raw
        # source libraries; exact page-bound masters and the explicit historical
        # recovery form may use their first frame.
        minimum_start_ms = (
            0
            if authority == SHIPSTREAM_PAGE_MASTER_AUTHORITY
            or (
                isinstance(authority, str)
                and authority.startswith(SHIPSTREAM_HISTORICAL_AUTHORITY_PREFIX)
            )
            else MIN_ORIGINAL_START_MS
        )
        reserved = [
            (start, end)
            for source, master_sha256, start, end in (exclusions or [])
            if source == identity and (master_sha256 is None or master_sha256 == master.sha256)
        ]
        lanes.append((master, minimum_start_ms, reserved))
    # The saved cutDurationMs is the page's target. Each immutable master
    # deterministically rotates through the target +/- 2 seconds inside the
    # advertised 5-9 second executor bounds. Within one phase the 9-second slot
    # grid keeps even the longest neighboring cuts disjoint, while slot_id
    # continues to reserve the source position across treatment changes.
    # Phases run in order, so fresh footage is always cut before a re-cut, and
    # one plan never holds two overlapping cuts of the same master.
    for phase_ms in RECUT_PHASES_MS:
        for master, minimum_start_ms, reserved in lanes:
            for start_ms in range(phase_ms, master.duration_ms, CUT_SLOT_STEP_MS):
                if master.source_offset_ms + start_ms < minimum_start_ms:
                    continue
                duration_ms = planned_source_cut_duration(recipe, master, start_ms)
                if start_ms + duration_ms > master.duration_ms:
                    continue
                original_start = master.source_offset_ms + start_ms
                if any(begin < original_start + duration_ms and end > original_start for begin, end in reserved):
                    continue
                if any(
                    chosen.master.sha256 == master.sha256
                    and chosen.start_ms < start_ms + duration_ms
                    and start_ms < chosen.start_ms + chosen.duration_ms
                    for chosen in candidates
                ):
                    continue
                cut = SourceCut(master, start_ms, duration_ms)
                if cut.slot_id not in served_slots:
                    candidates.append(cut)
                if len(candidates) >= quantity:
                    return candidates
    return candidates
    return candidates


def planned_source_cut_duration(
    recipe: SourceRecipe, master: MasterSource, start_ms: int,
) -> int:
    """Return the hash-bound varied duration for one reserved source slot."""
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
