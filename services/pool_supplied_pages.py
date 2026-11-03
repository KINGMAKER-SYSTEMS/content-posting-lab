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
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("content_lab.pool_supplied_pages")

SCHEMA = "content-lab.pool-supplied-pages.v1"
_PATH = Path(__file__).resolve().parents[1] / "recipes" / "pool-supplied-pages.v1.json"

# Same closed charset as routers/control_plane.PAGE_ID_RE: page ids are bound
# the same way here so a rejected value is logged as an escaped %r token in the
# ALERT log line rather than flowing verbatim into structured output. The %r
# form escapes control characters but does NOT truncate: a long hostile value
# is logged in full (escaped), not as a short token.
_PAGE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

# The committed list is loaded once per process (per _PATH). A missing/malformed
# file then alerts exactly once (on first load), not once per catalog/capabilities
# call. The path is part of the cache key so a test that points _PATH at a fresh
# fixture gets a fresh fail-closed decision instead of a stale cached one.
_cache: tuple[Path, frozenset[str]] | None = None

# Guards the check-then-set in load_pool_supplied_page_ids so a concurrent cold
# start (several threads hitting the catalog on the very first load) parses and
# ALERTs exactly once instead of once per racing thread.
_cache_lock = threading.Lock()


def _alert(message: str, *args: Any) -> None:
    log.error("ALERT pool-supplied-pages: " + message, *args)


def load_pool_supplied_page_ids() -> frozenset[str]:
    """Return the unique safe page ids, or an empty set when the file fails closed.

    Cached per process path: the committed list is read and parsed once, so
    callers (dossier catalog build, recipe registration, capabilities) share
    one fail-closed decision and a malformed file alerts once instead of per
    call. The load is serialized by _cache_lock so concurrent cold starts
    also share that single decision and single ALERT.
    """
    global _cache
    with _cache_lock:
        if _cache is not None and _cache[0] == _PATH:
            return _cache[1]
        ids = _load_pool_supplied_page_ids_uncached()
        _cache = (_PATH, ids)
        return ids


def reset_pool_supplied_page_ids_cache() -> None:
    """Drop the cached list so tests can point _PATH at a fresh fixture."""
    global _cache
    _cache = None


def _load_pool_supplied_page_ids_uncached() -> frozenset[str]:
    try:
        raw = _PATH.read_bytes()
    except OSError as error:
        _alert("list unreadable (%s): %s", _PATH, error)
        return frozenset()
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as error:
        # JSONDecodeError and UnicodeDecodeError are both ValueError subclasses;
        # a deeply nested file raises RecursionError. All fail closed + ALERT.
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
