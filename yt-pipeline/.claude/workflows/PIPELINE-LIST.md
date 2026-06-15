# List-Episode Pipeline (variant)

For countdown/listicle episodes ("10 Open Source Tools..."), visuals lean on REAL
page-nav screen recordings of the projects covered, framed over the darkField,
interleaved with short CSS kinetic beats. First used: ep3.

## Flow

1. **`0-research-pipeline.js`** with args `{slug, brief, facets, structure}` —
   structure specifies countdown order (10→1), per-item targetWords 100-125.
   Output: outline ledger where each tool segment carries the repo URL in sources.
2. **TTS + word grid** — `build_vo.py` pattern (per-segment wavs; grid == audio time).
3. **`5-gh-capture.js`** with args `{tools: [{rank, name, url, extraUrl?}], outDir}` —
   real playwright nav recordings per repo via `tools/gh-capture/capture_nav.cjs`
   (dark mode, header hold → eased README scroll → end hold, 1920x1080/24fps).
   Verified: identity visible, dark frames, no error/captcha pages.
4. **CSS beat fleet** (clip-fleet variant) — per tool segment, builders make SHORT
   beats only (rank slam intro card ~4-6s, killer-number payoff ~4-6s), VO-aligned
   single pass; the footage occupies the middle window. Builders receive the
   footage file + its window so beats hand off cleanly.
5. **Compile** — build script places per segment: [intro beat][FRAMED footage]
   [payoff beat] over the segment wav. Framed treatment (uniform, deterministic
   ffmpeg): footage scaled to 78% over darkField, 1px hairline border, deep
   shadow, mono source chip (github.com/...) bottom-left, rank badge top-left.
   Then: crossfade body chain + bed floor 0.08 + duck + loudnorm −14 (the law),
   intro/outro stitch, timescale 24000 everywhere.
6. **Council + freeze gate** — unchanged from the standard pipeline. NOTE: real
   footage naturally passes freezedetect (scroll motion); the gate mainly guards
   the CSS beats and holds.

## Laws inherited (do not relearn)

- freezedetect −50dB/4s must return 0 events on every deliverable
- ambient motion lives inside seek(t), never one-shot CSS animations
- container-to-content < 1s; no >40%-void frames
- verbatim copy from the script field; words[] is timing truth
- per-stream verification + frame extractions after every stitch
- light-mode footage is a defect (strobes in the dark episode)
