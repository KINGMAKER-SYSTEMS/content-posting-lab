import hashlib
import json

from services.dossier_ingredients import _master_source_options
from services.source_dna_registry import (
    MANIFEST_DIR,
    parse_source_dna_manifest,
)

ORIGINAL_LIBRARY_ID = "pov-night-core-between-the-lines66-v1"
ALIAS_LIBRARY_ID = "pov-night-core-between-the-lines66-automation-v1"
ORIGINAL_PAGE_ID = "tt-warner-between-the-lines66"
ALIAS_PAGE_ID = "acct:between-the-lines66"

# SHA-256 of the original manifest's exact on-disk bytes (pinned so history
# stays immutable even if a later edit touches the retired old-tt file).
ORIGINAL_MANIFEST_SHA256 = (
    "9e6b200e374096c3c53d379b6225dadde98c6e1b9629a81573bfe09f9ec492a2"
)
MASTER_SHA256 = (
    "3816ff5c4c79d73eff1694cc21f3644aff18bb68c3a97ca5b8306316feda1314"
)
MASTER_BYTES = 205_190_968
MASTER_DURATION_MS = 800_000


def _raw(library_id: str) -> bytes:
    return (MANIFEST_DIR / f"{library_id}.json").read_bytes()


def test_both_manifests_parse_existing_contract():
    for library_id in (ORIGINAL_LIBRARY_ID, ALIAS_LIBRARY_ID):
        library = parse_source_dna_manifest(_raw(library_id), library_id)
        assert library.format_slug == "pov-night-core"
        assert library.library_id == library_id
        # One primary each; the live alias may also list backups (source pool).
        assert [master.role for master in library.masters].count("primary") == 1


def test_alias_is_only_library_id_and_page_id_delta():
    original = json.loads(_raw(ORIGINAL_LIBRARY_ID))
    alias = json.loads(_raw(ALIAS_LIBRARY_ID))
    # Backups added to the live alias (source pool, role "backup") are not part
    # of the alias contract: its primary masters are exactly the original's.
    alias["masters"] = [row for row in alias["masters"] if row.get("role", "primary") == "primary"]
    expected = dict(original)
    expected["libraryId"] = ALIAS_LIBRARY_ID
    expected["pageId"] = ALIAS_PAGE_ID
    assert alias == expected
    assert alias["pageId"] == ALIAS_PAGE_ID
    assert alias["libraryId"] == ALIAS_LIBRARY_ID
    assert original["pageId"] == ORIGINAL_PAGE_ID


def test_original_manifest_bytes_sha_is_pinned():
    raw = _raw(ORIGINAL_LIBRARY_ID)
    assert hashlib.sha256(raw).hexdigest() == ORIGINAL_MANIFEST_SHA256
    assert (
        parse_source_dna_manifest(raw, ORIGINAL_LIBRARY_ID).sha256
        == ORIGINAL_MANIFEST_SHA256
    )


def test_alias_preserves_master_identity_source_window_and_provenance():
    original = parse_source_dna_manifest(_raw(ORIGINAL_LIBRARY_ID), ORIGINAL_LIBRARY_ID)
    alias = parse_source_dna_manifest(_raw(ALIAS_LIBRARY_ID), ALIAS_LIBRARY_ID)
    primaries = tuple(item for item in alias.masters if item.role == "primary")
    master = primaries[0]
    assert master.duration_ms == MASTER_DURATION_MS
    assert master.bytes == MASTER_BYTES
    assert master.sha256 == MASTER_SHA256
    assert master.source_offset_ms == 0
    # Master records and provenance remain equal: only page scope changed.
    assert primaries == original.masters
    assert alias.sha256 == parse_source_dna_manifest(
        (json.dumps({**json.loads(_raw(ALIAS_LIBRARY_ID)), "masters": [
            row for row in json.loads(_raw(ALIAS_LIBRARY_ID))["masters"] if row.get("role", "primary") == "primary"
        ]}, sort_keys=True, separators=(",", ":")) + "\n").encode(), ALIAS_LIBRARY_ID).sha256, \
        "a backup never moves the alias's version"


def test_alias_exposes_new_page_and_original_exposes_old_tt_not_wrong_page():
    alias_rows = _master_source_options("pov-night-core", ALIAS_PAGE_ID)
    original_rows = _master_source_options("pov-night-core", ORIGINAL_PAGE_ID)

    # BTL's new primary lives in automation-v2 (tests/test_btl_new_primary_v2.py); v1 stays
    # registered until BTL's Dossier is re-published onto v2, so both are offered meanwhile.
    assert [row["libraryId"] for row in alias_rows] == [ALIAS_LIBRARY_ID, "pov-night-core-between-the-lines66-automation-v2"]
    assert [row["pageId"] for row in alias_rows] == [ALIAS_PAGE_ID, ALIAS_PAGE_ID]
    assert [row["libraryId"] for row in original_rows] == [ORIGINAL_LIBRARY_ID]
    assert [row["pageId"] for row in original_rows] == [ORIGINAL_PAGE_ID]

    assert _master_source_options("pov-night-core", "acct:between-the-lines77") == []
