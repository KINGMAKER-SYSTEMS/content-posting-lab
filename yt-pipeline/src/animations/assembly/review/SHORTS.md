# SHORTS.md — Shorts/Reels Crop & Sub-Clip Review

Reviewer: **Shorts/Reels Editor** (lens: vertical-crop survival + mobile readability + 3s hook)
Council seat 1 of 4. All 10 ABN aligned cuts reviewed.

## Method / what the numbers mean

- Source cuts are **1920×1080, 24fps** (s0…s9).
- Target delivery is **9:16 vertical**. A *static center crop* from 1080p keeps a
  **608px-wide column, x = 656 → 1264** (1080 × 9/16 = 607.5px). Everything left of 656
  and right of 1264 is **discarded** — 656px gone from each side.
- "Crop-safe" = the hero hook + the payoff/stat both fall inside x 656–1264.
- All frames extracted with ffmpeg; full-frame guides (red band = keep zone) live in
  `review/frames/`, the actual center-crop renders in `review/crop9x16/`, contact sheets
  in `review/sheets/`.
- In/out timestamps below are on the **aligned-cut timeline** (each `sN_aligned.mp4` starts at 0).

## Hard-fail rule for this lens

Fail a cut ONLY for (a) type unreadable at mobile size, or (b) a hook that takes
**>3s to land**. Note: in 9:16 the hook must land *in the cropped frame*. Most of these
clips were composed left-weighted / split-screen for 16:9, so the center crop guts the
hero word and the hook does not land at all — that is the dominant failure here.
Legibility of the *surviving* type is uniformly good (large, high-contrast): no cut fails
purely on mobile readability. The single low-contrast small element flagged is s8's gray
"reach expert level strategy" caption.

---

## Crop-safety verdict table

| Cut | Best 9:16 sub-clip (in→out) | Len | Hook lands in crop? | Crop verdict |
|---|---|---|---|---|
| s0 | 00:00 → 00:23.0 (hook→"keys out") | 23.0s | **NO** — "BUILD\|EXPLOIT" split → crop shows "D \| EX" | **REFRAME** |
| s1 | 00:00 → 00:24.0 (title→FLASH/<100) | 24.0s | **NO** — title left-anchored, center black to ~2.5s | **REFRAME** |
| s2 | 00:00 → 00:31.0 (SKIP FIGMA→50 tweaks) | 31.0s | **NO** — "SKIP FIGMA" left → crop shows "P / A" | **REFRAME** |
| s3 | 00:00 → 00:31.0 (DID CLAUDE→stats verdict) | 31.0s | Borderline — reads "(C)LAUDE…BUGS?" | **SHIP + note** |
| s4 | 00:00 → 00:31.0 (hook→1,000,000) | 31.0s | **NO** — "WROTE/EVERY LINE" → "TE / RY LINE"; 1,000,000 → "0,000" | **REFRAME** |
| s5 | 00:00 → 00:31.0 (drop→WEEKS OF WORK) | 31.0s | Borderline — reads "JUST DROPPED…OPEN SOURCE…CLAUDE AI" | **SHIP + note** |
| s6 | 00:06.2 → 00:30.0 (75B→"safer/keys") | 23.8s | **NO** — "THE NSA" → "A"; "75" → "5" | **REFRAME** |
| s7 | 00:00 → 00:18.0 (DROPPED SCOUT→30% FASTER) | 18.0s | Borderline — "DROPPED…(SC)OUT," head clips | **SHIP + note** |
| s8 | 00:00 → 00:24.6 (175B→token/state split) | 24.6s | **NO** — center black to 3s, "175B" → "n parameters"; split-stack gutted | **REFRAME** |
| s9 | 00:00 → 00:27.3 (NVIDIA hook→16GB) | 27.3s | **NO** — "USE YOUR NVIDIA GPU" → "OUR / A / GPU / P" | **REFRAME** |

---

## Per-cut detail (in/out + concrete reframe instructions)

