"""Project a banked R2 source-bank entry into a page's Lab source library.

SU-BANK writes ``rt-source-library/<prefix>/bank.json`` for every banked
YouTube/DJ source (schema ``rt.source-bank.v1``, ``role: backup``,
``boundToPages: false``). That entry is the shared source pool; it is not a
page library.

This adapter is the reviewed-registry-commit half of adding a backup (Eric's
rule, ``SOURCE-ENGINE-DESIGN-1022.md``): it projects one bank entry's
``role: backup`` masters for a single page into that page's existing
``SourceDnaLibrary``. The page's primaries are left byte-for-byte as they were,
so the library version the Worker binds clips to does not move. A backup master
declares no ``gradeOverride``: it renders at the page look (lead 2026-10-26).

The adapter never reads or writes R2 itself; the caller hands it the bank.json
bytes it already fetched. It performs no network access and no storage copy.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import replace
from typing import Any

from services.source_dna_registry import (
    SCHEMA as LIBRARY_SCHEMA,
    MasterSource,
    SourceDnaError,
    SourceDnaLibrary,
    parse_source_dna_manifest,
)

BANK_SCHEMA = "rt.source-bank.v1"
MAX_MANIFEST_BYTES = 2_097_152
MAX_MASTERS = 500
# Any timestamp that could round to a legal millisecond value is far below this
# bound; rejecting larger magnitudes as integers keeps ``float(value)`` from
# raising OverflowError on absurd manifest numbers (as in the ShipStream twin).
MAX_SECONDS = 100_000
_HANDLE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class SourceBankError(ValueError):
    pass


def _nonblank(value: Any, maximum: int = 2_048) -> bool:
    return isinstance(value, str) and value == value.strip() and 0 < len(value) <= maximum


def _https(value: Any) -> bool:
    if not _nonblank(value):
        return False
    from urllib.parse import urlparse
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username and not parsed.password


def _slug(value: str) -> str:
    # 39 keeps the composite ``bank-<origin>-<page>-<start>-<end>`` sourceId at or
    # under the registry TOKEN bound of 100 chars even when both handles slug to
    # their full 39 chars and each second rounds to six digits.
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:39]


def _seconds(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and not math.isfinite(value))
        or value < 0
        or value > MAX_SECONDS
    ):
        raise SourceBankError(f"source bank {label} is invalid")
    return float(value)


def manifest_row(master: MasterSource) -> dict[str, Any]:
    """Serialize a master back to its ``content-lab.source-dna-library.v2`` row.

    A primary master carries no ``role`` key; a backup carries ``role`` and its
    declared ``gradeOverride`` only when one is present (backups at the page
    look declare none, so their rows omit the key entirely).
    """
    row: dict[str, Any] = {
        "sourceId": master.source_id,
        "sha256": master.sha256,
        "bytes": master.bytes,
        "filename": master.filename,
        "mimeType": master.mime_type,
        "storageKey": master.storage_key,
        "durationMs": master.duration_ms,
        "sourceOffsetMs": master.source_offset_ms,
        "provenance": dict(master.provenance),
    }
    if master.role != "primary":
        row["role"] = master.role
    if master.grade_override is not None:
        row["gradeOverride"] = master.grade_override
    return row


def _project_backup_master(
    entry: Any,
    index: int,
    *,
    page_handle: str,
    source_url: str,
    acquired_at: str,
    storage_prefix: str,
) -> dict[str, Any]:
    """Project one bank master into a backup row of the page's library."""
    if not isinstance(entry, dict):
        raise SourceBankError(f"source bank masters[{index}] is invalid")
    if entry.get("role") != "backup":
        raise SourceBankError(f"source bank masters[{index}] is not a backup master")
    if entry.get("page") != page_handle:
        # Another page's share of the same source; not this page's footage.
        raise SourceBankError(f"source bank masters[{index}] page does not match")
    sha256 = entry.get("sha256")
    byte_count = entry.get("bytes")
    name = entry.get("name")
    r2_key = entry.get("r2Key")
    origin_source_id = entry.get("originSourceId")
    if not isinstance(sha256, str) or len(sha256) != 64:
        raise SourceBankError(f"source bank masters[{index}].sha256 is invalid")
    if not _nonblank(origin_source_id, 100) or not _HANDLE.fullmatch(origin_source_id):
        raise SourceBankError(f"source bank masters[{index}].originSourceId is invalid")
    if not isinstance(name, str) or not name.strip():
        raise SourceBankError(f"source bank masters[{index}].name is invalid")
    filename = name.rsplit("/", 1)[-1]
    if not filename:
        raise SourceBankError(f"source bank masters[{index}].name is invalid")
    if not _nonblank(r2_key, 500) or r2_key.startswith("/") or ".." in r2_key.split("/"):
        raise SourceBankError(f"source bank masters[{index}].r2Key is invalid")
    start_sec = _seconds(entry.get("originStartSec"), f"masters[{index}].originStartSec")
    end_sec = _seconds(entry.get("originEndSec"), f"masters[{index}].originEndSec")
    duration_sec = _seconds(entry.get("durationSec"), f"masters[{index}].durationSec")
    if end_sec <= start_sec:
        raise SourceBankError(f"source bank masters[{index}] origin window is invalid")
    mime_type = "video/quicktime" if filename.lower().endswith(".mov") else "video/mp4"
    source_id = (
        f"bank-{_slug(origin_source_id)}-{_slug(page_handle)}-"
        f"{int(round(start_sec))}-{int(round(end_sec))}"
    )
    return {
        "sourceId": source_id,
        "sha256": sha256,
        "bytes": byte_count,
        "filename": filename,
        "mimeType": mime_type,
        "storageKey": f"{storage_prefix}/{sha256}.mp4",
        "durationMs": int(round(duration_sec * 1_000)),
        "sourceOffsetMs": int(round(start_sec * 1_000)),
        "role": "backup",
        "provenance": {
            "sourceUrl": source_url,
            "acquiredAt": acquired_at,
            "authority": (
                f"SU-BANK {origin_source_id} role backup for {page_handle}; "
                f"banked at R2 rt-source-library/{r2_key}"
            ),
        },
    }


