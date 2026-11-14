"""Versioned durable request acceptance; no provider/generation/render calls."""
import json
import pytest
from pydantic import ValidationError
from services import post_render_jobs as jobs
from services.post_render import sha256
from services.post_render_timed import TimedPostRenderRequest, _resolve_timing
from services.lyric_timing import MAX_TIMING_PHRASES, lyric_timing_catalog, resolve_lyric_timing
from tests.test_post_render_timed import request as fixture_request, NOW


def submission():
    option=lyric_timing_catalog(page_id='acct:burnt-mill-rd',account='burnt.mill.rd')[0]
    timing=resolve_lyric_timing(option['id'], page_id='acct:burnt-mill-rd',account='burnt.mill.rd',sound_id=option['soundId'],expected_sha256=option['sha256'])
    request=fixture_request(caption=timing['canonicalCaption'],lyric_timing_set_id=timing['id'],lyric_timing_sha256=timing['sha256'],source_program_id=timing['sourceProgramId'],source_program_sha256=timing['sourceProgramSha256'])
    slot={'schema_version':4,'slot_id':request.slot_id,'page_id':request.page_id,'handle':request.account,
          'device_hint':{'device_serial':request.device_serial},'asset':{'sha256':request.source_sha256},
          'caption':{'text':request.caption},'sound':{'music_id':request.sound_id},'render_treatment':json.loads(request.render_treatment_json)}
    raw=json.dumps(slot,separators=(',',':'))
    request=TimedPostRenderRequest.model_validate({**request.model_dump(by_alias=True),'slot_payload_sha256':sha256(raw.encode())})
    return {'schema':jobs.JOB_SCHEMA,'request':request.model_dump(by_alias=True),'slot_payload_json':raw}


def test_timed_job_preserves_exact_request_and_existing_durable_idempotency(tmp_path):
    body=jobs.RenderJobSubmission.model_validate(submission())
    assert isinstance(body.request,TimedPostRenderRequest)
    service=jobs.PostRenderJobs(tmp_path/'jobs',clock_ms=lambda:NOW)
    first=service.enqueue(body,'timed-fixture')
    second=service.enqueue(body,'timed-fixture')
    assert first['id']==second['id']
    assert _resolve_timing(body.request)[1][0]['startMs']==120
    assert _resolve_timing(body.request)[1][-1]['endMs']==20000


@pytest.mark.parametrize('field,value',[('sound_id','7694528135171295233'),('lyric_timing_sha256','f'*64),('source_program_sha256','f'*64),('source_program_id','foreign-program'),('lyric_timing_set_id','foreign')])
def test_timed_job_rejects_foreign_authority_before_queue(field,value):
    body=submission();body['request'][field]=value
    with pytest.raises((ValueError,ValidationError)):jobs.RenderJobSubmission.model_validate(body)


def test_timed_slot_must_have_matching_official_sound():
    body=submission();slot=json.loads(body['slot_payload_json']);slot['sound']['music_id']='7694528135171295233'
    body['slot_payload_json']=json.dumps(slot,separators=(',',':'));body['request']['slot_payload_sha256']=sha256(body['slot_payload_json'].encode())
    with pytest.raises(ValidationError,match='official slot sound'): jobs.RenderJobSubmission.model_validate(body)


def test_timed_slot_cannot_replace_canonical_caption():
    body=submission();slot=json.loads(body['slot_payload_json']);slot['caption']['text']='custom cue text'
    body['slot_payload_json']=json.dumps(slot,separators=(',',':'));body['request'].update(caption='custom cue text',caption_sha256=sha256(b'custom cue text'),slot_payload_sha256=sha256(body['slot_payload_json'].encode()))
    with pytest.raises(ValueError,match='canonical caption'):jobs.RenderJobSubmission.model_validate(body)


def test_timed_workspace_fits_unchanged_static_worker_reservation():
    # Source download remains globally bounded; the timed verified copy has a
    # stricter 22MiB cap. Up to MAX_TIMING_PHRASES PNGs+base64 receipts and four
    # cue frames are covered by the existing v1 worker floor, so legacy
    # admission is unchanged.
    timed_peak=(jobs.MAX_SOURCE_BYTES + jobs.MAX_FINAL_BYTES
                + MAX_TIMING_PHRASES*(jobs.MAX_OVERLAY_BYTES+jobs.MAX_CAPTION_RENDER_JSON_BYTES)
                + 4*jobs.MAX_OVERLAY_BYTES + jobs.MAX_FINAL_BYTES+1
                + jobs.MAX_QA_BYTES+1 + jobs.MAX_RENDER_METADATA_BYTES)
    assert timed_peak <= jobs.PEAK_RENDER_WORKSPACE_BYTES

from tests.test_post_render_timed import actual_source

