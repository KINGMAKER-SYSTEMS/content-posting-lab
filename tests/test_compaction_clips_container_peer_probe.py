"""Pure persisted-job compaction resilience evidence; no archive/media deletion."""
import copy
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from services import job_store_compaction as compaction
from services.json_store import atomic_load, atomic_save

NOW = datetime(2026, 11, 1, 21, 0, tzinfo=timezone.utc)
SHA = 'a' * 64


def _stored_pair(tmp_path, clips):
    private = tmp_path / 'private-artifact.bin'
    private.write_bytes(b'owned-private-artifact')
    stamp = (NOW - timedelta(days=40)).isoformat()
    store = {'jobs': {
        'bad': {'jobId': 'bad', 'pageId': 'page', 'sourceKind': 'generated',
                'status': 'completed', 'createdAt': stamp, 'completedAt': stamp,
                'clips': clips, 'artifactRoot': str(tmp_path)},
        'good': {'jobId': 'good', 'pageId': 'page', 'sourceKind': 'generated',
                 'status': 'completed', 'createdAt': stamp, 'completedAt': stamp, 'clips': []},
    }, 'byIdempotency': {'bad-key': 'bad', 'good-key': 'good'}}
    if clips == 'missing':
        del store['jobs']['bad']['clips']
    path = tmp_path / 'jobs.json'
    atomic_save(path, store)
    return path, private, atomic_load(path, default={})


@pytest.mark.parametrize('shape', [None, 7, True], ids=['null', 'integer', 'boolean'])
def test_noniterable_completed_clips_hold_row_and_allow_bounded_peer_progress(tmp_path, shape):
    path, private, store = _stored_pair(tmp_path, shape)
    original, disk = copy.deepcopy(store), path.read_bytes()
    failure = None
    try:
        try:
            first, archived = compaction.compact_job_store(store, NOW, batch_size=1)
        except TypeError as error:
            frames = traceback.extract_tb(error.__traceback__)
            assert any(Path(frame.filename).resolve() == Path(compaction.__file__).resolve()
                       and frame.name == '_clip_sha256s' and frame.lineno == 140 for frame in frames)
            assert 'not iterable' in str(error)
            failure = error
        assert failure is None, 'opaque completed clips must hold only that row and allow cursor progress'
        assert archived == [] and first['jobs']['bad'] == original['jobs']['bad']
        assert first[compaction.COMPACTION_SCAN_NEXT_KEY] == 'good'
        second, archived = compaction.compact_job_store(first, NOW, batch_size=1)
        assert [job['jobId'] for job in archived] == ['good']
        assert set(second['jobs']) == {'bad'}
        assert second[compaction.COMPACTION_SCAN_NEXT_KEY] == 'bad'
        assert second['byIdempotency']['bad-key'] == 'bad'
    finally:
        assert store == original and path.read_bytes() == disk
        assert private.read_bytes() == b'owned-private-artifact'


@pytest.mark.parametrize('shape', [{}, 'opaque'], ids=['dictionary', 'string'])
def test_iterable_nonlist_completed_clips_refuse_opaque_fact_extraction(tmp_path, shape):
    path, private, store = _stored_pair(tmp_path, shape)
    original, disk = copy.deepcopy(store), path.read_bytes()
    refused = None
    try:
        try:
            compaction.extract_index_entries(store['jobs']['bad'])
        except ValueError as error:
            refused = error
        assert isinstance(refused, ValueError), 'opaque non-list completed clips must refuse archive fact extraction'
    finally:
        assert store == original and path.read_bytes() == disk
        assert private.read_bytes() == b'owned-private-artifact'


@pytest.mark.parametrize('clips', ['missing', [], [{'sha256': SHA}]], ids=['missing', 'empty', 'valid-list'])
def test_missing_empty_and_valid_completed_clip_lists_keep_legacy_compaction(tmp_path, clips):
    path, private, store = _stored_pair(tmp_path, clips)
    original, disk = copy.deepcopy(store), path.read_bytes()
    result, archived = compaction.compact_job_store(store, NOW, batch_size=2)
    assert [job['jobId'] for job in archived] == ['bad', 'good']
    assert result['jobs'] == {} and compaction.COMPACTION_SCAN_NEXT_KEY not in result
    expected = {'page': [SHA]} if isinstance(clips, list) and clips else {}
    assert result[compaction.ARCHIVE_INDEX_KEY]['usedClipSha256'] == expected
    assert store == original and path.read_bytes() == disk
    assert private.read_bytes() == b'owned-private-artifact'
