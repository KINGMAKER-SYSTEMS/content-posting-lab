"""Import attempts retain ownership until their persistence threads settle."""
import asyncio
import hashlib
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import routers.control_plane as cp
from tests.test_control_plane_source_imports import lab, _body, HEADERS, PAGE_ID, TOKEN
from services.control_plane_source_imports import SourceImportArtifact, SourceImportMedia


def install_fake_media(monkeypatch):
    data = b'fake import'
    digest = hashlib.sha256(data).hexdigest()
    media = SourceImportMedia(42500, 1080, 1920, 'h264', 'yuv420p', 30.0, 0)
    async def fake_download(_url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return SourceImportArtifact(destination, digest, len(data), media, digest, len(data), media)
    async def fake_thumbnail(root, _video, _index):
        path = root / 'thumbnails' / '0000.jpg'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'jpg')
        return cp._generated_manifest(root, path)
    monkeypatch.setattr(cp, 'download_source_video', fake_download)
    monkeypatch.setattr(cp, '_thumbnail_manifest', fake_thumbnail)
    return data


@pytest.mark.asyncio
async def test_expired_import_waits_for_late_completion_before_reusing_root(lab, monkeypatch):
    client, intent, revision, _ = lab
    created = client.post('/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision)).json()
    job_id = created['jobId']
    data = install_fake_media(monkeypatch)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    persist = cp._update_source_import_attempt
    def gated(target, attempt, **fields):
        if target == job_id and fields.get('status') == 'completed':
            entered.set()
            assert release.wait(10)
            try: return persist(target, attempt, **fields)
            finally: finished.set()
        return persist(target, attempt, **fields)
    monkeypatch.setattr(cp, '_update_source_import_attempt', gated)
    task = asyncio.create_task(cp._run_page_source_import(job_id))
    cp._source_import_tasks[job_id] = task
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        store = cp._load_jobs()
        store['jobs'][job_id]['createdAt'] = (datetime.now(timezone.utc) - timedelta(seconds=cp.SOURCE_IMPORT_ACTIVE_DEADLINE_SECONDS + 1)).isoformat()
        cp.atomic_save(cp._jobs_path(), store)
        status_task = asyncio.create_task(cp.job_status(job_id, x_rt_page_id=PAGE_ID, authorization=f'Bearer {TOKEN}'))
        await asyncio.sleep(0)
        assert not status_task.done(), 'status must join the persistence thread before retry'
        release.set()
        status = await status_task
        assert finished.is_set()
        assert status['status'] == 'completed', 'a completion that won is not overwritten as a restart'
        retry = await asyncio.to_thread(client.post, '/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision))
        assert retry.json()['status'] == 'completed'
        final = cp._load_jobs()['jobs'][job_id]
        root = Path(final['artifactRoot'])
        assert (root / final['clips'][0]['path']).read_bytes() == data
    finally:
        release.set()
        cp._source_import_tasks.pop(job_id, None)


@pytest.mark.asyncio
async def test_orphaned_old_attempt_write_cannot_complete_a_new_retry(lab):
    client, intent, revision, _ = lab
    created = client.post('/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision)).json()
    job_id = created['jobId']
    old = cp._load_jobs()['jobs'][job_id]
    old_attempt = old['sourceImportAttemptId']
    cp._update_job(job_id, status='failed', error='source_import_runtime_restarted')
    retry = await asyncio.to_thread(client.post, '/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision))
    assert retry.json()['status'] == 'queued'
    fresh = cp._load_jobs()['jobs'][job_id]
    assert fresh['sourceImportAttemptId'] != old_attempt
    assert cp._update_source_import_attempt(job_id, old_attempt, status='completed', clips=[{'path': 'old'}]) is None
    final = cp._load_jobs()['jobs'][job_id]
    assert final['status'] == 'queued'
    assert final['sourceImportAttemptId'] == fresh['sourceImportAttemptId']
    assert final['clips'] == []


@pytest.mark.parametrize('door', ['status', 'replay'])
@pytest.mark.asyncio
async def test_foreign_reader_cannot_restart_a_live_import(lab, monkeypatch, door):
    client, intent, revision, _ = lab
    created = client.post('/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision)).json()
    job_id = created['jobId']
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked_download(_url, _destination):
        entered.set()
        await release.wait()
        raise RuntimeError('fixture stopped before output')
    monkeypatch.setattr(cp, 'download_source_video', blocked_download)
    runner = asyncio.create_task(cp._run_page_source_import(job_id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        before = cp._load_jobs()['jobs'][job_id]
        monkeypatch.setattr(cp, '_GENERATION_RUNTIME_ID', 'another-runtime')
        if door == 'status':
            result = await cp.job_status(job_id, x_rt_page_id=PAGE_ID, authorization=f'Bearer {TOKEN}')
        else:
            response = await asyncio.to_thread(client.post, '/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision))
            assert response.status_code == 200
            result = response.json()
        assert result['status'] == 'running'
        after = cp._load_jobs()['jobs'][job_id]
        assert after['sourceImportAttemptId'] == before['sourceImportAttemptId']
        assert after['token'] == before['token']
    finally:
        release.set()
        await runner


@pytest.mark.parametrize('door', ['status', 'replay'])
@pytest.mark.parametrize('marker', ['legacy', 'boolean', 'bad_attempt'])
@pytest.mark.asyncio
async def test_legacy_foreign_import_is_not_guessed_dead(lab, monkeypatch, door, marker):
    client, intent, revision, _ = lab
    created = client.post('/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision)).json()
    job_id = created['jobId']
    store = cp._load_jobs()
    if marker == 'legacy':
        store['jobs'][job_id].pop('sourceImportClaimVersion')
        store['jobs'][job_id].pop('sourceImportAttemptId')
    elif marker == 'boolean':
        store['jobs'][job_id]['sourceImportClaimVersion'] = True
    else:
        store['jobs'][job_id]['sourceImportAttemptId'] = 'invalid'
    store['jobs'][job_id]['status'] = 'running'
    cp.atomic_save(cp._jobs_path(), store)
    monkeypatch.setattr(cp, '_GENERATION_RUNTIME_ID', 'another-runtime')
    if door == 'status':
        result = await cp.job_status(job_id, x_rt_page_id=PAGE_ID, authorization=f'Bearer {TOKEN}')
    else:
        response = await asyncio.to_thread(client.post, '/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision))
        assert response.status_code == 200
        result = response.json()
    assert result['status'] == 'running'


@pytest.mark.asyncio
async def test_old_queued_runner_cannot_start_a_new_attempt(lab, monkeypatch):
    client, intent, revision, _ = lab
    created = client.post('/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision)).json()
    job_id = created['jobId']
    old_attempt = cp._load_jobs()['jobs'][job_id]['sourceImportAttemptId']
    monkeypatch.setattr(cp, '_GENERATION_RUNTIME_ID', 'another-runtime')
    status = await cp.job_status(job_id, x_rt_page_id=PAGE_ID, authorization=f'Bearer {TOKEN}')
    assert status['status'] == 'failed'
    retry = await asyncio.to_thread(client.post, '/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision))
    assert retry.json()['status'] == 'queued'
    calls = []
    async def unexpected(*_):
        calls.append(True)
        raise RuntimeError('unexpected download')
    monkeypatch.setattr(cp, 'download_source_video', unexpected)
    await cp._run_page_source_import(job_id, old_attempt)
    assert calls == []
    assert cp._load_jobs()['jobs'][job_id]['status'] == 'queued'


@pytest.mark.asyncio
async def test_import_claim_survives_cancel_all_until_completion_write_settles(lab, monkeypatch):
    client, intent, revision, _ = lab
    created = client.post('/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision)).json()
    job_id = created['jobId']
    install_fake_media(monkeypatch)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    persist = cp._update_source_import_attempt
    def gated(target, attempt, **fields):
        if target == job_id and fields.get('status') == 'completed':
            entered.set()
            assert release.wait(10)
            try: return persist(target, attempt, **fields)
            finally: finished.set()
        return persist(target, attempt, **fields)
    monkeypatch.setattr(cp, '_update_source_import_attempt', gated)
    runner = asyncio.create_task(cp._run_page_source_import(job_id))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        runner.cancel()
        for task in asyncio.all_tasks():
            if task is not runner and task is not asyncio.current_task() and getattr(task.get_coro(), '__qualname__', '') == 'to_thread':
                task.cancel()
        await asyncio.sleep(0)
        runner.cancel()
        await asyncio.sleep(0)
        with cp.runner_lock(cp._jobs_path(), job_id) as acquired:
            assert not acquired
        assert not runner.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await runner
    assert finished.is_set()
    assert cp._load_jobs()['jobs'][job_id]['status'] == 'completed'
