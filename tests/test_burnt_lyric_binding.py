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


def test_point_seventy_five_managed_job_is_not_refused():
    value, publication, job = fixture()
    value['renderTreatment']['filters']['brightness'] = 0.75
    raw = json.dumps(value, separators=(',', ':'))
    publication['recipeSpecCanonical'] = raw
    publication['recipeSpecHash'] = hashlib.sha256(raw.encode()).hexdigest()
    job['recipeSpecHash'] = publication['recipeSpecHash']
    timing = validate_managed_lyric_job(job, publication, value)
    assert timing['id'] == 'burnt-heavy-foot-chorus-v1'


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


@pytest.mark.parametrize('mutation',['missing','reformatted','phrase','caption','scope','extra'])
def test_artifact_read_rejects_any_nonregistered_manifest_bytes(monkeypatch,mutation):
    from routers import control_plane as routes
    from fastapi import HTTPException
    from services.lyric_timing import _TIMING_PATH
    value,publication,job=fixture()
    monkeypatch.setattr(routes,'load_registered_recipe',lambda *a:publication)
    monkeypatch.setattr(routes,'typed_recipe_spec',lambda p:value)
    binding=managed_lyric_binding(job,publication,value,'a'*64)
    assert binding['manifestJson'].encode('utf-8')==_TIMING_PATH.read_bytes()
    assert hashlib.sha256(binding['manifestJson'].encode('utf-8')).hexdigest()==binding['timingSha256']
    original=json.loads(binding['manifestJson'])
    assert len(original['phrases'])==7
    assert original['canonicalCaption']=='\n'.join(p['text'] for p in original['phrases'])
    if mutation=='missing':binding.pop('manifestJson')
    else:
        if mutation=='phrase':original['phrases'][0]['endMs']+=1
        if mutation=='caption':original['canonicalCaption']='foreign'
        if mutation=='scope':original['pageId']='acct:culture-core-1'
        if mutation=='extra':original['extra']='untrusted'
        binding['manifestJson']=json.dumps(original,sort_keys=True)
    with pytest.raises(HTTPException) as error:
        routes._artifact_lyric_timing(job,{'sha256':'a'*64,'lyricTiming':binding})
    assert error.value.status_code==409


@pytest.mark.asyncio
async def test_normal_managed_runner_persists_exact_manifest_after_hashed_completion(monkeypatch,tmp_path):
    # Execute the actual runner with synthetic durable authority/store and mocked remote media.
    from routers import control_plane as routes
    from services import control_plane_slideshows as slides
    from services.lyric_timing import _TIMING_PATH
    from tests.test_control_plane_lyric_video import recipe_and_library
    from tests.test_control_plane_slideshow_execution import _fake_syzygy_boundary
    value,publication,job=fixture();recipe,library=recipe_and_library()
    plan=slides.plan_slideshows(recipe,library,1,set(),'managed-capsule')[0]
    sizes={item.key:item.bytes for item in library.objects}
    plan['media']=[{'objectKey':key,'bytes':sizes[key]} for key in plan['mediaKeys']]
    job.update(status='queued',artifactRoot=str(tmp_path),sourceLibraryId=recipe.library_id,
               formatContractVersion=recipe.format_contract_version,executorVersion=recipe.executor_version,
               materialSource=recipe.material_source,assetType=recipe.asset_type,slideshowPlan=[plan])
    monkeypatch.setattr(routes,'_get_job_or_404',lambda _:job)
    monkeypatch.setattr(routes,'_job_matches_current_master_pages',lambda _:True)
    monkeypatch.setattr(routes,'load_registered_recipe',lambda *a:publication)
    monkeypatch.setattr(routes,'resolve_slideshow_recipe',lambda _:recipe)
    monkeypatch.setattr(routes,'typed_recipe_spec',lambda _:value)
    monkeypatch.setattr(routes,'job_profile_authority_matches',lambda *a:True)
    monkeypatch.setattr(routes,'load_recipe_library',lambda _:library)
    monkeypatch.setattr(routes,'_update_job',lambda _,**changes:job.update(changes))
    monkeypatch.setattr(routes,'_claim_unique_slideshow_clip',lambda *a:None)
    monkeypatch.setattr(routes,'_source_provenance',lambda _,source:source)
    _fake_syzygy_boundary(monkeypatch,library)
    pinned={w['key']:w for w in plan['windows']}
    async def observe(_,keys):
        return [{'objectKey':k,'bytes':pinned[k]['sourceBytes'],'sha256':pinned[k]['sourceSha256']} for k in keys]
    async def thumbnail(*a):return None
    monkeypatch.setattr(routes,'observe_library_media',observe)
    monkeypatch.setattr(routes,'_thumbnail_manifest',thumbnail)
    await routes._run_syzygy_slideshow(job['jobId'])
    assert job['status']=='completed',job.get('error')
    clip=job['clips'][0];binding=clip['lyricTiming']
    assert binding['manifestJson'].encode('utf-8')==_TIMING_PATH.read_bytes()
    assert binding['sourceSha256']==hashlib.sha256((tmp_path/clip['path']).read_bytes()).hexdigest()
    assert binding['generationJobId']==job['jobId']
    assert routes._artifact_lyric_timing(job,clip)==binding
