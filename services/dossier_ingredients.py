"""Visual ingredient catalog for the page Dossier editor.

The existing registries correctly fail closed, but they expose independent
views of the same creative system: format DNA, commissioned executors, prompt
modules, model capabilities, reference anchors, and approved source libraries.
This module joins those authorities into one read-only, versioned catalog.

It does not resolve a page policy and it does not mutate a recipe. The control
plane overlays the page's saved bindings and active policy on this catalog.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from providers import PROVIDERS
from routers.video import PROVIDER_SCHEMAS
from services.content_engine_registry import MaterialProfile, load_engine_registry
from services.content_format_contracts import FormatContract, load_format_contracts
from services.control_plane_generation import (
    load_prompt_catalog,
    render_treatment_capability,
)
from services.control_plane_sources import source_treatment_capability
from services.control_plane_source_libraries import (
    MANIFEST_DIR,
    SourceLibraryError,
    load_source_library_manifest,
)
from services.dossier_catalog_version import dossier_catalog_version
from services.master_pages_contract import exact_intent
from services.source_dna_registry import (
    MANIFEST_DIR as SOURCE_DNA_MANIFEST_DIR,
    SourceDnaError,
    parse_source_dna_manifest,
)


SCHEMA = "content-lab.dossier-ingredient-catalog.v1"
SELECTION_AUTHORITY_SCHEMA = "content-lab.dossier-selection-authority.v1"
PINNED_LEGACY_DOSSIER_CATALOG_VERSIONS_BY_PUBLICATION = {
    # Exact active v3 publications inventoried through the live dossier
    # gateway on 2026-09-01 before this migration. The tuple binding prevents
    # a legacy fleet hash from authorizing any newly registered publication.
    (
        "tt-chase-miles-4l",
        "pov-dirt-bike:master",
        "dossier-07c9d7bde75fe1d3",
        "437-3c0955cd95d9235f",
        "sha256:07c9d7bde75fe1d3603c74737b96e2ef9ad02b97f87e218b97b1e05ce1251781",
    ): "sha256:217efb75a3f98ba1ea96dc7dc57a5e012b3f87891b59cbc42ea5ca1db8d66da1",
    (
        "acct:rail:bacc74107b722f39adfc0347",
        "truck-scenic:master",
        "dossier-bc754ff466dcd88a",
        "458-71742be29acbf84a",
        "sha256:bc754ff466dcd88accbde7961c2e3d4b3ada890302e151839189ab768451e60b",
    ): "sha256:217efb75a3f98ba1ea96dc7dc57a5e012b3f87891b59cbc42ea5ca1db8d66da1",
    (
        "tt-warner-beaujenkins",
        "truck-scenic:master",
        "dossier-9374e7859261ba0c",
        "646-fd890ef54d6ae5b8",
        "sha256:9374e7859261ba0c7945a8652a30d5c5a0023e5cf2c8ef0cbad98719969721ca",
    ): "sha256:00910ba76b3ba4cba312670fa734e6ac769fd7cfe42ea946d8599b318feb2b69",
    (
        "tt-warner-codyjames6-7",
        "truck-scenic:master",
        "dossier-d70e3f73aa097590",
        "648-f5e84a9fb80874b9",
        "sha256:d70e3f73aa097590f8e6bbb61025e76f186dc94aa49a521ef5de85366f031adf",
    ): "sha256:00910ba76b3ba4cba312670fa734e6ac769fd7cfe42ea946d8599b318feb2b69",
}
PINNED_LEGACY_DOSSIER_CATALOG_VERSIONS = frozenset(
    PINNED_LEGACY_DOSSIER_CATALOG_VERSIONS_BY_PUBLICATION.values()
)


def is_pinned_legacy_catalog_version(
    value: Any,
    *,
    page_id: str,
    recipe_id: str,
    recipe_version: str,
    dossier_revision: str,
    recipe_spec_hash: str,
) -> bool:
    """Accept a pre-migration hash only for its inventoried publication."""
    if not isinstance(value, str):
        return False
    return value == PINNED_LEGACY_DOSSIER_CATALOG_VERSIONS_BY_PUBLICATION.get(
        (page_id, recipe_id, recipe_version, dossier_revision, recipe_spec_hash)
    )


def _selected_model_authority(option: dict[str, Any]) -> dict[str, Any]:
    def execution_shape(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: execution_shape(item)
                for key, item in value.items()
                if key not in {"label", "note", "placeholder"}
            }
        if isinstance(value, list):
            return [execution_shape(item) for item in value]
        return value

    return {
        "providerId": option.get("providerId"),
        "modelId": option.get("modelId"),
        "defaults": option.get("defaults"),
        "inputContract": option.get("inputContract"),
        "controls": execution_shape(option.get("controls")),
    }


def _selection_catalog_version(
    format_entry: dict[str, Any],
    production: dict[str, Any],
) -> str | None:
    """Version the exact selected format and ingredients, not alternatives."""
    ingredients = {
        entry["ingredientId"]: entry for entry in format_entry.get("ingredients", [])
    }
    model = ingredients.get("visual-model")
    selected_model = None
    provider_id = production.get("providerId")
    model_id = production.get("modelId")
    if (provider_id is None) != (model_id is None):
        return None
    if provider_id is not None:
        selected_model = next((
            option for option in (model or {}).get("options", [])
            if option.get("providerId") == provider_id
            and option.get("modelId") == model_id
        ), None)
        if selected_model is None:
            return None

    prompt = (ingredients.get("prompt-module") or {}).get("binding")
    if production.get("promptModuleId") != (
        prompt.get("familyId") if isinstance(prompt, dict) else None
    ):
        return None
    reference = (ingredients.get("reference-media") or {}).get("binding")
    if production.get("referenceSetId") != (
        reference.get("referenceSetId") if isinstance(reference, dict) else None
    ):
        return None
    source = ingredients.get("master-source-video")
    source_id = production.get("sourceLibraryId")
    selected_source = None
    if source_id is not None:
        selected_source = next((
            option for option in (source or {}).get("options", [])
            if option.get("libraryId") == source_id
        ), None)
        if selected_source is None:
            return None
    elif source is not None:
        return None

    treatment = next((
        entry.get("binding") for entry in format_entry.get("ingredients", [])
        if entry.get("ingredientId") in {"clip-treatment", "slideshow-treatment"}
    ), None)
    model_binding = (model or {}).get("binding")
    projection = {
        "schema": SELECTION_AUTHORITY_SCHEMA,
        "format": {
            key: format_entry.get(key) for key in (
                "formatId", "recipeId", "contentEngine", "materialSource",
                "definitionStatus", "executionStatus", "formatContractVersion",
                "executor", "review",
            )
        },
        "selection": {
            "model": None if selected_model is None else _selected_model_authority(selected_model),
            "familyOverrides": (
                model_binding.get("familyOverrides")
                if isinstance(model_binding, dict) else None
            ),
            "prompt": prompt,
            "reference": reference,
            "source": selected_source,
            "treatment": treatment,
        },
    }
    canonical = json.dumps(
        projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return dossier_catalog_version(canonical)


def _production_selections(format_entry: dict[str, Any]) -> list[dict[str, Any]]:
    ingredients = {
        entry["ingredientId"]: entry for entry in format_entry.get("ingredients", [])
    }
    prompt = (ingredients.get("prompt-module") or {}).get("binding")
    reference = (ingredients.get("reference-media") or {}).get("binding")
    base = {
        "promptModuleId": prompt.get("familyId") if isinstance(prompt, dict) else None,
        "referenceSetId": reference.get("referenceSetId") if isinstance(reference, dict) else None,
    }
    model = ingredients.get("visual-model")
    source = ingredients.get("master-source-video")
    if model is not None:
        identities = [{
            **base,
            "providerId": option.get("providerId"),
            "modelId": option.get("modelId"),
            "sourceLibraryId": None,
        } for option in model.get("options", [])]
    elif source is not None:
        identities = [{
            **base,
            "providerId": None,
            "modelId": None,
            "sourceLibraryId": option.get("libraryId"),
        } for option in source.get("options", [])]
    else:
        identities = []
    selections: list[dict[str, Any]] = []
    for identity in identities:
        version = _selection_catalog_version(format_entry, identity)
        if version is not None:
            selections.append({"catalogVersion": version, **identity})
    return selections


def _advertised_defaults(provider_config: dict[str, Any]) -> dict[str, Any]:
    defaults = dict(provider_config.get("base_input") or {})
    if "prompt_optimizer" in defaults:
        defaults["optimize_prompt"] = bool(defaults.pop("prompt_optimizer"))
    return defaults


def _model_options(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    configured = catalog.get("providers", {})
    for provider_id, provider_config in sorted(configured.items()):
        provider = PROVIDERS.get(provider_id)
        if not isinstance(provider, dict) or not isinstance(provider_config, dict):
            continue
        controls = PROVIDER_SCHEMAS.get(provider_id)
        if not isinstance(controls, dict):
            controls = {}
        for model_id in provider.get("models", []):
            if not isinstance(model_id, str) or not model_id:
                continue
            options.append({
                "providerId": provider_id,
                "providerLabel": str(provider.get("name") or provider_id),
                "modelId": model_id,
                "defaults": _advertised_defaults(provider_config),
                "inputContract": {
                    "referenceImage": "required" if controls.get("image_required") is True else "not_advertised",
                    "lastImage": controls.get("last_image_supported") is True,
                },
                "controls": controls,
            })
    return options


def _model_binding(family: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any] | None:
    provider_id = family.get("provider")
    provider = catalog.get("providers", {}).get(provider_id)
    runtime = PROVIDERS.get(provider_id)
    if not isinstance(provider_id, str) or not isinstance(provider, dict) or not isinstance(runtime, dict):
        return None
    model_id = provider.get("replicate_model")
    if not isinstance(model_id, str) or model_id not in runtime.get("models", []):
        return None
    controls = PROVIDER_SCHEMAS.get(provider_id)
    controls = controls if isinstance(controls, dict) else {}
    return {
        "providerId": provider_id,
        "modelId": model_id,
        "inputContract": {
            "referenceImage": "required" if controls.get("image_required") is True else "not_advertised",
            "lastImage": controls.get("last_image_supported") is True,
        },
        "defaults": _advertised_defaults(provider),
        "familyOverrides": dict(family.get("extra") or {}),
    }


def _prompt_binding(family_id: str, family: dict[str, Any]) -> dict[str, Any]:
    slots = family.get("slots")
    return {
        "familyId": family_id,
        "method": family.get("method"),
        "fixedSubject": family.get("fixed_subject"),
        "template": family.get("template"),
        "motion": family.get("motion_prompt"),
        "qualityGuards": family.get("quality_guards"),
        "variationGroups": dict(slots) if isinstance(slots, dict) else {},
    }


def _reference_binding(family: dict[str, Any]) -> dict[str, Any] | None:
    manifest_sha = family.get("anchor_manifest_sha256")
    if isinstance(manifest_sha, str) and len(manifest_sha) == 64:
        return {
            "referenceSetId": f"anchor-manifest:{manifest_sha}",
            "kind": "image_set",
            "manifestSha256": manifest_sha,
            "count": family.get("anchor_count"),
            "rotation": family.get("anchor_rotation"),
        }
    base_sha = family.get("base_anchor_sha256")
    if isinstance(base_sha, str) and len(base_sha) == 64:
        return {
            "referenceSetId": f"anchor:{base_sha}",
            "kind": "image",
            "sha256": base_sha,
            "bytes": family.get("base_anchor_bytes"),
            "path": family.get("base_anchor"),
        }
    return None


def _approved_cut_library_options(format_slug: str) -> list[dict[str, Any]]:
    """Project existing v1 manifests as approved cuts, never as source DNA.

    The v1 manifest proves the selected clip bytes and their QA authority. It
    carries no master-media SHA, source duration, or cut-window lineage, so it
    cannot truthfully advertise those already-cut files as recuttable masters.
    """
    options: list[dict[str, Any]] = []
    for path in sorted(Path(MANIFEST_DIR).glob("*.json")):
        try:
            manifest = load_source_library_manifest(path.stem)
        except SourceLibraryError:
            continue
        if manifest.format_slug != format_slug:
            continue
        options.append({
            "libraryId": manifest.library_id,
            "version": f"sha256:{manifest.sha256}",
            "clipCount": len(manifest.clips),
            "totalBytes": manifest.total_bytes,
            "selectionAuthority": manifest.authority.get("selection"),
            "role": "approved_derivative_clips",
            "recutEligible": False,
            "lineageStatus": "parent_source_and_cut_window_missing",
            "clips": [{
                "sha256": clip.sha256,
                "bytes": clip.bytes,
                "filename": clip.filename,
                "railPath": clip.rail_path,
                "parentSource": None,
                "cutWindow": None,
            } for clip in manifest.clips],
        })
    return options


def _master_source_options(format_slug: str, page_id: str) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for path in sorted(Path(SOURCE_DNA_MANIFEST_DIR).glob("*.json")):
        try:
            library = parse_source_dna_manifest(path.read_bytes(), path.stem)
        except (OSError, SourceDnaError):
            continue
        if library.format_slug != format_slug or library.page_id != page_id:
            continue
        options.append({
            "libraryId": library.library_id,
            "version": f"sha256:{library.sha256}",
            "pageId": library.page_id,
            "masterCount": len(library.masters),
            "masters": [{
                "sourceId": master.source_id,
                "sha256": master.sha256,
                "bytes": master.bytes,
                "filename": master.filename,
                "mimeType": master.mime_type,
                "storageKey": master.storage_key,
                "durationMs": master.duration_ms,
                "sourceOffsetMs": master.source_offset_ms,
                "provenance": master.provenance,
            } for master in library.masters],
        })
    return options


def _ingredient(
    ingredient_id: str,
    kind: str,
    *,
    required: bool,
    status: str,
    binding: dict[str, Any] | None = None,
    options: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "ingredientId": ingredient_id,
        "kind": kind,
        "required": required,
        "status": status,
        "binding": binding,
        "options": options or [],
    }


def _generated_ingredients(
    format_slug: str,
    catalog: dict[str, Any],
    model_options: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    format_config = catalog.get("formats", {}).get(format_slug)
    family_id = format_config.get("family") if isinstance(format_config, dict) else None
    family = catalog.get("families", {}).get(family_id)
    family = family if isinstance(family, dict) else None
    model = _model_binding(family, catalog) if family is not None else None
    prompt = _prompt_binding(family_id, family) if isinstance(family_id, str) and family is not None else None
    reference = _reference_binding(family) if family is not None else None
    model_options = [
        option for option in model_options
        if (option["inputContract"]["referenceImage"] == "required") == (reference is not None)
    ]
    reference_requirement = (
        model.get("inputContract", {}).get("referenceImage") if model is not None else None
    )
    reference_required = reference_requirement == "required"
    return [
        _ingredient(
            "visual-model", "model", required=True,
            status="bound" if model is not None else "missing",
            binding=model, options=model_options,
        ),
        _ingredient(
            "prompt-module", "prompt_module", required=True,
            status="bound" if prompt is not None else "missing",
            binding=prompt,
        ),
        _ingredient(
            "reference-media", "reference_media", required=reference_required,
            status="bound" if reference is not None else ("missing" if reference_required else "optional"),
            binding=reference,
        ),
        _ingredient(
            "clip-treatment", "render_treatment", required=True,
            status="bound", binding=render_treatment_capability(),
        ),
    ]


def _sourced_ingredients(
    format_slug: str,
    profile: MaterialProfile,
    page_id: str,
) -> list[dict[str, Any]]:
    options = _approved_cut_library_options(format_slug)
    master_options = _master_source_options(format_slug, page_id)
    bound_master = master_options[0] if len(master_options) == 1 else None
    selected = next(
        (option for option in options if option["libraryId"] == profile.executor_id),
        None,
    )
    if selected is None and len(options) == 1:
        selected = options[0]
    return [
        _ingredient(
            "master-source-video", "master_video_library", required=True,
            status="bound" if bound_master is not None else (
                "selection_required" if master_options else "missing"
            ),
            binding=bound_master,
            options=master_options,
        ),
        _ingredient(
            "approved-cut-library", "approved_derivative_video_library", required=False,
            status="reference" if selected is not None else "missing",
            binding=selected, options=options,
        ),
        _ingredient(
            "clip-treatment", "render_treatment", required=True,
            status="bound",
            binding=source_treatment_capability(),
        ),
    ]


def _slideshow_ingredients(profile: MaterialProfile) -> list[dict[str, Any]]:
    kind = "lyric_image_library" if profile.content_engine == "lyrics_slideshows" else "image_library"
    return [
        _ingredient("image-library", kind, required=True, status="missing"),
        _ingredient("slideshow-treatment", "slideshow_treatment", required=True, status="missing"),
    ]


def _format_entry(
    contract: FormatContract,
    profile: MaterialProfile,
    catalog: dict[str, Any],
    model_options: list[dict[str, Any]],
    page_id: str,
) -> dict[str, Any]:
    if profile.content_engine == "ai_video":
        ingredients = _generated_ingredients(contract.format_slug, catalog, model_options)
    elif profile.content_engine == "sourced_video":
        ingredients = _sourced_ingredients(contract.format_slug, profile, page_id)
    else:
        ingredients = _slideshow_ingredients(profile)
    ingredients.extend([
        _ingredient("caption-bank", "caption_bank", required=True, status="page_binding_required"),
        _ingredient("sound-collection", "sound_collection", required=True, status="page_binding_required"),
    ])
    blockers = list(contract.definition_gaps)
    blockers.extend(
        f'{ingredient["ingredientId"]}:{ingredient["status"]}'
        for ingredient in ingredients
        if ingredient["required"] and ingredient["status"] in {"missing", "selection_required"}
    )
    return {
        "formatId": contract.format_slug,
        "recipeId": f"{contract.format_slug}:master",
        "contentNiche": contract.content_niche,
        "contentEngine": contract.content_engine,
        "materialSource": contract.material_source,
        "definitionStatus": contract.definition_status,
        "executionStatus": profile.execution_status,
        "formatContractVersion": profile.format_contract_version,
        "executor": None if profile.executor_id is None else {
            "kind": profile.executor_kind,
            "id": profile.executor_id,
            "version": profile.executor_version,
            "maxQuantity": profile.max_quantity,
        },
        "ingredients": ingredients,
        "review": {
            "authority": contract.review_authority,
            "gates": list(contract.review_gates),
        },
        "blockers": sorted(set(blockers)),
    }


def build_dossier_ingredient_catalog(
    page_id: str,
    master_pages: dict[str, Any],
    master_pages_hash: str,
) -> dict[str, Any]:
    intent = exact_intent(
        master_pages,
        master_pages_hash,
        expected_page_id=page_id,
    )
    if intent is None:
        raise ValueError("Master Pages intent is invalid")
    contracts, _ = load_format_contracts()
    profiles, _ = load_engine_registry()
    prompt_catalog, _ = load_prompt_catalog()
    model_options = _model_options(prompt_catalog)
    formats = [
        _format_entry(
            contracts[format_slug], profiles[format_slug], prompt_catalog,
            model_options, page_id,
        )
        for format_slug in sorted(contracts)
    ]
    current_format_candidates = [
        entry["formatId"]
        for entry in formats
        if entry["contentNiche"] == intent["contentNiche"]
        and entry["contentEngine"] == intent["contentEngine"]
    ]
    for entry in formats:
        entry["productionSelections"] = _production_selections(entry)
    catalog_canonical = json.dumps(
        {"schema": SCHEMA, "formats": formats},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return {
        "schema": SCHEMA,
        "pageId": page_id,
        "masterPages": intent,
        "masterPagesHash": master_pages_hash,
        # This versions the complete editor snapshot only. Executable recipes
        # bind a productionSelections[].catalogVersion instead.
        "catalogVersion": dossier_catalog_version(catalog_canonical),
        "currentFormatCandidates": current_format_candidates,
        "formats": formats,
    }


def selected_dossier_catalog_version(
    page_id: str,
    master_pages: dict[str, Any],
    master_pages_hash: str,
    format_id: str,
    production: dict[str, Any],
) -> str | None:
    """Resolve the exact selected authority without building every format."""
    intent = exact_intent(
        master_pages, master_pages_hash, expected_page_id=page_id,
    )
    if intent is None:
        return None
    contracts, _ = load_format_contracts()
    profiles, _ = load_engine_registry()
    contract = contracts.get(format_id)
    profile = profiles.get(format_id)
    if (
        contract is None
        or profile is None
        or contract.content_niche != intent["contentNiche"]
        or contract.content_engine != intent["contentEngine"]
    ):
        return None
    prompt_catalog, _ = load_prompt_catalog()
    entry = _format_entry(
        contract, profile, prompt_catalog, _model_options(prompt_catalog), page_id,
    )
    return _selection_catalog_version(entry, production)


def catalog_selection_version(
    catalog: dict[str, Any],
    format_id: str,
    production: dict[str, Any],
) -> str | None:
    """Read the exact precomputed selection version from an editor catalog."""
    identity_fields = (
        "providerId", "modelId", "promptModuleId", "referenceSetId", "sourceLibraryId",
    )
    entry = next((
        item for item in catalog.get("formats", []) if item.get("formatId") == format_id
    ), None)
    if entry is None:
        return None
    selection = next((
        item for item in entry.get("productionSelections", [])
        if all(item.get(field) == production.get(field) for field in identity_fields)
    ), None)
    return selection.get("catalogVersion") if isinstance(selection, dict) else None


def canonical_catalog_bytes(
    page_id: str,
    master_pages: dict[str, Any],
    master_pages_hash: str,
) -> bytes:
    """Stable bytes used by tests and future ETag generation."""
    return json.dumps(
        build_dossier_ingredient_catalog(page_id, master_pages, master_pages_hash),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
