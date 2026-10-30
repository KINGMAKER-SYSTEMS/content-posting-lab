"""The BTL alias page and its retired old-tt page expose disjoint libraries.

between.the.lines66 (acct:between-the-lines66) moved onto
pov-night-core-between-the-lines66-automation-v2 (primary 76c34024, a fresh
YouTube master) and its retired automation-v1 library (primary 3816ff5c, Drive
IMG_4962 origin) is deleted from the registry. The ORIGINAL tt-warner page
keeps its immutable pov-night-core-between-the-lines66-v1 library, so its
historical bytes stay pinned without making the retired footage cuttable again.
"""

import hashlib
import json

from services.dossier_ingredients import _master_source_options
from services.source_dna_registry import (
    MANIFEST_DIR,
    parse_source_dna_manifest,
)

ORIGINAL_LIBRARY_ID = "pov-night-core-between-the-lines66-v1"
ALIAS_LIBRARY_ID = "pov-night-core-between-the-lines66-automation-v2"
ORIGINAL_PAGE_ID = "tt-warner-between-the-lines66"
ALIAS_PAGE_ID = "acct:between-the-lines66"

# SHA-256 of the original manifest's exact on-disk bytes (pinned so history
# stays immutable even if a later edit touches the retired old-tt file).
ORIGINAL_MANIFEST_SHA256 = (
    "9e6b200e374096c3c53d379b6225dadde98c6e1b9629a81573bfe09f9ec492a2"
)
# The ORIGINAL page's master (retired network-wide; kept only as this library's
# single original primary for immutable history).
ORIGINAL_MASTER_SHA256 = (
    "3816ff5c4c79d73eff1694cc21f3644aff18bb68c3a97ca5b8306316feda1314"
)
ORIGINAL_MASTER_BYTES = 205_190_968
ORIGINAL_MASTER_DURATION_MS = 800_000
# The ALIAS page's new primary (YouTube oKw0bu2qFVU 600-1200 s).
ALIAS_MASTER_SHA256 = (
    "76c34024438df3239bfdd286deb091280f0226ba63b22a0d09b93fdfebaac649"
)
ALIAS_MASTER_BYTES = 180_564_887
ALIAS_MASTER_DURATION_MS = 600_000
ALIAS_MASTER_SOURCE_OFFSET_MS = 600_000


def _raw(library_id: str) -> bytes:
    return (MANIFEST_DIR / f"{library_id}.json").read_bytes()


def _primary(library_id: str):
    library = parse_source_dna_manifest(_raw(library_id), library_id)
    return next(master for master in library.masters if master.role == "primary")


def test_both_manifests_parse_existing_contract():
    for library_id in (ORIGINAL_LIBRARY_ID, ALIAS_LIBRARY_ID):
        library = parse_source_dna_manifest(_raw(library_id), library_id)
        assert library.format_slug == "pov-night-core"
        assert library.library_id == library_id
        assert [master.role for master in library.masters].count("primary") == 1


def test_original_manifest_bytes_sha_is_pinned():
    raw = _raw(ORIGINAL_LIBRARY_ID)
    assert hashlib.sha256(raw).hexdigest() == ORIGINAL_MANIFEST_SHA256
    assert (
        parse_source_dna_manifest(raw, ORIGINAL_LIBRARY_ID).sha256
        == ORIGINAL_MANIFEST_SHA256
    )


def test_alias_lists_only_v2_and_original_keeps_historical_v1_listing():
    alias_rows = _master_source_options("pov-night-core", ALIAS_PAGE_ID)
    original_rows = _master_source_options("pov-night-core", ORIGINAL_PAGE_ID)

    # The alias page's automation-v1 library is retired and deleted: ONLY v2 is
    # offered. The original tt-warner page keeps its v1 listing for history;
    # listing does not authorize execution of its retired master.
    assert [row["libraryId"] for row in alias_rows] == [ALIAS_LIBRARY_ID]
    assert [row["pageId"] for row in alias_rows] == [ALIAS_PAGE_ID]
    assert [row["libraryId"] for row in original_rows] == [ORIGINAL_LIBRARY_ID]
    assert [row["pageId"] for row in original_rows] == [ORIGINAL_PAGE_ID]

    assert _master_source_options("pov-night-core", "acct:between-the-lines77") == []


def test_alias_primary_identity_source_window_and_provenance_moved_to_v2():
    master = _primary(ALIAS_LIBRARY_ID)
    assert master.sha256 == ALIAS_MASTER_SHA256
    assert master.bytes == ALIAS_MASTER_BYTES
    assert master.duration_ms == ALIAS_MASTER_DURATION_MS
    assert master.source_offset_ms == ALIAS_MASTER_SOURCE_OFFSET_MS
    assert master.source_id == "yt-okw0bu2qfvu-600-1200"
    assert master.storage_key == (
        f"source-dna/pov-night-core/tt-warner-between-the-lines66/{ALIAS_MASTER_SHA256}.mp4"
    )
    assert master.provenance["sourceUrl"] == "https://www.youtube.com/watch?v=oKw0bu2qFVU"

    # The original page's master stays the retired old Drive master, unchanged.
    original = _primary(ORIGINAL_LIBRARY_ID)
    assert original.sha256 == ORIGINAL_MASTER_SHA256
    assert original.bytes == ORIGINAL_MASTER_BYTES
    assert original.duration_ms == ORIGINAL_MASTER_DURATION_MS
    assert original.source_offset_ms == 0


def test_alias_backups_never_move_the_library_version():
    # A backup never moves the alias's version: stripping every backup from v2
    # yields the same version the Worker binds clips to (its primary only).
    library = parse_source_dna_manifest(_raw(ALIAS_LIBRARY_ID), ALIAS_LIBRARY_ID)
    value = json.loads(_raw(ALIAS_LIBRARY_ID))
    value["masters"] = [
        row for row in value["masters"] if row.get("role", "primary") == "primary"
    ]
    primary_only = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    assert library.sha256 == parse_source_dna_manifest(primary_only, ALIAS_LIBRARY_ID).sha256
