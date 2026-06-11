#!/usr/bin/env python3
"""Final release stitch: intro + master + outro with 0.42s crossfades,
then recompute the chapter map (shifted by intro length minus crossfade)
and patch RELEASE.md in place.

Run after branding/abn_intro.mp4 and branding/abn_outro.mp4 exist.
"""
import re, subprocess, sys
from pathlib import Path

ASM = Path('/Users/risingtidesdev/dev/content-posting-lab/yt-pipeline/src/animations/assembly')
BR = ASM.parent / 'branding'
XF = 0.42

INTRO = BR / 'abn_intro.mp4'
MASTER = ASM / 'ep_154d0920_master.mp4'
OUTRO = BR / 'abn_outro.mp4'
OUT = ASM / 'ep_154d0920_final.mp4'

# master-relative chapter joints (from build_master run, RELEASE.md table)
CHAPTERS = [
    (0.00, 'VSCode GitHub OAuth Token Hack — One Click Exploit Explained'),
    (98.18, "MAI Code 1 Flash: Microsoft's 32K-Token, 100ms AI Coding Model"),
    (182.16, 'Skip Figma with Claude — AI Design Prototypes in Real Code'),
    (250.79, 'Did Claude Increase Bugs in rsync? The Data Says No'),
    (325.47, 'Harness Engineering: OpenAI Codex Writes 1M Lines, 10x Faster'),
    (410.21, "Anthropic's Open-Source Claude Security Scanner (C/C++ Bugs)"),
    (488.64, "NSA Using Anthropic's Mythos AI for Cyber Attacks — 75B Params"),
    (568.57, 'Microsoft Scout Autonomous Agent: 30% Faster, Built on OpenClaw'),
    (626.66, 'If ChatGPT Has Human-Like Reasoning, So Does Age of Empires II'),
    (708.69, 'Nvidia GPU VRAM as Linux Swap Space — RTX 3070 7GB Pool Trick'),
]

def dur(p):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'csv=p=0', str(p)], capture_output=True, text=True)
    return float(r.stdout.strip())

for f in (INTRO, MASTER, OUTRO):
    if not f.exists():
        print(f'missing: {f}'); sys.exit(1)

d_in, d_m, d_out = dur(INTRO), dur(MASTER), dur(OUTRO)
off1 = d_in - XF                 # intro -> master joint
off2 = off1 + d_m - XF           # master -> outro joint
total = off2 + d_out

fc = [
    f"[0:v]fps=24,settb=AVTB,format=yuv420p[v0]",
    f"[1:v]fps=24,settb=AVTB,format=yuv420p[v1]",
    f"[2:v]fps=24,settb=AVTB,format=yuv420p[v2]",
    f"[0:a]aresample=48000,aformat=channel_layouts=stereo[a0]",
    f"[1:a]aresample=48000,aformat=channel_layouts=stereo[a1]",
    f"[2:a]aresample=48000,aformat=channel_layouts=stereo[a2]",
    f"[v0][v1]xfade=transition=fade:duration={XF}:offset={off1:.4f}[xv1]",
    f"[xv1][v2]xfade=transition=fade:duration={XF}:offset={off2:.4f}[xv2]",
    f"[a0][a1]acrossfade=d={XF}:c1=tri:c2=tri[xa1]",
    f"[xa1][a2]acrossfade=d={XF}:c1=tri:c2=tri[xa2]",
    "[xv2]format=yuv420p[vout]",
    "[xa2]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]",
]
fcs = ASM / 'work_master' / 'final_fc.txt'
fcs.parent.mkdir(exist_ok=True)
fcs.write_text(';\n'.join(fc))

cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-i', str(INTRO), '-i', str(MASTER), '-i', str(OUTRO),
       '-filter_complex_script', str(fcs), '-map', '[vout]', '-map', '[aout]',
       '-c:v', 'libx264', '-crf', '18', '-preset', 'medium', '-r', '24',
       '-video_track_timescale', '24000', '-c:a', 'aac', '-b:a', '192k', str(OUT)]
print(f'stitch: intro {d_in:.2f}s + master {d_m:.2f}s + outro {d_out:.2f}s -> {total:.2f}s')
r = subprocess.run(cmd, capture_output=True, text=True)
if r.returncode != 0:
    print(r.stderr[-2000:]); sys.exit(1)

shift = off1  # chapter 0 moves to the intro->master joint... but YT chapter 1 must be 0:00
print('OK ->', OUT)

# chapter map: YouTube requires first chapter at 0:00 — that becomes "Intro".
def ts(t):
    t = int(round(t)); return f'{t // 60}:{t % 60:02d}'

lines = ['0:00 Intro']
for t, title in CHAPTERS:
    lines.append(f'{ts(t + shift if t > 0 else shift)} {title}')
chapter_block = '\n'.join(lines)
print(chapter_block)

rel = ASM / 'RELEASE.md'
txt = rel.read_text()
txt = re.sub(r'Chapters:\n(?:.+\n)+?\n', 'Chapters:\n' + chapter_block + '\n\n', txt, count=1)
txt = txt.replace('assembly/ep_154d0920_master.mp4', 'assembly/ep_154d0920_final.mp4')
rel.write_text(txt)
print('RELEASE.md updated (final file + shifted chapters)')
