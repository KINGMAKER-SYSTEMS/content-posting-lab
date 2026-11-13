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

_start_import_runner = cp._start_page_source_import


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


@pytest.mark.parametrize('stage', ['initial', 'cancel', 'failure', 'setup'])
@pytest.mark.asyncio
async def test_import_claim_drains_all_state_writes_during_cancel_all(lab, monkeypatch, stage):
    client, intent, revision, _ = lab
    created = client.post('/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision)).json()
    job_id = created['jobId']
    attempt = cp._load_jobs()['jobs'][job_id]['sourceImportAttemptId']
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    download_entered = asyncio.Event()
    persist = cp._update_source_import_attempt
    def gated(target, target_attempt, **fields):
        is_initial = fields.get('status') == 'running'
        is_failed = fields.get('status') == 'failed'
        if target == job_id and ((stage == 'initial' and is_initial) or (stage != 'initial' and is_failed)):
            entered.set()
            assert release.wait(10)
            try: return persist(target, target_attempt, **fields)
            finally: finished.set()
        return persist(target, target_attempt, **fields)
    monkeypatch.setattr(cp, '_update_source_import_attempt', gated)
    async def blocked_download(*_):
        download_entered.set()
        if stage == 'failure':
            raise RuntimeError('fixture download failure')
        await asyncio.Event().wait()
    monkeypatch.setattr(cp, 'download_source_video', blocked_download)
    if stage == 'setup':
        async def broken_runner(*_):
            raise RuntimeError('fixture setup failure')
        monkeypatch.setattr(cp, '_run_page_source_import', broken_runner)
    runner = asyncio.create_task(cp._guarded_page_source_import(job_id, attempt))
    try:
        if stage == 'cancel':
            await asyncio.wait_for(download_entered.wait(), 5)
            runner.cancel()
        assert await asyncio.to_thread(entered.wait, 5)
        runner.cancel()
        for task in asyncio.all_tasks():
            if task is not runner and task is not asyncio.current_task() and getattr(task.get_coro(), '__qualname__', '') == 'to_thread':
                task.cancel()
        await asyncio.sleep(0)
        runner.cancel()
        await asyncio.sleep(0)
        assert not runner.done()
        with cp.runner_lock(cp._jobs_path(), job_id) as acquired:
            assert not acquired, 'the active persistence thread retains the import claim'
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await runner
    assert finished.is_set()
    with cp.runner_lock(cp._jobs_path(), job_id) as acquired:
        assert acquired
    if stage != 'initial':
        assert cp._load_jobs()['jobs'][job_id]['status'] == 'failed'


@pytest.mark.asyncio
async def test_import_completion_wins_before_stale_expiry_write(lab, monkeypatch):
    client, intent, revision, _ = lab
    created = client.post('/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision)).json()
    job_id = created['jobId']
    old = cp._load_jobs()['jobs'][job_id]
    attempt = old['sourceImportAttemptId']
    monkeypatch.setattr(cp, '_GENERATION_RUNTIME_ID', 'another-runtime')
    entered, release = threading.Event(), threading.Event()
    persist = cp._update_source_import_attempt
    def gated(target, target_attempt, **fields):
        if target == job_id and fields.get('error') == 'source_import_runtime_restarted':
            entered.set()
            assert release.wait(10)
        return persist(target, target_attempt, **fields)
    monkeypatch.setattr(cp, '_update_source_import_attempt', gated)
    def query():
        return asyncio.run(cp.job_status(job_id, x_rt_page_id=PAGE_ID, authorization=f'Bearer {TOKEN}'))
    task = asyncio.create_task(asyncio.to_thread(query))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        persist(job_id, attempt, status='completed', clips=[{'proof': 'delivered'}])
    finally:
        release.set()
    result = await task
    assert result['status'] == 'completed'
    assert cp._load_jobs()['jobs'][job_id]['clips'] == [{'proof': 'delivered'}]


@pytest.mark.asyncio
async def test_setup_failure_cannot_fail_another_import_owner(lab, monkeypatch):
    client, intent, revision, _ = lab
    created = client.post('/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision)).json()
    job_id = created['jobId']
    job = cp._load_jobs()['jobs'][job_id]
    cp._update_job(job_id, status='running')
    async def broken(*_):
        raise RuntimeError('fixture setup failure')
    monkeypatch.setattr(cp, '_run_page_source_import', broken)
    with cp.runner_lock(cp._jobs_path(), job_id) as acquired:
        assert acquired
        await cp._guarded_page_source_import(job_id, job['sourceImportAttemptId'])
        assert cp._load_jobs()['jobs'][job_id]['status'] == 'running'


@pytest.mark.asyncio
async def test_stale_status_cannot_cancel_a_new_import_attempt(lab):
    client, intent, revision, _ = lab
    created = client.post('/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision)).json()
    job_id = created['jobId']
    old = cp._load_jobs()['jobs'][job_id]['sourceImportAttemptId']
    cp._update_job(job_id, status='failed', error='source_import_runtime_restarted')
    retry = await asyncio.to_thread(client.post, '/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision))
    assert retry.json()['status'] == 'queued'
    current = cp._load_jobs()['jobs'][job_id]['sourceImportAttemptId']
    assert current != old
    task = asyncio.create_task(asyncio.Event().wait())
    task._source_import_attempt_id = current
    cp._source_import_tasks[job_id] = task
    try:
        assert not await cp._cancel_page_source_import(job_id, old)
        assert not task.cancelled()
    finally:
        cp._source_import_tasks.pop(job_id, None)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_old_child_late_open_cannot_touch_new_attempt_or_use_old_token(lab, tmp_path):
    import subprocess
    import sys
    import time

    client, intent, revision, _ = lab
    created = client.post('/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision)).json()
    job_id = created['jobId']
    old = cp._load_jobs()['jobs'][job_id]
    old_root, old_token = Path(old['artifactRoot']), old['token']
    (old_root / 'source').mkdir(parents=True)
    started, release = tmp_path / 'child-started', tmp_path / 'child-release'
    script = (
        'import pathlib,sys,time\n'
        'ready,gate,target=map(pathlib.Path,sys.argv[1:])\n'
        'ready.write_text("ready")\n'
        'while not gate.exists(): time.sleep(0.01)\n'
        'target.write_bytes(b"old-child")\n'
    )
    child = subprocess.Popen([sys.executable, '-c', script, str(started), str(release), str(old_root / 'source' / 'source.mp4')])
    try:
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert started.exists()
        cp._update_job(job_id, status='failed', error='source_import_runtime_restarted')
        retry = await asyncio.to_thread(client.post, '/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision))
        assert retry.json()['status'] == 'queued'
        current = cp._load_jobs()['jobs'][job_id]
        new_root = Path(current['artifactRoot'])
        assert new_root != old_root and new_root.parent == old_root.parent
        new_source = new_root / 'source' / 'source.mp4'
        new_source.parent.mkdir(parents=True)
        new_source.write_bytes(b'fresh')
        release.write_text('go')
        assert await asyncio.to_thread(child.wait, 5) == 0
        assert (old_root / 'source' / 'source.mp4').read_bytes() == b'old-child'
        assert new_source.read_bytes() == b'fresh'
        cp._update_source_import_attempt(job_id, current['sourceImportAttemptId'], status='completed',
            clips=[{'path': 'source/source.mp4', 'name': 'source.mp4'}])
        with pytest.raises(Exception) as error:
            cp.job_download(job_id, 0, token=old_token)
        assert getattr(error.value, 'status_code', None) == 403
        assert Path(cp.job_download(job_id, 0, token=current['token']).path).read_bytes() == b'fresh'
    finally:
        release.touch(exist_ok=True)
        if child.poll() is None:
            child.kill()
            await asyncio.to_thread(child.wait)


def test_foreign_active_import_still_occupies_page_capacity(lab):
    client, intent, revision, _ = lab
    first = client.post('/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision))
    assert first.status_code == 200
    job_id = first.json()['jobId']
    cp._update_job(job_id, runtimeId='another-runtime')
    second = client.post('/api/control-plane/v1/source-imports',
        headers={**HEADERS, 'Idempotency-Key': 'source:intake:another-attempt'}, json=_body(intent, revision))
    assert second.status_code == 409
    assert second.json()['detail'] == 'source import is already active for this page'
    assert cp._load_jobs()['jobs'][job_id]['status'] == 'queued'


def install_blocked_download_join(monkeypatch):
    """Keep the real download join; replace only the media subprocess boundary."""
    from services import control_plane_source_imports as imports

    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def download(*_args, **_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            raise
    async def through_real_join(url, destination):
        return await imports._download_with_workspace_limit(url, destination)
    monkeypatch.setattr(imports, 'download_video', download)
    monkeypatch.setattr(cp, 'download_source_video', through_real_join)
    return entered, cleaning, release


@pytest.mark.parametrize('cancel_request', [False, True])
@pytest.mark.asyncio
async def test_expired_import_keeps_its_retry_reason_when_status_is_cancelled(lab, monkeypatch, cancel_request):
    client, intent, revision, _ = lab
    created = client.post('/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision))
    assert created.status_code == 200
    job_id = created.json()['jobId']
    attempt = cp._load_jobs()['jobs'][job_id]['sourceImportAttemptId']
    entered, cleaning, release = install_blocked_download_join(monkeypatch)
    _start_import_runner(job_id)
    runner = cp._source_import_tasks[job_id]
    status = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        cp._update_job(job_id, createdAt=(datetime.now(timezone.utc) - timedelta(
            seconds=cp.SOURCE_IMPORT_ACTIVE_DEADLINE_SECONDS + 1)).isoformat())
        status = asyncio.create_task(cp.job_status(job_id, x_rt_page_id=PAGE_ID, authorization=f'Bearer {TOKEN}'))
        await asyncio.wait_for(cleaning.wait(), 5)
        if cancel_request:
            status.cancel()
            await asyncio.sleep(0)
        assert not runner.done(), 'expiry must join download cleanup'
        with cp.runner_lock(cp._jobs_path(), job_id) as acquired:
            assert not acquired, 'the old attempt still owns its claim while cleanup waits'
        release.set()
        result = (await asyncio.gather(status, return_exceptions=True))[0]
        assert isinstance(result, (dict, asyncio.CancelledError))
        final = cp._load_jobs()['jobs'][job_id]
        assert final['status'] == 'failed'
        assert final['error'] == 'source_import_runtime_restarted'
        assert final['sourceImportAttemptId'] == attempt
        retry = await asyncio.to_thread(client.post, '/api/control-plane/v1/source-imports',
            headers=HEADERS, json=_body(intent, revision))
        assert retry.status_code == 200
        assert retry.json()['status'] == 'queued'
        assert cp._load_jobs()['jobs'][job_id]['sourceImportAttemptId'] != attempt
    finally:
        release.set()
        if status is not None and not status.done():
            status.cancel()
        if not runner.done():
            runner.cancel()
        await asyncio.gather(runner, *([status] if status is not None else []), return_exceptions=True)
        if cp._source_import_tasks.get(job_id) is runner:
            cp._source_import_tasks.pop(job_id, None)


@pytest.mark.parametrize('repeat_cancel', [False, True, 'expiry'])
@pytest.mark.asyncio
async def test_ordinary_import_cancellation_stays_terminal_after_join(lab, monkeypatch, repeat_cancel):
    client, intent, revision, _ = lab
    created = client.post('/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision))
    assert created.status_code == 200
    job_id = created.json()['jobId']
    attempt = cp._load_jobs()['jobs'][job_id]['sourceImportAttemptId']
    entered, cleaning, release = install_blocked_download_join(monkeypatch)
    _start_import_runner(job_id)
    runner = cp._source_import_tasks[job_id]
    try:
        await asyncio.wait_for(entered.wait(), 5)
        runner.cancel()
        await asyncio.wait_for(cleaning.wait(), 5)
        if repeat_cancel:
            if repeat_cancel == 'expiry':
                runner.cancel('source_import_runtime_restarted')
            else:
                runner.cancel()
            await asyncio.sleep(0)
        assert not runner.done(), 'ordinary cancellation must also join cleanup'
        with cp.runner_lock(cp._jobs_path(), job_id) as acquired:
            assert not acquired
        release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await runner
        assert cancelled.value.args == ()
        final = cp._load_jobs()['jobs'][job_id]
        assert final['status'] == 'failed'
        assert final['error'] == 'source_import_cancelled'
        retry = await asyncio.to_thread(client.post, '/api/control-plane/v1/source-imports',
            headers=HEADERS, json=_body(intent, revision))
        assert retry.status_code == 200
        assert retry.json()['status'] == 'failed'
        assert cp._load_jobs()['jobs'][job_id]['sourceImportAttemptId'] == attempt
    finally:
        release.set()
        if not runner.done():
            runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        if cp._source_import_tasks.get(job_id) is runner:
            cp._source_import_tasks.pop(job_id, None)


@pytest.mark.parametrize('failure', ['workspace', 'timeout', 'storage'])
@pytest.mark.parametrize('later_expiry_cancel', [False, True])
@pytest.mark.asyncio
async def test_download_failure_is_not_made_retryable_by_late_expiry_cancel(
    lab, monkeypatch, failure, later_expiry_cancel,
):
    from services import control_plane_source_imports as imports

    client, intent, revision, _ = lab
    created = client.post('/api/control-plane/v1/source-imports', headers=HEADERS, json=_body(intent, revision))
    assert created.status_code == 200
    job_id = created.json()['jobId']
    attempt = cp._load_jobs()['jobs'][job_id]['sourceImportAttemptId']
    entered, cleaning, release = install_blocked_download_join(monkeypatch)
    monkeypatch.setattr(imports, 'SOURCE_IMPORT_POLL_SECONDS', 0.001)
    if failure == 'workspace':
        monkeypatch.setattr(imports, '_tree_bytes', lambda _: imports.MAX_SOURCE_IMPORT_WORKSPACE_BYTES + 1)
        expected_detail = 'workspace limit'
    elif failure == 'timeout':
        monkeypatch.setattr(imports, 'MAX_SOURCE_IMPORT_SECONDS', 0.001)
        expected_detail = 'download timed out'
    else:
        def broken_storage(_):
            raise OSError('fixture storage fault')
        monkeypatch.setattr(imports, '_tree_bytes', broken_storage)
        expected_detail = 'fixture storage fault'
    _start_import_runner(job_id)
    runner = cp._source_import_tasks[job_id]
    try:
        await asyncio.wait_for(cleaning.wait(), 5)
        assert entered.is_set(), 'the download was active before its failure began cleanup'
        if later_expiry_cancel:
            runner.cancel('source_import_runtime_restarted')
            await asyncio.sleep(0)
            runner.cancel()
            await asyncio.sleep(0)
        assert not runner.done()
        with cp.runner_lock(cp._jobs_path(), job_id) as acquired:
            assert not acquired, 'terminal-error cleanup must also retain the import claim'
        release.set()
        result = (await asyncio.gather(runner, return_exceptions=True))[0]
        assert result is None or isinstance(result, asyncio.CancelledError)
        final = cp._load_jobs()['jobs'][job_id]
        assert final['status'] == 'failed'
        assert final['error'] != 'source_import_runtime_restarted'
        if not later_expiry_cancel:
            assert expected_detail in final['error']
        retry = await asyncio.to_thread(client.post, '/api/control-plane/v1/source-imports',
            headers=HEADERS, json=_body(intent, revision))
        assert retry.status_code == 200
        assert retry.json()['status'] == 'failed'
        assert cp._load_jobs()['jobs'][job_id]['sourceImportAttemptId'] == attempt
    finally:
        release.set()
        if not runner.done():
            runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        if cp._source_import_tasks.get(job_id) is runner:
            cp._source_import_tasks.pop(job_id, None)
