# Workflows Over System Prompts: Fable 5 Runs for Days Unattended — The Model Is Not Why

> ABN episode outline ledger — 9 segments, ~1,460 spoken words, ~10.1 min at 145 wpm.
> Register: technical wire-service, proof-first, no hype. Audience: builders who ship agentic systems.
> Every keyFact traces to the verified dossier. Honest caveats are carried inline and must stay attached downstream.
> Supersedes the earlier outline draft of 2026-06-10 17:52 (which predated the full verified dossier — Codex story, Fable harness primitives, ralph lineage, and METR numbers were missing).

---

## Segment 0 — Cold Open: The Workflow Engine Wearing a Model's Name
**Target: 135 words**

**Hook:** There's a 319-page breakdown of Fable 5 going around today. Everyone read the eval numbers. Almost nobody read the part where the "model" is actually a workflow engine wearing a model's name.

**Key facts:**
- Claude Fable 5 launched June 9, 2026 — "capabilities exceed those of any model we've ever made generally available" — at $10/$50 per million input/output tokens, included on Pro, Max, Team, and seat-based Enterprise plans through June 22.
- Anthropic's duration claim is harness-framed, verbatim: "Run Claude Fable 5 in an agent harness like Claude Code or Claude Managed Agents, and it can work for days at a time: planning across stages, delegating to sub-agents, and checking its own work." The autonomy verbs are explicitly tied to the harness, not raw model output.
- Harness-induced variance in agent benchmark scores is 7.80x larger than model-induced variance (HV 18.48 pp² vs MV 2.37 pp²) — "Stop Comparing LLM Agents Without Disclosing the Harness," May 2026.
- Same model, different harness: Claude Opus 4.5 scores 45.9% on SWE-bench Pro under the standardized SEAL scaffold vs 55.4% under Claude Code — a 9.5-point swing with zero model change.

**Sources:** anthropic.com/news/claude-fable-5-mythos-5 · anthropic.com/claude/fable · arxiv.org/html/2605.23950v1

**SEO:** Claude Fable 5, Fable 5 launch, agent harness, long-horizon autonomy, AI agent orchestration

---

## Segment 1 — The Thesis: The Prompt Is Not the Program
**Target: 160 words**

**Hook:** Everyone is tuning the wrong layer — the measured variance in agent performance lives in the harness, not the wording.

**Key facts:**
- Anthropic's canonical definition: "Workflows are systems where LLMs and tools are orchestrated through predefined code paths" — and workflows "offer predictability and consistency for well-defined tasks." Five named patterns: prompt chaining, routing, parallelization, orchestrator-workers, evaluator-optimizer.
- Prompt chaining is defined WITH enforcement: sequential LLM calls plus "programmatic checks (see gate in the diagram below) on any intermediate steps."
- METR (Feb 13, 2026): branded harnesses don't beat dumb defaults — Codex CLI beat METR's simple Triframe loop in only 14.5% of bootstrap samples (GPT-5); Claude Code beat ReAct in only 50.7% (Opus 4.5); neither statistically significant. METR's words: "Claude Code and Codex aren't obviously better than the default scaffolds we use for our agents." (Caveat: "the unlock is human-engineered workflow structure" is this episode's inference, not METR's stated claim.)
- Berkeley MAST: 1642 annotated traces across 7 frameworks → 14 failure modes in 3 categories (~43% system design, ~31% inter-agent misalignment, ~24% task verification); "41% to 86.7% failure rate on 7 state-of-the-art (SOTA) open-source MAS"; adding a high-level verification step to ChatDev lifted task success +15.6%.
- Practitioner contract-testing analysis (Apr 20, 2026): 64% of AI pipeline risk sits at the schema layer, and "schema drift in AI pipelines often produces outputs that parse without error" — failures live in the handoffs, and they fail silently. (Caveat: practitioner blog, figures as-stated by the author.)

