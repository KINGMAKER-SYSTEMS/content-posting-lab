"""Actual tiny owned MP4 controls, synthetic cached visual authority, no provider QA."""
import copy, hashlib, json, os, shutil, subprocess, time
from pathlib import Path
import pytest
from services import source_byte_facts as f
from services import visual_admission as v
from services.source_treatment import source_treatment_receipt
from tests.master_pages_fixtures import master_pages
from PIL import Image
PAGE='acct:g1-fixture'; JOB='cpl-0123456789abcdef'
LAB_ORIGIN='https://risingtides-content-lab-production.up.railway.app'
MASTER,MASTER_HASH=master_pages(PAGE,handle='g1.fixture',vault_url='https://shipstream.risingtidesviral.com/vault/g1.fixture')
TREATMENT={'filters':{},'clipSpeed':1,'clipCrop':None,'stylePreset':None,
 'captionStyle':{'font':'TikTokSans12pt-Regular.ttf','size_pt':16,'line_balance':30,'position':'middle','align':'center','case':'as_written','color':'#ffffff','outline_width_px':0}}
RECIPE_ID='truck-scenic:master'; RECIPE_VERSION='fixture-v1'
RECIPE_SPEC={'schema':'dossier.recipe-spec.v4','masterPages':MASTER,'masterPagesHash':MASTER_HASH,
 'production':{'catalogVersion':'sha256:'+'0'*64,'controls':{},'modelId':None,'promptModuleId':None,'providerId':None,'referenceSetId':None,'sourceLibraryId':None,'variationValues':{}},
 'renderTreatment':TREATMENT,'demand':{'formatMix':{'truck-scenic':1}},
 'captionDiscipline':{'captionSet':'fixture','register':['relatable_meme'],'slingshotShare':None}}
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
SPEC='sha256:'+hashlib.sha256(canonical(RECIPE_SPEC)).hexdigest()
POLICY_INPUT={'pageId':PAGE,'recipeSpecHash':SPEC,'recipeId':RECIPE_ID,'recipeVersion':RECIPE_VERSION,'phase':'source_blank_inventory'}
POLICY='sha256:'+hashlib.sha256(canonical(POLICY_INPUT)).hexdigest()
BINDING_PLAN={'engine':'content_lab','pageId':PAGE,'jobId':JOB,'masterPages':MASTER,'masterPagesHash':MASTER_HASH,'recipeSpec':RECIPE_SPEC,
 'recipeSpecHash':SPEC,'recipeId':RECIPE_ID,'recipeVersion':RECIPE_VERSION,'policyHash':POLICY,'policyHashInput':POLICY_INPUT,
 'authority':'TEST-OWNED Dossier/context with genuine canonical hashes; not production or a paid generation job'}

@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    def forbidden(*a,**k): raise AssertionError('source facts must not launch visual/provider QA')
    monkeypatch.setattr(v,'scan_artifact',forbidden); monkeypatch.setattr(v,'_vision',forbidden)

@pytest.fixture(scope='session')
def media(tmp_path_factory):
    root=tmp_path_factory.mktemp('owned-g1-media')
    for name,color,audio in [('silent','black',False),('silent2','white',False),('silent3','red',False),('audio','black',True)]:
        args=['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',f'color=c={color}:s=64x64:r=30:d=0.1']
        if audio: args+=['-f','lavfi','-i','sine=frequency=440:sample_rate=44100:duration=0.1']
        args+=['-frames:v','3','-c:v','libx264','-threads','1','-pix_fmt','yuv420p']
        args+=['-c:a','aac','-shortest'] if audio else ['-an']
        subprocess.run([*args,str(root/(name+'.mp4'))],check=True,capture_output=True,timeout=15)
        Image.new('RGB',(64,64),color).save(root/(name+'.jpg'),format='JPEG',quality=90)
    return root

def visual(clip,index=0,verdict='clean'):
    return {'schema':v.SCHEMA,'pageId':PAGE,'jobId':JOB,'outputIndex':index,'sha256':clip['sha256'],'bytes':clip['bytes'],
      'verdict':verdict,'scannedAt':'2026-11-11T00:00:00+00:00',
      'reason':'full_frame_ocr_and_vision_clean' if verdict=='clean' else 'pre_existing_text_vision',
      'sampling':{'algorithm':v.ALGORITHM,'mode':'all_frames','frameCount':3,'expectedFrameCount':3},
      'ocr':{'status':'clean','candidateFrames':[]},
      'model':{'name':'SYNTHETIC-CACHED-VISUAL-NOT-PRODUCTION','status':'clean','sampledFrames':[0,1,2]}}

