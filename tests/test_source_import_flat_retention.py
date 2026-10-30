"""Phase-A flat ownership is valid, without authorizing private sibling cleanup."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import routers.control_plane as cp
from services import generated_media_retention as retention
from tests.test_control_plane_source_imports import lab, _body, HEADERS
from tests.test_source_import_late_retry import install_fake_media


@pytest.fixture
def completed_flat(lab, monkeypatch):
    client, intent, revision, _ = lab
    monkeypatch.setattr(cp.source_import_layout, 'PRIVATE_ATTEMPT_ROOTS', False)
    data = install_fake_media(monkeypatch)
    created = client.post('/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision))
    assert created.status_code == 200
    job_id = created.json()['jobId']
    asyncio.run(cp._run_page_source_import(job_id))
    store = cp._load_jobs()
    job = store['jobs'][job_id]
    directory = Path(job['artifactRoot'])
    assert job['status'] == 'completed' and job['sourceImportClaimVersion'] == 1
    assert directory.name == job_id and directory.parent.name == 'source-imports'
    now = datetime.now(timezone.utc) + timedelta(days=3)
    return store, job_id, directory, now, data


def test_actual_a_producer_flat_result_ages_and_requires_r2_confirmation(completed_flat):
    store, job_id, directory, now, data = completed_flat
    stray = directory / 'source' / 'original.part'
    stray.write_bytes(b'partial')
    clip = directory / store['jobs'][job_id]['clips'][0]['path']
    original = copy.deepcopy(store)
    worker = retention.GeneratedMediaRetention(confirm=lambda *_: False)
    worker.sweep(store, cp._generation_root(), now)
    assert clip.read_bytes() == data and not stray.exists()
    worker = retention.GeneratedMediaRetention(confirm=lambda *_: True)
    summary = worker.sweep(store, cp._generation_root(), now)
    assert summary['clip_files'] == 2 and not clip.exists()
    assert store == original


@pytest.mark.parametrize('timing', ['before_plan', 'during_confirmation', 'after_plan'])
def test_completed_flat_never_walks_new_private_child(completed_flat, timing):
    store, job_id, directory, now, _ = completed_flat
    private = directory / ('d' * 32) / 'source' / 'live.mp4'
    def create_child():
        private.parent.mkdir(parents=True, exist_ok=True)
        private.write_bytes(b'live retry')
    def confirm(*_):
        if timing == 'during_confirmation':
            create_child()
        return True
    worker = retention.GeneratedMediaRetention(confirm=confirm)
    if timing == 'before_plan':
        create_child()
    if timing == 'after_plan':
        original_plan = worker.plan
        def plan_then_child(*args, **kwargs):
            plan = original_plan(*args, **kwargs)
            create_child()
            return plan
        worker.plan = plan_then_child
    worker.sweep(store, cp._generation_root(), now)
    assert private.read_bytes() == b'live retry'


@pytest.mark.parametrize('change', ['boolean_version', 'missing_version', 'attempt', 'runtime', 'page', 'job', 'source_kind', 'active', 'failed', 'restartable_failed'])
def test_unproven_or_noncompleted_flat_rows_are_never_cleaned(completed_flat, change):
    store, job_id, directory, now, data = completed_flat
    job = store['jobs'][job_id]
    if change == 'boolean_version': job['sourceImportClaimVersion'] = True
    elif change == 'missing_version': job.pop('sourceImportClaimVersion')
    elif change == 'source_kind': job['sourceKind'] = 'generated'
    elif change == 'attempt': job['sourceImportAttemptId'] = 'invalid'
    elif change == 'runtime': job['runtimeId'] = 'unknown'
    elif change == 'page': job['pageId'] = 'acct:other'
    elif change == 'job': job['jobId'] = 'cpl-' + 'e' * 16
    elif change == 'active': job['status'] = 'running'
    else:
        job['status'] = 'failed'
        job['error'] = 'source_import_runtime_restarted' if change == 'restartable_failed' else 'source_import_cancelled'
    asked = []
    worker = retention.GeneratedMediaRetention(confirm=lambda *args: asked.append(args) or True)
    plan = worker.plan(store, cp._generation_root(), now)
    # A failed flat snapshot may be followed by a same-root retry before sweep.
    clip = directory / job['clips'][0]['path']
    if change in {'failed', 'restartable_failed'}:
        clip.write_bytes(b'new same-root retry')
    worker.plan = lambda *_args, **_kwargs: plan
    worker.sweep(store, cp._generation_root(), now)
    assert asked == [] and clip.exists()
    assert not plan.clip_files and not plan.stray_files


def test_flat_does_not_follow_manifest_into_private_or_symlink_namespace(completed_flat):
    store, job_id, directory, now, _ = completed_flat
    private = directory / ('d' * 32) / 'source' / 'live.mp4'
    private.parent.mkdir(parents=True)
    private.write_bytes(b'live')
    # Even a corrupt manifest cannot grant deletion of a private child's bytes.
    store['jobs'][job_id]['clips'] = [{'path': str(private.relative_to(directory)),
        'sha256': 'd' * 64, 'bytes': 4, 'thumbnail': {}}]
    (directory / 'source' / 'alias.mp4').symlink_to(private)
    worker = retention.GeneratedMediaRetention(confirm=lambda *_: True)
    worker.sweep(store, cp._generation_root(), now)
    assert private.read_bytes() == b'live'
    assert (directory / 'source' / 'alias.mp4').is_symlink()


def test_active_descendant_protects_completed_flat_group(completed_flat):
    store, job_id, directory, now, data = completed_flat
    store['jobs']['active'] = {'status':'running','artifactRoot':str(directory / ('e' * 32))}
    worker = retention.GeneratedMediaRetention(confirm=lambda *_: True)
    plan = worker.plan(store, cp._generation_root(), now)
    assert not plan.clip_files and not plan.stray_files


@pytest.mark.parametrize('answer', [False, None])
def test_completed_flat_unconfirmed_bytes_survive_age_and_pressure(completed_flat, answer):
    store, job_id, directory, now, data = completed_flat
    clip = directory / store['jobs'][job_id]['clips'][0]['path']
    worker = retention.GeneratedMediaRetention(confirm=lambda *_: answer)
    worker.sweep(store, cp._generation_root(), now, pressure='floor')
    assert clip.read_bytes() == data


def test_completed_flat_settles_before_any_confirmation(completed_flat):
    store, job_id, directory, now, _ = completed_flat
    asked = []
    worker = retention.GeneratedMediaRetention(confirm=lambda *args: asked.append(args) or True)
    stamp = datetime.fromisoformat(store['jobs'][job_id]['completedAt'])
    worker.sweep(store, cp._generation_root(), stamp + timedelta(minutes=30))
    assert not asked and directory.exists()


@pytest.mark.parametrize('page', ['.', '..'])
def test_dot_page_cannot_normalize_away_exact_flat_owner_path(tmp_path, page):
    root = (tmp_path / 'generated').resolve()
    root.mkdir()
    job_id = 'cpl-' + 'a' * 16
    job = {'sourceKind':'page_source_import', 'status':'completed', 'jobId':job_id,
           'pageId':page, 'artifactRoot':str((root / page / 'source-imports' / job_id).resolve()),
           'sourceImportClaimVersion':1, 'sourceImportAttemptId':'b' * 32, 'runtimeId':'c' * 32}
    assert retention._completed_flat_import_dir(root, job_id, job) is None
