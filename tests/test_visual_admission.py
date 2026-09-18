import hashlib
import io
import shutil
import subprocess
import time

import pytest

from services import visual_admission as gate


def test_rejected_boat_frame_ocr_noise_is_not_text():
    # Captured word/confidence rows from cpl-f3d545fa8cfac071 output0, frame0.
    # The frame shows a boat on a lake; rails and reflections produced glyphs.
    tsv = "level\tconf\ttext\n" + "\n".join([
        "5\t14.277382\tTg", "5\t0.000000\tfail)",
        "5\t60.585262\tmM", "5\t81.099945\ti",
        "5\t3.059891\tWily", "5\t85.793335\t\\",
        "5\t89.040390\t\\", "5\t50.927628\tme",
    ])
    assert gate._recognized_words(tsv) == ""


def test_readable_words_numbers_and_short_logos_remain_text():
    assert gate._recognized_words(
        "level\tconf\ttext\n5\t95.864319\tEXISTING\n"
        "5\t96.126114\tTEXT\n5\t92\tAI\n5\t90\t24\n"
    ) == "EXISTING TEXT AI 24"


@pytest.mark.parametrize('tsv', ['garbage', 'level\tconf\ttext\n5\tnan\tTEXT',
                                  'level\tconf\ttext\n5\t101\tTEXT'])
def test_malformed_ocr_does_not_become_clean(tsv):
    with pytest.raises((RuntimeError, ValueError)):
        gate._recognized_words(tsv)


def test_upside_down_low_confidence_rechecks_same_pixels(monkeypatch):
    replies = iter([b"level\tconf\ttext\n5\t40.197716\tONILSIXA\n",
                    b"level\tconf\ttext\n5\t95.864319\tEXISTING\n"])
    seen = []
    def run(args, *, input, timeout):
        seen.append((args, input, timeout))
        return next(replies)
    monkeypatch.setattr(gate, '_run', run)
    pixels = bytes(range(12))
    assert gate._ocr(pixels, 2, 2, time.monotonic()+30, 1) == 'EXISTING'
    assert len(seen) == 2
    assert seen[0][1].endswith(pixels)
    assert seen[1][1].endswith(pixels[9:12]+pixels[6:9]+pixels[3:6]+pixels[0:3])
    assert seen[1][2] <= seen[0][2]


@pytest.fixture(autouse=True)
def isolated_scanner_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, '_lock_path', lambda: tmp_path/'scanner.lock')


def run_scan(path):
    return gate.scan_artifact(path, page_id="acct:test", job_id="cpj_0123456789abcdef", index=0,
                              sha256=hashlib.sha256(path.read_bytes()).hexdigest(), byte_count=path.stat().st_size)


def fake_media(monkeypatch, tmp_path):
    path = tmp_path / 'clip.mp4'
    path.write_bytes(b'opaque-video')
    monkeypatch.setattr(gate.shutil, 'which', lambda tool: tool)
    monkeypatch.setattr(gate, '_probe', lambda *args: (4, 4, 3, 6))
    monkeypatch.setattr(gate, '_frames', lambda *args: iter([b'\0'*48]*3))
    monkeypatch.setattr(gate, '_ocr', lambda *args: '')
    monkeypatch.setattr(gate, '_vision', lambda *args: {'verdict':'clean','reason':'No text visible'})
    return path


def test_clean_requires_all_frames_and_both_detectors(monkeypatch, tmp_path):
    result = run_scan(fake_media(monkeypatch, tmp_path))
    assert result['verdict'] == 'clean'
    assert result['sampling']['frameCount'] == result['sampling']['expectedFrameCount'] == 3
    assert result['model']['sampledFrames'] == [0, 1, 2]
    assert result['ocr']['status'] == 'clean'


@pytest.mark.parametrize('fault', ['vision', 'ocr', 'decode', 'coverage'])
def test_unavailable_fails_closed(monkeypatch, tmp_path, fault):
    path = fake_media(monkeypatch, tmp_path)
    def fail(*args):
        raise RuntimeError('provider failed with private credential')
    if fault == 'vision': monkeypatch.setattr(gate, '_vision', fail)
    if fault == 'ocr': monkeypatch.setattr(gate, '_ocr', fail)
    if fault == 'decode': monkeypatch.setattr(gate, '_frames', fail)
    if fault == 'coverage': monkeypatch.setattr(gate, '_frames', lambda *args: iter([b'\0'*48]))
    result = run_scan(path)
    assert result['verdict'] == 'unavailable'
    assert 'private' not in str(result)


