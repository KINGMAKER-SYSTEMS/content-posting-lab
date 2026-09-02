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


class ShipStreamSourceError(ValueError):
    pass


class ShipStreamSourceMissing(ShipStreamSourceError):
    pass


class ShipStreamSourceUnavailable(ShipStreamSourceError):
    pass


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
