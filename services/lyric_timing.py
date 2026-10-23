"""Immutable page/sound timing authority for the scoped lyric-video format.

Every timing set is a data file under ``recipes/lyric-timing/*.v1.json``. This
module loads and validates the whole directory generically: a page is
registered iff it has a timing file, a set is looked up by its own ``id``, and
duplicate ``id``/``pageId`` values fail closed at load. No page name or set id
is compiled in.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

_TIMING_DIR = Path(__file__).resolve().parents[1] / "recipes" / "lyric-timing"
_FIELDS = {"schema", "id", "pageId", "account", "soundId", "durationMs", "phrases", "canonicalCaption", "referenceAudioSha256", "timingProvenance", "intervalSemantics", "sourceProgramId", "sourceProgramSha256", "notionPageId"}
_SCHEMA = "content-lab.lyric-timing-set.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# Derived revision shims for the single shipped set. These are not load
# authority (the directory scan is); they are kept so tests and downstream
# receipts that reference the canonical set keep byte-identical names/digests.
_TIMING_ID = "burnt-heavy-foot-chorus-v1"
_TIMING_PATH = _TIMING_DIR / "burnt-heavy-foot-chorus.v1.json"
_TIMING_SHA256 = "134be148cc3c522fa8ff48e7bcdd8aebc5ff0c6f1a8073ee5b4010558c3f3e9f"

# Shipped timing files are immutable relative to code: every filename recorded
# here must load with exactly these bytes. A file not listed here is a data-only
# addition and keeps its self-derived sha256 (its authority is the bytes on
# disk); a listed file that has been edited without its pin fails loudly.
_TIMING_MANIFEST = {
    "burnt-heavy-foot-chorus.v1.json": _TIMING_SHA256,
}

# Loaded catalog cache, keyed by the resolved timing directory plus a content
# fingerprint (file mtimes+sizes) so a runtime data edit is observed without a
# process restart. A test that swaps ``_TIMING_DIR`` observes a fresh load.
_catalog: dict[str, Any] | None = None
_catalog_dir: str | None = None
_catalog_fp: tuple[tuple[str, int, int], ...] | None = None


class LyricTimingError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _reset_catalog() -> None:
    """Clear the loaded catalog (test hook)."""
    global _catalog, _catalog_dir, _catalog_fp
    _catalog = None
    _catalog_dir = None
    _catalog_fp = None


def _catalog_fingerprint() -> tuple[tuple[str, int, int], ...]:
    """Return a content key over the timing directory (filename, mtime, size)."""
    entries: list[tuple[str, int, int]] = []
    try:
        for path in _TIMING_DIR.glob("*.v1.json"):
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((path.name, stat.st_mtime_ns, stat.st_size))
    except OSError:
        return ()
    return tuple(sorted(entries))


def _load_file(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LyricTimingError("lyric_timing_unavailable", "timing manifest is unavailable") from exc
    digest = hashlib.sha256(raw).hexdigest()
    pin = _TIMING_MANIFEST.get(path.name)
    if pin is not None and digest != pin:
        raise LyricTimingError("lyric_timing_hash_mismatch", "timing selection revision is stale or invalid")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise LyricTimingError("lyric_timing_invalid", "timing manifest is invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value) != _FIELDS
        or value.get("schema") != _SCHEMA
        or type(value.get("durationMs")) is not int
        or value["durationMs"] != 20_000
    ):
        raise LyricTimingError("lyric_timing_invalid", "timing identity or shape is invalid")
    for key in (
        "id", "pageId", "account", "soundId", "notionPageId",
        "timingProvenance", "intervalSemantics", "sourceProgramId",
    ):
        item = value.get(key)
        if not isinstance(item, str) or not item or item != item.strip():
            raise LyricTimingError("lyric_timing_invalid", "timing identity or shape is invalid")
    for key in ("referenceAudioSha256", "sourceProgramSha256"):
        if not isinstance(value.get(key), str) or not _SHA256.fullmatch(value[key]):
            raise LyricTimingError("lyric_timing_invalid", "timing identity or shape is invalid")
    if not isinstance(value.get("phrases"), list) or len(value["phrases"]) != 7:
        raise LyricTimingError("lyric_timing_invalid", "timing identity or shape is invalid")
    previous_end = None
    for phrase in value["phrases"]:
        if (not isinstance(phrase, dict) or set(phrase) != {"startMs", "endMs", "text"}
                or type(phrase.get("startMs")) is not int or type(phrase.get("endMs")) is not int
                or not 0 <= phrase["startMs"] < phrase["endMs"] <= value["durationMs"]
                or (previous_end is not None and phrase["startMs"] != previous_end)
                or not isinstance(phrase.get("text"), str) or not phrase["text"].strip()
                or phrase["text"] != phrase["text"].strip() or len(phrase["text"].encode()) > 1_000
                or any(ord(c) < 32 for c in phrase["text"])):
            raise LyricTimingError("lyric_timing_invalid", "timing phrases must be bounded, contiguous and nonoverlapping")
        previous_end = phrase["endMs"]
    canonical = "\n".join(p["text"] for p in value["phrases"])
    if previous_end != value["durationMs"] or value["canonicalCaption"] != canonical or len(canonical) > 4_000:
        raise LyricTimingError("lyric_timing_invalid", "timing caption or final boundary is invalid")
    # Retain the exact registered UTF-8 bytes; reserialization changes authority.
    return {**value, "sha256": digest, "manifestJson": raw.decode("utf-8")}


def _load_catalog() -> dict[str, Any]:
    global _catalog, _catalog_dir, _catalog_fp
    resolved = str(_TIMING_DIR.resolve())
    fingerprint = _catalog_fingerprint()
    if _catalog is not None and _catalog_dir == resolved and _catalog_fp == fingerprint:
        return _catalog
    by_id: dict[str, dict[str, Any]] = {}
    by_page_id: dict[str, dict[str, Any]] = {}
    by_page: dict[tuple[str, str], list[dict[str, Any]]] = {}
    try:
        paths = sorted(_TIMING_DIR.glob("*.v1.json"))
    except OSError:
        paths = []
    for path in paths:
        value = _load_file(path)
        set_id = value["id"]
        page_id = value["pageId"]
        if set_id in by_id:
            raise LyricTimingError("lyric_timing_invalid", f"duplicate lyric timing set id {set_id!r}")
        if page_id in by_page_id:
            raise LyricTimingError("lyric_timing_invalid", f"duplicate lyric timing page {page_id!r}")
        by_id[set_id] = value
        by_page_id[page_id] = value
        by_page.setdefault((page_id, value["account"]), []).append(value)
    _catalog = {"by_id": by_id, "by_page_id": by_page_id, "by_page": by_page}
    _catalog_dir = resolved
    _catalog_fp = fingerprint
    return _catalog


def resolve_lyric_timing(set_id: str, *, page_id: str, account: str, sound_id: str, expected_sha256: str) -> dict[str, Any]:
    catalog = _load_catalog()
    value = catalog["by_id"].get(set_id)
    if value is None:
        raise LyricTimingError("lyric_timing_unknown", "timing selection is not registered")
    if expected_sha256 != value["sha256"]:
        raise LyricTimingError("lyric_timing_hash_mismatch", "timing selection revision is stale or invalid")
    if (page_id, account, sound_id) != (value["pageId"], value["account"], value["soundId"]):
        raise LyricTimingError("lyric_timing_scope_mismatch", "timing selection belongs to a different page, account or sound")
    return value


def lyric_timing_catalog(*, page_id: str, account: str) -> list[dict[str, Any]]:
    catalog = _load_catalog()
    values = catalog["by_page"].get((page_id, account), [])
    return [{"id": value["id"], "sha256": value["sha256"], "soundId": value["soundId"], "durationMs": value["durationMs"], "canonicalCaption": value["canonicalCaption"], "sourceProgramId": value["sourceProgramId"], "sourceProgramSha256": value["sourceProgramSha256"], "notionPageId": value["notionPageId"]} for value in values]


def _timing_set_for_page(page_id: str) -> dict[str, Any] | None:
    """Return the full timing record for a page id, or ``None`` when unregistered."""
    return _load_catalog()["by_page_id"].get(page_id)
