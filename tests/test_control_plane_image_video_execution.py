"""Real Lab admission/runner path with captured provider and renderer boundaries."""
import hashlib
import json
from pathlib import Path
import pytest
import tests.test_control_plane_slideshow_execution as base
import services.control_plane_slideshows as ss
import services.control_plane_image_video as iv
import routers.control_plane as cp

@pytest.fixture(params=[
    ('acct:pen-pal3','pen.pal3','image-video','sourced_slideshow','slideshow','3ed1465b-b829-809f-afb4-da66093e7f9d'),
    ('acct:twin47996','twin47996','single-image-video','sourced_image','POV','3ed1465b-b829-8056-aa9b-f8579c3210b8'),
])
def image_lab(request,monkeypatch,tmp_path):
    page,handle,format_slug,engine,niche,notion=request.param;photo=iv.image_video_plan(page)
    library=ss.SyzygyLibrary(photo['libraryId'],photo['lane'],photo['subject'],tuple(ss.SyzygyObject(x['key'],x['bytes'],'https://r2.example/'+x['key'])for x in photo['media']),'c'*64)
    for key,value in {'PAGE_ID':page,'HANDLE':handle,'SUBJECT':photo['subject'],'LANE':photo['lane'],'FORMAT':format_slug,'ENGINE':engine,'NICHE':niche,'LIBRARY_ID':photo['libraryId'],'RECIPE_ID':format_slug+':master'}.items():monkeypatch.setattr(base,key,value)
    # These are the actual observed Notion tuples, not a slideshow-normalized Twin.
    original_master_pages = base.master_pages
    def actual_master_pages(*args, **kwargs):
        from services.master_pages_contract import intent_hash
        intent, _ = original_master_pages(*args, **kwargs)
        intent.update(notionPageId=notion, group='MON ROVIA', groupLabel='Mon Rovia',
                      posterName='PIXEL-8', status='active', accountType=None, pageType=None,
                      vaultUrl='https://shipstream.risingtidesviral.com/vault/'+handle)
        return intent, intent_hash(intent)
    monkeypatch.setattr(base,'master_pages',actual_master_pages)
    monkeypatch.setattr(base,'_library',lambda *a,**k:library)
    original=base.publication
    def publication(*args,**kwargs):
        payload=original(*args,**kwargs);spec=json.loads(payload['recipeSpecCanonical']);spec['renderTreatment']['filters']={'brightness':.85}
        canonical=json.dumps(spec,sort_keys=True,separators=(',',':'));payload.update(recipeSpecCanonical=canonical,recipeSpecHash='sha256:'+hashlib.sha256(canonical.encode()).hexdigest());return payload
    monkeypatch.setattr(base,'publication',publication)
    client,tmp_path,started,library,payload=base.lab.__wrapped__(monkeypatch,tmp_path)
    return client,library,payload,photo

