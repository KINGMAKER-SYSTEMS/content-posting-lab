#!/usr/bin/env python3
"""Ep2 VO chain: scripts -> pocket-tts (John's voice) -> whisper word grid ->
segments.json ledger. Outputs stay in the repo tree (factory wipes T9)."""
import json, re, subprocess, sys
from pathlib import Path

ROOT = Path('/Users/risingtidesdev/dev/content-posting-lab')
EP = ROOT / 'yt-pipeline/drafts/ep-workflows-over-prompts'
OUT = ROOT / 'yt-pipeline/src/animations/ep2'
VO = OUT / 'vo'
VOICE = ROOT / 'agenticnews_assets/john_voice.safetensors'
VO.mkdir(parents=True, exist_ok=True)

outline = json.loads((EP / 'outline.json').read_text())
seg_titles = {s['index']: s['title'] for s in outline['segments']}

segments = []
for i in range(9):
    md = (EP / f'scripts/s{i}.md').read_text().strip()
    lines = md.split('\n')
    # drop header line(s) starting with '#'; script body is the rest
    body = '\n'.join(l for l in lines if not l.startswith('#')).strip()
    assert len(body.split()) > 60, f's{i} body too short'

    wav = VO / f's{i}.wav'
    if not wav.exists():
        cmd = ['pocket-tts', 'generate', '--text', body, '--output-path', str(wav), '--quiet']
        if VOICE.exists():
            cmd += ['--voice', str(VOICE)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not wav.exists():
            print(f's{i} TTS FAILED: {r.stderr[-300:]}'); sys.exit(1)

    dur = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                '-of', 'csv=p=0', str(wav)], capture_output=True, text=True).stdout.strip())

    wj = VO / f's{i}.json'
    if not wj.exists():
        r = subprocess.run(['whisper', str(wav), '--model', 'tiny.en', '--word_timestamps', 'True',
                            '--output_format', 'json', '--output_dir', str(VO), '--language', 'en'],
                           capture_output=True, text=True, timeout=600)
        if not wj.exists():
            print(f's{i} whisper FAILED: {r.stderr[-300:]}'); sys.exit(1)

    W = json.loads(wj.read_text())
    words = [{'w': w['word'].strip(), 't': round(w['start'], 2), 'e': round(w['end'], 2)}
             for seg in W['segments'] for w in seg.get('words', [])]
    segments.append({'i': i, 'title': seg_titles.get(i, f'Segment {i}'),
                     'durationSec': round(dur, 2), 'script': body, 'words': words})
    print(f's{i}: {dur:.1f}s, {len(words)} words — {seg_titles.get(i, "")[:50]}')

(OUT / 'segments.json').write_text(json.dumps(segments, indent=1))
total = sum(s['durationSec'] for s in segments)
print(f'LEDGER: {OUT}/segments.json — {len(segments)} segments, {total/60:.1f} min total VO')