def test_single_transient_ocr_frame_requires_visual_corroboration(monkeypatch, tmp_path):
    path = fake_media(monkeypatch, tmp_path)
    monkeypatch.setattr(gate, '_frames', lambda *args: iter([b'0'*48,b'1'*48,b'2'*48]))
    monkeypatch.setattr(gate, '_ocr', lambda frame,*args: 'SALE' if frame[0] == 49 else '')
    monkeypatch.setattr(gate, '_vision', lambda *args: {'verdict':'text','reason':'SALE visible'})
    result = run_scan(path)
    assert result['verdict'] == 'text'
    assert result['ocr']['candidateFrames'] == [1]
    assert 1 in result['model']['sampledFrames']


def test_ocr_candidate_outside_uniform_sample_cannot_be_dropped(monkeypatch, tmp_path):
    path = fake_media(monkeypatch, tmp_path)
    monkeypatch.setattr(gate, '_probe', lambda *args: (4, 4, 30, 6))
    monkeypatch.setattr(gate, '_frames', lambda *args: iter([bytes([n])*48 for n in range(30)]))
    monkeypatch.setattr(gate, '_ocr', lambda frame,*args: 'AW' if frame[0] == 1 else '')
    result = run_scan(path)
    assert result['verdict'] == 'clean'
    assert result['ocr']['candidateFrames'] == [1]
    assert 1 in result['model']['sampledFrames']
    assert len(result['model']['batches']) == 2
    assert all(b['status'] == 'clean' for b in result['model']['batches'])


def test_batch_provenance_retains_each_answering_provider(monkeypatch, tmp_path):
    path = fake_media(monkeypatch, tmp_path)
    monkeypatch.setattr(gate, '_probe', lambda *args: (4, 4, 2, 6))
    monkeypatch.setattr(gate, '_frames', lambda *args: iter([b'0'*48]*2))
    monkeypatch.setattr(gate, '_ocr', lambda *args: 'AW')
    names = iter(['glm-4.6v-flash', 'gpt-4o-mini'])
    monkeypatch.setattr(gate, '_vision', lambda *args: {
        'verdict':'clean', 'reason':'No visible writing', 'model':{'name':next(names)}})
    result = run_scan(path)
    assert result['verdict'] == 'clean'
    assert [b['name'] for b in result['model']['batches']] == ['glm-4.6v-flash', 'gpt-4o-mini']
    assert all(b['status'] == 'clean' for b in result['model']['batches'])


def test_brief_candidate_is_presented_alone_not_hidden_in_scenery_batch(monkeypatch, tmp_path):
    path = fake_media(monkeypatch, tmp_path)
    monkeypatch.setattr(gate, '_probe', lambda *args: (4, 4, 30, 6))
    monkeypatch.setattr(gate, '_frames', lambda *args: iter([bytes([n])*48 for n in range(30)]))
    monkeypatch.setattr(gate, '_ocr', lambda frame,*args: 'EXISTING TEXT' if frame[0] == 7 else '')
    def vision(samples, deadline):
        assert len(samples) == 1
        return {'verdict':'text', 'reason':'Existing caption visible on this frame'}
    monkeypatch.setattr(gate, '_vision', vision)
    result = run_scan(path)
    assert result['verdict'] == 'text'
    assert result['model']['sampledFrames'] == [7]
    assert result['sampling']['frameCount'] == 8


def test_artifact_changed_during_visual_confirmation_cannot_pass(monkeypatch, tmp_path):
    path = fake_media(monkeypatch, tmp_path)
    def vision(*args):
        path.write_bytes(b'different-video')
        return {'verdict':'clean', 'reason':'Visible sample clean'}
    monkeypatch.setattr(gate, '_vision', vision)
    result = run_scan(path)
    assert result['verdict'] == 'unavailable'
    assert result['reason'] == 'incomplete_or_changed_artifact'


