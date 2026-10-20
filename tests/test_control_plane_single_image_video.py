"""Actual Notion image/slideshow tuples through the existing managed service API.

Transport/render bytes are mocked; registry, publication, capability and job
validation are real. PIXEL-8 is saved ontology, not a claimed hardware mapping.
"""
import copy
import hashlib
import json
import pytest
from tests.test_control_plane_image_video_execution import image_lab
import tests.test_control_plane_slideshow_execution as base
import services.control_plane_image_video as iv
import services.control_plane_slideshows as ss
from services.dossier_ingredients import build_dossier_ingredient_catalog, catalog_selection_version
from services.master_pages_contract import intent_hash


def test_actual_intent_candidate_selection_publication_and_capability(image_lab):
    client, _, payload, photo = image_lab
    spec = json.loads(payload['recipeSpecCanonical'])
    intent = spec['masterPages']
    catalog = build_dossier_ingredient_catalog(intent['pageId'], intent, spec['masterPagesHash'])
    assert base.FORMAT in catalog['currentFormatCandidates']
    other = 'single-image-video' if base.FORMAT == 'image-video' else 'image-video'
    assert other not in catalog['currentFormatCandidates']
    assert catalog_selection_version(catalog, base.FORMAT, spec['production']) == spec['production']['catalogVersion']
    if base.FORMAT == 'single-image-video':
        assert intent['contentEngine'] == 'sourced_image' and intent['contentNiche'] == 'POV'
        assert catalog['currentFormatCandidates'] == ['single-image-video']
        assert len(photo['media']) == 1 and photo['media'][0]['duration'] == 12
    response = client.get('/api/control-plane/v1/capabilities', headers=base.headers('real-image-capability'))
    assert response.status_code == 200, response.json()
    row = next(x for x in response.json()['capabilities'] if x['recipeId'] == base.RECIPE_ID)
    assert row['engine'] == intent['contentEngine'] and row['maxQuantity'] == 1


@pytest.mark.parametrize('field,value', [
    ('pageId','acct:foreign'), ('handle','foreign.page'),
    ('notionPageId','00000000-0000-0000-0000-000000000000'),
    ('contentNiche','unapproved'), ('contentEngine','ai_video'),
])
def test_exact_identity_or_tuple_change_never_selects_owned_photo_plan(image_lab,field,value):
    _, _, payload, _ = image_lab
    intent = json.loads(payload['recipeSpecCanonical'])['masterPages']
    intent[field] = value
    assert iv.image_video_plan_for_format(base.FORMAT, intent) is None
    from services.content_engine_registry import load_engine_registry
    profiles,_ = load_engine_registry()
    assert ss.slideshow_library_address(profiles[base.FORMAT], intent) is None


def test_wrong_tuple_refuses_typed_publication_and_job_without_provider(image_lab,monkeypatch):
    client, _, payload, _ = image_lab
    import routers.control_plane as cp
    called=[]
    def forbidden(*args, **kwargs):
        called.append(True)
        raise AssertionError('invalid publication must not start provider work')
    monkeypatch.setattr(cp,'_start_syzygy_slideshow',forbidden)
    wrong=copy.deepcopy(payload);spec=json.loads(wrong['recipeSpecCanonical'])
    # Twin is NOT normalized into slideshow; PenPal is not a single-image page.
    engine,niche = ('sourced_slideshow','slideshow') if base.FORMAT == 'single-image-video' else ('sourced_image','POV')
    spec['masterPages'].update(contentEngine=engine,contentNiche=niche)
    spec['masterPagesHash']=intent_hash(spec['masterPages'])
    canonical=json.dumps(spec,sort_keys=True,separators=(',',':'))
    wrong.update(engine=engine,recipeVersion='dossier-wrong-image-tuple',recipeSpecCanonical=canonical,
                 recipeSpecHash='sha256:'+hashlib.sha256(canonical.encode()).hexdigest())
    assert ss.resolve_slideshow_recipe(wrong,require_runtime=False) is None
    response=client.post('/api/control-plane/v1/recipes',json=wrong,headers=base.headers('wrong-image-publication'))
    assert response.status_code in (400,409,422),response.json()
    response=client.post('/api/control-plane/v1/jobs',json=base.job_body(1,wrong),headers=base.headers('wrong-image-job'))
    assert response.status_code in (400,409,422),response.json()
    assert called == []


def test_shared_photo_registry_does_not_allow_cross_format_page(image_lab):
    _,_,payload,_=image_lab
    spec=json.loads(payload['recipeSpecCanonical'])
    other='single-image-video' if base.FORMAT=='image-video' else 'image-video'
    assert iv.image_video_plan_for_format(other,spec['masterPages']) is None


@pytest.mark.asyncio
async def test_direct_submission_also_refuses_other_pages_format_before_http(image_lab):
    _,_,payload,photo=image_lab
    other='single-image-video' if base.FORMAT=='image-video' else 'image-video'
    called=[]
    async def forbidden(*args,**kwargs):
        called.append(True)
        raise AssertionError('foreign format must not submit')
    with pytest.raises(iv.ImageVideoError,match='format differs'):
        await iv.submit_image_video_render(photo,page_id=photo['pageId'],
            recipe_version=payload['recipeVersion'],request_json=forbidden,format_slug=other)
    assert called == []


def test_same_request_rechecks_live_image_library_without_file_or_intent_change(image_lab,monkeypatch):
    client,library,payload,_=image_lab
    import routers.control_plane as cp
    reads=[]
    def available(*args,**kwargs):
        reads.append('available')
        return library
    monkeypatch.setattr(cp,'load_syzygy_library',available)
    headers=base.headers('same-image-capability-request')
    first=client.get('/api/control-plane/v1/capabilities',headers=headers)
    assert first.status_code==200
    row=next(x for x in first.json()['capabilities'] if x['recipeId']==base.RECIPE_ID)
    assert row['engine']==base.ENGINE and row['maxQuantity']==1
    assert reads==['available']
    def unavailable(*args,**kwargs):
        reads.append('unavailable')
        raise ss.SyzygyUnavailable('live library is temporarily unavailable')
    monkeypatch.setattr(cp,'load_syzygy_library',unavailable)
    # Same publication, current intent, request, jobs and on-disk files. Only
    # the live library changed, which a file-keyed cache cannot detect.
    second=client.get('/api/control-plane/v1/capabilities',headers=headers)
    assert second.status_code==200
    assert all(x['recipeId']!=base.RECIPE_ID for x in second.json()['capabilities'])
    assert reads==['available','unavailable']
