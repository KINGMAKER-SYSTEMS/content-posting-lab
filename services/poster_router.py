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
    exact = [p for p in posters if _normalize(p.get("name", "")) == target_norm]
    if exact:
        return exact[0] if len(exact) == 1 else None

    # Fuzzy: accept "Jake Balik" matching "Jake B." etc — first-name + initial
    target_tokens = re.findall(r"[a-z0-9]+", target.lower())
    candidates = []
    for p in posters:
        tokens = re.findall(r"[a-z0-9]+", p.get("name", "").lower())
        if not tokens or tokens[0] != target_tokens[0]:
            continue
        if len(target_tokens) > 1 and len(tokens) != len(target_tokens):
            continue
        if all(a == b or (len(a) == 1 and b.startswith(a)) or
               (len(b) == 1 and a.startswith(b))
               for a, b in zip(target_tokens[1:], tokens[1:])):
            candidates.append(p)
    return candidates[0] if len(candidates) == 1 else None
