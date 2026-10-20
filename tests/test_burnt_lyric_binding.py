"""Producer metadata unit tests with explicit synthetic durable jobs; no render."""
import hashlib
import json
import pytest
from services.control_plane_lyric_video import managed_lyric_binding, validate_managed_lyric_job
from tests.test_control_plane_lyric_video import spec, PAGE


def fixture():
    value=spec();raw=json.dumps(value,separators=(',',':'));sha=hashlib.sha256(raw.encode()).hexdigest()
    publication={'pageId':PAGE,'recipeId':'lyric-video:master','engine':'lyrics_slideshows','recipeVersion':'fixture-reviewed-version',
                 'recipeSpecCanonical':raw,'recipeSpecHash':sha}
    job={k:publication[k] for k in ('pageId','recipeId','engine','recipeVersion','recipeSpecHash')}
    job.update(jobId='test-only-managed-job',sourceKind='syzygy_slideshow',status='completed')
    return value,publication,job


def test_producer_binding_contains_full_immutable_authority_and_actual_source_hash():
    value,publication,job=fixture();binding=managed_lyric_binding(job,publication,value,'a'*64)
    assert binding['schema']=='content-lab.lyric-timing-binding.v1'
    assert binding['pageId']==PAGE and binding['account']=='burnt.mill.rd'
    assert binding['notionPageId']=='3451465b-b829-80a8-b9d1-fb7e34b98ff5'
    assert binding['soundId']=='7611482162773035025' and binding['durationMs']==20000
    assert binding['sourceSha256']=='a'*64 and binding['generationJobId']==job['jobId']
    assert binding['recipeSpecHash']==publication['recipeSpecHash']
    assert len(binding['timingSha256'])==len(binding['sourceProgramSha256'])==64


@pytest.mark.parametrize('mutation',['operator','page','specHash','specBytes','version','program','notion'])
def test_foreign_stale_or_operator_authority_is_refused(mutation):
    value,publication,job=fixture()
    if mutation=='operator':job['sourceKind']='operator-proof'
    if mutation=='page':job['pageId']='acct:culture-core-1'
    if mutation=='specHash':job['recipeSpecHash']='f'*64
    if mutation=='specBytes':publication['recipeSpecCanonical']+=' '
    if mutation=='version':job['recipeVersion']='foreign'
    if mutation=='program':job['recipeId']='ugc-slideshow:master'
    if mutation=='notion':value['masterPages']['notionPageId']='foreign'
    with pytest.raises(ValueError): managed_lyric_binding(job,publication,value,'a'*64)


def test_artifact_read_requires_real_stored_producer_binding_and_current_exact_authority(monkeypatch):
    from routers import control_plane as routes
    from fastapi import HTTPException
    value,publication,job=fixture()
    monkeypatch.setattr(routes,'load_registered_recipe',lambda *a:publication)
    monkeypatch.setattr(routes,'typed_recipe_spec',lambda p:value)
    clip={'sha256':'a'*64,'lyricTiming':managed_lyric_binding(job,publication,value,'a'*64)}
    assert routes._artifact_lyric_timing(job,clip)==clip['lyricTiming']
    for mutation in ['missing','source','timing','job','status']:
        altered=dict(clip);changed_job=dict(job)
        if mutation=='missing':altered.pop('lyricTiming')
        if mutation=='source':altered['sha256']='b'*64
        if mutation=='timing':altered['lyricTiming']={**clip['lyricTiming'],'timingSha256':'f'*64}
        if mutation=='job':changed_job['jobId']='different-managed-job'
        if mutation=='status':changed_job['status']='running'
        with pytest.raises(HTTPException) as error:routes._artifact_lyric_timing(changed_job,altered)
        assert error.value.status_code==409
    assert routes._artifact_lyric_timing({'recipeId':'ugc-slideshow:master'}, {'lyricTiming':'untrusted-ignored'}) is None