def _master_identity(master: Any) -> str | None:
    """The SU-BANK identity of a master: its R2 key, else its name."""
    if not isinstance(master, dict):
        return None
    key = master.get("r2Key")
    if not (isinstance(key, str) and key.strip()):
        key = master.get("name")
    if not (isinstance(key, str) and key.strip()):
        return None
    return key


def _master_ids(masters: Any) -> set[str]:
    """Distinct identities of a master list (empty for a non-list entry)."""
    if not isinstance(masters, list):
        return set()
    ids: set[str] = set()
    for master in masters:
        identity = _master_identity(master)
        if identity is not None:
            ids.add(identity)
    return ids


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge a list of half-open intervals into disjoint, sorted intervals."""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: list[list[float]] = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _covers(required: list[tuple[float, float]], banked: list[tuple[float, float]]) -> bool:
    """True when the union of ``banked`` intervals covers every ``required`` interval."""
    merged = _merge_intervals(banked)
    return all(
        any(b_start <= start and end <= b_end for b_start, b_end in merged)
        for start, end in required
    )


def _subtract(
    window: tuple[float, float],
    excluded: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Return ``window`` minus ``excluded`` as disjoint half-open intervals."""
    start, end = window
    pieces: list[tuple[float, float]] = []
    cursor = start
    for ex_start, ex_end in sorted(excluded):
        if ex_end <= cursor:
            continue
        if ex_start >= end:
            break
        clip_start = max(ex_start, cursor)
        clip_end = min(ex_end, end)
        if clip_start > cursor:
            pieces.append((cursor, clip_start))
        cursor = max(cursor, clip_end)
    if cursor < end:
        pieces.append((cursor, end))
    return pieces


def _usable_range(value: dict) -> tuple[float, float] | None:
    """The bank's planned usable range, or None when the entry does not declare one."""
    usable = value.get("usableRangeSec")
    if usable is None:
        return None
    if not isinstance(usable, dict):
        raise SourceBankError("source bank usableRangeSec is invalid")
    start = _seconds(usable.get("start"), "usableRangeSec.start")
    end = _seconds(usable.get("end"), "usableRangeSec.end")
    if end <= start:
        raise SourceBankError("source bank usableRangeSec window is invalid")
    return (start, end)


