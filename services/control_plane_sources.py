"""Version-bound approved-library executors for dossier recipes.

Source execution is derived from the same Master Pages-bound registry used by
generated media. A similarly named project or recipe marker is never enough:
the registry must commission one exact source-library manifest hash.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from services.content_engine_registry import resolve_material_profile
from services.control_plane_generation import typed_recipe_spec
from services.control_plane_source_libraries import (
    SourceLibraryError,
    load_source_library_manifest,
)


@dataclass(frozen=True)
class SourceRecipe:
    recipe_id: str
    format_slug: str
    engine: str
    base_recipe_id: str
    base_recipe_version: str
    max_quantity: int
    engine_registry_hash: str
    format_contract_version: str
    source_manifest_hash: str
    material_source: str
    asset_type: str
    recipe_spec: dict[str, Any]

    @property
    def served_ledger_key(self) -> str:
        return f"dossier-source:{self.base_recipe_id}:{self.base_recipe_version}"


def resolve_source_recipe(
    publication: dict[str, Any],
    *,
    base_recipe_lookup: Callable[[str], dict[str, Any] | None],
) -> SourceRecipe | None:
    if not isinstance(publication, dict) or not callable(base_recipe_lookup):
        return None
    recipe_id = publication.get("recipeId")
    engine = publication.get("engine")
    if not isinstance(recipe_id, str) or engine != "sourced_video":
        return None
    spec = typed_recipe_spec(publication)
    if spec is None:
        return None
    profile = resolve_material_profile(publication, spec)
    if (
        profile is None
        or profile.content_engine != engine
        or profile.material_source != "source_library"
        or profile.executor_kind != "source_library"
        or profile.executor_id is None
        or profile.executor_version is None
        or recipe_id != f"{profile.format_slug}:master"
    ):
        return None
    try:
        manifest = load_source_library_manifest(profile.executor_id)
    except SourceLibraryError:
        return None
    if (
        manifest.format_slug != profile.format_slug
        or profile.executor_version != f"sha256:{manifest.sha256}"
    ):
        return None
    base_recipe_version = manifest.recipe_version
    base = base_recipe_lookup(profile.executor_id)
    if (
        not isinstance(base, dict)
        or base.get("recipeId") != profile.executor_id
        or base.get("engine") != "content_lab"
        or base.get("recipeVersion") != base_recipe_version
        or not isinstance(base.get("maxQuantity"), int)
        or isinstance(base.get("maxQuantity"), bool)
        or base["maxQuantity"] < profile.max_quantity
    ):
        return None
    return SourceRecipe(
        recipe_id=recipe_id,
        format_slug=profile.format_slug,
        engine=engine,
        base_recipe_id=profile.executor_id,
        base_recipe_version=base_recipe_version,
        max_quantity=profile.max_quantity,
        engine_registry_hash=profile.registry_hash,
        format_contract_version=profile.format_contract_version,
        source_manifest_hash=manifest.sha256,
        material_source=profile.material_source,
        asset_type=profile.asset_type,
        recipe_spec=spec,
    )