def test_visual_text_and_uncertainty_reject(monkeypatch, tmp_path):
    path = fake_media(monkeypatch, tmp_path)
    for verdict in ['text', 'unavailable']:
        monkeypatch.setattr(gate, '_vision', lambda *args: {'verdict':verdict,'reason':'evidence'})
        assert run_scan(path)['verdict'] == verdict


def test_mutated_bytes_and_exhausted_budget_fail_closed(monkeypatch, tmp_path):
    path = fake_media(monkeypatch, tmp_path)
    result = gate.scan_artifact(path, page_id='acct:test', job_id='job', index=0, sha256='0'*64, byte_count=12)
    assert result['reason'] == 'artifact_identity_mismatch'
    gate._GATE.acquire()
    try:
        assert run_scan(path)['reason'] == 'scan_pending'
    finally:
        gate._GATE.release()


@pytest.mark.skipif(not all(shutil.which(t) for t in ['ffmpeg','ffprobe','tesseract']), reason='native tools required')
@pytest.mark.parametrize('has_text,rotation', [(False,0), (True,0), (True,90), (True,180), (True,270)])
def test_real_video_single_frame_text(monkeypatch, tmp_path, has_text, rotation):
    from PIL import Image, ImageDraw, ImageFont
    # A single 1/30-second text frame between blank frames must be detected.
    for n in range(3):
        frame = Image.new('RGB', (640, 360), 'white')
        if has_text and n == 1:
            font_path = '/System/Library/Fonts/Supplemental/Arial.ttf'
            if not __import__('pathlib').Path(font_path).exists():
                font_path = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
            ImageDraw.Draw(frame).text((50, 130), 'EXISTING TEXT', fill='black', font=ImageFont.truetype(font_path, 48))
        if rotation: frame = frame.rotate(rotation, expand=False)
        frame.save(tmp_path / f'{n:02}.png')
    path = tmp_path / 'video.mp4'
    subprocess.run(['ffmpeg','-v','error','-framerate','30','-i',str(tmp_path/'%02d.png'),
        '-c:v','libx264','-pix_fmt','yuv420p',str(path)], check=True)
    monkeypatch.setattr(gate, '_vision', lambda *args: {'verdict':'text' if has_text else 'clean','reason':'fixture model; live original replay is verified separately'})
    result = run_scan(path)
    assert result['verdict'] == ('text' if has_text else 'clean'), result
    assert result['sampling']['frameCount'] == (2 if has_text else 3)
    if has_text:
        assert result['ocr']['candidateFrames'] == [1]
        assert 1 in result['model']['sampledFrames']


