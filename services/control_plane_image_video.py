"""Closed page-owned photo-to-MP4 preparation for Syzygy image-video pages.

This helper owns the deterministic page-bound planning and render-wire for a
small set of explicitly configured still-image pages (PenPal kittens and Twin apartment).
It does **not** grade pixels: the ``brightnessGainFromOriginals`` value is only
carried as provenance for the *later* source-treatment rendering boundary, and
no render receipt here may claim that gain has been applied.

The executor file is a closed page -> ordered photo plan. Every configured
object carries its exact original source key plus its runtime canonical
dedicated key, SHA-256 and byte count. Planning is deterministic and never
permuted; the approved plan is variant 0 and later variants re-draw the page's
own library photos. This module is a pure helper consumable by the existing
SlideshowRecipe dispatch, which remains owned elsewhere.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

EXECUTOR_PATH = (
    Path(__file__).resolve().parents[1]
    / "recipes/executors/syzygy-image-video.v1.json"
)
EXECUTOR_SCHEMA = "content-lab.syzygy-image-video-executor.v1"
EXECUTOR_ID = "syzygy-slideshow"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Dedicated photo object key: lane/subject/photos/<file>.<ext>.
OBJECT_KEY = re.compile(
    r"^[a-z0-9][a-z0-9-]{0,63}/[a-z0-9][a-z0-9-]{0,99}/"
    r"photos/[a-z0-9][a-z0-9_-]{0,199}\.[a-z0-9]{1,10}$"
)
LANE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SUBJECT = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
MAX_OBJECTS = 1_000
PLAN_SLUG = "page-photo-hold"
# Deterministic re-draw variants of the approved photo hold. Variant 0 is the
# approved plan; variants 1..N rotate the same slot timings/framing across the
# page's own library photos in a deterministic order, each distinct content
# window once. The cap is a sane bound well above any page's photo count
# (pen-pal3: 33, twin47996: 24).
IMAGE_VIDEO_VARIANT_LIMIT = 200


class ImageVideoError(RuntimeError):
    """A deterministic contract, validation, or render failure."""


class ImageVideoUnavailable(ImageVideoError):
    """A missing or transient Syzygy/R2 boundary."""


def _finite_number(value: Any, *, minimum: float | None = None) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not math.isfinite(value):
        return False
    if minimum is not None and value < minimum:
        return False
    return True


def _parse_ken_burns(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"dir", "zoom"}:
        raise ValueError("kenBurns is invalid")
    direction = value.get("dir")
    zoom = value.get("zoom")
    if direction not in {"in", "out", "none"}:
        raise ValueError("kenBurns dir is invalid")
    if not _finite_number(zoom, minimum=0) or zoom > 0.5:
        raise ValueError("kenBurns zoom is invalid")
    if direction == "none" and zoom != 0:
        raise ValueError("kenBurns none requires zero zoom")
    if direction != "none" and zoom <= 0:
        raise ValueError("kenBurns motion requires positive zoom")
    return {"dir": direction, "zoom": float(zoom)}


def parse_plan_config(page_id: Any, cfg: Any) -> dict[str, Any]:
    """Validate and normalize one closed page -> photo plan config.

    Anything malformed, nonfinite, boolean-as-number, negative, duplicate,
    unknown-field, wrong-keyed, wrong-hashed, or duration-mismatched fails
    closed. Returns normalized config with explicit page and source identities.
    """
    if not isinstance(page_id, str) or not page_id.strip():
        raise ValueError("page id is invalid")
    if not isinstance(cfg, dict) or set(cfg) != {
        "lane", "subject", "totalDurationSeconds",
        "brightnessGainFromOriginals", "objects",
    }:
        raise ValueError("plan config fields are invalid")
    lane = cfg.get("lane")
    subject = cfg.get("subject")
    if not isinstance(lane, str) or not LANE.fullmatch(lane):
        raise ValueError("plan lane is invalid")
    if not isinstance(subject, str) or not SUBJECT.fullmatch(subject):
        raise ValueError("plan subject is invalid")
    total = cfg.get("totalDurationSeconds")
    if not _finite_number(total, minimum=1e-9) or total > 60:
        raise ValueError("totalDurationSeconds is invalid")
    gain = cfg.get("brightnessGainFromOriginals")
    if not _finite_number(gain, minimum=0) or gain > 3:
        raise ValueError("brightnessGainFromOriginals is invalid")

    objects = cfg.get("objects")
    if not isinstance(objects, list) or not objects or len(objects) > MAX_OBJECTS:
        raise ValueError("plan objects are invalid")
    expected_prefix = f"{lane}/{subject}/photos/"
    seen_keys: set[str] = set()
    seen_sources: set[str] = set()
    duration_sum = 0.0
    normalized: list[dict[str, Any]] = []
    for obj in objects:
        if (
            not isinstance(obj, dict)
            or set(obj) != {
                "sourceKey", "key", "sha256", "bytes", "duration",
                "cropMode", "focalX", "focalY", "kenBurns",
            }
        ):
            raise ValueError("plan object fields are invalid")
        source_key = obj.get("sourceKey")
        key = obj.get("key")
        if not isinstance(source_key, str) or not OBJECT_KEY.fullmatch(source_key):
            raise ValueError("plan object sourceKey is invalid")
        if not isinstance(key, str) or not OBJECT_KEY.fullmatch(key):
            raise ValueError("plan object key is invalid")
        if not key.startswith(expected_prefix):
            raise ValueError("plan object key is not in the page library")
        if key.rsplit("/", 1)[1] != source_key.rsplit("/", 1)[1]:
            raise ValueError("plan object runtime key filename differs from source")
        sha = obj.get("sha256")
        if not isinstance(sha, str) or not SHA256.fullmatch(sha):
            raise ValueError("plan object sha256 is invalid")
        bytes_ = obj.get("bytes")
        if not isinstance(bytes_, int) or isinstance(bytes_, bool) or bytes_ <= 0:
            raise ValueError("plan object bytes is invalid")
        duration = obj.get("duration")
        if not _finite_number(duration, minimum=1e-9):
            raise ValueError("plan object duration is invalid")
        if obj.get("cropMode") != "cover":
            raise ValueError("plan object cropMode is invalid")
        focal_x = obj.get("focalX")
        focal_y = obj.get("focalY")
        if (
            not _finite_number(focal_x, minimum=0)
            or focal_x > 1
            or not _finite_number(focal_y, minimum=0)
            or focal_y > 1
        ):
            raise ValueError("plan object focal is invalid")
        ken_burns = _parse_ken_burns(obj.get("kenBurns"))
        if key in seen_keys:
            raise ValueError("plan object keys are not unique")
        if source_key in seen_sources:
            raise ValueError("plan object sourceKeys are not unique")
        seen_keys.add(key)
        seen_sources.add(source_key)
        duration_sum += float(duration)
        normalized.append({
            "sourceKey": source_key,
            "key": key,
            "sha256": sha,
            "bytes": bytes_,
            "duration": float(duration),
            "cropMode": "cover",
            "focalX": float(focal_x),
            "focalY": float(focal_y),
            "kenBurns": ken_burns,
        })
    if abs(duration_sum - float(total)) > 1e-6:
        raise ValueError("plan object durations do not sum to totalDurationSeconds")
    return {
        "pageId": page_id,
        "lane": lane,
        "subject": subject,
        "totalDurationSeconds": float(total),
        "brightnessGainFromOriginals": float(gain),
        "objects": normalized,
    }


def image_video_executor_contract() -> tuple[dict[str, Any], str]:
    """Load and validate the closed executor file, returning (config, sha256)."""
    raw = EXECUTOR_PATH.read_bytes()
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value) != {
            "schema", "executorId", "source", "selection", "reservation",
            "plans", "output",
        }
        or value.get("schema") != EXECUTOR_SCHEMA
        or value.get("executorId") != EXECUTOR_ID
        or value.get("source") != "syzygy_r2_library"
        or value.get("selection") != "deterministic_single_plan_without_replacement"
        or value.get("reservation") != "active_jobs_and_completed_outputs"
        or not isinstance(value.get("plans"), dict)
        or not value["plans"]
        or value.get("output") != {
            "container": "video/mp4",
            "aspectRatio": "9:16",
            "width": 1080,
            "height": 1920,
            "encodePreset": "tiktok_delivery_v1",
            "audioPolicy": "source_audio_disabled",
        }
    ):
        raise ValueError("image-video executor contract is invalid")
    parsed: dict[str, Any] = {}
    for page_id, cfg in value["plans"].items():
        parsed[page_id] = parse_plan_config(page_id, cfg)
    return {
        "schema": value["schema"],
        "executorId": value["executorId"],
        "source": value["source"],
        "selection": value["selection"],
        "reservation": value["reservation"],
        "plans": parsed,
        "output": dict(value["output"]),
    }, hashlib.sha256(raw).hexdigest()


def _plan_config(page_id: Any) -> dict[str, Any] | None:
    if not isinstance(page_id, str) or not page_id.strip():
        return None
    try:
        executor, _ = image_video_executor_contract()
    except (OSError, ValueError, json.JSONDecodeError):
        raise ImageVideoError("image-video executor contract is unavailable")
    cfg = executor["plans"].get(page_id)
    return dict(cfg) if isinstance(cfg, dict) else None


def image_video_plan_signature(plan: dict[str, Any]) -> str:
    """SHA-256 identity binding only the page, library, and ordered media plan.

    The identity is content-addressed: two plans with the same page/library,
    ordered media set and render params share one signature regardless of any
    variant number. Variant 0 (the approved photo hold) carries no variant
    field, so its identity — and every reservation already recorded against it
    — stays byte for byte what it was before variants existed.
    """
    identity = {
        "pageId": plan["pageId"],
        "libraryId": plan["libraryId"],
        "totalDurationSeconds": plan["totalDurationSeconds"],
        "brightnessGainFromOriginals": plan["brightnessGainFromOriginals"],
        "media": [
            {
                "key": item["key"],
                "sourceKey": item["sourceKey"],
                "sha256": item["sha256"],
                "bytes": item["bytes"],
                "duration": item["duration"],
                "focalX": item["focalX"],
                "focalY": item["focalY"],
                "cropMode": item["cropMode"],
                "kenBurns": item["kenBurns"],
            }
            for item in plan["media"]
        ],
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def image_video_plan(page_id: str) -> dict[str, Any] | None:
    """The approved (variant 0) closed ordered photo plan, or None if unknown.

    Never permuted; this is the pinned photo hold whose signature predates
    variants. ``brightnessGainFromOriginals`` is provenance for the later
    source-treatment boundary, not an applied grade.
    """
    cfg = _plan_config(page_id)
    if cfg is None:
        return None
    media = [
        {
            "key": obj["key"],
            "sourceKey": obj["sourceKey"],
            "sha256": obj["sha256"],
            "bytes": obj["bytes"],
            "duration": obj["duration"],
            "focalX": obj["focalX"],
            "focalY": obj["focalY"],
            "cropMode": obj["cropMode"],
            "kenBurns": dict(obj["kenBurns"]) if obj["kenBurns"] is not None else None,
        }
        for obj in cfg["objects"]
    ]
    plan = {
        "planSlug": PLAN_SLUG,
        "pageId": cfg["pageId"],
        "libraryId": f"syzygy:{cfg['lane']}:{cfg['subject']}",
        "lane": cfg["lane"],
        "subject": cfg["subject"],
        "totalDurationSeconds": cfg["totalDurationSeconds"],
        "brightnessGainFromOriginals": cfg["brightnessGainFromOriginals"],
        "media": media,
    }
    plan["signature"] = image_video_plan_signature(plan)
    return plan


def image_video_variant_number(plan: Any) -> int | None:
    """The variant a stored/wire photo plan claims: 0 when absent, None if bad.

    The approved plan is only ever spelled without a variant field, so an
    explicit ``"variant": 0`` is refused rather than given a second spelling.
    New plans are emitted with ``variantIndex``; the legacy ``variant`` spelling
    is accepted here on read only, so jobs created before the rename (the two
    in-flight pen/twin jobs) still re-derive, and never emitted again.
    """
    if not isinstance(plan, dict):
        return None
    if "variantIndex" in plan and "variant" in plan:
        return None
    if "variantIndex" in plan:
        variant = plan["variantIndex"]
    elif "variant" in plan:
        variant = plan["variant"]
    else:
        return 0
    if (
        isinstance(variant, bool)
        or not isinstance(variant, int)
        or not 1 <= variant < IMAGE_VIDEO_VARIANT_LIMIT
    ):
        return None
    return variant


def image_video_source_for_wire(source: Any) -> Any:
    """Serialize a stored artifact source for the wire, translating legacy ``variant``.

    Jobs rendered before the ``variant`` → ``variantIndex`` rename (the two
    in-flight pen/twin jobs) persisted ``photoPlan`` with the legacy key. The
    Worker's ``managedPhotoPlanValid`` only accepts ``variantIndex``, so those
    already-rendered artifacts are re-serialized with ``variantIndex`` (never
    emitting ``variant``) without a re-render. The plan signature is
    content-only, so the rename leaves it unchanged.
    """
    if not isinstance(source, dict):
        return source
    plan = source.get("photoPlan")
    if not isinstance(plan, dict) or "variant" not in plan or "variantIndex" in plan:
        return source
    wire = dict(source)
    photo = dict(plan)
    photo["variantIndex"] = photo.pop("variant")
    wire["photoPlan"] = photo
    return wire


def _sha256_from_photo_key(key: str) -> str | None:
    """Return a photo object's pinned SHA-256 when its key embeds one.

    The page's own pexels library objects encode their content hash in the
    filename (``pexels_<id>_<64-hex>.jpg``), which is what lets an unpinned
    extra object be selected without weakening the pinned-SHA check at render
    time. Objects without an embedded hash (e.g. the executor-pinned
    ``pinterest_*`` objects, which are resolved separately) are not eligible.
    """
    stem = key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    candidate = stem.split("_")[-1] if "_" in stem else ""
    return candidate if SHA256.fullmatch(candidate) else None


def _image_video_pool(cfg: dict[str, Any], library: Any) -> list[dict[str, Any]]:
    """The page's own photos usable for variants, in deterministic key order.

    Every entry carries ``key``, ``sourceKey``, ``sha256`` and ``bytes``. Only
    objects under ``<lane>/<subject>/photos/`` are drawn, and only those whose
    SHA-256 is known (executor-pinned or embedded in the key) so the render-time
    pinned-SHA check still applies to every variant object.
    """
    prefix = f"{cfg['lane']}/{cfg['subject']}/photos/"
    pinned = {obj["key"]: obj for obj in cfg["objects"]}
    by_key = {obj.key: obj for obj in library.objects}
    pool: list[dict[str, Any]] = []
    for key in sorted(by_key):
        if not key.startswith(prefix):
            continue
        item = by_key[key]
        if key in pinned:
            source_key = pinned[key]["sourceKey"]
            sha256 = pinned[key]["sha256"]
        else:
            sha256 = _sha256_from_photo_key(key)
            if sha256 is None:
                continue
            source_key = key
        pool.append({
            "key": key,
            "sourceKey": source_key,
            "sha256": sha256,
            "bytes": item.bytes,
        })
    return pool


def image_video_variant_plans(page_id: str, library: Any) -> list[dict[str, Any]]:
    """Every photo hold for one page in variant order (variant 0 first).

    Variant 0 is the approved plan, byte-identical to ``image_video_plan``.
    Variants 1..N keep each slot's duration, focal and ken-burns and rotate
    which of the page's own photos fills each slot, in a deterministic order
    indexed by the variant number. Plans are content-addressed, so no two
    offered plans share a signature and identical content is never offered
    twice.
    """
    cfg = _plan_config(page_id)
    if cfg is None:
        return []
    base = image_video_plan(page_id)
    if base is None:
        return []
    pool = _image_video_pool(cfg, library)
    media_count = len(cfg["objects"])
    plans = [base]
    seen_media = {json.dumps([item["key"] for item in base["media"]], separators=(",", ":"))}
    pool_size = len(pool)
    if pool_size < media_count:
        return plans
    for variant in range(1, min(IMAGE_VIDEO_VARIANT_LIMIT, pool_size + 1)):
        offset = variant - 1
        media = []
        for slot in range(media_count):
            item = pool[(offset + slot) % pool_size]
            base_slot = cfg["objects"][slot]
            media.append({
                "key": item["key"],
                "sourceKey": item["sourceKey"],
                "sha256": item["sha256"],
                "bytes": item["bytes"],
                "duration": base_slot["duration"],
                "focalX": base_slot["focalX"],
                "focalY": base_slot["focalY"],
                "cropMode": "cover",
                "kenBurns": (
                    dict(base_slot["kenBurns"])
                    if base_slot["kenBurns"] is not None else None
                ),
            })
        fingerprint = json.dumps([item["key"] for item in media], separators=(",", ":"))
        if fingerprint in seen_media:
            continue
        plan = {
            "planSlug": PLAN_SLUG,
            "pageId": cfg["pageId"],
            "libraryId": f"syzygy:{cfg['lane']}:{cfg['subject']}",
            "lane": cfg["lane"],
            "subject": cfg["subject"],
            "totalDurationSeconds": cfg["totalDurationSeconds"],
            "brightnessGainFromOriginals": cfg["brightnessGainFromOriginals"],
            "media": media,
            "variantIndex": variant,
        }
        plan["signature"] = image_video_plan_signature(plan)
        seen_media.add(fingerprint)
        plans.append(plan)
    return plans


def image_video_plan_for_variant(page_id: str, library: Any, variant: int) -> dict[str, Any] | None:
    """One photo hold variant (with signature), or None when it is not offered."""
    if isinstance(variant, bool) or not isinstance(variant, int):
        return None
    for plan in image_video_variant_plans(page_id, library):
        if image_video_variant_number(plan) == variant:
            return plan
    return None

def plan_image_video(
    page_id: str, unavailable_signatures: set[str],
) -> list[dict[str, Any]]:
    """Plan one page's image-video, or [] once its signature is reserved.

    Unknown pages and invalid reservation sets also return [] so a dispatch
    loop can treat "no plan" uniformly without probing a foreign page.
    """
    if (
        not isinstance(page_id, str)
        or not page_id.strip()
        or not isinstance(unavailable_signatures, set)
        or any(not isinstance(item, str) or not SHA256.fullmatch(item) for item in unavailable_signatures)
    ):
        return []
    plan = image_video_plan(page_id)
    if plan is None or plan["signature"] in unavailable_signatures:
        return []
    return [plan]


def validate_image_video_library(
    page_id: str, observations: Any,
) -> str:
    """Validate live observations against a page's configured objects.

    ``observations`` is a list of ``{"key", "bytes", "sha256"}`` dicts. Every
    configured object must be present with exactly its pinned bytes and SHA-256;
    duplicates, missing objects, wrong sizes/hashes, and malformed entries fail
    closed. Extras in the observed library are ignored and never selected.
    Returns the canonical snapshot hash over the page's ordered objects.
    """
    cfg = _plan_config(page_id)
    if cfg is None:
        raise ImageVideoError("image-video page is unknown")
    if not isinstance(observations, (list, tuple)):
        raise ValueError("library observations are invalid")
    by_key: dict[str, tuple[int, str]] = {}
    for obs in observations:
        if not isinstance(obs, dict) or set(obs) != {"key", "bytes", "sha256"}:
            raise ValueError("library observation is invalid")
        key = obs.get("key")
        bytes_ = obs.get("bytes")
        sha = obs.get("sha256")
        if not isinstance(key, str) or not OBJECT_KEY.fullmatch(key):
            raise ValueError("library observation key is invalid")
        if not isinstance(bytes_, int) or isinstance(bytes_, bool) or bytes_ <= 0:
            raise ValueError("library observation bytes is invalid")
        if not isinstance(sha, str) or not SHA256.fullmatch(sha):
            raise ValueError("library observation sha256 is invalid")
        if key in by_key:
            raise ValueError("library observation key is duplicated")
        by_key[key] = (bytes_, sha)
    for obj in cfg["objects"]:
        observed = by_key.get(obj["key"])
        if observed is None:
            raise ImageVideoError("configured object is missing from library")
        if observed[0] != obj["bytes"] or observed[1] != obj["sha256"]:
            raise ImageVideoError("configured object bytes/hash changed")
    canonical = json.dumps(
        [[obj["key"], obj["bytes"], obj["sha256"]] for obj in cfg["objects"]],
        separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _image_video_plan_valid(plan: Any) -> bool:
    """A photo hold is structurally valid for its page and self-consistent.

    The job-time runner re-derives the exact claimed variant from the executor
    and the live library; this is the submit-boundary defense so a foreign or
    forged plan never reaches the remote render POST. Media must stay inside
    the page's own ``<lane>/<subject>/photos/`` library and carry a full
    SHA-256, positive bytes, finite duration/focal, ``cover`` crop and a valid
    ken-burns, and the signature must equal the recomputed identity.
    """
    if not isinstance(plan, dict):
        return False
    page_id = plan.get("pageId")
    cfg = _plan_config(page_id)
    if cfg is None:
        return False
    if image_video_variant_number(plan) is None:
        return False
    if (
        plan.get("planSlug") != PLAN_SLUG
        or plan.get("libraryId") != f"syzygy:{cfg['lane']}:{cfg['subject']}"
        or plan.get("lane") != cfg["lane"]
        or plan.get("subject") != cfg["subject"]
        or plan.get("totalDurationSeconds") != cfg["totalDurationSeconds"]
        or plan.get("brightnessGainFromOriginals") != cfg["brightnessGainFromOriginals"]
    ):
        return False
    media = plan.get("media")
    if not isinstance(media, list) or len(media) != len(cfg["objects"]):
        return False
    prefix = f"{cfg['lane']}/{cfg['subject']}/photos/"
    seen: set[str] = set()
    for item in media:
        if not isinstance(item, dict) or set(item) != {
            "key", "sourceKey", "sha256", "bytes", "duration",
            "focalX", "focalY", "cropMode", "kenBurns",
        }:
            return False
        key = item.get("key")
        source_key = item.get("sourceKey")
        if (
            not isinstance(key, str)
            or not OBJECT_KEY.fullmatch(key)
            or not key.startswith(prefix)
            or not isinstance(source_key, str)
            or not OBJECT_KEY.fullmatch(source_key)
        ):
            return False
        if (
            not isinstance(item.get("sha256"), str)
            or not SHA256.fullmatch(item["sha256"])
        ):
            return False
        if (
            not isinstance(item.get("bytes"), int)
            or isinstance(item.get("bytes"), bool)
            or item["bytes"] <= 0
        ):
            return False
        if not _finite_number(item.get("duration"), minimum=1e-9):
            return False
        if item.get("cropMode") != "cover":
            return False
        focal_x, focal_y = item.get("focalX"), item.get("focalY")
        if (
            not _finite_number(focal_x, minimum=0) or focal_x > 1
            or not _finite_number(focal_y, minimum=0) or focal_y > 1
        ):
            return False
        if item.get("kenBurns") is not None:
            try:
                _parse_ken_burns(item.get("kenBurns"))
            except ValueError:
                return False
        if key in seen:
            return False
        seen.add(key)
    if plan.get("signature") != image_video_plan_signature(plan):
        return False
    return True


def build_image_video_spec(plan: dict[str, Any]) -> dict[str, Any]:
    """Build the supported Syzygy photo spec with explicit per-media fields.

    No template_slug, cuts, or flow-stage defaults; no source audio/URL; blank
    hook/lyrics/captions/text and empty effects/overlays. Total duration is
    carried both by the explicit per-media durations and the timing audio
    section (``section_start``/``section_end``), which the Syzygy photo parser
    uses to size the output. Brightness is deliberately absent: the later
    source-treatment boundary applies the gain once.
    """
    if not _image_video_plan_valid(plan):
        raise ImageVideoError("image-video plan differs from current page authority")
    # Variant 0 is the approved photo hold: it must equal the pinned authority
    # byte for byte. Higher variants carry their own signature and are validated
    # structurally here; the job-time runner re-derives them against the live
    # library before submission.
    if image_video_variant_number(plan) == 0 and image_video_plan(plan.get("pageId")) != plan:
        raise ImageVideoError("image-video plan differs from current page authority")
    media = []
    for obj in plan["media"]:
        item = {
            "type": "photo",
            "library": obj["key"],
            "duration": obj["duration"],
            "focal_x": obj["focalX"],
            "focal_y": obj["focalY"],
            "crop_mode": obj["cropMode"],
        }
        if obj["kenBurns"] is not None:
            item["ken_burns"] = dict(obj["kenBurns"])
        else:
            item["ken_burns"] = {"dir": "none", "zoom": 0.0}
        media.append(item)
    return {
        "format": "photo",
        "aspect": "9:16",
        "transition": 0,
        "hook": {"text": "", "position": "none"},
        "lyrics": [],
        "captions": {"position": "none"},
        "textCards": [],
        "text_cards": [],
        "lyrics_lrc": "",
        "lyrics_required": False,
        "uses_original_audio": False,
        "effects": [],
        "overlays": [],
        "media": media,
        "audio": {
            "section_start": 0,
            "section_end": round(float(plan["totalDurationSeconds"]), 6),
        },
    }


def image_video_idempotency_key(*parts: str) -> str:
    canonical = "\0".join(parts)
    return "cpl-image-video-" + hashlib.sha256(canonical.encode()).hexdigest()


async def submit_image_video_render(
    plan: dict[str, Any],
    *,
    page_id: str,
    recipe_version: str,
    request_json: Any = None,
    timeout: float = 90.0,
    format_slug: str | None = None,
) -> tuple[str, str]:
    """Submit one prepared photo plan and return (job_id, quality).

    ``request_json`` is dependency-injectable (matching the existing
    ``_request_json`` signature) so tests capture the exact request receipt
    without a live runtime. The idempotency key is scoped to page + recipe +
    plan signature. The receipt is (job_id, quality) only and never asserts an
    applied brightness gain.
    """
    if (not isinstance(plan, dict) or plan.get("pageId") != page_id
            or not isinstance(recipe_version, str) or not recipe_version.strip()
            or recipe_version != recipe_version.strip()):
        raise ImageVideoError("image-video submission identity is invalid")
    if format_slug is not None:
        target = IMAGE_VIDEO_TARGETS.get(format_slug)
        if target is None or page_id != target[0] or len(plan.get("media", [])) != target[5]:
            raise ImageVideoError("image-video submission format differs from exact page")
    spec = build_image_video_spec(plan)
    key = image_video_idempotency_key(
        page_id, recipe_version, plan["signature"], "render-v1",
    )
    if request_json is None:
        from services.control_plane_slideshows import _request_json
        request_json = _request_json
    request = request_json
    value = await request(
        "POST", "/api/v1/render", body={"spec": spec},
        idempotency_key=key, timeout=timeout,
    )
    job_id = value.get("job_id")
    quality = value.get("quality")
    if not isinstance(job_id, str) or not job_id or not isinstance(quality, str):
        raise ImageVideoError("Syzygy image-video render receipt is invalid")
    return job_id, quality


FORMAT_SLUG = "image-video"
SINGLE_IMAGE_FORMAT_SLUG = "single-image-video"
IMAGE_VIDEO_FORMATS = frozenset({FORMAT_SLUG, SINGLE_IMAGE_FORMAT_SLUG})
# Exact ontology identities; the original plan file remains the media authority.
IMAGE_VIDEO_TARGETS = {
    FORMAT_SLUG: ("acct:pen-pal3", "pen.pal3", "3ed1465b-b829-809f-afb4-da66093e7f9d", "slideshow", "sourced_slideshow", 3),
    SINGLE_IMAGE_FORMAT_SLUG: ("acct:twin47996", "twin47996", "3ed1465b-b829-8056-aa9b-f8579c3210b8", "POV", "sourced_image", 1),
}


def image_video_plan_for_format(format_slug, master_pages):
    """Select only the exact current page/format tuple, never a foreign still pool."""
    target = IMAGE_VIDEO_TARGETS.get(format_slug)
    if target is None or not isinstance(master_pages, dict):
        return None
    page, handle, notion, niche, engine, count = target
    if (master_pages.get("pageId"), master_pages.get("handle"),
        master_pages.get("notionPageId"), master_pages.get("contentNiche"),
        master_pages.get("contentEngine")) != (page, handle, notion, niche, engine):
        return None
    plan = image_video_plan(page)
    if plan is None or len(plan["media"]) != count:
        return None
    return plan


def recipe_image_video_plans(recipe, library):
    """Every offered photo hold variant envelope for a recipe, in variant order.

    None when the recipe is not an image-video recipe or its page/library
    identity mismatches; [] when the live library no longer holds the pinned
    base objects. Variant 0 is the approved plan (same signature as before
    variants existed); variants 1..N re-draw the page's own library photos.
    """
    if recipe.format_slug not in IMAGE_VIDEO_FORMATS:
        return None
    photo = image_video_plan_for_format(
        recipe.format_slug, recipe.recipe_spec.get("masterPages"),
    )
    if (
        photo is None
        or recipe.library_id != photo["libraryId"]
        or library.library_id != photo["libraryId"]
    ):
        return None
    by_key = {obj.key: obj for obj in library.objects}
    if len(by_key) != len(library.objects) or any(
        item["key"] not in by_key or by_key[item["key"]].bytes != item["bytes"]
        for item in photo["media"]
    ):
        return []
    page_id = photo["pageId"]
    return [
        {
            "templateSlug": PLAN_SLUG,
            "mediaKeys": [item["key"] for item in variant["media"]],
            "photoPlan": variant,
            "signature": variant["signature"],
            "librarySnapshotHash": library.snapshot_hash,
        }
        for variant in image_video_variant_plans(page_id, library)
    ]


def recipe_image_video_plan(recipe, library):
    """The approved (variant 0) photo hold envelope, kept for existing callers."""
    plans = recipe_image_video_plans(recipe, library)
    return plans[0] if plans else None


def recipe_image_video_plan_for_variant(recipe, library, variant):
    """One photo hold envelope for a claimed variant, or None if not offered."""
    if isinstance(variant, bool) or not isinstance(variant, int):
        return None
    plans = recipe_image_video_plans(recipe, library)
    if not plans:
        return None
    for plan in plans:
        if image_video_variant_number(plan["photoPlan"]) == variant:
            return plan
    return None

def required_image_video_treatment(spec, page_id):
    """Refuse recipes whose actual treatment would contradict the closed grade."""
    from services.source_treatment import normalized_visual_treatment, NEUTRAL_FILTERS, NEUTRAL_CROP
    plan = image_video_plan(page_id)
    if plan is None:
        return False
    try:
        actual = normalized_visual_treatment(spec.get("renderTreatment"))
    except (ValueError, TypeError):
        return False
    expected = {"filters": {**NEUTRAL_FILTERS, "brightness": plan["brightnessGainFromOriginals"]},
                "clipSpeed": 1., "clipCrop": NEUTRAL_CROP}
    return actual == expected

def slideshow_executor_adapter(format_slug=FORMAT_SLUG):
    """Existing executor vocabulary; original legacy JSON stays byte-identical."""
    if format_slug not in IMAGE_VIDEO_FORMATS:
        raise ImageVideoError("image-video format is unknown")
    config, digest = image_video_executor_contract()
    lane = "interiors" if format_slug == SINGLE_IMAGE_FORMAT_SLUG else "animals"
    return {"formats": {format_slug: {"lane": lane, "minimumLibraryItems": 1,
                "templates": [{"slug": PLAN_SLUG, "mediaCount": 1}]}},
            "selection": config["selection"], "reservation": config["reservation"],
            "output": {**config["output"], "audioPolicy": "campaign_sound_bound_downstream"}}, digest
