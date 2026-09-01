"""Immutable dossier recipe publications accepted from ShipStream.

Registration is not execution. Publications remain absent from the capability
catalog until a new-media executor can consume the exact typed treatment.
Every accepted publication is immutable and page-scoped.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException

from services.roster import ROSTER_PATH
from services.master_pages_contract import exact_intent, intent_hash
from services.dossier_ingredients import (
    build_dossier_ingredient_catalog,
    catalog_selection_version,
    is_pinned_legacy_catalog_version,
)
from services.caption_discipline import validate_caption_discipline


LANE = "content-bucket-control-plane"
REQUEST_SCHEMA = "content-lab.recipe-publication.v1"
RESPONSE_SCHEMA = "content-lab.response.v1"
BODY_FIELDS = {
    "schema", "pageId", "lane", "recipeId", "engine", "recipeVersion",
    "dossierRevision", "recipeSpecHash", "recipeSpecCanonical",
}
SPEC_FIELDS_V2 = {"schema", "masterPages", "masterPagesHash", "renderTreatment", "demand"}
SPEC_FIELDS_V3 = SPEC_FIELDS_V2 | {"production"}
SPEC_FIELDS_V4 = SPEC_FIELDS_V3 | {"captionDiscipline"}
PRODUCTION_FIELDS = {
    "catalogVersion", "providerId", "modelId", "promptModuleId",
    "referenceSetId", "sourceLibraryId", "variationValues", "controls",
}
RENDER_REQUIRED_FIELDS = {"stylePreset", "filters", "captionStyle"}
RENDER_OPTIONAL_FIELDS = {"clipSpeed", "clipCrop"}
CLIP_CROP_FIELDS = {"zoom", "focusX", "focusY"}
DEMAND_FIELDS = {"formatMix"}
TOKEN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
)
MAX_SPEC_BYTES = 131_072

router = APIRouter()


def _root() -> Path:
    configured = os.environ.get("CONTENT_LAB_RECIPE_ROOT", "").strip()
    root = Path(configured) if configured else ROSTER_PATH.parent / "control_plane_recipes"
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def require_control_plane_bearer(authorization: str | None) -> None:
    expected = os.environ.get("CONTROL_PLANE_TOKEN", "")
    if not expected:
        raise HTTPException(503, "Content Lab control-plane credential is not configured")
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(401, "invalid control-plane credential")
    supplied = authorization[len(prefix):]
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "invalid control-plane credential")


def _token(value: Any, name: str, maximum: int = 200) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(char not in TOKEN_CHARS for char in value)
    ):
        raise HTTPException(400, f"{name} must be a safe bounded token")
    return value


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _record_path(root: Path, body: dict[str, Any]) -> Path:
    identity = json.dumps(
        [body["pageId"], body["recipeId"], body["engine"], body["recipeVersion"]],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return root / f"{hashlib.sha256(identity.encode('utf-8')).hexdigest()}.json"


def _validate_spec(
    canonical: str,
    *,
    page_id: str,
    recipe_id: str,
    recipe_version: str,
    dossier_revision: str,
    recipe_spec_hash: str,
) -> dict[str, Any]:
    if not isinstance(canonical, str) or len(canonical.encode("utf-8")) > MAX_SPEC_BYTES:
        raise HTTPException(400, "recipeSpecCanonical is invalid or too large")
    try:
        spec = json.loads(canonical)
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "recipeSpecCanonical is not valid JSON") from exc
    schema = spec.get("schema") if isinstance(spec, dict) else None
    expected_fields = {
        "dossier.recipe-spec.v2": SPEC_FIELDS_V2,
        "dossier.recipe-spec.v3": SPEC_FIELDS_V3,
        "dossier.recipe-spec.v4": SPEC_FIELDS_V4,
    }.get(schema)
    if not isinstance(spec, dict) or set(spec) != expected_fields or schema not in {
        "dossier.recipe-spec.v2", "dossier.recipe-spec.v3", "dossier.recipe-spec.v4",
    }:
        raise HTTPException(400, "recipe spec schema mismatch")
    master_pages = exact_intent(
        spec.get("masterPages"), spec.get("masterPagesHash"),
        expected_page_id=str(spec.get("masterPages", {}).get("pageId") or ""),
    )
    if master_pages is None:
        raise HTTPException(400, "recipe spec Master Pages intent is invalid")
    render = spec.get("renderTreatment")
    demand = spec.get("demand")
    if (
        not isinstance(render, dict)
        or not RENDER_REQUIRED_FIELDS <= set(render)
        or set(render) - RENDER_REQUIRED_FIELDS - RENDER_OPTIONAL_FIELDS
    ):
        raise HTTPException(400, "render treatment schema mismatch")
    if not isinstance(demand, dict) or set(demand) != DEMAND_FIELDS:
        raise HTTPException(400, "recipe demand schema mismatch")
    if not isinstance(render.get("filters"), dict) or not isinstance(render.get("captionStyle"), dict):
        raise HTTPException(400, "render treatment objects are required")
    if not isinstance(demand.get("formatMix"), dict):
        raise HTTPException(400, "formatMix must be an object")
    preset = render.get("stylePreset")
    if preset is not None and (not isinstance(preset, str) or not preset.strip()):
        raise HTTPException(400, "stylePreset must be a non-empty string or null")
    clip_speed = render.get("clipSpeed", 1.0)
    if (
        isinstance(clip_speed, bool)
        or not isinstance(clip_speed, (int, float))
        or not math.isfinite(float(clip_speed))
        or not 0.5 <= float(clip_speed) <= 2.0
    ):
        raise HTTPException(400, "clipSpeed must be a finite number from 0.5 through 2.0")
    clip_crop = render.get("clipCrop")
    if clip_crop is not None:
        if not isinstance(clip_crop, dict) or set(clip_crop) != CLIP_CROP_FIELDS:
            raise HTTPException(400, "clipCrop schema mismatch")
        zoom = clip_crop.get("zoom")
        focus_x = clip_crop.get("focusX")
        focus_y = clip_crop.get("focusY")
        if (
            isinstance(zoom, bool)
            or not isinstance(zoom, (int, float))
            or not math.isfinite(float(zoom))
            or not 1.0 <= float(zoom) <= 3.0
            or isinstance(focus_x, bool)
            or not isinstance(focus_x, (int, float))
            or not math.isfinite(float(focus_x))
            or not 0.0 <= float(focus_x) <= 1.0
            or isinstance(focus_y, bool)
            or not isinstance(focus_y, (int, float))
            or not math.isfinite(float(focus_y))
            or not 0.0 <= float(focus_y) <= 1.0
        ):
            raise HTTPException(
                400,
                "clipCrop must use 1-3x zoom and normalized 0-1 focal points",
            )
    if any(key.lower() in {"prompt", "instruction", "instructions", "message", "messages"}
           for key in _walk_keys(spec)):
        raise HTTPException(400, "free-form instruction fields are not accepted")
    if schema in {"dossier.recipe-spec.v3", "dossier.recipe-spec.v4"}:
        _validate_production_selection(
            spec,
            page_id=page_id,
            recipe_id=recipe_id,
            recipe_version=recipe_version,
            dossier_revision=dossier_revision,
            recipe_spec_hash=recipe_spec_hash,
        )
    if schema == "dossier.recipe-spec.v4":
        try:
            validate_caption_discipline(spec.get("captionDiscipline"))
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
    return spec


def _ingredient(format_entry: dict[str, Any], ingredient_id: str) -> dict[str, Any] | None:
    return next((entry for entry in format_entry.get("ingredients", [])
                 if entry.get("ingredientId") == ingredient_id), None)


def _single_format(spec: dict[str, Any]) -> str:
    selected = [(key, value) for key, value in spec["demand"]["formatMix"].items()
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0]
    if len(selected) != 1 or float(selected[0][1]) != 1.0:
        raise HTTPException(409, "recipe must select one Content Lab format at weight 1")
    return selected[0][0]


def _valid_control_value(value: Any, control: dict[str, Any]) -> bool:
    kind = control.get("type")
    if kind == "toggle":
        return isinstance(value, bool)
    if kind == "select":
        return value in control.get("options", [])
    if kind == "range":
        valid = (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
            and float(control.get("min")) <= float(value) <= float(control.get("max"))
        )
        step = control.get("step")
        if not valid or step is None:
            return valid
        return (
            not isinstance(step, bool)
            and isinstance(step, (int, float))
            and float(step) > 0
            and abs(
                (float(value) - float(control["min"])) / float(step)
                - round((float(value) - float(control["min"])) / float(step))
            ) < 1e-9
        )
    return False


def _validate_production_selection(
    spec: dict[str, Any],
    *,
    page_id: str,
    recipe_id: str,
    recipe_version: str,
    dossier_revision: str,
    recipe_spec_hash: str,
) -> None:
    production = spec.get("production")
    if not isinstance(production, dict) or set(production) - PRODUCTION_FIELDS:
        raise HTTPException(400, "production selection schema mismatch")
    intent = spec["masterPages"]
    catalog = build_dossier_ingredient_catalog(
        intent["pageId"], intent, spec["masterPagesHash"],
    )
    format_id = _single_format(spec)
    if format_id not in catalog["currentFormatCandidates"]:
        raise HTTPException(409, "selected format does not match Master Pages intent")
    supplied_catalog_version = production.get("catalogVersion")
    if not isinstance(supplied_catalog_version, str):
        raise HTTPException(400, "production catalogVersion must be a string")
    if (
        not is_pinned_legacy_catalog_version(
            supplied_catalog_version,
            page_id=page_id,
            recipe_id=recipe_id,
            recipe_version=recipe_version,
            dossier_revision=dossier_revision,
            recipe_spec_hash=recipe_spec_hash,
        )
        and supplied_catalog_version
        != catalog_selection_version(catalog, format_id, production)
    ):
        raise HTTPException(409, "production selection uses a stale Content Lab catalog")
    format_entry = next((entry for entry in catalog["formats"]
                         if entry["formatId"] == format_id), None)
    if format_entry is None:
        raise HTTPException(409, "selected format is absent from Content Lab")

    model = _ingredient(format_entry, "visual-model")
    provider_id = production.get("providerId")
    model_id = production.get("modelId")
    if (provider_id is None) != (model_id is None):
        raise HTTPException(409, "providerId and modelId must be selected together")
    selected_model = None
    if provider_id is not None:
        selected_model = next((option for option in (model or {}).get("options", [])
                               if option.get("providerId") == provider_id
                               and option.get("modelId") == model_id), None)
        if selected_model is None:
            raise HTTPException(409, "selected model is not advertised by Content Lab")

    prompt_id = production.get("promptModuleId")
    prompt = _ingredient(format_entry, "prompt-module")
    if prompt_id is not None and prompt_id != (prompt or {}).get("binding", {}).get("familyId"):
        raise HTTPException(409, "selected prompt module is not bound to the format")
    reference_id = production.get("referenceSetId")
    reference = _ingredient(format_entry, "reference-media")
    if reference_id is not None and reference_id != (reference or {}).get("binding", {}).get("referenceSetId"):
        raise HTTPException(409, "selected reference set is not bound to the format")
    source_id = production.get("sourceLibraryId")
    source = _ingredient(format_entry, "master-source-video")
    if source_id is not None and not any(option.get("libraryId") == source_id
                                         for option in (source or {}).get("options", [])):
        raise HTTPException(409, "selected source library is not registered as master source DNA")
    if source is not None and source.get("required") is True and source_id is None:
        raise HTTPException(409, "sourceLibraryId is required for sourced master DNA")

    variations = production.get("variationValues", {})
    groups = (prompt or {}).get("binding", {}).get("variationGroups", {})
    if not isinstance(variations, dict) or any(
        not isinstance(value, str) or value not in groups.get(key, [])
        for key, value in variations.items()
    ):
        raise HTTPException(409, "prompt variation selection is not advertised by Content Lab")
    controls = production.get("controls", {})
    if not isinstance(controls, dict):
        raise HTTPException(400, "production controls must be an object")
    advertised = dict((selected_model or {}).get("controls", {}))
    treatment = _ingredient(format_entry, "clip-treatment")
    treatment_controls = (treatment or {}).get("binding", {}).get("controls", {})
    if isinstance(treatment_controls, dict):
        advertised.update(treatment_controls)
    advanced = advertised.get("_advanced", {}) if isinstance(advertised, dict) else {}
    for key, value in controls.items():
        control = advertised.get(key) if isinstance(advertised, dict) else None
        if control is None and isinstance(advanced, dict):
            control = advanced.get(key)
        if not isinstance(control, dict) or not _valid_control_value(value, control):
            raise HTTPException(409, f"production control {key} is not advertised or valid")


def register_recipe(
    body: dict[str, Any], *, lane: str, page_id: str, idempotency_key: str,
) -> dict[str, Any]:
    if not isinstance(body, dict) or set(body) != BODY_FIELDS:
        raise HTTPException(400, "recipe publication fields do not match the contract")
    if body["schema"] != REQUEST_SCHEMA or body["lane"] != LANE or lane != LANE:
        raise HTTPException(400, "recipe publication lane or schema mismatch")
    if body["pageId"] != page_id:
        raise HTTPException(400, "page header does not match body")
    for field in ("pageId", "recipeId", "engine", "recipeVersion", "dossierRevision"):
        _token(body[field], field)
    _token(idempotency_key, "Idempotency-Key")

    canonical = body["recipeSpecCanonical"]
    spec = _validate_spec(
        canonical,
        page_id=body["pageId"],
        recipe_id=body["recipeId"],
        recipe_version=body["recipeVersion"],
        dossier_revision=body["dossierRevision"],
        recipe_spec_hash=body["recipeSpecHash"],
    )
    master_pages = spec["masterPages"]
    if master_pages["pageId"] != body["pageId"] or master_pages["contentEngine"] != body["engine"]:
        raise HTTPException(409, "recipe publication does not match Master Pages identity and engine")
    if _hash(canonical) != body["recipeSpecHash"]:
        raise HTTPException(409, "recipeSpecHash does not bind the supplied canonical bytes")

    record = {**body, "idempotencyKey": idempotency_key, "status": "registered"}
    path = _record_path(_root(), body)
    encoded = json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != record:
            raise HTTPException(409, "recipe tuple is already registered with different bytes")
    else:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    return _registration_response(record)


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def _registration_response(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": RESPONSE_SCHEMA,
        "recipeId": record["recipeId"],
        "engine": record["engine"],
        "recipeVersion": record["recipeVersion"],
        "dossierRevision": record["dossierRevision"],
        "recipeSpecHash": record["recipeSpecHash"],
        "status": "registered",
    }


def load_registered_recipe(
    page_id: str, recipe_id: str, engine: str, recipe_version: str,
) -> dict[str, Any] | None:
    identity = {
        "pageId": page_id,
        "recipeId": recipe_id,
        "engine": engine,
        "recipeVersion": recipe_version,
    }
    path = _record_path(_root(), identity)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    expected = {
        "pageId": page_id,
        "recipeId": recipe_id,
        "engine": engine,
        "recipeVersion": recipe_version,
    }
    if record.get("status") != "registered" or any(record.get(key) != value for key, value in expected.items()):
        return None
    return record


def publication_matches_master_pages(
    publication: dict[str, Any],
    master_pages: dict[str, Any],
    master_pages_hash: str,
) -> bool:
    """Verify one publication against current intent, allowing only an ID rebind.

    The control plane historically minted operational page ids before the
    Master Pages projection had stable canonical ids.  The immutable Notion
    identity and every ontology field remain authoritative; only ``pageId``
    may differ.  This keeps old, reviewed recipe publications usable after an
    identity migration without allowing a changed page to inherit stale
    creative authority.
    """
    target = exact_intent(
        master_pages, master_pages_hash,
        expected_page_id=str(master_pages.get("pageId") or ""),
    )
    if target is None:
        return False
    try:
        spec = json.loads(publication["recipeSpecCanonical"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return False
    if not isinstance(spec, dict):
        return False
    stored_value = spec.get("masterPages")
    stored_hash = spec.get("masterPagesHash")
    stored_page_id = str((stored_value or {}).get("pageId") or "")
    stored = exact_intent(
        stored_value, stored_hash, expected_page_id=stored_page_id,
    )
    if stored is None or publication.get("pageId") != stored_page_id:
        return False
    if stored == target and stored_hash == master_pages_hash:
        return True
    if (
        stored_page_id == target["pageId"]
        or stored.get("source") != "notion"
        or target.get("source") != "notion"
        or not stored.get("notionPageId")
    ):
        return False
    rebound = {**stored, "pageId": target["pageId"]}
    return rebound == target and intent_hash(target) == master_pages_hash


def load_registered_recipe_binding(
    page_id: str,
    recipe_id: str,
    engine: str,
    recipe_version: str,
    master_pages: dict[str, Any],
    master_pages_hash: str,
) -> tuple[str, dict[str, Any]] | None:
    """Resolve one exact or Notion-identity-bound publication.

    Ambiguous aliases fail closed.  Exact page-scoped publications always win
    when they still match the current Master Pages intent.
    """
    exact = load_registered_recipe(page_id, recipe_id, engine, recipe_version)
    if exact is not None and publication_matches_master_pages(
        exact, master_pages, master_pages_hash,
    ):
        return page_id, exact

    matches: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(_root().glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        publication_page_id = str(record.get("pageId") or "")
        if (
            publication_page_id == page_id
            or record.get("status") != "registered"
            or record.get("recipeId") != recipe_id
            or record.get("engine") != engine
            or record.get("recipeVersion") != recipe_version
            or not publication_matches_master_pages(
                record, master_pages, master_pages_hash,
            )
        ):
            continue
        matches.append((publication_page_id, record))
    return matches[0] if len(matches) == 1 else None


def list_registered_recipe_bindings(
    page_id: str,
    master_pages: dict[str, Any],
    master_pages_hash: str,
) -> list[tuple[str, dict[str, Any]]]:
    """List current exact/aliased publications, preferring exact tuples."""
    candidates: dict[tuple[str, str, str], list[tuple[str, dict[str, Any]]]] = {}
    for path in sorted(_root().glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            record.get("status") != "registered"
            or not publication_matches_master_pages(
                record, master_pages, master_pages_hash,
            )
        ):
            continue
        key = (
            str(record.get("recipeId") or ""),
            str(record.get("engine") or ""),
            str(record.get("recipeVersion") or ""),
        )
        candidates.setdefault(key, []).append((str(record.get("pageId") or ""), record))

    resolved: list[tuple[str, dict[str, Any]]] = []
    for key in sorted(candidates):
        rows = candidates[key]
        exact = [row for row in rows if row[0] == page_id]
        if len(exact) == 1:
            resolved.append(exact[0])
        elif len(rows) == 1:
            resolved.append(rows[0])
    return resolved


def list_registered_recipes(page_id: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(_root().glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("status") == "registered" and record.get("pageId") == page_id:
            records.append(record)
    return records


@router.post("/v1/recipes")
def publish_recipe(
    body: dict[str, Any],
    authorization: str | None = Header(default=None),
    x_rt_lane: str = Header(alias="X-RT-Lane"),
    x_rt_page_id: str = Header(alias="X-RT-Page-Id"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
):
    require_control_plane_bearer(authorization)
    return register_recipe(
        body, lane=x_rt_lane, page_id=x_rt_page_id, idempotency_key=idempotency_key,
    )
