"""Phase A reads and retries both stored layouts without clearing old media."""
import asyncio
import json
import os
from pathlib import Path

import pytest
import routers.control_plane as cp
from tests.test_control_plane_source_imports import lab, _body, HEADERS, PAGE_ID, TOKEN
from tests.test_source_import_late_retry import install_fake_media
from tests.test_source_runtime_reconciliation import history


@pytest.mark.parametrize('initial_private,switch_private', [(False, False), (False, True), (True, False)])
def test_phase_a_rollforward_and_rollback_retry(lab, monkeypatch, initial_private, switch_private):
    client, intent, revision, _ = lab
    monkeypatch.setattr(cp.source_import_layout, 'PRIVATE_ATTEMPT_ROOTS', initial_private)
    body = _body(intent, revision)
    created = client.post('/api/control-plane/v1/source-imports', headers=HEADERS, json=body)
    assert created.status_code == 200
    job_id = created.json()['jobId']
    old = cp._load_jobs()['jobs'][job_id]
    parent = cp._generation_root() / PAGE_ID / 'source-imports' / job_id
    assert Path(old['artifactRoot']) == (parent / old['sourceImportAttemptId'] if initial_private else parent)
    marker = Path(old['artifactRoot']) / 'old-media-kept'
    marker.write_bytes(b'previous immutable bytes')
    cp._update_job(job_id, status='failed', error='source_import_runtime_restarted')
    monkeypatch.setattr(cp.source_import_layout, 'PRIVATE_ATTEMPT_ROOTS', switch_private)
    retried = client.post('/api/control-plane/v1/source-imports', headers=HEADERS, json=body)
    assert retried.status_code == 200
    current = cp._load_jobs()['jobs'][job_id]
    assert current['sourceImportAttemptId'] != old['sourceImportAttemptId']
    private = initial_private or switch_private
    assert Path(current['artifactRoot']) == (parent / current['sourceImportAttemptId'] if private else parent)
    assert marker.read_bytes() == b'previous immutable bytes'
    if switch_private:
        # B converted a flat A job. Roll back to A and retry B's private job.
        monkeypatch.setattr(cp.source_import_layout, 'PRIVATE_ATTEMPT_ROOTS', False)
        cp._update_job(job_id, status='failed', error='source_import_runtime_restarted')
        assert client.post('/api/control-plane/v1/source-imports', headers=HEADERS, json=body).status_code == 200
        current = cp._load_jobs()['jobs'][job_id]
        assert Path(current['artifactRoot']) == parent / current['sourceImportAttemptId']
        assert marker.read_bytes() == b'previous immutable bytes'
    data = install_fake_media(monkeypatch)
    asyncio.run(cp._run_page_source_import(job_id))
    # Reading B's completed private artifacts with A does not depend on producer mode.
    monkeypatch.setattr(cp.source_import_layout, 'PRIVATE_ATTEMPT_ROOTS', False)
    auth = {'Authorization': f'Bearer {TOKEN}', 'X-RT-Page-Id': PAGE_ID}
    status = client.get(f'/api/control-plane/v1/jobs/{job_id}', headers=auth)
    artifacts = client.get(f'/api/control-plane/v1/jobs/{job_id}/artifacts', headers=auth)
    assert status.status_code == artifacts.status_code == 200
    assert status.json()['status'] == 'completed'
    final = cp._load_jobs()['jobs'][job_id]
    assert 'sourceImportAttemptId' not in status.json() and 'artifactRoot' not in status.json()
    assert client.get(f'/api/control-plane/v1/jobs/{job_id}/download/0', params={'token': old['token']}).status_code == 403
    download = client.get(f'/api/control-plane/v1/jobs/{job_id}/download/0', params={'token': final['token']})
    assert download.status_code == 200 and download.content == data
    assert marker.read_bytes() == b'previous immutable bytes'
    # Optional cross-repository contract proof exports synthetic HTTP payloads.
    output = os.environ.get('LAB_LAYOUT_CONTRACT_RECEIPT_DIR')
    if output:
        Path(output).mkdir(parents=True, exist_ok=True)
        (Path(output) / f'{initial_private}-{switch_private}.json').write_text(json.dumps({
            'request': body, 'created': created.json(), 'retried': retried.json(),
            'status': status.json(), 'artifacts': artifacts.json(), 'pageId': PAGE_ID,
            'jobId': job_id, 'downloadBytes': len(data)}))


def test_phase_a_legacy_reconciliation_preserves_flat_layout(history, monkeypatch):
    from tests.test_source_runtime_reconciliation import as_import, apply
    from services import source_runtime_reconciliation as reconcile
    parent, media = as_import(history)
    monkeypatch.setattr(reconcile.source_import_layout, 'PRIVATE_ATTEMPT_ROOTS', False)
    assert apply(history)['status'] == 'reconciled'
    stored = json.loads(history[0].read_text())['jobs']
    job = next(iter(stored.values()))
    assert Path(job['artifactRoot']) == parent
    assert job['error'] == 'source_import_runtime_restarted'
    assert list(parent.iterdir()) == [media]
