"""Exact ShipStream page-source projection into Content Lab."""

from __future__ import annotations

import copy
import json

import pytest

from services.shipstream_source_manifest import (
    ShipStreamSourceError,
    load_shipstream_source_dna_library,
    parse_shipstream_source_manifest,
    source_manifest_url,
)
from tests.master_pages_fixtures import master_pages


PAGE_ID = "acct:operator:love-night"
HANDLE = "lovenightwalks"
NOTION_PAGE_ID = "3c61465b-b829-8095-86ec-f979f90ee48a"
FORMAT = "pov-night-core"


def _intent():
    intent, revision = master_pages(
        PAGE_ID,
        handle=HANDLE,
        content_niche="POV — Night Core",
        content_engine="sourced_video",
        vault_url=f"https://shipstream.risingtidesviral.com/vault/{HANDLE}",
    )
    intent["notionPageId"] = NOTION_PAGE_ID
    intent["automationMode"] = "Operator"
    return intent, revision


def _historical_row(ordinal: int = 1) -> dict:
    sha256 = f"{ordinal:064x}"
    return {
        "type": "historical_posted_cut",
        "pageHandle": HANDLE,
        "notionPageId": NOTION_PAGE_ID,
        "sha256": sha256,
        "storageKey": f"vault/{HANDLE}/pool/{sha256}.mp4",
        "bytes": 10_000_000 + ordinal,
        "uploadedAt": "2026-08-25T20:35:39.194Z",
        "media": {"durationSeconds": 10.01},
    }


def _historical_manifest() -> dict:
    return {
        "schema": "shipstream.source-manifest.v1",
        "page": HANDLE,
        "notion": {
            "pageId": NOTION_PAGE_ID,
            "contentEngine": "sourced_video",
            "contentNiche": "POV — Night Core",
            "serviceMode": "Operator",
        },
        "format": FORMAT,
        "sourceAuthority": {
            "kind": "historical_posted_cut_recovery",
            "pageHandle": HANDLE,
            "notionPageId": NOTION_PAGE_ID,
            "pageBound": True,
            "replacementEligible": True,
        },
        "master": None,
        "historicalPostedCuts": [_historical_row(2), _historical_row(1)],
        "cuts": [],
        "supersededGenericMaster": {
            "sha256": "f" * 64,
            "disposition": "superseded-generic-page-mismatch",
        },
        "updatedAt": "2026-09-01T21:25:29.489163+00:00",
    }


def _raw(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def test_historical_page_bytes_become_the_only_recuttable_sources():
    intent, _ = _intent()
    library = parse_shipstream_source_manifest(
        _raw(_historical_manifest()), intent,
        page_id=PAGE_ID, expected_format=FORMAT,
    )
    assert library.page_id == PAGE_ID
    assert library.format_slug == FORMAT
    assert [source.sha256 for source in library.masters] == [
        f"{ordinal:064x}" for ordinal in (1, 2)
    ]
    assert all(source.storage_key.startswith(f"vault/{HANDLE}/pool/")
               for source in library.masters)
    assert all(source.source_offset_ms == 0 for source in library.masters)
    assert "f" * 64 not in {source.sha256 for source in library.masters}


def test_refill_cuts_and_manifest_timestamp_do_not_change_source_version():
    intent, _ = _intent()
    before = _historical_manifest()
    after = copy.deepcopy(before)
    after["cuts"] = [{"sha256": "e" * 64, "ordinal": 99}]
    after["updatedAt"] = "2026-09-02T00:00:00Z"
    first = parse_shipstream_source_manifest(_raw(before), intent, page_id=PAGE_ID)
    second = parse_shipstream_source_manifest(_raw(after), intent, page_id=PAGE_ID)
    assert second.library_id == first.library_id
    assert second.sha256 == first.sha256


def test_exact_registered_master_uses_its_own_file_from_offset_zero():
    intent, _ = _intent()
    sha256 = "a" * 64
    manifest = _historical_manifest()
    manifest["sourceAuthority"] = {
        "kind": "exact_page_binding",
        "pageHandle": HANDLE,
        "notionPageId": NOTION_PAGE_ID,
        "replacementEligible": True,
    }
    # This is the current normal ShipStream producer shape. The exact Notion
    # id is present in sourceAuthority even though the older notion block did
    # not repeat it.
    manifest["notion"].pop("pageId")
    manifest["master"] = {
        "sha256": sha256,
        "storageKey": f"vault/{HANDLE}/masters/{sha256}.mp4",
        "bytes": 20_000_000,
        "media": {"durationSeconds": 42.5},
        "originSourceUrl": "https://cdn.example/source.mp4",
        "originWindowSeconds": [120.0, 162.5],
        "registeredAt": "2026-09-01T21:25:29Z",
    }
    manifest.pop("historicalPostedCuts")
    library = parse_shipstream_source_manifest(
        _raw(manifest), intent, page_id=PAGE_ID,
    )
    assert len(library.masters) == 1
    assert library.masters[0].storage_key.endswith(f"masters/{sha256}.mp4")
    assert library.masters[0].source_offset_ms == 0
    assert library.masters[0].duration_ms == 42_500


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("page",), "another-page"),
        (("notion", "pageId"), "another-notion-row"),
        (("notion", "contentNiche"), "POV — Scenic"),
        (("notion", "serviceMode"), "Automation"),
        (("sourceAuthority", "pageBound"), False),
        (("format",), "pov-scenic"),
    ],
)
def test_page_and_master_pages_drift_fails_closed(path, value):
    intent, _ = _intent()
    manifest = _historical_manifest()
    cursor = manifest
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    with pytest.raises(ShipStreamSourceError):
        parse_shipstream_source_manifest(
            _raw(manifest), intent, page_id=PAGE_ID,
            expected_format=FORMAT,
        )


def test_loader_uses_only_the_master_pages_vault_handle():
    intent, _ = _intent()
    calls = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return _raw(_historical_manifest())

    library = load_shipstream_source_dna_library(
        intent, page_id=PAGE_ID, fetch_manifest=fetch,
    )
    assert calls == [source_manifest_url(HANDLE)]
    assert library.library_id.startswith("shipstream-lovenightwalks-")


def test_foreign_or_non_shipstream_vault_url_is_never_fetched():
    intent, _ = _intent()
    intent["vaultUrl"] = "https://example.com/vault/lovenightwalks"
    called = False

    def fetch(_url: str) -> bytes:
        nonlocal called
        called = True
        return b"{}"

    with pytest.raises(ShipStreamSourceError):
        load_shipstream_source_dna_library(
            intent, page_id=PAGE_ID, fetch_manifest=fetch,
        )
    assert called is False


def test_http_fetch_stops_when_a_chunked_manifest_exceeds_the_cap(monkeypatch):
    import services.shipstream_source_manifest as source_manifest

    class Response:
        status_code = 200
        headers = {}

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def iter_bytes():
            yield b"a" * source_manifest.MAX_MANIFEST_BYTES
            yield b"b"

    class Stream:
        def __enter__(self):
            return Response()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(source_manifest.httpx, "stream", lambda *args, **kwargs: Stream())
    with pytest.raises(ShipStreamSourceError, match="too large"):
        source_manifest._fetch_manifest(source_manifest.source_manifest_url(HANDLE))
