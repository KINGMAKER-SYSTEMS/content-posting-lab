"""Read-only resolver exception evidence; never sweep or unlink artifacts."""
from datetime import timedelta

import pytest
import routers.control_plane as cp
from tests.test_generated_media_retention import B, NOW, make_job, sweeper


@pytest.mark.parametrize('field', ['primary', 'thumbnail'])
def test_nul_descriptor_plan_holds_bad_job_and_keeps_confirmed_peer(tmp_path, monkeypatch, field):
    root = tmp_path / 'gen'
    bad_dir, bad = make_job(root, name='bad', age=timedelta(days=3), extra_files=())
    good_dir, good = make_job(root, name='good', age=timedelta(hours=3),
                              clips=((B, 'renders/good.mp4'),), extra_files=())
    if field == 'primary':
        bad['clips'][0]['path'] = 'renders/bad\x00.mp4'
    else:
        bad['clips'][0]['thumbnail']['path'] = 'thumbnails/bad\x00.jpg'
    store = tmp_path / 'jobs.json'
    monkeypatch.setattr(cp, '_jobs_path', lambda: store)
    cp.atomic_save(store, {'jobs': {'bad': bad, 'good': good}, 'byIdempotency': {}})
    saved = store.read_bytes()
    before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    worker, asked = sweeper(True)
    result = None
    resolver_error = None
    try:
        result = worker.plan(cp._load_jobs(), root, NOW)
    except ValueError as error:
        if 'null' not in str(error).lower():
            raise
        resolver_error = error
    assert store.read_bytes() == saved
    assert before == {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    if resolver_error is not None:
        assert asked == [], 'resolver failure must precede confirmation'
        raise AssertionError(
            'NUL descriptor escaped conservative planning: '
            f'{type(resolver_error).__name__}: {resolver_error}'
        ) from resolver_error
    assert result is not None
    assert not any(bad_dir in p.parents for p in result.clip_files + result.stray_files)
    assert result.clip_files == [good_dir / 'renders/good.mp4', good_dir / 'thumbnails/0000.jpg']
    assert asked == [(B, 100)]
