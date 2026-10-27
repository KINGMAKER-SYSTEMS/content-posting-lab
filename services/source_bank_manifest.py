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
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:48]


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
        return parse_source_dna_manifest(
            json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(),
            primary_library.library_id,
        )
    except SourceDnaError as error:
        raise SourceBankError(str(error)) from error
