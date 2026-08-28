"""Fail-closed commissioning for hash-pinned 8791 source libraries.

A registered recipe marker alone never exposes a capability. Source bytes are
uploaded into a manifest-specific staging directory, verified independently,
and linked into the final project. The exact manifest bytes become
``prompts.json`` only after every clip is present; that last atomic create is
the capability boundary used by the existing recipe catalog.
"""

from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
from typing import Any

from project_manager import PROJECTS_DIR


MANIFEST_SCHEMA = "content-lab.source-library-manifest.v1"
RESPONSE_SCHEMA = "content-lab.source-library-response.v1"
MANIFEST_DIR = Path(__file__).resolve().parents[1] / "recipes/source-libraries"
TOP_FIELDS = {"schema", "libraryId", "format", "authority", "clips"}
AUTHORITY_FIELDS = {
    "system", "clipBankPath", "clipBankSha256", "selection",
}
CLIP_FIELDS = {"sha256", "bytes", "filename", "railPath"}
TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.mp4$")
MAX_MANIFEST_BYTES = 1_048_576
MAX_CLIPS = 500
MAX_CLIP_BYTES = 5_000_000_000
MAX_LIBRARY_BYTES = 100_000_000_000


class SourceLibraryError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class SourceClip:
    sha256: str
    bytes: int
    filename: str
    rail_path: str

    @property
    def stored_name(self) -> str:
        return f"{self.sha256}.mp4"


@dataclass(frozen=True)
class SourceLibraryManifest:
    library_id: str
    format_slug: str
    authority: dict[str, str]
    clips: tuple[SourceClip, ...]
    raw: bytes
    sha256: str

    @property
    def recipe_version(self) -> str:
        return "v" + self.sha256[:12]

    @property
    def total_bytes(self) -> int:
        return sum(clip.bytes for clip in self.clips)

    @property
    def by_sha256(self) -> dict[str, SourceClip]:
        return {clip.sha256: clip for clip in self.clips}


def _manifest_error(detail: str) -> SourceLibraryError:
    return SourceLibraryError(503, f"source library manifest is invalid: {detail}")


