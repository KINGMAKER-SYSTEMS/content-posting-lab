"""Evidence-only pure native332 transform; no archive or media deletion."""
import copy
import json
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from services import job_store_compaction as compaction
from services.json_store import atomic_save, atomic_load

NOW = datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc)


def _stored_pair(tmp_path, source_cuts):
    private = tmp_path / 'private-artifact.bin'
    private.write_bytes(b'owned-private-artifact')
    stamp = (NOW - timedelta(days=40)).isoformat()
    store = {'jobs': {
        'bad': {'jobId': 'bad', 'pageId': 'page', 'sourceKind': 'dossier_source_dna',
                'status': 'completed', 'completedAt': stamp, 'createdAt': stamp,
                'sourceCuts': source_cuts, 'clips': [], 'artifactRoot': str(private.parent)},
        'good': {'jobId': 'good', 'pageId': 'page', 'sourceKind': 'generated',
                 'status': 'completed', 'completedAt': stamp, 'createdAt': stamp, 'clips': []},
    }, 'byIdempotency': {'bad-key': 'bad', 'good-key': 'good'}}
    path = tmp_path / 'jobs.json'
    atomic_save(path, store)
    return path, private, atomic_load(path, {})


@pytest.mark.parametrize('shape', [None, 7, True], ids=['null', 'integer', 'boolean'])
def test_noniterable_sourcecuts_holds_row_and_preserves_cursor_peer_progress(tmp_path, shape):
    path, private, store = _stored_pair(tmp_path, shape)
    original = copy.deepcopy(store)
    disk = path.read_bytes()
    error = None
    try:
        try:
            first, archived = compaction.compact_job_store(store, NOW, batch_size=1)
        except TypeError as caught:
            frames = traceback.extract_tb(caught.__traceback__)
            assert any(Path(f.filename).resolve() == Path(compaction.__file__).resolve()
                       and f.name == 'extract_index_entries' and f.lineno == 233 for f in frames)
            assert 'not iterable' in str(caught)
            error = caught
        assert error is None, 'opaque sourceCuts must hold only that row and allow cursor progress'
        assert archived == [] and first['jobs']['bad'] == original['jobs']['bad']
        assert first[compaction.COMPACTION_SCAN_NEXT_KEY] == 'good'
        second, archived = compaction.compact_job_store(first, NOW, batch_size=1)
        assert [row['jobId'] for row in archived] == ['good']
        assert set(second['jobs']) == {'bad'}
        assert second[compaction.COMPACTION_SCAN_NEXT_KEY] == 'bad'
        assert second['byIdempotency']['bad-key'] == 'bad'
    finally:
        assert store == original
        assert path.read_bytes() == disk
        assert private.read_bytes() == b'owned-private-artifact'


@pytest.mark.parametrize('source_cuts', [[], [{'slotId': 'slot', 'masterSha256': 'a' * 64,
                                            'startMs': 0, 'durationMs': 6000}]])
def test_valid_sourcecuts_still_archive_both_rows_without_io(tmp_path, source_cuts):
    path, private, store = _stored_pair(tmp_path, source_cuts)
    original = copy.deepcopy(store)
    disk = path.read_bytes()
    result, archived = compaction.compact_job_store(store, NOW, batch_size=2)
    assert [row['jobId'] for row in archived] == ['bad', 'good']
    assert result['jobs'] == {}
    assert len(result[compaction.ARCHIVE_INDEX_KEY]['sourceDnaCuts']) == len(source_cuts)
    assert compaction.COMPACTION_SCAN_NEXT_KEY not in result
    assert store == original and path.read_bytes() == disk
    assert private.read_bytes() == b'owned-private-artifact'
