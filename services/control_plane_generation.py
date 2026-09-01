"""Server-owned, new-media generation recipes for ShipStream dossier versions.

The control plane sends only a recipe identity and a typed visual treatment.
Prompt composition stays here, from a hash-pinned catalog migrated out of the
posting rail. This module is pure planning: provider submission and durable job
state remain in the control-plane router.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from providers import PROVIDERS
from providers.base import API_KEYS
from services.content_engine_registry import resolve_material_profile
from services.caption_discipline import validate_caption_discipline
from services.content_format_contracts import load_format_contracts


CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "recipes/generation/prompt_modules.v1.json"
)
READY_MODE = "ready"
MAX_PROVIDER_CALLS = 20
MAX_CAPABILITY_QUANTITY = 10
MAX_ANCHOR_BYTES = 5 * 1024 * 1024
MAX_ANCHOR_MANIFEST_BYTES = 64 * 1024
SHA256 = re.compile(r"^[a-f0-9]{64}$")
SUPPORTED_FILTERS = {
    "brightness", "contrast", "saturation", "warmth", "fade", "grain", "vignette",
}
log = logging.getLogger("content_lab.control_plane_generation")
FILTER_RANGES = {
    "brightness": (0.0, 3.0),
    "contrast": (0.0, 3.0),
    "saturation": (0.0, 3.0),
    "warmth": (-1.0, 1.0),
    "fade": (0.0, 1.0),
    "grain": (0.0, 1.0),
    "vignette": (0.0, 1.0),
}
MIN_CLIP_SPEED = 0.5
MAX_CLIP_SPEED = 2.0
MIN_CLIP_CROP_ZOOM = 1.0
MAX_CLIP_CROP_ZOOM = 3.0
MIN_CLIP_CROP_FOCUS = 0.0
MAX_CLIP_CROP_FOCUS = 1.0


def render_treatment_capability() -> dict[str, Any]:
    """Controls accepted by the strict recipe decoder and render executors."""
    return {
        "filters": {
            "brightness": {
                "type": "range", "minimum": FILTER_RANGES["brightness"][0],
                "maximum": FILTER_RANGES["brightness"][1], "step": 0.05,
                "default": 1.0, "label": "Brightness",
            },
            "contrast": {
                "type": "range", "minimum": FILTER_RANGES["contrast"][0],
                "maximum": FILTER_RANGES["contrast"][1], "step": 0.05,
                "default": 1.0, "label": "Contrast",
            },
            "saturation": {
                "type": "range", "minimum": FILTER_RANGES["saturation"][0],
                "maximum": FILTER_RANGES["saturation"][1], "step": 0.05,
                "default": 1.0, "label": "Saturation",
            },
            "warmth": {
                "type": "range", "minimum": FILTER_RANGES["warmth"][0],
                "maximum": FILTER_RANGES["warmth"][1], "step": 0.05,
                "default": 0.0, "label": "Warmth",
            },
            "fade": {
                "type": "range", "minimum": FILTER_RANGES["fade"][0],
                "maximum": FILTER_RANGES["fade"][1], "step": 0.05,
                "default": 0.0, "label": "Fade",
            },
            "grain": {
                "type": "range", "minimum": FILTER_RANGES["grain"][0],
                "maximum": FILTER_RANGES["grain"][1], "step": 0.05,
                "default": 0.0, "label": "Grain",
            },
            "vignette": {
                "type": "range", "minimum": FILTER_RANGES["vignette"][0],
                "maximum": FILTER_RANGES["vignette"][1], "step": 0.05,
                "default": 0.0, "label": "Vignette",
            },
        },
        "clipSpeed": {
            "type": "range", "minimum": MIN_CLIP_SPEED,
            "maximum": MAX_CLIP_SPEED, "default": 1.0,
        },
        "clipCrop": {
            "zoom": {
                "type": "range", "minimum": MIN_CLIP_CROP_ZOOM,
                "maximum": MAX_CLIP_CROP_ZOOM, "default": MIN_CLIP_CROP_ZOOM,
            },
            "focusX": {
                "type": "range", "minimum": MIN_CLIP_CROP_FOCUS,
                "maximum": MAX_CLIP_CROP_FOCUS, "default": 0.5,
            },
            "focusY": {
                "type": "range", "minimum": MIN_CLIP_CROP_FOCUS,
                "maximum": MAX_CLIP_CROP_FOCUS, "default": 0.5,
            },
        },
    }


def render_treatment_capability_hash() -> str:
    """Hash the exact UI/executor treatment schema into the Dossier catalog."""
    canonical = json.dumps(
        render_treatment_capability(), sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class GenerationRecipe:
    recipe_id: str
    format_slug: str
    family_name: str
    engine: str
    provider_model: str
    engine_registry_hash: str
    format_contract_version: str
    material_source: str
    asset_type: str
    executor_version: str
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
        # A control-plane quantity is a requested delivery count. Multi-crop
        # generators emit several independently deliverable clips from one
        # provider master, so spending must be based on that exact output
        # count. ``keepers_per_gen`` remains useful for budgeting/forecasting;
        # it must not make the executor generate five full crop groups for a
        # request that needs only one group.
        calls = max(1, math.ceil(quantity / self.clips_per_generation))
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
    if not isinstance(spec, dict) or spec.get("schema") not in {
        "dossier.recipe-spec.v2", "dossier.recipe-spec.v3", "dossier.recipe-spec.v4",
    }:
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
    for name, value in filters.items():
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return None
        lower, upper = FILTER_RANGES[name]
        if not lower <= float(value) <= upper:
            return None
    clip_speed = render.get("clipSpeed", 1.0)
    if (
        isinstance(clip_speed, bool)
        or not isinstance(clip_speed, (int, float))
        or not math.isfinite(float(clip_speed))
        or not MIN_CLIP_SPEED <= float(clip_speed) <= MAX_CLIP_SPEED
    ):
        return None
    clip_crop = render.get("clipCrop")
    if clip_crop is not None:
        if not isinstance(clip_crop, dict) or set(clip_crop) != {"zoom", "focusX", "focusY"}:
            return None
        zoom = clip_crop.get("zoom")
        focus_x = clip_crop.get("focusX")
        focus_y = clip_crop.get("focusY")
        if (
            isinstance(zoom, bool)
            or not isinstance(zoom, (int, float))
            or not math.isfinite(float(zoom))
            or not MIN_CLIP_CROP_ZOOM <= float(zoom) <= MAX_CLIP_CROP_ZOOM
            or isinstance(focus_x, bool)
            or not isinstance(focus_x, (int, float))
            or not math.isfinite(float(focus_x))
            or not MIN_CLIP_CROP_FOCUS <= float(focus_x) <= MAX_CLIP_CROP_FOCUS
            or isinstance(focus_y, bool)
            or not isinstance(focus_y, (int, float))
            or not math.isfinite(float(focus_y))
            or not MIN_CLIP_CROP_FOCUS <= float(focus_y) <= MAX_CLIP_CROP_FOCUS
        ):
            return None
    if spec.get("schema") == "dossier.recipe-spec.v4":
        try:
            validate_caption_discipline(spec.get("captionDiscipline"))
        except ValueError:
            return None
    return spec


def typed_recipe_spec(publication: dict[str, Any]) -> dict[str, Any] | None:
    """Public strict decoder shared by generated and sourced executors."""
    return _typed_recipe_spec(publication)


def _unavailable(publication: dict[str, Any], reason: str) -> None:
    """Record one bounded reason without logging recipe bytes or credentials."""
    log.warning(
        "registered dossier recipe unavailable page=%s recipe=%s version=%s reason=%s",
        publication.get("pageId"), publication.get("recipeId"),
        publication.get("recipeVersion"), reason,
    )
    return None


def resolve_generation_recipe(
    publication: dict[str, Any], *, require_runtime: bool = True,
) -> GenerationRecipe | None:
    recipe_id = publication.get("recipeId")
    content_engine = publication.get("engine")
    if not isinstance(recipe_id, str) or not recipe_id.endswith(":master"):
        return _unavailable(publication, "recipe_identity")
    if content_engine != "ai_video":
        return _unavailable(publication, "content_engine")
    spec = _typed_recipe_spec(publication)
    if spec is None:
        return _unavailable(publication, "typed_recipe_spec")

    format_slug = recipe_id.removesuffix(":master")
    profile = resolve_material_profile(publication, spec)
    if (
        profile is None
        or profile.format_slug != format_slug
        or profile.material_source != "generated_video"
        or profile.executor_kind != "prompt_family"
        or profile.executor_id is None
        or profile.executor_version is None
    ):
        return _unavailable(publication, "material_profile")
    catalog, catalog_hash = load_prompt_catalog()
    if profile.executor_version != f"sha256:{catalog_hash}":
        return _unavailable(publication, "executor_version")
    format_config = catalog["formats"].get(format_slug)
    if not isinstance(format_config, dict):
        return _unavailable(publication, "format_config")
    if format_config.get("clip_mode") is True or format_config.get("parked") is True:
        return _unavailable(publication, "non_generation_format")
    family_name = format_config.get("family")
    family = catalog["families"].get(family_name)
    if (
        not isinstance(family_name, str)
        or family_name != profile.executor_id
        or not isinstance(family, dict)
    ):
        return _unavailable(publication, "prompt_family")
    method = family.get("method")
    if method not in {"t2v", "i2v"}:
        return _unavailable(publication, "generation_method")
    if method == "t2v" and family.get("base_anchor") not in (None, ""):
        return _unavailable(publication, "unexpected_t2v_anchor")
    if method == "i2v":
        manifest_sha = family.get("anchor_manifest_sha256")
        base_sha = family.get("base_anchor_sha256")
        if bool(manifest_sha) == bool(base_sha):
            return _unavailable(publication, "i2v_anchor_identity")
        if manifest_sha and (
            not isinstance(manifest_sha, str)
            or not SHA256.fullmatch(manifest_sha)
            or not isinstance(family.get("anchor_count"), int)
            or not 1 <= family["anchor_count"] <= 100
        ):
            return _unavailable(publication, "i2v_anchor_manifest")
        if base_sha and (
            not isinstance(base_sha, str)
            or not SHA256.fullmatch(base_sha)
            or not isinstance(family.get("base_anchor_bytes"), int)
            or not 1 <= family["base_anchor_bytes"] <= MAX_ANCHOR_BYTES
        ):
            return _unavailable(publication, "i2v_base_anchor")
    production = (
        spec.get("production")
        if spec.get("schema") in {"dossier.recipe-spec.v3", "dossier.recipe-spec.v4"}
        else {}
    )
    if not isinstance(production, dict):
        return _unavailable(publication, "production_selection")
    if production.get("promptModuleId") not in (None, family_name):
        return _unavailable(publication, "prompt_module")
    if production:
        master_pages = spec.get("masterPages")
        master_pages_hash = spec.get("masterPagesHash")
        if not isinstance(master_pages, dict) or not isinstance(master_pages_hash, str):
            return _unavailable(publication, "catalog_version")
        supplied_catalog_version = production.get("catalogVersion")
        # Local import avoids the ingredient catalog's intentional import of
        # this module for prompt/model bindings.
        from services.dossier_ingredients import (
            is_pinned_legacy_catalog_version,
            selected_dossier_catalog_version,
        )

        if not is_pinned_legacy_catalog_version(
            supplied_catalog_version,
            page_id=str(publication.get("pageId") or ""),
            recipe_id=str(publication.get("recipeId") or ""),
            recipe_version=str(publication.get("recipeVersion") or ""),
            dossier_revision=str(publication.get("dossierRevision") or ""),
            recipe_spec_hash=str(publication.get("recipeSpecHash") or ""),
        ):
            try:
                expected_catalog_version = selected_dossier_catalog_version(
                    str(master_pages.get("pageId") or ""),
                    master_pages,
                    master_pages_hash,
                    format_slug,
                    production,
                )
            except (OSError, ValueError, json.JSONDecodeError, KeyError):
                return _unavailable(publication, "catalog_version")
            if supplied_catalog_version != expected_catalog_version:
                return _unavailable(publication, "catalog_version")
    provider_engine = production.get("providerId") or family.get("provider")
    if not isinstance(provider_engine, str) or not provider_engine:
        return _unavailable(publication, "provider_identity")
    if require_runtime and not _runtime_ready(provider_engine):
        return _unavailable(publication, "provider_runtime")
    provider_config = catalog["providers"].get(provider_engine)
    runtime_provider = PROVIDERS.get(provider_engine)
    if not isinstance(provider_config, dict) or not isinstance(runtime_provider, dict):
        return _unavailable(publication, "provider_config")
    model = production.get("modelId") or provider_config.get("replicate_model")
    if model not in runtime_provider.get("models", []):
        return _unavailable(publication, "provider_model")
    if float(provider_config.get("cost_per_gen_usd") or 0) <= 0:
        return _unavailable(publication, "provider_cost")

    return GenerationRecipe(
        recipe_id=recipe_id,
        format_slug=format_slug,
        family_name=family_name,
        engine=provider_engine,
        provider_model=model,
        engine_registry_hash=profile.registry_hash,
        format_contract_version=profile.format_contract_version,
        material_source=profile.material_source,
        asset_type=profile.asset_type,
        executor_version=profile.executor_version,
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
    selected_variations = recipe.recipe_spec.get("production", {}).get("variationValues", {})
    if not isinstance(selected_variations, dict):
        selected_variations = {}
    for name, values in ordered:
        count = max(1, len(values))
        pick = combo % count
        combo //= count
        value = str(selected_variations.get(name, values[pick] if values else ""))
        used[name] = value
        prompt = prompt.replace("{" + name + "}", value)
    if "{" in prompt or "}" in prompt:
        raise ValueError("prompt template contains an unfilled slot")
    if recipe.family.get("method") == "i2v":
        motion = str(recipe.family.get("motion_prompt") or "").strip()
        if not motion:
            raise ValueError("image-to-video recipe has no server-owned motion prompt")
        prompt = f"{prompt.rstrip('. ')}. Motion: {motion}"
    return prompt, used


def _anchor_origin() -> str:
    configured = os.environ.get("SHIPSTREAM_ANCHOR_ORIGIN", "").strip()
    parsed = urlsplit(configured)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("SHIPSTREAM_ANCHOR_ORIGIN must be a credential-free HTTPS origin")
    return urlunsplit(("https", parsed.netloc, "", "", "")).rstrip("/")


async def _fetch_anchor_object(sha256: str, extension: str, max_bytes: int) -> bytes:
    token = os.environ.get("CONTROL_PLANE_TOKEN", "").strip()
    if len(token) < 32:
        raise ValueError("CONTROL_PLANE_TOKEN is missing or invalid")
    if not SHA256.fullmatch(sha256) or extension not in {"json", "jpg", "png"}:
        raise ValueError("generation anchor identity is invalid")
    url = f"{_anchor_origin()}/api/control-plane/v1/service/content-lab-anchors/{sha256}.{extension}"
    async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
        response = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/octet-stream"},
        )
    if response.status_code != 200:
        raise RuntimeError(f"ShipStream anchor fetch returned {response.status_code}")
    declared = response.headers.get("content-length")
    if declared and (not declared.isdigit() or int(declared) > max_bytes):
        raise RuntimeError("ShipStream anchor content length is invalid")
    if not 1 <= len(response.content) <= max_bytes:
        raise RuntimeError("ShipStream anchor response size is invalid")
    return response.content


def _verified_anchor_bytes(raw: bytes, expected_sha: str, expected_bytes: int) -> bytes:
    if len(raw) != expected_bytes:
        raise RuntimeError("generation anchor byte length does not match its manifest")
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise RuntimeError("generation anchor hash does not match its manifest")
    return raw


def _anchor_extension(path: object) -> str:
    suffix = Path(str(path or "")).suffix.lower().lstrip(".")
    if suffix not in {"jpg", "png"}:
        raise ValueError("generation anchor must be JPEG or PNG")
    return suffix


async def load_generation_anchor(
    recipe: GenerationRecipe,
    run_id: str,
    index: int,
    *,
    fetcher=None,
) -> tuple[str, dict[str, Any]] | None:
    """Return one hash-verified I2V anchor as a data URI.

    Manifest-backed pools rotate without replacement for one bounded job. The
    returned metadata is written beside every generated artifact so retries and
    QA can prove exactly which visual anchor created it.
    """
    if recipe.family.get("method") != "i2v":
        return None
    if not isinstance(run_id, str) or not run_id or not isinstance(index, int) or index < 0:
        raise ValueError("run_id and non-negative generation index are required")
    fetch = fetcher or _fetch_anchor_object
    manifest_sha = recipe.family.get("anchor_manifest_sha256")
    manifest_bound = None
    if manifest_sha:
        manifest_raw = await fetch(manifest_sha, "json", MAX_ANCHOR_MANIFEST_BYTES)
        if hashlib.sha256(manifest_raw).hexdigest() != manifest_sha:
            raise RuntimeError("generation anchor manifest hash mismatch")
        try:
            manifest = json.loads(manifest_raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("generation anchor manifest is invalid JSON") from error
        entries = manifest.get("anchors") if isinstance(manifest, dict) else None
        if (
            manifest.get("schema") != "rail.anchor-pool.v1"
            or manifest.get("recipeId") != recipe.recipe_id
            or manifest.get("format") != recipe.format_slug
            or manifest.get("rotation") != "deterministic_without_replacement"
            or not isinstance(entries, list)
            or len(entries) != recipe.family.get("anchor_count")
        ):
            raise RuntimeError("generation anchor manifest contract mismatch")
        start = fnv1a_64(run_id) % len(entries)
        entry = entries[(start + index) % len(entries)]
        manifest_bound = manifest_sha
    else:
        entry = {
            "path": recipe.family.get("base_anchor"),
            "sha256": recipe.family.get("base_anchor_sha256"),
            "bytes": recipe.family.get("base_anchor_bytes"),
        }
    if not isinstance(entry, dict):
        raise RuntimeError("generation anchor manifest entry is invalid")
    sha256 = entry.get("sha256")
    byte_count = entry.get("bytes")
    if (
        not isinstance(sha256, str)
        or not SHA256.fullmatch(sha256)
        or not isinstance(byte_count, int)
        or not 1 <= byte_count <= MAX_ANCHOR_BYTES
    ):
        raise RuntimeError("generation anchor identity is invalid")
    extension = _anchor_extension(entry.get("path"))
    raw = _verified_anchor_bytes(
        await fetch(sha256, extension, MAX_ANCHOR_BYTES),
        sha256,
        byte_count,
    )
    mime = "image/jpeg" if extension == "jpg" else "image/png"
    data_uri = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    metadata = {
        "sha256": sha256,
        "bytes": byte_count,
        "path": str(entry.get("path") or ""),
        "manifestSha256": manifest_bound,
    }
    return data_uri, metadata


def generation_options(recipe: GenerationRecipe) -> dict[str, Any]:
    base = recipe.provider_config.get("base_input")
    options = dict(base) if isinstance(base, dict) else {}
    if "prompt_optimizer" in options:
        options["optimize_prompt"] = bool(options.pop("prompt_optimizer"))
    extra = recipe.family.get("extra")
    if isinstance(extra, dict):
        options.update(extra)
    selected = recipe.recipe_spec.get("production", {}).get("controls", {})
    if isinstance(selected, dict):
        options.update(selected)
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
    for name in ("grain", "vignette"):
        value = filters.get(name)
        if isinstance(value, (int, float)):
            result[name] = float(value) * 100.0
    return result


def dossier_clip_speed(recipe: GenerationRecipe) -> float:
    """Return the validated playback-rate multiplier for a locked recipe."""
    return float(recipe.recipe_spec["renderTreatment"].get("clipSpeed", 1.0))


def dossier_clip_crop(recipe: GenerationRecipe) -> dict[str, float] | None:
    """Return the normalized crop treatment, or None for legacy recipes."""
    value = recipe.recipe_spec["renderTreatment"].get("clipCrop")
    if value is None:
        return None
    return {
        "zoom": float(value["zoom"]),
        "focusX": float(value["focusX"]),
        "focusY": float(value["focusY"]),
    }