@pytest.mark.asyncio
async def test_managed_photo_admission_wire_grade_provenance_and_exhaustion(image_lab,monkeypatch):
    client,library,payload,photo=image_lab
    response=client.post('/api/control-plane/v1/jobs',json=base.job_body(1,payload),headers=base.headers('image-run-1'))
    assert response.status_code==200,response.json();job_id=response.json()['jobId'];captured=[];grades=[]
    base._fake_syzygy_boundary(monkeypatch,library)
    async def observe(_lib,keys):return [{'objectKey':x['key'],'bytes':x['bytes'],'sha256':x['sha256'],'etag':'"pinned"'} for x in photo['media'] if x['key'] in keys]
    async def request(method,endpoint,**kwargs):captured.append((method,endpoint,kwargs));return {'job_id':'real-boundary-fixture','quality':'draft'}
    async def grade(src,dst,correction,**kwargs):grades.append(correction);Path(dst).write_bytes(b'fixture-managed-photo-output')
    monkeypatch.setattr(cp,'observe_library_media',observe);monkeypatch.setattr(cp,'submit_syzygy_render',ss.submit_syzygy_render);monkeypatch.setattr(ss,'_request_json',request);monkeypatch.setattr(cp,'run_color_correct',grade)
    await cp._run_syzygy_slideshow(job_id);job=cp._load_jobs()['jobs'][job_id];assert job['status']=='completed',job.get('error')
    assert len(captured)==1 and len(grades)==1
    assert grades[0]["brightness"] == pytest.approx(-15.)
    spec=captured[0][2]['body']['spec'];assert spec==iv.build_image_video_spec(photo);assert spec['transition']==0;assert 'template_slug'not in spec and 'cuts'not in spec
    assert sum(x['duration']for x in spec['media'])==12
    clip=job['clips'][0];assert clip['source']['photoPlan']==photo
    assert clip['source']['contentEngine']==base.ENGINE
    assert clip['sourceTreatment']['visualTreatment']['filters']['brightness']==.85
    assert clip['sourceTreatment']['sourceSha256']==clip['sha256']
    again=client.post('/api/control-plane/v1/jobs',json=base.job_body(1,payload),headers=base.headers('image-run-2'))
    assert again.status_code==409 and again.json()['detail']=='slideshow_inventory_exhausted'

@pytest.mark.asyncio
async def test_wrong_pinned_source_refuses_before_provider(image_lab,monkeypatch):
    client,library,payload,photo=image_lab;response=client.post('/api/control-plane/v1/jobs',json=base.job_body(1,payload),headers=base.headers('image-bad-sha'));assert response.status_code==200,response.json();called=[]
    async def observe(_lib,keys):return [{'objectKey':x['key'],'bytes':x['bytes'],'sha256':'0'*64,'etag':None}for x in photo['media']]
    async def forbidden(*args,**kwargs):called.append(True);raise AssertionError('provider must remain untouched')
    monkeypatch.setattr(cp,'observe_library_media',observe);monkeypatch.setattr(cp,'submit_syzygy_render',forbidden)
    await cp._run_syzygy_slideshow(response.json()['jobId']);job=cp._load_jobs()['jobs'][response.json()['jobId']]
    assert job['status']=='failed' and job['error']=='slideshow_source_sha_mismatch';assert called==[]

def test_recipe_requires_exact_once_grade(image_lab):
    _,_,payload,_=image_lab;assert ss.resolve_slideshow_recipe(payload)is not None
    spec=json.loads(payload['recipeSpecCanonical']);spec['renderTreatment']['filters']['brightness']=1.;canonical=json.dumps(spec,sort_keys=True,separators=(',',':'));payload.update(recipeSpecCanonical=canonical,recipeSpecHash='sha256:'+hashlib.sha256(canonical.encode()).hexdigest());assert ss.resolve_slideshow_recipe(payload)is None


@pytest.mark.asyncio
async def test_tampered_reserved_photo_plan_refuses_before_media_work(image_lab,monkeypatch):
    client,library,payload,photo=image_lab
    response=client.post('/api/control-plane/v1/jobs',json=base.job_body(1,payload),headers=base.headers('image-tampered'));assert response.status_code==200
    job_id=response.json()['jobId'];job=cp._load_jobs()['jobs'][job_id];plans=job['slideshowPlan'];plans[0]['photoPlan']['media'][0]['focalX']=.99
    plans[0]['photoPlan']['signature']=iv.image_video_plan_signature(plans[0]['photoPlan']);plans[0]['signature']=plans[0]['photoPlan']['signature'];cp._update_job(job_id,slideshowPlan=plans)
    called=[]
    async def forbidden(*args,**kwargs):called.append(True);raise AssertionError('media work forbidden')
    monkeypatch.setattr(cp,'observe_library_media',forbidden);monkeypatch.setattr(cp,'submit_syzygy_render',forbidden)
    await cp._run_syzygy_slideshow(job_id);job=cp._load_jobs()['jobs'][job_id]
    assert job['status']=='failed' and job['error']=='slideshow_plan_invalid';assert called==[]