def ctx(root,media,name='silent',verdict='clean'):
    root.mkdir(parents=True,exist_ok=True); path=root/'source.mp4'; shutil.copyfile(media/(name+'.mp4'),path)
    sha=hashlib.sha256(path.read_bytes()).hexdigest()
    thumb=root/'thumb.jpg';shutil.copyfile(media/(name+'.jpg'),thumb)
    clip={'path':path.name,'name':path.name,'sha256':sha,'bytes':path.stat().st_size,'bakedAudio':False,'captionDelivery':'in_app',
      'source':{'pageId':PAGE,'masterPagesHash':MASTER_HASH,'contentNiche':MASTER['contentNiche'],'contentEngine':MASTER['contentEngine'],'vaultUrl':MASTER['vaultUrl']},
      'thumbnail':{'path':thumb.name,'sha256':hashlib.sha256(thumb.read_bytes()).hexdigest(),'bytes':thumb.stat().st_size},'sourceTreatment':{'schema':'content-lab.source-treatment.v1','sourceSha256':sha,
        'recipeSpecHash':SPEC,'generationJobId':JOB,'visualTreatment':{},'sourceRecipeTreatment':{}}}
    job={'engine':'content_lab','jobId':JOB,'pageId':PAGE,'recipeSpecHash':SPEC,'policyHash':POLICY,'recipeId':RECIPE_ID,'recipeVersion':RECIPE_VERSION,'masterPages':copy.deepcopy(MASTER),'masterPagesHash':MASTER_HASH,
      'sourceKind':'generated','artifactRoot':str(root),'status':'completed','clips':[clip],'quantityRequested':1,'token':'synthetic-download-token'}
    clip['sourceTreatment']=source_treatment_receipt(job,TREATMENT,sha,clip_speed=1,clip_crop=None)
    if verdict is not None: job['visualAdmission']={'0':visual(clip,verdict=verdict)}
    return job,clip,path

def test_real_silence_and_aac_ignore_in_app_false_audio_marker(media,tmp_path):
    for name,count in [('silent',0),('audio',1)]:
        job,clip,path=ctx(tmp_path/name,media,name); r,p=f.build(job,JOB,0,clip,path,allow_probe=True)
        assert r['status']=='MEASURED' and r['audio']['audioStreams']==count
        assert r['reason']==('audio_streams_present' if count else 'raw_source_facts_measured')
        assert r['native']=={'sound':'NOT_ASSESSED','end':'NOT_ASSESSED'} and r['policyHash']==POLICY
        assert hashlib.sha256(p['probeJson'].encode()).hexdigest()==r['audio']['probeSha256']
        assert r['custody']=={'beforeSha256':clip['sha256'],'beforeBytes':clip['bytes'],'afterSha256':clip['sha256'],'afterBytes':clip['bytes']}

@pytest.mark.parametrize('field,value',[('pageId','acct:wrong'),('jobId','cpl-'+'1'*16),('outputIndex',1),('outputIndex',False),('outputIndex',0.0),('sha256','0'*64),('bytes',1)])
def test_cached_wrong_identity_unknown(media,tmp_path,field,value):
    job,clip,path=ctx(tmp_path,media); job['visualAdmission']['0'][field]=value
    r,_=f.build(job,JOB,0,clip,path,allow_probe=True)
    assert r['status']=='UNKNOWN' and r['audio']['status']=='MEASURED' and r['visual']['decision'] is None

@pytest.mark.parametrize('fault',['algorithm','missing','terminal_unavailable','partial','model','timestamp','bool_count'])
def test_missing_stale_or_incomplete_visual_not_clean(media,tmp_path,fault):
    job,clip,path=ctx(tmp_path,media); d=job['visualAdmission']['0']
    if fault=='algorithm': d['sampling']['algorithm']='old'
    if fault=='missing': job.pop('visualAdmission')
    if fault=='terminal_unavailable': d.update(verdict='unavailable',reason='scan_failure_retry_exhausted')
    if fault=='partial': d['sampling']['frameCount']=2
    if fault=='model': d['model']['status']='unavailable'
    if fault=='timestamp': d['scannedAt']='yesterday'
    if fault=='bool_count': d['sampling'].update(frameCount=1,expectedFrameCount=True);d['model']['sampledFrames']=[0]
    r,_=f.build(job,JOB,0,clip,path,allow_probe=True)
    assert r['status']=='UNKNOWN' and r['audio']['audioStreams']==0

