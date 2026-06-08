# AgenticBuilderNews — Format-Type & Retention Architecture (Design v1)

> **Status:** DESIGN — not yet built. John's call: "design first, build after."
> **Goal:** stop the channel producing slop; make it a format-driven, retention-engineered,
> RPM-revenue machine. Every choice below is grounded in sourced 2025–2026 YouTube research
> (see `## Sources`), NOT training-data assumptions — that training-data riffing is the exact
> failure mode that produced the academic-drift slop.

---

## 0. Why the current factory produces slop (root cause)

The v1 factory is a **single monolithic line** (`produce_one_episode`, ~480 lines) that stamps
out every video the same way regardless of format:

- **Captions are formulaic** because there's ONE hardcoded lower-third template
  (`abn_factory.py:1423` — `startSec:0.5, durationSec:4.0, headline:title[:72]`) used for every
  segment of every video.
- **Visuals are slop** because there's ONE dumb fallback chain (`ui OR screenshot OR card OR logo`)
  — for concept stories it screenshots the researched blog and scrolls a wall of text.
- **Topics drift academic** because experts riff from training data; no real research, no
  competitor signal, no retention lens.
- **No format owns its own structure** — roundup/deepdive/lore differ only in segment count and a
  couple of prompt tweaks; pacing, captions, shot selection, and ad-beat placement are identical.

The fix is not more patches. It's: **formats become first-class types**, each owning its structure,
pacing, caption style, shot strategy, and ad-beat placement — and the script's *content* drives a
**catalog of shot types** per scene, instead of one fallback chain.

---

## 1. The four format types (the retention spine)

Grounded in how real faceless AI/tech channels structure for retention + the mid-roll RPM math.
**Money formats target 10–14 min for 2 mid-rolls** (mid-roll requires 8:00 hard minimum; RPM
roughly 2–3× vs a sub-8-min video).

| Format | Model | Length | Mid-rolls | Structure | Primary visual strategy |
|---|---|---|---|---|---|
| **PULSE** (news roundup) | "This week in AI" | 10–12 min | 2 (~45%, ~75%) | 5–7 segmented stories, each a 60–90s beat with its own micro-hook → retention graph re-engages at every segment boundary | screen-rec of the actual tool/announcement + motion-graphic story cards; NEVER scroll a blog |
| **DEEPDIVE** (single tool) | Fireship-scaled-up | 8–10 min | 1 (~50%) | result-first hook → what it is → how it works → verdict vs the incumbent | annotated screen recording + diagram of the mechanism + code walkthrough |
| **LORE** ("rise of X") | ColdFusion | 12–18 min | 2–3 (on resolution beats) | 6-beat documentary arc, calm single-narrator VO + score | custom motion graphics + stock + archival screenshots; cinematic holds |
| **SHORT** (top-of-funnel) | Fireship 100s | ≤100s | 0 | one concept, 10–15 cuts/min | fast-cut code/graphics; drives the long-form |

**LORE is the highest-RPM format** (longest, most mid-roll inventory, narrative tension to place ads
around). **SHORT carries no ads** — it's funnel, not revenue; each loop now counts as a view
(March 2025) so it's cheap reach.

Each format is a **config object**, not a code branch. See §4.

---

## 2. Retention mechanics every format must encode (hard rules)

These become enforced parameters in the format config, the same way the 10-min gate is enforced:

- **Hook window = first 7 seconds** (tightened from 15–30s). 0–5s attention grab (result/tease/
  question), 5–15s clarify the promise, 15–30s open a loop. A pattern interrupt in the first 5s is
  worth ~+23% retention; an open loop ~+32% watch time; on-screen text in the hook ~+18%.
- **55% of viewers leave by 60s.** Holding ~70% retention at 0:30 triggers algorithmic promotion.
  → the cold-open is the single most important asset; it gets its own quality gate.
- **Cut cadence by audience** (AI-builder audience skews 25+): hold shots **20–40s**, cut on topic
  shift — do NOT Fireship-cut a deepdive. PULSE uses the "contrast pattern": calm 15–25s cuts with
  a 5–10s quick-burst every 2–3 min.
- **Re-engage every 2–3 min:** restate the core question/stakes (a "narrative loop") to drag
  viewers past minute 8.
