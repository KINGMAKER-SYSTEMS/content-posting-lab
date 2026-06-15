# PACKAGE — Workflows over Prompts (ep-workflows-over-prompts)

> Editor-in-chief packaging pass v2 — rebuilt against the FINAL s0–s8 scripts (the previous package
> targeted the retired "319 pages" cold open). Grounded against scripts/s0–s8.md, the Forge Signal
> thumbnail contract (brand/ABN-AGENT-BRANDING-BRIEF.md), and live search-behavior checks
> (see SEO grounding notes at bottom). Runtime ~11:20 @ 145 wpm (1,644 words); chapter timestamps
> are word-budget estimates — re-stamp against the final VO assembly before upload.

---

## 1. Title Options

**A (recommended):** `Fable 5 Runs for Days Unattended. The Model Is Not Why.`
The winning angle, upgraded from "hours" to "days" — that's Anthropic's actual launch claim (s0 quotes
it) and the bigger number is both more accurate and more arresting. Flat declarative + negation is the
curiosity engine; don't decorate it. Matches the live "run Claude Code autonomously/unattended" query
cluster while riding the day-old Fable 5 news cycle.

**B (search-forward):** `Why Claude Fable 5 Can Run for Days Without You (It's Not the Model)`
Front-loads the exact news-cycle entity "Claude Fable 5" (TechCrunch/VentureBeat/CNBC coverage all
title on that string) plus "Why" for question intent. Keeps the negation as parenthetical hook. Best
CTR-vs-search compromise if A underperforms in the first 48h.

**C (evergreen):** `Harness Engineering: Why AI Agents Run for Days (and Yours Dies in Minutes)`
"Harness engineering" is now a named, searched discipline — OpenAI coined it, InfoQ/Towards AI/multiple
2026 guides rank for it, and it's the literal subject of s2. Second clause adds the personal-stakes gap.
Day-30 swap candidate once the Fable 5 news wave fades.

---

## 2. Description

```
Harness engineering, not model weights: how Claude Fable 5 and Codex actually run for days unattended.

Anthropic's Fable 5 launch (June 9) claims days of autonomous work — but the autonomy verbs belong to
the harness, not the model: harness-induced variance in agent benchmarks measures 7.8x larger than
model-induced. We trace the receipts — OpenAI's million-line codebase with zero hand-written lines, the
Ralph loop all three major vendors just compiled into their products, the JSON-schema contracts that let
pipeline stages trust each other — and end with the bash wrapper + JSON ledger you can run this week.

Chapters:
0:00 The Workflow Engine Wearing a Model's Name
1:04 The Prompt Is Not the Program
2:19 OpenAI Productized the Loop (Codex)
3:39 Fable 5 Ships the Enforcement Primitives
4:59 /loop, /goal, and the Ralph Lineage
6:16 Schemas as Ledgers: Contracts Between Stages
7:35 The Scoreboard: Workflows vs the Best Prompts
8:53 Your First Locked Loop
10:10 Make It Legible and Enforceable

Sources:
https://www.anthropic.com/news/claude-fable-5-mythos-5
https://www.anthropic.com/research/building-effective-agents
https://www.infoq.com/news/2026/02/openai-harness-engineering-codex/
https://github.com/anthropics/claude-code/blob/main/plugins/ralph-wiggum/README.md
https://arxiv.org/abs/2503.13657
https://platform.claude.com/docs/en/build-with-claude/structured-outputs
```

Notes: first line is the strongest alt title (a concept-led restatement — does not duplicate the chosen
title, per packaging rule). Chapter names are the segment titles from scripts/s0–s8.md headers, trimmed
for the chapter rail. Timestamps computed from actual per-segment word counts
(154/181/194/194/186/190/190/185/170 @ 145 wpm) — recompute from the final VO assembly.

---

## 3. Tags (20)

1. claude fable 5
2. fable 5
3. anthropic fable 5
4. claude code
5. agent harness
6. harness engineering
7. ai agents
8. agentic workflows
9. workflows vs agents
10. ralph wiggum claude code
11. ralph loop
12. claude code hooks
13. claude code autonomous
14. openai codex
15. long running ai agents
16. ai agent orchestration
17. multi-agent systems
18. why do multi-agent llm systems fail
19. structured outputs
20. anthropic

Rationale: 1–4 catch the day-old news-cycle traffic (press titles all use "Claude Fable 5" verbatim);
5–6 catch the named-discipline cluster OpenAI created in February (InfoQ + multiple ranking guides);
7–9 catch the established "workflows vs agents" query family (Anthropic doc + promptingguide.ai rank);
10–13 match the verified ralph/autonomous-run cluster (official Anthropic plugin, The Register,
awesomeclaude.ai, "4 Hooks That Let Claude Code Run Autonomously" all rank); 14–16 catch the
Codex/long-horizon side of the episode; 17–18 match the MAST paper's literal searched title; 19–20 are
tutorial-intent + channel authority.

---

## 4. Thumbnail Concept — formula: `forge-alert`

Forge-alert is correct: breaking model/platform shift, launched yesterday.

