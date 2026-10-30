"""Source windows stay held while another runtime still owns the job."""
import asyncio
import subprocess
import sys
import pytest
import routers.control_plane as cp
from tests.test_control_plane_source_execution import (
    lab, _isolated_source_dna, _advertise_zero_reasons, offline_shipstream_manifest,
    PAGE_ID, TOKEN, headers, job_body, publication,
)


@pytest.mark.parametrize("door", ["status", "replay"])
@pytest.mark.asyncio
async def test_foreign_runtime_status_does_not_release_a_running_source_job(lab, monkeypatch, tmp_path, door):
    client, _, _ = lab
    response = client.post('/api/control-plane/v1/jobs', json=job_body(1), headers=headers('source-overlap-test'))
    assert response.status_code == 200
    job_id = response.json()['jobId']
    entered, release = asyncio.Event(), asyncio.Event()
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'fixture')

    async def cached(*_):
        entered.set()
        await release.wait()
        return source

    async def encode(_source, destination, *_args, **_kwargs):
        from pathlib import Path
        Path(destination).write_bytes(b'fixture output')

    monkeypatch.setattr(cp, '_cached_source_master', cached)
    monkeypatch.setattr(cp, 'run_color_correct', encode)
    monkeypatch.setattr(cp, 'transforms_enabled', lambda *_: False)
    monkeypatch.setattr(cp, 'varies_clips', lambda *_: False)
    runner = asyncio.create_task(cp._run_dossier_source(job_id))
    await asyncio.wait_for(entered.wait(), 5)
    original = cp._load_jobs()['jobs'][job_id]
    planned = original['sourceCuts'][0]['slotId']
    recipe = cp._dossier_source_recipe(publication())
    try:
        monkeypatch.setattr(cp, '_GENERATION_RUNTIME_ID', 'another-runtime')
        if door == 'status':
            result = await cp.job_status(job_id, x_rt_page_id=PAGE_ID, authorization=f'Bearer {TOKEN}')
        else:
            replay = await asyncio.to_thread(client.post, '/api/control-plane/v1/jobs',
                json=job_body(1), headers=headers('source-overlap-test'))
            assert replay.status_code == 200
            result = replay.json()
        reserved, _ = cp._source_dna_cut_ledger(cp._load_jobs(), recipe, original['recipeVersion'])
        assert result['status'] == 'running'
        assert planned in reserved
        # A separate process cannot seize the exact same claim either.
        probe = await asyncio.to_thread(subprocess.run, [sys.executable, '-c',
            'from services.generation_recovery import runner_lock; import sys\n'
            'with runner_lock(sys.argv[1], sys.argv[2]) as acquired: print(int(acquired))',
            str(cp._jobs_path()), job_id], capture_output=True, text=True, check=True)
        assert probe.stdout.strip() == '0'

    finally:
        release.set()
        await runner
    completed = cp._load_jobs()['jobs'][job_id]
    assert completed['status'] == 'completed'
    assert len(completed['clips']) == 1
    reserved, _ = cp._source_dna_cut_ledger(cp._load_jobs(), recipe, original['recipeVersion'])
    assert planned in reserved, 'a delivered window stays reserved after completion'


@pytest.mark.asyncio
async def test_foreign_reader_fences_a_queued_runner_before_expiring_it(lab, monkeypatch):
    client, _, _ = lab
    response = client.post('/api/control-plane/v1/jobs', json=job_body(1), headers=headers('source-queued-overlap'))
    job_id = response.json()['jobId']
    monkeypatch.setattr(cp, '_GENERATION_RUNTIME_ID', 'another-runtime')
    status = await cp.job_status(job_id, x_rt_page_id=PAGE_ID, authorization=f'Bearer {TOKEN}')
    assert status['status'] == 'failed'
    calls = []
    async def unexpected(*_):
        calls.append(True)
        raise RuntimeError('fixture refused the cut')
    monkeypatch.setattr(cp, '_cached_source_master', unexpected)
    await cp._run_dossier_source(job_id)
    assert calls == [], 'a fenced job must never begin cutting'
    assert cp._load_jobs()['jobs'][job_id]['status'] == 'failed'


@pytest.mark.parametrize("door", ["status", "replay"])
@pytest.mark.parametrize("version", [None, True, "1", 2])
@pytest.mark.asyncio
async def test_legacy_foreign_source_without_a_claim_keeps_its_reservations(lab, monkeypatch, door, version):
    client, _, _ = lab
    response = client.post('/api/control-plane/v1/jobs', json=job_body(1), headers=headers('source-legacy-overlap'))
    job_id = response.json()['jobId']
    store = cp._load_jobs()
    job = store['jobs'][job_id]
    if version is None:
        job.pop('sourceClaimVersion')
    else:
        job['sourceClaimVersion'] = version
    job['status'] = 'running'
    planned = job['sourceCuts'][0]['slotId']
    cp.atomic_save(cp._jobs_path(), store)
    recipe = cp._dossier_source_recipe(publication())
    monkeypatch.setattr(cp, '_GENERATION_RUNTIME_ID', 'another-runtime')
    if door == 'status':
        result = await cp.job_status(job_id, x_rt_page_id=PAGE_ID, authorization=f'Bearer {TOKEN}')
    else:
        replay = await asyncio.to_thread(client.post, '/api/control-plane/v1/jobs',
            json=job_body(1), headers=headers('source-legacy-overlap'))
        assert replay.status_code == 200
        result = replay.json()
    reserved, _ = cp._source_dna_cut_ledger(cp._load_jobs(), recipe, job['recipeVersion'])
    assert result['status'] == 'running'
    assert planned in reserved


