"""A live truck recovery must retain its master reservation."""
import asyncio
import hashlib
import json
import threading
import pytest
import routers.control_plane as cp
from tests.master_pages_fixtures import master_pages
from tests.test_control_plane_dossier_execution import (lab, PAGE_ID, TOKEN, HEADERS, recipe_publication,
    current_generation_authority, job_body, _async_value)

def _seed_recovery(lab, monkeypatch):
    client, tmp_path, started = lab
    old_root = tmp_path / "generated" / PAGE_ID / "legacy" / "cpl-2222222222222222"
    old_root.mkdir(parents=True)
    master = old_root / "renders" / "paid-master.mp4"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"another-exact-paid-provider-master")
    master_sha = hashlib.sha256(master.read_bytes()).hexdigest()
    intent, revision = master_pages(PAGE_ID, handle="tucker.reeves")
    producer_publication = recipe_publication()
    producer_treatment = json.loads(producer_publication["recipeSpecCanonical"])["renderTreatment"]
    parent_treatment = cp.source_treatment_receipt(
        {"jobId": "cpl-2222222222222222", "recipeSpecHash": producer_publication["recipeSpecHash"]},
        producer_treatment, master_sha,
        clip_speed=producer_treatment["clipSpeed"], clip_crop=producer_treatment["clipCrop"],
    )
    store = cp._load_jobs()
    store["jobs"]["cpl-2222222222222222"] = {
        **current_generation_authority(),
        "recipeSpecHash": producer_publication["recipeSpecHash"],
        "recipeVersion": producer_publication["recipeVersion"],
        "jobId": "cpl-2222222222222222",
        "pageId": PAGE_ID,
        "sourceKind": "generated",
        "status": "completed",
        "artifactRoot": str(old_root),
        "createdAt": "2026-08-30T00:00:00+00:00",
        "clips": [{
            "path": "renders/paid-master.mp4",
            "sha256": master_sha,
            "bytes": master.stat().st_size,
            "sourceTreatment": parent_treatment,
            "source": {
                "recipeId": "truck-scenic:master",
                "recipeVersion": producer_publication["recipeVersion"],
                "path": "renders/paid-master.mp4",
                "sha256": master_sha,
                "bytes": master.stat().st_size,
                "pageId": PAGE_ID,
                "masterPagesHash": revision,
                "contentNiche": intent["contentNiche"],
                "contentEngine": intent["contentEngine"],
                "vaultUrl": intent["vaultUrl"],
            },
        }],
    }
    cp.atomic_save(cp._jobs_path(), store)
    original_producer = cp._load_jobs()["jobs"]["cpl-2222222222222222"]
    monkeypatch.setattr(cp, "_is_exact_16x9_video", lambda _: True)
    response = client.post(
        "/api/control-plane/v1/jobs", json=job_body(quantity=1), headers=HEADERS,
    )
    job_id = response.json()["jobId"]
    assert started == [f"recovery:{job_id}"]
    async def fake_crops(copied, mode):
        assert mode == "both"
        paths = []
        for index in range(5):
            path = copied.with_stem(f"{copied.stem}_crop{index}")
            path.write_bytes(f"portrait-crop-{index}".encode())
            paths.append(path)
        return paths

    async def fake_thumbnail(job_root, video, index):
        target = job_root / "thumbnails" / f"{index:04d}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"jpeg-{index}".encode())
        return cp._generated_manifest(job_root, target)

    async def fail_generate(*_args, **_kwargs):
        raise AssertionError("master recovery must not call a provider")

    monkeypatch.setattr(cp, "_video_geometry", lambda _: _async_value((1920, 1080)))
    monkeypatch.setattr(cp, "_video_geometry_for_delivery", lambda _: _async_value((606, 1080)))
    monkeypatch.setattr(cp, "multi_crop_vertical", fake_crops)
    monkeypatch.setattr(cp, "_thumbnail_manifest", fake_thumbnail)
    monkeypatch.setattr(cp, "generate_one", fail_generate)
    return client, job_id, master_sha, master


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["status", "create"])
async def test_live_truck_recovery_keeps_master_reservation(lab, monkeypatch, door):
    client, job_id, master_sha, master = _seed_recovery(lab, monkeypatch)
    reached, release = threading.Event(), threading.Event()
    actual_update = cp._update_truck_recovery_job
    def pause(job_id_arg, attempt, runtime, **fields):
        if job_id_arg == job_id and fields.get("status") == "completed":
            reached.set()
            assert release.wait(10)
        return actual_update(job_id_arg, attempt, runtime, **fields)
    monkeypatch.setattr(cp, "_update_truck_recovery_job", pause)
    task = asyncio.create_task(cp._run_truck_master_recovery(job_id))
    try:
        assert await asyncio.to_thread(reached.wait, 10)
        live = cp._load_jobs()["jobs"][job_id]
        assert live["status"] == "running" and not live["clips"]
        assert [row["sha256"] for row in live["recoveryMasters"]] == [master_sha]
        monkeypatch.setattr(cp, "_GENERATION_RUNTIME_ID", "foreign-runtime")
        if door == "status":
            observed = client.get(f"/api/control-plane/v1/jobs/{job_id}", headers=HEADERS)
        else:
            observed = client.post("/api/control-plane/v1/jobs", json=job_body(quantity=1), headers=HEADERS)
        assert observed.status_code == 200, observed.text
        observed_status = observed.json()["status"]
        headers2 = {**HEADERS, "Idempotency-Key": "truck-ownership-second"}
        second = client.post("/api/control-plane/v1/jobs", json=job_body(quantity=1), headers=headers2)
        second_id = second.json().get("jobId") if second.status_code == 200 else None
        second_job = cp._load_jobs()["jobs"].get(second_id, {})
        second_masters = [row["sha256"] for row in second_job.get("recoveryMasters", [])]
    finally:
        release.set()
        await asyncio.wait_for(task, 10)
    finished = cp._load_jobs()["jobs"][job_id]
    artifacts = client.get(f"/api/control-plane/v1/jobs/{job_id}/artifacts", headers=HEADERS)
    assert finished["status"] == "completed" and len(finished["clips"]) == 5
    assert artifacts.status_code == 200 and len(artifacts.json()["artifacts"]) == 5
    assert hashlib.sha256(master.read_bytes()).hexdigest() == master_sha
    assert observed_status == "running", f"live owner marked {observed_status}; second={second.status_code}, same_master={master_sha in second_masters}"
    assert master_sha not in second_masters


