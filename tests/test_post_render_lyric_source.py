import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from services import post_render as render
from services import post_render_jobs as jobs
from tests.test_post_render import NOW, request as ordinary_request
from tests.test_post_render_jobs import submission as ordinary_submission


def lyric_payload(*, source='lyric', delivery=None):
    body=ordinary_submission().model_dump(by_alias=True)
    slot=json.loads(body['slot_payload_json'])
    slot['caption']={'source':source,'text':''}
    treatment=json.loads(body['request']['render_treatment_json'])
    if delivery is not None:
        treatment['captionDelivery']=delivery
    treatment_json=json.dumps(treatment,sort_keys=True,separators=(',',':'))
    slot['render_treatment']=treatment
    body['slot_payload_json']=json.dumps(slot,separators=(',',':'))
    body['request'].update(caption='',caption_sha256=render.sha256(b''),source_text_mode='lyric',
        slot_payload_sha256=render.sha256(body['slot_payload_json'].encode()),
        render_treatment_json=treatment_json,treatment_sha256=render.sha256(treatment_json.encode()))
    return body


def rebind_slot(body, mutate):
    slot=json.loads(body['slot_payload_json']);mutate(slot)
    body['slot_payload_json']=json.dumps(slot,separators=(',',':'))
    body['request']['slot_payload_sha256']=render.sha256(body['slot_payload_json'].encode())
    return body


@pytest.mark.parametrize('delivery',[None,'in_app'])
def test_actual_worker_lyric_shape_is_accepted_without_mutating_slot_or_hashes(delivery):
    body=lyric_payload(delivery=delivery)
    accepted=jobs.RenderJobSubmission.model_validate(body)
    assert accepted.slot_payload_json==body['slot_payload_json']
    assert accepted.request.caption==''
    assert accepted.request.caption_sha256==render.sha256(b'')
    assert accepted.request.source_text_mode=='lyric'
    assert accepted.request.source_sha256==json.loads(body['slot_payload_json'])['asset']['sha256']
    assert render._caption_delivery(accepted.request) is None
    assert accepted.model_dump(by_alias=True)==body
    assert jobs.RenderJobSubmission.model_validate_json(accepted.model_dump_json(by_alias=True))==accepted


@pytest.mark.parametrize('mutate',[
    lambda b:b['request'].pop('source_text_mode'),
    lambda b:b['request'].update(caption='words',caption_sha256=render.sha256(b'words')),
    lambda b:b['request'].update(source_text_mode='other'),
    lambda b:b['request'].update(caption_sha256='0'*64),
    lambda b:rebind_slot(b,lambda s:s['caption'].update(source='sound')),
    lambda b:rebind_slot(b,lambda s:s['caption'].pop('source')),
    lambda b:rebind_slot(b,lambda s:s['caption'].update(text='words')),
    lambda b:rebind_slot(b,lambda s:s['asset'].update(sha256='f'*64)),
    lambda b:b['request'].update(renderer_version='2'),
])
def test_marker_cannot_bypass_immutable_source_caption_hash_or_renderer(mutate):
    body=lyric_payload();mutate(body)
    with pytest.raises(ValidationError):jobs.RenderJobSubmission.model_validate(body)


@pytest.mark.parametrize('caption',['',' ','\n','\x00'])
def test_unmarked_blank_or_invisible_caption_stays_forbidden(caption):
    with pytest.raises(ValidationError):
        ordinary_request(caption=caption,caption_sha256=render.sha256(caption.encode()))


def test_lyric_still_validates_style_delivery_and_treatment():
    for bad in ['style','delivery','visual']:
        body=lyric_payload();req=body['request']
        if bad=='visual':
            req['source_visual_treatment_sha256']='0'*64
        else:
            treatment=json.loads(req['render_treatment_json'])
            if bad=='style':treatment['captionStyle']['font']='not-an-advertised-font.ttf'
            else:treatment['captionDelivery']='burned'
            req['render_treatment_json']=json.dumps(treatment,separators=(',',':'))
            req['treatment_sha256']=render.sha256(req['render_treatment_json'].encode())
        with pytest.raises((ValidationError,render.PostRenderError)):
            render.PostRenderRequest.model_validate(req)


def test_absent_marker_does_not_change_ordinary_serialized_request_or_durable_identity():
    request=ordinary_request()
    assert 'source_text_mode' not in request.model_dump(by_alias=True)
    assert 'source_text_mode' not in json.loads(request.model_dump_json(by_alias=True))
    body=ordinary_submission().model_dump(by_alias=True)
    assert 'source_text_mode' not in body['request']
    assert jobs.RenderJobSubmission.model_validate(body).model_dump(by_alias=True)==body


