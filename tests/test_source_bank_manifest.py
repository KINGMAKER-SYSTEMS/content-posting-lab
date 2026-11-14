"""Banked R2 source-bank entry projects into a page's Lab source library.

A bank.json (``rt.source-bank.v1``) is a shared ``role: backup`` pool, not a
page library. ``parse_source_bank_manifest`` projects its backup masters for
one page into that page's existing ``SourceDnaLibrary``: role stays ``backup``,
no ``gradeOverride`` is declared (backups render at the page look, lead
2026-10-26), and the library version the Worker binds clips to stays pinned to
the primaries only.

The real capture ``source_bank_rjuenv8ouv0.json`` is the 10-26 SU-BANK
``bank.json`` (trimmed, sha prefix 8d5c5c0a2445). It is an *unstamped partial*:
it carries no ``partial``/``missingRanges`` markers and its shares cover only
120..1198 of ``usableRangeSec`` 120..3356 (the legacy ``uploader.sh`` shape the
build round must fail closed). A complete bank for rjuENv8oUV0 does not exist
yet (the source is 2/5 banked), so the happy-path tests use a schema-conformant
synthetic completion of that real capture, clearly labelled.
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
    """The real (unstamped-partial) 10-26 capture, as-is."""
    return FIXTURE.read_bytes()


def _master(tag: str, start: int, end: int, duration: int, sha256: str) -> dict:
    """A schema-conformant SU-BANK master row (same shape as the real capture)."""
    return {
        "name": f"masters/rjuENv8oUV0-lovenightwalks-{tag}.mp4",
        "role": "backup",
        "originSourceId": "rjuENv8oUV0",
        "page": HANDLE,
        "originStartSec": start,
        "originEndSec": end,
        "durationSec": duration,
        "originToleranceSec": 6,
        "encoder": "videotoolbox",
        "bytes": 200_000_000,
        "sha256": sha256,
        "ocrFlaggedFrames": 0,
        "r2Key": f"youtube/rjuENv8oUV0/lovenightwalks/rjuENv8oUV0-lovenightwalks-{tag}.mp4",
    }


def _complete_bank_raw() -> bytes:
    """A clearly-labelled synthetic COMPLETE bank, derived from the real capture.

    The real capture is an unstamped partial (shares cover 120..1198 of
    usableRangeSec 120..3356). No real complete bank.json exists for
    rjuENv8oUV0 yet, so the happy path tiles the real ``usableRangeSec`` fully.
    Only the extension is synthetic; the schema and every field are the real
    bank's own shape.
    """
    value = json.loads(_bank_raw())
    masters = [
        _master("120-1198", 120, 1198, 1078, "c1" * 32),
        _master("1198-2278", 1198, 2278, 1080, "c2" * 32),
        _master("2278-3356", 2278, 3356, 1078, "c3" * 32),
    ]
    value["shares"] = [{
        "role": "backup",
        "page": HANDLE,
        "shareStartSec": 120,
        "shareEndSec": 3356,
        "sourceFormat": "f401",
        "sourceFile": "./rjuENv8oUV0.lovenightwalks.120-3356.f401.mp4",
        "masters": [dict(m) for m in masters],
    }]
    value["masters"] = [dict(m) for m in masters]
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def test_bank_backup_masters_become_lab_rows_with_role_backup_and_page_look():
    library = parse_source_bank_manifest(
        _complete_bank_raw(), page_handle=HANDLE, primary_library=_primary_library(),
    )
    roles = [master.role for master in library.masters]
    assert roles.count("primary") == 1
    assert roles.count("backup") == 3
    backups = [master for master in library.masters if master.role == "backup"]
    assert sorted(master.sha256 for master in backups) == ["c1" * 32, "c2" * 32, "c3" * 32]
    # Backups render at the page look: no gradeOverride is declared.
    assert all(master.grade_override is None for master in backups)
    # The primary is preserved verbatim, and the library re-validated through
    # the registry contract (pageId, format, libraryId all intact).
    assert library.page_id == PAGE_ID
    assert library.format_slug == FORMAT
    assert library.library_id == "lovenightwalks-primary-v1"


def test_adding_bank_backups_does_not_move_the_library_version():
    primary = _primary_library()
    pooled = parse_source_bank_manifest(
        _complete_bank_raw(), page_handle=HANDLE, primary_library=primary,
    )
    assert pooled.sha256 == primary.sha256, "the Worker's bound version stays the primary's"


def test_adding_bank_backups_does_not_move_the_version_for_noncanonical_primary_bytes():
    # A real caller may hold the primary library serialized with different
    # whitespace/key order than the registry's canonical form. The registry pins
    # an all-primary manifest to its exact bytes, so the adapter must keep the
    # primary library's own version rather than re-hash a rebuilt document.
    doc = {
        "masters": [{
            "bytes": 123_456_789,
            "filename": "primary-master.mp4",
            "mimeType": "video/mp4",
            "provenance": {
                "authority": "operator-approved primary footage",
                "sourceUrl": "https://example.com/primary",
                "acquiredAt": "2026-07-29T14:49:47Z",
            },
            "sha256": PRIMARY_SHA,
            "storageKey": "source-dna/pov-night-core/lovenightwalks/primary-master.mp4",
            "durationMs": 800_000,
            "sourceId": "primary-master-01",
            "sourceOffsetMs": 0,
        }],
        "libraryId": "lovenightwalks-primary-v1",
        "format": FORMAT,
        "pageId": PAGE_ID,
        "schema": "content-lab.source-dna-library.v2",
    }
    raw = json.dumps(doc, indent=2).encode()  # non-canonical whitespace
    primary = parse_source_dna_manifest(raw, doc["libraryId"])
    pooled = parse_source_bank_manifest(
        _complete_bank_raw(), page_handle=HANDLE, primary_library=primary,
    )
    assert pooled.sha256 == primary.sha256


def test_backup_storage_key_shares_the_page_footage_store_and_keeps_origin_offsets():
    library = parse_source_bank_manifest(
        _complete_bank_raw(), page_handle=HANDLE, primary_library=_primary_library(),
    )
    backups = sorted(
        (master for master in library.masters if master.role == "backup"),
        key=lambda master: master.source_offset_ms,
    )
    # Storage keys land beside the primary in the page's footage store.
    assert all(master.storage_key.startswith("source-dna/pov-night-core/lovenightwalks/")
               for master in backups)
    # The bank's origin window becomes the master's source offset and duration.
    assert [master.source_offset_ms for master in backups] == [120_000, 1_198_000, 2_278_000]
    assert [master.duration_ms for master in backups] == [1_078_000, 1_080_000, 1_078_000]
    # Provenance names the SU-BANK authority and the R2 key it was banked at.
    assert "SU-BANK rjuENv8oUV0 role backup for lovenightwalks" in backups[0].provenance["authority"]
    assert "rt-source-library/youtube/rjuENv8oUV0/lovenightwalks/" in backups[0].provenance["authority"]


def test_only_the_requested_pages_share_is_projected():
    value = json.loads(_complete_bank_raw())
    other_page_row = _master("other-120-607", 120, 607, 487, "b" * 64)
    other_page_row["page"] = "some.other.page"
    value["masters"].append(other_page_row)
    library = parse_source_bank_manifest(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
        page_handle=HANDLE, primary_library=_primary_library(),
    )
    assert [master.role for master in library.masters].count("backup") == 3
    assert "b" * 64 not in {master.sha256 for master in library.masters}


def test_adapter_projects_into_a_real_registered_primary_library():
    # MED-3: exercise the adapter against the real registry format (a registered
    # all-primary recipe) instead of a synthetic primary library.
    from services.source_dna_registry import load_source_dna_library

    # A registered all-primary library with no retired footage (the original
    # Between the Lines library holds only the retired IMG_4962 master).
    primary = load_source_dna_library("pov-night-core-drivetoclearmymind001-v1")
    assert all(master.role == "primary" for master in primary.masters)
    pooled = parse_source_bank_manifest(
        _complete_bank_raw(), page_handle=HANDLE, primary_library=primary,
    )
    assert [master.role for master in pooled.masters].count("backup") == 3
    assert pooled.sha256 == primary.sha256


def test_adapter_never_pools_backups_onto_a_retired_primary():
    # Rule #1: a retired master may stay only as a library's single original
    # primary, so a bank must never add backups to that library.
    from services.source_dna_registry import RETIRED_MASTER_SHAS, load_source_dna_library

    retired = load_source_dna_library("pov-night-core-between-the-lines66-v1")
    assert {master.sha256 for master in retired.masters} <= RETIRED_MASTER_SHAS
    with pytest.raises(SourceBankError, match="is retired"):
        parse_source_bank_manifest(
            _complete_bank_raw(), page_handle=HANDLE, primary_library=retired,
        )


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
    value = json.loads(_complete_bank_raw())
    mutate(value)
    with pytest.raises(SourceBankError, match=message):
        parse_source_bank_manifest(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
            page_handle=HANDLE, primary_library=_primary_library(),
        )


def test_a_bank_entry_with_no_share_for_the_page_fails_closed():
    value = json.loads(_complete_bank_raw())
    for master in value["masters"]:
        master["page"] = "some.other.page"
    with pytest.raises(SourceBankError, match="missing banked masters for page"):
        parse_source_bank_manifest(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
            page_handle=HANDLE, primary_library=_primary_library(),
        )


def test_a_non_backup_master_share_is_rejected():
    value = json.loads(_complete_bank_raw())
    value["masters"][0]["role"] = "primary"
    with pytest.raises(SourceBankError, match="not a backup master"):
        parse_source_bank_manifest(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
            page_handle=HANDLE, primary_library=_primary_library(),
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda v: v.update(partial=True), "partial index"),
        (lambda v: v.update(missingRanges=[[1198, 3356]]), "missing ranges"),
        (lambda v: v.__setitem__("masters", v["masters"][:1]), "missing banked masters"),
    ],
)
def test_partial_bank_entries_fail_closed(mutate, message):
    """Each SU-BANK partial marker alone (flag, missing ranges, or fewer banked
    masters than the shares declare) must fail closed, not project a half library."""
    value = json.loads(_complete_bank_raw())
    mutate(value)
    with pytest.raises(SourceBankError, match=message):
        parse_source_bank_manifest(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
            page_handle=HANDLE, primary_library=_primary_library(),
        )


def test_the_real_partial_upload_shape_fails_closed():
    """The actual SU-BANK partial-upload object (partial:true + missingRanges +
    only 1 of the declared masters banked) must never project a half library."""
    value = json.loads(_complete_bank_raw())
    value["partial"] = True
    value["missingRanges"] = [[1198, 3356]]
    value["masters"] = value["masters"][:1]  # 1 of 3 declared masters banked
    with pytest.raises(SourceBankError, match="partial index"):
        parse_source_bank_manifest(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
            page_handle=HANDLE, primary_library=_primary_library(),
        )


def test_the_real_unstamped_partial_capture_fails_closed():
    """The real 10-26 bank.json (no partial/missingRanges markers) must fail
    closed when the declared shares stop short of usableRangeSec. To isolate the
    usable-range guard, the share is shrunk to match the banked masters
    (120..1095); only the usable range 120..3356 is left uncovered, which is
    exactly the legacy uploader.sh shape the build round must reject."""
    value = json.loads(_bank_raw())
    value["shares"][0]["shareEndSec"] = 1095  # masters cover this share fully
    with pytest.raises(SourceBankError, match="covers only part of its usable range"):
        parse_source_bank_manifest(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
            page_handle=HANDLE, primary_library=_primary_library(),
        )


def test_a_declared_master_relabeled_to_another_page_fails_closed():
    """A master present by identity (r2Key) in the declared share but relabeled
    to another page drops out of the projection; the page-coverage guard must
    catch the resulting gap rather than return a silently incomplete library."""
    value = json.loads(_complete_bank_raw())
    value["masters"][1]["page"] = "some.other.page"
    with pytest.raises(SourceBankError, match="missing banked masters for page"):
        parse_source_bank_manifest(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
            page_handle=HANDLE, primary_library=_primary_library(),
        )


def test_source_id_stays_within_the_registry_token_bound():
    """LOW-2: a 48-char origin slug plus a 48-char page slug must not push the
    composite ``bank-...`` sourceId past the registry TOKEN's 100-char bound."""
    value = json.loads(_complete_bank_raw())
    long_origin = "x" * 48
    long_page = "y" * 48
    for master in value["masters"]:
        master["originSourceId"] = long_origin
        master["page"] = long_page
        master["r2Key"] = f"youtube/{long_origin}/{long_page}/{master['name'].rsplit('/', 1)[-1]}"
    for share in value["shares"]:
        share["page"] = long_page
        for master in share["masters"]:
            master["originSourceId"] = long_origin
            master["page"] = long_page
            master["r2Key"] = f"youtube/{long_origin}/{long_page}/{master['name'].rsplit('/', 1)[-1]}"
    value["source"]["url"] = "https://www.youtube.com/watch?v=rjuENv8oUV0"
    value["shares"][0]["shareStartSec"] = 120
    value["shares"][0]["shareEndSec"] = 3356
    # Recompute the page's masters to match the long page handle so the
    # page-coverage guard still passes.
    value["masters"] = [
        {**m, "page": long_page} for m in value["masters"]
    ]
    library = parse_source_bank_manifest(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
        page_handle=long_page, primary_library=_primary_library(),
    )
    assert all(len(master.source_id) <= 100 for master in library.masters)
