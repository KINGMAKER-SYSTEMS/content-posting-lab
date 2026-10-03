"""Fixture bridge: actual Worker client -> HTTP Lab -> native OCR -> Worker gate.

The GLM reply and bucket are deliberate fixture doubles. Live GLM/deployment
receipts remain separate; this harness cannot prove production is configured.
Run: .venv/bin/python scripts/verify_visual_admission_e2e.py /path/to/control-plane-worker
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI
from PIL import Image, ImageDraw, ImageFont
import uvicorn
from routers import control_plane as cp
from services import visual_admission as gate

worker = Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory(prefix='visual-e2e-') as directory:
    root = Path(directory)
    os.environ['CONTROL_PLANE_TOKEN'] = 'fixture-visual-token'
    cp._jobs_path = lambda: root/'jobs.json'
    gate._lock_path = lambda: root/'scan.lock'
    gate._vision = lambda *a: {'verdict':'clean','reason':'Explicit fixture GLM double'}
    font_paths = ['/System/Library/Fonts/Supplemental/Arial.ttf','/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']
    font = ImageFont.truetype(next(p for p in font_paths if Path(p).exists()), 42)
    clips = []
    for label in ['clean', 'text']:
        frames = root/label; frames.mkdir()
        for index in range(25):
            image = Image.new('RGB',(640,360),'white')
            if label == 'text' and index == 12:
                ImageDraw.Draw(image).text((40,140),'EXISTING TEXT',font=font,fill='black')
            image.save(frames/f'{index:02}.png')
        video = root/f'{label}.mp4'
        subprocess.run(['ffmpeg','-v','error','-framerate','5','-i',str(frames/'%02d.png'),'-c:v','libx264','-pix_fmt','yuv420p',str(video)],check=True)
        clips.append({'path':video.name,'sha256':hashlib.sha256(video.read_bytes()).hexdigest(),'bytes':video.stat().st_size})
    job_id = 'cpl-0123456789abcdef'
    cp.atomic_save(cp._jobs_path(), {'jobs':{job_id:{'pageId':'acct:fixture','artifactRoot':str(root),'clips':clips}}})
    app = FastAPI(); app.include_router(cp.router,prefix='/api/control-plane')
    sock = socket.socket(); sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app,log_level='error'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]),daemon=True); thread.start()
    until = time.monotonic()+5
    while not server.started and time.monotonic()<until: time.sleep(.01)
    assert server.started
    script = '''
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';
const [worker, port, clipsJson] = process.argv.slice(1);
const {ContentLabClient} = await import(pathToFileURL(`${worker}/src/adapters/contentLabClient.js`));
const {requireVisualAdmission} = await import(pathToFileURL(`${worker}/src/ops/visualAdmission.js`));
const client = new ContentLabClient({expectedOrigin:'https://lab.fixture',token:'fixture-visual-token',retries:0,
 fetchImpl:(url,init)=>fetch(`http://127.0.0.1:${port}${new URL(url).pathname}`,init)});
const d1={prepare:sql=>({bind:(...bind)=>({sql,bind})}),batch:async()=>[]};
const clips=JSON.parse(clipsJson), bankWrites=[], results=[];
for(let index=0;index<clips.length;index++) {
 const binding={jobId:'cpl-0123456789abcdef',pageId:'acct:fixture',outputIndex:index,sha256:clips[index].sha256,bytes:clips[index].bytes,sourcedVideo:true};
 let decision; const deadline=Date.now()+60000;
 do { decision=await client.getVisualAdmission(binding); if(decision.reason==='scan_pending') await new Promise(r=>setTimeout(r,100)); }
 while(decision.reason==='scan_pending'&&Date.now()<deadline);
 try { await requireVisualAdmission(d1,{client,ownerId:'fixture',now:new Date().toISOString(),...binding}); bankWrites.push(index); }
 catch(error) { assert.equal(error.code,'ARTIFACT_PREEXISTING_TEXT'); }
 results.push({outputIndex:index,verdict:decision.verdict,frames:decision.sampling.frameCount,durationMs:decision.sampling.durationMs});
}
assert.deepEqual(bankWrites,[0]);
assert.equal(results[0].verdict,'clean'); assert.equal(results[1].verdict,'text');
console.log(JSON.stringify({fixtureOnly:true,glm:'stub',transport:'real HTTP',results,bankWrites}));
'''
    try:
        result = subprocess.run(['node','--input-type=module','-e',script,str(worker),str(port),json.dumps(clips)],capture_output=True,text=True,timeout=90)
        if result.returncode: raise RuntimeError(result.stderr)
        print(result.stdout.strip())
    finally:
        server.should_exit=True; thread.join(timeout=5); sock.close()