@pytest.fixture(scope='module')
def lyric_source(tmp_path_factory):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.fail('ffmpeg and ffprobe required for lyric no-overlay evidence')
    path=tmp_path_factory.mktemp('lyric-compat')/'source.mp4'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',
        'color=c=navy:size=1080x1920:rate=24:duration=0.8','-vf',
        'drawbox=x=160:y=500:w=760:h=160:color=white:t=fill','-an','-c:v','libx264',
        '-preset','ultrafast','-crf','20','-threads','2','-pix_fmt','yuv420p',str(path)],check=True,timeout=30)
    return path


@pytest.mark.parametrize('delivery',[None,'in_app'])
def test_real_lyric_render_has_no_overlay_or_fallback_and_keeps_decode_qa_receipt_contract(lyric_source,tmp_path,monkeypatch,delivery):
    body=lyric_payload(delivery=delivery)
    source_sha=render.sha256(lyric_source.read_bytes())
    body['request']['source_sha256']=source_sha
    body['source_provenance']['source_sha256']=source_sha
    rebind_slot(body,lambda s:s['asset'].update(sha256=source_sha))
    accepted=jobs.RenderJobSubmission.model_validate(body)
    def forbidden(*a,**kw):pytest.fail('lyric clip must not enter caption overlay renderer')
    monkeypatch.setattr(render,'render_caption_with_background',forbidden)
    output=tmp_path/'prepared'
    result=render.render_post(lyric_source,output,accepted.request,clock_ms=lambda:NOW)
    receipt=json.loads(result.receipt_json)
    assert result.decoded_video_frames>0
    assert (receipt['width'],receipt['height'])==(1080,1920)
    assert receipt['caption_sha256']==render.sha256(b'')
    assert receipt['source_sha256']==source_sha
    assert receipt['final_sha256']==render.sha256(result.final_path.read_bytes())
    assert receipt['qa_frame_sha256']==render.sha256(result.qa_frame_path.read_bytes())
    assert 'caption_delivery' not in receipt and 'burned_fallback' not in receipt
    assert 'source_text_mode' not in receipt
    assert not (output/'overlay.png').exists()
    assert not (output/'caption-render.json').exists()
    assert not (output/'burned').exists()
    assert not (output/'source.mp4').exists()
    assert json.loads((output/'request.json').read_text())['source_text_mode']=='lyric'
    # Keep a genuine locally rendered receipt for the downstream Worker validator.
    target=Path(__file__).parents[2]/'genuine-lyric-receipt.json'
    target.write_bytes(result.receipt_json)


def test_actual_durable_runner_accepts_lyric_and_recovers_same_artifacts(lyric_source,tmp_path,monkeypatch):
    body=lyric_payload(delivery="in_app")
    source_sha=render.sha256(lyric_source.read_bytes())
    body["request"]["source_sha256"]=source_sha
    body["source_provenance"]["source_sha256"]=source_sha
    rebind_slot(body,lambda s:s["asset"].update(sha256=source_sha))
    accepted=jobs.RenderJobSubmission.model_validate(body)
    monkeypatch.setattr(render,"render_caption_with_background",lambda *a,**kw:pytest.fail("lyric overlay forbidden"))
    service=jobs.PostRenderJobs(tmp_path/"jobs",clock_ms=lambda:NOW,
        fetcher=lambda req,target:shutil.copyfile(lyric_source,target))
    job=service.enqueue(accepted,"lyric-source-fixture")
    assert job["state"]=="queued"
    assert service.run_one()
    status=service.status(job["id"])
    assert status["state"]=="succeeded"
    assert status["attempts"]==1
    receipt_path=service.artifact(job["id"],"receipt")
    receipt=json.loads(receipt_path.read_bytes())
    assert receipt["caption_sha256"]==render.sha256(b"")
    assert "burned_fallback" not in receipt
    recovered=jobs.PostRenderJobs(tmp_path/"jobs",clock_ms=lambda:NOW,
        fetcher=lambda *a:pytest.fail("must not fetch completed lyric again"))
    assert recovered.enqueue(accepted,"lyric-source-fixture")["state"]=="succeeded"
    assert not recovered.run_one()
    assert recovered.artifact(job["id"],"receipt").read_bytes()==receipt_path.read_bytes()


def test_existing_timed_lyric_renderer_cannot_take_source_only_marker():
    from tests.test_post_render_timed_jobs import submission
    body=submission()
    body["request"].update(source_text_mode="lyric",caption="",caption_sha256=render.sha256(b""))
    with pytest.raises(ValidationError,match="empty v1 caption"):
        jobs.RenderJobSubmission.model_validate(body)