### s0 — 1-Click GitHub Token Stealing — **FIX**
- **Sub-clip:** in **00:00.0** → out **00:23.0**. Hook ("BUILD vs EXPLOIT / DOUBLE-EDGED
  SWORD", Video4 0–9.18) + payoff "boom, your keys are out" lands ~21s ("ONE CLICK", Video1).
  Self-contained, 23s.
- **Crop:** FAIL. The hook is a hard vertical split — "BUILD" (cyan) lives left of x=656,
  "EXPLOIT" (red) lives right of x=1264. Center crop at t=0.5–2.5 shows only inner edges
  "D | EX" + the center divider. At t=5 the crop reads "D | EX … — DOUBLE-EDG". Hook never lands.
- **Smallest text:** news-wire ticker ("into a double-edged sword •") IS legible at mobile — not a fail reason.
- **Fix a compiler can execute:** for the static-crop deliverable, re-render Video4's hero
  words **center-stacked** (BUILD over EXPLOIT, both centered on x=960) instead of L/R split,
  OR ship this cut with an **animated reframe**: hold crop-center on the cyan "BUILD" (x≈300,
  pan window centered ~x=360) for 0–1.5s, whip-pan to "EXPLOIT" (x≈1500, window ~x=1560) at
  1.5–3s. Same treatment for the t=21 "ONE CLICK" payoff (currently "ONE" left / "CLICK" right →
  center it). Until then, hook does not land in 9:16.

### s1 — MAI-Code-1-Flash — **FIX**
- **Sub-clip:** in **00:00.0** → out **00:24.0**. Title hook + "<100ms / FLASH" stat payoff (~t=11–23).
- **Crop:** FAIL. "MAI CODE 1." title is anchored in the left third; center crop is
  **essentially black through t≈2.5** (verified `crop9x16/s1_t002p50_crop.png` — only the
  radial glow, no text). Hook does not land within 3s in 9:16.
- **Note (good):** the body stat monument is fine — `crop9x16/s1_t011p40_crop_native.png`
  shows "21K TOKENS / reads up to 32,000 tokens at once" fully centered and crisply legible
  at mobile. So the cut's *interior* is crop-safe; only the opening title is mis-anchored.
- **Fix:** re-center the "MAI CODE 1. FLASH" title block onto x=960 for the first ~6s so the
  hook occupies the keep band. No legibility change needed elsewhere.

### s2 — I design with Claude more than Figma — **FIX**
- **Sub-clip:** in **00:00.0** → out **00:31.0**. "SKIP FIGMA" hook + "50 tweaks / PROTOTYPE
  EVOLVES" payoff (Video2 region ~24.6–31.8).
- **Crop:** FAIL. "SKIP" (white) and "FIGMA" (red) are left-anchored; center crop at t=2.5
  (`crop9x16/s2_t002p50_crop.png`) reads "LD / P / A" — i.e. tails of COULD / SKIP / FIGMA.
  Hook word "SKIP FIGMA" does not read.
- **Fix:** re-anchor the "SKIP / FIGMA" slam to center (x=960). The "BUILD REAL PROTOTYPES"
  payoff and the "33→50" counter are already near-center and survive — leave them.

### s3 — Did Claude increase bugs in rsync? — **SHIP (with crop note)**
- **Sub-clip:** in **00:00.0** → out **00:31.0**. "DID CLAUDE … INCREASE BUGS?" hook +
  "Run the stats / NO CLEAR SIGN" verdict (~20–31s).
- **Crop:** PASS-borderline. "CLAUDE" and "BUGS?" sit center; crop reads "(C)LAUDE … (INCRE)ASE
  BUGS?" — the leading "DID" and the "C" clip, but the hook still communicates. Hook lands < 3s.
- **Crop note (not a fail):** the stats **terminal panel (Video4, ~t=21) is left-anchored** —
  `crop9x16/s3_t021p30_crop.png` shows command prefixes (`--metric`, `--is-post…`) sliced off
  the left; survives as "—span all-releases / bugs / LOC", still readable. Nudge that terminal
  +120px right (toward x=960) if a polish pass happens; ships as-is.
- **Mobile readability:** all surviving type legible. Ship.

### s4 — Harness engineering (Codex) — **FIX**
- **Sub-clip:** in **00:00.0** → out **00:31.0**. "WHAT IF AI WROTE EVERY LINE?" hook +
  "1,000,000 LINES" stat monument (~t=22–30).
- **Crop:** FAIL on two counts. (1) Hook left-anchored: t=2.5 crop
  (`crop9x16/s4_t002p50_crop.png`) reads "AI / TE / RY LINE" — "WROTE / EVERY LINE" clipped to
  fragments. (2) The hero stat "1,000,000" is left-anchored → center crop reads "0,000"
  (`sheets/s4_crop.png`), killing the million-line punch.
- **Fix:** center the hook stack ("WROTE / EVERY LINE") and the "1,000,000 / LINES OF
  AI-WRITTEN CODE" monument on x=960. The right-side "1,500 PRs" cell already falls outside —
  acceptable as secondary. Type is large/legible; this is purely a horizontal-anchor fix.

### s5 — Anthropic open-source vuln framework — **SHIP (with crop note)**
- **Sub-clip:** in **00:00.0** → out **00:31.0**. "ANTHROPIC JUST DROPPED / OPEN SOURCE…
  CLAUDE AI" hook + "5 / WEEKS OF WORK" payoff (~t=40 lands just past 31; tighten to 35 if you
  want the full WEEKS payoff: in 00:00 → out **00:42.0** = 42s, still in-window).
- **Crop:** PASS-borderline. t=2.5 crop (`crop9x16/s5_t002p50_crop.png`) reads "JUST DROPPED /
  (OPEN)SOURCE / USES CLAUDE AI TO" — leading "OPEN"/"ANTHROPIC" clip but the hook lands and
  reads. Best hook survival in the batch.
- **Crop note (not a fail):** the 5-step pipeline nodes (EXPLORES/HUNTS/VERIFIES…) are small
  bottom-LEFT panels and clip out of the crop. If those steps matter, re-stack them vertically
  centered; otherwise the "WEEKS OF WORK" payoff carries the cut. Ship.

