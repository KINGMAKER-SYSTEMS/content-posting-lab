"""Hash-bound Syzygy/R2 slideshow planning and execution helpers.

The Dossier selects a stable shared-library address. R2 inventory may grow
behind that address without changing the page recipe; every job snapshots the
exact ordered keys it reserved, and every finished artifact records the bytes
and ETag it actually observed. Content Lab remains the job and page-authority
boundary while Syzygy remains the renderer.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
import functools
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlsplit

import httpx

from services.content_engine_registry import MaterialProfile, resolve_material_profile
from services.control_plane_image_video import IMAGE_VIDEO_FORMATS, image_video_plan_for_format
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
LYRIC_VIDEO_FORMAT_SLUG = "lyric-video"
CLEAN_VIDEO_FORMATS = frozenset({UGC_FORMAT_SLUG, LYRIC_VIDEO_FORMAT_SLUG})
LYRIC_VIDEO_EXECUTOR_PATH = Path(__file__).resolve().parents[1] / "recipes/executors/syzygy-slideshow-lyric-video.v1.json"
# Lab plan identity, not an applied Syzygy builtin template.
CURATED_VIDEO_PLAN_SLUG = "curated-video-montage"
UGC_EXECUTOR_PATH = (
    Path(__file__).resolve().parents[1]
    / "recipes/executors/syzygy-slideshow-ugc.v1.json"
)
EXECUTOR_SCHEMA = "content-lab.syzygy-slideshow-executor.v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# The Syzygy renderer reports its deployed revision on /readyz. It may be a
# full 64-hex SHA-256 or a 40-hex git object id; both are accepted as an opaque
# renderer revision. The strict 64-hex ``SHA256`` pattern above remains the only
# validator for plan signatures and source hashes, which must always be full
# SHA-256 digests.
RENDERER_REVISION = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
LIBRARY_ID = re.compile(r"^syzygy:[a-z0-9][a-z0-9-]{0,63}:[a-z0-9][a-z0-9-]{0,99}$")
# The curated video library lane. Generic UGC photo pages resolve the format's
# ``ugc`` lane; a page with a closed curatedVideoPlan resolves this canonical
# clips lane so its library keys are
# ``slideshow-webweaving/<subject>/clips/<file>``.
CURATED_VIDEO_LANE = "slideshow-webweaving"
SOURCE_FILENAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,199}\.[a-z0-9]{1,10}$")
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


def _parse_curated_video_plan(value: Any) -> dict[str, Any]:
    """Validate and normalize one closed curatedVideoPlan page treatment.

    The tracked executor carries ordered source clip windows
    with pinned SHA-256/bytes/duration. Anything malformed, nonfinite, negative,
    overrunning its source duration or non-contiguous fails closed. The executor
    version and stored plan signature bind the approved order.
    """
    if not isinstance(value, dict) or set(value) != {
        "lane", "subject", "audioSectionEnd", "muted", "speed", "cropMode", "sources", "windows",
    }:
        raise ValueError("curatedVideoPlan fields are invalid")
    lane = value.get("lane")
    if (
        not isinstance(lane, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", lane)
    ):
        raise ValueError("curatedVideoPlan lane is invalid")
    subject = value.get("subject")
    if not isinstance(subject, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", subject):
        raise ValueError("curatedVideoPlan subject is invalid")
    audio_end = value.get("audioSectionEnd")
    if (
        isinstance(audio_end, bool)
        or not isinstance(audio_end, (int, float))
        or not math.isfinite(audio_end)
        or not 0 < audio_end <= 60
    ):
        raise ValueError("curatedVideoPlan audioSectionEnd is invalid")
    if value.get("muted") is not True:
        raise ValueError("curatedVideoPlan muted must be true")
    speed = value.get("speed")
    if (
        isinstance(speed, bool)
        or not isinstance(speed, (int, float))
        or not math.isfinite(speed)
        or speed != 1
    ):
        raise ValueError("curatedVideoPlan speed is invalid")
    if value.get("cropMode") != "cover":
        raise ValueError("curatedVideoPlan cropMode is invalid")

    sources = value.get("sources")
    if not isinstance(sources, list) or not sources or len(sources) > 1_000:
        raise ValueError("curatedVideoPlan sources are invalid")
    by_file: dict[str, dict[str, Any]] = {}
    for source in sources:
        if (
            not isinstance(source, dict)
            or set(source) != {"file", "sha256", "bytes", "duration"}
            or not isinstance(source.get("file"), str)
            or not SOURCE_FILENAME.fullmatch(source["file"])
            or not isinstance(source.get("sha256"), str)
            or not SHA256.fullmatch(source["sha256"])
            or not isinstance(source.get("bytes"), int)
            or isinstance(source.get("bytes"), bool)
            or source["bytes"] <= 0
            or isinstance(source.get("duration"), bool)
            or not isinstance(source.get("duration"), (int, float))
            or not math.isfinite(source["duration"])
            or source["duration"] <= 0
        ):
            raise ValueError("curatedVideoPlan source is invalid")
        if source["file"] in by_file:
            raise ValueError("curatedVideoPlan sources are not unique")
        by_file[source["file"]] = source

    windows = value.get("windows")
    if not isinstance(windows, list) or not windows or len(windows) > 1_000:
        raise ValueError("curatedVideoPlan windows are invalid")
    previous_end = 0.0
    for index, window in enumerate(windows):
        if (
            not isinstance(window, dict)
            or set(window) != {
                "source", "sourceStart", "sourceEnd", "outputStart", "outputEnd", "focalX",
            }
            or not isinstance(window.get("source"), str)
            or window["source"] not in by_file
        ):
            raise ValueError("curatedVideoPlan window is invalid")
        source = by_file[window["source"]]
        bounds = (
            window.get("sourceStart"), window.get("sourceEnd"),
            window.get("outputStart"), window.get("outputEnd"), window.get("focalX"),
        )
        if any(
            isinstance(num, bool) or not isinstance(num, (int, float)) or not math.isfinite(num)
            for num in bounds
        ):
            raise ValueError("curatedVideoPlan window bound is invalid")
        source_start, source_end, output_start, output_end, focal_x = bounds
        if (
            source_start < 0
            or source_end <= source_start
            or source_end > source["duration"] + 1e-9
            or output_start < 0
            or output_end <= output_start
            or output_end > audio_end + 1e-9
            or not 0 <= focal_x <= 1
            or abs((source_end - source_start) - (output_end - output_start)) > 1e-9
        ):
            raise ValueError("curatedVideoPlan window bounds are invalid")
        if index == 0:
            if output_start != 0:
                raise ValueError("curatedVideoPlan windows do not start at zero")
        elif abs(output_start - previous_end) > 1e-9:
            raise ValueError("curatedVideoPlan windows are not contiguous")
        previous_end = output_end
    if abs(previous_end - audio_end) > 1e-9:
        raise ValueError("curatedVideoPlan windows do not cover the audio section")
    return {
        "lane": lane,
        "subject": subject,
        "audioSectionEnd": audio_end,
        "muted": value["muted"],
        "speed": speed,
        "cropMode": value["cropMode"],
        "sources": [dict(item) for item in sources],
        "windows": [dict(item) for item in windows],
    }


def curated_plan_signature(plan: dict[str, Any]) -> str:
    """SHA-256 identity of a curated video plan.

    Binds the ordered unique media keys, the full ordered window list (source
    key, bounds, crop focus, pinned SHA/bytes/duration), and the render
    constants, so a stored job plan can be replayed exactly without rereading
    executor config.
    """
    identity = {
        "templateSlug": plan["templateSlug"],
        "mediaKeys": list(plan["mediaKeys"]),
        "muted": plan["muted"],
        "speed": plan["speed"],
        "cropMode": plan["cropMode"],
        "audioSectionEnd": plan["audioSectionEnd"],
        "windows": [
            {
                "key": window["key"],
                "sourceStart": window["sourceStart"],
                "sourceEnd": window["sourceEnd"],
                "outputStart": window["outputStart"],
                "outputEnd": window["outputEnd"],
                "focalX": window["focalX"],
                "sourceSha256": window["sourceSha256"],
                "sourceBytes": window["sourceBytes"],
                "sourceDuration": window["sourceDuration"],
            }
            for window in plan["windows"]
        ],
    }
    # Variant 0 (the approved montage) carries no variant field, so its
    # identity, and every reservation already recorded against it, is byte for
    # byte what it was before variants existed. Variants 1..N sign their number.
    variant = plan.get("variant", 0)
    if variant != 0:
        identity["variant"] = variant
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# Curated variants. A curated page's approved montage is variant 0. Variants
# 1..N are deterministic re-chops of the same pinned sources:
#   * every output window keeps its exact outputStart/outputEnd, and the plan
#     keeps audioSectionEnd, muted, speed and cropMode, so cut rhythm and lyric
#     sync never move;
#   * the clip that fills each window rotates among the plan's own sources;
#   * the source moment shifts inside that clip (same window length, never past
#     the clip's pinned duration);
#   * the horizontal crop focus nudges a little.
# Only the plan's pinned sources are used. Extra library clips carry no pinned
# SHA-256 or duration, so a window inside them could not be proven in bounds or
# checked before the paid render. There is no mirror: the render wire has no
# flip field. Every variant is re-validated by _parse_curated_video_plan and has
# its own signature, reserved once like any other slideshow plan.
CURATED_VARIANT_LIMIT = 200
_VARIANT_FOCAL_OFFSETS = (0.0, -0.06, 0.06, -0.12, 0.12)
_VARIANT_TAIL_MARGIN = 0.75  # Tides HIGH-1: stay clear of end-of-clip fades (ytal-v-0016 fades to black from 3.80 s)
_VARIANT_STEP = 0.6180339887498949
_VARIANT_WINDOW_STEP = 0.4142135623730951


def _curated_variant_windows(
    curated: dict[str, Any], variant: int,
) -> list[dict[str, Any]] | None:
    """Source-file windows for one variant, or None if a window cannot fit."""
    if variant == 0:
        return [dict(window) for window in curated["windows"]]
    by_file = {source["file"]: source for source in curated["sources"]}
    order: list[str] = []
    first_focal: dict[str, float] = {}
    for window in curated["windows"]:
        if window["source"] not in first_focal:
            order.append(window["source"])
            first_focal[window["source"]] = window["focalX"]
    turn = variant % len(order)
    rotate = {name: order[(index + turn) % len(order)] for index, name in enumerate(order)}
    windows = []
    for index, window in enumerate(curated["windows"]):
        source = rotate[window["source"]]
        length = window["outputEnd"] - window["outputStart"]
        span = by_file[source]["duration"] - length
        if span < 0:
            return None
        span -= min(_VARIANT_TAIL_MARGIN, span)
        fraction = math.modf(variant * _VARIANT_STEP + index * _VARIANT_WINDOW_STEP)[0]
        start = math.floor(fraction * span * 1000) / 1000
        base_focal = window["focalX"] if source == window["source"] else first_focal[source]
        offset = _VARIANT_FOCAL_OFFSETS[(variant + index) % len(_VARIANT_FOCAL_OFFSETS)]
        windows.append({
            "source": source,
            "sourceStart": start,
            "sourceEnd": start + length,
            "outputStart": window["outputStart"],
            "outputEnd": window["outputEnd"],
            "focalX": round(min(1.0, max(0.0, base_focal + offset)), 3),
        })
    return windows


@functools.lru_cache(maxsize=16)
def _curated_variant_table(curated_json: str) -> tuple[dict[str, Any] | None, ...]:
    curated = json.loads(curated_json)
    prefix = f'{curated["lane"]}/{curated["subject"]}/clips/'
    # mediaKeys stay the approved plan's unique ordered source keys for every
    # variant: rotation only re-assigns the same pinned sources to windows.
    media_keys: list[str] = []
    for window in curated["windows"]:
        if prefix + window["source"] not in media_keys:
            media_keys.append(prefix + window["source"])
    table: list[dict[str, Any] | None] = []
    seen: set[str] = set()
    for variant in range(CURATED_VARIANT_LIMIT):
        windows = _curated_variant_windows(curated, variant)
        plan = None
        if windows is not None:
            try:
                checked = _parse_curated_video_plan({**curated, "windows": windows})
            except ValueError:
                checked = None
            fingerprint = json.dumps(windows, sort_keys=True, separators=(",", ":"))
            if checked is not None and fingerprint not in seen:
                seen.add(fingerprint)
                by_file = {source["file"]: source for source in checked["sources"]}
                plan = {
                    "templateSlug": CURATED_VIDEO_PLAN_SLUG,
                    "mediaKeys": list(media_keys),
                    "muted": checked["muted"],
                    "speed": checked["speed"],
                    "cropMode": checked["cropMode"],
                    "audioSectionEnd": checked["audioSectionEnd"],
                    "windows": [
                        {
                            "key": prefix + window["source"],
                            "sourceStart": window["sourceStart"],
                            "sourceEnd": window["sourceEnd"],
                            "outputStart": window["outputStart"],
                            "outputEnd": window["outputEnd"],
                            "focalX": window["focalX"],
                            "sourceSha256": by_file[window["source"]]["sha256"],
                            "sourceBytes": by_file[window["source"]]["bytes"],
                            "sourceDuration": by_file[window["source"]]["duration"],
                        }
                        for window in checked["windows"]
                    ],
                }
                if variant:
                    plan["variant"] = variant
                plan["signature"] = curated_plan_signature(plan)
        table.append(plan)
    return tuple(table)


def curated_variant_plans(curated: dict[str, Any]) -> list[dict[str, Any] | None]:
    """Every variant plan of one parsed curated plan, indexed by variant number.

    A None entry is a variant that cannot fit its sources or would repeat an
    earlier variant's windows; it is never offered. Plans carry their
    signature but no library snapshot.
    """
    table = _curated_variant_table(json.dumps(curated, sort_keys=True))
    return [copy.deepcopy(plan) for plan in table]


def curated_variant_plan(curated: dict[str, Any], variant: int) -> dict[str, Any] | None:
    """One variant plan (with signature), or None when it is not offered."""
    if isinstance(variant, bool) or not isinstance(variant, int):
        return None
    table = _curated_variant_table(json.dumps(curated, sort_keys=True))
    if not 0 <= variant < len(table) or table[variant] is None:
        return None
    return copy.deepcopy(table[variant])


def curated_variant_number(plan: Any) -> int | None:
    """The variant a stored/wire plan claims: 0 when absent, None if malformed.

    The approved plan is only ever spelled without a variant field, so an
    explicit ``"variant": 0`` is refused rather than given a second spelling.
    """
    if not isinstance(plan, dict):
        return None
    if "variant" not in plan:
        return 0
    variant = plan["variant"]
    if (isinstance(variant, bool) or not isinstance(variant, int)
            or not 1 <= variant < CURATED_VARIANT_LIMIT):
        return None
    return variant


def _executor_contract(format_slug: str | None = None) -> tuple[dict[str, Any], str]:
    if format_slug in IMAGE_VIDEO_FORMATS:
        from services.control_plane_image_video import slideshow_executor_adapter
        return slideshow_executor_adapter(format_slug)
    path = (LYRIC_VIDEO_EXECUTOR_PATH if format_slug == LYRIC_VIDEO_FORMAT_SLUG else
            UGC_EXECUTOR_PATH if format_slug == UGC_FORMAT_SLUG else EXECUTOR_PATH)
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
    # pageTreatments is optional server-owned config: a page_id -> closed policy
    # that drives the render wire for that exact page only. It is not a protocol;
    # unknown pages keep the generic UGC clean-slideshow behavior. A treatment is
    # EITHER the legacy cut_style trio OR a closed curatedVideoPlan object (not
    # both); anything else fails closed.
    treatments = value.get("pageTreatments", {})
    if not isinstance(treatments, dict):
        raise ValueError("Syzygy slideshow executor pageTreatments is invalid")
    for page_id, treatment in treatments.items():
        if (
            not isinstance(page_id, str)
            or not page_id.strip()
            or not isinstance(treatment, dict)
        ):
            raise ValueError("Syzygy slideshow executor pageTreatment is invalid")
        if set(treatment) == {"cutMode", "cuts", "audioSectionEnd"}:
            if (
                treatment.get("cutMode") != "cut_style"
                or not isinstance(treatment.get("cuts"), int)
                or not 1 <= treatment["cuts"] <= 240
                or not isinstance(treatment.get("audioSectionEnd"), (int, float))
                or not 0 < treatment["audioSectionEnd"] <= 60
            ):
                raise ValueError("Syzygy slideshow executor pageTreatment is invalid")
        elif set(treatment) == {"curatedVideoPlan"}:
            _parse_curated_video_plan(treatment["curatedVideoPlan"])
        else:
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


def ugc_page_treatment(page_id: str, format_slug: str = UGC_FORMAT_SLUG) -> dict[str, Any] | None:
    """Return the closed server-owned treatment for one UGC page, if any.

    Returns either a legacy ``{"cutMode","cuts","audioSectionEnd"}`` policy or a
    full ``{"curatedVideoPlan": ...}`` (with ``sourceKeys``/``windows`` already
    normalized). Unknown pages return None and keep generic UGC behavior.
    """
    if not isinstance(page_id, str) or not page_id.strip():
        return None
    try:
        executor, _ = _executor_contract(format_slug)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    treatment = executor.get("pageTreatments", {}).get(page_id)
    if not isinstance(treatment, dict):
        return None
    if "curatedVideoPlan" in treatment and set(treatment) == {"curatedVideoPlan"}:
        plan = _parse_curated_video_plan(treatment["curatedVideoPlan"])
        return {"curatedVideoPlan": plan}
    if set(treatment) == {"cutMode", "cuts", "audioSectionEnd"}:
        return dict(treatment)
    return None


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
    if profile.format_slug in IMAGE_VIDEO_FORMATS:
        photo = image_video_plan_for_format(profile.format_slug, master_pages)
        if photo is None or subject != photo["subject"]:
            return None
        return photo["libraryId"], photo["lane"], photo["subject"]
    lane = config["lane"]
    # A page with a curated video plan resolves its canonical clips lane so the
    # library keys are slideshow-webweaving/<subject>/clips/<file>. Generic
    # pages keep the format's photo lane.
    if profile.format_slug in CLEAN_VIDEO_FORMATS:
        page_treatment = ugc_page_treatment(master_pages.get("pageId"), profile.format_slug)
        curated = (
            page_treatment.get("curatedVideoPlan")
            if isinstance(page_treatment, dict) else None
        )
        if isinstance(curated, dict):
            if subject != curated["subject"]:
                return None
            lane = curated["lane"]
    if profile.format_slug == LYRIC_VIDEO_FORMAT_SLUG:
        from services.lyric_timing import lyric_timing_catalog
        if (not isinstance(curated, dict)
                or not lyric_timing_catalog(
                    page_id=master_pages.get("pageId"),
                    account=master_pages.get("handle"))):
            return None
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


def syzygy_runtime_available() -> bool:
    """The Syzygy slideshow runtime is configured and usable."""
    return _runtime() is not None


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
        or engine not in {"sourced_slideshow", "lyrics_slideshows", "sourced_image"}
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
    if profile.format_slug == LYRIC_VIDEO_FORMAT_SLUG:
        from services.control_plane_lyric_video import validate_lyric_recipe
        try:
            validate_lyric_recipe(spec)
        except ValueError:
            return None
    if profile.format_slug in IMAGE_VIDEO_FORMATS:
        from services.control_plane_image_video import required_image_video_treatment
        if not required_image_video_treatment(spec, master_pages.get("pageId")):
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


def _curated_library_ready(
    recipe: SlideshowRecipe, library: SyzygyLibrary, curated: dict[str, Any],
) -> bool:
    """The recipe, live library and plan agree, and every source holds its pinned bytes.

    Every variant uses exactly the approved plan's sources, so this one check
    covers them all. Extras in the library are never selected.
    """
    if (recipe.lane != curated["lane"] or recipe.subject != curated["subject"]
            or library.lane != recipe.lane or library.subject != recipe.subject
            or library.library_id != recipe.library_id):
        return False
    library_bytes = {item.key: item.bytes for item in library.objects}
    return all(
        library_bytes.get(f"{recipe.lane}/{recipe.subject}/clips/{source['file']}")
        == source["bytes"]
        for source in curated["sources"]
        if any(window["source"] == source["file"] for window in curated["windows"])
    )


def _build_curated_plan(
    recipe: SlideshowRecipe, library: SyzygyLibrary, curated: dict[str, Any],
    variant: int = 0,
) -> dict[str, Any] | None:
    """Build one closed curated video plan (variant 0 = the approved montage).

    ``mediaKeys`` are the unique ordered source keys (one per source file) for
    source observation and the Worker receipt; ``windows`` is the full ordered
    window expansion that drives the render wire and is bound into the plan
    signature. Every required key must exist in the live library with its
    pinned bytes.
    """
    if not _curated_library_ready(recipe, library, curated):
        return None
    plan = curated_variant_plan(curated, variant)
    if plan is None:
        return None
    return {**plan, "librarySnapshotHash": library.snapshot_hash}


def requires_curated_video_plan(recipe: SlideshowRecipe) -> bool:
    """Derive the execution mode from server-owned recipe authority, not job shape."""
    if recipe.format_slug not in CLEAN_VIDEO_FORMATS:
        return False
    page_id = recipe.recipe_spec.get("masterPages", {}).get("pageId")
    treatment = ugc_page_treatment(page_id, recipe.format_slug) if isinstance(page_id, str) else None
    return isinstance(treatment, dict) and "curatedVideoPlan" in treatment


def _recipe_curated_plan(recipe: SlideshowRecipe) -> dict[str, Any] | None:
    page_id = recipe.recipe_spec.get("masterPages", {}).get("pageId")
    treatment = ugc_page_treatment(page_id, recipe.format_slug) if isinstance(page_id, str) else None
    curated = treatment.get("curatedVideoPlan") if isinstance(treatment, dict) else None
    return curated if isinstance(curated, dict) else None


def curated_plan_for_recipe(
    recipe: SlideshowRecipe, library: SyzygyLibrary, variant: int = 0,
) -> dict[str, Any] | None:
    """One curated video plan variant for a recipe, or None if not curated/offered.

    Re-derived from the pinned executor config for the recipe's page; the recipe
    executor_version already binds the executor bytes, so this is stable across
    a job's lifecycle unless the job itself was tampered with.
    """
    curated = _recipe_curated_plan(recipe)
    if curated is None:
        return None
    return _build_curated_plan(recipe, library, curated, variant)


def curated_plans_for_recipe(
    recipe: SlideshowRecipe, library: SyzygyLibrary,
) -> list[dict[str, Any]] | None:
    """Every offered curated variant for a recipe in variant order.

    None when the page is not curated; [] when its live library does not hold
    the pinned sources.
    """
    curated = _recipe_curated_plan(recipe)
    if curated is None:
        return None
    if not _curated_library_ready(recipe, library, curated):
        return []
    return [
        {**plan, "librarySnapshotHash": library.snapshot_hash}
        for plan in curated_variant_plans(curated)
        if plan is not None
    ]


def _generic_clean_video_chunk(
    template: SyzygyTemplate, objects_in_order: list[SyzygyObject], chunk: int,
) -> dict[str, Any] | None:
    """One media chunk for the generic clean-video path, or None.

    Chunk 0 is ``objects[:mediaCount]`` exactly as before (same signature);
    later chunks are the next ``objects[k*n:(k+1)*n]``. The signature is
    content-only (media keys + template slug), so adding or removing a photo
    never re-offers a posted chunk (#266's HIGH-1 lesson: never bind an index).
    """
    media_count = template.media_count
    start = chunk * media_count
    end = start + media_count
    if end > len(objects_in_order):
        return None
    selected = objects_in_order[start:end]
    identity = {
        "templateSlug": template.slug,
        "mediaKeys": [item.key for item in selected],
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return {
        **identity,
        "signature": hashlib.sha256(canonical.encode()).hexdigest(),
    }


def _permutation_plan_candidates(
    recipe: SlideshowRecipe, library: SyzygyLibrary, unavailable_signatures: set[str],
) -> list[dict[str, Any]]:
    """Every unreserved stride/start permutation plan, in deterministic order.

    Extracted from plan_slideshows so explain_empty_slideshow_plan can re-derive
    exhaustion without duplicating the signature logic. The produced plan shapes
    and signatures are byte-identical to the inline version that preceded it.
    """
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
    return candidates


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
    if recipe.format_slug in IMAGE_VIDEO_FORMATS:
        from services.control_plane_image_video import recipe_image_video_plans
        plans = recipe_image_video_plans(recipe, library)
        if not plans:
            return []
        # Variant 0 is the approved photo hold; each later variant re-draws the
        # page's own library photos and is reserved once through its own
        # signature. Extras in the library are ignored.
        return [
            plan for plan in plans
            if plan["signature"] not in unavailable_signatures
        ][:count]
    # The UGC format is a curated page library. A page with a curatedVideoPlan
    # renders its approved montage (variant 0) and then its deterministic
    # re-chop variants 1..N, each exactly once, in variant order; a generic
    # page renders the first mediaCount photo objects in ordinal order (no
    # offset/stride permutation) as chunk 0, then the next unreserved chunk
    # objects[k*n:(k+1)*n] when chunk 0 is reserved, so fresh media keeps
    # producing new plans instead of an exhausted generic set staying exhausted.
    if recipe.format_slug in CLEAN_VIDEO_FORMATS:
        has_curated_plan = requires_curated_video_plan(recipe)
        variants = curated_plans_for_recipe(recipe, library)
        if has_curated_plan and not variants:
            return []
        if variants:
            # Each variant is reserved once through its own signature.
            # Extras in the library are ignored.
            return [
                plan for plan in variants
                if plan["signature"] not in unavailable_signatures
            ][:count]
        template = recipe.templates[0]
        objects_in_order = list(library.objects)
        for chunk in range(len(objects_in_order) + 1):
            plan = _generic_clean_video_chunk(template, objects_in_order, chunk)
            if plan is None:
                return []
            if plan["signature"] in unavailable_signatures:
                continue
            return [{
                **plan,
                "librarySnapshotHash": library.snapshot_hash,
            }]
        return []
    candidates = _permutation_plan_candidates(recipe, library, unavailable_signatures)
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
    """Return the staged server-owned legacy fast-cut policy for one page, if any.

    Only the ``{"cutMode","cuts","audioSectionEnd"}`` treatment is a cut policy;
    a curatedVideoPlan page is handled by the curated wire and returns None here.
    An unknown page returns None and keeps generic UGC behavior.
    """
    treatment = ugc_page_treatment(page_id)
    if isinstance(treatment, dict) and set(treatment) == {"cutMode", "cuts", "audioSectionEnd"}:
        return dict(treatment)
    return None


def ugc_encode_args(preset: str) -> list[str]:
    """Delivery encode args for the UGC neutral background.

    The same closed delivery preset as the legacy lyric/meme slideshow, plus
    ``-an`` to drop any renderer-shipped audio track. Legacy callers keep the
    preset byte-for-byte unchanged; this is for clean managed video backgrounds.
    """
    from services.ffmpeg import delivery_encode_args

    return [*delivery_encode_args(preset), "-an"]


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
    if not isinstance(revision, str) or not RENDERER_REVISION.fullmatch(revision):
        raise SyzygyError("Syzygy renderer revision is unavailable")
    return revision


async def submit_syzygy_render(
    plan: dict[str, Any], *, page_id: str, recipe_version: str,
    format_slug: str | None = None,
) -> tuple[str, str]:
    if format_slug in IMAGE_VIDEO_FORMATS:
        from services.control_plane_image_video import submit_image_video_render
        return await submit_image_video_render(plan.get("photoPlan"), page_id=page_id,
            recipe_version=recipe_version, request_json=_request_json, format_slug=format_slug)
    if format_slug == LYRIC_VIDEO_FORMAT_SLUG:
        from services.control_plane_lyric_video import validate_lyric_wire_plan
        validate_lyric_wire_plan(plan, page_id)
    if format_slug in CLEAN_VIDEO_FORMATS and isinstance(plan.get("windows"), list):
        # Curated video montage: the renderer selects on spec.format (video),
        # NOT on kind, and receives the full ordered window expansion. No
        # template_slug (its builtin template style would fill absent cuts from
        # the clean-slideshow seed), no cuts (the flow-stage windows override
        # explicit durations/order), and no audio section.
        media = []
        for window in plan["windows"]:
            media.append({
                "type": "video",
                "library": window["key"],
                "duration": round(window["outputEnd"] - window["outputStart"], 6),
                "clip_start": window["sourceStart"],
                "clip_end": window["sourceEnd"],
                "muted": True,
                "speed": 1,
                "crop_mode": "cover",
                "focal_x": window["focalX"],
                "focal_y": 0.5,
                "transition": {"type": "cut", "duration": 0},
            })
        spec = {
            "format": "video",
            "aspect": "9:16",
            "hook": {"text": "", "position": "none"},
            "lyrics": [],
            "captions": {"position": "none"},
            "textCards": [],
            "text_cards": [],
            "lyrics_lrc": "",
            "lyrics_required": False,
            "uses_original_audio": False,
            "media": media,
        }
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

    if format_slug == LYRIC_VIDEO_FORMAT_SLUG:
        raise SyzygyError("lyric video requires the registered curated plan")
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
        policy = ugc_page_cut_policy(page_id)
        if policy is not None:
            spec["cuts"] = {"mode": policy["cutMode"], "cuts": policy["cuts"]}
            spec["audio"]["section_end"] = policy["audioSectionEnd"]
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


_REDIRECT_STATUSES = frozenset({302, 303, 307, 308})


def _signed_redirect_target(base: str, name: str, location: str | None) -> str:
    """Validate a Syzygy render-file redirect mint and return the URL to GET.

    The signed URL is a bearer credential: the API key must never travel on it.
    Only a same-origin redirect back to the identical ``/render-file/<name>``
    path carrying exactly one non-empty ``token`` query value is accepted;
    everything else (cross-origin, renamed file, extra query keys, a fragment, a
    missing/blank token) fails closed so the key is never leaked to an arbitrary
    target. Error messages never include the token or the raw Location.
    """
    if not location:
        raise SyzygyError("Syzygy artifact redirect rejected: missing Location header")
    target = urljoin(base + "/", location)
    parsed = urlsplit(target)
    base_parsed = urlsplit(base)
    if parsed.scheme != base_parsed.scheme or parsed.netloc != base_parsed.netloc:
        raise SyzygyError("Syzygy artifact redirect rejected: cross-origin target")
    if parsed.fragment:
        raise SyzygyError("Syzygy artifact redirect rejected: fragment in target")
    if unquote(parsed.path) != f"/render-file/{name}":
        raise SyzygyError("Syzygy artifact redirect rejected: target is a different render file")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if len(query) != 1 or query[0][0] != "token" or not query[0][1]:
        raise SyzygyError("Syzygy artifact redirect rejected: invalid token query")
    return target


async def _write_response_bytes(response: httpx.Response, destination: Path) -> None:
    with destination.open("xb") as handle:
        async for chunk in response.aiter_bytes():
            handle.write(chunk)


async def download_syzygy_artifact(play_url: str, destination: Path) -> None:
    runtime = _runtime()
    if runtime is None:
        raise SyzygyUnavailable("Syzygy slideshow runtime is unavailable")
    base, key = runtime
    absolute = urljoin(base + "/", play_url.lstrip("/"))
    if urlsplit(absolute).netloc != urlsplit(base).netloc:
        raise SyzygyError("Syzygy artifact origin is invalid")
    name = play_url.removeprefix("/render-file/")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=False) as client:
            async with client.stream("GET", absolute, headers=_headers(key)) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    target = _signed_redirect_target(
                        base, name, response.headers.get("location"),
                    )
                    # The signed URL is the credential; never send the API key.
                    # A transport error here must not retain the token-bearing
                    # URL as __cause__, so it is raised from None.
                    try:
                        async with client.stream("GET", target) as signed:
                            if signed.status_code in _REDIRECT_STATUSES:
                                raise SyzygyError(
                                    "Syzygy artifact redirect rejected: signed URL redirected again"
                                )
                            if signed.status_code != 200:
                                raise SyzygyError(
                                    f"Syzygy artifact returned {signed.status_code}"
                                )
                            await _write_response_bytes(signed, destination)
                    except httpx.HTTPError:
                        destination.unlink(missing_ok=True)
                        raise SyzygyUnavailable("Syzygy artifact download failed") from None
                elif response.status_code == 200:
                    await _write_response_bytes(response, destination)
                else:
                    raise SyzygyError(
                        f"Syzygy artifact returned {response.status_code}"
                    )
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
