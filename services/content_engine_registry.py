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

from services.content_format_contracts import FormatContract, load_format_contracts


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
ALLOWED_BINDINGS = {
    "ai_video": ("generated_video", "prompt_family"),
    "sourced_video": ("source_library", "source_dna_recut"),
    "sourced_slideshow": ("image_library", "slideshow_renderer"),
    "lyrics_slideshows": ("lyric_image_library", "slideshow_renderer"),
}


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
        or content_engine not in ALLOWED_BINDINGS
        or not _nonempty(material_source)
        or asset_type != "video/mp4"
        or execution_status not in {"commissioned", "uncommissioned"}
        or not isinstance(format_contract_version, str)
        or not SHA256_VERSION.fullmatch(format_contract_version)
        or isinstance(max_quantity, bool)
        or not isinstance(max_quantity, int)
    ):
        raise ValueError(f"engine registry profile {format_slug} is invalid")
    expected_source, expected_kind = ALLOWED_BINDINGS[content_engine]
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
    profiles = {
        format_slug: _parse_profile(
            format_slug, profile, registry_hash, contracts[format_slug],
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
        or master_pages.get("contentNiche") != profile.content_niche
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
