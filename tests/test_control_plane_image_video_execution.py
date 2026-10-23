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
async def test_managed_photo_admission_wire_grade_provenance_and_refill(image_lab,monkeypatch):
    client,library,payload,photo=image_lab
    response=client.post('/api/control-plane/v1/jobs',json=base.job_body(1,payload),headers=base.headers('image-run-1'))
    assert response.status_code==200,response.json();job_id=response.json()['jobId'];captured=[];grades=[]
    base._fake_syzygy_boundary(monkeypatch,library)
    async def observe(_lib,keys):return [{'objectKey':x['key'],'bytes':x['bytes'],'sha256':x['sha256'],'etag':'"pinned"'} for x in photo['media'] if x['key'] in keys]
    async def request(method,endpoint,**kwargs):captured.append((method,endpoint,kwargs));return {'job_id':'real-boundary-fixture','quality':'draft'}
    async def grade(src,dst,correction,**kwargs):grades.append(correction);Path(dst).write_bytes(json.dumps(captured[-1][2]['body']['spec'],sort_keys=True).encode())
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
    assert again.status_code==200, again.json()
    second_id = again.json()['jobId']
    second = cp._load_jobs()['jobs'][second_id]['slideshowPlan'][0]['photoPlan']
    assert second['signature'] != photo['signature']
    assert iv.build_image_video_spec(second) != spec
    assert [x['key'] for x in second['media']] == [x['key'] for x in photo['media']]
    await cp._run_syzygy_slideshow(second_id)
    assert cp._load_jobs()['jobs'][second_id]['status'] == 'completed'
    assert len(captured) == 2
    assert captured[0][2]['idempotency_key'] != captured[1][2]['idempotency_key']
    assert captured[1][2]['body']['spec'] == iv.build_image_video_spec(second)
    replay = client.post('/api/control-plane/v1/jobs',json=base.job_body(1,payload),headers=base.headers('image-run-2'))
    assert replay.status_code == 200 and replay.json()['jobId'] == second_id
    cap = client.get('/api/control-plane/v1/capabilities',headers=base.headers('after-completed'))
    assert next(x for x in cap.json()['capabilities'] if x['recipeId']==base.RECIPE_ID)['maxQuantity'] == 1

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


def test_many_fresh_treatments_keep_tiny_catalog_available(image_lab):
    _, library, payload, original = image_lab
    recipe = ss.resolve_slideshow_recipe(payload)
    reserved = {original['signature']}
    for index in range(200):
        plans = ss.plan_slideshows(recipe, library, 1, reserved, f'fresh-{index}')
        assert len(plans) == 1
        photo = plans[0]['photoPlan']
        assert photo['signature'] not in reserved
        assert iv.image_video_plan_signature(photo) == photo['signature']
        assert iv.build_image_video_spec(photo) != iv.build_image_video_spec(original)
        reserved.add(photo['signature'])
    assert len(reserved) == 201


def test_original_equivalent_variant_does_not_duplicate_batch(image_lab, monkeypatch):
    _, library, payload, original = image_lab
    if original['pageId'] != 'acct:pen-pal3':
        return
    recipe = ss.resolve_slideshow_recipe(payload)
    class ForcedHash:
        def hexdigest(self):
            return f'{74450:016x}' + '0' * 48
    # Only the planner's run seed is forced; signatures remain real hashes.
    real_hash = ss.hashlib.sha256
    def forced(value):
        return ForcedHash() if value == b'collision-seed' else real_hash(value)
    monkeypatch.setattr(ss.hashlib, 'sha256', forced)
    plans = ss.plan_slideshows(recipe, library, 2, set(), 'collision-seed')
    assert len(plans) == 2
    assert len({plan['signature'] for plan in plans}) == 2


def test_variant_validation_retains_source_authority():
    from copy import deepcopy
    for page in ['acct:pen-pal3','acct:twin47996']:
        assert iv.image_video_plan(page, True) is None
        assert iv.image_video_plan(page, iv.PHOTO_TREATMENT_COUNT+1) is None
        variant = iv.image_video_plan(page, 15)
        for field, value in [('duration',99),('sha256','0'*64),('focalX',.99)]:
            forged = deepcopy(variant)
            forged['media'][0][field] = value
            forged['signature'] = iv.image_video_plan_signature(forged)
            with pytest.raises(iv.ImageVideoError):
                iv.build_image_video_spec(forged)


