"""Closed-schema source of truth for pages the Worker supplies from its vault pool.

These pages post FINISHED clips from ``vault/<handle>/pool`` instead of
Lab-rendered media, so the Lab must not treat a Lab image library as a required
dossier ingredient for them. This module is the ONE committed list both the
dossier catalog and recipe registration read.

Fail closed: a missing, unreadable, malformed, wrong-schema, or duplicate
page-id file resolves to the empty set and logs an ALERT, never crashes the
Dossier editor and never lets a caller treat an unexpected page as supplied.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("content_lab.pool_supplied_pages")

SCHEMA = "content-lab.pool-supplied-pages.v1"
_PATH = Path(__file__).resolve().parents[1] / "recipes" / "pool-supplied-pages.v1.json"

# Same closed charset as routers/control_plane.PAGE_ID_RE: page ids are bound
# the same way here so a hostile list value cannot flow into a log line.
_PAGE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _alert(message: str, *args: Any) -> None:
    log.error("ALERT pool-supplied-pages: " + message, *args)


def load_pool_supplied_page_ids() -> frozenset[str]:
    """Return the unique safe page ids, or an empty set when the file fails closed."""
    try:
        raw = _PATH.read_bytes()
    except OSError as error:
        _alert("list unreadable (%s): %s", _PATH, error)
        return frozenset()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as error:
        _alert("list is not valid JSON: %s", error)
        return frozenset()
    if (
        not isinstance(data, dict)
        or data.get("schema") != SCHEMA
        or set(data) != {"schema", "pages"}
    ):
        _alert("list schema mismatch (expected %r)", SCHEMA)
        return frozenset()
    pages = data.get("pages")
    if not isinstance(pages, list):
        _alert("list pages must be an array")
        return frozenset()
    page_ids: set[str] = set()
    for page_id in pages:
        if (
            not isinstance(page_id, str)
            or not _PAGE_ID_RE.fullmatch(page_id)
            or page_id in page_ids
        ):
            _alert("list has an invalid or duplicate page id: %r", page_id)
            return frozenset()
        page_ids.add(page_id)
    return frozenset(page_ids)


def is_pool_supplied(page_id: str) -> bool:
    """True only for an exact, listed page id."""
    return isinstance(page_id, str) and page_id in load_pool_supplied_page_ids()
