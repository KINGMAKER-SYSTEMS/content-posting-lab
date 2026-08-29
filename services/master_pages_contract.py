"""Lossless non-secret Master Pages intent contract shared by machine routes."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


SCHEMA = "master-pages.page-intent.v1"
FIELDS = (
    "pageId", "handle", "group", "groupLabel", "pageType", "accountType",
    "posterName", "status", "project", "tiktokUrl", "notionPageId", "source",
    "contentNiche", "contentEngine", "automationMode", "vaultUrl", "pipeline",
    "soundsReference", "archived",
)
REQUIRED_MATERIAL_FIELDS = (
    "contentNiche", "contentEngine", "automationMode", "vaultUrl",
)
HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


def canonical_intent(value: Any, *, expected_page_id: str | None = None) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != {"schema", *FIELDS}:
        return None
    if value.get("schema") != SCHEMA:
        return None
    if not isinstance(value.get("pageId"), str) or not value["pageId"].strip():
        return None
    if expected_page_id is not None and value["pageId"] != expected_page_id:
        return None
    if not isinstance(value.get("handle"), str) or not value["handle"].strip():
        return None
    if not isinstance(value.get("archived"), bool):
        return None
    for field in set(FIELDS) - {"pageId", "handle", "archived"}:
        if value[field] is not None and not isinstance(value[field], str):
            return None
    canonical = {"schema": SCHEMA}
    for field in FIELDS:
        item = value[field]
        canonical[field] = item.strip() if isinstance(item, str) and item.strip() else (None if isinstance(item, str) else item)
    return canonical


def intent_hash(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def exact_intent(value: Any, supplied_hash: Any, *, expected_page_id: str) -> dict[str, Any] | None:
    canonical = canonical_intent(value, expected_page_id=expected_page_id)
    if canonical is None or not isinstance(supplied_hash, str) or not HASH.fullmatch(supplied_hash):
        return None
    if supplied_hash != intent_hash(canonical):
        return None
    if canonical["archived"]:
        return None
    if any(not isinstance(canonical[field], str) or not canonical[field] for field in REQUIRED_MATERIAL_FIELDS):
        return None
    return canonical