@pytest.mark.asyncio
async def test_cancelled_final_write_drains_and_keeps_completed_crops(lab, monkeypatch):
    client, job_id, master_sha, _ = _seed_recovery(lab, monkeypatch)
    reached, release = threading.Event(), threading.Event()
    actual = cp._update_truck_recovery_job
    def delayed(job_id_arg, attempt, runtime, **fields):
        if fields.get('status') == 'completed':
            reached.set()
            assert release.wait(10)
        return actual(job_id_arg, attempt, runtime, **fields)
    monkeypatch.setattr(cp, '_update_truck_recovery_job', delayed)
    task = asyncio.create_task(cp._run_truck_master_recovery(job_id))
    try:
        assert await asyncio.to_thread(reached.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        monkeypatch.setattr(cp, '_GENERATION_RUNTIME_ID', 'foreign-runtime')
        status = client.get(f'/api/control-plane/v1/jobs/{job_id}', headers=HEADERS)
        assert status.status_code == 200 and status.json()['status'] == 'running'
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
    row = cp._load_jobs()['jobs'][job_id]
    assert row['status'] == 'completed' and len(row['clips']) == 5
    artifacts = client.get(f'/api/control-plane/v1/jobs/{job_id}/artifacts', headers=HEADERS)
    assert artifacts.status_code == 200 and len(artifacts.json()['artifacts']) == 5
    for item in artifacts.json()['artifacts']:
        assert client.get(item['url']).status_code == 200
    assert {clip['delivery']['crop']['sourceSha256'] for clip in row['clips']} == {master_sha}


@pytest.mark.asyncio
async def test_stale_queued_attempt_and_wrong_runtime_do_not_execute(lab, monkeypatch):
    client, job_id, _, _ = _seed_recovery(lab, monkeypatch)
    job = cp._load_jobs()['jobs'][job_id]
    attempt, runtime = job['truckRecoveryAttemptId'], job['runtimeId']
    cp._update_job(job_id, truckRecoveryAttemptId='new-attempt')
    await cp._run_truck_master_recovery(job_id, attempt, runtime)
    assert cp._load_jobs()['jobs'][job_id]['status'] == 'queued'
    monkeypatch.setattr(cp, '_GENERATION_RUNTIME_ID', 'foreign-runtime')
    await cp._run_truck_master_recovery(job_id)
    assert cp._load_jobs()['jobs'][job_id]['status'] == 'queued'


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('door', ['status', 'create'])
async def test_foreign_recovery_requires_marked_free_owner(lab, monkeypatch, legacy, door):
    client, job_id, _, _ = _seed_recovery(lab, monkeypatch)
    if legacy:
        store = cp._load_jobs()
        store['jobs'][job_id].pop('truckRecoveryClaimVersion')
        store['jobs'][job_id].pop('truckRecoveryAttemptId')
        cp.atomic_save(cp._jobs_path(), store)
    monkeypatch.setattr(cp, '_GENERATION_RUNTIME_ID', 'foreign-runtime')
    if door == 'status':
        response = client.get(f'/api/control-plane/v1/jobs/{job_id}', headers=HEADERS)
    else:
        response = client.post('/api/control-plane/v1/jobs', json=job_body(quantity=1), headers=HEADERS)
    assert response.status_code == 200
    assert response.json()['status'] == 'failed'
    if door == 'status':
        assert response.json()['error'] == 'generation_runtime_restarted'


@pytest.mark.asyncio
async def test_legacy_failed_exposed_truck_clips_keep_master_through_compaction(lab, monkeypatch):
    from datetime import datetime, timedelta, timezone
    client, job_id, master_sha, _ = _seed_recovery(lab, monkeypatch)
    await cp._run_truck_master_recovery(job_id)
    cp._update_job(job_id, status='failed', error='legacy partial')
    store = cp._load_jobs()
    store['jobs'][job_id].pop('truckRecoveryClaimVersion')
    store['jobs'][job_id].pop('truckRecoveryAttemptId')
    store['jobs'][job_id].update(status='running', runtimeId='old-runtime')
    cp.atomic_save(cp._jobs_path(), store)
    observed = client.get(f'/api/control-plane/v1/jobs/{job_id}', headers=HEADERS)
    assert observed.status_code == 200 and observed.json()['status'] == 'failed'
    assert observed.json()['error'] == 'generation_runtime_restarted'
    row = cp._load_jobs()['jobs'][job_id]
    assert len(row['clips']) == 5
    artifacts = client.get(f'/api/control-plane/v1/jobs/{job_id}/artifacts', headers=HEADERS)
    assert artifacts.status_code == 200 and len(artifacts.json()['artifacts']) == 5
    assert all(client.get(item['url']).status_code == 200 for item in artifacts.json()['artifacts'])
    other = client.post('/api/control-plane/v1/jobs', json=job_body(quantity=1),
                        headers={**HEADERS, 'Idempotency-Key': 'legacy-exposed-second'})
    assert other.status_code == 200
    assert master_sha not in [x['sha256'] for x in cp._load_jobs()['jobs'][other.json()['jobId']].get('recoveryMasters', [])]
    compacted, archived = cp.compaction.compact_job_store(cp._load_jobs(), datetime.now(timezone.utc) + timedelta(days=60))
    assert any(x['jobId'] == job_id for x in archived)
    assert master_sha in compacted[cp.compaction.ARCHIVE_INDEX_KEY]['truckRecoveryMasters']
    assert all(clip['sha256'] in compacted[cp.compaction.ARCHIVE_INDEX_KEY]['usedClipSha256'][PAGE_ID] for clip in row['clips'])
    cp.atomic_save(cp._jobs_path(), compacted)
    third = client.post('/api/control-plane/v1/jobs', json=job_body(quantity=1),
                        headers={**HEADERS, 'Idempotency-Key': 'legacy-exposed-third'})
    assert third.status_code == 200
    assert master_sha not in [x['sha256'] for x in cp._load_jobs()['jobs'][third.json()['jobId']].get('recoveryMasters', [])]


def test_malformed_exposed_legacy_truck_evidence_cannot_be_archived():
    job = {'pageId': PAGE_ID, 'sourceKind': 'truck_master_recovery', 'status': 'failed',
           'recoveryMasters': [], 'clips': [{'sha256': 'a' * 64}]}
    with pytest.raises(ValueError, match='truck_partial_master_invalid'):
        cp.compaction.extract_index_entries(job)
    job['recoveryMasters'] = [{'sha256': 'b' * 64}]
    job['clips'] = [{'sha256': 'invalid'}]
    with pytest.raises(ValueError, match='truck_partial_clip_invalid'):
        cp.compaction.extract_index_entries(job)


def test_failed_without_exposed_clips_releases_master(lab, monkeypatch):
    client, job_id, master_sha, _ = _seed_recovery(lab, monkeypatch)
    cp._update_job(job_id, status='failed', error='before delivery')
    other = client.post('/api/control-plane/v1/jobs', json=job_body(quantity=1),
                        headers={**HEADERS, 'Idempotency-Key': 'unused-master-second'})
    assert other.status_code == 200
    assert master_sha in [x['sha256'] for x in cp._load_jobs()['jobs'][other.json()['jobId']]['recoveryMasters']]


@pytest.mark.asyncio
async def test_lost_startup_compare_and_set_stops_before_media_work(lab, monkeypatch):
    _, job_id, _, _ = _seed_recovery(lab, monkeypatch)
    called = []
    async def geometry(_):
        called.append(True)
        raise AssertionError('must not inspect or render media')
    monkeypatch.setattr(cp, '_video_geometry', geometry)
    monkeypatch.setattr(cp, '_update_truck_recovery_job', lambda *args, **kwargs: None)
    await cp._run_truck_master_recovery(job_id)
    assert called == []
    assert cp._load_jobs()['jobs'][job_id]['status'] == 'queued'


@pytest.mark.asyncio
async def test_foreign_status_cannot_release_before_cropping(lab, monkeypatch):
    client, job_id, _, _ = _seed_recovery(lab, monkeypatch)
    reached, release = threading.Event(), threading.Event()
    async def geometry(_):
        reached.set()
        await asyncio.to_thread(release.wait, 10)
        return (1920, 1080)
    monkeypatch.setattr(cp, '_video_geometry', geometry)
    task = asyncio.create_task(cp._run_truck_master_recovery(job_id))
    try:
        assert await asyncio.to_thread(reached.wait, 10)
        monkeypatch.setattr(cp, '_GENERATION_RUNTIME_ID', 'foreign-runtime')
        status = client.get(f'/api/control-plane/v1/jobs/{job_id}', headers=HEADERS)
        assert status.status_code == 200 and status.json()['status'] == 'running'
        assert cp._load_jobs()['jobs'][job_id]['clips'] == []
    finally:
        release.set()
        await asyncio.wait_for(task, 10)
    assert cp._load_jobs()['jobs'][job_id]['status'] == 'completed'
