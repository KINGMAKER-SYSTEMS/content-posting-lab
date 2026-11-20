"""pad #342: Joy-only Lab receipt for Joy's alternate AI bodies (no generation on this path)."""
import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import finished_clips

JOY = 'acct:joy-to-the-world709'
PARENT = 'd635d3cbf7efb777bc6ef5e6c5adb99a078dabc70553a1050f118a729295dd51'
SPEC = 'sha256:' + 'e' * 64
CAPTION = {'align': 'center', 'color': '#ffffff', 'font': 'TikTokSans12pt-Regular.ttf',
           'line_balance': 30, 'position': 'middle', 'size_pt': 15}
JOY_RECIPE = {'stylePreset': 'default', 'filters': {'brightness': 0.9, 'contrast': 1, 'warmth': 0},
              'captionStyle': CAPTION, 'clipSpeed': 0.85, 'clipCrop': {'zoom': 1, 'focusX': 0.5, 'focusY': 0.5}}
BODY = b'joy ai body fixture bytes'
BODY_SHA = hashlib.sha256(BODY).hexdigest()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('CONTROL_PLANE_TOKEN', 'test-token')
    monkeypatch.setattr(finished_clips, '_root', lambda: tmp_path)
    monkeypatch.setattr(finished_clips, '_probe', lambda path, audio: {
        'width': 1080, 'height': 1920, 'codec': 'h264', 'durationSeconds': 9.4, 'hasAudio': audio,
    })
    monkeypatch.setattr(finished_clips, 'JOY_AI_REUSE', {BODY_SHA: PARENT}, raising=False)
    app = FastAPI()
    app.include_router(finished_clips.router)
    return TestClient(app)


def booking(body=BODY, page=JOY, **extra):
    data = {'pageId': page, 'publicationId': 'pub-joy', 'masterPagesHash': 'sha256:' + 'a' * 64,
            'recipeSpecHash': SPEC, 'renderTreatment': JOY_RECIPE, 'ordinal': 2,
            'bookedSha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body), 'hasAudio': False,
            'treatment': {'textMode': 'blank'}}
    data.update(extra)
    return data


def submit(client, body, data, page=JOY, key='joy-1'):
    return client.post('/v1/finished-clips', content=body, headers={
        'Authorization': 'Bearer test-token', 'X-RT-Page-Id': page,
        'Idempotency-Key': key, 'X-RT-Finished-Clip-Booking': json.dumps(data)})


def test_joy_body_gets_joy_receipt_bound_to_bytes_parent_and_recipe(client):
    response = submit(client, BODY, booking())
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt['provenance'] == 'joy_ai_input_reuse'
    assert receipt['pageId'] == JOY and receipt['sha256'] == BODY_SHA
    treatment = receipt['sourceTreatment']
    assert treatment['schema'] == 'content-lab.source-treatment.v1'
    assert treatment['sourceSha256'] == BODY_SHA
    assert treatment['recipeSpecHash'] == SPEC
    assert treatment['generationJobId'] == 'cpl-18882c4ced3c15ba'
    assert treatment['derivedFrom'] == {'sourceSha256': PARENT, 'generationJobId': 'cpl-18882c4ced3c15ba'}
    assert treatment['visualTreatment'] == {
        'filters': {'brightness': 0.9, 'contrast': 1.0, 'saturation': 1.0, 'warmth': 0.0,
                    'fade': 0.0, 'grain': 0.0, 'vignette': 0.0},
        'clipSpeed': 0.85, 'clipCrop': {'zoom': 1.0, 'focusX': 0.5, 'focusY': 0.5}}
    read = client.get('/v1/finished-clips/' + BODY_SHA, headers={'Authorization': 'Bearer test-token', 'X-RT-Page-Id': JOY})
    assert read.json() == receipt
    # The Worker reads a receipt of at most 8192 bytes.
    assert len(read.content) <= 8192


@pytest.mark.parametrize('change,status,detail', [
    ({'recipeSpecHash': None}, 400, 'recipe_spec_hash_invalid'),
    ({'recipeSpecHash': 'e' * 64}, 400, 'recipe_spec_hash_invalid'),
    ({'renderTreatment': None}, 400, 'render_treatment_invalid'),
    ({'renderTreatment': {**JOY_RECIPE, 'clipSpeed': 1}}, 400, 'joy_recipe_treatment_mismatch'),
    ({'renderTreatment': {**JOY_RECIPE, 'filters': {'brightness': 1}}}, 400, 'joy_recipe_treatment_mismatch'),
])
def test_joy_refuses_missing_or_different_recipe(client, change, status, detail):
    response = submit(client, BODY, booking(**change))
    assert (response.status_code, response.json()['detail']) == (status, detail)


def test_joy_refuses_unlisted_sha_wrong_bytes_and_wrong_page(client):
    other = b'not a listed Joy body'
    response = submit(client, other, booking(other))
    assert (response.status_code, response.json()['detail']) == (400, 'clip_not_booked')
    response = submit(client, b'tampered joy body bytes!!', booking(bookedSha256=BODY_SHA, bytes=25))
    assert (response.status_code, response.json()['detail']) == (422, 'booked_bytes_mismatch')
    # A Joy body is not booked on Culture or Burnt, and its booking cannot name another page.
    for page in ('acct:culture-core-1', 'acct:burnt-mill-rd'):
        response = submit(client, BODY, booking(page=page, treatment={'textMode': 'blank', 'captionMode': 'in_app'}), page=page)
        assert (response.status_code, response.json()['detail']) == (400, 'clip_not_booked')
    response = submit(client, BODY, booking(page='acct:culture-core-1'), page=JOY)
    assert (response.status_code, response.json()['detail']) == (400, 'booking_page_mismatch')


def test_culture_and_burnt_receipts_stay_plain(client, monkeypatch):
    monkeypatch.setattr(finished_clips, '_booked', lambda page, sha: page != JOY)
    body = b'burnt finished clip'
    data = {'pageId': 'acct:burnt-mill-rd', 'publicationId': 'publication-1', 'masterPagesHash': 'sha256:' + 'a' * 64,
            'bookedSha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body), 'hasAudio': False,
            'treatment': {'textMode': 'blank', 'captionMode': 'in_app'}}
    receipt = submit(client, body, data, page='acct:burnt-mill-rd').json()
    assert receipt['provenance'] == 'finished_clip_booked'
    assert 'sourceTreatment' not in receipt


def test_joy_list_is_the_nine_verified_bodies_on_joy_only():
    joy = getattr(finished_clips, 'JOY_AI_REUSE', {})
    assert len(joy) == 9
    assert all(finished_clips.SHA.fullmatch(sha) and finished_clips.SHA.fullmatch(parent) for sha, parent in joy.items())
    assert '68b2154761ea1bfe0ccd3961701928f95fdbf4c4e0548ab57c941b6d81ae4fc1' not in joy
    assert JOY not in finished_clips.BOOKED
    for sha in joy:
        assert finished_clips._booked(JOY, sha)
        assert not finished_clips._booked('acct:culture-core-1', sha)
        assert not finished_clips._booked('acct:burnt-mill-rd', sha)
