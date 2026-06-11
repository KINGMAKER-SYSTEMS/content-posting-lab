# Facet 5 — Evidence that workflows / locked values BEAT prompt-tuning (+ honest counters)

> Episode: "Fable 5 runs for hours unattended. The model is not why."
> Researched: 2026-06-10. All sources fetched and verified this date.
> Format: claim → source → verbatim quote → numbers → confidence.

---

## A. THE HEADLINE EVIDENCE (workflows / harness / locked values win)

### A1. Harness variance is ~8x larger than model variance (peer-reviewed, May 2026)
- **Source:** "Stop Comparing LLM Agents Without Disclosing the Harness" — arXiv:2605.23950, Zhang, Wang, Ge, Xu, Hamm, Reddy. Published May 7, 2026. https://arxiv.org/html/2605.23950v1
- **Quote:** "Average HV is 18.48 pp² versus average MV of 2.37 pp², a ratio of 7.80×" — harness-induced variance ~7.8x model-induced variance on SWE-bench Verified.
- **More numbers:**
  - Holding the model fixed, changing ONLY the harness raises Terminal-Bench 2 pass@1 from **69.7% → 77.0%** (+7.3 pp).
  - Claude Opus 4.5 on SWE-bench Pro: **45.9%** in one harness vs **55.4%** under Claude Code (+9.5 pp, identical model).
  - "up to **15 percentage points** of scaffold-only variation on SWE-bench Verified."
  - Thesis quote: "benchmark scores for LLM agents on long-horizon tasks are not valid for cross-model comparison unless the execution harness is disclosed."
- **Why it's gold:** This is THE number for the episode title. The model is literally not why — the harness explains ~8x more of the outcome variance.
- **Confidence: HIGH** (primary source, current, peer-style arXiv paper)

