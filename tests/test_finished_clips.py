import hashlib
import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from routers import finished_clips

REAL_PROBE = finished_clips._probe
REAL_BOOKED = finished_clips._booked


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('CONTROL_PLANE_TOKEN', 'test-token')
    monkeypatch.setattr(finished_clips, '_root', lambda: tmp_path)
    monkeypatch.setattr(finished_clips, '_probe', lambda path, audio: {
        'width': 1080, 'height': 1920, 'codec': 'h264', 'durationSeconds': 20, 'hasAudio': audio,
    })
    if hasattr(finished_clips, '_booked'):
        monkeypatch.setattr(finished_clips, '_booked', lambda page, sha: True)
    app = FastAPI()
    app.include_router(finished_clips.router)
    return TestClient(app)


def booking(body, page='acct:burnt-mill-rd', **extra):
    data = {'pageId': page, 'publicationId': 'publication-1', 'masterPagesHash': 'sha256:' + 'a' * 64,
            'bookedDateTime': '2026-11-16T11:00:00-05:00', 'bookedSha256': hashlib.sha256(body).hexdigest(),
            'bytes': len(body), 'hasAudio': False,
            'treatment': {'textMode': 'blank', 'captionMode': 'in_app'}}
    data.update(extra)
    return data


def submit(client, body, data, key='request-1', page='acct:burnt-mill-rd'):
    return client.post('/v1/finished-clips', content=body, headers={
        'Authorization': 'Bearer test-token', 'X-RT-Page-Id': page,
        'Idempotency-Key': key, 'X-RT-Finished-Clip-Booking': json.dumps(data),
    })


def test_silent_burnt_optional_evidence_and_readback(client):
    body = b'mp4 fixture bytes'
    data = booking(body)
    result = submit(client, body, data)
    assert result.status_code == 200
    assert result.json()['provenance'] == 'finished_clip_booked'
    assert result.json()['booking'] == data
    assert submit(client, body, data, key='another-request').json() == result.json()
    read = client.get('/v1/finished-clips/' + data['bookedSha256'], headers={
        'Authorization': 'Bearer test-token', 'X-RT-Page-Id': data['pageId']})
    assert read.json() == result.json()
    with_evidence = booking(b'other bytes', qaReceiptPath='qa/receipt.json', qaReceiptSha256='b' * 64)
    echoed = submit(client, b'other bytes', with_evidence).json()
    assert echoed['booking']['qaReceiptPath'] == 'qa/receipt.json'


def test_no_slot_ordinal_only_and_booking_change_is_idempotent(client):
    body = b'ordinal stock'
    data = booking(body, ordinal=1)
    del data['bookedDateTime']
    first = submit(client, body, data)
    assert first.status_code == 200
    assert first.json()['booking'] == data
    changed = {**data, 'bookedDateTime': '2026-11-16T11:00:00-05:00'}
    retry = submit(client, body, changed)
    assert retry.status_code == 200
    assert retry.json() == first.json()


def test_present_malformed_time_and_audio_refused(client):
    body = b'bad-time'
    for value in ('invalid', '2026-11-16T11:00:00', None):
        assert submit(client, body, booking(body, bookedDateTime=value)).status_code == 400
    assert submit(client, body, booking(body, hasAudio=True)).status_code == 400
    assert submit(client, body, booking(body, page='acct:culture-core-1', hasAudio=True), page='acct:culture-core-1').status_code == 400


def test_custody_scope():
    assert finished_clips._booked('acct:burnt-mill-rd', '3fc6a5414216274fc6bc0d8eb78757516782f453e4120356379fbb0344779a43')
    assert not finished_clips._booked('acct:other', '3fc6a5414216274fc6bc0d8eb78757516782f453e4120356379fbb0344779a43')


def test_unbooked_sha_refused_at_route(client, monkeypatch):
    monkeypatch.setattr(finished_clips, '_booked', REAL_BOOKED)
    body = b'non-custody synthetic bytes'
    response = submit(client, body, booking(body))
    assert response.status_code == 400
    assert response.json()['detail'] == 'clip_not_booked'


def test_real_silent_clip_probe_and_ordinal_intake(client, monkeypatch):
    custody = os.environ.get('FINISHED_CLIP_CUSTODY_DIR')
    if not custody:
        pytest.skip('optional real custody clip not configured')
    clip = Path(custody) / '3fc6a5414216274fc6bc0d8eb78757516782f453e4120356379fbb0344779a43.mp4'
    if not clip.is_file():
        pytest.skip('optional real custody clip absent')
    body = clip.read_bytes()
    monkeypatch.setattr(finished_clips, '_probe', REAL_PROBE)
    data = booking(body, ordinal=1)
    del data['bookedDateTime']
    response = submit(client, body, data)
    assert response.status_code == 200, response.text
    assert response.json()['media']['hasAudio'] is False
    sha = data['bookedSha256']
    headers = {'Authorization': 'Bearer test-token', 'X-RT-Page-Id': data['pageId']}
    assert client.get(f'/v1/finished-clips/{sha}/download', headers=headers).content == body
    assert client.get(f'/v1/finished-clips/{sha}/download', headers={'X-RT-Page-Id': data['pageId']}).status_code == 401


def test_orphan_video_retry_completes_receipt(client, monkeypatch):
    body = b'orphan video bytes'
    data = booking(body)
    save = finished_clips.atomic_save
    calls = 0
    def fail_once(path, receipt):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError('simulated receipt write failure')
        return save(path, receipt)
    monkeypatch.setattr(finished_clips, 'atomic_save', fail_once)
    with pytest.raises(OSError):
        submit(client, body, data)
    assert submit(client, body, data).status_code == 200


@pytest.mark.parametrize('change,body,page,status', [
    ({'pageId': 'acct:culture-core-1'}, b'first', 'acct:burnt-mill-rd', 400),
    ({'bookedSha256': '0' * 64}, b'first', 'acct:burnt-mill-rd', 422),
    ({'bytes': 3}, b'first', 'acct:burnt-mill-rd', 422),
    ({'hasAudio': True}, b'first', 'acct:burnt-mill-rd', 400),
])
def test_refuses_mismatch(client, change, body, page, status):
    assert submit(client, body, booking(body, **change), page=page).status_code == status


def test_replay_different_bytes_and_media_failure(client, monkeypatch):
    body = b'original'
    data = booking(body)
    assert submit(client, body, data).status_code == 200
    assert submit(client, b'changed!', data).status_code == 422
    monkeypatch.setattr(finished_clips, '_probe', lambda path, audio: (_ for _ in ()).throw(HTTPException(422, 'media_video_mismatch')))
    assert submit(client, b'new body', booking(b'new body')).status_code == 422


def test_authentication_required(client):
    body = b'private bytes'
    response = client.post('/v1/finished-clips', content=body, headers={
        'X-RT-Page-Id': 'acct:burnt-mill-rd', 'Idempotency-Key': 'request-1',
        'X-RT-Finished-Clip-Booking': json.dumps(booking(body)),
    })
    assert response.status_code == 401
