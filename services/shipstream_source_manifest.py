"""Project an exact ShipStream page source manifest into Content Lab.

Master Pages selects the page and its vault. ShipStream owns the immutable
source bytes and their page-scoped lineage. This adapter joins those existing
authorities into the same ``SourceDnaLibrary`` contract used by the Dossier and
the source recut executor; it does not invent or persist another registry.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote, unquote, urlparse

import httpx

from services.source_dna_registry import (
    SourceDnaLibrary,
    SourceDnaError,
    parse_source_dna_manifest,
)


SHIPSTREAM_ORIGIN = "https://shipstream.risingtidesviral.com"
MANIFEST_SCHEMA = "shipstream.source-manifest.v1"
HANDLE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_MANIFEST_BYTES = 2_097_152
MAX_SOURCES = 500
MAX_CUTS = 500
MAX_MEDIA_BYTES = 20_000_000_000


class ShipStreamSourceError(ValueError):
    pass


class ShipStreamSourceMissing(ShipStreamSourceError):
    pass


class ShipStreamSourceUnavailable(ShipStreamSourceError):
    pass


class ShipStreamApprovedCutsError(ShipStreamSourceError):
    pass


@dataclass(frozen=True)
class ShipStreamApprovedCut:
    ordinal: int
    sha256: str
    bytes: int
    storage_key: str
    parent_sha256: str
    parent_type: str
    source_start_ms: int
    source_duration_ms: int
    output_duration_ms: int
    playback_speed: float
    uploaded_at: str
    review: str
    media: dict[str, Any]


@dataclass(frozen=True)
class ShipStreamApprovedCutLibrary:
    library_id: str
    format_slug: str
    page_id: str
    cuts: tuple[ShipStreamApprovedCut, ...]
    sha256: str


@dataclass(frozen=True)
class ShipStreamSourceProjection:
    source_library: SourceDnaLibrary
    approved_cut_library: ShipStreamApprovedCutLibrary | None
    approved_cut_status: str


def _nonblank(value: Any, maximum: int = 2_048) -> bool:
    return isinstance(value, str) and value == value.strip() and 0 < len(value) <= maximum


def _https(value: Any) -> bool:
    if not _nonblank(value):
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username and not parsed.password


def _vault_handle(master_pages: dict[str, Any]) -> str:
    handle = master_pages.get("handle")
    vault_url = master_pages.get("vaultUrl")
    if not isinstance(handle, str) or not HANDLE.fullmatch(handle):
        raise ShipStreamSourceError("ShipStream page handle is invalid")
    if not isinstance(vault_url, str):
        raise ShipStreamSourceError("ShipStream vault URL is missing")
    parsed = urlparse(vault_url)
    try:
        path_handle = unquote(parsed.path.removeprefix("/vault/").rstrip("/"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ShipStreamSourceError("ShipStream vault URL is invalid") from error
    if (
        parsed.scheme != "https"
        or parsed.netloc != "shipstream.risingtidesviral.com"
        or not parsed.path.startswith("/vault/")
        or parsed.params
        or parsed.query
        or parsed.fragment
        or path_handle.lower() != handle.lower()
    ):
        raise ShipStreamSourceError("ShipStream vault URL does not match the page")
    return handle


def source_manifest_url(handle: str) -> str:
    key = quote(f"vault/{handle}/source-manifest.json", safe="")
    return f"{SHIPSTREAM_ORIGIN}/assets/{key}"


def _fetch_manifest(url: str) -> bytes:
    try:
        with httpx.stream(
            "GET",
            url,
            headers={"accept": "application/json"},
            timeout=2.0,
            follow_redirects=False,
        ) as response:
            if response.status_code == 404:
                raise ShipStreamSourceMissing("ShipStream source manifest is missing")
            response.raise_for_status()
            advertised = response.headers.get("content-length")
            if advertised is not None:
                try:
                    if int(advertised) > MAX_MANIFEST_BYTES:
                        raise ShipStreamSourceError("ShipStream source manifest is too large")
                except ValueError as error:
                    raise ShipStreamSourceError(
                        "ShipStream source manifest content length is invalid"
                    ) from error
            chunks = bytearray()
            for chunk in response.iter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_MANIFEST_BYTES:
                    raise ShipStreamSourceError("ShipStream source manifest is too large")
    except ShipStreamSourceError:
        raise
    except httpx.HTTPError as error:
        raise ShipStreamSourceUnavailable(
            "ShipStream source manifest is unavailable"
        ) from error
    raw = bytes(chunks)
    if not raw:
        raise ShipStreamSourceError("ShipStream source manifest is empty")
    return raw


def _milliseconds(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ShipStreamSourceError(f"{label} is invalid")
    result = round(float(value) * 1_000)
    if result < 1 or result > 86_400_000:
        raise ShipStreamSourceError(f"{label} is outside the supported duration")
    return result


def _milliseconds_allow_zero(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ShipStreamSourceError(f"{label} is invalid")
    result = round(float(value) * 1_000)
    if result < 0 or result > 86_400_000:
        raise ShipStreamSourceError(f"{label} is outside the supported duration")
    return result


def _approved_cut_library(
    value: dict[str, Any],
    source_library: SourceDnaLibrary,
    *,
    handle: str,
) -> ShipStreamApprovedCutLibrary | None:
    rows = value.get("cuts")
    if not isinstance(rows, list) or len(rows) > MAX_CUTS:
        raise ShipStreamSourceError("ShipStream approved cuts are invalid or too large")
    if not rows:
        return None

    source_by_sha = {source.sha256: source for source in source_library.masters}
    authority = value.get("sourceAuthority")
    historical = (
        isinstance(authority, dict)
        and authority.get("kind") == "historical_posted_cut_recovery"
    )
    cuts: list[ShipStreamApprovedCut] = []
    ordinals: set[int] = set()
    hashes: set[str] = set()
    storage_keys: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ShipStreamSourceError(f"ShipStream approved cuts[{index}] is invalid")
        ordinal = row.get("ordinal")
        sha256 = row.get("sha256")
        storage_key = row.get("storageKey")
        parent_sha256 = row.get("parentSha256")
        parent_type = row.get("parentType")
        media = row.get("media")
        uploaded_at = row.get("uploadedAt")
        review = row.get("review")
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal < 0
            or ordinal > 2_147_483_647
            or ordinal in ordinals
            or not isinstance(sha256, str)
            or not SHA256.fullmatch(sha256)
            or sha256 in hashes
            or sha256 in source_by_sha
            or storage_key != f"vault/{handle}/pool/{sha256}.mp4"
            or storage_key in storage_keys
            or not isinstance(parent_sha256, str)
            or parent_sha256 not in source_by_sha
            or not isinstance(media, dict)
            or row.get("status") != "ready"
            or not _nonblank(uploaded_at, 100)
            or not _nonblank(review, 500)
        ):
            raise ShipStreamSourceError(
                f"ShipStream approved cuts[{index}] identity is invalid"
            )
        expected_parent_type = "historical_posted_cut" if historical else "page_master"
        # The original page-master producer predates parentType. Its only legal
        # parent is still the exact page master selected above.
        if parent_type is None and not historical:
            parent_type = "page_master"
        if parent_type != expected_parent_type:
            raise ShipStreamSourceError(
                f"ShipStream approved cuts[{index}] parent type is invalid"
            )

        source_start_ms = _milliseconds_allow_zero(
            row.get("sourceStartSeconds"),
            f"ShipStream approved cuts[{index}] source start",
        )
        source_duration_ms = _milliseconds(
            row.get("sourceDurationSeconds"),
            f"ShipStream approved cuts[{index}] source duration",
        )
        output_duration_ms = _milliseconds(
            row.get("outputDurationSeconds"),
            f"ShipStream approved cuts[{index}] output duration",
        )
        playback_speed = row.get("playbackSpeed")
        byte_count = media.get("bytes")
        fps = media.get("fps")
        if (
            isinstance(playback_speed, bool)
            or not isinstance(playback_speed, (int, float))
            or not math.isfinite(playback_speed)
            or not 0.1 <= float(playback_speed) <= 10.0
            or isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or not 1 <= byte_count <= MAX_MEDIA_BYTES
            or media.get("width") != 1080
            or media.get("height") != 1920
            or media.get("videoCodec") != "h264"
            or media.get("pixelFormat") != "yuv420p"
            or media.get("audioStreams") != 0
            or isinstance(fps, bool)
            or not isinstance(fps, (int, float))
            or not math.isfinite(fps)
            or abs(float(fps) - 30.0) > 0.01
        ):
            raise ShipStreamSourceError(
                f"ShipStream approved cuts[{index}] media is invalid"
            )
        media_duration_ms = _milliseconds(
            media.get("durationSeconds"),
            f"ShipStream approved cuts[{index}] media duration",
        )
        parent = source_by_sha[parent_sha256]
        if (
            source_start_ms + source_duration_ms > parent.duration_ms + 100
            or abs(source_duration_ms - round(output_duration_ms * float(playback_speed))) > 5
            or abs(media_duration_ms - output_duration_ms) > 250
        ):
            raise ShipStreamSourceError(
                f"ShipStream approved cuts[{index}] timing is invalid"
            )

        ordinals.add(ordinal)
        hashes.add(sha256)
        storage_keys.add(storage_key)
        cuts.append(ShipStreamApprovedCut(
            ordinal=ordinal,
            sha256=sha256,
            bytes=byte_count,
            storage_key=storage_key,
            parent_sha256=parent_sha256,
            parent_type=parent_type,
            source_start_ms=source_start_ms,
            source_duration_ms=source_duration_ms,
            output_duration_ms=output_duration_ms,
            playback_speed=float(playback_speed),
            uploaded_at=uploaded_at,
            review=review,
            media=dict(media),
        ))
    cuts.sort(key=lambda cut: cut.ordinal)
    canonical = json.dumps(
        [{
            "ordinal": cut.ordinal,
            "sha256": cut.sha256,
            "bytes": cut.bytes,
            "storageKey": cut.storage_key,
            "parentSha256": cut.parent_sha256,
            "parentType": cut.parent_type,
            "sourceStartMs": cut.source_start_ms,
            "sourceDurationMs": cut.source_duration_ms,
            "outputDurationMs": cut.output_duration_ms,
            "playbackSpeed": cut.playback_speed,
            "uploadedAt": cut.uploaded_at,
            "review": cut.review,
            "media": cut.media,
        } for cut in cuts],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()
    return ShipStreamApprovedCutLibrary(
        library_id=f"{source_library.library_id}-cuts",
        format_slug=source_library.format_slug,
        page_id=source_library.page_id,
        cuts=tuple(cuts),
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _master_from_page_master(row: Any, handle: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ShipStreamSourceError("ShipStream page master is invalid")
    sha256 = row.get("sha256")
    storage_key = row.get("storageKey")
    byte_count = row.get("bytes")
    media = row.get("media")
    registered_at = row.get("registeredAt")
    origin_window = row.get("originWindowSeconds")
    if (
        not isinstance(sha256, str)
        or not SHA256.fullmatch(sha256)
        or storage_key != f"vault/{handle}/masters/{sha256}.mp4"
        or isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or byte_count < 1
        or not isinstance(media, dict)
        or not _nonblank(registered_at, 100)
    ):
        raise ShipStreamSourceError("ShipStream page master identity is invalid")
    duration_ms = _milliseconds(media.get("durationSeconds"), "ShipStream page master duration")
    if isinstance(origin_window, list) and len(origin_window) == 2:
        start, end = origin_window
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not math.isfinite(start)
            or not math.isfinite(end)
            or float(start) < 0
            or float(end) <= float(start)
        ):
            raise ShipStreamSourceError("ShipStream page master origin window is invalid")
    source_url = row.get("originSourceUrl")
    if source_url is not None and not _https(source_url):
        raise ShipStreamSourceError("ShipStream page master source URL is invalid")
    return {
        "sourceId": f"shipstream-{sha256}",
        "sha256": sha256,
        "bytes": byte_count,
        "filename": f"{sha256}.mp4",
        "mimeType": "video/mp4",
        "storageKey": storage_key,
        "durationMs": duration_ms,
        # ``storageKey`` is the already-extracted page master. The origin
        # window describes its lineage in the upstream file, not an offset to
        # apply again while cutting these registered bytes.
        "sourceOffsetMs": 0,
        "provenance": {
            "sourceUrl": source_url or f"{SHIPSTREAM_ORIGIN}/assets/{quote(storage_key, safe='')}",
            "acquiredAt": registered_at,
            "authority": "ShipStream source-manifest.v1 exact page master",
        },
    }


def _master_from_historical_cut(row: Any, handle: str, notion_page_id: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ShipStreamSourceError("ShipStream historical source is invalid")
    sha256 = row.get("sha256")
    storage_key = row.get("storageKey")
    byte_count = row.get("bytes")
    media = row.get("media")
    uploaded_at = row.get("uploadedAt")
    if (
        row.get("type") != "historical_posted_cut"
        or row.get("pageHandle") != handle
        or row.get("notionPageId") != notion_page_id
        or not isinstance(sha256, str)
        or not SHA256.fullmatch(sha256)
        or storage_key != f"vault/{handle}/pool/{sha256}.mp4"
        or isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or byte_count < 1
        or not isinstance(media, dict)
        or not _nonblank(uploaded_at, 100)
    ):
        raise ShipStreamSourceError("ShipStream historical source identity is invalid")
    return {
        "sourceId": f"history-{sha256}",
        "sha256": sha256,
        "bytes": byte_count,
        "filename": f"{sha256}.mp4",
        "mimeType": "video/mp4",
        "storageKey": storage_key,
        "durationMs": _milliseconds(media.get("durationSeconds"), "ShipStream historical source duration"),
        "sourceOffsetMs": 0,
        "provenance": {
            "sourceUrl": f"{SHIPSTREAM_ORIGIN}/assets/{quote(storage_key, safe='')}",
            "acquiredAt": uploaded_at,
            "authority": (
                "ShipStream source-manifest.v1 page-bound historical posted cut; "
                "original source unavailable"
            ),
        },
    }


def parse_shipstream_source_manifest(
    raw: bytes,
    master_pages: dict[str, Any],
    *,
    page_id: str,
    expected_format: str | None = None,
    expected_library_id: str | None = None,
) -> SourceDnaLibrary:
    handle = _vault_handle(master_pages)
    if not raw or len(raw) > MAX_MANIFEST_BYTES:
        raise ShipStreamSourceError("ShipStream source manifest is empty or too large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ShipStreamSourceError("ShipStream source manifest is not valid JSON") from error
    notion = value.get("notion") if isinstance(value, dict) else None
    authority = value.get("sourceAuthority") if isinstance(value, dict) else None
    format_slug = value.get("format") if isinstance(value, dict) else None
    notion_page_id = master_pages.get("notionPageId")
    if not _nonblank(notion_page_id):
        raise ShipStreamSourceError("Master Pages Notion page ID is missing")
    notion_page_matches = (
        isinstance(notion, dict)
        and (
            notion.get("pageId") == notion_page_id
            or (
                notion.get("pageId") is None
                and isinstance(authority, dict)
                and authority.get("kind") == "exact_page_binding"
                and authority.get("notionPageId") == notion_page_id
            )
        )
    )
    if (
        not isinstance(value, dict)
        or value.get("schema") != MANIFEST_SCHEMA
        or value.get("page") != handle
        or not isinstance(notion, dict)
        or not notion_page_matches
        or notion.get("contentNiche") != master_pages.get("contentNiche")
        or notion.get("contentEngine") != master_pages.get("contentEngine")
        or notion.get("serviceMode") != master_pages.get("automationMode")
        or not isinstance(authority, dict)
        or authority.get("pageHandle") != handle
        or authority.get("notionPageId") != notion_page_id
        or (
            authority.get("pageBound") is not True
            and authority.get("kind") != "exact_page_binding"
        )
        or authority.get("replacementEligible") is not True
        or not isinstance(format_slug, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", format_slug)
        or (expected_format is not None and format_slug != expected_format)
    ):
        raise ShipStreamSourceError("ShipStream source manifest diverges from Master Pages")
    if value.get("master") is not None:
        masters = [_master_from_page_master(value["master"], handle)]
    elif authority.get("kind") == "historical_posted_cut_recovery":
        rows = value.get("historicalPostedCuts")
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_SOURCES:
            raise ShipStreamSourceError("ShipStream historical source library is empty or too large")
        masters = [
            _master_from_historical_cut(row, handle, notion_page_id)
            for row in rows
        ]
    else:
        raise ShipStreamSourceError("ShipStream source manifest has no executable page source")
    if len({row["sha256"] for row in masters}) != len(masters):
        raise ShipStreamSourceError("ShipStream source manifest contains duplicate source bytes")
    masters.sort(key=lambda row: row["sourceId"])
    identity = hashlib.sha256(json.dumps(
        {"pageId": page_id, "format": format_slug, "masters": masters},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    slug = re.sub(r"[^a-z0-9]+", "-", handle.lower()).strip("-")[:48]
    library_id = f"shipstream-{slug}-{identity[:16]}"
    if expected_library_id is not None and library_id != expected_library_id:
        raise ShipStreamSourceError("ShipStream source library version changed")
    document = {
        "schema": "content-lab.source-dna-library.v2",
        "libraryId": library_id,
        "format": format_slug,
        "pageId": page_id,
        "masters": masters,
    }
    try:
        return parse_source_dna_manifest(json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode(), library_id)
    except SourceDnaError as error:
        raise ShipStreamSourceError(str(error)) from error


def parse_shipstream_source_projection(
    raw: bytes,
    master_pages: dict[str, Any],
    *,
    page_id: str,
    expected_format: str | None = None,
    expected_library_id: str | None = None,
) -> ShipStreamSourceProjection:
    """Read one manifest into its source and approved-cut views.

    The immutable source library remains the only recut authority. Approved
    cuts are a separately versioned, page-scoped view of derivative bytes and
    never become replacement masters.
    """
    source_library = parse_shipstream_source_manifest(
        raw,
        master_pages,
        page_id=page_id,
        expected_format=expected_format,
        expected_library_id=expected_library_id,
    )
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ShipStreamSourceError("ShipStream source manifest is not valid JSON") from error
    if not isinstance(value, dict):
        raise ShipStreamSourceError("ShipStream source manifest is invalid")
    try:
        approved_cut_library = _approved_cut_library(
            value,
            source_library,
            handle=_vault_handle(master_pages),
        )
    except ShipStreamSourceError as error:
        raise ShipStreamApprovedCutsError(str(error)) from error
    return ShipStreamSourceProjection(
        source_library=source_library,
        approved_cut_library=approved_cut_library,
        approved_cut_status="reference" if approved_cut_library is not None else "missing",
    )


def load_shipstream_source_dna_library(
    master_pages: dict[str, Any],
    *,
    page_id: str,
    expected_format: str | None = None,
    expected_library_id: str | None = None,
    fetch_manifest: Callable[[str], bytes] | None = None,
) -> SourceDnaLibrary:
    handle = _vault_handle(master_pages)
    fetch = fetch_manifest or _fetch_manifest
    raw = fetch(source_manifest_url(handle))
    return parse_shipstream_source_manifest(
        raw,
        master_pages,
        page_id=page_id,
        expected_format=expected_format,
        expected_library_id=expected_library_id,
    )


def load_shipstream_source_projection(
    master_pages: dict[str, Any],
    *,
    page_id: str,
    expected_format: str | None = None,
    expected_library_id: str | None = None,
    fetch_manifest: Callable[[str], bytes] | None = None,
) -> ShipStreamSourceProjection:
    handle = _vault_handle(master_pages)
    fetch = fetch_manifest or _fetch_manifest
    raw = fetch(source_manifest_url(handle))
    try:
        return parse_shipstream_source_projection(
            raw,
            master_pages,
            page_id=page_id,
            expected_format=expected_format,
            expected_library_id=expected_library_id,
        )
    except ShipStreamApprovedCutsError:
        # Derivative inventory is not source authority. Preserve a valid exact
        # source binding while making the separate cut view fail closed.
        return ShipStreamSourceProjection(
            source_library=parse_shipstream_source_manifest(
                raw,
                master_pages,
                page_id=page_id,
                expected_format=expected_format,
                expected_library_id=expected_library_id,
            ),
            approved_cut_library=None,
            approved_cut_status="invalid",
        )
