"""Scoped managed lyric mode: real authorities, mocked remote render transport."""
import copy
import hashlib
import json
from pathlib import Path
import pytest
from services import control_plane_slideshows as slides
from services.control_plane_lyric_video import validate_lyric_recipe, lyric_ingredient
from services.content_engine_registry import load_engine_registry
from services.lyric_timing import lyric_timing_catalog

PAGE = 'acct:burnt-mill-rd'
ACCOUNT = 'burnt.mill.rd'

def spec():
    return {'masterPages': {'pageId': PAGE, 'handle': ACCOUNT, 'notionPageId':'3451465b-b829-80a8-b9d1-fb7e34b98ff5', 'automationMode':'Automation', 'archived':False},
            'production': {'controls': {'lyricTimingSetId': 'burnt-heavy-foot-chorus-v1'}},
            'renderTreatment': {'filters': {'brightness': .85}, 'clipSpeed': 1}}


def recipe_and_library():
    raw = slides.LYRIC_VIDEO_EXECUTOR_PATH.read_bytes()
    config = json.loads(raw)['pageTreatments'][PAGE]['curatedVideoPlan']
    objects = tuple(slides.SyzygyObject(f'slideshow-webweaving/burnt-mill-rd/clips/{s["file"]}', s['bytes'], 'https://example.invalid/unused') for s in config['sources'])
    library = slides.SyzygyLibrary('syzygy:slideshow-webweaving:burnt-mill-rd', 'slideshow-webweaving', 'burnt-mill-rd', objects, 'e'*64)
    recipe = slides.SlideshowRecipe('lyric-video:master', 'lyric-video', 'lyrics_slideshows', 'a'*64, 'sha256:'+'b'*64,
        'lyric_image_library', 'video/mp4', 'sha256:'+hashlib.sha256(raw).hexdigest(), library.library_id,
        library.lane, library.subject, 20, (slides.SyzygyTemplate('curated-video-montage',20),), 1, 1080,1920,'tiktok_delivery_v1',spec())
    return recipe, library


def test_scoped_catalog_binds_timing_and_source_program_without_changing_legacy():
    profiles,_ = load_engine_registry()
    assert profiles['lyric-edits'].executor_version == profiles['meme-slideshow'].executor_version == 'sha256:485feda76c885d7aab19be256f61c749edcce19417ebc560d265ba92b60974ad'
    timing = validate_lyric_recipe(spec())
    assert timing['sourceProgramSha256'] == hashlib.sha256(slides.LYRIC_VIDEO_EXECUTOR_PATH.read_bytes()).hexdigest()
    ingredient = lyric_ingredient(page_id=PAGE, master_pages=spec()['masterPages'])
    assert ingredient['binding']['id'] == 'burnt-heavy-foot-chorus-v1'
    assert ingredient['binding']['soundId'] == '7611482162773035025'
    assert lyric_ingredient(page_id='acct:culture-core-1', master_pages={'handle':'culture.core.1'})['binding'] is None


@pytest.mark.parametrize('mutation', ['page','notion','operator','archived','account','selector','cue','hash','speed','grade','crop'])
def test_unadvertised_or_foreign_recipe_refused(mutation):
    value=spec()
    if mutation=='page': value['masterPages']['pageId']='acct:culture-core-1'
    if mutation=='notion': value['masterPages']['notionPageId']='different-notion'
    if mutation=='operator': value['masterPages']['automationMode']='Operator'
    if mutation=='archived': value['masterPages']['archived']=True
    if mutation=='account': value['masterPages']['handle']='another.page'
    if mutation=='selector': value['production']['controls']['lyricTimingSetId']='foreign'
    if mutation=='cue': value['production']['controls']['phrases']='arbitrary lyric'
    if mutation=='hash': value['production']['controls']['lyricTimingSha256']='f'*64
    if mutation=='speed': value['renderTreatment']['clipSpeed']=1.5
    if mutation=='grade': value['renderTreatment']['filters']['contrast']=1.1
    if mutation=='crop': value['renderTreatment']['clipCrop']={'zoom':2,'focusX':.5,'focusY':.5}
    with pytest.raises(ValueError): validate_lyric_recipe(value)


