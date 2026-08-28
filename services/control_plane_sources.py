"""Version-bound approved-library executors for dossier recipes.

The mapping is deliberately closed and reviewable. A dossier publication never
inherits a similarly named project, and a moving library version immediately
removes the capability until this registry is explicitly updated.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from services.control_plane_generation import typed_recipe_spec


CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "recipes/dossier-source-executors.v1.json"
)
CATALOG_SCHEMA = "content-lab.dossier-source-executors.v1"
ENTRY_FIELDS = {
    "format", "engine", "baseRecipeId", "baseRecipeVersion", "maxQuantity",
}
SAFE_TOKEN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
)


@dataclass(frozen=True)
class SourceRecipe:
    recipe_id: str
    format_slug: str
    engine: str
    base_recipe_id: str
    base_recipe_version: str
    max_quantity: int
    catalog_hash: str
    recipe_spec: dict[str, Any]

    @property
    def served_ledger_key(self) -> str:
        return f"dossier-source:{self.base_recipe_id}:{self.base_recipe_version}"


def _token(value: Any, maximum: int = 200) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(char not in SAFE_TOKEN_CHARS for char in value)
    ):
        return None
    return value


def load_source_catalog() -> tuple[dict[str, Any], str]:
    raw = CATALOG_PATH.read_bytes()
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "executors"}
        or value.get("schema") != CATALOG_SCHEMA
        or not isinstance(value.get("executors"), dict)
    ):
        raise ValueError("dossier source executor catalog is invalid")
    return value, hashlib.sha256(raw).hexdigest()


def resolve_source_recipe(
    publication: dict[str, Any],
    *,
    base_recipe_lookup: Callable[[str], dict[str, Any] | None],
) -> SourceRecipe | None:
    if not isinstance(publication, dict) or not callable(base_recipe_lookup):
        return None
    recipe_id = _token(publication.get("recipeId"))
    engine = _token(publication.get("engine"))
    if recipe_id is None or engine != "sourced_video":
        return None
    spec = typed_recipe_spec(publication)
    if spec is None:
        return None
    try:
        catalog, catalog_hash = load_source_catalog()
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    entry = catalog["executors"].get(recipe_id)
    if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
        return None
    format_slug = _token(entry.get("format"))
    base_recipe_id = _token(entry.get("baseRecipeId"))
    base_recipe_version = _token(entry.get("baseRecipeVersion"))
    max_quantity = entry.get("maxQuantity")
    if (
        format_slug is None
        or entry.get("engine") != engine
        or base_recipe_id is None
        or base_recipe_version is None
        or not isinstance(max_quantity, int)
        or isinstance(max_quantity, bool)
        or not 1 <= max_quantity <= 20
    ):
        return None
    if recipe_id != f"{format_slug}:master":
        return None
    format_mix = spec["demand"]["formatMix"]
    if set(format_mix) != {format_slug}:
        return None
    share = format_mix.get(format_slug)
    if (
        isinstance(share, bool)
        or not isinstance(share, (int, float))
        or float(share) <= 0
    ):
        return None
    base = base_recipe_lookup(base_recipe_id)
    if (
        not isinstance(base, dict)
        or base.get("recipeId") != base_recipe_id
        or base.get("engine") != "content_lab"
        or base.get("recipeVersion") != base_recipe_version
        or not isinstance(base.get("maxQuantity"), int)
        or isinstance(base.get("maxQuantity"), bool)
        or base["maxQuantity"] < max_quantity
    ):
        return None
    return SourceRecipe(
        recipe_id=recipe_id,
        format_slug=format_slug,
        engine=engine,
        base_recipe_id=base_recipe_id,
        base_recipe_version=base_recipe_version,
        max_quantity=max_quantity,
        catalog_hash=catalog_hash,
        recipe_spec=spec,
    )
