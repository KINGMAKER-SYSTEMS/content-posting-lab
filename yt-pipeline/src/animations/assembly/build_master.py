#!/usr/bin/env python3
"""Continuous-master assembly v2 — no SFX, no bed, no dead air.

Each segment's aligned cut is trimmed at the end of its last CLEAN spoken
sentence (dropping the factory's broken 'Next up...' segues, TTS stutters,
and trailing artifacts), then segments are joined with a 0.42s audio+video
crossfade. Audio is each cut's own dry VO — no external audio needed.

Trim points are GRID-time sentence ends chosen by hand from segments.json
(see TRIM_GRID below), converted to cut-local time via each cut's cutBase.

Usage: build_master.py <firstSeg> <lastSeg> <out.mp4>
"""
import subprocess, sys
from pathlib import Path

ASM = Path('/Users/risingtidesdev/dev/content-posting-lab/yt-pipeline/src/animations/assembly')
XF = 0.42  # story->story fade, ~10f @ 24fps

CUTBASE = {n: 0.0 for n in range(10)}
CUTBASE[7] = 21.34

# grid-time trim: ~0.4-0.6s after the final word of the last clean sentence
TRIM_GRID = {
    0: 98.60,   # "...if you're not careful."         (drops wrong-promise segue)
    1: 84.40,   # "...This might finally do it."      (drops TTS stutter loop)
    2: 69.05,   # "...could reshape how you build."   (drops wrong-promise segue)
    3: 75.10,   # "...don't blame AI without data."   (drops segue + artifact)
    4: 85.16,   # "...might be your next best skill." (drops 'tools.' artifact)
    5: 78.85,   # "...not a plug and play fix."       (drops undelivered promise)
    6: 80.35,   # "...who holds the keys."            (rest was already truncated mid-sentence)
    7: 79.85,   # "...focus on the creative stuff."   (full close)
    8: 82.45,   # "...expert level thinking too."     (final sentence was chopped at cutEnd)
    9: 83.95,   # "...no hardware upgrades required." (full close)
}

def probe_dur(p):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'csv=p=0', str(p)], capture_output=True, text=True)
    return float(r.stdout.strip())

first, last, outname = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
segs = list(range(first, last + 1))

parts = []
for n in segs:
    src = ASM / f's{n}_aligned.mp4'
    local_trim = TRIM_GRID[n] - CUTBASE[n]
    full = probe_dur(src)
    t = min(local_trim, full)
    parts.append(dict(n=n, src=src, t=t))
    print(f's{n}: trim at {t:.2f}s (cut is {full:.2f}s)')

inputs = []
for p in parts:
    inputs += ['-i', str(p['src'])]

fc = []
# trimmed, normalized streams
for i, p in enumerate(parts):
    fc.append(f"[{i}:v]trim=0:{p['t']:.3f},setpts=PTS-STARTPTS,fps=24,settb=AVTB,format=yuv420p[v{i}]")
    fc.append(f"[{i}:a]atrim=0:{p['t']:.3f},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[a{i}]")

pv, pa = '[v0]', '[a0]'
t_acc = parts[0]['t']
for i in range(1, len(parts)):
    off = t_acc - XF
    fc.append(f"{pv}[v{i}]xfade=transition=fade:duration={XF}:offset={off:.4f}[xv{i}]")
    fc.append(f"{pa}[a{i}]acrossfade=d={XF}:c1=tri:c2=tri[xa{i}]")
    pv, pa = f'[xv{i}]', f'[xa{i}]'
    t_acc = off + parts[i]['t']

fc.append(f"{pv}format=yuv420p[vout]")
# room-tone floor: bed_v2 at the approved barely-there level (-53dB presence in
# VO gaps), sidechain-ducked under speech. Kills the digital-silence "dropout"
# feel between sentences without bringing back SFX.
BED = '/Volumes/T9/agenticbuildernews/agenticnews_assets/bed_v2.mp3'
bed_idx = len(parts)
fc.append(f"{pa}asplit=2[voA][voB]")
fc.append(f"[{bed_idx}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=duration={t_acc:.3f},volume=0.08,afade=t=in:st=0:d=1,afade=t=out:st={t_acc - 2:.3f}:d=2[bedv]")
fc.append("[bedv][voB]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=300[duck]")
fc.append("[voA][duck]amix=inputs=2:weights=1 0.9:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]")

work = ASM / 'work_master'
work.mkdir(exist_ok=True)
fcs = work / 'master_fc.txt'
fcs.write_text(';\n'.join(fc))

inputs += ['-stream_loop', '-1', '-i', BED]
out = ASM / outname
cmd = ['ffmpeg', '-y', '-loglevel', 'error'] + inputs + [
    '-filter_complex_script', str(fcs), '-map', '[vout]', '-map', '[aout]',
    '-c:v', 'libx264', '-crf', '18', '-preset', 'medium', '-r', '24',
    '-video_track_timescale', '24000', '-c:a', 'aac', '-b:a', '192k', str(out)]
print(f'total: {t_acc:.2f}s across {len(parts)} segments')
r = subprocess.run(cmd, capture_output=True, text=True)
if r.returncode != 0:
    print(r.stderr[-2500:]); sys.exit(1)
print('OK ->', out)
