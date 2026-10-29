import json

import pytest

from services.dossier_ingredients import _master_source_options
from services.source_dna_registry import SCHEMA, SourceDnaError, parse_source_dna_manifest


def _manifest():
    return {
        "schema": SCHEMA,
        "libraryId": "dirt-bike-masters-v1",
        "format": "pov-dirt-bike",
        "pageId": "tt-dirt-bike",
        "masters": [{
            "sourceId": "ozark-master-01",
            "sha256": "a" * 64,
            "bytes": 123_456_789,
            "filename": "ozark-master-01.mp4",
            "mimeType": "video/mp4",
            "storageKey": "source-dna/pov-dirt-bike/ozark-master-01.mp4",
            "durationMs": 900_000,
            "sourceOffsetMs": 120_000,
            "provenance": {
                "sourceUrl": "https://source.example/watch/ozark-01",
                "acquiredAt": "2026-08-31T20:00:00Z",
                "authority": "operator-reviewed original footage",
            },
        }],
    }


def _raw(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def test_master_source_requires_immutable_bytes_storage_duration_and_provenance():
    parsed = parse_source_dna_manifest(_raw(_manifest()), "dirt-bike-masters-v1")
    assert parsed.format_slug == "pov-dirt-bike"
    assert parsed.page_id == "tt-dirt-bike"
    assert parsed.masters[0].storage_key == "source-dna/pov-dirt-bike/ozark-master-01.mp4"
    assert parsed.masters[0].duration_ms == 900_000
    assert parsed.masters[0].source_offset_ms == 120_000


@pytest.mark.parametrize("field", ["sourceUrl", "acquiredAt", "authority"])
def test_master_source_rejects_incomplete_traceback(field):
    value = _manifest()
    del value["masters"][0]["provenance"][field]
    with pytest.raises(SourceDnaError):
        parse_source_dna_manifest(_raw(value), "dirt-bike-masters-v1")


def test_derived_cut_shape_cannot_be_mistaken_for_master_source():
    value = _manifest()
    value["masters"][0] = {
        "sha256": "a" * 64,
        "bytes": 123,
        "filename": "already-cut.mp4",
        "railPath": "out/clips/pov-dirt-bike/already-cut.mp4",
    }
    with pytest.raises(SourceDnaError):
        parse_source_dna_manifest(_raw(value), "dirt-bike-masters-v1")


def test_night_core_masters_are_exactly_page_scoped_and_never_cross_offered():
    between_page = "tt-warner-between-the-lines66"
    drive_page = "tt-warner-drivetoclearmymind001"
    between = _master_source_options("pov-night-core", between_page)
    drive = _master_source_options("pov-night-core", drive_page)

    assert [row["pageId"] for row in between] == [between_page]
    assert [row["pageId"] for row in drive] == [drive_page]
    assert between[0]["masters"][0]["sha256"] == (
        "3816ff5c4c79d73eff1694cc21f3644aff18bb68c3a97ca5b8306316feda1314"
    )
    assert drive[0]["masters"][0]["sha256"] == (
        "60eb4ca7b2fff7178d88c3a31e77e805165b1b50eaba32b4aebb71da567f5abf"
    )
    assert between[0]["masters"][0]["sourceOffsetMs"] == 0
    assert drive[0]["masters"][0]["sourceOffsetMs"] == 850_000
    assert _master_source_options("pov-night-core", "tt-warner-other") == []


def test_a_retired_master_is_refused_when_re_added_as_backup_or_extra_primary():
    """Rule #1: a retired master may remain ONLY as a library's single original
    primary (immutable history). Re-adding it as a backup, or beside a new
    primary, must fail closed at parse time so it never becomes cuttable again."""
    import services.source_dna_registry as registry

    v2 = (registry.MANIFEST_DIR / "pov-night-core-between-the-lines66-automation-v2.json").read_bytes()
    base = json.loads(v2)

    def add_retired(role):
        value = json.loads(json.dumps(base))
        row = {**value["masters"][0], "sourceId": f"zz-retired-{role}", "role": role,
               "sha256": "3816ff5c4c79d73eff1694cc21f3644aff18bb68c3a97ca5b8306316feda1314",
               "storageKey": "source-dna/pov-night-core/retired/3816ff5c4c79d73eff1694cc21f3644aff18bb68c3a97ca5b8306316feda1314.mp4"}
        value["masters"].append(row)
        return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()

    for role in ("backup", "primary"):
        with pytest.raises(SourceDnaError, match="retired"):
            parse_source_dna_manifest(add_retired(role), "pov-night-core-between-the-lines66-automation-v2")


def test_a_retired_master_may_remain_as_a_librarys_single_original_primary():
    """The retired old-tt library still parses: its 3816ff5c primary is history,
    never cuttable, because nothing offers or resolves that library any more."""
    from services.source_dna_registry import MANIFEST_DIR, parse_source_dna_manifest
    raw = (MANIFEST_DIR / "pov-night-core-between-the-lines66-v1.json").read_bytes()
    library = parse_source_dna_manifest(raw, "pov-night-core-between-the-lines66-v1")
    assert [m.sha256 for m in library.masters] == [
        "3816ff5c4c79d73eff1694cc21f3644aff18bb68c3a97ca5b8306316feda1314"
    ]
