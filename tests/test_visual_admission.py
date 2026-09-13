import hashlib
import shutil
import subprocess

import pytest

from services import visual_admission as gate


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


def test_single_transient_frame_rejects_without_model(monkeypatch, tmp_path):
    path = fake_media(monkeypatch, tmp_path)
    monkeypatch.setattr(gate, '_frames', lambda *args: iter([b'0'*48,b'1'*48,b'2'*48]))
    monkeypatch.setattr(gate, '_ocr', lambda frame,*args: 'SALE' if frame[0] == 49 else '')
    monkeypatch.setattr(gate, '_vision', lambda *args: pytest.fail('should not need model to reject'))
    result = run_scan(path)
    assert result['verdict'] == 'text'
    assert result['ocr']['frame'] == 1


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
    monkeypatch.setattr(gate, '_vision', lambda *args: {'verdict':'clean','reason':'fixture mock; independent live model test required'})
    result = run_scan(path)
    assert result['verdict'] == ('text' if has_text else 'clean'), result
    assert result['sampling']['frameCount'] == (2 if has_text else 3)


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
        assert payload['messages'][0]['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
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
        'image_url': {'url': 'data:image/png;base64,ZmFrZQ=='},
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