def _share_windows(value: dict) -> list[tuple[float, float]]:
    """Every declared share window, validated and sorted-free (order preserved)."""
    shares = value.get("shares")
    if shares is None:
        return []
    if not isinstance(shares, list):
        raise SourceBankError("source bank shares must be a list")
    windows: list[tuple[float, float]] = []
    for index, share in enumerate(shares):
        if not isinstance(share, dict):
            raise SourceBankError(f"source bank shares[{index}] is invalid")
        start = _seconds(share.get("shareStartSec"), f"shares[{index}].shareStartSec")
        end = _seconds(share.get("shareEndSec"), f"shares[{index}].shareEndSec")
        if end <= start:
            raise SourceBankError(f"source bank shares[{index}] window is invalid")
        windows.append((start, end))
    return windows


def _excluded_ranges(share: dict, index: int) -> list[tuple[float, float]]:
    """Validated excluded ranges of one share (OCR text gaps are not mastered)."""
    excluded: list[tuple[float, float]] = []
    for ex_index, entry in enumerate(share.get("excludedRanges") or []):
        if not isinstance(entry, dict):
            raise SourceBankError(
                f"source bank shares[{index}].excludedRanges[{ex_index}] is invalid"
            )
        start = _seconds(
            entry.get("startSec"), f"shares[{index}].excludedRanges[{ex_index}].startSec"
        )
        end = _seconds(
            entry.get("endSec"), f"shares[{index}].excludedRanges[{ex_index}].endSec"
        )
        if end <= start:
            raise SourceBankError(
                f"source bank shares[{index}].excludedRanges[{ex_index}] window is invalid"
            )
        excluded.append((start, end))
    return excluded


def _page_required_windows(value: dict, page_handle: str) -> list[tuple[float, float]]:
    """The share windows ``page_handle`` must bank, minus OCR-excluded gaps."""
    shares = value.get("shares")
    if not isinstance(shares, list):
        return []
    required: list[tuple[float, float]] = []
    for index, share in enumerate(shares):
        if not isinstance(share, dict) or share.get("page") != page_handle:
            continue
        start = _seconds(share.get("shareStartSec"), f"shares[{index}].shareStartSec")
        end = _seconds(share.get("shareEndSec"), f"shares[{index}].shareEndSec")
        if end <= start:
            raise SourceBankError(f"source bank shares[{index}] window is invalid")
        required.extend(_subtract((start, end), _excluded_ranges(share, index)))
    return required


def _banked_page_windows(rows: Any, page_handle: str) -> list[tuple[float, float]]:
    """The origin windows of the banked masters that belong to ``page_handle``."""
    if not isinstance(rows, list):
        return []
    banked: list[tuple[float, float]] = []
    for index, entry in enumerate(rows):
        if not isinstance(entry, dict) or entry.get("page") != page_handle:
            continue
        start = _seconds(entry.get("originStartSec"), f"masters[{index}].originStartSec")
        end = _seconds(entry.get("originEndSec"), f"masters[{index}].originEndSec")
        if end <= start:
            raise SourceBankError(f"source bank masters[{index}] origin window is invalid")
        banked.append((start, end))
    return banked