def test_cached_text_is_measured_negative(media,tmp_path):
    job,clip,path=ctx(tmp_path,media,verdict='text'); r,_=f.build(job,JOB,0,clip,path,allow_probe=True)
    assert r['status']=='MEASURED' and r['reason']=='visual_text_detected'

@pytest.mark.parametrize('fault',['job','phase','lyric','policy','bool_bytes'])
def test_bad_binding_phase_never_probes(media,tmp_path,monkeypatch,fault):
    job,clip,path=ctx(tmp_path,media)
    if fault=='job': job['jobId']='cpl-'+'1'*16
    if fault=='phase': job['recipeId']='lyric-video:master'
    if fault=='lyric': clip['lyricTiming']={'fixture':True}
    if fault=='policy': job.pop('policyHash')
    if fault=='bool_bytes': clip['bytes']=True
    monkeypatch.setattr(f,'_probe',lambda *_:pytest.fail('bad binding must not probe'))
    r,_=f.build(job,JOB,0,clip,path,allow_probe=True); assert r['status']=='UNKNOWN' and r['audio']['audioStreams'] is None

def test_mid_probe_change_refuses_with_actual_custody(media,tmp_path,monkeypatch):
    job,clip,path=ctx(tmp_path,media); real=f._probe
    def changed(p):
        value=real(p)
        with p.open('ab') as h:h.write(b'changed')
        return value
    monkeypatch.setattr(f,'_probe',changed); r,_=f.build(job,JOB,0,clip,path,allow_probe=True)
    assert r['status']=='UNKNOWN' and r['reason']=='source_changed_during_probe'
    assert r['custody']['beforeSha256']==clip['sha256'] and r['custody']['afterSha256']!=clip['sha256']
    assert r['audio']['audioStreams'] is None

def test_unreadable_does_not_fabricate_declared_hash(media,tmp_path):
    job,clip,path=ctx(tmp_path,media);path.unlink();r,_=f.build(job,JOB,0,clip,path,allow_probe=True)
    assert r['status']=='UNKNOWN' and all(x is None for x in r['custody'].values())

def test_stale_producer_phase_and_time_require_new_measurement(media,tmp_path):
    job,clip,path=ctx(tmp_path,media); measured,evidence=f.build(job,JOB,0,clip,path,allow_probe=True)
    job['sourceByteFactsEvidence']={'0':evidence};job['sourceByteFacts']={'0':copy.deepcopy(measured)}
    positive,_=f.build(job,JOB,0,clip,path);assert positive['status']=='MEASURED'
    for fault in ['policy','time','phase']:
        old=copy.deepcopy(measured)
        if fault=='policy':old['producer']['policySha256']='0'*64
        if fault=='time':old['measuredAt']='invalid'
        if fault=='phase':old['phase']='prepared_final'
        job['sourceByteFacts']={'0':old};r,_=f.build(job,JOB,0,clip,path)
        assert r['status']=='UNKNOWN' and r['audio']['audioStreams'] is None

def test_owned_probe_timeout_is_bounded(media,tmp_path,monkeypatch):
    tool=tmp_path/'owned-probe';tool.write_text('#!/bin/sh\nsleep 10\n');tool.chmod(0o700)
    monkeypatch.setattr(f.shutil,'which',lambda _:str(tool));monkeypatch.setattr(f,'PROBE_SECONDS',.15)
    started=time.monotonic()
    with pytest.raises(ValueError,match='audio_probe_timeout'):f._probe(media/'silent.mp4')
    assert time.monotonic()-started<2