**Sources:** anthropic.com/engineering/building-effective-agents · metr.org/notes/2026-02-13-measuring-time-horizon-using-claude-code-and-codex/ · arxiv.org/abs/2503.13657 · tianpan.co/blog/2026-04-20-contract-testing-ai-pipelines

**SEO:** workflows vs prompts, agent workflows, multi-agent failure modes, MAST taxonomy, prompt engineering vs orchestration

---

## Segment 2 — The Codex Story: OpenAI Productized the Loop
**Target: 170 words**

**Hook:** OpenAI's own team shipped a million-line codebase with zero hand-written lines — and the writeup credits the harness, not raw intelligence.

**Key facts:**
- GPT-5-Codex (Sept 15, 2025): OpenAI observed it "worked independently for more than 7 hours at a time on large, complex tasks, iterating on its implementation, fixing test failures."
- GPT-5.1-Codex-Max (Nov 19, 2025): first model natively trained to operate across multiple context windows via "compaction"; OpenAI reported internal tasks lasting more than 24 hours. Reality check: METR measured its 50%-time-horizon at ~2h40m (95% CI 75min–5h50m) vs GPT-5's 2h17m — an order of magnitude below the vendor demo. (Caveat: the 24h contrast is the episode's framing; METR's report never references the marketed figure.) Trendline since: GPT-5.2 at ~6.6h; Claude Mythos Preview ≥16h, with METR flagging measurements above 16h as unreliable.
- Feb 23, 2026 OpenAI dev blog, verbatim: "Codex ran for about 25 hours uninterrupted, used about 13M tokens, and generated about 30k lines of code" — explicitly crediting the agent loop over one-shot intelligence, run on four durable markdown memory files (prompt.md spec, plan.md milestones with acceptance criteria, implement.md runbook, documentation.md status/decision log) with verification commands after milestones.
- Harness engineering: a team of 3 growing to 7 engineers over 5 months drove Codex to ~1,000,000 LOC across ~1,500 opened-and-merged PRs (3.5 PRs per engineer per day) with "0 lines of manually-written code," in an estimated 1/10th the time of writing by hand — and "We regularly see single Codex runs work on a single task for upwards of six hours."
- Feb 4, 2026: OpenAI named the layer — the Codex harness is "the agent loop and logic that underlies all Codex experiences," exposed via the App Server, "a client-friendly, bidirectional JSON-RPC API" with Item/Turn/Thread primitives and threads that can be created, resumed, forked, and archived.

**Sources:** openai.com/index/introducing-upgrades-to-codex/ · openai.com/index/gpt-5-1-codex-max/ · metr.org/evaluations/gpt-5-1-codex-max-report/ · developers.openai.com/blog/run-long-horizon-tasks-with-codex · openai.com/index/harness-engineering/ · openai.com/index/unlocking-the-codex-harness/

**SEO:** GPT-5-Codex, Codex harness, GPT-5.1-Codex-Max compaction, harness engineering, METR time horizon, long-horizon coding agent

---

## Segment 3 — The Fable 5 Story: Anthropic Ships the Enforcement Primitives
**Target: 170 words**

**Hook:** Anthropic's launch copy ties the autonomy verbs to the harness — and Claude Code's docs show exactly where the deterministic control sits.

