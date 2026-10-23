import copy
import itertools
import math
import pytest
from services import beat_slideshow as beat
from services import control_plane_image_video as iv


def config(percent=100):
    return dict(experimentId="cats-1022", soundId="song-1", soundUrl="https://www.tiktok.com/music/song-1", bpm=120., firstBeatOffset=.13, beatSource="metadata", captionPoolId="pen-pal3-cute", captions=["tiny paws big feelings", "you are my favorite human"], canaryPercent=percent)


def base():
    return iv.image_video_plan("acct:pen-pal3")


@pytest.mark.parametrize("note,step",list(zip(beat.NOTES,(.125,.25,.5,1.))))
def test_actual_photo_changes_follow_subdivision_without_moving_hook(note,step):
    cfg=config();plan=beat.make_plan(base(),cfg,beat.NOTES.index(note))
    spec=iv.build_image_video_spec(plan)
    assert spec["audio"]=={"section_start":0,"section_end":12.}
    assert spec["hook"]["text"]==cfg["captions"][0]
    starts=list(itertools.accumulate([0]+[x["duration"] for x in spec["media"]]))
    assert starts[-1]==pytest.approx(12)
    assert all(b-a==pytest.approx(step,abs=1e-6) for a,b in zip(starts[1:-2],starts[2:-1]))
    for t in starts[1:-1]:
        assert (t-cfg["firstBeatOffset"])/step==pytest.approx(round((t-cfg["firstBeatOffset"])/step),abs=1e-6)
    assert all(x["duration"]>0 for x in spec["media"])
    assert all(a["library"]!=b["library"] for a,b in zip(spec["media"],spec["media"][1:]))


def test_factorial_arms_and_caption_arrangement_identity():
    cfg=config();plans=[beat.make_plan(base(),cfg,i) for i in range(48)]
    assert len({p["signature"] for p in plans})==48
    assert {p["beatSlideshow"]["note"] for p in plans}==set(beat.NOTES)
    assert len({tuple(p["beatSlideshow"]["arrangement"]) for p in plans})==6
    assert plans[0]["media"]==plans[4]["media"]
    assert plans[0]["signature"]!=plans[4]["signature"]
    assert plans[0]["beatSlideshow"]["caption"]!=plans[4]["beatSlideshow"]["caption"]
    assert plans[0]["beatSlideshow"]["caption"]==plans[8]["beatSlideshow"]["caption"]
    assert plans[0]["media"]!=plans[8]["media"]


def test_holdout_is_stable_fixed_asset_and_page_assignment():
    cfg=config(0);a=beat.make_plan(base(),cfg,0);b=beat.make_plan(base(),cfg,3)
    assert a["media"]==b["media"]
    assert a["signature"]==b["signature"]
    expected=copy.deepcopy(base()["media"])
    for item in expected:item["kenBurns"]=None
    assert a["media"]==expected
    assert a["beatSlideshow"]["cohort"]=="holdout"
    assert beat.cohort("acct:pen-pal3",config(50))==beat.cohort("acct:pen-pal3",config(50))


@pytest.mark.parametrize("key,value",[("bpm",True),("bpm",float('nan')),("firstBeatOffset",-1),("canaryPercent",101),("captions",[]),("soundUrl","http://bad")])
def test_bad_input_never_becomes_an_experimental_render(key,value):
    cfg=config();cfg[key]=value
    with pytest.raises(ValueError):beat.make_plan(base(),cfg,0)


def test_tampered_caption_or_grid_refuses_even_if_resigned():
    plan=beat.make_plan(base(),config(),0)
    plan["media"][0]["duration"]+=.01
    plan["signature"]=iv.image_video_plan_signature(plan)
    with pytest.raises(iv.ImageVideoError):iv.build_image_video_spec(plan)


@pytest.mark.asyncio
async def test_metadata_skips_download_and_keeps_start(monkeypatch):
    import scraper.frame_extractor as extractor
    async def forbidden(*a,**k):raise AssertionError("metadata requires no download")
    monkeypatch.setattr(extractor,"download_video",forbidden)
    result=await beat.resolve_sound_beats("https://www.tiktok.com/music/song-1",config())
    assert result["bpm"]==120 and result["firstBeatOffset"]==.13
    assert result["audioStart"]==0 and result["analysisSeconds"]==0


def test_same_page_can_supply_both_cohorts_without_duplicate_holdouts():
    cfg=config(50)
    plans=[beat.make_plan(base(),cfg,i) for i in range(48)]
    assert {p["beatSlideshow"]["cohort"] for p in plans}=={"canary","holdout"}
    for plan in plans:
        assert all(x["kenBurns"] is None for x in plan["media"])
    specs={}
    for plan in plans:
        encoded=beat.digest(iv.build_image_video_spec(plan))
        assert specs.setdefault(encoded,plan["signature"])==plan["signature"]


@pytest.mark.asyncio
async def test_metadata_invalid_url_refuses():
    with pytest.raises(ValueError):await beat.resolve_sound_beats("not a sound link",config())


def test_outcomes_keep_timing_arrangement_caption_and_sound_separate():
    plans=[beat.make_plan(base(),config(),i) for i in (0,1,4,8)]
    rows=[dict(postId=str(i),views24h=100*(i+1),receipt=p['beatSlideshow']) for i,p in enumerate(plans)]
    result=beat.summarize_results(rows)
    timing={x['arm']:x for x in result if x['factor']=='timing'}
    assert timing['1/16']['posts']==3 and timing['1/16']['meanViews24h']==pytest.approx(800/3)
    assert timing['1/8']['posts']==1 and timing['1/8']['meanViews24h']==200
    assert {x['factor'] for x in result}=={'timing','arrangement','caption','cohort'}
    with pytest.raises(ValueError):beat.summarize_results(rows+[rows[0]])


def test_analysis_route_auth_metadata_and_fixed_fallback(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routers.control_plane as cp
    app=FastAPI();app.include_router(cp.router,prefix='/api/control-plane')
    client=TestClient(app);monkeypatch.setenv('CONTROL_PLANE_TOKEN','beat-test-token')
    url='/api/control-plane/v1/slideshow-beats'
    assert client.post(url,json={'soundUrl':config()['soundUrl']}).status_code==401
    headers={'Authorization':'Bearer beat-test-token'}
    result=client.post(url,json={'soundUrl':config()['soundUrl'],'metadata':config()},headers=headers)
    assert result.status_code==200 and result.json()['audioStart']==0
    async def unavailable(*a,**k):raise ValueError('silence')
    monkeypatch.setattr(beat,'resolve_sound_beats',unavailable)
    result=client.post(url,json={'soundUrl':config()['soundUrl']},headers=headers)
    assert result.status_code==422 and result.json()['detail']=='beat_analysis_unavailable_keep_fixed_slideshow'


def test_comparison_separates_config_revisions_and_rejects_false_receipts():
    a=config();b=config();b['bpm']=121
    rows=[dict(postId=str(i),views24h=100,receipt=beat.make_plan(base(),cfg,0)['beatSlideshow']) for i,cfg in enumerate((a,b))]
    result=beat.summarize_results(rows)
    assert len({x['configSha256'] for x in result})==2
    rows[0]['receipt']['caption']='foreign caption'
    with pytest.raises(ValueError):beat.summarize_results(rows)