- **Ad-beat placement:** script DELIBERATE resolution/"breath" beats at ~45% and ~75% where a
  mid-roll can land after a satisfying moment (YouTube's May-2025 auto-placement seeks natural
  breaks — so give it good ones). Never place an ad in a climax.
- **Music ducking:** −20 to −25 dB under calm narration, −8 to −12 dB under energetic sequences
  (v1 already sidechain-ducks; the *target* becomes format-aware).

---

## 3. Captions: kill the formulaic lower-third

The current single 4-second title lower-third on every segment is the "formulaic" John flagged.
Replace with a **format-aware caption strategy**:

- **Phrase-level, not word-by-word.** Word-by-word bounce reads frantic; caption the meaning-bearing
  phrase (the outcome, the number, the warning). Captioned videos show ~12–15% higher completion.
- **Don't caption everything** on long-form. Emphasize key phrases; let the VO carry the rest.
- **One font, channel-wide.** Never switch fonts/styles mid-video (breaks reading flow, lowers
  watch time). One bold sans (the brand display font), kept.
- **Specs:** ≤42 chars/line (~5–7 words), max 2 lines, contrast ≥4.5:1, appear 0.1–0.3s before
  audio.
- **Per-format caption role:**
  - PULSE: story-label lower-third at each segment start (the micro-hook) + phrase captions.
  - DEEPDIVE: term/spec callouts synced to the mechanism explanation.
  - LORE: sparse — name/date/place supers only; let the narration breathe (documentary register).
- **Caveat:** kinetic typography is a novelty bump that fades — optimize for durable clarity, not
  motion density.

This replaces the hardcoded `lowerThirds` formula with a `CaptionStrategy` chosen by format.

---

## 4. Meta-scenes → shot catalog (the visual rebuild)

This is John's model: **the script is deconstructed into meta-scenes by VO context, and each scene
declares what shot type it needs from a catalog** — instead of one fallback chain.

### 4a. Scene model
A script segment is broken into **scenes** (a scene ≈ one idea / one VO sentence-group, ~20–40s).
Each scene carries a `scene_role` derived from the VO content:

| scene_role | what the VO is doing | preferred shot types (in order) |
|---|---|---|
| HOOK | the 0–7s grab | bold number/stat card, fast tool montage, "vs" face-off card |
| CLAIM | stating what happened | tool screen-rec, announcement screenshot (brief, ≤3s), logo reveal |
| MECHANISM | how it works | motion-graphic diagram, annotated screen-rec, code walkthrough |
| NUMBER | a stat/benchmark | data-viz / chart card, big-number kinetic card |
| COMPARISON | X vs Y | split-screen, vs card, side-by-side demo |
| TAKE | the opinion/verdict | host-replacement b-roll + pull-quote card |
| TRANSITION / ad-beat | breath before next | brand motion b-roll (the cached library), logo sting |

### 4b. Shot catalog
A registry of shot **generators**, each producing a clip for a scene. The existing v1 functions
become catalog entries:

- `screen_recording` (`capture_sync`) — for real tools with a UI. **Capped at ~3s of any one
  scroll**; never the whole segment.
- `diagram` — NEW: generate a clean motion-graphic of the mechanism (the gap that makes concept
  stories slop today).
- `code_walkthrough` (`_real_demo` / `_code_demo`) — real repo or honest pseudocode.
- `data_card` / `number_card` / `quote_card` — designed ImageMagick cards (real fonts, no AI text).
- `brand_broll` — the cached abstract library (already built; `broll_library/`).
- `title_card` (`_card`) — designed, for HOOK/TRANSITION.

**Rule:** the screenshot-the-blog path is DEMOTED to a ≤3s "source" cutaway, never the primary
visual. A scene with no good real visual falls back to a designed card over brand b-roll — a
designed explainer look, not a scrolled article. (This is the "inauthentic content" demonetization
risk too — template-churn + scraped-text walls are exactly what the July-2025 policy targets.)

### 4c. Who builds it
A **visual-director stage** takes the scripted scenes + their roles and assembles a varied shot
list per scene from the catalog, honoring the format's cut cadence. This is where "the visual team
aggregates a catalog and stacks varied assets" lives. It runs a **self-review** (the OCR slop gate
already built, extended: also reject a scene that is >X% scrolled-text screenshot).

