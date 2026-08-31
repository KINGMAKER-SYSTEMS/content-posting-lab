"""Immutable master-source registry for refillable sourced-video families.

Approved posting cuts are not source DNA. A master can enter this registry only
when its original bytes, storage identity, duration, and acquisition authority
are all explicit. Cut windows and derived asset lineage are separate records.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any
from urllib.parse import urlparse


SCHEMA = "content-lab.source-dna-library.v2"
MANIFEST_DIR = Path(__file__).resolve().parents[1] / "recipes/source-dna"
TOKEN = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,199}$")
TOP_FIELDS = {"schema", "libraryId", "format", "pageId", "masters"}
MASTER_FIELDS = {
    "sourceId", "sha256", "bytes", "filename", "mimeType", "storageKey",
    "durationMs", "sourceOffsetMs", "provenance",
}
PROVENANCE_FIELDS = {"sourceUrl", "acquiredAt", "authority"}
MIME_TYPES = {"video/mp4", "video/quicktime"}
MAX_MANIFEST_BYTES = 1_048_576
MAX_MASTERS = 500
MAX_SOURCE_BYTES = 20_000_000_000
MAX_DURATION_MS = 86_400_000


class SourceDnaError(ValueError):
    pass


@dataclass(frozen=True)
class MasterSource:
    source_id: str
    sha256: str
    bytes: int
    filename: str
    mime_type: str
    storage_key: str
    duration_ms: int
    source_offset_ms: int
    provenance: dict[str, str | None]


@dataclass(frozen=True)
class SourceDnaLibrary:
    library_id: str
    format_slug: str
    page_id: str
    masters: tuple[MasterSource, ...]
    sha256: str


def _nonblank(value: Any, maximum: int) -> bool:
    return isinstance(value, str) and value == value.strip() and 0 < len(value) <= maximum


def _https_or_null(value: Any) -> bool:
    if value is None:
        return True
    if not _nonblank(value, 2_048):
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _storage_key(value: Any) -> bool:
    if not _nonblank(value, 500) or value.startswith("/"):
        return False
    path = PurePosixPath(value)
    return path.as_posix() == value and ".." not in path.parts and len(path.parts) > 1


def parse_source_dna_manifest(raw: bytes, expected_library_id: str | None = None) -> SourceDnaLibrary:
    if not raw or len(raw) > MAX_MANIFEST_BYTES:
        raise SourceDnaError("source DNA manifest is empty or too large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceDnaError("source DNA manifest is not valid UTF-8 JSON") from error
    if not isinstance(value, dict) or set(value) != TOP_FIELDS or value.get("schema") != SCHEMA:
        raise SourceDnaError("source DNA manifest fields or schema are invalid")
    library_id = value.get("libraryId")
    format_slug = value.get("format")
    page_id = value.get("pageId")
    if not isinstance(library_id, str) or not TOKEN.fullmatch(library_id):
        raise SourceDnaError("source DNA libraryId is invalid")
    if expected_library_id is not None and library_id != expected_library_id:
        raise SourceDnaError("source DNA libraryId does not match its filename")
    if not isinstance(format_slug, str) or not TOKEN.fullmatch(format_slug):
        raise SourceDnaError("source DNA format is invalid")
    if not _nonblank(page_id, 128) or not re.fullmatch(r"[A-Za-z0-9._:-]+", page_id):
        raise SourceDnaError("source DNA pageId is invalid")
    rows = value.get("masters")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_MASTERS:
        raise SourceDnaError("source DNA masters must be a non-empty bounded list")
    masters: list[MasterSource] = []
    source_ids: set[str] = set()
    hashes: set[str] = set()
    storage_keys: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != MASTER_FIELDS:
            raise SourceDnaError(f"source DNA masters[{index}] fields are invalid")
        source_id = row.get("sourceId")
        sha256 = row.get("sha256")
        byte_count = row.get("bytes")
        filename = row.get("filename")
        mime_type = row.get("mimeType")
        storage_key = row.get("storageKey")
        duration_ms = row.get("durationMs")
        source_offset_ms = row.get("sourceOffsetMs")
        provenance = row.get("provenance")
        if not isinstance(source_id, str) or not TOKEN.fullmatch(source_id) or source_id in source_ids:
            raise SourceDnaError(f"source DNA masters[{index}].sourceId is invalid or duplicated")
        if not isinstance(sha256, str) or not SHA256.fullmatch(sha256) or sha256 in hashes:
            raise SourceDnaError(f"source DNA masters[{index}].sha256 is invalid or duplicated")
        if isinstance(byte_count, bool) or not isinstance(byte_count, int) or not 1 <= byte_count <= MAX_SOURCE_BYTES:
            raise SourceDnaError(f"source DNA masters[{index}].bytes is invalid")
        if not isinstance(filename, str) or not FILENAME.fullmatch(filename) or Path(filename).name != filename:
            raise SourceDnaError(f"source DNA masters[{index}].filename is invalid")
        if mime_type not in MIME_TYPES or not _storage_key(storage_key) or storage_key in storage_keys:
            raise SourceDnaError(f"source DNA masters[{index}] media identity is invalid or duplicated")
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or not 1 <= duration_ms <= MAX_DURATION_MS:
            raise SourceDnaError(f"source DNA masters[{index}].durationMs is invalid")
        if (
            isinstance(source_offset_ms, bool)
            or not isinstance(source_offset_ms, int)
            or not 0 <= source_offset_ms <= MAX_DURATION_MS
            or source_offset_ms + duration_ms > MAX_DURATION_MS
        ):
            raise SourceDnaError(f"source DNA masters[{index}].sourceOffsetMs is invalid")
        if not isinstance(provenance, dict) or set(provenance) != PROVENANCE_FIELDS:
            raise SourceDnaError(f"source DNA masters[{index}].provenance fields are invalid")
        if not _https_or_null(provenance.get("sourceUrl")) \
                or not _nonblank(provenance.get("acquiredAt"), 100) \
                or not _nonblank(provenance.get("authority"), 500):
            raise SourceDnaError(f"source DNA masters[{index}].provenance is incomplete")
        source_ids.add(source_id)
        hashes.add(sha256)
        storage_keys.add(storage_key)
        masters.append(MasterSource(
            source_id, sha256, byte_count, filename, mime_type, storage_key,
            duration_ms, source_offset_ms, dict(provenance),
        ))
    if [item.source_id for item in masters] != sorted(source_ids):
        raise SourceDnaError("source DNA masters must be sorted by sourceId")
    return SourceDnaLibrary(
        library_id=library_id,
        format_slug=format_slug,
        page_id=page_id,
        masters=tuple(masters),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def load_source_dna_library(library_id: str) -> SourceDnaLibrary:
    if not isinstance(library_id, str) or not TOKEN.fullmatch(library_id):
        raise SourceDnaError("source DNA libraryId is invalid")
    try:
        raw = (MANIFEST_DIR / f"{library_id}.json").read_bytes()
    except OSError as error:
        raise SourceDnaError("source DNA library is unavailable") from error
    return parse_source_dna_manifest(raw, library_id)


def source_dna_catalog_hash() -> str:
    """Hash the complete valid master registry, including page scope."""
    rows: list[str] = []
    for path in sorted(MANIFEST_DIR.glob("*.json")):
        try:
            library = parse_source_dna_manifest(path.read_bytes(), path.stem)
        except (OSError, SourceDnaError):
            continue
        rows.append(f"{library.library_id}:{library.sha256}")
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()
