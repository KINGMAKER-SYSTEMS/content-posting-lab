import json

import pytest

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
