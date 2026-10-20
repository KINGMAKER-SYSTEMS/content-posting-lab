"""Real Lab admission/runner path with captured provider and renderer boundaries."""
import hashlib
import json
from pathlib import Path
import pytest
import tests.test_control_plane_slideshow_execution as base
import services.control_plane_slideshows as ss
import services.control_plane_image_video as iv
import routers.control_plane as cp

@pytest.fixture(params=[('acct:pen-pal3','pen.pal3'),('acct:twin47996','twin47996')])
def image_lab(request,monkeypatch,tmp_path):
    page,handle=request.param;photo=iv.image_video_plan(page)
    library=ss.SyzygyLibrary(photo['libraryId'],photo['lane'],photo['subject'],tuple(ss.SyzygyObject(x['key'],x['bytes'],'https://r2.example/'+x['key'])for x in photo['media']),'c'*64)
    for key,value in {'PAGE_ID':page,'HANDLE':handle,'SUBJECT':photo['subject'],'LANE':photo['lane'],'FORMAT':'image-video','ENGINE':'sourced_slideshow','NICHE':'slideshow','LIBRARY_ID':photo['libraryId'],'RECIPE_ID':'image-video:master'}.items():monkeypatch.setattr(base,key,value)
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
    entries=_slideshow_ingredients(profiles["image-video"],json.loads(payload["recipeSpecCanonical"])["masterPages"])
    bindings={x["ingredientId"]:x["binding"] for x in entries}
    count=len(photo["media"])
    assert bindings["image-library"]["minimumLibraryItems"]==count
    assert bindings["slideshow-treatment"]["minimumLibraryItems"]==count
    assert bindings["slideshow-treatment"]["templates"]==[{"slug":"page-photo-hold","mediaCount":count}]