@pytest.mark.asyncio
async def test_cancelled_late_completion_cannot_revive_a_reassigned_window(lab, monkeypatch, tmp_path):
    import threading
    from pathlib import Path
    from services.control_plane_sources import SourceCut

    client, _, _ = lab
    response = client.post('/api/control-plane/v1/jobs', json=job_body(1), headers=headers('source-late-old'))
    old_id = response.json()['jobId']
    original = cp._load_jobs()['jobs'][old_id]
    cut = original['sourceCuts'][0]
    recipe = cp._dossier_source_recipe(publication())
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'fixture')
    async def cached(*_):
        return source
    async def encode(_source, destination, *_args, **_kwargs):
        Path(destination).write_bytes(b'fixture output')
    monkeypatch.setattr(cp, '_cached_source_master', cached)
    monkeypatch.setattr(cp, 'run_color_correct', encode)
    monkeypatch.setattr(cp, 'transforms_enabled', lambda *_: False)
    monkeypatch.setattr(cp, 'varies_clips', lambda *_: False)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    persist = cp._update_source_job
    def blocked(job_id, **fields):
        if job_id == old_id and fields.get('status') == 'completed':
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test barrier timed out')
            try:
                return persist(job_id, **fields)
            finally:
                finished.set()
        return persist(job_id, **fields)
    monkeypatch.setattr(cp, '_update_source_job', blocked)
    runner = asyncio.create_task(cp._run_dossier_source(old_id))
    try:
        assert await asyncio.to_thread(entered.wait, 5), 'completion reached the durable write'
        runner.cancel()
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        monkeypatch.setattr(cp, '_GENERATION_RUNTIME_ID', 'another-runtime')
        status = await cp.job_status(old_id, x_rt_page_id=PAGE_ID, authorization=f'Bearer {TOKEN}')
        assert status['status'] == 'failed'
        reserved, _ = cp._source_dna_cut_ledger(cp._load_jobs(), recipe, original['recipeVersion'])
        assert cut['slotId'] not in reserved
        # Force the actual create door to choose that now-free, valid old
        # window, demonstrating that an intervening job can own it.
        def select_old(recipe, quantity, served, *_args, **_kwargs):
            assert quantity == 1 and cut['slotId'] not in served
            master = next(m for m in recipe.masters if m.sha256 == cut['masterSha256'])
            return [SourceCut(master, cut['startMs'], cut['durationMs'], fixed_length=True)]
        monkeypatch.setattr(cp, 'plan_source_cuts', select_old)
        new = await asyncio.to_thread(client.post, '/api/control-plane/v1/jobs',
            json=job_body(1), headers=headers('source-late-new'))
        assert new.status_code == 200
        new_id = new.json()['jobId']
        assert cp._load_jobs()['jobs'][new_id]['sourceCuts'][0]['slotId'] == cut['slotId']
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5), 'the detached write settled'
    final = cp._load_jobs()['jobs'][old_id]
    assert final['status'] == 'failed', 'a late completion cannot revive the old job'
    assert final['clips'] == [], 'the old output was never made servable'


@pytest.mark.parametrize('stage', ['initial', 'progress'])
@pytest.mark.asyncio
async def test_cancelled_late_source_updates_cannot_revive_failed_job(lab, monkeypatch, tmp_path, stage):
    import threading
    from pathlib import Path

    client, _, _ = lab
    response = client.post('/api/control-plane/v1/jobs', json=job_body(1), headers=headers('source-late-' + stage))
    job_id = response.json()['jobId']
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'fixture')
    async def cached(*_):
        return source
    async def encode(_source, destination, *_args, **_kwargs):
        Path(destination).write_bytes(b'fixture output')
    monkeypatch.setattr(cp, '_cached_source_master', cached)
    monkeypatch.setattr(cp, 'run_color_correct', encode)
    monkeypatch.setattr(cp, 'transforms_enabled', lambda *_: False)
    monkeypatch.setattr(cp, 'varies_clips', lambda *_: False)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    persist = cp._update_source_job
    def blocked(target, **fields):
        is_initial = fields.get('status') == 'running'
        is_progress = 'progress' in fields and 'status' not in fields
        if target == job_id and ((stage == 'initial' and is_initial) or (stage == 'progress' and is_progress)):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test barrier timed out')
            try:
                return persist(target, **fields)
            finally:
                finished.set()
        return persist(target, **fields)
    monkeypatch.setattr(cp, '_update_source_job', blocked)
    runner = asyncio.create_task(cp._run_dossier_source(job_id))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        runner.cancel()
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        monkeypatch.setattr(cp, '_GENERATION_RUNTIME_ID', 'another-runtime')
        status = await cp.job_status(job_id, x_rt_page_id=PAGE_ID, authorization=f'Bearer {TOKEN}')
        assert status['status'] == 'failed'
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
    assert cp._load_jobs()['jobs'][job_id]['status'] == 'failed'