def test_actual_managed_v2_worker_produces_persistent_verified_receipt(actual_source, tmp_path):
    import shutil
    body=submission()
    source_hash=sha256(actual_source.read_bytes())
    body['request']['source_sha256']=source_hash
    slot=json.loads(body['slot_payload_json']);slot['asset']['sha256']=source_hash
    body['slot_payload_json']=json.dumps(slot,separators=(',',':'))
    body['request']['slot_payload_sha256']=sha256(body['slot_payload_json'].encode())
    body['source_provenance']={'schema':'posting-source-treatment/v1','source_sha256':source_hash,
       'source_visual_treatment_sha256':body['request']['source_visual_treatment_sha256'],
       'provenance_id':'test-only-treated-source'}
    service=jobs.PostRenderJobs(tmp_path/'jobs',clock_ms=lambda:NOW,
        fetcher=lambda request,path:shutil.copyfile(actual_source,path))
    accepted=service.enqueue(jobs.RenderJobSubmission.model_validate(body),'managed-v2-fixture')
    assert accepted['state']=='queued'
    assert service.run_one()
    status=service.status(accepted['id'])
    assert status['state']=='succeeded', status
    receipt=json.loads(service.artifact(accepted['id'],'receipt').read_bytes())
    assert receipt['schema']=='posting-prepared-artifact/v2'
    assert receipt['sound_id']=='7611482162773035025'
    assert receipt['source_program_sha256']==body['request']['source_program_sha256']
    assert len(receipt['phrases'])==7 and len(receipt['phrase_frame_evidence'])==4
    assert receipt['phrases'][0]['startMs']==120 and receipt['phrases'][-1]['endMs']==20000
    assert all(e['maxMeanGlyphError']<=35 for e in receipt['phrase_frame_evidence'])
    reopened=jobs.PostRenderJobs(service.root,clock_ms=lambda:NOW)
    assert reopened.status(accepted['id'])['receipt_sha256']==sha256(service.artifact(accepted['id'],'receipt').read_bytes())


@pytest.mark.parametrize("duration", [1000, 19965, 20035, True, 20000.0])
def test_durable_timed_authority_rejects_wrong_duration_even_when_decode_agrees(tmp_path, duration):
    """A real queued v2 row cannot bless self-consistent but wrong temporal facts."""
    service = jobs.PostRenderJobs(tmp_path / "jobs", clock_ms=lambda: NOW)
    body = jobs.RenderJobSubmission.model_validate(submission())
    accepted = service.enqueue(body, "duration-refusal")
    row = service._row(accepted["id"])
    authority = _duration_authority(body.request, duration)
    with pytest.raises(jobs.RenderJobError, match="receipt_binding_mismatch"):
        service._validate_output_authority(row, authority)


@pytest.mark.parametrize("duration", [19966, 20000, 20034])
def test_durable_timed_authority_keeps_existing_34ms_delivery_tolerance(tmp_path, duration):
    service = jobs.PostRenderJobs(tmp_path / "jobs", clock_ms=lambda: NOW)
    body = jobs.RenderJobSubmission.model_validate(submission())
    accepted = service.enqueue(body, "duration-positive")
    authority = _duration_authority(body.request, duration)
    assert service._validate_output_authority(service._row(accepted["id"]), authority) is authority


def _duration_authority(request, duration):
    import math
    _, phrases = _resolve_timing(request)
    receipt = {key: getattr(request, key) for key in (
        "slot_id", "slot_payload_sha256", "page_id", "program_id", "device_serial", "account",
        "source_sha256", "caption_sha256", "treatment_sha256", "renderer_id", "renderer_version",
        "lyric_timing_set_id", "lyric_timing_sha256", "sound_id", "source_program_id", "source_program_sha256")}
    receipt.update(schema="posting-prepared-artifact/v2", final_sha256="b" * 64,
        final_byte_length=1024, qa_frame_sha256="c" * 64,
        final_object_key="posting/final/" + "b" * 64 + ".mp4", mime_type="video/mp4",
        width=1080, height=1920, completed_at_ms=NOW, duration_ms=duration,
        phrases=[{**phrase, "pngSha256": "e" * 64} for phrase in phrases],
        phrase_frame_evidence=[{"phraseIndex": index, "frame": frame,
            "maxMeanGlyphError": 0, "sha256": "d" * 64}
            for index in (0, len(phrases) - 1)
            for frame in sorted({math.ceil(phrases[index]["startMs"] * 30 / 1000),
                                 math.ceil(phrases[index]["endMs"] * 30 / 1000) - 1})])
    return {"schema": jobs.OUTPUT_AUTHORITY_SCHEMA, "receipt": receipt,
        "receipt_sha256": sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()),
        "decode": {"final_sha256": "b" * 64, "qa_frame_sha256": "c" * 64,
                   "decoded_video_frames": 600, "final_probe": {"duration_ms": duration}},
        "final_sha256": "b" * 64, "qa_frame_sha256": "c" * 64, "final_byte_length": 1024}
