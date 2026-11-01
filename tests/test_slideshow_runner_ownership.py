"""A live runner must retain its plan until final publication settles."""
import asyncio
import threading
from pathlib import Path

import pytest
import routers.control_plane as cp

ORIGINAL_START = cp._start_syzygy_slideshow
from tests.test_control_plane_slideshow_execution import (
    lab, headers, job_body, _fake_syzygy_boundary, resolve_slideshow_recipe,
)


@pytest.mark.asyncio
async def test_foreign_status_cannot_release_live_slideshow_plan(lab, monkeypatch):
    client, _, _, library, payload = lab
    first = client.post('/api/control-plane/v1/jobs', json=job_body(1, payload),
                        headers=headers('ownership-first'))
    assert first.status_code == 200, first.text
    first_id = first.json()['jobId']
    first_job = cp._load_jobs()['jobs'][first_id]
    signature = first_job['slideshowPlan'][0]['signature']
    recipe = resolve_slideshow_recipe(payload)
    # Find a distinct valid Lab job id whose real planner chooses this same
    # plan when the reservation is absent; no planner behavior is stubbed.
    candidate = None
    for number in range(1, 10000):
        token = f'{number:016x}'
        job_id = cp.JOB_ID_PREFIX + token
        if job_id == first_id:
            continue
        plans = cp.plan_slideshows(recipe, library, 1, set(), job_id)
        if plans and plans[0]['signature'] == signature:
            candidate = token
            break
    assert candidate is not None
    _fake_syzygy_boundary(monkeypatch, library)
    reached, release = threading.Event(), threading.Event()
    actual_update = cp._update_slideshow_job
    def pause_completion(job_id, attempt, runtime, **fields):
        if job_id == first_id and fields.get('status') == 'completed':
            reached.set()
            assert release.wait(10)
        return actual_update(job_id, attempt, runtime, **fields)
    monkeypatch.setattr(cp, '_update_slideshow_job', pause_completion)
    task = asyncio.create_task(cp._run_syzygy_slideshow(first_id))
    try:
        assert await asyncio.to_thread(reached.wait, 10)
        stored = cp._load_jobs()['jobs'][first_id]
        assert stored['status'] == 'running' and len(stored['clips']) == 1
        assert signature in cp._slideshow_unavailable_signatures(cp._load_jobs(), recipe, first_job['pageId'])
        # A foreign Lab runtime observes the actual authenticated status API.
        monkeypatch.setattr(cp, '_GENERATION_RUNTIME_ID', 'foreign-runtime')
        observed = client.get(f'/api/control-plane/v1/jobs/{first_id}', headers=headers())
        assert observed.status_code == 200, observed.text
        observed_status = observed.json()['status']
        replay = client.post('/api/control-plane/v1/jobs', json=job_body(1, payload),
                             headers=headers('ownership-first'))
        assert replay.status_code == 200 and replay.json()['status'] == 'running'
        monkeypatch.setattr(cp._secrets, 'token_hex', lambda n: candidate if n == 8 else 'a' * (2 * n))
        second = client.post('/api/control-plane/v1/jobs', json=job_body(1, payload),
                             headers=headers('ownership-second'))
        second_status = second.status_code
        second_id = second.json().get('jobId') if second_status == 200 else None
        second_signature = (cp._load_jobs()['jobs'][second_id]['slideshowPlan'][0]['signature']
                            if second_id else None)
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=10)
    final = client.get(f'/api/control-plane/v1/jobs/{first_id}/artifacts', headers=headers())
    assert final.status_code == 200, final.text
    assert len(final.json()['artifacts']) == 1
    assert observed_status == 'running', f'foreign status released live job: {observed_status}; second={second_status}, same_plan={second_signature == signature}'
    assert second_signature != signature


