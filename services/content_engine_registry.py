"""Closed Master Pages-to-material execution registry.

The dossier format selects neither a content engine nor an arbitrary project.
It resolves only when the exact Master Pages niche and engine are commissioned
to one hash-bound executor. Known but uncommissioned formats stay visible as
intent without becoming executable capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from services.content_format_contracts import FormatContract, format_page_admits, load_format_contracts


REGISTRY_PATH = (
    Path(__file__).resolve().parents[1]
    / "recipes/content-engine-registry.v1.json"
)
REGISTRY_SCHEMA = "content-lab.content-engine-registry.v1"
PROFILE_FIELDS = {
    "contentNiche", "contentEngine", "materialSource", "assetType",
    "executionStatus", "executorKind", "executorId", "executorVersion",
    "maxQuantity", "formatContractVersion",
}
FORMAT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
SHA256_VERSION = re.compile(r"^sha256:[0-9a-f]{64}$")

# THE ONE ENGINE LIST (B9, 2026-11-22). Engines used to be a Python literal here
# and a JS literal in the Worker; a new engine needed code in two repos. Now
# recipes/content-engines.v1.json names every engine with its material source
# and executor kind, this module builds the bindings from it, and
# GET /v1/format-contracts serves the same list (plus its hash) to the Worker,
# which drops its own literal. What stays code is the executor itself: an
# engine may only bind to an executor kind the Lab has a renderer for.
ENGINES_PATH = Path(__file__).resolve().parents[1] / "recipes/content-engines.v1.json"
ENGINES_SCHEMA = "content-lab.content-engines.v1"
ENGINE_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
ENGINE_FIELDS = {"label", "materialSource", "executorKind"}
# Executor kinds with code behind them (services/control_plane_generation.py,
# control_plane_sources.py, control_plane_slideshows.py). Adding a kind IS a
# code change, by nature; adding an engine on an existing kind is not.
KNOWN_EXECUTOR_KINDS = frozenset({"prompt_family", "source_dna_recut", "slideshow_renderer"})
# Material sources each executor kind reads. A binding outside this table
# would name material the executor cannot consume, so it is refused at load.
MATERIAL_SOURCES_BY_KIND = {
    "prompt_family": frozenset({"generated_video"}),
    "source_dna_recut": frozenset({"source_library"}),
    "slideshow_renderer": frozenset({"image_library", "lyric_image_library"}),
}
# The dispatch family an engine belongs to, by what its executor consumes.
ENGINE_FAMILY_BY_KIND = {
    "prompt_family": "generated",
    "source_dna_recut": "sourced",
    "slideshow_renderer": "slideshow",
}


@dataclass(frozen=True)
class ContentEngine:
    engine: str
    label: str
    material_source: str
    executor_kind: str

    @property
    def family(self) -> str:
        return ENGINE_FAMILY_BY_KIND[self.executor_kind]


def _engines_path() -> Path:
    configured = os.environ.get("CONTENT_LAB_CONTENT_ENGINES", "").strip()
    return Path(configured).resolve() if configured else ENGINES_PATH


def canonical_engines_hash(engines: dict[str, ContentEngine]) -> str:
    """sha256 of the engine list in canonical JSON (sorted keys, no spaces).

    The Worker recomputes exactly this over the `engines` it receives and
    refuses the whole authority when it differs, so a partial or edited list
    can never be acted on. Formatting of the file on disk does not matter.
    """
    canonical = json.dumps(
        {
            engine.engine: {
                "executorKind": engine.executor_kind,
                "label": engine.label,
                "materialSource": engine.material_source,
            }
            for engine in engines.values()
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


# Material source -> dispatch family, for a profile (whose executor may be
# None while uncommissioned) and for callers that hold only the engine name.
MATERIAL_FAMILY = {
    "generated_video": "generated",
    "source_library": "sourced",
    "image_library": "slideshow",
    "lyric_image_library": "slideshow",
}

_ENGINES_CACHE: tuple[tuple[str, int, int], tuple[dict[str, ContentEngine], str]] | None = None


def load_content_engines() -> tuple[dict[str, ContentEngine], str]:
    """Every engine, by id, plus the canonical hash the Worker checks.

    Re-read when the file changes (path, mtime, size); the file is tiny and
    the dispatch helpers below ask on every request.
    """
    global _ENGINES_CACHE
    path = _engines_path()
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if _ENGINES_CACHE is not None and _ENGINES_CACHE[0] == key:
        return _ENGINES_CACHE[1]
    value = json.loads(path.read_bytes())
    if (
        not isinstance(value, dict)
        or not {"schema", "engines"} <= set(value) <= {"schema", "engines", "note"}
        or value.get("schema") != ENGINES_SCHEMA
        or not isinstance(value.get("engines"), dict)
        or not value["engines"]
    ):
        raise ValueError("content engines file is invalid")
    engines: dict[str, ContentEngine] = {}
    for engine_id, entry in value["engines"].items():
        if not isinstance(engine_id, str) or not ENGINE_ID.fullmatch(engine_id):
            raise ValueError("content engine id is invalid")
        if not isinstance(entry, dict) or set(entry) != ENGINE_FIELDS:
            raise ValueError(f"content engine {engine_id} fields are invalid")
        label = entry.get("label")
        material_source = entry.get("materialSource")
        executor_kind = entry.get("executorKind")
        if (
            not _nonempty(label, 80)
            or not _nonempty(material_source, 60)
            or executor_kind not in KNOWN_EXECUTOR_KINDS
        ):
            raise ValueError(f"content engine {engine_id} is invalid")
        if material_source not in MATERIAL_SOURCES_BY_KIND[executor_kind]:
            raise ValueError(f"content engine {engine_id} binds material its executor cannot read")
        engines[engine_id] = ContentEngine(engine_id, label, material_source, executor_kind)
    result = (engines, canonical_engines_hash(engines))
    _ENGINES_CACHE = (key, result)
    return result


def allowed_bindings() -> dict[str, tuple[str, str]]:
    """engine -> (materialSource, executorKind), from the engines file."""
    engines, _ = load_content_engines()
    return {
        engine.engine: (engine.material_source, engine.executor_kind)
        for engine in engines.values()
    }


def engine_family(engine: Any) -> str | None:
    """'generated' | 'sourced' | 'slideshow' for a known engine, else None.

    Dispatch branches ask this instead of comparing engine names, so an
    engine added to the file dispatches like its siblings with no code edit.
    An unknown engine (or an unreadable file) is None: no branch claims it.
    """
    if not isinstance(engine, str):
        return None
    try:
        engines, _ = load_content_engines()
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    known = engines.get(engine)
    return known.family if known is not None else None


def engines_in_family(family: str) -> frozenset[str]:
    """The engine ids whose executor is in one dispatch family."""
    try:
        engines, _ = load_content_engines()
    except (OSError, ValueError, json.JSONDecodeError):
        return frozenset()
    return frozenset(
        engine.engine for engine in engines.values() if engine.family == family
    )


@dataclass(frozen=True)
class MaterialProfile:
    format_slug: str
    content_niche: str
    content_engine: str
    material_source: str
    asset_type: str
    execution_status: str
    executor_kind: str | None
    executor_id: str | None
    executor_version: str | None
    max_quantity: int
    format_contract_version: str
    registry_hash: str
    authority_hash: str

    @property
    def family(self) -> str:
        """'generated' | 'sourced' | 'slideshow', by the material it consumes."""
        return MATERIAL_FAMILY[self.material_source]


def _registry_path() -> Path:
    configured = os.environ.get("CONTENT_LAB_ENGINE_REGISTRY", "").strip()
    return Path(configured).resolve() if configured else REGISTRY_PATH


def _nonempty(value: Any, maximum: int = 200) -> bool:
    return (
        isinstance(value, str)
        and value == value.strip()
        and bool(value)
        and len(value) <= maximum
    )


def _parse_profile(
    format_slug: str,
    value: Any,
    registry_hash: str,
    contract: FormatContract,
    bindings: dict[str, tuple[str, str]],
) -> MaterialProfile:
    if not FORMAT_ID.fullmatch(format_slug):
        raise ValueError("engine registry format is invalid")
    if not isinstance(value, dict) or set(value) != PROFILE_FIELDS:
        raise ValueError(f"engine registry profile {format_slug} fields are invalid")
    content_niche = value.get("contentNiche")
    content_engine = value.get("contentEngine")
    material_source = value.get("materialSource")
    asset_type = value.get("assetType")
    execution_status = value.get("executionStatus")
    executor_kind = value.get("executorKind")
    executor_id = value.get("executorId")
    executor_version = value.get("executorVersion")
    max_quantity = value.get("maxQuantity")
    format_contract_version = value.get("formatContractVersion")
    if (
        not _nonempty(content_niche)
        or content_engine not in bindings
        or not _nonempty(material_source)
        or asset_type != "video/mp4"
        or execution_status not in {"commissioned", "uncommissioned"}
        or not isinstance(format_contract_version, str)
        or not SHA256_VERSION.fullmatch(format_contract_version)
        or isinstance(max_quantity, bool)
        or not isinstance(max_quantity, int)
    ):
        raise ValueError(f"engine registry profile {format_slug} is invalid")
    expected_source, expected_kind = bindings[content_engine]
    if material_source != expected_source:
        raise ValueError(f"engine registry profile {format_slug} changes engine semantics")
    if (
        contract.format_slug != format_slug
        or contract.content_niche != content_niche
        or contract.content_engine != content_engine
        or contract.material_source != material_source
        or contract.asset_type != asset_type
        or format_contract_version != f"sha256:{contract.contract_hash}"
    ):
        raise ValueError(f"engine registry profile {format_slug} diverges from its format contract")
    if execution_status == "commissioned":
        authority = contract.creative_authority
        if (
            contract.definition_status != "complete"
            or authority is None
            or executor_kind != expected_kind
            or not isinstance(executor_id, str)
            or not SAFE_ID.fullmatch(executor_id)
            or not isinstance(executor_version, str)
            or not SHA256_VERSION.fullmatch(executor_version)
            or not 1 <= max_quantity <= 20
        ):
            raise ValueError(f"engine registry profile {format_slug} executor is invalid")
        if (
            authority.kind != executor_kind
            or authority.authority_id != executor_id
            or authority.version != executor_version
        ):
            raise ValueError(f"engine registry profile {format_slug} executor diverges from its format contract")
    elif (
        executor_kind is not None
        or executor_id is not None
        or executor_version is not None
        or max_quantity != 0
    ):
        raise ValueError(f"uncommissioned profile {format_slug} exposes an executor")
    # Registry membership is global, but an active job is authorized by the
    # selected profile. Keep admission caps out of this digest: changing a cap
    # affects new work, not work already admitted under the same executor and
    # format contract.
    authority_hash = profile_authority_hash(format_slug, value)
    return MaterialProfile(
        format_slug=format_slug,
        content_niche=content_niche,
        content_engine=content_engine,
        material_source=material_source,
        asset_type=asset_type,
        execution_status=execution_status,
        executor_kind=executor_kind,
        executor_id=executor_id,
        executor_version=executor_version,
        max_quantity=max_quantity,
        format_contract_version=format_contract_version,
        registry_hash=registry_hash,
        authority_hash=authority_hash,
    )


def profile_authority_hash(format_slug: str, value: dict[str, Any]) -> str:
    """Hash one executable profile without unrelated registry members/caps."""
    authority_fields = {
        key: value[key]
        for key in sorted(PROFILE_FIELDS - {"maxQuantity"})
    }
    authority_bytes = json.dumps(
        {"formatSlug": format_slug, **authority_fields},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(authority_bytes).hexdigest()


def job_profile_authority_matches(
    job: dict[str, Any], expected_hash: str,
    legacy_pins: dict[str, Any] | None = None,
) -> bool:
    """Accept exact new pins, or legacy jobs with explicit matching authority pins."""
    persisted = job.get("engineProfileHash")
    if persisted is not None:
        return persisted == expected_hash
    return bool(legacy_pins) and all(
        job.get(key) == expected_value
        for key, expected_value in legacy_pins.items()
    )


def load_engine_registry() -> tuple[dict[str, MaterialProfile], str]:
    raw = _registry_path().read_bytes()
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "profiles"}
        or value.get("schema") != REGISTRY_SCHEMA
        or not isinstance(value.get("profiles"), dict)
        or not value["profiles"]
    ):
        raise ValueError("content engine registry is invalid")
    registry_hash = hashlib.sha256(raw).hexdigest()
    contracts, _ = load_format_contracts()
    if set(value["profiles"]) != set(contracts):
        raise ValueError("engine registry and format contract coverage diverge")
    bindings = allowed_bindings()
    profiles = {
        format_slug: _parse_profile(
            format_slug, profile, registry_hash, contracts[format_slug], bindings,
        )
        for format_slug, profile in value["profiles"].items()
    }
    return profiles, registry_hash


def resolve_material_profile(
    publication: dict[str, Any], spec: dict[str, Any],
) -> MaterialProfile | None:
    recipe_id = publication.get("recipeId")
    if not isinstance(recipe_id, str) or not recipe_id.endswith(":master"):
        return None
    format_slug = recipe_id.removesuffix(":master")
    try:
        profiles, _ = load_engine_registry()
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    profile = profiles.get(format_slug)
    if profile is None or profile.execution_status != "commissioned":
        return None
    master_pages = spec.get("masterPages")
    if (
        not isinstance(master_pages, dict)
        or publication.get("engine") != profile.content_engine
        or master_pages.get("contentEngine") != profile.content_engine
        or not format_page_admits(format_slug, profile.content_niche, master_pages)
    ):
        return None
    demand = spec.get("demand")
    format_mix = demand.get("formatMix") if isinstance(demand, dict) else None
    if not isinstance(format_mix, dict) or set(format_mix) != {format_slug}:
        return None
    share = format_mix.get(format_slug)
    if (
        isinstance(share, bool)
        or not isinstance(share, (int, float))
        or float(share) <= 0
    ):
        return None
    return profile