def test_authenticated_endpoint_binds_and_persists_exact_artifact(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import control_plane as cp
    path = fake_media(monkeypatch, tmp_path)
    job_id='cpl-0123456789abcdef'
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    job={'pageId':'acct:test', 'artifactRoot':str(tmp_path), 'clips':[{'path':path.name,'sha256':digest,'bytes':path.stat().st_size}]}
    job['clips'].append(dict(job['clips'][0]))
    monkeypatch.setattr(cp, '_jobs_path', lambda: tmp_path/'jobs.json')
    cp.atomic_save(cp._jobs_path(), {'jobs':{job_id:job}})
    monkeypatch.setenv('CONTROL_PLANE_TOKEN','test-secret')
    app=FastAPI(); app.include_router(cp.router,prefix='/api/control-plane')
    client=TestClient(app)
    url=f'/api/control-plane/v1/jobs/{job_id}/visual-admission/0'
    body={'sha256':digest,'bytes':path.stat().st_size}
    headers={'Authorization':'Bearer test-secret','X-RT-Page-Id':'acct:test'}
    assert client.post(url,json=body).status_code == 401
    assert client.post(url,json=body,headers={**headers,'X-RT-Page-Id':'acct:other'}).status_code == 404
    assert client.post(url,json={**body,'sha256':'0'*64},headers=headers).status_code == 409
    assert client.post(url,json={**body,'verdict':'clean'},headers=headers).status_code == 400
    result=client.post(url,json=body,headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()['reason'] == 'scan_pending'
    result=client.post(url,json=body,headers=headers)
    assert result.json()['verdict'] == 'clean'
    assert cp._load_jobs()['jobs'][job_id]['visualAdmission']['0'] == result.json()
    assert cp._load_jobs()['jobs'][job_id]['visualAdmission']['1']['outputIndex'] == 1
    assert cp._load_jobs()['jobs'][job_id]['visualAdmissionSweep']['running'] is False
    # A stale decision bound to another page must be repaired by the sweep.
    store = cp._load_jobs()
    store['jobs'][job_id]['visualAdmission']['0']['pageId'] = 'acct:wrong'
    cp.atomic_save(cp._jobs_path(), store)
    assert client.post(url,json=body,headers=headers).json()['reason'] == 'scan_pending'
    result = client.post(url,json=body,headers=headers)
    assert result.json()['pageId'] == 'acct:test'
    assert result.json()['verdict'] == 'clean'
    monkeypatch.setattr(gate, '_vision', lambda *args: pytest.fail('cached exact bytes must not trigger another model call'))
    assert client.post(url,json=body,headers=headers).json() == result.json()
    path.write_bytes(b'changed')
    assert client.post(url,json=body,headers=headers).json()['verdict'] == 'unavailable'


def test_primary_vision_url_override_is_allowlisted(monkeypatch):
    override = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
    monkeypatch.setenv('CONTENT_LAB_VISION_URL', override)
    assert gate._configured_vision_url() == override


def test_primary_vision_url_rejects_unallowlisted_value(monkeypatch):
    monkeypatch.setenv('CONTENT_LAB_VISION_URL', 'https://attacker.example/chat/completions')
    with pytest.raises(gate.VisionConfigurationError, match='vision_url_not_allowed'):
        gate._configured_vision_url()


def test_vision_read_is_bounded_before_response_materialization(monkeypatch):
    import base64
    import time
    import httpx
    reads=[]
    class OversizedStream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(1000):
                reads.append(1)
                yield b'x'*4096
    def handler(request):
        assert request.url == gate.VISION_URL
        payload=__import__('json').loads(request.content)
        assert payload['model'] == gate.MODEL
        assert payload['messages'][0]['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,')
        return httpx.Response(200, stream=OversizedStream())
    original_client=httpx.Client
    monkeypatch.setenv('CONTENT_LAB_VISION_API_KEY','fixture-key')
    monkeypatch.setattr(gate.httpx,'Client',lambda **kw: original_client(transport=httpx.MockTransport(handler),**kw))
    with pytest.raises(RuntimeError,match='vision_response_oversized'):
        gate._vision_request([base64.b64encode(b'fixture-image').decode()],time.monotonic()+5, url=gate.VISION_URL, model=gate.MODEL, provider='z.ai', key='fixture-key')
    assert len(reads) == 17


def test_vision_result_records_answering_url_host(monkeypatch):
    import httpx
    original_client = gate.httpx.Client
    monkeypatch.setattr(
        gate.httpx,
        'Client',
        lambda **kw: original_client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={
                'choices': [{'message': {'content': '{"verdict":"clean","reason":"ok"}'}}],
            }),
        ), **kw),
    )
    result = gate._vision_request(
        ['ZmFrZQ=='], __import__('time').monotonic() + 5,
        url='https://open.bigmodel.cn/api/paas/v4/chat/completions',
        model=gate.MODEL, provider='z.ai', key='fixture-key',
    )
    assert result['model']['urlHost'] == 'open.bigmodel.cn'


def test_vision_primary_ok_names_actual_model_and_no_fallback(monkeypatch):
    monkeypatch.setenv('CONTENT_LAB_VISION_API_KEY', 'fixture-key')
    def request(*args, **kwargs):
        assert kwargs['model'] == gate.MODEL
        return {'verdict': 'clean', 'reason': 'primary evidence', 'model': {
            'name': gate.MODEL, 'provider': 'z.ai', 'fallback': False, 'fallbackReason': None}}
    monkeypatch.setattr(gate, '_vision_request', request)
    result = gate._vision(['ZmFrZQ=='], __import__('time').monotonic() + 5)
    assert result['model'] == {'name': gate.MODEL, 'provider': 'z.ai', 'fallback': False, 'fallbackReason': None}