**Key facts:**
- Slay the Spire eval, verbatim: "giving it access to persistent file-based memory improved its performance three times more than for Opus 4.8; Fable also reached the game's final act three times more often" — two distinct 3x figures, and the memory uplift is a harness feature, not a weights feature.
- Stripe, verbatim: "In a 50-million-line Ruby codebase, the model performed a codebase-wide migration in a day that would otherwise have taken a whole team over two months by hand."
- Claude Code hooks: 30 lifecycle events, 5 handler types; exit code 2 is a blocking error whose "stderr text is fed back to Claude as an error message"; Stop hook exit 2 "Prevents Claude from stopping, continues the conversation" — "when is the agent done" is decided by deterministic hook code.
- Subagents each run in their own context window with a custom system prompt, tool allowlists, and independent permissions; model resolution is deterministic: env var → invocation param → frontmatter → main conversation's model; worktree isolation runs them in temporary git worktrees, auto-cleaned if no changes.
- Agent teams (experimental, v2.1.32+): lead + teammates + shared task list + mailbox; "Task claiming uses file locking to prevent race conditions"; quality gates are hook code — TeammateIdle exit 2 keeps a teammate working, TaskCompleted exit 2 blocks completion.
- Task Budgets (beta): the model sees a running token countdown for the whole agentic loop and self-moderates (minimum 20,000 tokens) — distinct from max_tokens, an enforced per-response ceiling the model is NOT aware of.

**Sources:** anthropic.com/news/claude-fable-5-mythos-5 · code.claude.com/docs/en/hooks.md · code.claude.com/docs/en/sub-agents · code.claude.com/docs/en/agent-teams · platform.claude.com/docs/en/about-claude/models/migration-guide.md

**SEO:** Claude Code hooks, Claude Code subagents, agent teams, stop hook, Fable 5 Claude Code, task budgets

---

## Segment 4 — Compiled Control Flow: /loop, /goal, and the Ralph Lineage
**Target: 170 words**

**Hook:** The most influential orchestration technique of the agent era is one line of bash that throws the model's memory away on purpose.