@pytest.mark.asyncio
async def test_source_setup_failure_handler_cannot_erase_completed_claim(lab):
    client, _, _ = lab
    response = client.post('/api/control-plane/v1/jobs', json=job_body(1), headers=headers('source-late-setup'))
    job_id = response.json()['jobId']
    async def raced(_job_id):
        cp._update_job(job_id, status='completed', clips=[{'proof': 'delivered'}])
        raise RuntimeError('late setup failure')
    await cp._guarded_job_runner(raced, job_id, 'source_setup_failed')
    job = cp._load_jobs()['jobs'][job_id]
    assert job['status'] == 'completed'
    assert job['clips'] == [{'proof': 'delivered'}]


@pytest.mark.asyncio
async def test_late_completion_wins_before_foreign_expiry_write(lab, monkeypatch):
    import threading

    client, _, _ = lab
    response = client.post('/api/control-plane/v1/jobs', json=job_body(1), headers=headers('source-expiry-race'))
    job_id = response.json()['jobId']
    original = cp._load_jobs()['jobs'][job_id]
    cut = original['sourceCuts'][0]['slotId']
    recipe = cp._dossier_source_recipe(publication())
    monkeypatch.setattr(cp, '_GENERATION_RUNTIME_ID', 'another-runtime')
    read, release = threading.Event(), threading.Event()
    real_get = cp._get_job_or_404
    calls = 0
    def gated(target):
        nonlocal calls
        result = real_get(target)
        calls += 1
        if calls == 2:
            read.set()
            if not release.wait(10):
                raise RuntimeError('test barrier timed out')
        return result
    monkeypatch.setattr(cp, '_get_job_or_404', gated)
    def query():
        return asyncio.run(cp.job_status(job_id, x_rt_page_id=PAGE_ID, authorization=f'Bearer {TOKEN}'))
    task = asyncio.create_task(asyncio.to_thread(query))
    try:
        assert await asyncio.to_thread(read.wait, 5), 'expiry read the active state'
        cp._update_source_job(job_id, status='completed', clips=[{'proof': 'delivered'}])
    finally:
        release.set()
    result = await task
    assert result['status'] == 'completed'
    final = cp._load_jobs()['jobs'][job_id]
    assert final['status'] == 'completed'
    reserved, _ = cp._source_dna_cut_ledger(cp._load_jobs(), recipe, original['recipeVersion'])
    assert cut in reserved, 'the delivered window remains reserved'


@pytest.mark.asyncio
async def test_setup_failure_does_not_fail_another_live_source_runner(lab):
    client, _, _ = lab
    response = client.post('/api/control-plane/v1/jobs', json=job_body(1), headers=headers('source-setup-claim'))
    job_id = response.json()['jobId']
    cp._update_job(job_id, status='running')
    async def failed_setup(_):
        raise RuntimeError('fixture setup failure')
    with cp.runner_lock(cp._jobs_path(), job_id) as acquired:
        assert acquired
        await cp._guarded_job_runner(failed_setup, job_id, 'source_setup_failed')
        assert cp._load_jobs()['jobs'][job_id]['status'] == 'running'


@pytest.mark.asyncio
async def test_cancelled_setup_failure_keeps_claim_until_write_settles(lab, monkeypatch):
    import threading

    client, _, _ = lab
    response = client.post('/api/control-plane/v1/jobs', json=job_body(1), headers=headers('source-fallback-cancel'))
    job_id = response.json()['jobId']
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    persist = cp._update_source_job
    def blocked(target, **fields):
        entered.set()
        if not release.wait(10):
            raise RuntimeError('test barrier timed out')
        try:
            return persist(target, **fields)
        finally:
            finished.set()
    monkeypatch.setattr(cp, '_update_source_job', blocked)
    async def failed_setup(_):
        raise RuntimeError('fixture setup failure')
    task = asyncio.create_task(cp._guarded_job_runner(failed_setup, job_id, 'source_setup_failed'))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        with cp.runner_lock(cp._jobs_path(), job_id) as acquired:
            assert not acquired, 'the failure write still owns the source claim'
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    assert cp._load_jobs()['jobs'][job_id]['status'] == 'failed'
    with cp.runner_lock(cp._jobs_path(), job_id) as acquired:
        assert acquired, 'the claim is released once the write settles'
