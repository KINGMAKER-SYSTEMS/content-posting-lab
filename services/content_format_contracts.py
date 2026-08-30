"""Strict content-format definitions behind the Master Pages ontology.

A niche/engine label is routing intent, not a creative recipe.  This registry
records whether the material, visual, output, review, and downstream binding
contracts for a format are actually defined.  Engine commissioning binds to
the canonical hash of one complete entry; incomplete entries remain visible
without becoming executable.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any


CONTRACTS_PATH = (
    Path(__file__).resolve().parents[1]
    / "recipes/content-format-contracts.v1.json"
)
CONTRACTS_SCHEMA = "content-lab.content-format-contracts.v1"
FORMAT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
SHA256_VERSION = re.compile(r"^sha256:[0-9a-f]{64}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
CONTRACT_FIELDS = {
    "contentNiche", "contentEngine", "materialSource", "assetType",
    "definitionStatus", "creativeAuthority", "dimensions", "output",
    "reviewAuthority", "reviewGates", "distribution", "definitionGaps",
}
AUTHORITY_FIELDS = {"kind", "id", "version"}
DIMENSION_FIELDS = {
    "subject", "setting", "shotGrammar", "lighting", "motion", "duration",
    "textPolicy", "negativeRules", "referenceExamples",
}
DIMENSION_VALUE_FIELDS = {"status", "rule", "authority"}
OUTPUT_FIELDS = {"aspectRatio", "container", "durationPolicy", "audioPolicy"}
DISTRIBUTION_FIELDS = {"captions", "sounds"}


@dataclass(frozen=True)
class CreativeAuthority:
    kind: str
    authority_id: str
    version: str


@dataclass(frozen=True)
class FormatContract:
    format_slug: str
    content_niche: str
    content_engine: str
    material_source: str
    asset_type: str
    definition_status: str
    creative_authority: CreativeAuthority | None
    dimensions: dict[str, dict[str, Any]]
    output: dict[str, str]
    review_authority: str | None
    review_gates: tuple[str, ...]
    distribution: dict[str, str]
    definition_gaps: tuple[str, ...]
    contract_hash: str


def _contracts_path() -> Path:
    configured = os.environ.get("CONTENT_LAB_FORMAT_CONTRACTS", "").strip()
    return Path(configured).resolve() if configured else CONTRACTS_PATH


def _nonempty(value: Any, maximum: int = 240) -> bool:
    return (
        isinstance(value, str)
        and value == value.strip()
        and bool(value)
        and len(value) <= maximum
    )


def _canonical_hash(value: dict[str, Any]) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _parse_authority(value: Any) -> CreativeAuthority | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != AUTHORITY_FIELDS:
        raise ValueError("format creative authority is invalid")
    kind = value.get("kind")
    authority_id = value.get("id")
    version = value.get("version")
    if (
        not _nonempty(kind)
        or not isinstance(authority_id, str)
        or not SAFE_ID.fullmatch(authority_id)
        or not isinstance(version, str)
        or not SHA256_VERSION.fullmatch(version)
    ):
        raise ValueError("format creative authority is invalid")
    return CreativeAuthority(kind, authority_id, version)


def _parse_contract(format_slug: str, value: Any) -> FormatContract:
    if not FORMAT_ID.fullmatch(format_slug):
        raise ValueError("content format id is invalid")
    if not isinstance(value, dict) or set(value) != CONTRACT_FIELDS:
        raise ValueError(f"content format {format_slug} fields are invalid")

    content_niche = value.get("contentNiche")
    content_engine = value.get("contentEngine")
    material_source = value.get("materialSource")
    asset_type = value.get("assetType")
    definition_status = value.get("definitionStatus")
    if (
        not _nonempty(content_niche)
        or not _nonempty(content_engine)
        or not _nonempty(material_source)
        or asset_type != "video/mp4"
        or definition_status not in {"complete", "incomplete"}
    ):
        raise ValueError(f"content format {format_slug} identity is invalid")

    authority = _parse_authority(value.get("creativeAuthority"))
    dimensions = value.get("dimensions")
    if not isinstance(dimensions, dict) or set(dimensions) != DIMENSION_FIELDS:
        raise ValueError(f"content format {format_slug} dimensions are invalid")
    undefined: list[str] = []
    for name, dimension in dimensions.items():
        if (
            not isinstance(dimension, dict)
            or set(dimension) != DIMENSION_VALUE_FIELDS
            or dimension.get("status") not in {"defined", "undefined"}
        ):
            raise ValueError(f"content format {format_slug} dimension {name} is invalid")
        pointer = dimension.get("authority")
        rule = dimension.get("rule")
        if dimension["status"] == "defined":
            if not _nonempty(pointer) or not _nonempty(rule, maximum=600):
                raise ValueError(f"content format {format_slug} dimension {name} has no authority")
        elif pointer is not None or rule is not None:
            raise ValueError(f"content format {format_slug} undefined dimension {name} exposes authority")
        else:
            undefined.append(name)

    output = value.get("output")
    if (
        not isinstance(output, dict)
        or set(output) != OUTPUT_FIELDS
        or output.get("aspectRatio") != "9:16"
        or output.get("container") != asset_type
        or output.get("durationPolicy") not in {"executor_bound", "source_bound"}
        or output.get("audioPolicy") != "campaign_sound_bound_downstream"
    ):
        raise ValueError(f"content format {format_slug} output is invalid")

    review_authority = value.get("reviewAuthority")
    if review_authority is not None and not _nonempty(review_authority):
        raise ValueError(f"content format {format_slug} review authority is invalid")
    review_gates = value.get("reviewGates")
    if (
        not isinstance(review_gates, list)
        or not review_gates
        or any(not _nonempty(gate) for gate in review_gates)
        or len(review_gates) != len(set(review_gates))
    ):
        raise ValueError(f"content format {format_slug} review gates are invalid")

    distribution = value.get("distribution")
    if (
        not isinstance(distribution, dict)
        or set(distribution) != DISTRIBUTION_FIELDS
        or distribution.get("captions") != "page_binding_required"
        or distribution.get("sounds") != "page_binding_required"
    ):
        raise ValueError(f"content format {format_slug} distribution is invalid")

    gaps = value.get("definitionGaps")
    if (
        not isinstance(gaps, list)
        or any(not _nonempty(gap) for gap in gaps)
        or gaps != sorted(set(gaps))
    ):
        raise ValueError(f"content format {format_slug} gaps are invalid")
    expected_gaps = sorted([
        *(("creativeAuthority",) if authority is None else ()),
        *(("reviewAuthority",) if review_authority is None else ()),
        *undefined,
    ])
    if gaps != expected_gaps:
        raise ValueError(f"content format {format_slug} gaps do not match its definition")
    if definition_status == "complete" and gaps:
        raise ValueError(f"complete content format {format_slug} has definition gaps")
    if definition_status == "incomplete" and not gaps:
        raise ValueError(f"incomplete content format {format_slug} has no definition gaps")

    return FormatContract(
        format_slug=format_slug,
        content_niche=content_niche,
        content_engine=content_engine,
        material_source=material_source,
        asset_type=asset_type,
        definition_status=definition_status,
        creative_authority=authority,
        dimensions=dimensions,
        output=output,
        review_authority=review_authority,
        review_gates=tuple(review_gates),
        distribution=distribution,
        definition_gaps=tuple(gaps),
        contract_hash=_canonical_hash(value),
    )


def load_format_contracts() -> tuple[dict[str, FormatContract], str]:
    raw = _contracts_path().read_bytes()
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "contracts"}
        or value.get("schema") != CONTRACTS_SCHEMA
        or not isinstance(value.get("contracts"), dict)
        or not value["contracts"]
    ):
        raise ValueError("content format contracts registry is invalid")
    contracts = {
        format_slug: _parse_contract(format_slug, contract)
        for format_slug, contract in value["contracts"].items()
    }
    return contracts, hashlib.sha256(raw).hexdigest()