---

## 5. Research that doesn't riff from training data

The academia-drift root cause: experts generate from training data. Fix the research layer so the
script team is fed **real, current material**:

- **Real fetch, not recall:** the research stage must pull the actual source (the repo README, the
  release notes, the announcement) and competitor coverage — not summarize from memory.
- **Competitor scraping (NEW):** regularly scrape the top channels in-niche (their titles,
  thumbnails, what's getting views) to inform topic + framing choice. Feeds the scout/score stage.
- **Format-aware research:** PULSE wants the broadly-searched angle; DEEPDIVE wants the mechanism +
  the honest verdict; LORE wants verifiable names/dates/events.
- The research brief structure was already retuned to WHAT / WHY-IT-MATTERS / HOW / NUMBERS / TAKE /
  HOOK — that stays; what changes is it must be grounded in fetched material, with sources cited,
  and a validator that rejects a brief with no real source.

---

## 6. A/B testing + the feedback flywheel

- **Titles + thumbnails via YouTube Test & Compare** (title A/B went global Dec 2025; up to 3
  variants). **Winner = watch-time share, NOT CTR** — so generate variants that are accurate, not
  baity (bait wins clicks, loses the test, loses the algo). The titler/thumbnailer already produce
  multiple candidates; wire the top 3 into a test instead of picking one.
- **Retention is the master signal:** YouTube prefers 6% CTR + 50% AVD over 10% CTR + 20% retention.
  Design for AVD + session continuation ("what do they watch next"), not raw clicks.
- **The flywheel** (`abn_memory`) already records episodes + winning theses. Extend it to record
  per-format retention outcomes so the system learns which format/hook/length actually retains and
  weights future production toward winners. This is the "A/B test what works best" loop.

---

## 7. How this maps onto the typed v2 spine (build plan, later)

The v2 contracts (`factory/contracts/`) already have the stage machine. Format-type slots in cleanly:

1. **`FormatType` config objects** (new `factory/formats/`): one per format (PULSE/DEEPDIVE/LORE/
   SHORT), declaring length target, mid-roll beats, cut cadence, caption strategy, scene-role →
   shot-catalog preferences, research emphasis. `VideoFormat` enum already exists in `stages.py:34`.
2. **`score` stage** picks the format (it already outputs `ScoreOut.format`).
3. **`research` stage** fetches real material (+ competitor signal), format-aware.
4. **`script` stage** outputs scenes tagged with `scene_role` (extend `Segment` → `Scene`).
5. **NEW `visual-director` stage** (between assets + assemble): scenes → shot list from the catalog,
   with self-review.
6. **`assemble`** builds the timeline using the format's caption strategy + cut cadence.
7. **`review`** gates against the format's retention rules (hook strength, length floor, no-slop,
   ad-beats present).
8. **Publish** wires title/thumb A/B test; flywheel records retention per format.

Each stage keeps its hard contract + self-review. The monolith keeps running until v2 produces a
provably-better episode (strangler pattern — never stop the old meta until the new one is ready).

---

## 8. First build target (when we move to build)

Per John: **format types + retention structure first.** Smallest first slice that proves the
architecture without the full rebuild:

- Define the 4 `FormatType` configs.
- Implement the **scene-role tagging** in scripting + the **shot-catalog visual-director** for ONE
  format (PULSE — it's the daily driver and the segmented structure is the clearest retention win).
- Replace the formulaic caption with PULSE's caption strategy.
- Render one PULSE episode and put it in front of John to judge — voice, pacing, captions, visuals,
  all together — before extending to the other formats.

---

## Sources

YouTube Help (mid-roll 8-min rule; Test & Compare optimizes watch-time-share not CTR), vidIQ &
fluxnote & tubebuddy (mid-roll RPM math + May-2025 auto-placement), AIR Media-Tech (cut-cadence by
audience, contrast pattern), virvid & tubeanalytics & opus.pro (hook window, retention upliftsf,
caption specs), Grokipedia/Wikitia (Fireship & ColdFusion structures), Search Engine Journal (global
title A/B Dec 2025), YouTube policy (July-2025 "inauthentic content"). Uplift percentages are
directional (marketing-blog studies); the 8-min rule, watch-time-share metric, and policy changes
are first-party-anchored high-confidence facts.