def test_owner_brightness_is_accepted_only_inside_the_dossier_range():
    for brightness in (0.0, 0.75, 0.85, 1.0, 3.0):
        value = spec()
        value['renderTreatment']['filters']['brightness'] = brightness
        assert validate_lyric_recipe(value)['id'] == 'burnt-heavy-foot-chorus-v1'


@pytest.mark.parametrize('brightness', [-0.1, 3.1, '0.75', None, True])
def test_out_of_range_or_non_numeric_brightness_is_refused(brightness):
    value = spec()
    value['renderTreatment']['filters']['brightness'] = brightness
    with pytest.raises(ValueError):
        validate_lyric_recipe(value)


@pytest.mark.parametrize('mutation', ['contrast', 'saturation', 'speed', 'crop'])
def test_any_other_treatment_key_drift_is_refused(mutation):
    value = spec()
    if mutation == 'contrast':
        value['renderTreatment']['filters']['contrast'] = 1.1
    if mutation == 'saturation':
        value['renderTreatment']['filters']['saturation'] = 1.1
    if mutation == 'speed':
        value['renderTreatment']['clipSpeed'] = 1.1
    if mutation == 'crop':
        value['renderTreatment']['clipCrop'] = {'zoom': 1.1, 'focusX': 0.5, 'focusY': 0.5}
    with pytest.raises(ValueError):
        validate_lyric_recipe(value)


def test_exact_27_window_plan_is_single_use_not_photo_permutation():
    recipe, library=recipe_and_library()
    assert slides.requires_curated_video_plan(recipe)
    plans=slides.plan_slideshows(recipe,library,10,set(),'fixture')
    assert len(plans)==1
    plan=plans[0]
    assert len(plan['windows'])==27 and len(plan['mediaKeys'])==20
    assert plan['audioSectionEnd']==20
    assert plan['windows'][0]['outputStart']==0 and plan['windows'][-1]['outputEnd']==20
    assert slides.plan_slideshows(recipe,library,1,{plan['signature']},'fixture')==[]
    missing=slides.SyzygyLibrary(library.library_id,library.lane,library.subject,library.objects[:-1],library.snapshot_hash)
    assert slides.plan_slideshows(recipe,missing,1,set(),'fixture')==[]


@pytest.mark.asyncio
async def test_direct_wire_carries_real_ordered_video_windows_no_words_or_music(monkeypatch):
    recipe,library=recipe_and_library(); plan=slides.plan_slideshows(recipe,library,1,set(),'x')[0]
    calls=[]
    async def transport(method,path,**kw): calls.append((method,path,kw)); return {'job_id':'mock-capture-only','quality':'draft'}
    monkeypatch.setattr(slides,'_request_json',transport)
    result=await slides.submit_syzygy_render(plan,page_id=PAGE,recipe_version='test-version',format_slug='lyric-video')
    assert result[0]=='mock-capture-only' and len(calls)==1
    wire=calls[0][2]['body']['spec']
    assert len(wire['media'])==27 and all(m['type']=='video' and m['muted'] for m in wire['media'])
    assert wire['lyrics']==[] and wire['hook']['text']=='' and wire['textCards']==[]
    assert 'audio' not in wire and 'cuts' not in wire and 'template_slug' not in wire
    assert [m['clip_start'] for m in wire['media']]==[w['sourceStart'] for w in plan['windows']]
    assert [m['focal_x'] for m in wire['media']]==[w['focalX'] for w in plan['windows']]


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['missing','reordered','sourcehash','foreign','generic'])
async def test_wire_refuses_before_remote_post(monkeypatch,mutation):
    recipe,library=recipe_and_library(); plan=slides.plan_slideshows(recipe,library,1,set(),'x')[0]; page=PAGE
    if mutation=='missing': plan.pop('windows')
    if mutation=='reordered': plan['windows'].reverse()
    if mutation=='sourcehash': plan['windows'][0]['sourceSha256']='f'*64
    if mutation=='foreign': page='acct:culture-core-1'
    if mutation=='generic': plan={'templateSlug':'clean-slideshow','mediaKeys':plan['mediaKeys'][:3]}
    calls=[]
    async def transport(*a,**kw): calls.append(1); raise AssertionError('must not post')
    monkeypatch.setattr(slides,'_request_json',transport)
    with pytest.raises(ValueError): await slides.submit_syzygy_render(plan,page_id=page,recipe_version='fixture',format_slug='lyric-video')
    assert not calls


