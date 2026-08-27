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
    method = family.get("method")
    if method not in {"t2v", "i2v"}:
        return None
    if method == "t2v" and family.get("base_anchor") not in (None, ""):
        return None
    if method == "i2v":
        manifest_sha = family.get("anchor_manifest_sha256")
        base_sha = family.get("base_anchor_sha256")
        if bool(manifest_sha) == bool(base_sha):
            return None
        if manifest_sha and (
            not isinstance(manifest_sha, str)
            or not SHA256.fullmatch(manifest_sha)
            or not isinstance(family.get("anchor_count"), int)
            or not 1 <= family["anchor_count"] <= 100
        ):
            return None
        if base_sha and (
            not isinstance(base_sha, str)
            or not SHA256.fullmatch(base_sha)
            or not isinstance(family.get("base_anchor_bytes"), int)
            or not 1 <= family["base_anchor_bytes"] <= MAX_ANCHOR_BYTES
        ):
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
