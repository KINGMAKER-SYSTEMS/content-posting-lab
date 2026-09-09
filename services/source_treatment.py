"""Actual applied video treatment; caption style is recipe context, never source overlay proof."""
from __future__ import annotations

import copy
import math
import re

NEUTRAL_FILTERS = {"brightness": 1.0, "contrast": 1.0, "saturation": 1.0,
                   "warmth": 0.0, "fade": 0.0, "grain": 0.0, "vignette": 0.0}
NEUTRAL_CROP = {"zoom": 1.0, "focusX": 0.5, "focusY": 0.5}


def _number(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("invalid visual treatment number")
    return float(value)


def normalized_visual_treatment(treatment: dict) -> dict:
    """Resolve only grade, speed and focal crop; exclude recipe preset and captionStyle."""
    if not isinstance(treatment, dict) or not isinstance(treatment.get("filters"), dict):
        raise ValueError("visual treatment is required")
    if set(treatment["filters"]) - set(NEUTRAL_FILTERS):
        raise ValueError("unknown visual filter")
    filters = {}
    for name, neutral in NEUTRAL_FILTERS.items():
        value = treatment["filters"].get(name)
        minimum, maximum = ((-1, 1) if name == "warmth" else (0, 3) if name in {"brightness", "contrast", "saturation"} else (0, 1))
        filters[name] = _number(neutral if value is None else value, minimum, maximum)
    crop = treatment.get("clipCrop")
    if crop is None:
        crop = NEUTRAL_CROP
    if not isinstance(crop, dict) or set(crop) != set(NEUTRAL_CROP):
        raise ValueError("invalid visual crop")
    return {"filters": filters, "clipSpeed": _number(treatment.get("clipSpeed", 1), 0.5, 2),
            "clipCrop": {name: _number(crop[name], 1 if name == "zoom" else 0, 3 if name == "zoom" else 1) for name in NEUTRAL_CROP}}


def source_treatment_receipt(job: dict, recipe_treatment: dict, source_sha256: str, *, clip_speed, clip_crop) -> dict:
    """Call only after a successful actual generation/treatment and exact output hashing."""
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256) or not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", job.get("recipeSpecHash", "")):
        raise ValueError("source treatment identity is incomplete")
    visual = normalized_visual_treatment({**recipe_treatment, "clipSpeed": clip_speed, "clipCrop": clip_crop})
    return {"schema": "content-lab.source-treatment.v1", "sourceSha256": source_sha256,
            "visualTreatment": visual, "sourceRecipeTreatment": copy.deepcopy(recipe_treatment),
            "recipeSpecHash": job["recipeSpecHash"], "generationJobId": job["jobId"]}


def recovery_treatment_matches(receipt: dict | None, job: dict, source_sha256: str,
                               requested_treatment: dict) -> bool:
    """Reuse only source-bound video treatment that the final renderer can accept.

    Missing historical evidence is not neutral footage. Skip that master so
    replenishment can generate new media instead of recycling unpreparable bytes.
    Caption-only changes do not require a new source render.
    """
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema", "sourceSha256", "visualTreatment", "sourceRecipeTreatment",
        "recipeSpecHash", "generationJobId",
    }:
        return False
    if (
        receipt["schema"] != "content-lab.source-treatment.v1"
        or receipt["sourceSha256"] != source_sha256
        or not isinstance(source_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", source_sha256)
        or not isinstance(job.get("jobId"), str)
        or not job["jobId"]
        or receipt["generationJobId"] != job["jobId"]
        or not isinstance(receipt["recipeSpecHash"], str)
        or not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", receipt["recipeSpecHash"])
        or receipt["recipeSpecHash"] != job.get("recipeSpecHash")
    ):
        return False
    try:
        actual = normalized_visual_treatment(receipt["visualTreatment"])
        return (
            receipt["visualTreatment"] == actual
            and actual == normalized_visual_treatment(receipt["sourceRecipeTreatment"])
            and actual == normalized_visual_treatment(requested_treatment)
        )
    except (TypeError, ValueError):
        return False


def derived_source_treatment(parent: dict | None, parent_sha: str, source_sha: str, job_id: str) -> dict | None:
    """Recovery re-crops inherited source; it cannot attest a new requested grade or speed."""
    if parent is None:
        return None
    if parent.get("schema") != "content-lab.source-treatment.v1" or parent.get("sourceSha256") != parent_sha:
        raise ValueError("recovery source treatment binding mismatch")
    result = copy.deepcopy(parent)
    result["visualTreatment"] = normalized_visual_treatment(parent["visualTreatment"])
    result["sourceSha256"] = source_sha
    result["generationJobId"] = job_id
    result["derivedFrom"] = {"sourceSha256": parent_sha, "generationJobId": parent["generationJobId"]}
    return result
