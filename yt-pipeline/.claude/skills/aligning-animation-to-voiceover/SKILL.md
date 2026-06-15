---
name: aligning-animation-to-voiceover
description: Use when creating or retiming seekable-HTML/CSS animation clips that must sync with narration — word timestamps, VO windows, script-rooted reveals, "align with the voiceover", retiming kinetic inserts, or replacing static cards with live clips in the ABN factory.
---

# Aligning Animation to Voiceover

## Overview

Animations earn their place by landing reveals ON the spoken word. The script's
word timestamps are the timing grid; the animation is choreographed onto it,
never the other way around. Battle-tested on the 50-video Ralph fleet
(2026-06-10, `yt-pipeline/src/animations/`).

## Source of truth

- Word grid: per-segment `wordTimestamps` (`[{w, s, e}]`, segment-relative
  seconds) from the episode timeline JSON, or `src/animations/segments.json`.
- Copy: VERBATIM script words only. Inventing on-screen copy = instant fail
  (baseline run fabricated "iframe_count: 47 active" — caught by audit).

## The contract (seekable-html-video renderer)

- `window.duration` + `window.seek(t)`; `seek(0)` on load; every visual is
  pure f(t). No wall-clock, no setInterval, no runtime randomness, no hover.
- CSS keyframes: FINITE `animation-duration` and `animation-iteration-count`
  (never `infinite`), `animation-fill-mode: both`, `animation-play-state:
  paused`, driven in `seek(t)` via `el.style.animationDelay = (-t + start)+'s'`.
- Exactly ONE `const P = {...};` block (factory refills it by regex). Beat
  times go in P so timing stays parameterizable.
- Fonts bundled via relative `@font-face` (TikTok Sans TTFs; never Inter).
- Frame 0 fully composed — clips hard-cut in; blank first frame fails QA.
- Style: `yt-pipeline/remotion/DESIGN.md` atoms + banned moves.

## Alignment method

1. **Window:** pick `[voStart, voEnd]` spanning the lines the clip visualizes.
   ≤0.5s lead-in, ≤1.0s tail. Sibling clips in one segment: disjoint windows,
   chronological order.
2. **Duration:** `window.duration = voEnd - voStart` exactly; rendered mp4
   must match within 0.06s (ffprobe).
3. **Beats:** every spoken on-screen word reveals within ±0.15s of
   `word.s - voStart`. Unspoken chrome/ambient times freely.
4. **Re-choreograph, don't stretch:** long windows breathe — stagger
   sub-reveals on their own word timestamps, keep ambient drift alive; no
   static dead frame >4s. Never linearly time-stretch a dense 12s piece.
5. **Prove it with frames:** for 2+ words (early + late):
   `ffmpeg -y -ss <tw+0.2> -i final.mp4 -frames:v 1 proof.png` — word visible;
   at `tw-0.8` — absent or entering. Reveal >0.4s off = fail. Independent
   review picks DIFFERENT words than the builder proved.

## Render + QA

```bash
cd <repo> && NODE_PATH=frontend/node_modules node \
  tools/seekable-html-video/render_seekable.cjs --input index.html \
  --output final.mp4 --fps 24 --contact-sheet contact.jpg --cleanup-frames
# expect 1920x1080 yuv420p 24/1; READ the contact sheet before calling it done
```

## Common mistakes (all observed in baseline)

| Mistake | Fix |
|---|---|
| Fixed 12s duration regardless of VO | duration = window, to the frame |
| Invented/paraphrased on-screen copy | verbatim words from the script only |
| Linear time-stretch of dense choreography | re-choreograph on the word grid |
| Blank frame 0 | back-date the first entrance |
| `infinite` iteration counts | finite count covering window.duration |
| Trusting "it rendered" | frame-extraction proof at word times |