def test_vision_primary_429_falls_back_and_names_reason(monkeypatch):
    import httpx
    monkeypatch.setenv('CONTENT_LAB_VISION_API_KEY', 'fixture-key')
    monkeypatch.setenv('CONTENT_LAB_VISION_FALLBACK_API_KEY', 'fallback-key')
    monkeypatch.setattr(gate, 'VISION_FALLBACK_MODEL', 'qwen2.5vl:7b')
    calls = []
    def request(*args, **kwargs):
        calls.append((kwargs['model'], kwargs.get('key', '')))

        if len(calls) == 1:
            raise httpx.HTTPStatusError('rate limited', request=httpx.Request('POST', 'https://primary'), response=httpx.Response(429))
        return {'verdict': 'clean', 'reason': 'fallback evidence', 'model': {
            'name': kwargs['model'], 'provider': kwargs['provider'], 'fallback': False, 'fallbackReason': None}}
    monkeypatch.setattr(gate, '_vision_request', request)
    result = gate._vision(['ZmFrZQ=='], __import__('time').monotonic() + 5)
    assert calls == [(gate.MODEL, 'fixture-key'), ('qwen2.5vl:7b', 'fallback-key')]
    assert result['model']['name'] == 'qwen2.5vl:7b'
    assert result['model']['provider'] == 'ollama'
    assert result['model']['fallback'] is True
    assert result['model']['fallbackReason'] == 'vision_rate_limited'


def test_vision_both_fail_is_unavailable_and_named(monkeypatch):
    monkeypatch.delenv('CONTENT_LAB_VISION_API_KEY', raising=False)
    monkeypatch.setattr(gate, 'VISION_FALLBACK_MODEL', 'qwen2.5vl:7b')
    def request(*args, **kwargs):
        raise RuntimeError('vision_response_invalid')
    monkeypatch.setattr(gate, '_vision_request', request)
    with pytest.raises(gate.VisionUnavailable) as error:
        gate._vision(['ZmFrZQ=='], __import__('time').monotonic() + 5)
    assert str(error.value) == 'vision_unavailable_all_providers'
    assert error.value.model['name'] == 'qwen2.5vl:7b'
    assert error.value.model['fallback'] is True


def test_vision_unknown_fallback_model_is_refused(monkeypatch):
    monkeypatch.delenv('CONTENT_LAB_VISION_API_KEY', raising=False)
    monkeypatch.setattr(gate, 'VISION_FALLBACK_MODEL', 'unknown-model')
    with pytest.raises(gate.VisionUnavailable) as error:
        gate._vision(['ZmFrZQ=='], __import__('time').monotonic() + 5)
    assert str(error.value) == 'vision_model_not_allowed'
    assert error.value.model['name'] == 'unknown-model'


def test_vision_primary_transport_failure_falls_back(monkeypatch):
    import httpx
    monkeypatch.setenv('CONTENT_LAB_VISION_API_KEY', 'fixture-key')
    monkeypatch.setattr(gate, 'VISION_FALLBACK_MODEL', 'qwen2.5vl:7b')
    calls = []
    def request(*args, **kwargs):
        calls.append(kwargs['model'])
        if len(calls) == 1:
            raise httpx.ConnectError('primary refused')
        return {'verdict': 'clean', 'reason': 'fallback evidence', 'model': {
            'name': kwargs['model'], 'provider': kwargs['provider'], 'fallback': False, 'fallbackReason': None}}
    monkeypatch.setattr(gate, '_vision_request', request)
    result = gate._vision(['ZmFrZQ=='], __import__('time').monotonic() + 5)
    assert calls == [gate.MODEL, 'qwen2.5vl:7b']
    assert result['model']['name'] == 'qwen2.5vl:7b'
    assert result['model']['fallback'] is True
    assert result['model']['fallbackReason'] == 'vision_transport_unavailable'


def test_vision_both_transport_fail_is_typed_and_names_fallback(monkeypatch):
    import httpx
    monkeypatch.setenv('CONTENT_LAB_VISION_API_KEY', 'fixture-key')
    monkeypatch.setattr(gate, 'VISION_FALLBACK_MODEL', 'qwen2.5vl:7b')
    monkeypatch.setattr(gate, '_vision_request', lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ConnectError('refused')))
    with pytest.raises(gate.VisionUnavailable) as error:
        gate._vision(['ZmFrZQ=='], __import__('time').monotonic() + 5)
    assert str(error.value) == 'vision_unavailable_all_providers'
    assert error.value.model['name'] == 'qwen2.5vl:7b'
    assert error.value.model['fallback'] is True
    assert error.value.model['fallbackError'] == 'vision_transport_unavailable'