### s6 — NSA using Anthropic's Mythos — **FIX**
- **Sub-clip:** in **00:06.2** → out **00:30.0** (start at the "75B parameters" stat to dodge
  the dead redaction-bar hook). ~23.8s.
- **Crop:** FAIL. (1) Hook "THE NSA" is top-left → crop shows lone "A" + white redaction bars,
  reveal payoff (MYTHOS AI / CYBER ATTACKS) sits outside band. (2) Hero stat "75" is
  left-anchored → t=9 crop (`crop9x16/s6_t009p00_crop.png`) reads "5", and the verdict block
  "WHO HOLDS THE KEYS." clips every line to "OLDS THE" (`crop9x16/s6_t080p00_crop.png`).
- **Fix:** center the "75" counter and "A HUGE LANGUAGE MODEL" on x=960; re-anchor the verbatim
  verdict block (incl. "WHO HOLDS THE KEYS.") to center. Type is large/legible — anchor only.

### s7 — Microsoft Scout (OpenClaw) — **SHIP (with crop note)**
- **Sub-clip:** in **00:00.0** → out **00:18.0**. "MICROSOFT JUST DROPPED SCOUT," hook +
  "30% FASTER" stat (~t=9–12). 18s, tight and self-contained.
- **Crop:** PASS-borderline. t=2.5 crop (`crop9x16/s7_t002p50_crop.png`) reads "(MICRO)SOFT /
  (DR)OPPED / (SC)OUT," — the product name SCOUT loses its "SC" head but the red "OUT," + ghost
  outline make it inferable and the hook reads "MICROSOFT DROPPED [SC]OUT". Lands < 3s.
- **Crop note (not a fail):** "30%" clips its leading "3" → reads "%/FASTER"; if the 30% stat is
  load-bearing, center that counter. Ships on the strength of the SCOUT hook.
- **Mobile readability:** type is large and crisp. Ship.

### s8 — LLMs vs Age of Empires II — **FIX**
- **Sub-clip:** in **00:00.0** → out **00:24.6**. "175B parameters" hook + token-vs-game-state
  comparison (Video4 ~t=10.9–24.6).
- **Crop:** FAIL on multiple counts. (1) Hook: center crop is **black until ~t=3**
  (`crop9x16/s8_t003p00_crop.png` shows only "n parameters") — the "175B" hero is left-anchored;
  hook does not land in 3s. (2) The token↔game-state **split-stack comparison is gutted**
  (`crop9x16/s8_t055p00_crop.png`): left column shows "ens", right cards clip their labels
  (GAME STA…/STRATEGY/AGE OF E…) off the right edge — the equivalence payoff is unreadable.
- **Mobile readability flag:** the gray caption "reach expert level strategy" is **low-contrast
  on dark** — borderline at mobile; bump to white or +contrast.
- **Note:** this cut also carries the most dark freeze-gap stretches (NOTES: freezes at
  24.6–43.96 and 47.26–67), so the 9:16 has long low-information spans. Recommend tightening the
  sub-clip to 0–24.6 and centering the 175B hook + collapsing the two comparison columns into a
  single centered stack.

### s9 — Nvidia VRAM as Linux swap — **FIX**
- **Sub-clip:** in **00:00.0** → out **00:27.3**. "USE YOUR NVIDIA GPU AS SWAP" hook +
  "16GB" counter (Video2 ~t=18.7–27.3).
- **Crop:** FAIL. Hook left-anchored: t=1.46 crop (`crop9x16/s9_t001p46_crop.png`) reads
  "OUR / A / GPU / P" — "USE YOUR NVIDIA … AS SWAP" reduced to fragments, right half of frame
  black. The subject word "NVIDIA" is a lone red "A". Hook does not land.
- **Crop note:** the CUDA terminal (Video3, ~t=35.7, `crop9x16/s9_t035p70_crop.png`) is
  left-anchored — command names slice off left, survives as "…and GPU VRAM / 7168 MiB". The
  Warhol-grid "16GB" counter reads as edge fragments in crop.
- **Fix:** center the hook stack "USE YOUR / NVIDIA / GPU / AS SWAP" on x=960 and center the
  "16GB" counter; nudge the terminal toward center. Type is large/legible — anchor only.

---

## Batch state (this lens)

7 of 10 cuts **fail crop-safety**: s0, s1, s2, s4, s6, s8, s9. Root cause is systemic — these
were composed left-weighted / hard-split-screen for 16:9, so a static center 9:16 crop slices
the hero hook and the load-bearing stat (BUILD|EXPLOIT, MAI-title, SKIP|FIGMA, 1,000,000→0,000,
75→5, 175B→"n parameters", NVIDIA→"A"). The fix is the same family of edits everywhere: re-anchor
hero hook + hero stat onto x=960 (center), or ship an animated reframe (pan to each side of a
split). Three cuts ship as-is for vertical: **s3, s5, s7** — their hooks survive the center crop
well enough to read (CLAUDE…BUGS?, JUST DROPPED…OPEN SOURCE, DROPPED…SCOUT). Mobile legibility is
NOT the batch problem — every surviving glyph is large and high-contrast; the only legibility nit
is s8's gray "reach expert level strategy" caption.