def test_real_editor_catalog_advertises_only_closed_scalar_timing_selection(monkeypatch):
    from services import dossier_ingredients as ingredients
    from tests.master_pages_fixtures import master_pages
    recipe, library = recipe_and_library()
    monkeypatch.setattr(ingredients,'load_syzygy_library',lambda *a,**kw: library)
    intent, revision = master_pages(PAGE, handle=ACCOUNT, content_niche='lyric-edits', content_engine='lyrics_slideshows')
    intent['notionPageId']='3451465b-b829-80a8-b9d1-fb7e34b98ff5'
    from services.master_pages_contract import intent_hash
    revision=intent_hash(intent)
    catalog=ingredients.build_dossier_ingredient_catalog(PAGE,intent,revision)
    entry=next(e for e in catalog['formats'] if e['formatId']=='lyric-video')
    selected=entry['productionSelections']
    assert len(selected)==1
    assert selected[0]['controls']=={'lyricTimingSetId':'burnt-heavy-foot-chorus-v1'}
    assert ingredients.catalog_selection_version(catalog,'lyric-video',selected[0])==selected[0]['catalogVersion']
    stale={**selected[0],'controls':{'lyricTimingSetId':'unregistered'}}
    assert ingredients.catalog_selection_version(catalog,'lyric-video',stale) is None
    assert ingredients.selected_dossier_catalog_version(PAGE,intent,revision,'lyric-video',stale) is None
    foreign_intent,foreign_hash=master_pages('acct:foreign',handle='foreign',content_niche='lyric-edits',content_engine='lyrics_slideshows')
    foreign=ingredients.build_dossier_ingredient_catalog('acct:foreign',foreign_intent,foreign_hash)
    assert next(e for e in foreign['formats'] if e['formatId']=='lyric-video')['productionSelections']==[]


def test_legacy_maps_retain_baseline_hashes_and_static_renderer_matches_reviewed_pin():
    root=Path(__file__).resolve().parents[1]
    for path,key,expected in [
        ("content-engine-registry.v1.json","profiles","70baee7058a9805fb9865de57e37262ddc055d0a89f6ff50853c65c7415ec7a2"),
        ("content-format-contracts.v1.json","contracts","de1902e864747568f2f5849597833bd76873204d3131eb037c6953cf039c3432")]:
        current=json.loads((root/"recipes"/path).read_bytes())[key]
        # Exclude only the four additive formats; preserve the entire frozen233 legacy map.
        current.pop("lyric-video")
        current.pop("image-video")
        current.pop("single-image-video")
        current.pop("meme")
        assert hashlib.sha256(json.dumps(current,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()==expected
    # Identity evidence: maj:~/rt-work/cx1022-t73L2/t73L2-evidence/identity-comparison.txt (15 legacy profiles/contracts; 30 byte-identical non-in_app renders).
    assert hashlib.sha256((root/"services/post_render.py").read_bytes()).hexdigest()=="8b57a9791be0a6fc45d5808866f3392816a8f6223ddecf6e85554225c1603484"
