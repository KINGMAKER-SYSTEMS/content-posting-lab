"""Regression pin: the configured vault origin derives the same identities as the
pre-PR hardcoded origin did.

Before #180 the ShipStream origin was a module constant. It is now read from
SHIPSTREAM_VAULT_ORIGIN. The origin feeds provenance ``sourceUrl`` values,
so it also feeds source-library ids and hashes, approved-cut ids, the Dossier
``catalogVersion`` and the manifest URL. If production is configured with the
old origin, every one of those must come out byte-identical.

PINNED was computed by running the pre-PR code (commit 8137cb8) with its
origin constant and host pin pointed at the synthetic ORIGIN below, so the
real production origin never appears here. Its formula is
``f"{origin}/assets/{quote(storage_key, safe='')}"``. Any change to how the
origin enters an identity turns this test red.
"""

from __future__ import annotations

import copy
import json
from urllib.parse import quote

import services.dossier_ingredients as ingredients
import services.shipstream_source_manifest as source_manifest
from services.master_pages_contract import intent_hash
from tests.master_pages_fixtures import master_pages


ORIGIN = "https://vault.identity.test"
PAGE_ID = "acct:operator:love-night"
HANDLE = "lovenightwalks"
NOTION_PAGE_ID = "3c61465b-b829-8095-86ec-f979f90ee48a"
FORMAT = "pov-night-core"
MASTER_SHA = "a" * 64


def _intent() -> tuple[dict, str]:
    intent, _ = master_pages(
        PAGE_ID, handle=HANDLE, content_niche="POV — Night Core",
        content_engine="sourced_video", vault_url=f"{ORIGIN}/vault/{HANDLE}",
    )
    intent["notionPageId"] = NOTION_PAGE_ID
    intent["automationMode"] = "Operator"
    return intent, intent_hash(intent)


def _historical(ordinal: int) -> dict:
    sha256 = f"{ordinal:064x}"
    return {
        "type": "historical_posted_cut", "pageHandle": HANDLE, "notionPageId": NOTION_PAGE_ID,
        "sha256": sha256, "storageKey": f"vault/{HANDLE}/pool/{sha256}.mp4",
        "bytes": 10_000_000 + ordinal, "uploadedAt": "2026-08-25T20:35:39.194Z",
        "media": {"durationSeconds": 10.01},
    }


def _cut(ordinal: int, parent: str, parent_type: str | None) -> dict:
    sha256 = f"{10_000 + ordinal:064x}"
    cut = {
        "ordinal": ordinal, "sha256": sha256, "storageKey": f"vault/{HANDLE}/pool/{sha256}.mp4",
        "parentSha256": parent, "sourceStartSeconds": 3.936, "sourceDurationSeconds": 6.0,
        "outputDurationSeconds": 6.0, "playbackSpeed": 1.0, "status": "ready",
        "review": "technical-pass-and-page-bound-historical-identity",
        "uploadedAt": "2026-09-01T21:25:40.815494+00:00",
        "media": {"audioStreams": 0, "bytes": 5_886_873 + ordinal, "durationSeconds": 6.0,
                  "fps": 30.0, "height": 1920, "pixelFormat": "yuv420p",
                  "videoCodec": "h264", "width": 1080},
    }
    if parent_type:
        cut["parentType"] = parent_type
    return cut


def _historical_manifest() -> dict:
    return {
        "schema": "shipstream.source-manifest.v1", "page": HANDLE,
        "notion": {"pageId": NOTION_PAGE_ID, "contentEngine": "sourced_video",
                   "contentNiche": "POV — Night Core", "serviceMode": "Operator"},
        "format": FORMAT,
        "sourceAuthority": {"kind": "historical_posted_cut_recovery", "pageHandle": HANDLE,
                            "notionPageId": NOTION_PAGE_ID, "pageBound": True,
                            "replacementEligible": True},
        "master": None,
        "historicalPostedCuts": [_historical(2), _historical(1)],
        "cuts": [_cut(0, f"{1:064x}", "historical_posted_cut")],
    }


def _master_manifest() -> dict:
    """A page master without originSourceUrl: its sourceUrl is built from the origin."""
    manifest = _historical_manifest()
    manifest["sourceAuthority"] = {"kind": "exact_page_binding", "pageHandle": HANDLE,
                                   "notionPageId": NOTION_PAGE_ID, "replacementEligible": True}
    manifest["notion"].pop("pageId")
    sha256 = MASTER_SHA
    manifest["master"] = {
        "sha256": sha256, "storageKey": f"vault/{HANDLE}/masters/{sha256}.mp4",
        "bytes": 20_000_000, "media": {"durationSeconds": 42.5},
        "originWindowSeconds": [120.0, 162.5], "registeredAt": "2026-09-01T21:25:29Z",
    }
    manifest.pop("historicalPostedCuts")
    manifest["cuts"] = [_cut(0, sha256, None)]
    return manifest