### A2. Same model, different tool: 16-point Terminal-Bench swing from harness alone
- **Source:** "The Harness Effect: Why the Same Model Scores 16 Points Higher in a Different Tool" — Daniel Vaughan, Apr 19, 2026 (updated May 24, 2026), citing Pawel Jozefiak's Apr 15, 2026 analysis. https://codex.danielvaughan.com/2026/04/19/the-harness-effect-same-model-different-tool-different-score/
- **Quote:** "That is a 16-point differential from harness tuning alone — no model change, no fine-tuning, no prompt engineering on the task itself." Also: "The infrastructure surrounding the model shifted the result more than swapping to an entirely different frontier model would have."
- **Numbers (Terminal-Bench 2.0, Claude Opus):** Cursor **93%**, Claude Code "Mythos" config **92.1%**, Claude Code default **77%**, Codex CLI w/ GPT-5.4 **77.3%**.
- **Confidence: MEDIUM-HIGH** (practitioner analysis aggregating leaderboard data; numbers consistent with A1's direction; secondary, so attribute as "one analysis found")

### A3. Anthropic's C compiler: ~zero prompt effort, all environment effort
- **Source:** "Building a C compiler with a team of parallel Claudes" — Anthropic engineering blog, Nicholas Carlini (Safeguards team), Feb 2026. https://www.anthropic.com/engineering/building-c-compiler (coverage: InfoQ, The Register Feb 9, 2026)
- **Quotes:**
  - "Most of my effort went into designing the environment around Claude—the tests, the environment, the feedback."
  - "It's important that the task verifier is nearly perfect, otherwise Claude will solve the wrong problem."
- **Numbers:** **16** parallel agents · **~2 weeks** unattended · **~2,000** Claude Code sessions · **just under $20,000** API cost · **100,000-line** Rust-based C compiler that builds **Linux 6.9** on x86, ARM, RISC-V · **2 billion** input tokens, **140 million** output tokens.
- **Mechanics of "locked values":** lock files in git for task claiming; CI to prevent regressions; GCC as oracle; deterministic test subsampling (1–10%). The prompt itself was basically "break problems into pieces and keep going until it's perfect."
- **Why it's gold:** the longest-running unattended-agent showcase to date, and the author explicitly says prompt effort was minimal vs environment design.
- **Confidence: HIGH** (primary Anthropic source)

### A4. Anthropic's canonical guidance: simple composable patterns beat clever frameworks
- **Source:** "Building Effective Agents" — Anthropic, Dec 19, 2024 (still the canonical reference; basis of Spring AI docs etc.). https://www.anthropic.com/research/building-effective-agents
- **Quotes:**
  - "the most successful implementations weren't using complex frameworks or specialized libraries. Instead, they were building with simple, composable patterns."
  - "Workflows are systems where LLMs and tools are orchestrated through predefined code paths."
  - "workflows offer predictability and consistency for well-defined tasks, whereas agents are the better option when flexibility and model-driven decision-making are needed at scale."
- **Confidence: HIGH** (primary; older but explicitly still canonical — quoted across 2026 docs)

### A5. Project Vend phase 2: procedures + scaffolding turned a money-losing AI shop profitable
- **Source:** "Project Vend: Phase two" — Anthropic, Dec 18, 2025. https://www.anthropic.com/research/project-vend-2 (also red.anthropic.com/2025/project-vend-2/)
- **Quotes:**
  - "It's likely that Claudius struggled with its shopkeeping mission in phase one because of a lack of scaffolding."
  - "Among the most impactful changes we made was forcing Claudius to follow procedures."
- **Numbers/facts:** Phase 1 (Sonnet 3.7) lost money over time; Phase 2 (Sonnet 4.0 → 4.5) with CRM, inventory tooling, price-research procedures: "weeks with negative profit margin were largely eliminated." Expanded to 3 cities (SF, NY, London).
- **Why it's gold:** a real-money longitudinal A/B where Anthropic itself attributes the turnaround to scaffolding + forced procedures, not raw model intelligence.
- **Confidence: HIGH** (primary)

### A6. Locked values in decoding: schema adherence goes <40% (prompting) → 100% (constrained)
- **Source:** OpenAI, "Introducing Structured Outputs in the API," Aug 6, 2024. https://openai.com/index/introducing-structured-outputs-in-the-api/ (page 403s to fetchers; figure confirmed via Humanloop: https://humanloop.com/blog/structured-outputs and multiple secondaries)
- **Quote (Humanloop):** "According to OpenAI, getting LLMs to respond in a specific format via prompt engineering was around 35.9% reliable before structured outputs. Now, it's 100% reliable (if strict is set to true)."
- **Numbers:** gpt-4o-2024-08-06 + structured outputs: **100%** on complex JSON-schema following; gpt-4-0613 with prompting: **<40%** (~35.9%).
- **Why it's gold:** cleanest possible "lock the value in the system, don't ask nicely in the prompt" datapoint. Constrained decoding is a workflow/system guarantee; prompting tops out far below.
- **Confidence: HIGH** for the numbers (multiple independent confirmations); note the announcement is 2024 — the *number pair* is canonical, not breaking news.

### A7. Compounding error math: why per-step prompting can never save a long run
- **Source:** "Why most AI agents fail in production: the compounding error problem" — Prodigal Tech blog; same math in MindStudio "Reliability Compounding Problem" and Towards Data Science "The Multi-Agent Trap." https://www.prodigaltech.com/blog/why-most-ai-agents-fail-in-production
- **Numbers (pure math, independently checkable):**
  - 95% per-step reliability over 20 steps → **36%** end-to-end success (0.95^20 ≈ 0.358).
  - 99% per-step over 20 steps → **82%** (0.99^20 ≈ 0.818); over 100 steps → **36.6%**.
  - Five 99%-reliable components → 95% system reliability; ten → 90%.
- **Practitioner note:** real-world per-step error rates on complex tasks run "closer to 10–20%."
- **Why it's gold for visuals:** an exponential-decay curve. Prompt tuning moves per-step accuracy a little; workflow design (checkpoints, verifiers, retries, deterministic steps) changes the *exponent structure*.
- **Confidence: HIGH** for the math; MEDIUM for the "10–20% real-world per-step error" practitioner estimate.

### A8. Agents have a "half-life": failure compounds at a constant rate per minute
- **Source:** "Is there a Half-Life for the Success Rates of AI Agents?" — Toby Ord, arXiv:2505.05115, May 8, 2025 (built on METR data). https://arxiv.org/abs/2505.05115
- **Quote:** "performance of AI agents on longer-duration tasks can be explained by an extremely simple mathematical model — a constant rate of failing during each minute" → "exponentially declining success rate with the length of the task"; "each agent could be characterised by its own half-life."
- **Why it matters:** formalizes A7 against real agent data. To run for hours, you don't need a better prompt — you need to reset/checkpoint context so the hazard clock restarts (exactly what loops + fresh contexts + file-system memory do).
- **Confidence: HIGH** (primary paper; widely cited through 2026)

### A9. In Anthropic's own multi-agent system, token budget — not prompt cleverness — explained performance
- **Source:** "How we built our multi-agent research system" — Anthropic engineering, Jun 2025. https://www.anthropic.com/engineering/multi-agent-research-system
- **Quotes:** "token usage by itself explains 80% of the variance" (BrowseComp analysis); multi-agent (Opus 4 lead + Sonnet 4 subagents) "outperformed single-agent Claude Opus 4 by 90.2%"; "multi-agent systems use about 15× more tokens than chats."
- **Numbers:** **80%** of variance = token usage; ~10% tool-call count; ~5% model choice. **+90.2%** vs single agent. **15×** token cost.
- **Why it's gold:** Anthropic's own decomposition puts *model choice* at ~5% of variance. Architecture (parallel context windows = more token budget) is the lever.
- **Confidence: HIGH** (primary)

### A10. Ralph loops: a dumb bash loop ships real codebases overnight
- **Source:** "A Brief History of Ralph" — HumanLayer blog, Jan 6, 2026. https://www.humanlayer.dev/blog/brief-history-of-ralph (origin: Geoffrey Huntley, mid-2025)
- **Quote:** the whole technique is "while :; do cat PROMPT.md | npx --yes @sourcegraph/amp ; done" — and "dumb things can work surprisingly well."
- **Numbers/facts:** **6 repositories shipped overnight** (Aug 2025); the CURSED language (self-hosting compiler) built over **~3 months** of continuous loops; a frontend refactor done in **6 hours** unattended. Failure mode recorded too: left running too long it added unrequested "post-quantum cryptography support."
- **Mechanism:** fresh context each iteration; the *file system and git history*, not the conversation, are the memory — i.e., locked state outside the model.
- **Caveat in the same source:** badly specified PROMPT.md still produced "meh" results — specification quality still matters (feeds the counter section).
- **Confidence: MEDIUM-HIGH** (practitioner primary source; results self-reported)

### A11. 12-Factor Agents: production "agents" are ~90% deterministic code
- **Source:** humanlayer/12-factor-agents (Dex Horthy), GitHub + talks through 2025-2026. https://github.com/humanlayer/12-factor-agents
- **Quote (paraphrase-faithful):** most production AI agent products "are mostly deterministic code, with LLM steps sprinkled in at just the right points."
- **Numbers:** micro-agents scoped to **3–20 steps** max; HumanLayer's own deploy bot described as **90% deterministic code**.
- **Confidence: MEDIUM-HIGH** (widely-cited practitioner framework; the 90% figure is one company's self-report)

### A12. METR: frontier models now run multi-hour tasks — and even METR's own numbers shift with the scaffold
- **Source:** "Time Horizon 1.1" — METR, Jan 29, 2026. https://metr.org/blog/2026-1-29-time-horizon-1-1/ ; live page https://metr.org/time-horizons/ (last updated May 8, 2026, includes "Claude Mythos Preview (early)")
- **Numbers:** Claude Opus 4.5 50%-success time horizon **320 minutes** (~5.3 hrs) [CI 170–729]; GPT-5 **214 min**; P50 doubling time (≥2024 models) **88.6 days** under TH1.1 (vs 108.9 earlier). Caveat: "Measurements above 16 hrs are unreliable with our current task suite."
- **Scaffold-sensitivity quote:** "two models (GPT-4o and o3) had statistically significantly higher scores under Vivaria than Inspect" — i.e., even the measurement org sees harness-dependent scores.
- **Why it's gold:** grounds the episode premise ("runs for hours") with the current best public measurement, AND shows harness effects inside the benchmark itself.
- **Confidence: HIGH** (primary; current through May 8, 2026)

### A13. What practitioners say blocks agents: quality/reliability, not model IQ or cost
- **Source:** LangChain "State of Agent Engineering" survey — fielded Nov 18–Dec 2, 2025, **1,340 responses**. https://www.langchain.com/state-of-agent-engineering
- **Numbers:** **~33%** cite quality (accuracy, consistency, policy adherence) as the #1 blocker; latency **20%**; cost is fading as a concern.
- **Why it matters:** consistency/adherence is exactly what workflows + locked values buy and what prompt-tuning struggles to guarantee.
- **Confidence: MEDIUM** (vendor survey; large N; self-selected respondents)

---

## B. THE COUNTER-ARGUMENTS (when prompts genuinely matter — keep the episode honest)

### B1. Three sentences of prompt = ~+20% on SWE-bench Verified (OpenAI, primary)
- **Source:** GPT-4.1 Prompting Guide — OpenAI Cookbook. https://developers.openai.com/cookbook/examples/gpt4-1_prompting_guide
- **Quote:** "The model adhered closely to these three simple instructions and increased our internal SWE-bench Verified score by close to 20%." And: "inducing explicit planning increased the pass rate by 4%."
- **The three instructions:** persistence (don't yield turn early), tool-calling (don't guess, use tools), planning (plan + reflect).
- **Honest framing:** the single biggest documented agentic gain in this whole file came from PROMPT text. But note what kind: it's *behavioral contract* text (keep going, use tools, plan) — closer to harness policy written in English than to wording wizardry. And it ships inside a harness.
- **Confidence: HIGH** (primary)

### B2. Prompt formatting alone can swing accuracy by up to 76 points
- **Source:** Sclar, Choi, Tsvetkov, Suhr — "Quantifying Language Models' Sensitivity to Spurious Features in Prompt Design," arXiv:2310.11324 (v2 Jul 2024), ICLR 2024. https://arxiv.org/abs/2310.11324
- **Numbers:** up to **76 accuracy points** difference between semantically equivalent prompt formats (LLaMA-2-13B); **~10 points** average spread across 50+ tasks; sensitivity persists with bigger models, more few-shot examples, and instruction tuning.
- **Double-edged:** proves prompts matter enormously — but in a *spurious, unpredictable* way. Arguably the strongest argument FOR locking formats down in code and testing them, rather than hand-tuning by vibes. Use this as the pivot from counter back to thesis.
- **Confidence: HIGH** (peer-reviewed; older models — flag that frontier-model sensitivity is lower but not gone)

### B3. Optimized prompts can beat reinforcement learning itself (GEPA, ICLR 2026 Oral)
- **Source:** "GEPA: Reflective Prompt Evolution Can Outperform Reinforcement Learning" — arXiv:2507.19457, revised Feb 14, 2026, ICLR 2026 Oral. https://arxiv.org/abs/2507.19457
- **Quote:** "GEPA outperforms GRPO by 6% on average and by up to 20%, while using up to 35x fewer rollouts." Also "outperforms the leading prompt optimizer, MIPROv2, by over 10% (e.g., +12% accuracy on AIME-2025)."
- **Numbers:** **+6% avg / +20% max** over GRPO RL; **35×** fewer rollouts; **+10%+** over MIPROv2.
- **Honest framing:** the prompt is such a high-leverage surface that *evolving the prompt beats retraining the weights*. BUT: GEPA is an automated, eval-driven optimizer inside a pipeline — i.e., even the strongest pro-prompt result is "prompts optimized by a workflow," not a human tweaking adjectives.
- **Confidence: HIGH** (primary, current)

### B4. Inside the Ralph ecosystem itself: bad specs → bad output
- **Source:** HumanLayer "A Brief History of Ralph" (same as A10).
- **Fact:** a poorly-specified PROMPT.md produced "meh" results in their productivity-tool experiment; the technique's own proponents stress spec quality and context engineering as "such a high-leverage activity."
- **Framing:** the loop doesn't rescue an empty spec. Workflows amplify whatever contract you wrote.
- **Confidence: MEDIUM-HIGH**

---

## C. SYNTHESIS FOR THE EPISODE

The honest line the evidence supports:

1. **Variance decomposition says harness ≈ 8× model** (A1), token budget ≈ 80% of variance with model ≈ 5% (A9), and a 16-point benchmark swing comes from tooling alone (A2). The model is not why.
2. **Long unattended runs are an error-compounding problem** (A7, A8). Prompts move per-step accuracy a few points; only workflow structure (verifiers, locks, CI, fresh contexts, file-system state) changes the failure exponent. The two flagship multi-hour/multi-week runs — the C compiler (A3) and Ralph/CURSED (A10) — both attribute success to environment design, near-perfect verifiers, and locked state, with explicitly minimal prompt effort.
3. **Locked values beat asked-for values**: <40% → 100% schema adherence (A6); Vend went profitable when procedures were *forced*, not suggested (A5).
4. **But prompts are not nothing**: 3 sentences bought OpenAI ~20% on SWE-bench (B1), formatting alone can swing 76 points (B2), and evolved prompts beat RL (B3). The resolution: every big prompt win is either a behavioral contract (harness policy in English) or the output of an automated optimizer — i.e., prompts matter most when you treat them like code inside a workflow, and least when you hand-tune them like magic words.

### Best numbers for visuals (ranked)
- **7.80×** — harness variance vs model variance (A1)
- **0.95^20 = 36%** — the compounding-error decay curve (A7)
- **<40% → 100%** — prompting vs constrained schema adherence (A6)
- **80% / ~10% / ~5%** — variance: tokens / tool calls / model choice (A9)
- **93 vs 77** — same model, two harnesses, Terminal-Bench 2.0 (A2)
- **16 agents · 2 weeks · $20k · 100k lines · ~2,000 sessions** (A3)
- **+20% from 3 sentences** — the counter (B1)
- **76 points from formatting** — the pivot stat (B2)
- **320 min** Opus 4.5 time horizon; doubling every **88.6 days** (A12)

### Recency check
- Current-2026 primary sources: A1 (May 2026), A2 (Apr/May 2026), A3 (Feb 2026), A12 (Jan/May 2026), B3 (Feb 2026 revision, ICLR 2026), A10 (Jan 2026), A5 (Dec 2025), A13 (Dec 2025).
- Canonical-but-older (flag in script as "still the standard reference"): A4 (Dec 2024), A6 (Aug 2024), B1 (Apr 2025), B2 (2023/24), A8 (May 2025), A9 (Jun 2025).
