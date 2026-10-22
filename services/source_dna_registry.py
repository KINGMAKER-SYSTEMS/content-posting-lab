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
# A master's place in its page's pool (Eric 2026-10-22). Absent means primary:
# the page's main footage, cut as much as possible. A backup is cut only when
# the primaries have no fresh window, or as an occasional variety cut
# (services/control_plane_sources.py plan_source_cuts). A backup is added only
# by a reviewed commit to this registry; nothing automatic writes it.
MASTER_ROLES = ("primary", "backup")
# A backup master may declare its own brightness (Eric 2026-10-22: backups
# render at 0.65 where the page recipe says otherwise). Brightness only, inside
# GRADE_OVERRIDE_BOUNDS; applied at cut time on clip-varying pages and written
# into the clip's receipt. The master file itself is never re-encoded.
GRADE_OVERRIDE_BOUNDS = {"brightness": (0.4, 1.0)}
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
    role: str = "primary"
    grade_override: dict[str, float] | None = None


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
        if not isinstance(row, dict) or set(row) - {"role", "gradeOverride"} != MASTER_FIELDS:
            raise SourceDnaError(f"source DNA masters[{index}] fields are invalid")
        role = row.get("role", "primary")
        if role not in MASTER_ROLES:
            raise SourceDnaError(f"source DNA masters[{index}].role is invalid")
        grade_override = _grade_override(row.get("gradeOverride"), role, index)
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
            duration_ms, source_offset_ms, dict(provenance), role, grade_override,
        ))
    if [item.source_id for item in masters] != sorted(source_ids):
        raise SourceDnaError("source DNA masters must be sorted by sourceId")
    if not any(item.role == "primary" for item in masters):
        raise SourceDnaError("source DNA library needs at least one primary master")
    return SourceDnaLibrary(
        library_id=library_id,
        format_slug=format_slug,
        page_id=page_id,
        masters=tuple(masters),
        sha256=_primary_version(value, raw),
    )


def _grade_override(value: Any, role: str, index: int) -> dict[str, float] | None:
    if value is None:
        return None
    if (role != "backup" or not isinstance(value, dict) or not value
            or set(value) - set(GRADE_OVERRIDE_BOUNDS)
            or any(isinstance(item, bool) or not isinstance(item, (int, float))
                   or not GRADE_OVERRIDE_BOUNDS[name][0] <= item <= GRADE_OVERRIDE_BOUNDS[name][1]
                   for name, item in value.items())):
        raise SourceDnaError(f"source DNA masters[{index}].gradeOverride must be a backup's brightness from 0.4 to 1.0")
    return {name: float(item) for name, item in value.items()}


def _primary_version(value: dict, raw: bytes) -> str:
    """The library version, pinned to its primary masters only.

    Adding or removing a backup must not move the version: the Worker binds a
    page's clips and planned posts to it (pageSourceLockstep), so a moved
    version would read every current clip as an old source. A library with no
    backup hashes its exact bytes, as before; one with backups hashes the same
    canonical form (sorted keys, compact, trailing newline: the registry's own
    format) with the backups and every role key left out.
    """
    rows = value["masters"]
    if all(row.get("role", "primary") == "primary" and "role" not in row for row in rows):
        return hashlib.sha256(raw).hexdigest()
    primary = {**value, "masters": [
        {key: item for key, item in row.items() if key != "role"}
        for row in rows if row.get("role", "primary") == "primary"
    ]}
    return hashlib.sha256(
        (json.dumps(primary, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    ).hexdigest()


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


def source_dna_master_hashes() -> set[str]:
    """Every master SHA named by a locally-registered source DNA manifest.

    The ``_source_dna`` byte cache pins these files so a currently-registered
    master never has to be re-downloaded. A master that drops out of every
    local manifest (replaced by a later revision) is evictable once no active
    job references it. ShipStream manifests are fetched over the network and
    are deliberately not walked here: their masters are still pinned by any
    active job's ``sourceCuts``, and a network read must never gate eviction.
    """
    hashes: set[str] = set()
    for path in sorted(MANIFEST_DIR.glob("*.json")):
        try:
            library = parse_source_dna_manifest(path.read_bytes(), path.stem)
        except (OSError, SourceDnaError):
            continue
        hashes.update(master.sha256 for master in library.masters)
    return hashes
