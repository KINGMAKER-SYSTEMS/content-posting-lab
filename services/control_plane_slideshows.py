"""Hash-bound Syzygy/R2 slideshow planning and execution helpers.

The Dossier selects a stable shared-library address. R2 inventory may grow
behind that address without changing the page recipe; every job snapshots the
exact ordered keys it reserved, and every finished artifact records the bytes
and ETag it actually observed. Content Lab remains the job and page-authority
boundary while Syzygy remains the renderer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from services.content_engine_registry import MaterialProfile, resolve_material_profile
from services.control_plane_generation import typed_recipe_spec


EXECUTOR_PATH = (
    Path(__file__).resolve().parents[1]
    / "recipes/executors/syzygy-slideshow.v1.json"
)
# The UGC format uses a scoped executor file with the same schema and
# executorId. The original file's bytes (and therefore the lyric-edits and
# meme-slideshow profile hashes) stay unchanged; only `ugc-slideshow` resolves
# the scoped copy.
UGC_FORMAT_SLUG = "ugc-slideshow"
UGC_EXECUTOR_PATH = (
    Path(__file__).resolve().parents[1]
    / "recipes/executors/syzygy-slideshow-ugc.v1.json"
)
EXECUTOR_SCHEMA = "content-lab.syzygy-slideshow-executor.v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
LIBRARY_ID = re.compile(r"^syzygy:[a-z0-9][a-z0-9-]{0,63}:[a-z0-9][a-z0-9-]{0,99}$")
OBJECT_KEY = re.compile(
    r"^[a-z0-9][a-z0-9-]{0,63}/[a-z0-9][a-z0-9-]{0,99}/"
    r"(?:photos|clips)/[a-z0-9][a-z0-9_-]{0,199}\.[a-z0-9]{1,10}$"
)
TERMINAL_FAILURES = {"error", "failed", "failed_qa", "cancelled", "canceled"}
MAX_LIBRARY_OBJECTS = 1_000
MAX_RENDER_SECONDS = 15 * 60
POLL_SECONDS = 2.0


class SyzygyError(RuntimeError):
    """A deterministic contract or render failure."""


class SyzygyUnavailable(SyzygyError):
    """A missing or transient Syzygy/R2 boundary."""


@dataclass(frozen=True)
class SyzygyObject:
    key: str
    bytes: int
    url: str

    @property
    def media_type(self) -> str:
        return "video" if "/clips/" in self.key else "photo"


@dataclass(frozen=True)
class SyzygyLibrary:
    library_id: str
    lane: str
    subject: str
    objects: tuple[SyzygyObject, ...]
    snapshot_hash: str


@dataclass(frozen=True)
class SyzygyTemplate:
    slug: str
    media_count: int


@dataclass(frozen=True)
class SlideshowRecipe:
    recipe_id: str
    format_slug: str
    engine: str
    engine_registry_hash: str
    format_contract_version: str
    material_source: str
    asset_type: str
    executor_version: str
    library_id: str
    lane: str
    subject: str
    minimum_library_items: int
    templates: tuple[SyzygyTemplate, ...]
    max_quantity: int
    output_width: int
    output_height: int
    encode_preset: str
    recipe_spec: dict[str, Any]
    engine_profile_hash: str = ""


def _executor_contract(format_slug: str | None = None) -> tuple[dict[str, Any], str]:
    path = UGC_EXECUTOR_PATH if format_slug == UGC_FORMAT_SLUG else EXECUTOR_PATH
    raw = path.read_bytes()
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or not {
            "schema", "executorId", "source", "selection", "reservation",
            "formats", "output",
        }.issubset(value)
        or set(value) - {
            "schema", "executorId", "source", "selection", "reservation",
            "formats", "output", "pageTreatments",
        }
        or value.get("schema") != EXECUTOR_SCHEMA
        or value.get("executorId") != "syzygy-slideshow"
        or value.get("source") != "syzygy_r2_library"
        or value.get("selection") != "deterministic_variety_without_replacement"
        or value.get("reservation") != "active_jobs_and_completed_outputs"
        or not isinstance(value.get("formats"), dict)
        or value.get("output") != {
            "container": "video/mp4",
            "aspectRatio": "9:16",
            "width": 1080,
            "height": 1920,
            "encodePreset": "tiktok_delivery_v1",
            "audioPolicy": "campaign_sound_bound_downstream",
        }
    ):
        raise ValueError("Syzygy slideshow executor contract is invalid")
    # pageTreatments is optional server-owned config: a page_id -> closed cut
    # policy mapping that never binds the render wire. It is not a protocol.
    treatments = value.get("pageTreatments", {})
    if not isinstance(treatments, dict):
        raise ValueError("Syzygy slideshow executor pageTreatments is invalid")
    for page_id, treatment in treatments.items():
        if (
            not isinstance(page_id, str)
            or not page_id.strip()
            or not isinstance(treatment, dict)
            or set(treatment) != {
                "cutStyle", "cutCount", "framesPerCut", "cutsPerSecond",
                "targetSeconds",
            }
            or treatment.get("cutStyle") != "fast_sixteenth"
            or not isinstance(treatment.get("cutCount"), int)
            or not 1 <= treatment["cutCount"] <= 240
            or not isinstance(treatment.get("framesPerCut"), int)
            or not 1 <= treatment["framesPerCut"] <= 60
            or not isinstance(treatment.get("cutsPerSecond"), (int, float))
            or not 0 < treatment["cutsPerSecond"] <= 60
            or not isinstance(treatment.get("targetSeconds"), (int, float))
            or not 0 < treatment["targetSeconds"] <= 60
        ):
            raise ValueError("Syzygy slideshow executor pageTreatment is invalid")
    for format_slug, config in value["formats"].items():
        if (
            not isinstance(format_slug, str)
            or not isinstance(config, dict)
            or set(config) != {"lane", "minimumLibraryItems", "templates"}
            or not isinstance(config.get("lane"), str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", config["lane"])
            or not isinstance(config.get("minimumLibraryItems"), int)
            or not 1 <= config["minimumLibraryItems"] <= 50
            or not isinstance(config.get("templates"), list)
            or not config["templates"]
        ):
            raise ValueError("Syzygy slideshow format contract is invalid")
        for template in config["templates"]:
            if (
                not isinstance(template, dict)
                or set(template) != {"slug", "mediaCount"}
                or not isinstance(template.get("slug"), str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", template["slug"])
                or not isinstance(template.get("mediaCount"), int)
                or not 1 <= template["mediaCount"] <= config["minimumLibraryItems"]
            ):
                raise ValueError("Syzygy slideshow template contract is invalid")
    return value, hashlib.sha256(raw).hexdigest()


def slideshow_treatment_capability(format_slug: str) -> dict[str, Any]:
    executor, digest = _executor_contract(format_slug)
    config = executor["formats"].get(format_slug)
    if not isinstance(config, dict):
        raise ValueError("Syzygy slideshow format is not registered")
    return {
        "renderer": "syzygy",
        "rendererContractVersion": f"sha256:{digest}",
        "selection": executor["selection"],
        "reservation": executor["reservation"],
        "minimumLibraryItems": config["minimumLibraryItems"],
        "templates": [dict(item) for item in config["templates"]],
        "output": dict(executor["output"]),
    }


def _subject(handle: Any) -> str | None:
    if not isinstance(handle, str):
        return None
    value = re.sub(r"[^a-z0-9]+", "-", handle.strip().casefold()).strip("-")
    return value if value and len(value) <= 100 else None


def slideshow_library_address(
    profile: MaterialProfile, master_pages: dict[str, Any],
) -> tuple[str, str, str] | None:
    executor, digest = _executor_contract(profile.format_slug)
    if profile.executor_version != f"sha256:{digest}":
        return None
    config = executor["formats"].get(profile.format_slug)
    subject = _subject(master_pages.get("handle"))
    if not isinstance(config, dict) or subject is None:
        return None
    lane = config["lane"]
    library_id = f"syzygy:{lane}:{subject}"
    return library_id, lane, subject


def _runtime() -> tuple[str, str] | None:
    if os.environ.get("CONTENT_LAB_GENERATION_MODE", "off").strip() != "ready":
        return None
    raw = os.environ.get("SYZYGY_API_URL", "").strip()
    key = os.environ.get("SYZYGY_API_KEY", "").strip()
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not key.startswith("syz_")
    ):
        return None
    return raw.rstrip("/"), key


def _headers(key: str, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def load_syzygy_library(
    profile: MaterialProfile, master_pages: dict[str, Any], *, timeout: float = 15.0,
) -> SyzygyLibrary:
    address = slideshow_library_address(profile, master_pages)
    if address is None:
        raise SyzygyUnavailable("Syzygy slideshow runtime is unavailable")
    library_id, lane, subject = address
    return _fetch_library(library_id, lane, subject, timeout=timeout)


def load_recipe_library(
    recipe: SlideshowRecipe, *, timeout: float = 15.0,
) -> SyzygyLibrary:
    """Read the live library behind one resolved recipe's stable address."""
    return _fetch_library(
        recipe.library_id, recipe.lane, recipe.subject, timeout=timeout,
    )


