"""Read-only evidence: directory-valued descriptors must not expose descendants."""
from datetime import timedelta

import pytest
import routers.control_plane as cp
from tests.test_generated_media_retention import B, NOW, make_job, sweeper


@pytest.mark.parametrize('descriptor', ['primary_directory', 'thumbnail_directory'])
def test_directory_descriptor_plan_holds_unidentified_descendants(tmp_path, monkeypatch, descriptor):
    root = tmp_path / 'gen'
    bad_dir, bad = make_job(root, name='bad', age=timedelta(days=3), extra_files=())
    good_dir, good = make_job(root, name='good', age=timedelta(hours=3),
                              clips=((B, 'renders/good.mp4'),), extra_files=())
    if descriptor == 'primary_directory':
        bad['clips'][0]['path'] = 'renders'
    else:
        bad['clips'][0]['thumbnail']['path'] = 'thumbnails'
    store_path = tmp_path / 'jobs.json'
    monkeypatch.setattr(cp, '_jobs_path', lambda: store_path)
    cp.atomic_save(store_path, {'jobs': {'bad': bad, 'good': good}, 'byIdempotency': {}})
    stored_bytes = store_path.read_bytes()
    before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    worker, asked = sweeper(True)
    plan = worker.plan(cp._load_jobs(), root, NOW)
    assert before == {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert store_path.read_bytes() == stored_bytes
    assert (B, 100) in asked
    assert good_dir / 'renders/good.mp4' in plan.clip_files
    assert good_dir / 'thumbnails/0000.jpg' in plan.clip_files
    selected = [p for p in plan.clip_files + plan.stray_files if bad_dir in p.parents]
    assert selected == [], 'directory descriptors must hold unidentified descendants'