@pytest.mark.asyncio
@pytest.mark.parametrize('confirmed', [False, True])
@pytest.mark.parametrize('legacy_restart', [False, True])
async def test_failed_partial_clip_remains_reserved_and_archived(lab, monkeypatch, confirmed, legacy_restart):
    client, _, _, library, payload = lab
    first = client.post('/api/control-plane/v1/jobs', json=job_body(2, payload),
                        headers=headers('partial-ownership-first'))
    assert first.status_code == 200
    job_id = first.json()['jobId']
    calls = _fake_syzygy_boundary(monkeypatch, library)
    real_render = cp.run_color_correct
    count = 0
    async def fail_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError('second render failed')
        return await real_render(*args, **kwargs)
    monkeypatch.setattr(cp, 'run_color_correct', fail_second)
    await cp._run_syzygy_slideshow(job_id)
    job = cp._load_jobs()['jobs'][job_id]
    assert job['status'] == 'failed' and len(job['clips']) == 1
    if legacy_restart:
        store = cp._load_jobs()
        legacy = store['jobs'][job_id]
        legacy.update(status='running', runtimeId='old-runtime')
        legacy.pop('slideshowClaimVersion')
        legacy.pop('slideshowAttemptId')
        cp.atomic_save(cp._jobs_path(), store)
        observed = client.get(f'/api/control-plane/v1/jobs/{job_id}', headers=headers())
        assert observed.status_code == 200 and observed.json()['status'] == 'failed'
        assert observed.json()['error'] == 'generation_runtime_restarted'
        job = cp._load_jobs()['jobs'][job_id]
        assert len(job['clips']) == 1
    artifact = client.get(f'/api/control-plane/v1/jobs/{job_id}/artifacts', headers=headers())
    assert artifact.status_code == 200 and len(artifact.json()['artifacts']) == 1
    download = client.get(artifact.json()['artifacts'][0]['url'])
    assert download.status_code == 200 and download.content
    signature = job['clips'][0]['source']['planSignature']
    recipe = resolve_slideshow_recipe(payload)
    assert signature in cp._slideshow_unavailable_signatures(cp._load_jobs(), recipe, job['pageId'])
    archive = cp.compaction.extract_index_entries(job)
    assert signature in {entry['signature'] for entry in archive['slideshow']}
    assert (job['pageId'], job['clips'][0]['sha256']) in archive['usedClipSha256']
    assert len(archive['slideshow']) == 1  # failed undelivered plan is free
    second = client.post('/api/control-plane/v1/jobs', json=job_body(1, payload),
                         headers=headers('partial-ownership-second'))
    assert second.status_code == 200, second.text
    second_id = second.json()['jobId']
    second_plan = cp._load_jobs()['jobs'][second_id]['slideshowPlan'][0]['signature']
    assert second_plan != signature
    with pytest.raises(RuntimeError, match='duplicate_slideshow_artifact'):
        cp._claim_unique_slideshow_clip(second_id, job['clips'][0], second_plan)
    from datetime import datetime, timedelta, timezone
    from services.generated_media_retention import GeneratedMediaRetention
    now = datetime.now(timezone.utc) + timedelta(days=3)
    media = Path(job['artifactRoot']) / job['clips'][0]['path']
    thumbnail = Path(job['artifactRoot']) / job['clips'][0]['thumbnail']['path']
    assert media.is_file() and thumbnail.is_file()
    asked = []
    def confirm(sha, size):
        asked.append((sha, size))
        return confirmed
    sweeper = GeneratedMediaRetention(confirm=confirm, monotonic=lambda: 0.0)
    sweeper.sweep(cp._load_jobs(), cp._generation_root(), now)
    assert asked == [(job['clips'][0]['sha256'], job['clips'][0]['bytes'])]
    assert media.exists() is (not confirmed) and thumbnail.exists() is (not confirmed)
    compacted, archived = cp.compaction.compact_job_store(cp._load_jobs(), datetime.now(timezone.utc) + timedelta(days=60))
    assert any(row['jobId'] == job_id for row in archived)
    assert signature in cp._slideshow_unavailable_signatures(compacted, recipe, job['pageId'])
    cp.atomic_save(cp._jobs_path(), compacted)
    # A fresh pass sees the compacted live store, without a cached negative R2
    # result. Archive-only local media is an existing cleanup completeness gap.
    fresh_sweeper = GeneratedMediaRetention(confirm=confirm, monotonic=lambda: 0.0)
    fresh_sweeper.sweep(cp._load_jobs(), cp._generation_root(), now + timedelta(days=1))
    assert signature in cp._slideshow_unavailable_signatures(cp._load_jobs(), recipe, job['pageId'])
    assert job['clips'][0]['sha256'] in compacted[cp.compaction.ARCHIVE_INDEX_KEY]['usedClipSha256'][job['pageId']]
    assert media.exists() is (not confirmed) and thumbnail.exists() is (not confirmed)
    with pytest.raises(RuntimeError, match='duplicate_slideshow_artifact'):
        cp._claim_unique_slideshow_clip(second_id, job['clips'][0], second_plan)


