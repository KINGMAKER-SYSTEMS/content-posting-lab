#!/usr/bin/env python3
"""Ep2 deterministic compile: per-segment cuts (clips at VO windows over the
segment wav, Ken-Burns gap fills), 0.42s crossfade body chain, room-tone bed
floor (0.08 + duck, the approved law), then intro + body + outro final.

All learned laws baked in: timescale 24000 everywhere, fps/settb normalize
before xfade, per-stream verification by the caller."""
import json, subprocess, sys
from pathlib import Path

ROOT = Path('/Users/risingtidesdev/dev/content-posting-lab')
EP2 = ROOT / 'yt-pipeline/src/animations/ep2'
BR = ROOT / 'yt-pipeline/src/animations/branding'
BED = '/Volumes/T9/agenticbuildernews/agenticnews_assets/bed_v2.mp3'
XF = 0.42
WORK = EP2 / 'work'
WORK.mkdir(exist_ok=True)

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(' '.join(map(str, cmd))[:300]); print(r.stderr[-1500:]); sys.exit(1)

def vdur(p):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'csv=p=0', str(p)], capture_output=True, text=True)
    return float(r.stdout.strip())

segs = json.loads((EP2 / 'segments.json').read_text())

seg_cuts = []
for s in segs:
    n = s['i']
    wav = EP2 / f'vo/s{n}.wav'
    align = json.loads((EP2 / f'Seg{n}/ALIGN.json').read_text())
    for v in align['videos']:
        v.setdefault('video', v.get('name'))
    wins = sorted(align['videos'], key=lambda v: v['voStart'])
    D = vdur(wav)

    parts = []  # (path, dur)
    def gap_fill(src, frame_at_end, t0, t1, tag):
        """Ken-Burns still from src for the [t0,t1] gap."""
        d = t1 - t0
        if d < 0.06: return
        png = WORK / f's{n}_{tag}.png'
        if frame_at_end:
            run(['ffmpeg', '-y', '-loglevel', 'error', '-sseof', '-0.1', '-i', str(src), '-frames:v', '1', str(png)])
        else:
            run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(src), '-frames:v', '1', str(png)])
        nfr = max(2, int(round(d * 24)))
        mp4 = WORK / f's{n}_{tag}.mp4'
        run(['ffmpeg', '-y', '-loglevel', 'error', '-loop', '1', '-i', str(png), '-frames:v', str(nfr),
             '-vf', f"scale=2112:1188,zoompan=z='1+0.05*on/{nfr}':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':d=1:s=1920x1080:fps=24,format=yuv420p",
             '-c:v', 'libx264', '-crf', '18', '-preset', 'medium', '-video_track_timescale', '24000', str(mp4)])
        parts.append((mp4, nfr / 24.0))

    cursor = 0.0
    for k, w in enumerate(wins):
        clip = EP2 / f"Seg{n}/{w['video']}/final.mp4"
        if w['voStart'] - cursor >= 0.06:
            ref = clip if k == 0 else EP2 / f"Seg{n}/{wins[k-1]['video']}/final.mp4"
            gap_fill(ref, k != 0, cursor, w['voStart'], f'g{k}')
        cd = vdur(clip)
        norm = WORK / f's{n}_c{k}.mp4'
        run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(clip), '-an',
             '-c:v', 'libx264', '-crf', '18', '-preset', 'medium', '-r', '24',
             '-video_track_timescale', '24000', str(norm)])
        parts.append((norm, cd))
        cursor = w['voEnd']
    if D - cursor >= 0.06:
        last = EP2 / f"Seg{n}/{wins[-1]['video']}/final.mp4"
        gap_fill(last, True, cursor, D, 'tail')

    lst = WORK / f's{n}_list.txt'
    lst.write_text('\n'.join(f"file '{p}'" for p, _ in parts))
    cut = EP2 / f's{n}_cut.mp4'
    run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', str(lst),
         '-i', str(wav),
         '-map', '0:v', '-map', '1:a',
         '-c:v', 'libx264', '-crf', '18', '-preset', 'medium', '-r', '24',
         '-video_track_timescale', '24000',
         '-af', 'aresample=48000,aformat=channel_layouts=stereo,apad', '-shortest',
         '-c:a', 'aac', '-b:a', '192k', str(cut)])
    seg_cuts.append((cut, vdur(cut)))
    print(f's{n}_cut: {vdur(cut):.2f}s (wav {D:.2f}s, {len(parts)} parts)')

