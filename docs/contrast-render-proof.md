# Dossier colour preview render proof

Date: 2026-10-01  
Host: `risingtidess-macbook-pro` (`maj`)  
Branch change: `fix/contrast-matches-preview`

## Contract under proof

The production colour path must render the Dossier preview's ordered CSS operations, including a real contrast pivot at normalized RGB `0.5`. The live `lovenightdrives` look was used for all three proofs:

- brightness: `0.7`
- contrast: `1.95`
- saturation: `1.65`

Each real master was clipped at 2 seconds, rendered for one second through `run_color_correct` with the closed `tiktok_delivery_v1` H.264/yuv420p preset, and decoded back to RGB. The corresponding source frame was decoded to RGB and evaluated with the same ordered CSS math: brightness, contrast around `0.5`, then BT.709 saturation, with each CSS filter stage clamped to normalized RGB.

The acceptance tolerance was **mean absolute error (MAE) <= 1.0 and p99 absolute error <= 5 per 8-bit RGB channel**. This permits delivery-codec and 4:2:0 chroma rounding while requiring the posted frame's overall colour result to match the preview. Maximum error is retained as a diagnostic because isolated high-contrast/chroma edges are expected to be outliers after H.264 chroma subsampling.

## Results

- `/Users/risingtides/work/nc/master-stay_inmy.lane.mp4` (180,470,506 source bytes): 6,220,800 channel samples; MAE `0.111`; p95 `0`; p99 `3`; max `61`; **PASS**.
- `/Users/risingtides/work/nc/master-roma.roundtheworld.mp4` (180,364,764 source bytes): 6,220,800 channel samples; MAE `0.128`; p95 `0`; p99 `4`; max `58`; **PASS**.
- `/Users/risingtides/work/nc/master-yearningcentralized.mp4` (179,910,874 source bytes): 6,220,800 channel samples; MAE `0.214`; p95 `2`; p99 `4`; max `41`; **PASS**.

All three production-path renders completed without a `colorchannelmixer` initialization error. In particular, the former composed red-from-red coefficient of approximately `2.06` is no longer sent as one illegal coefficient: the look is represented as legal alpha-bearing stages, and the contrast stage applies offsets of `-0.475` through `ra`, `ga`, and `ba`.

## Intended rollout effect

This deliberately changes the posted colour of approximately 30 pages to the look already shown by their Dossier CSS previews. It is a parity correction, not a new creative treatment.

## After deploy

`scripts/contrast_render_proof.py` re-runs the same proof against the **live**
Lab colour endpoint instead of the local `run_color_correct` call, so the lead
can confirm the deployed service still reproduces the CSS preview.

It posts the look through `POST /api/video/color-correct` (the Lab's synchronous
single-video colour endpoint, which routes through the same shared
`services.ffmpeg.run_color_correct(..., scale=None)` colour math the Worker's
render uses in `routers/control_plane.py`), decodes the returned frame and the
source frame to RGB, and compares the output to the CSS preview math
(MAE <= 1.0 and p99 <= 5; else exit 1). The API key is read from `APP_API_KEY`
(the env var the Lab's `/api/*` key middleware checks) and is never printed.

The Lab's colour endpoints resolve server-side project paths, so first place a
1-2 s clip of the master at `<project>/videos/<name>` on the Lab. Extract the
clip with the same frame-accurate command the script uses (so the server's input
bytes match the local source frame), then run:

```bash
# 1. Make the clip (same command the script runs internally).
ffmpeg -ss 2.0 -i /path/to/master.mp4 -t 1.0 \
  -c:v libx264 -crf 18 -pix_fmt yuv420p -an clip.mp4

# 2. Place clip.mp4 at <project>/videos/<name> on the Lab, then:
APP_API_KEY=... python scripts/contrast_render_proof.py --lab-url <deployed Lab URL> \
  --master /path/to/master.mp4 --at 2.0 --project <project> --remote-path <name>.mp4
```

To check the comparison offline (no network, no `APP_API_KEY`), run the same
comparison through `services.ffmpeg.run_color_correct` in-process:

```bash
python scripts/contrast_render_proof.py --local --master /path/to/master.mp4 --at 2.0
```

The default look is the live `lovenightdrives` look (`brightness=0.7,
contrast=1.95, saturation=1.65`); any Dossier look is accepted via `--look`.