def test_vision_primary_oversized_body_falls_back(monkeypatch):
    monkeypatch.setenv('CONTENT_LAB_VISION_API_KEY', 'fixture-key')
    monkeypatch.setattr(gate, 'VISION_FALLBACK_MODEL', 'qwen2.5vl:7b')
    calls = []
    def request(*args, **kwargs):
        calls.append(kwargs['model'])
        if len(calls) == 1:
            raise RuntimeError('vision_response_oversized')
        return {'verdict': 'clean', 'reason': 'fallback evidence', 'model': {
            'name': kwargs['model'], 'provider': kwargs['provider'], 'fallback': False, 'fallbackReason': None}}
    monkeypatch.setattr(gate, '_vision_request', request)
    result = gate._vision(['ZmFrZQ=='], __import__('time').monotonic() + 5)
    assert result['verdict'] == 'clean'
    assert result['model']['fallback'] is True
    assert result['model']['fallbackReason'] == 'vision_response_oversized'


def test_fallback_request_uses_only_fallback_key(monkeypatch):
    import httpx
    seen = []
    def handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"verdict":"clean","reason":"ok"}'}}]})
    original_client = gate.httpx.Client
    monkeypatch.setattr(gate.httpx, 'Client', lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw))
    gate._vision_request(['ZmFrZQ=='], __import__('time').monotonic() + 5,
                         url='http://fallback.test/v1/chat/completions', model='qwen2.5vl:7b',
                         provider='ollama', key='fallback-secret')
    assert seen[0]['authorization'] == 'Bearer fallback-secret'
    assert 'primary-secret' not in str(seen[0])


def test_fallback_request_has_no_authorization_when_key_unset(monkeypatch):
    import httpx
    seen = []
    def handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"verdict":"clean","reason":"ok"}'}}]})
    original_client = gate.httpx.Client
    monkeypatch.setattr(gate.httpx, 'Client', lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw))
    gate._vision_request(['ZmFrZQ=='], __import__('time').monotonic() + 5,
                         url='http://fallback.test/v1/chat/completions', model='qwen2.5vl:7b',
                         provider='ollama', key='')
    assert 'authorization' not in seen[0]


def test_gpt4o_mini_is_allowed_fallback_and_names_openai(monkeypatch):
    import httpx
    monkeypatch.delenv('CONTENT_LAB_VISION_API_KEY', raising=False)
    monkeypatch.setenv('CONTENT_LAB_VISION_FALLBACK_API_KEY', 'fallback-key')
    monkeypatch.setattr(gate, 'VISION_FALLBACK_MODEL', 'gpt-4o-mini')
    monkeypatch.setattr(gate, '_vision_request', lambda *args, **kwargs: {
        'verdict': 'clean', 'reason': 'fallback evidence', 'model': {
            'name': kwargs['model'], 'provider': kwargs['provider'],
            'fallback': False, 'fallbackReason': None,
        },
    })
    result = gate._vision(['ZmFrZQ=='], __import__('time').monotonic() + 5)
    assert 'gpt-4o-mini' in gate.ALLOWED_VISION_MODELS
    assert result['model']['name'] == 'gpt-4o-mini'
    assert result['model']['provider'] == 'openai'
    assert result['model']['fallback'] is True


def test_replicate_is_third_fallback_when_primary_and_openai_are_unavailable(monkeypatch):
    import httpx
    monkeypatch.setenv('CONTENT_LAB_VISION_API_KEY', 'fixture-key')
    monkeypatch.setenv('CONTENT_LAB_VISION_FALLBACK_API_KEY', 'fallback-key')
    monkeypatch.setenv('REPLICATE_API_TOKEN', 'replicate-key')
    monkeypatch.setattr(gate, '_vision_request', lambda *args, **kwargs: (_ for _ in ()).throw(
        httpx.HTTPStatusError('unavailable', request=httpx.Request('POST', kwargs['url']), response=httpx.Response(429))))
    monkeypatch.setattr(gate, '_replicate_vision_request', lambda *args, **kwargs: {
        'verdict': 'clean', 'reason': 'replicate evidence', 'model': {
            'name': gate.REPLICATE_VISION_MODEL, 'provider': 'replicate',
            'urlHost': 'api.replicate.com', 'fallback': False, 'fallbackReason': None,
        },
    })
    result = gate._vision(['ZmFrZQ=='], __import__('time').monotonic() + 5)
    assert result['verdict'] == 'clean'
    assert result['model']['provider'] == 'replicate'
    assert result['model']['fallback'] is True
    assert result['model']['fallbackReason'] == 'vision_rate_limited'
    assert result['model']['priorFallbackError'] == 'vision_service_unavailable'


