"""Page-scoped immutable-master execution for sourced-video dossiers.

Already-cut outputs are historical evidence, never refillable source DNA. A
sourced recipe becomes executable only when its v3 production selection names
one registered master library bound to the exact page and format. The executor
plans unique cut slots deterministically and records the original-source offset
beside every output.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from services.content_engine_registry import resolve_material_profile
from services.content_format_contracts import load_format_contracts
from services.control_plane_generation import (
    load_prompt_catalog,
    render_treatment_capability,
    typed_recipe_spec,
)
from services.dossier_catalog_version import dossier_catalog_version
from services.source_dna_registry import (
    MasterSource,
    SourceDnaError,
    load_source_dna_library,
    source_dna_catalog_hash,
)


EXECUTOR_PATH = (
    Path(__file__).resolve().parents[1]
    / "recipes/executors/source-dna-recut.v1.json"
)
EXECUTOR_SCHEMA = "content-lab.source-dna-recut-executor.v1"
CUT_SLOT_STEP_MS = 8_500


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
    material_source: str
    asset_type: str
    recipe_spec: dict[str, Any]

    @property
    def served_ledger_key(self) -> str:
        return f"source-dna:{self.source_library_id}:{self.source_library_hash}"


@dataclass(frozen=True)
class SourceCut:
    master: MasterSource
    start_ms: int
    duration_ms: int

    @property
    def slot_id(self) -> str:
        # Deliberately excludes duration/speed/crop. Once a source position is
        # used, a cosmetically altered near-duplicate may not re-enter a job.
        return f"{self.master.sha256}:{self.start_ms}"


def _executor_contract() -> tuple[dict[str, Any], str]:
    raw = EXECUTOR_PATH.read_bytes()
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value) != {
            "schema", "executorId", "source", "selection", "controls",
        }
        or value.get("schema") != EXECUTOR_SCHEMA
        or value.get("executorId") != "source-dna-recut"
        or value.get("source") != "page_scoped_immutable_master"
        or value.get("selection") != "deterministic_without_replacement"
    ):
        raise ValueError("source DNA recut executor contract is invalid")
    controls = value.get("controls")
    cut = controls.get("cutDurationMs") if isinstance(controls, dict) else None
    if (
        not isinstance(cut, dict)
        or set(cut) != {"type", "min", "max", "step", "default"}
        or cut.get("type") != "range"
        or (cut.get("min"), cut.get("max"), cut.get("step"), cut.get("default"))
            != (6_000, 8_000, 1_000, 7_000)
    ):
        raise ValueError("source DNA recut controls are invalid")
    return value, hashlib.sha256(raw).hexdigest()


def source_treatment_capability() -> dict[str, Any]:
    executor, _ = _executor_contract()
    return {
        "scope": "master_source_window",
        "recutWindow": "deterministic_without_replacement",
        "controls": dict(executor["controls"]),
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
    if spec is None or spec.get("schema") != "dossier.recipe-spec.v3":
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
    try:
        library = load_source_dna_library(source_library_id)
        contracts, contracts_hash = load_format_contracts()
        _, prompt_hash = load_prompt_catalog()
    except (OSError, ValueError, json.JSONDecodeError, SourceDnaError):
        return None
    master_pages = spec.get("masterPages")
    expected_catalog_version = dossier_catalog_version(
        contracts_hash,
        profile.registry_hash,
        prompt_hash,
        source_dna_catalog_hash(),
    )
    if (
        library.format_slug != profile.format_slug
        or not isinstance(master_pages, dict)
        or library.page_id != master_pages.get("pageId")
        or production.get("catalogVersion") != expected_catalog_version
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
        material_source=profile.material_source,
        asset_type=profile.asset_type,
        recipe_spec=spec,
    )


def plan_source_cuts(
    recipe: SourceRecipe,
    quantity: int,
    served_slots: set[str],
) -> list[SourceCut]:
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        raise ValueError("quantity must be a positive integer")
    if not isinstance(served_slots, set) or any(
        not isinstance(value, str) for value in served_slots
    ):
        raise ValueError("served_slots must be a string set")
    candidates: list[SourceCut] = []
    for master in recipe.masters:
        max_start = master.duration_ms - recipe.cut_duration_ms
        for start_ms in range(0, max_start + 1, CUT_SLOT_STEP_MS):
            cut = SourceCut(master, start_ms, recipe.cut_duration_ms)
            if cut.slot_id not in served_slots:
                candidates.append(cut)
            if len(candidates) >= quantity:
                return candidates
    return candidates