@pytest.mark.asyncio
async def test_wrong_runtime_dispatch_never_calls_provider(lab, monkeypatch):
    client, _, _, library, payload = lab
    response = client.post('/api/control-plane/v1/jobs', json=job_body(1, payload),
                           headers=headers('ownership-foreign-start'))
    assert response.status_code == 200
    job_id = response.json()['jobId']
    calls = _fake_syzygy_boundary(monkeypatch, library)
    monkeypatch.setattr(cp, '_GENERATION_RUNTIME_ID', 'different-runtime')
    await cp._run_syzygy_slideshow(job_id)
    assert calls['submit'] == []
    assert cp._load_jobs()['jobs'][job_id]['status'] == 'queued'


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('door', ['status', 'create'])
async def test_foreign_recovery_requires_claim_evidence(lab, monkeypatch, legacy, door):
    client, _, _, _, payload = lab
    request = job_body(1, payload)
    response = client.post('/api/control-plane/v1/jobs', json=request,
                           headers=headers('ownership-orphan-check'))
    assert response.status_code == 200
    job_id = response.json()['jobId']
    if legacy:
        store = cp._load_jobs()
        store['jobs'][job_id].pop('slideshowClaimVersion')
        store['jobs'][job_id].pop('slideshowAttemptId')
        cp.atomic_save(cp._jobs_path(), store)
    monkeypatch.setattr(cp, '_GENERATION_RUNTIME_ID', 'foreign-runtime')
    if door == 'status':
        result = client.get(f'/api/control-plane/v1/jobs/{job_id}', headers=headers())
    else:
        result = client.post('/api/control-plane/v1/jobs', json=request,
                             headers=headers('ownership-orphan-check'))
    assert result.status_code == 200
    assert result.json()['status'] == 'failed'
    if door == 'status':
        assert result.json()['error'] == 'generation_runtime_restarted'


@pytest.mark.asyncio
async def test_repeated_cancellation_drains_final_write_before_release(lab, monkeypatch):
    client, _, _, library, payload = lab
    response = client.post('/api/control-plane/v1/jobs', json=job_body(1, payload),
                           headers=headers('ownership-drain-final'))
    job_id = response.json()['jobId']
    _fake_syzygy_boundary(monkeypatch, library)
    reached, release = threading.Event(), threading.Event()
    actual = cp._update_slideshow_job
    def delayed(job_id_arg, attempt, runtime, **fields):
        if fields.get('status') == 'completed':
            reached.set()
            assert release.wait(10)
        return actual(job_id_arg, attempt, runtime, **fields)
    monkeypatch.setattr(cp, '_update_slideshow_job', delayed)
    task = asyncio.create_task(cp._run_syzygy_slideshow(job_id))
    try:
        assert await asyncio.to_thread(reached.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        monkeypatch.setattr(cp, '_GENERATION_RUNTIME_ID', 'foreign-runtime')
        status = client.get(f'/api/control-plane/v1/jobs/{job_id}', headers=headers())
        assert status.status_code == 200 and status.json()['status'] == 'running'
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
    assert cp._load_jobs()['jobs'][job_id]['status'] == 'completed'


def test_malformed_partial_legacy_preserves_all_plans_or_refuses():
    plans = [{'signature': 'a' * 64, 'mediaKeys': ['one'], 'templateSlug': 'x'},
             {'signature': 'b' * 64, 'mediaKeys': ['two'], 'templateSlug': 'x'}]
    job = {'sourceKind': 'syzygy_slideshow', 'status': 'failed', 'slideshowPlan': plans,
           'clips': [{'sha256': 'c' * 64, 'source': {'planSignature': []}}]}
    assert cp.compaction.slideshow_reserved_plans(job) == plans
    assert cp.compaction.slideshow_reserved_plans({**job, 'clips': []}) == []
    with pytest.raises(ValueError, match='slideshow_partial_reservation_invalid'):
        cp.compaction.extract_index_entries({**job, 'status': 'completed', 'slideshowPlan': []})


def test_failed_job_without_exposed_clip_releases_plan(lab, monkeypatch):
    client, _, _, library, payload = lab
    first = client.post('/api/control-plane/v1/jobs', json=job_body(1, payload),
                        headers=headers('no-clip-first'))
    first_id = first.json()['jobId']
    job = cp._load_jobs()['jobs'][first_id]
    signature = job['slideshowPlan'][0]['signature']
    recipe = resolve_slideshow_recipe(payload)
    cp._update_job(first_id, status='failed', error='before artifact')
    assert signature not in cp._slideshow_unavailable_signatures(cp._load_jobs(), recipe, job['pageId'])
    candidate = next(f'{n:016x}' for n in range(1, 10000)
        if cp.JOB_ID_PREFIX + f'{n:016x}' != first_id
        and cp.plan_slideshows(recipe, library, 1, set(), cp.JOB_ID_PREFIX + f'{n:016x}')[0]['signature'] == signature)
    monkeypatch.setattr(cp._secrets, 'token_hex', lambda n: candidate if n == 8 else 'b' * (2 * n))
    second = client.post('/api/control-plane/v1/jobs', json=job_body(1, payload),
                         headers=headers('no-clip-second'))
    assert second.status_code == 200
    assert cp._load_jobs()['jobs'][second.json()['jobId']]['slideshowPlan'][0]['signature'] == signature


@pytest.mark.asyncio
async def test_foreign_status_cannot_release_before_first_clip(lab, monkeypatch):
    client, _, _, library, payload = lab
    response = client.post('/api/control-plane/v1/jobs', json=job_body(1, payload),
                           headers=headers('ownership-before-clip'))
    job_id = response.json()['jobId']
    _fake_syzygy_boundary(monkeypatch, library)
    reached, release = threading.Event(), threading.Event()
    actual = cp.load_recipe_library
    def delayed(recipe):
        reached.set()
        assert release.wait(10)
        return actual(recipe)
    monkeypatch.setattr(cp, 'load_recipe_library', delayed)
    task = asyncio.create_task(cp._run_syzygy_slideshow(job_id))
    try:
        assert await asyncio.to_thread(reached.wait, 10)
        assert cp._load_jobs()['jobs'][job_id]['clips'] == []
        monkeypatch.setattr(cp, '_GENERATION_RUNTIME_ID', 'foreign-runtime')
        response = client.get(f'/api/control-plane/v1/jobs/{job_id}', headers=headers())
        assert response.status_code == 200 and response.json()['status'] == 'running'
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=10)
    assert cp._load_jobs()['jobs'][job_id]['status'] == 'completed'


