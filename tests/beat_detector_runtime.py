"""Run with a librosa-installed Python; exact shipped detector, generated sound."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import wave,math,struct,subprocess,json,time,tempfile
from services.beat_slideshow import DETECT_AUDIO_CODE
with tempfile.TemporaryDirectory() as d:
    p=d+'/click.wav';sr=22050;offset=float(sys.argv[1]) if len(sys.argv)>1 else .13
    data=[]
    for i in range(sr*20):
        t=i/sr;phase=(t-offset)%.5
        value=.9*math.sin(2*math.pi*880*t)*math.exp(-phase*180) if t>=offset and phase<.03 else 0
        data.append(struct.pack('<h',int(value*32767)))
    with wave.open(p,'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(sr);f.writeframes(b''.join(data))
    start=time.monotonic()
    r=subprocess.run([sys.executable,'-c',DETECT_AUDIO_CODE,p],text=True,capture_output=True,timeout=45)
    assert r.returncode==0,r.stderr
    out=json.loads(r.stdout);assert abs(out['bpm']-120)<.5,out
    assert abs(out['firstBeatOffset']-offset)<.02,out
    print(json.dumps(dict(proof='shipped-detector-synthetic-click',**out,seconds=round(time.monotonic()-start,3))))
    with wave.open(p,'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(sr);f.writeframes(b'\0\0'*(sr*3))
    silent=subprocess.run([sys.executable,'-c',DETECT_AUDIO_CODE,p],text=True,capture_output=True,timeout=45)
    assert silent.returncode!=0 and 'insufficient sound' in silent.stderr
    print('silent sound refused; fixed-slideshow fallback remains available')