def parse_source_bank_manifest(
    raw: bytes,
    *,
    page_handle: str,
    primary_library: SourceDnaLibrary,
) -> SourceDnaLibrary:
    """Project a bank entry's backup masters for ``page_handle`` into a library.

    The page's primary masters are taken verbatim from ``primary_library``; the
    banked backups ride beside them with ``role: backup`` and no
    ``gradeOverride`` (page look). The returned library is re-validated through
    ``parse_source_dna_manifest`` so a moved or malformed projection fails
    closed, and its version stays pinned to the primaries only.

    A partial index is never projected (F-SU-BANK-PARTIAL-INDEX-BLOCKS-TILES-
    1027): an entry stamped ``partial``, one naming ``missingRanges``, one
    whose declared shares do not cover ``usableRangeSec`` (the legacy
    ``uploader.sh`` writes an unstamped partial), or one whose banked masters
    do not cover the requested page's declared share raises, so a half library
    can never be returned.
    """
    if not _nonblank(page_handle, 64):
        raise SourceBankError("source bank page handle is invalid")
    if not raw or len(raw) > MAX_MANIFEST_BYTES:
        raise SourceBankError("source bank manifest is empty or too large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceBankError("source bank manifest is not valid JSON") from error
    if not isinstance(value, dict) or value.get("schema") != BANK_SCHEMA:
        raise SourceBankError("source bank manifest is not an rt.source-bank.v1 entry")
    if value.get("role") != "backup":
        raise SourceBankError("source bank entry is not a backup pool")
    if value.get("boundToPages") is not False:
        raise SourceBankError("source bank entry is page-bound, not a shared backup pool")
    source = value.get("source")
    source_url = source.get("url") if isinstance(source, dict) else None
    if not _https(source_url):
        raise SourceBankError("source bank source URL is invalid")
    acquired_at = value.get("updatedAt")
    if not _nonblank(acquired_at, 100):
        raise SourceBankError("source bank updatedAt is invalid")
    rows = value.get("masters")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_MASTERS:
        raise SourceBankError("source bank masters must be a non-empty bounded list")

    # Fail closed on the SU-BANK partial-upload shape before projecting anything.
    if value.get("partial"):
        raise SourceBankError("source bank entry is a partial index")
    if value.get("missingRanges"):
        raise SourceBankError("source bank entry has missing ranges")
    declared_ids: set[str] = set()
    for share in value.get("shares") or []:
        if isinstance(share, dict):
            declared_ids |= _master_ids(share.get("masters"))
    if declared_ids and not declared_ids.issubset(_master_ids(rows)):
        raise SourceBankError("source bank entry is missing banked masters")

    # Fail closed on the unstamped-partial shape the legacy uploader.sh writes
    # (no ``partial``/``missingRanges`` markers, shares present but not covering
    # the planned usable range): the declared share windows must tile
    # ``usableRangeSec``. This is the same ``covers_planned`` test the SU-BANK
    # upload proxy applies before publishing a complete bank.json.
    usable = _usable_range(value)
    if usable is not None and not _covers([usable], _share_windows(value)):
        raise SourceBankError("source bank entry covers only part of its usable range")

    # The page's banked masters must cover the page's declared share (minus any
    # OCR-excluded gaps). A master whose identity matches a declared share but
    # whose ``page`` was relabeled drops out of the projection and leaves a gap
    # here, so a silently incomplete page library can never be returned.
    required_windows = _page_required_windows(value, page_handle)
    if required_windows and not _covers(required_windows, _banked_page_windows(rows, page_handle)):
        raise SourceBankError(f"source bank entry is missing banked masters for page {page_handle!r}")

    primaries = [master for master in primary_library.masters if master.role == "primary"]
    if not primaries:
        raise SourceBankError("primary library has no primary master")
    storage_prefix = primaries[0].storage_key.rsplit("/", 1)[0]

    projected: list[dict[str, Any]] = []
    for index, entry in enumerate(rows):
        if isinstance(entry, dict) and entry.get("page") != page_handle:
            continue  # another page's share of this source
        projected.append(_project_backup_master(
            entry, index,
            page_handle=page_handle,
            source_url=source_url,
            acquired_at=acquired_at,
            storage_prefix=storage_prefix,
        ))
    if not projected:
        raise SourceBankError(f"source bank entry has no backup master for page {page_handle!r}")

    document: dict[str, Any] = {
        "schema": LIBRARY_SCHEMA,
        "libraryId": primary_library.library_id,
        "format": primary_library.format_slug,
        "pageId": primary_library.page_id,
        "masters": [manifest_row(master) for master in primary_library.masters] + projected,
    }
    document["masters"].sort(key=lambda row: row["sourceId"])
    try:
        validated = parse_source_dna_manifest(
            json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(),
            primary_library.library_id,
        )
    except SourceDnaError as error:
        raise SourceBankError(str(error)) from error
    # Re-serializing the primaries here would hash a different byte form than the
    # primary library the Worker already bound (the registry pins an all-primary
    # manifest to its exact bytes, or a backed-up one to the canonical
    # primary-only form). Keep the primary library's own version so adding
    # backups never moves it.
    return replace(validated, sha256=primary_library.sha256)