def test_replicate_fallback_batches_at_ten_and_uses_only_replicate_token(monkeypatch):
    import httpx
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(201, json={
            'status': 'succeeded',
            'output': ['{"verdict":"clean","reason":"no text"}'],
        })
    original_client = gate.httpx.Client
    monkeypatch.setattr(
        gate.httpx,
        'Client',
        lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw),
    )
    result = gate._replicate_vision_request(
        ['ZmFrZQ=='] * 16, __import__('time').monotonic() + 5,
        token='replicate-secret',
    )
    assert result['verdict'] == 'clean'
    assert result['model']['provider'] == 'replicate'
    assert len(requests) == 2
    import json
    bodies = [json.loads(request.read()) for request in requests]
    assert [len(body['input']['images']) for body in bodies] == [10, 6]
    assert all(request.headers['authorization'] == 'Bearer replicate-secret' for request in requests)
    assert 'fallback-secret' not in str(requests)


def test_all_three_vision_providers_fail_closed(monkeypatch):
    import httpx
    monkeypatch.setenv('CONTENT_LAB_VISION_API_KEY', 'fixture-key')
    monkeypatch.setenv('CONTENT_LAB_VISION_FALLBACK_API_KEY', 'fallback-key')
    monkeypatch.setenv('REPLICATE_API_TOKEN', 'replicate-key')
    monkeypatch.setattr(gate, '_vision_request', lambda *args, **kwargs: (_ for _ in ()).throw(
        httpx.ConnectError('refused')))
    monkeypatch.setattr(gate, '_replicate_vision_request', lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError('vision_service_unavailable')))
    with pytest.raises(gate.VisionUnavailable) as error:
        gate._vision(['ZmFrZQ=='], __import__('time').monotonic() + 5)
    assert str(error.value) == 'vision_unavailable_all_providers'
    assert error.value.model['provider'] == 'replicate'
    assert error.value.model['fallbackError'] == 'vision_service_unavailable'


def test_openai_compatible_fallback_request_uses_data_url_parts(monkeypatch):
    import httpx
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={'choices': [{'message': {
            'content': '{"verdict":"clean","reason":"ok"}',
        }}]})
    original_client = gate.httpx.Client
    monkeypatch.setattr(
        gate.httpx, 'Client',
        lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw),
    )
    gate._vision_request(
        ['ZmFrZQ=='], __import__('time').monotonic() + 5,
        url='https://api.openai.com/v1/chat/completions',
        model='gpt-4o-mini', provider='openai', key='fallback-secret',
    )
    payload = seen[0].read()
    import json
    body = json.loads(payload)
    content = body['messages'][0]['content']
    assert body['model'] == 'gpt-4o-mini'
    assert body['messages'][0]['role'] == 'user'
    assert content[1] == {
        'type': 'image_url',
        'image_url': {'url': 'data:image/jpeg;base64,ZmFrZQ=='},
    }
    assert 'thinking' not in body
    assert seen[0].headers['authorization'] == 'Bearer fallback-secret'


def test_transient_vision_failure_returns_pending_for_next_cycle(monkeypatch, tmp_path):
    path = fake_media(monkeypatch, tmp_path)
    monkeypatch.setattr(
        gate, '_vision',
        lambda *args: (_ for _ in ()).throw(gate.VisionUnavailable(
            'vision_unavailable_all_providers',
            model={'name': 'gpt-4o-mini', 'provider': 'openai', 'fallback': True},
        )),
    )
    result = run_scan(path)
    assert result['verdict'] == 'unavailable'
    assert result['reason'] == 'scan_pending'
    assert result['model']['status'] == 'unavailable'
    assert result['model']['reason'] == 'vision_unavailable_all_providers'


