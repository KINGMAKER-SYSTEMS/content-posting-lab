"""Server-owned, new-media generation recipes for ShipStream dossier versions.

The control plane sends only a recipe identity and a typed visual treatment.
Prompt composition stays here, from a hash-pinned catalog migrated out of the
posting rail. This module is pure planning: provider submission and durable job
state remain in the control-plane router.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from providers import PROVIDERS
from providers.base import API_KEYS


CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "recipes/generation/prompt_modules.v1.json"
)
READY_MODE = "ready"
MAX_PROVIDER_CALLS = 20
MAX_CAPABILITY_QUANTITY = 10
SUPPORTED_FILTERS = {
    "brightness", "contrast", "saturation", "warmth", "fade", "grain", "vignette",
}


@dataclass(frozen=True)
class GenerationRecipe:
    recipe_id: str
    format_slug: str
    family_name: str
    engine: str
    provider_model: str
    prompt_catalog_hash: str
    family: dict[str, Any]
    provider_config: dict[str, Any]
    recipe_spec: dict[str, Any]

    @property
    def clips_per_generation(self) -> int:
        return max(1, int(self.family.get("clips_per_gen") or 1))

    @property
    def keepers_per_generation(self) -> float:
        return max(0.05, float(self.family.get("keepers_per_gen") or 1.0))

    def planned_provider_calls(self, quantity: int) -> int:
        if not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        calls = max(1, math.ceil(quantity / self.keepers_per_generation))
        if calls > MAX_PROVIDER_CALLS:
            raise ValueError(f"generation plan exceeds {MAX_PROVIDER_CALLS} provider calls")
        return calls


def _catalog_path() -> Path:
    configured = os.environ.get("CONTENT_LAB_PROMPT_CATALOG", "").strip()
    return Path(configured).resolve() if configured else CATALOG_PATH


def load_prompt_catalog() -> tuple[dict[str, Any], str]:
    raw = _catalog_path().read_bytes()
    catalog = json.loads(raw)
    if not isinstance(catalog, dict):
        raise ValueError("prompt catalog must be an object")
    for field in ("formats", "families", "providers"):
        if not isinstance(catalog.get(field), dict):
            raise ValueError(f"prompt catalog {field} must be an object")
    return catalog, hashlib.sha256(raw).hexdigest()


def _runtime_ready(engine: str) -> bool:
    if os.environ.get("CONTENT_LAB_GENERATION_MODE", "off").strip() != READY_MODE:
        return False
    provider = PROVIDERS.get(engine)
    if not isinstance(provider, dict):
        return False
    key_id = provider.get("key_id")
    return bool(key_id and API_KEYS.get(key_id))


def _typed_recipe_spec(publication: dict[str, Any]) -> dict[str, Any] | None:
    try:
        spec = json.loads(publication["recipeSpecCanonical"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(spec, dict) or spec.get("schema") != "dossier.recipe-spec.v1":
        return None
    render = spec.get("renderTreatment")
    demand = spec.get("demand")
    if not isinstance(render, dict) or not isinstance(demand, dict):
        return None
    filters = render.get("filters")
    caption_style = render.get("captionStyle")
    format_mix = demand.get("formatMix")
    if not isinstance(filters, dict) or not isinstance(caption_style, dict) or not isinstance(format_mix, dict):
        return None
    if set(filters) - SUPPORTED_FILTERS:
        return None
    # Grain and vignette do not have an honest implementation in the current
    # ffmpeg treatment path. Refuse a recipe that uses them instead of
    # advertising a style the renderer would silently ignore.
    for unsupported in ("grain", "vignette"):
        value = filters.get(unsupported)
        if value not in (None, 0, 0.0):
            return None
    return spec


def resolve_generation_recipe(
    publication: dict[str, Any], *, require_runtime: bool = True,
) -> GenerationRecipe | None:
    recipe_id = publication.get("recipeId")
    engine = publication.get("engine")
    if not isinstance(recipe_id, str) or not recipe_id.endswith(":master"):
        return None
    if not isinstance(engine, str) or not engine:
        return None
    if require_runtime and not _runtime_ready(engine):
        return None
    spec = _typed_recipe_spec(publication)
    if spec is None:
        return None

    format_slug = recipe_id.removesuffix(":master")
    catalog, catalog_hash = load_prompt_catalog()
    format_config = catalog["formats"].get(format_slug)
    if not isinstance(format_config, dict):
        return None
    if format_config.get("clip_mode") is True or format_config.get("parked") is True:
        return None
    family_name = format_config.get("family")
    family = catalog["families"].get(family_name)
    if not isinstance(family_name, str) or not isinstance(family, dict):
        return None
    # The first production slice is deliberately text-to-video. I2V remains
    # blocked until its anchor bytes and anchor manifest are migrated and
    # hash-bound alongside this catalog.
    if family.get("method") != "t2v" or family.get("base_anchor") not in (None, ""):
        return None
    if family.get("provider") != engine:
        return None
    provider_config = catalog["providers"].get(engine)
    runtime_provider = PROVIDERS.get(engine)
    if not isinstance(provider_config, dict) or not isinstance(runtime_provider, dict):
        return None
    model = provider_config.get("replicate_model")
    if model not in runtime_provider.get("models", []):
        return None
    if float(provider_config.get("cost_per_gen_usd") or 0) <= 0:
        return None

    format_mix = spec["demand"]["formatMix"]
    if format_mix:
        if set(format_mix) != {format_slug}:
            return None
        share = format_mix.get(format_slug)
        if not isinstance(share, (int, float)) or float(share) <= 0:
            return None

    return GenerationRecipe(
        recipe_id=recipe_id,
        format_slug=format_slug,
        family_name=family_name,
        engine=engine,
        provider_model=model,
        prompt_catalog_hash=catalog_hash,
        family=family,
        provider_config=provider_config,
        recipe_spec=spec,
    )


def fnv1a_64(value: str) -> int:
    result = 0xCBF29CE484222325
    for byte in value.encode("utf-8"):
        result ^= byte
        result = (result * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return result


def compose_prompt(recipe: GenerationRecipe, run_id: str, index: int) -> tuple[str, dict[str, str]]:
    template = str(recipe.family.get("template") or "")
    subject = str(recipe.family.get("fixed_subject") or "")
    guards = str(recipe.family.get("quality_guards") or "")
    prompt = (
        template.replace("{subject}", subject)
        .replace("{guards}", guards)
        .replace("{subject_state}", guards)
    )
    slots = recipe.family.get("slots")
    if not isinstance(slots, dict):
        slots = {}
    ordered = [(name, values) for name, values in sorted(slots.items()) if isinstance(values, list)]
    space = math.prod(max(1, len(values)) for _, values in ordered)
    seed = fnv1a_64(run_id)
    stride = max(1, seed % max(1, space)) | 1
    while space > 1 and math.gcd(stride, space) != 1:
        stride += 2
    combo = (index * stride + seed) % max(1, space)
    used: dict[str, str] = {}
    for name, values in ordered:
        count = max(1, len(values))
        pick = combo % count
        combo //= count
        value = str(values[pick]) if values else ""
        used[name] = value
        prompt = prompt.replace("{" + name + "}", value)
    if "{" in prompt or "}" in prompt:
        raise ValueError("prompt template contains an unfilled slot")
    return prompt, used


def generation_options(recipe: GenerationRecipe) -> dict[str, Any]:
    base = recipe.provider_config.get("base_input")
    options = dict(base) if isinstance(base, dict) else {}
    if "prompt_optimizer" in options:
        options["optimize_prompt"] = bool(options.pop("prompt_optimizer"))
    extra = recipe.family.get("extra")
    if isinstance(extra, dict):
        options.update(extra)
    return options


def dossier_filters_to_color_correction(recipe: GenerationRecipe) -> dict[str, float]:
    filters = recipe.recipe_spec["renderTreatment"]["filters"]
    result: dict[str, float] = {}
    for source, target in (
        ("brightness", "brightness"),
        ("contrast", "contrast"),
        ("saturation", "saturation"),
    ):
        value = filters.get(source)
        if isinstance(value, (int, float)):
            result[target] = (float(value) - 1.0) * 100.0
    warmth = filters.get("warmth")
    if isinstance(warmth, (int, float)):
        result["temperature"] = float(warmth) * 100.0
    fade = filters.get("fade")
    if isinstance(fade, (int, float)):
        result["fade"] = float(fade) * 100.0
    return result