# body chain with crossfades + bed floor
inputs, fc = [], []
for p, _ in seg_cuts:
    inputs += ['-i', str(p)]
for i, (p, d) in enumerate(seg_cuts):
    fc.append(f"[{i}:v]fps=24,settb=AVTB,format=yuv420p[v{i}]")
    fc.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
pv, pa = '[v0]', '[a0]'
t_acc = seg_cuts[0][1]
for i in range(1, len(seg_cuts)):
    off = t_acc - XF
    fc.append(f"{pv}[v{i}]xfade=transition=fade:duration={XF}:offset={off:.4f}[xv{i}]")
    fc.append(f"{pa}[a{i}]acrossfade=d={XF}:c1=tri:c2=tri[xa{i}]")
    pv, pa = f'[xv{i}]', f'[xa{i}]'
    t_acc = off + seg_cuts[i][1]
bed_idx = len(seg_cuts)
fc.append(f"{pa}asplit=2[voA][voB]")
fc.append(f"[{bed_idx}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=duration={t_acc:.3f},volume=0.08,afade=t=in:st=0:d=1,afade=t=out:st={t_acc-2:.3f}:d=2[bedv]")
fc.append("[bedv][voB]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=300[duck]")
fc.append("[voA][duck]amix=inputs=2:weights=1 0.9:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]")
fc.append(f"{pv}format=yuv420p[vout]")
fcs = WORK / 'body_fc.txt'
fcs.write_text(';\n'.join(fc))
body = EP2 / 'ep2_body.mp4'
run(['ffmpeg', '-y', '-loglevel', 'error'] + inputs + ['-stream_loop', '-1', '-i', BED,
     '-filter_complex_script', str(fcs), '-map', '[vout]', '-map', '[aout]',
     '-c:v', 'libx264', '-crf', '18', '-preset', 'medium', '-r', '24',
     '-video_track_timescale', '24000', '-c:a', 'aac', '-b:a', '192k', str(body)])
print(f'body: {vdur(body):.2f}s')

# final: intro + body + outro
d_in, d_b, d_out = vdur(BR / 'abn_intro.mp4'), vdur(body), vdur(BR / 'abn_outro.mp4')
off1 = d_in - XF
off2 = off1 + d_b - XF
fc2 = [
    "[0:v]fps=24,settb=AVTB,format=yuv420p[v0]", "[1:v]fps=24,settb=AVTB,format=yuv420p[v1]",
    "[2:v]fps=24,settb=AVTB,format=yuv420p[v2]",
    "[0:a]aresample=48000,aformat=channel_layouts=stereo,afade=t=in:st=0:d=0.04[a0]",
    "[1:a]aresample=48000,aformat=channel_layouts=stereo[a1]",
    "[2:a]aresample=48000,aformat=channel_layouts=stereo[a2]",
    f"[v0][v1]xfade=transition=fade:duration={XF}:offset={off1:.4f}[xv1]",
    f"[xv1][v2]xfade=transition=fade:duration={XF}:offset={off2:.4f}[xv2]",
    f"[a0][a1]acrossfade=d={XF}:c1=tri:c2=tri[xa1]",
    f"[xa1][a2]acrossfade=d={XF}:c1=tri:c2=tri[xa2]",
    "[xv2]format=yuv420p[vout]",
    "[xa2]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]",
]
fcs2 = WORK / 'final_fc.txt'
fcs2.write_text(';\n'.join(fc2))
final = EP2 / 'ep2_workflows_draft.mp4'
run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(BR / 'abn_intro.mp4'), '-i', str(body),
     '-i', str(BR / 'abn_outro.mp4'), '-filter_complex_script', str(fcs2),
     '-map', '[vout]', '-map', '[aout]', '-c:v', 'libx264', '-crf', '18', '-preset', 'medium',
     '-r', '24', '-video_track_timescale', '24000', '-c:a', 'aac', '-b:a', '192k', str(final)])
print(f'FINAL DRAFT: {final} — {vdur(final):.2f}s')

# chapter map (intro shift)
shift = off1
cum = 0.0
print('0:00 Intro')
for i, (p, d) in enumerate(seg_cuts):
    t = int(round(shift + cum))
    print(f'{t // 60}:{t % 60:02d} ' + segs[i]['title'])
    cum += d - XF