**Key facts:**
- Ralph, per Geoffrey Huntley (July 14, 2025): "Ralph is a technique. In its purest form, Ralph is a Bash loop" — `while :; do cat PROMPT.md | claude-code ; done`. Headline economics (self-reported, not audited): "Cost of a $50k USD contract, delivered, MVP, tested + reviewed... $297 USD," and "We Put a Coding Agent in a While Loop and It Shipped 6 Repos Overnight."
- Locked state re-fed every iteration is FOUR files, not three: PROMPT.md, @fix_plan.md, @specs/*, and @AGENT.md ("the heart of the loop," which Ralph self-updates with learnings); "deterministically allocate the stack the same way every loop" — conversation history is discarded by construction; git commits and the codebase are the external memory.
- Control-flow rule: "One item per loop. I need to repeat myself here—one item per loop" — because ~170k of context is usable and "quality of output clips at the 147k-152k mark" (LinearB's "Dumb Zone": past 60–70% capacity).
- Chroma's Context Rot study (18 LLMs, July 14, 2025) is the empirical backbone: "even under these minimal conditions, model performance degrades as input length increases" — focused ~300-token prompts significantly beat ~113k-token full prompts across all models.
- Anthropic's official ralph-wiggum plugin (public Nov 16, 2025): the Stop hook "creates the self-referential feedback loop by blocking normal session exit"; --completion-promise uses exact string matching (`<promise>COMPLETE</promise>`); "Always rely on --max-iterations as your primary safety mechanism." Built-in guards: the stop_hook_active flag, and Claude Code "overrides the hook and ends the turn after 8 consecutive blocks."
- Vendors compiled it into first-class primitives: Codex /goal is "a verifiable stopping condition" (CLI 0.128.0 Apr 30, 2026; GA May 21, 2026; mobile Jun 9, 2026); Google's gemini-cli-extensions/ralph runs /ralph:loop with default 5 iterations, an AfterAgent stop-hook, and clears the previous turn's context — with "Special Thanks to Geoffrey Huntley."

**Optional color (if script has room):** Josh Owens critique (Feb 12, 2026) — Anthropic's plugin "kept the session alive... The restart wasn't a bug. It was the entire feature."

**Sources:** ghuntley.com/ralph/ · linearb.io/blog/ralph-loop-agentic-engineering-geoffrey-huntley · trychroma.com/research/context-rot · github.com/anthropics/claude-code (plugins/ralph-wiggum README) · code.claude.com/docs/en/hooks · developers.openai.com/codex/use-cases/follow-goals · developers.openai.com/codex/changelog · github.com/gemini-cli-extensions/ralph

**SEO:** ralph loop, ralph wiggum technique, Codex goal mode, stop hook loop, Geoffrey Huntley, context rot, fresh context agents

---

## Segment 5 — Schemas as Ledgers: Contracts Between Pipeline Stages
**Target: 170 words**

**Hook:** A JSON schema isn't output formatting — it's the contract that lets stage two trust stage one without re-reading its work.

**Key facts:**
- Schema adherence is an engineering layer, not a model property: gpt-4-0613 scored under 40% on complex JSON-schema following; training alone got gpt-4o-2024-08-06 to 93%; constrained decoding closed the gap to 100%. (Caveat: the 100% pairs a newer model WITH constrained decoding — not a perfectly isolated comparison.)
- Anthropic structured outputs (GA for Fable 5) compile the schema into a grammar that constrains token generation: "Always valid: No more JSON.parse() errors... Reliable: No retries needed for schema violations"; compiled grammars cached 24 hours from last use; hard limits: 20 strict tools, 24 optional parameters, 16 union-typed parameters per request.
- The Claude Agent SDK extends the contract to entire multi-turn runs: "The agent can use any tools it needs to complete the task, and you still get validated JSON matching your schema at the end," re-prompting on mismatch; failure is the typed result subtype `error_max_structured_output_retries` — never free-text garbage.
- Microsoft's Magentic-One keeps two explicit ledgers — a Task Ledger (facts, educated guesses, the plan) and a per-step Progress Ledger (self-reflection on task progress and agent assignments) — and when the stall counter exceeds 2 it re-enters the outer loop, updates the Task Ledger, and re-plans. (Editorial framing, fairly grounded: the ledger, not the model, is what makes long unattended runs recoverable.)
- In the OpenAI Agents SDK, even agent-to-agent handoffs are typed tool calls: "Handoffs are represented as tools to the LLM" (a handoff to Refund Agent becomes `transfer_to_refund_agent`), with a Pydantic input_type whose payload is parsed and validated before the receiving callback fires.
- The "JSON hurts reasoning" objection ("Let Me Speak Freely?", 2024) was re-run by dottxt with prompts held equal: structured matched or beat unstructured on every task tested (GSM8K 0.78 vs 0.77; Last Letter 0.77 vs 0.73; Shuffle Objects 0.44 vs 0.41). And XGrammar kills the runtime-cost objection: up to 100x speedup over existing structured-generation solutions, near-zero end-to-end serving overhead.

**Sources:** openai.com/index/introducing-structured-outputs-in-the-api/ · platform.claude.com/docs/en/build-with-claude/structured-outputs.md · code.claude.com/docs/en/agent-sdk/structured-outputs · microsoft.com/en-us/research/articles/magentic-one-a-generalist-multi-agent-system-for-solving-complex-tasks/ · openai.github.io/openai-agents-python/handoffs/ · arxiv.org/abs/2408.02442 · blog.dottxt.ai/say-what-you-mean.html · arxiv.org/abs/2411.15100

**SEO:** structured outputs, JSON schema contract, constrained decoding, agent handoffs, task ledger, Magentic-One, schema validation agents

---

## Segment 6 — The Scoreboard: Workflow Numbers vs the Best Prompt Numbers
**Target: 170 words**

**Hook:** Harness variance is 7.8 times model variance — but the prompt side has receipts too, so here's the honest scoreboard.

**Key facts:**
- Compounding-error math: at 95% per-step reliability, a 20-step agent succeeds ~36% of the time; at 99% per-step, ~82%; 100 steps at 99% falls back to ~37%. Prompt tuning nudges the per-step base; workflow structure changes the exponent. (The 0.99^100 figure is independently checkable arithmetic.)
- Anthropic's C compiler build: 16 parallel Claude agents, ~2 weeks, ~2,000 Claude Code sessions, just under $20,000, ~100,000 lines compiling Linux 6.9 (x86, ARM, RISC-V). Carlini, verbatim: "Most of my effort went into designing the environment around Claude—the tests, the environment, the feedback" and "it's important that the task verifier is nearly perfect, otherwise Claude will solve the wrong problem."
- Harness-only change moved Terminal-Bench 2 pass@1 from 69.7% to 77.0% on the same model (note: that row is GPT-5.4 high under "AHE harness evolution," not Claude); scaffold-only variation of up to 15pp reported on SWE-bench Verified.
- Counter #1: OpenAI's GPT-4.1 prompting guide — three instructions (persistence, tool-calling, planning) "increased our internal SWE-bench Verified score by close to 20%," and "inducing explicit planning increased the pass rate by 4%." Honest read: behavioral-contract sentences inside an agentic harness, not wording wizardry.
- Counter #2: meaning-preserving prompt formatting changes alone swing accuracy by up to 76 points (LLaMA-2-13B), ~10 points on average across 53 tasks — documented, but on older open models; frontier sensitivity is lower.
- Counter #3: GEPA reflective prompt evolution beats GRPO by 6% on average, up to 20%, with up to 35x fewer rollouts (ICLR 2026 Oral) — but the strongest pro-prompt result is prompts optimized by an automated eval-driven loop, not hand-tuning. The workflow optimizes the prompt.

**Sources:** prodigaltech.com/blog/why-most-ai-agents-fail-in-production · anthropic.com/engineering/building-c-compiler · arxiv.org/html/2605.23950v1 · developers.openai.com/cookbook/examples/gpt4-1_prompting_guide · arxiv.org/abs/2310.11324 · arxiv.org/abs/2507.19457

**SEO:** harness variance, agent reliability math, compounding error agents, GEPA prompt optimization, prompt sensitivity, SWE-bench scaffold

---

## Segment 7 — How to Start: Your First Locked Loop
**Target: 165 words**

**Hook:** Your first loop is a bash wrapper, a JSON ledger full of passes:false, and a completion promise — start at ten iterations, not fifty.

**Key facts:**
- Task fit (Tessl): Ralph suits "migrations, refactors, dependency updates, or test clean-ups — where progress is incremental and the definition of done can be stated upfront." Huntley's own caveat: "I see LLMs as an amplifier of operator skill, and if you just set it off and run away, you're not going to get a great outcome."
- Cost guardrails (one practitioner's estimate, not benchmarks): "A typical 50-iteration loop on a medium-sized codebase might cost $50-100+ in API usage"; "Start with lower iteration limits (10-20) for new tasks." One developer's Ralph refactor cut a test suite from 4 minutes to 2 seconds.
- Geocodio, Jan 2026: "I built two full apps this weekend using Ralph... I started Friday at 4pm, and by Sunday evening both were done with very little hands-on work from me" — a MAX_ITERATIONS=50 bash wrapper looping `cat PROMPT.md | claude --print` with a COMPLETE grep, state in prd.json (passes:false→true) plus progress.txt; a colleague "maxed out TWO Claude Max 20x subscriptions. That's $400/month in subscriptions, exhausted in a few days."
- snarktank/ralph (20,118 stars live): "Each iteration is a fresh instance with clean context"; "Default is 10 iterations"; "When all stories have passes: true, Ralph outputs <promise>COMPLETE</promise> and the loop exits."
- 12-Factor Agents (23.2k stars): production "agents" are "mostly deterministic code, with LLM steps sprinkled in at just the right points"; keep micro-agents to 3-10, maybe 20 steps max (say "3 to 20," not a hard cap).
- Anthropic's long-running-agents post (Nov 26, 2025): an initializer agent sets up "an init.sh script, a claude-progress.txt file... and an initial git commit"; the coder works one feature at a time against a JSON feature list — the claude.ai clone started with 200+ features marked "passes": false — and "However, compaction isn't sufficient."

**Sources:** tessl.io/blog/unpacking-the-unpossible-logic-of-ralph-wiggumstyle-ai-coding/ · atcyrus.com/stories/ralph-wiggum-technique-claude-code-autonomous-loops · geocod.io/code-and-coordinates/2026-01-27-ralph-loops · github.com/snarktank/ralph · github.com/humanlayer/12-factor-agents · anthropic.com/engineering/effective-harnesses-for-long-running-agents

**SEO:** how to run a ralph loop, autonomous coding loop tutorial, agent state files, prd.json passes true, 12-factor agents, long-running agent harness

---

## Segment 8 — Takeaway: Make It Legible and Enforceable
**Target: 150 words**

**Hook:** When OpenAI's harness team hit failures, the fix was almost never "try harder" — because the prompt is not the program; the workflow is.

**Key facts:**
- OpenAI harness-engineering debugging philosophy, verbatim: "the fix was almost never 'try harder'... human engineers always stepped into the task and asked: 'what capability is missing, and how do we make it both legible and enforceable for the agent?'"
- Project Vend phase 2: "Among the most impactful changes we made was forcing Claudius to follow procedures" — phase 1 lost money over time; in phase 2 "weeks with negative profit margin were largely eliminated" (now in 3 cities: SF, NY, London). The added CEO-agent supervisor — an intelligence-shaped fix — had mixed, possibly negative effect.
- Agents have a measurable half-life (Toby Ord, built on METR's data): a constant per-minute failure rate implies exponentially declining success with task length, and each agent can be characterized by its own half-life — structure is what moves the exponent.
- The unit of delegation is shifting (Mitch Ashley, Futurum, June 10, 2026): "the natural unit of delegation shifts from a single prompt or pull request to a bounded work block the agent owns end-to-end." Anthropic's own guidance matches: define "done" as a gradeable Managed Agents Outcome rubric with an iterate→grade→revise loop — default 3 iterations, max 20.

**Sources:** openai.com/index/harness-engineering/ · anthropic.com/research/project-vend-2 · arxiv.org/abs/2505.05115 · futurumgroup.com/insights/claude-fable-5-is-most-consequential-where-software-is-built/ · platform.claude.com/docs/en/managed-agents/define-outcomes.md

**SEO:** agent harness design, legible and enforceable, AI agent half-life, bounded work block, outcome rubric, workflows over prompts takeaway

---

## Word budget

| # | Segment | Target |
|---|---|---|
| 0 | Cold Open | 135 |
| 1 | The Thesis | 160 |
| 2 | The Codex Story | 170 |
| 3 | The Fable 5 Story | 170 |
| 4 | Compiled Control Flow | 170 |
| 5 | Schemas as Ledgers | 170 |
| 6 | The Scoreboard | 170 |
| 7 | How to Start | 165 |
| 8 | Takeaway | 150 |
| | **Total** | **1,460 ≈ 10.1 min @ 145 wpm** |

## Production notes for the script stage

- Headroom to 13 min exists if segments 2–6 expand with on-screen receipts; do not expand by adding unverified claims.
- The "319-page breakdown" in the cold-open hook is the brief's supplied hook line, not a dossier-verified figure — keep it as color and pivot to verified numbers within the first sentence, or swap to the 7.8x stat if standards require.
- Honest-caveat ledger carried into keyFacts and non-droppable: METR-inference disclaimer (seg 1), 24h-demo-vs-2h40m framing note (seg 2), self-reported Ralph economics (seg 4), non-isolated 100% comparison (seg 5), GPT-5.4-not-Claude harness-evolution row (seg 6), practitioner-estimate cost figures (seg 7).
- Date discipline: Fable 5 launched June 9, 2026; Futurum piece dated June 10, 2026; free Fable window runs "through June 22" — say it that way, not "13-day window."
- Quote discipline: everything inside double quotes in keyFacts is verbatim from the dossier's verified sources; paraphrase outside quotes only.
