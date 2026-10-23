"""Replay compiling timing mutants on a disposable checkout.

Usage: python scripts/verify_beat_slideshow_redproof.py [--syzygy /path/to/worktree]
Do not run while another process is reading the same source files.
"""
import argparse
from pathlib import Path
import subprocess
import sys


def prove(path, before, after, command, cwd, expected):
    original=path.read_text()
    if original.count(before)!=1:
        raise RuntimeError('mutant source no longer matches exactly once')
    try:
        path.write_text(original.replace(before,after))
        result=subprocess.run(command,cwd=cwd,text=True,capture_output=True,timeout=60)
        print(result.stdout)
        if result.returncode!=1 or expected not in result.stdout:
            raise RuntimeError('mutant did not fail its behavioral pin')
    finally:
        path.write_text(original)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--syzygy',type=Path);args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    prove(root/'services/beat_slideshow.py','step = 60 / bpm * BEATS[NOTES.index(note)]','step = 60 / bpm',
          [sys.executable,'-m','pytest','-q','tests/test_beat_slideshow.py','-k','actual_photo_changes'],root,'3 failed, 1 passed')
    if args.syzygy:
        remotion=args.syzygy.resolve()/'remotion'
        prove(remotion/'src/photoSlots.js','len = end - cursor;','len = Math.max(4, Math.round(sec * fps));',
              ['node','--test','src/photoSlots.test.mjs'],remotion,'fail 3')
