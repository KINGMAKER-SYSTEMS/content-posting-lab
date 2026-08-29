"""Immutable dossier recipe publications accepted from ShipStream.

Registration is not execution. Publications remain absent from the capability
catalog until a new-media executor can consume the exact typed treatment.
Every accepted publication is immutable and page-scoped.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException

from services.roster import ROSTER_PATH
from services.master_pages_contract import exact_intent


LANE = "content-bucket-control-plane"
REQUEST_SCHEMA = "content-lab.recipe-publication.v1"
RESPONSE_SCHEMA = "content-lab.response.v1"
BODY_FIELDS = {
    "schema", "pageId", "lane", "recipeId", "engine", "recipeVersion",
    "dossierRevision", "recipeSpecHash", "recipeSpecCanonical",
}
SPEC_FIELDS = {"schema", "masterPages", "masterPagesHash", "renderTreatment", "demand"}
RENDER_REQUIRED_FIELDS = {"stylePreset", "filters", "captionStyle"}
RENDER_OPTIONAL_FIELDS = {"clipSpeed"}
DEMAND_FIELDS = {"formatMix"}
TOKEN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
)
MAX_SPEC_BYTES = 131_072

router = APIRouter()


def _root() -> Path:
    configured = os.environ.get("CONTENT_LAB_RECIPE_ROOT", "").strip()
    root = Path(configured) if configured else ROSTER_PATH.parent / "control_plane_recipes"
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def require_control_plane_bearer(authorization: str | None) -> None:
    expected = os.environ.get("CONTROL_PLANE_TOKEN", "")
    if not expected:
        raise HTTPException(503, "Content Lab control-plane credential is not configured")
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(401, "invalid control-plane credential")
    supplied = authorization[len(prefix):]
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "invalid control-plane credential")


def _token(value: Any, name: str, maximum: int = 200) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(char not in TOKEN_CHARS for char in value)
    ):
        raise HTTPException(400, f"{name} must be a safe bounded token")
    return value


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _record_path(root: Path, body: dict[str, Any]) -> Path:
    identity = json.dumps(
        [body["pageId"], body["recipeId"], body["engine"], body["recipeVersion"]],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return root / f"{hashlib.sha256(identity.encode('utf-8')).hexdigest()}.json"


def _validate_spec(canonical: str) -> dict[str, Any]:
    if not isinstance(canonical, str) or len(canonical.encode("utf-8")) > MAX_SPEC_BYTES:
        raise HTTPException(400, "recipeSpecCanonical is invalid or too large")
    try:
        spec = json.loads(canonical)
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "recipeSpecCanonical is not valid JSON") from exc
    if not isinstance(spec, dict) or set(spec) != SPEC_FIELDS or spec.get("schema") != "dossier.recipe-spec.v2":
        raise HTTPException(400, "recipe spec schema mismatch")
    master_pages = exact_intent(
        spec.get("masterPages"), spec.get("masterPagesHash"),
        expected_page_id=str(spec.get("masterPages", {}).get("pageId") or ""),
    )
    if master_pages is None:
        raise HTTPException(400, "recipe spec Master Pages intent is invalid")
    render = spec.get("renderTreatment")
    demand = spec.get("demand")
    if (
        not isinstance(render, dict)
        or not RENDER_REQUIRED_FIELDS <= set(render)
        or set(render) - RENDER_REQUIRED_FIELDS - RENDER_OPTIONAL_FIELDS
    ):
        raise HTTPException(400, "render treatment schema mismatch")
    if not isinstance(demand, dict) or set(demand) != DEMAND_FIELDS:
        raise HTTPException(400, "recipe demand schema mismatch")
    if not isinstance(render.get("filters"), dict) or not isinstance(render.get("captionStyle"), dict):
        raise HTTPException(400, "render treatment objects are required")
    if not isinstance(demand.get("formatMix"), dict):
        raise HTTPException(400, "formatMix must be an object")
    preset = render.get("stylePreset")
    if preset is not None and (not isinstance(preset, str) or not preset.strip()):
        raise HTTPException(400, "stylePreset must be a non-empty string or null")
    clip_speed = render.get("clipSpeed", 1.0)
    if (
        isinstance(clip_speed, bool)
        or not isinstance(clip_speed, (int, float))
        or not math.isfinite(float(clip_speed))
        or not 0.5 <= float(clip_speed) <= 2.0
    ):
        raise HTTPException(400, "clipSpeed must be a finite number from 0.5 through 2.0")
    if any(
        part in key.lower()
        for key in _walk_keys(spec)
        for part in ("prompt", "instruction", "message")
    ):
        raise HTTPException(400, "free-form instruction fields are not accepted")
    return spec


def register_recipe(
    body: dict[str, Any], *, lane: str, page_id: str, idempotency_key: str,
) -> dict[str, Any]:
    if not isinstance(body, dict) or set(body) != BODY_FIELDS:
        raise HTTPException(400, "recipe publication fields do not match the contract")
    if body["schema"] != REQUEST_SCHEMA or body["lane"] != LANE or lane != LANE:
        raise HTTPException(400, "recipe publication lane or schema mismatch")
    if body["pageId"] != page_id:
        raise HTTPException(400, "page header does not match body")
    for field in ("pageId", "recipeId", "engine", "recipeVersion", "dossierRevision"):
        _token(body[field], field)
    _token(idempotency_key, "Idempotency-Key")

    canonical = body["recipeSpecCanonical"]
    spec = _validate_spec(canonical)
    master_pages = spec["masterPages"]
    if master_pages["pageId"] != body["pageId"] or master_pages["contentEngine"] != body["engine"]:
        raise HTTPException(409, "recipe publication does not match Master Pages identity and engine")
    if _hash(canonical) != body["recipeSpecHash"]:
        raise HTTPException(409, "recipeSpecHash does not bind the supplied canonical bytes")

    record = {**body, "idempotencyKey": idempotency_key, "status": "registered"}
    path = _record_path(_root(), body)
    encoded = json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != record:
            raise HTTPException(409, "recipe tuple is already registered with different bytes")
    else:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    return _registration_response(record)


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def _registration_response(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": RESPONSE_SCHEMA,
        "recipeId": record["recipeId"],
        "engine": record["engine"],
        "recipeVersion": record["recipeVersion"],
        "dossierRevision": record["dossierRevision"],
        "recipeSpecHash": record["recipeSpecHash"],
        "status": "registered",
    }


def load_registered_recipe(
    page_id: str, recipe_id: str, engine: str, recipe_version: str,
) -> dict[str, Any] | None:
    identity = {
        "pageId": page_id,
        "recipeId": recipe_id,
        "engine": engine,
        "recipeVersion": recipe_version,
    }
    path = _record_path(_root(), identity)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    expected = {
        "pageId": page_id,
        "recipeId": recipe_id,
        "engine": engine,
        "recipeVersion": recipe_version,
    }
    if record.get("status") != "registered" or any(record.get(key) != value for key, value in expected.items()):
        return None
    return record


def list_registered_recipes(page_id: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(_root().glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("status") == "registered" and record.get("pageId") == page_id:
            records.append(record)
    return records


@router.post("/v1/recipes")
def publish_recipe(
    body: dict[str, Any],
    authorization: str | None = Header(default=None),
    x_rt_lane: str = Header(alias="X-RT-Lane"),
    x_rt_page_id: str = Header(alias="X-RT-Page-Id"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
):
    require_control_plane_bearer(authorization)
    return register_recipe(
        body, lane=x_rt_lane, page_id=x_rt_page_id, idempotency_key=idempotency_key,
    )