def compute_identities() -> dict:
    """Every origin-dependent identity for two manifest shapes. Network-free."""
    intent, revision = _intent()
    fetched: list[str] = []
    out: dict = {"manifestUrl": source_manifest.source_manifest_url(HANDLE)}
    for name, manifest in (("historical", _historical_manifest()), ("master", _master_manifest())):
        raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()

        def fetch(url, raw=raw):
            fetched.append(url)
            return raw

        projection = source_manifest.load_shipstream_source_projection(
            intent, page_id=PAGE_ID, fetch_manifest=fetch,
        )
        library = projection.source_library
        real_fetch = source_manifest._fetch_manifest
        source_manifest._fetch_manifest = fetch
        try:
            catalog = ingredients.build_dossier_ingredient_catalog(
                PAGE_ID, copy.deepcopy(intent), revision,
            )
            entry = next(item for item in catalog["formats"] if item["formatId"] == FORMAT)
            selection = next(
                item for item in entry["productionSelections"]
                if item.get("sourceLibraryId") == library.library_id
            )
            selected = ingredients.selected_dossier_catalog_version(
                PAGE_ID, copy.deepcopy(intent), revision, FORMAT, selection,
            )
        finally:
            source_manifest._fetch_manifest = real_fetch
        approved = projection.approved_cut_library
        out[name] = {
            "libraryId": library.library_id,
            "librarySha256": library.sha256,
            "sourceUrls": [master.provenance["sourceUrl"] for master in library.masters],
            "approvedCuts": [approved.library_id, approved.sha256] if approved else None,
            "catalogVersion": catalog["catalogVersion"],
            "selectionCatalogVersion": selection["catalogVersion"],
            "selectedCatalogVersion": selected,
        }
    out["fetchedUrls"] = sorted(set(fetched))
    return out


# Computed by the pre-PR code (8137cb8); see the module docstring.
PINNED = {'fetchedUrls': ['https://vault.identity.test/assets/vault%2Flovenightwalks%2Fsource-manifest.json'],
 'historical': {'approvedCuts': ['shipstream-lovenightwalks-424b1424c96c3bd3-cuts',
                                 '68bb7a0aefb39340e8ecfa6eae7c7ca063599b96598a3e8300b64067b38e5c9e'],
                'catalogVersion': 'sha256:a39e39d6255e4a6c2acbd1cc16fb4745f465957bb48b3c036cc8d44abed7bdcb',
                'libraryId': 'shipstream-lovenightwalks-424b1424c96c3bd3',
                'librarySha256': '6de6e331fc409c4af05de284f9fdebe79a42c33be2e087879de4037a4d43c121',
                'selectedCatalogVersion': 'sha256:166cc1648410c5f4b2cf20103dc55e52bd8013fea7d944a76dd6bb932933ca48',
                'selectionCatalogVersion': 'sha256:166cc1648410c5f4b2cf20103dc55e52bd8013fea7d944a76dd6bb932933ca48',
                'sourceUrls': ['https://vault.identity.test/assets/vault%2Flovenightwalks%2Fpool%2F0000000000000000000000000000000000000000000000000000000000000001.mp4',
                               'https://vault.identity.test/assets/vault%2Flovenightwalks%2Fpool%2F0000000000000000000000000000000000000000000000000000000000000002.mp4']},
 'manifestUrl': 'https://vault.identity.test/assets/vault%2Flovenightwalks%2Fsource-manifest.json',
 'master': {'approvedCuts': ['shipstream-lovenightwalks-9fb1c5a785851769-cuts',
                             '1db4533ce3f2b6794a894e203e8f5d2e3168df568ba54a6d0f87c15280ec8eb0'],
            'catalogVersion': 'sha256:a1218cfec0082b1531567dabc0e731847b540b9bb1434dfbe256e419eed27558',
            'libraryId': 'shipstream-lovenightwalks-9fb1c5a785851769',
            'librarySha256': '9cb2290055bb563668154448122a3d22ab82b607dd9b9880682f5ec1dcb6a44a',
            'selectedCatalogVersion': 'sha256:b38d8d9c3da191376aae26102b8246640033776ae2433e9cb5088b2487c9310a',
            'selectionCatalogVersion': 'sha256:b38d8d9c3da191376aae26102b8246640033776ae2433e9cb5088b2487c9310a',
            'sourceUrls': ['https://vault.identity.test/assets/vault%2Flovenightwalks%2Fmasters%2Faaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.mp4']}}


def test_configured_origin_derives_the_pre_pr_identities(monkeypatch):
    monkeypatch.setenv("SHIPSTREAM_VAULT_ORIGIN", ORIGIN)
    assert compute_identities() == PINNED


def test_origin_enters_urls_only_through_the_pre_pr_formula(monkeypatch):
    monkeypatch.setenv("SHIPSTREAM_VAULT_ORIGIN", ORIGIN + "/")
    identities = compute_identities()
    manifest_url = f"{ORIGIN}/assets/{quote(f'vault/{HANDLE}/source-manifest.json', safe='')}"
    assert identities["manifestUrl"] == manifest_url
    assert identities["fetchedUrls"] == [manifest_url]
    assert identities["master"]["sourceUrls"] == [
        f"{ORIGIN}/assets/{quote(f'vault/{HANDLE}/masters/{MASTER_SHA}.mp4', safe='')}",
    ]
    assert identities["historical"]["sourceUrls"] == [
        f"{ORIGIN}/assets/{quote(f'vault/{HANDLE}/pool/{n:064x}.mp4', safe='')}"
        for n in (1, 2)
    ]