**Claim words (3):** `THE HARNESS WON.`
Steel white, display weight, stacked left. Deliberately complementary to the title (title says what
ISN'T the reason; thumbnail names what IS) — satisfies the no-title-repeat rule and creates a
two-message pincer. Reads at 25% zoom.

**Proof object (1):** a real terminal capture of an unattended Claude Code run — visible elapsed-time
counter reading something like `27:14:09`, a stop-hook line (`Stop hook: exit 2 — continuing, iteration
7/10`) and a green `passes: true 14/23` ledger line. This is the claim made visible: a day-plus on the
clock, the harness doing the enforcing. Capture from an actual ralph-loop session — we have the
pipeline; do not mock it (Critic Contract fails fake chrome).

**Layout:** void-black field; terminal occupies right ~55%, slightly rotated, signal-cyan source strap
(`CLAUDE CODE — LIVE RUN`) along its top edge; claim words stacked left in steel white with "WON" in
forge red; thin forge-red alert bar at frame left. Two focal points max (claim block + terminal).
Nothing critical in the bottom-right timestamp zone. No robots, brains, or circuit heads.

**Fallback claim words:** `NOT THE MODEL.` (with MODEL struck through in forge red) — only if title C
is chosen, since against titles A/B it semantically echoes.

**Fallback proof object:** the 7.8x variance figure as a two-bar chart (18.48 vs 2.37, harness bar
forge red) — only if the terminal capture reads poorly at mobile size.

---

## 5. Shorts-Cut Candidates (3)

**Short 1 — s4, "The Amnesia Was the Feature" (~42s, strongest)**
In: "Three vendors just compiled the same technique into their agent products: one line of bash that
throws the model's memory away on purpose." Out: "Fresh context agents won. The amnesia was the feature."
Self-contained arc: paradox hook → named technique (Ralph) → money receipt ($50k contract for $297, six
repos overnight) → all-three-vendors payoff → mic-drop closer. Trim the context-rot study and token-clip
numbers to hit 42s. Named, searched technique ("ralph wiggum claude code") = built-in query demand.

**Short 2 — s2, "Zero Lines by Hand" (~40s)**
In: "OpenAI's own team shipped a million-line codebase with zero lines of manually written code." Out:
"The long-horizon coding agent got productized. The product is the loop."
Complete arc: impossible-sounding stat hook → harness engineering named → the 25-hour/13M-token/30k-line
run → closer. Cut the GPT-5.1-Codex-Max/METR reality-check block to keep it one idea. The hook stat is
the single most shareable line in the episode.

**Short 3 — s0, "The Autonomy Isn't in the Weights" (~38s)**
In: "Anthropic launched Claude Fable 5 today, and the most load-bearing sentence in the announcement
isn't about the model — it's about the harness." Out: "Long-horizon autonomy isn't in the weights. It's
AI agent orchestration."
The cold open is already a self-contained news short: launch hook → harness-framed duration claim →
7.8x variance stat → 9.5-point same-model swing → punchline. Trim the pricing/plans sentence to land
~38s. Publish FIRST — it rides the 48-hour Fable 5 news window directly.

Runner-up: s7 ("bash wrapper, JSON ledger, completion promise") — best utility short, but the
how-to setup needs the episode's framing to land; hold it for the day-3 follow-up slot.

---

## SEO grounding notes (verified 2026-06-10 via web search)

- **"Claude Fable 5" is the news-cycle entity string** — TechCrunch, VentureBeat, CNBC, Business
  Standard, The Neuron all title on it (launch June 9, 2026; "days" autonomy claim and Stripe migration
  anecdote confirmed in launch coverage). Titles A/B and tags 1–3 ride this exact phrasing.
- **"Run Claude Code autonomously/unattended for hours" is a live query cluster** — davidloor.com,
  Stark Insider, dev.to ("4 Hooks That Let Claude Code Run Autonomously"), DevOps.com (Claude Code
  Routines), hidekazu-konishi.com ("Claude Code Harness and Environment Engineering"), plus ranking
  YouTube results. The unattended-duration title angle matches real demand, not just a clever line.
- **"Harness engineering" is now a named, searched discipline** — InfoQ news item, Towards AI,
  theneuron.ai, tonylee.im, multiple 2026 "complete guide" pages rank for it; OpenAI's million-line /
  ~1,500-PR / zero-hand-written-code experiment is the anchor story. Title C and tags 5–6 target it.
- **"Ralph wiggum / ralph loop claude code" is a named, searched technique** — official Anthropic
  plugin README + claude.com/plugins/ralph-loop, awesomeclaude.ai, The Register, claudefa.st, paddo.dev
  all rank. Tags use both the "ralph wiggum claude code" and shorter "ralph loop" forms.
- **"Workflows vs agents" remains an established query family** — Anthropic's Building Effective
  Agents, promptingguide.ai "AI Workflows vs. AI Agents", Spring AI reference, Medium explainers rank.
- **"Why Do Multi-Agent LLM Systems Fail?"** is the MAST paper's literal title and searched as-is —
  kept verbatim as a tag.