def _fetch_library(
    library_id: str, lane: str, subject: str, *, timeout: float = 15.0,
) -> SyzygyLibrary:
    runtime = _runtime()
    if runtime is None:
        raise SyzygyUnavailable("Syzygy slideshow runtime is unavailable")
    base, key = runtime
    try:
        response = httpx.get(
            f"{base}/api/v1/library/{quote(lane)}/{quote(subject)}",
            headers=_headers(key), timeout=timeout, follow_redirects=False,
        )
    except httpx.HTTPError as error:
        raise SyzygyUnavailable("Syzygy library read failed") from error
    if response.status_code != 200:
        raise SyzygyUnavailable(f"Syzygy library returned {response.status_code}")
    try:
        payload = response.json()
    except ValueError as error:
        raise SyzygyUnavailable("Syzygy library response is not JSON") from error
    if (
        not isinstance(payload, dict)
        or payload.get("lane") != lane
        or payload.get("subject") != subject
        or payload.get("truncated") is not False
        or payload.get("skipped") != 0
        or not isinstance(payload.get("objects"), list)
        or len(payload["objects"]) > MAX_LIBRARY_OBJECTS
        or payload.get("count") != len(payload["objects"])
    ):
        raise SyzygyUnavailable("Syzygy library response is incomplete")
    objects: list[SyzygyObject] = []
    expected_prefix = f"{lane}/{subject}/"
    for item in payload["objects"]:
        object_key = item.get("key") if isinstance(item, dict) else None
        byte_count = item.get("bytes") if isinstance(item, dict) else None
        url = item.get("url") if isinstance(item, dict) else None
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (
            not isinstance(object_key, str)
            or not object_key.startswith(expected_prefix)
            or not OBJECT_KEY.fullmatch(object_key)
            or not isinstance(byte_count, int)
            or isinstance(byte_count, bool)
            or byte_count <= 0
            or parsed is None
            or parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise SyzygyUnavailable("Syzygy library object is invalid")
        objects.append(SyzygyObject(object_key, byte_count, url))
    objects.sort(key=lambda item: item.key)
    canonical = json.dumps(
        [[item.key, item.bytes] for item in objects],
        separators=(",", ":"), ensure_ascii=False,
    )
    return SyzygyLibrary(
        library_id=library_id,
        lane=lane,
        subject=subject,
        objects=tuple(objects),
        snapshot_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def resolve_slideshow_recipe(
    publication: dict[str, Any], *, require_runtime: bool = True,
) -> SlideshowRecipe | None:
    recipe_id = publication.get("recipeId")
    engine = publication.get("engine")
    if (
        not isinstance(recipe_id, str)
        or not recipe_id.endswith(":master")
        or engine not in {"sourced_slideshow", "lyrics_slideshows"}
    ):
        return None
    spec = typed_recipe_spec(publication)
    if spec is None:
        return None
    profile = resolve_material_profile(publication, spec)
    if (
        profile is None
        or profile.format_slug != recipe_id.removesuffix(":master")
        or profile.executor_kind != "slideshow_renderer"
        or profile.executor_id != "syzygy-slideshow"
        or profile.executor_version is None
        or profile.content_engine != engine
    ):
        return None
    executor, digest = _executor_contract(profile.format_slug)
    if profile.executor_version != f"sha256:{digest}":
        return None
    master_pages = spec.get("masterPages")
    production = spec.get("production")
    address = (
        slideshow_library_address(profile, master_pages)
        if isinstance(master_pages, dict) else None
    )
    config = executor["formats"].get(profile.format_slug)
    if (
        address is None
        or not isinstance(production, dict)
        or production.get("sourceLibraryId") != address[0]
        or not isinstance(config, dict)
        or (require_runtime and _runtime() is None)
    ):
        return None
    templates = tuple(
        SyzygyTemplate(item["slug"], item["mediaCount"])
        for item in config["templates"]
    )
    return SlideshowRecipe(
        recipe_id=recipe_id,
        format_slug=profile.format_slug,
        engine=engine,
        engine_registry_hash=profile.registry_hash,
        engine_profile_hash=profile.authority_hash,
        format_contract_version=profile.format_contract_version,
        material_source=profile.material_source,
        asset_type=profile.asset_type,
        executor_version=profile.executor_version,
        library_id=address[0],
        lane=address[1],
        subject=address[2],
        minimum_library_items=config["minimumLibraryItems"],
        templates=templates,
        max_quantity=profile.max_quantity,
        output_width=executor["output"]["width"],
        output_height=executor["output"]["height"],
        encode_preset=executor["output"]["encodePreset"],
        recipe_spec=spec,
    )


def plan_slideshows(
    recipe: SlideshowRecipe,
    library: SyzygyLibrary,
    count: int,
    unavailable_signatures: set[str],
    run_id: str,
) -> list[dict[str, Any]]:
    if (
        library.library_id != recipe.library_id
        or len(library.objects) < recipe.minimum_library_items
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count <= 0
        or not isinstance(unavailable_signatures, set)
        or any(not SHA256.fullmatch(item) for item in unavailable_signatures)
    ):
        return []
    # The UGC format is a curated page library: the first mediaCount objects in
    # the library's ordinal order form the one and only plan. No offset/stride
    # permutation would be legal (it would reorder the curated arc), so there
    # is never "variety" to synthesize — an exhausted ordered set stays
    # exhausted until the operator ships a new curated set.
    if recipe.format_slug == UGC_FORMAT_SLUG:
        template = recipe.templates[0]
        objects_in_order = list(library.objects)
        if template.media_count > len(objects_in_order):
            return []
        selected = objects_in_order[: template.media_count]
        identity = {
            "templateSlug": template.slug,
            "mediaKeys": [item.key for item in selected],
        }
        canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        signature = hashlib.sha256(canonical.encode()).hexdigest()
        if signature in unavailable_signatures:
            return []
        return [{
            **identity,
            "signature": signature,
            "librarySnapshotHash": library.snapshot_hash,
        }]
    objects = list(library.objects)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for template in recipe.templates:
        if template.media_count > len(objects):
            continue
        for stride in range(1, len(objects) + 1):
            if math.gcd(stride, len(objects)) != 1:
                continue
            for start in range(len(objects)):
                selected = [
                    objects[(start + index * stride) % len(objects)]
                    for index in range(template.media_count)
                ]
                identity = {
                    "templateSlug": template.slug,
                    "mediaKeys": [item.key for item in selected],
                }
                canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
                signature = hashlib.sha256(canonical.encode()).hexdigest()
                if signature in seen or signature in unavailable_signatures:
                    continue
                seen.add(signature)
                candidates.append({
                    **identity,
                    "signature": signature,
                    "librarySnapshotHash": library.snapshot_hash,
                })
    if not candidates:
        return []
    seed = int(hashlib.sha256(run_id.encode()).hexdigest()[:16], 16)
    stride = max(1, seed % len(candidates)) | 1
    while len(candidates) > 1 and math.gcd(stride, len(candidates)) != 1:
        stride += 2
    planned = []
    for index in range(len(candidates)):
        candidate = candidates[(seed + index * stride) % len(candidates)]
        planned.append(candidate)
        if len(planned) == count:
            break
    return planned


def ugc_page_cut_policy(page_id: str) -> dict[str, Any] | None:
    """Return the staged server-owned fast-cut policy for one page, if any.

    The policy is config only: it describes the Culture Core cadence the
    operator intends Syzygy to render. It never binds the render wire, so an
    unknown page keeps the generic UGC clean-slideshow behavior unchanged.
    """
    if not isinstance(page_id, str) or not page_id.strip():
        return None
    try:
        executor, _ = _executor_contract(UGC_FORMAT_SLUG)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    treatment = executor.get("pageTreatments", {}).get(page_id)
    return dict(treatment) if isinstance(treatment, dict) else None


def ugc_fast_cut_cadence(
    *, cut_count: int, frames_per_cut: int, fps: int = 30,
) -> list[float]:
    """Pure cadence helper: evenly spaced cut boundaries for a fast UGC burst.

    Returns cut_count boundary timestamps (seconds) using frames_per_cut frames
    per cut at the given fps. No Syzygy wire field is derived here; the parent
    will pin the exact render payload once Syzygy's fast-cut contract lands.
    """
    if (
        not isinstance(cut_count, int) or isinstance(cut_count, bool)
        or cut_count <= 0
        or not isinstance(frames_per_cut, int) or isinstance(frames_per_cut, bool)
        or frames_per_cut <= 0
        or not isinstance(fps, int) or isinstance(fps, bool) or fps <= 0
    ):
        return []
    step = frames_per_cut / fps
    return [round(index * step, 6) for index in range(cut_count)]


def syzygy_idempotency_key(*parts: str) -> str:
    canonical = "\0".join(parts)
    return "cpl-syzygy-" + hashlib.sha256(canonical.encode()).hexdigest()


async def _request_json(
    method: str,
    endpoint: str,
    *,
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    runtime = _runtime()
    if runtime is None:
        raise SyzygyUnavailable("Syzygy slideshow runtime is unavailable")
    base, key = runtime
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.request(
                method,
                f"{base}{endpoint}",
                headers=_headers(key, idempotency_key),
                json=body,
            )
    except httpx.HTTPError as error:
        raise SyzygyUnavailable("Syzygy request failed") from error
    if response.status_code not in {200, 201}:
        message = ""
        try:
            payload = response.json()
            message = str(payload.get("error", {}).get("code") or "")
        except ValueError:
            pass
        raise SyzygyError(
            f"Syzygy returned {response.status_code}"
            + (f" {message}" if message else "")
        )
    try:
        value = response.json()
    except ValueError as error:
        raise SyzygyError("Syzygy response is not JSON") from error
    if not isinstance(value, dict):
        raise SyzygyError("Syzygy response is invalid")
    return value


async def syzygy_revision() -> str:
    value = await _request_json("GET", "/readyz", timeout=15.0)
    revision = value.get("revision")
    if not isinstance(revision, str) or not SHA256.fullmatch(revision):
        raise SyzygyError("Syzygy renderer revision is unavailable")
    return revision


async def submit_syzygy_render(
    plan: dict[str, Any], *, page_id: str, recipe_version: str,
    format_slug: str | None = None,
) -> tuple[str, str]:
    media = []
    for object_key in plan.get("mediaKeys", []):
        media.append({
            "type": "video" if "/clips/" in object_key else "photo",
            "library": object_key,
        })
    spec = {
        "format": "photo",
        "aspect": "9:16",
        "template_slug": plan["templateSlug"],
        "media": media,
        "audio": {"section_start": 0, "section_end": 15},
    }
    if format_slug == UGC_FORMAT_SLUG:
        # The neutral UGC background must carry no baked copy or source audio.
        # Syzygy's builtin template style fills absent keys, so these explicit
        # blank values suppress hook/lyric/caption text and original audio.
        spec.update({
            "hook": {"text": "", "position": "none"},
            "lyrics": [],
            "captions": {"position": "none"},
            "textCards": [],
            "text_cards": [],
            "lyrics_lrc": "",
            "lyrics_required": False,
            "uses_original_audio": False,
        })
    key = syzygy_idempotency_key(
        page_id, recipe_version, plan["signature"], "render-v1",
    )
    value = await _request_json(
        "POST", "/api/v1/render", body={"spec": spec},
        idempotency_key=key, timeout=90.0,
    )
    job_id = value.get("job_id")
    quality = value.get("quality")
    if not isinstance(job_id, str) or not job_id or not isinstance(quality, str):
        raise SyzygyError("Syzygy render receipt is invalid")
    return job_id, quality


async def poll_syzygy_render(job_id: str) -> str:
    deadline = asyncio.get_running_loop().time() + MAX_RENDER_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        value = await _request_json(
            "GET", f"/api/v1/render/{quote(job_id)}", timeout=30.0,
        )
        status = value.get("status")
        if status == "done":
            result = value.get("result")
            play_url = result.get("play_url") if isinstance(result, dict) else None
            if (
                not isinstance(play_url, str)
                or not play_url.startswith("/render-file/")
                or "?" in play_url
                or "#" in play_url
            ):
                raise SyzygyError("Syzygy completed without a valid artifact URL")
            return play_url
        if isinstance(status, str) and status in TERMINAL_FAILURES:
            raise SyzygyError(f"Syzygy render {status}")
        await asyncio.sleep(POLL_SECONDS)
    raise SyzygyUnavailable("Syzygy render timed out")


async def download_syzygy_artifact(play_url: str, destination: Path) -> None:
    runtime = _runtime()
    if runtime is None:
        raise SyzygyUnavailable("Syzygy slideshow runtime is unavailable")
    base, key = runtime
    absolute = urljoin(base + "/", play_url.lstrip("/"))
    if urlsplit(absolute).netloc != urlsplit(base).netloc:
        raise SyzygyError("Syzygy artifact origin is invalid")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=False) as client:
            async with client.stream("GET", absolute, headers=_headers(key)) as response:
                if response.status_code != 200:
                    raise SyzygyError(
                        f"Syzygy artifact returned {response.status_code}"
                    )
                with destination.open("xb") as handle:
                    async for chunk in response.aiter_bytes():
                        handle.write(chunk)
    except httpx.HTTPError as error:
        destination.unlink(missing_ok=True)
        raise SyzygyUnavailable("Syzygy artifact download failed") from error
    if not destination.is_file() or destination.stat().st_size <= 0:
        destination.unlink(missing_ok=True)
        raise SyzygyError("Syzygy artifact is empty")


async def observe_library_media(
    library: SyzygyLibrary, keys: list[str],
) -> list[dict[str, Any]]:
    by_key = {item.key: item for item in library.objects}
    observations = []
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
        for object_key in keys:
            item = by_key.get(object_key)
            if item is None:
                raise SyzygyError("reserved Syzygy library object disappeared")
            digest = hashlib.sha256()
            byte_count = 0
            try:
                async with client.stream("GET", item.url) as response:
                    if response.status_code != 200:
                        raise SyzygyError(
                            f"Syzygy library object returned {response.status_code}"
                        )
                    etag = response.headers.get("etag")
                    async for chunk in response.aiter_bytes():
                        digest.update(chunk)
                        byte_count += len(chunk)
            except httpx.HTTPError as error:
                raise SyzygyUnavailable("Syzygy library object read failed") from error
            if byte_count != item.bytes:
                raise SyzygyError("Syzygy library object size changed")
            observations.append({
                "objectKey": object_key,
                "bytes": byte_count,
                "etag": etag if isinstance(etag, str) and etag else None,
                "sha256": digest.hexdigest(),
            })
    return observations
