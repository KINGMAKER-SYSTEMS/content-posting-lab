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
@pytest.mark.asyncio
async def test_legacy_foreign_source_without_a_claim_keeps_its_reservations(lab, monkeypatch, door):
    client, _, _ = lab
    response = client.post('/api/control-plane/v1/jobs', json=job_body(1), headers=headers('source-legacy-overlap'))
    job_id = response.json()['jobId']
    store = cp._load_jobs()
    job = store['jobs'][job_id]
    job.pop('sourceClaimVersion')
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