def test_catalog_advertises_actual_page_photo_count(image_lab):
    _,_,payload,photo=image_lab
    from services.content_engine_registry import load_engine_registry
    from services.dossier_ingredients import _slideshow_ingredients
    profiles,_=load_engine_registry()
    entries=_slideshow_ingredients(profiles[base.FORMAT],json.loads(payload["recipeSpecCanonical"])["masterPages"])
    bindings={x["ingredientId"]:x["binding"] for x in entries}
    count=len(photo["media"])
    assert bindings["image-library"]["minimumLibraryItems"]==count
    assert bindings["slideshow-treatment"]["minimumLibraryItems"]==count
    assert bindings["slideshow-treatment"]["templates"]==[{"slug":"page-photo-hold","mediaCount":count}]


@pytest.mark.asyncio
@pytest.mark.parametrize("canary_percent", [0, 100])
async def test_beat_experiment_runs_through_registered_jobs_and_receipts(image_lab, monkeypatch, canary_percent):
    from tests.test_beat_slideshow import config
    client,library,payload,photo=image_lab
    if photo['pageId']!='acct:pen-pal3':
        pytest.skip('single-image page keeps its existing path')
    payload=dict(payload)
    payload['recipeVersion']='image-beat-experiment-v1'
    spec=json.loads(payload['recipeSpecCanonical']);spec['beatSlideshow']=config(canary_percent)
    canonical=json.dumps(spec,sort_keys=True,separators=(',',':'))
    payload.update(recipeSpecCanonical=canonical,recipeSpecHash='sha256:'+hashlib.sha256(canonical.encode()).hexdigest())
    registered=client.post('/api/control-plane/v1/recipes',json=payload,headers=base.headers('beat-register-0001'))
    assert registered.status_code==200,registered.json()
    response=client.post('/api/control-plane/v1/jobs',json=base.job_body(1,payload),headers=base.headers('beat-run-0001'))
    assert response.status_code==200,response.json()
    base._fake_syzygy_boundary(monkeypatch,library);captured=[]
    async def observe(_lib,keys):return [{'objectKey':x['key'],'bytes':x['bytes'],'sha256':x['sha256'],'etag':'"pinned"'} for x in photo['media'] if x['key'] in keys]
    async def request(method,endpoint,**kwargs):
        captured.append(kwargs);return {'job_id':'beat-'+str(len(captured)),'quality':'draft'}
    async def grade(src,dst,correction,**kwargs):Path(dst).write_bytes(('beat-output-'+str(len(captured))).encode())
    monkeypatch.setattr(cp,'observe_library_media',observe);monkeypatch.setattr(cp,'submit_syzygy_render',ss.submit_syzygy_render)
    monkeypatch.setattr(ss,'_request_json',request);monkeypatch.setattr(cp,'run_color_correct',grade)
    await cp._run_syzygy_slideshow(response.json()['jobId'])
    job=cp._load_jobs()['jobs'][response.json()['jobId']]
    assert job['status']=='completed',job.get('error')
    assert len(captured)==len(job['clips'])==1
    for clip,wire in zip(job['clips'],captured):
        receipt=clip['source']['photoPlan']['beatSlideshow']
        assert receipt['config']['soundId']=='song-1' and receipt['audioStart']==0
        assert receipt['cohort']==('canary' if canary_percent else 'holdout')
        assert wire['body']['spec']['hook']['text']==receipt['caption']
    again=client.post('/api/control-plane/v1/jobs',json=base.job_body(1,payload),headers=base.headers('beat-run-0002'))
    assert again.status_code==200,again.json()
    second=cp._load_jobs()['jobs'][again.json()['jobId']]
    assert second['slideshowPlan'][0]['signature']!=job['slideshowPlan'][0]['signature']
