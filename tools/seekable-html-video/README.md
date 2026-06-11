# Seekable HTML Video — 16:9 production tool

Browser-rendered motion graphics → real MP4. Not AI video generation: every frame is
HTML/CSS/SVG sampled at an exact timestamp, so there's no drift, no prompt lottery.
Full spec: `docs/plans/2026-06-10-seekable-html-video-16x9-handoff.md`.

## Render

```bash
NODE_PATH=frontend/node_modules node tools/seekable-html-video/render_seekable.cjs \
  --input runs/my-video/index.html \
  --output runs/my-video/final-16x9.mp4 \
  --contact-sheet runs/my-video/review/contact_start_mid_end.jpg \
  --poster-time 7.5 --poster runs/my-video/review/poster.png \
  --cleanup-frames
```

Defaults: 1920×1080 @ 24fps (override with `--width/--height/--fps`). Needs ffmpeg and
playwright (already in `frontend/node_modules`).

## Composition contract

The HTML file must expose `window.duration` (seconds) and `window.seek(t)`, call
`window.seek(0)` on load, and derive EVERY visual state from `t` — no wall-clock,
no setInterval, no hover. Bundle fonts locally (`@font-face` with a relative path).

## Templates

- `templates/builder-news-explainer/` — canonical 16:9 sample. Two-zone layout
  (headline left, status cards/checklist right), progress bar, source line, ambient
  brand-tinted glows. Styled per the ABN design contract
  (`yt-pipeline/remotion/DESIGN.md`) with the real TikTok Sans TTFs — NOT Inter;
  the handoff doc's `"font": "Inter"` example predates the contract.

Hook rule baked into the sample: scene-0 wipes are back-dated so frame 0 already
shows the headline — a blank first frame fails QA and kills the hover preview.

## QA before delivery

Render with `--contact-sheet` and look at start/mid/end BEFORE polishing. Then:

```bash
ffprobe -v error -select_streams v:0 \
  -show_entries stream=width,height,r_frame_rate,duration,pix_fmt \
  -of default=noprint_wrappers=1 final-16x9.mp4
```

Expect 1920/1080, 24/1, the graph's duration, yuv420p.
