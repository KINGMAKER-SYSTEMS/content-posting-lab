import hashlib,json
import pytest
from services import lyric_timing as timing

def resolve(**changed):
    args=dict(page_id='acct:burnt-mill-rd',account='burnt.mill.rd',sound_id='7611482162773035025',expected_sha256=timing._TIMING_SHA256);args.update(changed)
    return timing.resolve_lyric_timing('burnt-heavy-foot-chorus-v1',**args)

def test_registered_timing_matches_actual_approved_seven_phrase_anchors():
    t=resolve()
    assert [p['startMs']for p in t['phrases']]==[120,2990,5860,7060,9240,12310,17560]
    assert [p['endMs']for p in t['phrases']]==[2990,5860,7060,9240,12310,17560,20000]
    assert t['canonicalCaption']=='\n'.join(p['text']for p in t['phrases'])
    assert '(woo)' not in t['canonicalCaption']
    assert t['referenceAudioSha256']=='11ace1c61ab8535c87a71dddbe7f06d03ac56bd3324809561cb49a8ddf95823f'

@pytest.mark.parametrize('change',[{'page_id':'acct:culture-core-1'},{'account':'culture.core.1'},{'sound_id':'7694528135171295233'},{'expected_sha256':'0'*64}])
def test_scope_and_revision_refuse_other_page_sound_or_changed_manifest(change):
    with pytest.raises(timing.LyricTimingError):resolve(**change)

def test_unrelated_catalog_remains_available_when_burnt_manifest_is_missing(monkeypatch,tmp_path):
    monkeypatch.setattr(timing,'_TIMING_PATH',tmp_path/'missing.json')
    assert timing.lyric_timing_catalog(page_id='acct:culture-core-1',account='culture.core.1')==[]
    with pytest.raises(timing.LyricTimingError):resolve()

@pytest.mark.parametrize('mutate',[lambda t:t['phrases'][1].update(startMs=100),lambda t:t['phrases'][-1].update(endMs=20120),lambda t:t['phrases'][0].update(startMs=True),lambda t:t.update(canonicalCaption='unrelated quote'),lambda t:t['phrases'][0].update(extra='unknown')])
def test_structurally_invalid_approved_bytes_still_refuse(monkeypatch,tmp_path,mutate):
    t=json.loads(timing._TIMING_PATH.read_bytes());mutate(t);raw=json.dumps(t).encode();path=tmp_path/'timing.json';path.write_bytes(raw);digest=hashlib.sha256(raw).hexdigest();monkeypatch.setattr(timing,'_TIMING_PATH',path);monkeypatch.setattr(timing,'_TIMING_SHA256',digest)
    with pytest.raises(timing.LyricTimingError):resolve()

def test_same_length_content_tamper_fails_hash_before_loading(monkeypatch,tmp_path):
    raw=timing._TIMING_PATH.read_bytes().replace(b'Love me now',b'Hate me now');p=tmp_path/'tampered.json';p.write_bytes(raw);monkeypatch.setattr(timing,'_TIMING_PATH',p)
    with pytest.raises(timing.LyricTimingError,match='bytes differ'):resolve()


def test_manifest_export_preserves_exact_registered_utf8_bytes_not_catalog_payload():
    value=resolve()
    assert value['manifestJson'].encode('utf-8')==timing._TIMING_PATH.read_bytes()
    assert hashlib.sha256(value['manifestJson'].encode('utf-8')).hexdigest()==value['sha256']
    assert 'manifestJson' not in timing.lyric_timing_catalog(page_id=value['pageId'],account=value['account'])[0]