@pytest.mark.asyncio
async def test_two_failed_photo_jobs_pause_then_success_or_cooldown_reopens(image_lab, monkeypatch, caplog):
    from datetime import datetime, timedelta, timezone
    client, library, payload, photo = image_lab
    async def bad_source(_lib, keys):
        return [{'objectKey': x['key'], 'bytes': x['bytes'], 'sha256': '0'*64, 'etag': None}
                for x in photo['media'] if x['key'] in keys]
    monkeypatch.setattr(cp, 'observe_library_media', bad_source)
    ids = []
    def create(key):
        return client.post('/api/control-plane/v1/jobs', json=base.job_body(1, payload),
                           headers=base.headers(key))
    admitted = create('already-admitted-before-pause')
    assert admitted.status_code == 200
    admitted_id = admitted.json()['jobId']
    for index in range(2):
        response = create(f'photo-failure-{index}')
        assert response.status_code == 200, response.json()
        ids.append(response.json()['jobId'])
        await cp._run_syzygy_slideshow(ids[-1])
        assert cp._load_jobs()['jobs'][ids[-1]]['status'] == 'failed'
        if index == 0:
            cap = client.get('/api/control-plane/v1/capabilities', headers=base.headers('cap-one'))
            assert next(x for x in cap.json()['capabilities'] if x['recipeId'] == base.RECIPE_ID)['maxQuantity'] == 1
    before = cp._load_jobs()
    refused = create('third-photo-failure')
    assert refused.status_code == 409 and refused.json()['detail'] == 'photo_jobs_cooldown'
    assert cp._load_jobs() == before
    assert 'photo_jobs_paused page=' + photo['pageId'] in caplog.text
    cap = client.get('/api/control-plane/v1/capabilities', headers=base.headers('cap-paused'))
    assert next(x for x in cap.json()['capabilities'] if x['recipeId'] == base.RECIPE_ID)['maxQuantity'] == 0
    replay = create('photo-failure-1')
    assert replay.status_code == 200 and replay.json()['jobId'] == ids[-1]
    # A success from an already admitted job automatically clears the pause.
    cp._update_job(admitted_id, status='completed', completedAt=datetime.now(timezone.utc).isoformat())
    assert create('after-success').status_code == 200
    # Restore the terminal failure and advance the wall clock beyond cooldown.
    cp._update_job(admitted_id, status='failed', completedAt=datetime.now(timezone.utc).isoformat())
    assert create('still-paused').status_code == 409
    future = datetime.now(timezone.utc) + timedelta(seconds=cp.PHOTO_FAILURE_COOLDOWN_SECONDS)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return future.astimezone(tz)
    monkeypatch.setattr(cp, 'datetime', Clock)
    cap = client.get('/api/control-plane/v1/capabilities', headers=base.headers('cap-expired'))
    assert next(x for x in cap.json()['capabilities'] if x['recipeId'] == base.RECIPE_ID)['maxQuantity'] == 1
    assert create('after-cooldown').status_code == 200


def test_photo_pause_is_page_scoped_terminal_and_photo_only(image_lab):
    from datetime import datetime, timedelta, timezone
    from copy import deepcopy
    _, _, payload, photo = image_lab
    recipe = ss.resolve_slideshow_recipe(payload)
    now = datetime.now(timezone.utc)
    def row(status, age, **overrides):
        return dict(pageId=photo['pageId'], sourceKind='syzygy_slideshow',
                    status=status, completedAt=(now-timedelta(seconds=age)).isoformat(),
                    slideshowPlan=[{'photoPlan': photo}], **overrides)
    failed = row('failed', 2)
    store = {'jobs': {'a': failed, 'b': row('failed', 1)}}
    assert cp._photo_jobs_paused(store, recipe, photo['pageId'])
    for field, value in [('pageId', 'acct:foreign'), ('sourceKind', 'other'),
                         ('slideshowPlan', []), ('status', 'cancelled')]:
        altered = deepcopy(store)
        altered['jobs']['a'][field] = value
        assert not cp._photo_jobs_paused(altered, recipe, photo['pageId'])
    # Insertion order is not terminal order; the latest success clears failures.
    store['jobs'] = {'success': row('completed', 0), **store['jobs']}
    assert not cp._photo_jobs_paused(store, recipe, photo['pageId'])
    from types import SimpleNamespace
    assert not cp._photo_jobs_paused(store, SimpleNamespace(format_slug='lyric-video'), photo['pageId'])
