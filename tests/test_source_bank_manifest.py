"""Banked R2 source-bank entry projects into a page's Lab source library.

A bank.json (``rt.source-bank.v1``) is a shared ``role: backup`` pool, not a
page library. ``parse_source_bank_manifest`` projects its backup masters for
one page into that page's existing ``SourceDnaLibrary``: role stays ``backup``,
no ``gradeOverride`` is declared (backups render at the page look, lead
2026-10-26), and the library version the Worker binds clips to stays pinned to
the primaries only.
"""
import json
from pathlib import Path

import pytest

from services.source_bank_manifest import SourceBankError, parse_source_bank_manifest
from services.source_dna_registry import parse_source_dna_manifest

FIXTURE = Path(__file__).parent / "fixtures/source_bank_rjuenv8ouv0.json"
PAGE_ID = "acct:operator:lovenightwalks"
HANDLE = "lovenightwalks"
FORMAT = "pov-night-core"
PRIMARY_SHA = "a" * 64


def _primary_library() -> object:
    doc = {
        "schema": "content-lab.source-dna-library.v2",
        "libraryId": "lovenightwalks-primary-v1",
        "format": FORMAT,
        "pageId": PAGE_ID,
        "masters": [{
            "sourceId": "primary-master-01",
            "sha256": PRIMARY_SHA,
            "bytes": 123_456_789,
            "filename": "primary-master.mp4",
            "mimeType": "video/mp4",
            "storageKey": "source-dna/pov-night-core/lovenightwalks/primary-master.mp4",
            "durationMs": 800_000,
            "sourceOffsetMs": 0,
            "provenance": {
                "sourceUrl": "https://example.com/primary",
                "acquiredAt": "2026-07-29T14:49:47Z",
                "authority": "operator-approved primary footage",
            },
        }],
    }
    # The registry's canonical form (sort_keys, compact separators, ensure_ascii
    # off, trailing newline) is what the real recipe files use and what the
    # version pins; serialize the primary library the same way so the exact-bytes
    # (no-backup) version and the primary-only canonical projection agree.
    return parse_source_dna_manifest(
        json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n",
        doc["libraryId"],
    )


def _bank_raw() -> bytes:
    return FIXTURE.read_bytes()


def test_bank_backup_masters_become_lab_rows_with_role_backup_and_page_look():
    library = parse_source_bank_manifest(
        _bank_raw(), page_handle=HANDLE, primary_library=_primary_library(),
    )
    roles = [master.role for master in library.masters]
    assert roles.count("primary") == 1
    assert roles.count("backup") == 2
    backups = [master for master in library.masters if master.role == "backup"]
    assert [master.sha256 for master in backups] == [
        "27fc23dd531001346ca5750fd48d69774ee3e9086c9177dc85492c369ad011d4",
        "d35e00948b24428f16934ac6ae298ccc007c1dc7114120b45997e69810c7e860",
    ]
    # Backups render at the page look: no gradeOverride is declared.
    assert all(master.grade_override is None for master in backups)
    # The primary is preserved verbatim, and the library re-validated through
    # the registry contract (pageId, format, libraryId all intact).
    assert library.page_id == PAGE_ID
    assert library.format_slug == FORMAT
    assert library.library_id == "lovenightwalks-primary-v1"


def test_adding_bank_backups_does_not_move_the_library_version():
    primary = _primary_library()
    pooled = parse_source_bank_manifest(_bank_raw(), page_handle=HANDLE, primary_library=primary)
    assert pooled.sha256 == primary.sha256, "the Worker's bound version stays the primary's"


def test_backup_storage_key_shares_the_page_footage_store_and_keeps_origin_offsets():
    library = parse_source_bank_manifest(
        _bank_raw(), page_handle=HANDLE, primary_library=_primary_library(),
    )
    backups = sorted(
        (master for master in library.masters if master.role == "backup"),
        key=lambda master: master.source_offset_ms,
    )
    # Storage keys land beside the primary in the page's footage store.
    assert all(master.storage_key.startswith("source-dna/pov-night-core/lovenightwalks/")
               for master in backups)
    # The bank's origin window becomes the master's source offset and duration.
    assert backups[0].source_offset_ms == 120_000
    assert backups[0].duration_ms == 487_000
    assert backups[1].source_offset_ms == 607_000
    assert backups[1].duration_ms == 488_000
    # Provenance names the SU-BANK authority and the R2 key it was banked at.
    assert "SU-BANK rjuENv8oUV0 role backup for lovenightwalks" in backups[0].provenance["authority"]
    assert "rt-source-library/youtube/rjuENv8oUV0/lovenightwalks/" in backups[0].provenance["authority"]


def test_only_the_requested_pages_share_is_projected():
    value = json.loads(_bank_raw())
    other_page_row = {
        "name": "masters/rjuENv8oUV0-other-120-607.mp4",
        "role": "backup",
        "originSourceId": "rjuENv8oUV0",
        "page": "some.other.page",
        "originStartSec": 120,
        "originEndSec": 607,
        "durationSec": 487,
        "bytes": 1,
        "sha256": "b" * 64,
        "r2Key": "youtube/rjuENv8oUV0/some.other.page/other.mp4",
    }
    value["masters"].append(other_page_row)
    library = parse_source_bank_manifest(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
        page_handle=HANDLE, primary_library=_primary_library(),
    )
    assert [master.role for master in library.masters].count("backup") == 2
    assert "b" * 64 not in {master.sha256 for master in library.masters}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda v: v.update(schema="rt.not-a-bank.v1"), "not an rt.source-bank.v1 entry"),
        (lambda v: v.update(role="primary"), "not a backup pool"),
        (lambda v: v.update(boundToPages=True), "page-bound"),
        (lambda v: v.__setitem__("masters", []), "non-empty bounded list"),
    ],
)
def test_malformed_bank_entries_fail_closed(mutate, message):
    value = json.loads(_bank_raw())
    mutate(value)
    with pytest.raises(SourceBankError, match=message):
        parse_source_bank_manifest(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
            page_handle=HANDLE, primary_library=_primary_library(),
        )


def test_a_bank_entry_with_no_share_for_the_page_fails_closed():
    value = json.loads(_bank_raw())
    for master in value["masters"]:
        master["page"] = "some.other.page"
    with pytest.raises(SourceBankError, match="no backup master for page"):
        parse_source_bank_manifest(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
            page_handle=HANDLE, primary_library=_primary_library(),
        )


def test_a_non_backup_master_share_is_rejected():
    value = json.loads(_bank_raw())
    value["masters"][0]["role"] = "primary"
    with pytest.raises(SourceBankError, match="not a backup master"):
        parse_source_bank_manifest(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
            page_handle=HANDLE, primary_library=_primary_library(),
        )