@pytest.mark.asyncio
async def test_queued_callback_cannot_adopt_replaced_attempt(lab, monkeypatch):
    client, _, _, library, payload = lab
    response = client.post('/api/control-plane/v1/jobs', json=job_body(1, payload),
                           headers=headers('ownership-stale-callback'))
    job_id = response.json()['jobId']
    calls = _fake_syzygy_boundary(monkeypatch, library)
    ORIGINAL_START(job_id)
    task = cp._syzygy_slideshow_tasks[job_id]
    cp._update_job(job_id, slideshowAttemptId='replacement-attempt')
    await asyncio.wait_for(task, timeout=10)
    assert calls['submit'] == []
    assert cp._load_jobs()['jobs'][job_id]['status'] == 'queued'


@pytest.mark.asyncio
async def test_lost_startup_compare_and_set_stops_before_provider(lab, monkeypatch):
    client, _, _, library, payload = lab
    response = client.post('/api/control-plane/v1/jobs', json=job_body(1, payload),
                           headers=headers('ownership-startup-cas'))
    job_id = response.json()['jobId']
    calls = _fake_syzygy_boundary(monkeypatch, library)
    monkeypatch.setattr(cp, '_update_slideshow_job', lambda *args, **kwargs: None)
    await cp._run_syzygy_slideshow(job_id)
    assert calls['submit'] == []
    assert cp._load_jobs()['jobs'][job_id]['status'] == 'queued'


def test_completed_legacy_invalid_digest_cannot_be_compacted():
    job = {'pageId': 'acct:p', 'sourceKind': 'syzygy_slideshow', 'status': 'completed',
           'slideshowPlan': [{'signature': 'a' * 64, 'mediaKeys': ['one']}],
           'clips': [{'sha256': 'invalid', 'source': {'planSignature': 'a' * 64}}]}
    with pytest.raises(ValueError, match='slideshow_partial_digest_invalid'):
        cp.compaction.extract_index_entries(job)


from tests.test_control_plane_image_video_execution import image_lab


@pytest.mark.asyncio
async def test_image_video_foreign_status_respects_live_claim(image_lab, monkeypatch):
    from tests import test_control_plane_slideshow_execution as base
    client, library, payload, _ = image_lab
    response = client.post('/api/control-plane/v1/jobs', json=base.job_body(1, payload),
                           headers=base.headers('image-ownership-running'))
    assert response.status_code == 200
    job_id = response.json()['jobId']
    reached, release = threading.Event(), threading.Event()
    actual = cp.load_recipe_library
    def delayed(recipe):
        reached.set()
        assert release.wait(10)
        return actual(recipe)
    monkeypatch.setattr(cp, 'load_recipe_library', delayed)
    task = asyncio.create_task(cp._run_syzygy_slideshow(job_id))
    try:
        assert await asyncio.to_thread(reached.wait, 10)
        monkeypatch.setattr(cp, '_GENERATION_RUNTIME_ID', 'foreign-runtime')
        status = client.get(f'/api/control-plane/v1/jobs/{job_id}', headers=base.headers())
        assert status.status_code == 200 and status.json()['status'] == 'running'
    finally:
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
