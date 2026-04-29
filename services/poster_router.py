"""
Poster routing — given a roster page, find the right TelegramPoster.

Phase 1: name match against poster_name from Notion.
Phase 2 (future): capacity-based assignment, page-type-fit scoring,
historical-performance weighting. The interface stays the same so the
swap is one function later.
"""

import re
from typing import Any

from services.telegram import list_posters

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _normalize(s: str) -> str:
    return _NORMALIZE_RE.sub("", s.lower()).strip()


def resolve_poster_for_page(page: dict[str, Any]) -> dict[str, Any] | None:
    """Return the TelegramPoster dict for a page, or None.

    Phase 1: matches poster_name (from Notion) to a registered poster's `name`,
    case-insensitive, ignoring non-alphanumeric.
    """
    target = (page or {}).get("poster_name") or ""
    target = target.strip()
    if not target:
        return None

    target_norm = _normalize(target)
    if not target_norm:
        return None

    posters = list_posters()
    for p in posters:
        if _normalize(p.get("name", "")) == target_norm:
            return p

    # Fuzzy: accept "Jake Balik" matching "Jake B." etc — first-name + initial
    target_first = target.split()[0].lower() if target.split() else ""
    for p in posters:
        pname = p.get("name", "")
        if pname.lower().startswith(target_first) and target_first:
            return p

    return None