def test_scanner_busy_is_pending_not_terminal(monkeypatch, tmp_path):
    path = fake_media(monkeypatch, tmp_path)
    gate._GATE.acquire()
    try:
        result = run_scan(path)
    finally:
        gate._GATE.release()
    assert result['verdict'] == 'unavailable'
    assert result['reason'] == 'scan_pending'
    assert result['model']['reason'] == 'scanner_busy_retry'


def test_vision_frames_are_native_resolution_jpeg(monkeypatch, tmp_path):
    # PNG frames of detailed 1080x1920 footage are ~2.4 MB each after base64, so
    # the 32 MiB vision budget ran out after ~14 frames and every such clip was
    # refused as vision_input_budget_exceeded. JPEG keeps native pixels.
    import base64
    from PIL import Image
    path = fake_media(monkeypatch, tmp_path)
    seen = []
    def vision(samples, deadline):
        seen.extend(samples)
        return {'verdict': 'clean', 'reason': 'No text visible'}
    monkeypatch.setattr(gate, '_vision', vision)
    assert run_scan(path)['verdict'] == 'clean'
    assert len(seen) == 3
    for sample in seen:
        raw = base64.b64decode(sample)
        assert raw[:3] == b'\xff\xd8\xff'
        assert Image.open(io.BytesIO(raw)).size == (4, 4)


def test_final_unavailable_decision_is_served_and_never_rescanned(monkeypatch, tmp_path):
    # A refusal that rescanning cannot change (here the vision byte budget) must
    # reach the Worker. Masking it as scan_pending rescanned it every poll, and
    # the oldest such job held the single scanner ahead of every newer job.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import control_plane as cp
    path = fake_media(monkeypatch, tmp_path)
    monkeypatch.setattr(gate, 'MAX_VISION_BYTES', 1)
    job_id = 'cpl-0123456789abcdef'
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    job = {'pageId': 'acct:test', 'artifactRoot': str(tmp_path),
           'clips': [{'path': path.name, 'sha256': digest, 'bytes': path.stat().st_size}]}
    monkeypatch.setattr(cp, '_jobs_path', lambda: tmp_path/'jobs.json')
    cp.atomic_save(cp._jobs_path(), {'jobs': {job_id: job}})
    monkeypatch.setenv('CONTROL_PLANE_TOKEN', 'test-secret')
    app = FastAPI(); app.include_router(cp.router, prefix='/api/control-plane')
    client = TestClient(app)
    url = f'/api/control-plane/v1/jobs/{job_id}/visual-admission/0'
    body = {'sha256': digest, 'bytes': path.stat().st_size}
    headers = {'Authorization': 'Bearer test-secret', 'X-RT-Page-Id': 'acct:test'}
    assert client.post(url, json=body, headers=headers).json()['reason'] == 'scan_pending'
    first = client.post(url, json=body, headers=headers).json()
    assert first['verdict'] == 'unavailable'
    assert first['reason'] == 'vision_input_budget_exceeded'
    monkeypatch.setattr(gate, 'scan_artifact', lambda *args, **kwargs: pytest.fail('a final decision must not be rescanned'))
    later = __import__('datetime').datetime.now(__import__('datetime').timezone.utc) + __import__('datetime').timedelta(minutes=5)
    class LaterDatetime(__import__('datetime').datetime):
        @classmethod
        def now(cls, tz=None):
            return later
    monkeypatch.setattr(cp, 'datetime', LaterDatetime)
    assert client.post(url, json=body, headers=headers).json() == first
    assert cp._load_jobs()['jobs'][job_id]['visualAdmission']['0'] == first


@pytest.mark.parametrize('decision,final', [
    ({'verdict': 'clean', 'reason': 'full_frame_ocr_and_vision_clean'}, True),
    ({'verdict': 'text', 'reason': 'pre_existing_text_vision'}, True),
    ({'verdict': 'unavailable', 'reason': 'vision_input_budget_exceeded'}, True),
    ({'verdict': 'unavailable', 'reason': 'artifact_identity_mismatch'}, True),
    ({'verdict': 'unavailable', 'reason': 'scan_pending'}, False),
    ({'verdict': 'unavailable'}, False),
    ({}, False),
    (None, False),
])
def test_final_decision_classification(decision, final):
    assert gate.is_final_decision(decision) is final