@pytest.fixture
def endpoint(media,tmp_path,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import control_plane as cp
    job,clip,path=ctx(tmp_path/'media',media);store=tmp_path/'jobs.json'
    monkeypatch.setattr(cp,'_jobs_path',lambda:store)
    cp.atomic_save(store,{'version':1,'jobs':{JOB:job},'byIdempotency':{},'served':{}})
    monkeypatch.setenv('CONTROL_PLANE_TOKEN','synthetic-auth-fixture');monkeypatch.setenv('CONTENT_LAB_SOURCE_BYTE_FACTS','on')
    monkeypatch.setenv('CONTENT_LAB_SOURCE_BYTE_FACTS_COHORT',json.dumps([{'pageId':PAGE,'recipeSpecHash':SPEC}]))
    app=FastAPI();app.include_router(cp.router,prefix='/api/control-plane')
    return TestClient(app,base_url=LAB_ORIGIN),cp,store,job

def req(client,page=PAGE,auth=True):
    headers={'X-RT-Page-Id':page}
    if auth:headers['Authorization']='Bearer synthetic-auth-fixture'
    return client.get(f'/api/control-plane/v1/jobs/{JOB}/artifacts',headers=headers)

def test_normal_serializer_and_durable_raw_report(endpoint):
    client,cp,store,_=endpoint;response=req(client);assert response.status_code==200,response.text
    wire=response.json()['artifacts'][0]['sourceByteFacts'];assert wire['status']=='MEASURED' and wire['audio']['audioStreams']==0
    saved=cp.atomic_load(store)['jobs'][JOB];assert saved['sourceByteFacts']['0']==wire
    assert hashlib.sha256(saved['sourceByteFactsEvidence']['0']['probeJson'].encode()).hexdigest()==wire['audio']['probeSha256']
    wire['native']['sound']='FORGED';assert cp.atomic_load(store)['jobs'][JOB]['sourceByteFacts']['0']['native']['sound']=='NOT_ASSESSED'

@pytest.mark.parametrize('fault',['auth','page','off','empty','recipe'])
def test_auth_and_offscope_preserve_exact_store(endpoint,monkeypatch,fault):
    client,_,store,_=endpoint;before=store.read_bytes();monkeypatch.setattr(f,'_probe',lambda *_:pytest.fail('forbidden probe'))
    if fault=='off':monkeypatch.delenv('CONTENT_LAB_SOURCE_BYTE_FACTS')
    if fault=='empty':monkeypatch.setenv('CONTENT_LAB_SOURCE_BYTE_FACTS_COHORT','[]')
    if fault=='recipe':monkeypatch.setenv('CONTENT_LAB_SOURCE_BYTE_FACTS_COHORT',json.dumps([{'pageId':PAGE,'recipeSpecHash':'sha256:'+'b'*64}]))
    response=req(client,page='acct:other' if fault=='page' else PAGE,auth=fault!='auth')
    assert response.status_code==(401 if fault=='auth' else 404 if fault=='page' else 200)
    assert store.read_bytes()==before
    if response.status_code==200:assert 'sourceByteFacts' not in response.json()['artifacts'][0]

def test_cached_visual_arrival_does_not_reprobe_or_restamp(endpoint,monkeypatch):
    client,cp,store,_=endpoint;data=cp.atomic_load(store);cache=data['jobs'][JOB].pop('visualAdmission');cp.atomic_save(store,data)
    first=req(client).json()['artifacts'][0]['sourceByteFacts'];assert first['status']=='UNKNOWN' and first['audio']['audioStreams']==0
    data=cp.atomic_load(store);data['jobs'][JOB]['visualAdmission']=cache;cp.atomic_save(store,data)
    monkeypatch.setattr(f,'_probe',lambda *_:pytest.fail('valid byte facts must be reused'))
    second=req(client).json()['artifacts'][0]['sourceByteFacts'];assert second['status']=='MEASURED'
    assert second['audio']==first['audio'] and second['custody']==first['custody'] and second['measuredAt']==first['measuredAt']

def test_three_artifacts_progress_one_actual_probe_each_normal_poll(endpoint,media,monkeypatch):
    client,cp,store,job=endpoint
    for i,name in enumerate(['silent2','silent3'],1):
        extra,clip,path=ctx(Path(job['artifactRoot'])/str(i),media,name)
        clip['path']=str(path.relative_to(job['artifactRoot']));clip['thumbnail']['path']=str((path.parent/'thumb.jpg').relative_to(job['artifactRoot']));job['clips'].append(clip);job['visualAdmission'][str(i)]=visual(clip,index=i)
    job['quantityRequested']=3;cp.atomic_save(store,{'version':1,'jobs':{JOB:job},'byIdempotency':{},'served':{}})
    real=f._probe; calls=[]
    def observed(path):calls.append(str(path));return real(path)
    monkeypatch.setattr(f,'_probe',observed)
    export=os.environ.get('G1_FIXTURE_EXPORT_DIR')
    for n in [1,2,3]:
        response=req(client);assert response.status_code==200,response.text
        body=response.json();items=body['artifacts'];assert len(calls)==n
        assert sum(a['sourceByteFacts']['status']=='MEASURED' for a in items)==n
        assert cp.atomic_load(store)['jobs'][JOB]['sourceByteFactsCursor']==n%3
        if export:
            root=Path(export);root.mkdir(parents=True,exist_ok=True)
            (root/f'normal-poll-{n}.json').write_text(json.dumps({'response':body,'authority':'SYNTHETIC CACHED VISUAL FIXTURES; actual normal authenticated Lab serializer and real owned MP4 probes; no Worker automatic-consumption or production/public/native proof'},indent=2))
    assert len({a['sha256'] for a in items})==3
    if export:
        root=Path(export);media_root=root/'normal-media';media_root.mkdir(exist_ok=True)
        stored=cp.atomic_load(store)['jobs'][JOB];url_files=[]
        for i,artifact in enumerate(items):
            clip=stored['clips'][i]
            for kind,url,relative,ext in [('video',artifact['url'],clip['path'],'mp4'),('thumbnail',artifact['thumbnail']['url'],clip['thumbnail']['path'],'jpg')]:
                original=Path(stored['artifactRoot'])/relative;dest=media_root/(str(i)+'.'+ext);shutil.copyfile(original,dest)
                actual_sha=hashlib.sha256(dest.read_bytes()).hexdigest();expected=artifact if kind=='video' else artifact['thumbnail']
                assert actual_sha==expected['sha256'] and dest.stat().st_size==expected['bytes']
                url_files.append({'url':url,'path':str(dest),'sha256':actual_sha,'bytes':dest.stat().st_size,'contentType':'video/mp4' if kind=='video' else 'image/jpeg'})
        (root/'NORMAL-THREE-POLL-BINDING-AND-BYTES.json').write_text(json.dumps({'binding':BINDING_PLAN,'job':stored,'urlFiles':url_files,
          'authority':'Actual normal authenticated serializer, real owned MP4/JPEG/probes; cached visual authority SYNTHETIC ONLY, no Worker/runtime/public proof'},indent=2))

def test_export_actual_pair_controls(media,tmp_path):
    export=os.environ.get('G1_FIXTURE_EXPORT_DIR')
    if not export:return
    root=Path(export);root.mkdir(parents=True,exist_ok=True)
    for name,audio,verdict in [('silent_clean',False,'clean'),('unknown_visual',False,None),('cached_text',False,'text'),('aac_present',True,None)]:
        job,clip,path=ctx(tmp_path/name,media,'audio' if audio else 'silent',verdict)
        wire,private=f.build(job,JOB,0,clip,path,allow_probe=True)
        (root/(name+'.json')).write_text(json.dumps({'sourceByteFacts':wire,'job':job,'privateEvidence':private,
          'authority':'SYNTHETIC CACHED VISUAL FIXTURE ONLY; actual owned MP4/audio inventory; no production QA/public/native claim'},indent=2))
        shutil.copyfile(path,root/(name+'.mp4'))

def test_busy_owned_probe_does_not_overwrite_or_advance_cursor(endpoint):
    client,_,store,_=endpoint;before=store.read_bytes();f._PROBE_LOCK.acquire()
    try:r=req(client)
    finally:f._PROBE_LOCK.release()
    assert r.status_code==200 and r.json()['artifacts'][0]['sourceByteFacts']['status']=='UNKNOWN'
    assert store.read_bytes()==before

def test_context_change_during_probe_cannot_commit_old_facts(endpoint,monkeypatch):
    client,cp,store,_=endpoint;real=f._probe
    def changed(path):
        result=real(path);data=cp.atomic_load(store);data['jobs'][JOB]['policyHash']='sha256:'+'f'*64;cp.atomic_save(store,data);return result
    monkeypatch.setattr(f,'_probe',changed);r=req(client);assert r.status_code==200
    assert 'sourceByteFacts' not in r.json()['artifacts'][0]
    saved=cp.atomic_load(store)['jobs'][JOB];assert saved['policyHash']=='sha256:'+'f'*64 and 'sourceByteFacts' not in saved

def test_missing_private_measurement_is_remeasured_not_trusted(endpoint,monkeypatch):
    client,cp,store,_=endpoint;first=req(client);assert first.status_code==200
    data=cp.atomic_load(store);data['jobs'][JOB].pop('sourceByteFactsEvidence');cp.atomic_save(store,data)
    real=f._probe;calls=[]
    def counted(path):calls.append(path);return real(path)
    monkeypatch.setattr(f,'_probe',counted);second=req(client)
    assert second.status_code==200 and second.json()['artifacts'][0]['sourceByteFacts']['status']=='MEASURED'
    assert len(calls)==1

def test_cached_audio_summary_cannot_disagree_with_genuine_raw_probe(media,tmp_path):
    job,clip,path=ctx(tmp_path,media,'audio')
    measured,evidence=f.build(job,JOB,0,clip,path,allow_probe=True)
    assert measured['audio']['audioStreams']==1
    tampered=copy.deepcopy(measured);tampered['audio']['audioStreams']=0
    job['sourceByteFacts']={'0':tampered};job['sourceByteFactsEvidence']={'0':evidence}
    cached,_=f.build(job,JOB,0,clip,path)
    assert cached['status']=='UNKNOWN', 'genuine AAC probe + altered summary must not qualify cached silence'
    fresh,_=f.build(job,JOB,0,clip,path,allow_probe=True)
    assert fresh['audio']['audioStreams']==1 and fresh['reason']=='audio_streams_present'

# Opus R5 findings: these assertions must fail against the unchanged R5 source.
def test_older_poll_cannot_overwrite_newer_facts_cursor(endpoint,monkeypatch):
    client,cp,store,_=endpoint;real=f._probe
    def concurrent_commit(path):
        result=real(path);data=cp.atomic_load(store);job=data['jobs'][JOB]
        job['sourceByteFacts']={'0':{'newer':'owned concurrent result'}}
        job['sourceByteFactsEvidence']={'0':{'newer':'owned concurrent evidence'}}
        job['sourceByteFactsCursor']=7;cp.atomic_save(store,data)
        return result
    monkeypatch.setattr(f,'_probe',concurrent_commit)
    response=req(client);assert response.status_code==200
    current=cp.atomic_load(store)['jobs'][JOB]
    assert current['sourceByteFacts']=={'0':{'newer':'owned concurrent result'}}
    assert current['sourceByteFactsEvidence']=={'0':{'newer':'owned concurrent evidence'}}
    assert current['sourceByteFactsCursor']==7
    assert 'sourceByteFacts' not in response.json()['artifacts'][0]

@pytest.mark.parametrize('fault',['save','root_type','root_nul'])
def test_cohort_inventory_error_returns_normal_artifacts_without_qualification(endpoint,monkeypatch,fault):
    client,cp,store,_=endpoint
    if fault=='save':
        def broken(*a,**k):raise OSError('test-owned unavailable store')
        monkeypatch.setattr(cp,'_save_jobs',broken)
    else:
        data=cp.atomic_load(store);data['jobs'][JOB]['artifactRoot']=17 if fault=='root_type' else '\x00'
        cp.atomic_save(store,data)
    before=store.read_bytes();response=req(client)
    assert response.status_code==200,response.text
    assert len(response.json()['artifacts'])==1
    assert 'sourceByteFacts' not in response.json()['artifacts'][0]
    assert store.read_bytes()==before

@pytest.mark.parametrize('root',[None,''])
def test_missing_artifact_root_never_probes_cwd_or_commits(endpoint,monkeypatch,root):
    client,cp,store,_=endpoint;data=cp.atomic_load(store);data['jobs'][JOB]['artifactRoot']=root;cp.atomic_save(store,data)
    before=store.read_bytes();monkeypatch.setattr(f,'_probe',lambda *_:pytest.fail('missing artifactRoot must not probe'))
    response=req(client);assert response.status_code==200
    assert 'sourceByteFacts' not in response.json()['artifacts'][0]
    assert store.read_bytes()==before

def test_private_raw_probe_hash_pins_same_audio_count_content(media,tmp_path):
    job,clip,path=ctx(tmp_path,media);wire,private=f.build(job,JOB,0,clip,path,allow_probe=True)
    assert wire['audio']['audioStreams']==0
    job['sourceByteFacts']={'0':wire};job['sourceByteFactsEvidence']={'0':private}
    assert f.build(job,JOB,0,clip,path)[0]['status']=='MEASURED'
    raw=json.loads(private['probeJson']);raw['testOwnedExtra']='same audio count different raw bytes'
    private['probeJson']=json.dumps(raw)
    assert f._audio_count(private['probeJson'].encode())==0
    # Neither recorded raw hash changes; only the actual raw bytes are edited.
    assert private['probeSha256']==wire['audio']['probeSha256']
    assert f.build(job,JOB,0,clip,path)[0]['status']=='UNKNOWN'