def _safe_library_id(value: Any) -> str:
    if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
        raise SourceLibraryError(400, "library_id must be a safe bounded token")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_manifest(raw: bytes, expected_library_id: str) -> SourceLibraryManifest:
    if not raw or len(raw) > MAX_MANIFEST_BYTES:
        raise _manifest_error("manifest bytes are empty or too large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _manifest_error("manifest is not valid UTF-8 JSON") from error
    if not isinstance(value, dict) or set(value) != TOP_FIELDS:
        raise _manifest_error("top-level fields do not match the contract")
    if value.get("schema") != MANIFEST_SCHEMA:
        raise _manifest_error("schema mismatch")
    library_id = value.get("libraryId")
    format_slug = value.get("format")
    if (
        not isinstance(library_id, str)
        or not TOKEN_RE.fullmatch(library_id)
        or library_id != expected_library_id
    ):
        raise _manifest_error("libraryId does not match the requested manifest")
    if not isinstance(format_slug, str) or not TOKEN_RE.fullmatch(format_slug):
        raise _manifest_error("format must be a safe bounded token")

    authority = value.get("authority")
    if not isinstance(authority, dict) or set(authority) != AUTHORITY_FIELDS:
        raise _manifest_error("authority fields do not match the contract")
    if authority.get("system") != "8791":
        raise _manifest_error("authority.system must be 8791")
    if authority.get("clipBankPath") != "out/clip_bank.json":
        raise _manifest_error("authority.clipBankPath must be out/clip_bank.json")
    if not isinstance(authority.get("clipBankSha256"), str) or not SHA256_RE.fullmatch(
        authority["clipBankSha256"]
    ):
        raise _manifest_error("authority.clipBankSha256 must be lowercase SHA-256")
    selection = authority.get("selection")
    if not isinstance(selection, str) or not selection.strip() or len(selection) > 500:
        raise _manifest_error("authority.selection must be nonblank and bounded")

    clip_values = value.get("clips")
    if not isinstance(clip_values, list) or not 1 <= len(clip_values) <= MAX_CLIPS:
        raise _manifest_error(f"clips must contain 1 through {MAX_CLIPS} rows")
    clips: list[SourceClip] = []
    seen_hashes: set[str] = set()
    seen_paths: set[str] = set()
    for index, item in enumerate(clip_values):
        if not isinstance(item, dict) or set(item) != CLIP_FIELDS:
            raise _manifest_error(f"clips[{index}] fields do not match the contract")
        sha256 = item.get("sha256")
        byte_count = item.get("bytes")
        filename = item.get("filename")
        rail_path = item.get("railPath")
        if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
            raise _manifest_error(f"clips[{index}].sha256 is invalid")
        if sha256 in seen_hashes:
            raise _manifest_error(f"clips[{index}].sha256 is duplicated")
        if (
            isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or not 1 <= byte_count <= MAX_CLIP_BYTES
        ):
            raise _manifest_error(f"clips[{index}].bytes is invalid")
        if not isinstance(filename, str) or not FILENAME_RE.fullmatch(filename):
            raise _manifest_error(f"clips[{index}].filename is invalid")
        if Path(filename).name != filename:
            raise _manifest_error(f"clips[{index}].filename must be a basename")
        expected_path = f"out/clips/{format_slug}/{filename}"
        if (
            not isinstance(rail_path, str)
            or rail_path != expected_path
            or PurePosixPath(rail_path).as_posix() != rail_path
            or ".." in PurePosixPath(rail_path).parts
        ):
            raise _manifest_error(f"clips[{index}].railPath is invalid")
        if rail_path in seen_paths:
            raise _manifest_error(f"clips[{index}].railPath is duplicated")
        seen_hashes.add(sha256)
        seen_paths.add(rail_path)
        clips.append(SourceClip(sha256, byte_count, filename, rail_path))
    if [clip.sha256 for clip in clips] != sorted(seen_hashes):
        raise _manifest_error("clips must be ordered by sha256")
    if sum(clip.bytes for clip in clips) > MAX_LIBRARY_BYTES:
        raise _manifest_error("library byte total exceeds the commissioning ceiling")

    return SourceLibraryManifest(
        library_id=library_id,
        format_slug=format_slug,
        authority={key: str(authority[key]) for key in sorted(authority)},
        clips=tuple(clips),
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def load_source_library_manifest(library_id: str) -> SourceLibraryManifest:
    safe_id = _safe_library_id(library_id)
    path = MANIFEST_DIR / f"{safe_id}.json"
    try:
        raw = path.read_bytes()
    except FileNotFoundError as error:
        raise SourceLibraryError(404, "source library manifest not found") from error
    except OSError as error:
        raise SourceLibraryError(503, "source library manifest is unavailable") from error
    return _parse_manifest(raw, safe_id)


def _project_root(manifest: SourceLibraryManifest) -> Path:
    projects_root = Path(PROJECTS_DIR).resolve()
    candidate = projects_root / manifest.library_id
    if candidate.is_symlink():
        raise SourceLibraryError(409, "source library project may not be a symlink")
    resolved = candidate.resolve()
    if resolved.parent != projects_root:
        raise SourceLibraryError(409, "source library project escaped projects root")
    return candidate


def _paths(manifest: SourceLibraryManifest) -> tuple[Path, Path, Path, Path]:
    project = _project_root(manifest)
    staging = project / ".commissioning" / manifest.sha256
    videos = project / "videos"
    prompts = project / "prompts.json"
    return project, staging, videos, prompts


def _exact_clip(path: Path, clip: SourceClip) -> bool:
    try:
        return (
            not path.is_symlink()
            and path.is_file()
            and path.stat().st_size == clip.bytes
            and _sha256_file(path) == clip.sha256
        )
    except OSError:
        return False


def _exact_bytes(path: Path, expected: bytes) -> bool:
    try:
        return not path.is_symlink() and path.is_file() and path.read_bytes() == expected
    except OSError:
        return False


def _assert_project_structure(manifest: SourceLibraryManifest) -> None:
    project, staging, videos, prompts = _paths(manifest)
    if not project.exists():
        return
    if project.is_symlink() or not project.is_dir():
        raise SourceLibraryError(409, "source library project is not a regular directory")
    allowed_root = {".commissioning", "videos", "prompts.json"}
    if any(child.name not in allowed_root for child in project.iterdir()):
        raise SourceLibraryError(409, "source library project contains divergent content")

    commissioning = project / ".commissioning"
    if commissioning.exists():
        if commissioning.is_symlink() or not commissioning.is_dir():
            raise SourceLibraryError(409, "source library staging root is divergent")
        children = list(commissioning.iterdir())
        if any(child.name != manifest.sha256 for child in children):
            raise SourceLibraryError(409, "source library has a different staged manifest")
    expected_names = {clip.stored_name: clip for clip in manifest.clips}
    if staging.exists():
        if staging.is_symlink() or not staging.is_dir():
            raise SourceLibraryError(409, "source library staging directory is divergent")
        for path in staging.iterdir():
            if (
                expected_names.get(path.name) is None
                or path.is_symlink()
                or not path.is_file()
            ):
                raise SourceLibraryError(409, "source library staging structure is divergent")
    if videos.exists():
        if videos.is_symlink() or not videos.is_dir():
            raise SourceLibraryError(409, "source library videos directory is divergent")
        for path in videos.iterdir():
            if (
                expected_names.get(path.name) is None
                or path.is_symlink()
                or not path.is_file()
            ):
                raise SourceLibraryError(409, "source library videos structure is divergent")
    if prompts.exists() and (prompts.is_symlink() or not prompts.is_file()):
        raise SourceLibraryError(409, "source library prompts.json is not a regular file")


def _assert_project_shape(manifest: SourceLibraryManifest) -> None:
    _assert_project_structure(manifest)
    _, staging, videos, prompts = _paths(manifest)
    for clip in manifest.clips:
        staged = staging / clip.stored_name
        final = videos / clip.stored_name
        if staged.exists() and not _exact_clip(staged, clip):
            raise SourceLibraryError(409, "source library staging contains divergent bytes")
        if final.exists() and not _exact_clip(final, clip):
            raise SourceLibraryError(409, "source library project contains divergent video bytes")
    if prompts.exists() and not _exact_bytes(prompts, manifest.raw):
        raise SourceLibraryError(409, "source library prompts.json contains divergent bytes")


def _ensure_staging(manifest: SourceLibraryManifest) -> Path:
    project, staging, _, _ = _paths(manifest)
    Path(PROJECTS_DIR).resolve().mkdir(parents=True, exist_ok=True, mode=0o700)
    project.mkdir(mode=0o700, exist_ok=True)
    commissioning = project / ".commissioning"
    commissioning.mkdir(mode=0o700, exist_ok=True)
    staging.mkdir(mode=0o700, exist_ok=True)
    return staging


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _link_exact_once(source: Path, target: Path, *, existing_ok) -> None:
    try:
        os.link(source, target)
    except FileExistsError:
        if not existing_ok(target):
            raise SourceLibraryError(409, "source library target contains divergent bytes")
    else:
        os.chmod(target, 0o600)
        _fsync_directory(target.parent)


def _source_clip_receipt(
    manifest: SourceLibraryManifest,
    clip: SourceClip,
    *,
    status: str,
) -> dict[str, Any]:
    return {
        "schema": RESPONSE_SCHEMA,
        "libraryId": manifest.library_id,
        "manifestSha256": "sha256:" + manifest.sha256,
        "recipeVersion": manifest.recipe_version,
        "clipSha256": clip.sha256,
        "bytes": clip.bytes,
        "status": status,
    }


async def stage_source_clip(
    manifest: SourceLibraryManifest,
    clip_sha256: str,
    chunks: AsyncIterable[bytes],
    *,
    content_length: int,
) -> dict[str, Any]:
    clip = manifest.by_sha256.get(clip_sha256)
    if clip is None:
        raise SourceLibraryError(404, "clip is not present in the source manifest")
    if content_length != clip.bytes:
        raise SourceLibraryError(409, "Content-Length does not match the source manifest")
    _assert_project_structure(manifest)
    _, _, videos, prompts = _paths(manifest)
    final_path = videos / clip.stored_name
    if prompts.exists() or final_path.exists():
        if _exact_clip(final_path, clip) and _exact_bytes(prompts, manifest.raw):
            return _source_clip_receipt(manifest, clip, status="finalized")
        raise SourceLibraryError(409, "finalized source library may not be changed")

    staging = _ensure_staging(manifest)
    target = staging / clip.stored_name
    if target.exists():
        if _exact_clip(target, clip):
            return _source_clip_receipt(manifest, clip, status="staged")
        raise SourceLibraryError(409, "staged source clip contains divergent bytes")
    temporary = staging / f".upload-{clip.sha256}-{secrets.token_hex(8)}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    digest = hashlib.sha256()
    written = 0
    try:
        with os.fdopen(descriptor, "wb") as handle:
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise SourceLibraryError(400, "source clip body yielded non-bytes data")
                if not chunk:
                    continue
                written += len(chunk)
                if written > clip.bytes:
                    raise SourceLibraryError(413, "source clip body exceeds manifest bytes")
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if written != clip.bytes or digest.hexdigest() != clip.sha256:
            raise SourceLibraryError(409, "source clip bytes do not match the manifest")
        _link_exact_once(
            temporary,
            target,
            existing_ok=lambda path: _exact_clip(path, clip),
        )
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return _source_clip_receipt(manifest, clip, status="staged")


def finalize_source_library(manifest: SourceLibraryManifest) -> dict[str, Any]:
    _assert_project_shape(manifest)
    project, staging, videos, prompts = _paths(manifest)
    if prompts.exists():
        status = source_library_status(manifest)
        if status["finalized"]:
            return status
        raise SourceLibraryError(409, "source library capability exists without exact final bytes")
    if not project.exists() or not staging.is_dir():
        raise SourceLibraryError(409, "source library has no staged clips")
    missing = [
        clip.sha256
        for clip in manifest.clips
        if not _exact_clip(staging / clip.stored_name, clip)
        and not _exact_clip(videos / clip.stored_name, clip)
    ]
    if missing:
        raise SourceLibraryError(
            409, f"source library is incomplete: {len(missing)} clips are missing",
        )
    videos.mkdir(mode=0o700, exist_ok=True)
    for clip in manifest.clips:
        target = videos / clip.stored_name
        if _exact_clip(target, clip):
            continue
        _link_exact_once(
            staging / clip.stored_name,
            target,
            existing_ok=lambda path, expected=clip: _exact_clip(path, expected),
        )

    temporary = project / f".prompts-{manifest.sha256}-{secrets.token_hex(8)}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(manifest.raw)
            handle.flush()
            os.fsync(handle.fileno())
        _link_exact_once(
            temporary,
            prompts,
            existing_ok=lambda path: _exact_bytes(path, manifest.raw),
        )
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    _assert_project_shape(manifest)
    status = source_library_status(manifest)
    if not status["finalized"]:
        raise SourceLibraryError(409, "source library final verification failed")
    return status
def source_library_status(manifest: SourceLibraryManifest) -> dict[str, Any]:
    _assert_project_shape(manifest)
    _, staging, videos, prompts = _paths(manifest)
    staged_hashes = [
        clip.sha256
        for clip in manifest.clips
        if _exact_clip(staging / clip.stored_name, clip)
    ]
    final_hashes = [
        clip.sha256
        for clip in manifest.clips
        if _exact_clip(videos / clip.stored_name, clip)
    ]
    present_hashes = set(staged_hashes) | set(final_hashes)
    manifest_live = _exact_bytes(prompts, manifest.raw)
    finalized = manifest_live and len(final_hashes) == len(manifest.clips)
    return {
        "schema": RESPONSE_SCHEMA,
        "libraryId": manifest.library_id,
        "format": manifest.format_slug,
        "manifestSha256": "sha256:" + manifest.sha256,
        "recipeVersion": manifest.recipe_version,
        "clipCount": len(manifest.clips),
        "totalBytes": manifest.total_bytes,
        "stagedClips": len(staged_hashes),
        "stagedClipSha256": staged_hashes,
        "finalClips": len(final_hashes),
        "finalClipSha256": final_hashes,
        "missingClips": len(manifest.clips) - len(present_hashes),
        "manifestLive": manifest_live,
        "finalized": finalized,
    }
