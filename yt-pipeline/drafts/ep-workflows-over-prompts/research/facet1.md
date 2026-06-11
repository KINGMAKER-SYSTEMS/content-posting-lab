# Facet 1 — OpenAI Codex (GPT-5.x-codex line): long autonomous runs, orchestration, harness patterns

> Research for episode: "Fable 5 runs for hours unattended. The model is not why."
> Researched: 2026-06-10. All claims verified against current sources (primary where reachable; openai.com blocks direct fetch, so some official-post quotes verified via search snippets + mirrors).

---

## TL;DR for the episode thesis

The Codex line is the cleanest A/B proof that **the harness, not the model, is what unlocked long unattended runs**:

- OpenAI's own dev blog attributes a 25-hour uninterrupted run to "the agent loop the model operates inside" — markdown memory files + milestone verification — explicitly saying it was "less about model intelligence."
- OpenAI's harness-engineering team shipped ~1M LOC / ~1,500 PRs with ZERO human-written code by engineering the environment, not the model.
- METR's independent measurement is the kicker: marketing says ">24 hours," METR measures a 50% time horizon of **2h40m** for the same model — and separately finds that fancy harnesses (Claude Code, Codex CLI) **don't beat dumb default scaffolds** in autonomous evals. The harness's value is in the workflow structure (verification loops, durable memory, stopping conditions), not the brand of agent loop.
- OpenAI then productized the harness patterns: compaction (Codex-Max), /goal mode (durable objective + stopping condition), thread automations (scheduled wake-ups), App Server (harness as a protocol).

---

## Timeline of the Codex line (long-run capability arc)

| Date | Release | Long-run number |
|---|---|---|
| 2025-09-15 | GPT-5-Codex | ">7 hours" independent work observed |
| 2025-11-19 | GPT-5.1-Codex-Max | ">24 hours" internal; compaction across context windows; METR: 2h40m @ 50% |
| 2025-12 | GPT-5.2-Codex / GPT-5.2 | METR: GPT-5.2 (high) = 6.6 hr 50%-time-horizon (highest METR had reported) |
| 2026-02-05 | GPT-5.3-Codex | 25% faster; SWE-Bench Pro 56.8%; Terminal-Bench 2.0 77.3%; OSWorld 64.7% (+26.5 pts); "first model instrumental in creating itself" |
| 2026-02-12 | GPT-5.3-Codex-Spark | 1,000+ tok/s on Cerebras WSE-3 (vs ~65 tok/s standard) |
| 2026-04-23 | GPT-5.5 in Codex | new recommended frontier model for most Codex tasks; in-app browser; auto approval-review agent |
| 2026-04-30 | Codex CLI 0.128.0 | /goal workflows (experimental); --full-auto deprecated → permission profiles |
| 2026-05-21 | Codex app 26.519 | Goal mode GA across app/IDE/CLI; remote computer use after Mac locks |
| 2026-06-09 | Codex app 26.608 | "Migrate to Codex" importer for Claude Code / Claude Cowork setups |

---

## CLAIMS (with quotes + numbers)

### C1. GPT-5-Codex worked independently for 7+ hours (Sept 2025) — the line's first big run-length number
- Quote: "During testing, GPT-5-Codex worked independently for more than 7 hours at a time on large, complex tasks, iterating on its implementation, fixing test failures, and ultimately delivering a successful implementation."
- Source: https://openai.com/index/introducing-upgrades-to-codex/ (corroborated: https://venturebeat.com/dev/openai-unveils-new-model-gpt-5-codex-optimized-for-agentic-coding)
- Numbers: 7 hours. Released Sept 15, 2025; API access Sept 23, 2025.
- Confidence: HIGH

### C2. GPT-5.1-Codex-Max: first model natively trained for compaction; >24h internal runs (Nov 19, 2025)
- "First model natively trained to operate across multiple context windows through compaction," able to "coherently work over millions of tokens in a single task." Internal evals "observed GPT-5.1-Codex-Max work on tasks for more than 24 hours" — multi-step refactors, test-driven iteration, autonomous debugging. Also ~30% fewer thinking tokens than GPT-5.1-Codex at medium effort.
- Source: https://openai.com/index/gpt-5-1-codex-max/ (corroborated: https://venturebeat.com/ai/openai-debuts-gpt-5-1-codex-max-coding-model-and-it-already-completed-a-24)
- Numbers: >24 hours; millions of tokens; ~30% fewer thinking tokens.
- Confidence: HIGH
- NOTE: compaction = THE harness pattern productized into the model layer. Great visual: context window filling → squashing → continuing.

### C3. METR reality-check: Codex-Max 50% time horizon = 2h40m (vs the 24h marketing number)
- "Point estimate: 2 hours 40 minutes; 95% CI: 75 minutes to 5 hours 50 minutes." Up from GPT-5's 2h17m, on-trend with doubling-time curves.
- Source: https://metr.org/evaluations/gpt-5-1-codex-max-report/
- Numbers: 2h40m; CI 1h15m–5h50m; GPT-5 was 2h17m.
- Confidence: HIGH
- Visual gold: "24 hours (vendor demo)" vs "2h40m (independent 50% success horizon)" side-by-side bar.

### C4. METR: GPT-5.2 hit a 6.6-hour 50%-time-horizon — highest ever reported (early 2026)
- METR: "We estimate that GPT-5.2 with `high` (not `xhigh`) reasoning effort has a 50%-time-horizon of around 6.6 hrs (95% CI of 3 hr 20 min to 17 hr 30 min)... This is the highest estimate for a time horizon measurement we have reported to date."
- Source: https://x.com/METR_Evals/status/2019169900317798857 ; dashboard https://metr.org/time-horizons/
- Numbers: 6.6 hrs; CI 3h20m–17h30m.
- Confidence: HIGH

### C5. METR (Feb 13, 2026): the branded harness does NOT beat default scaffolds in autonomous evals
- "Neither Claude Code nor Codex outperform the default scaffolds METR uses." "For GPT-5, Codex beats Triframe in 14.5% of bootstrap samples." "For Opus 4.5, Claude Code beats ReAct in 50.7% of bootstrap samples." Despite to-do lists, subagents, sophisticated prompting in the branded harnesses.
- Source: https://metr.org/notes/2026-02-13-measuring-time-horizon-using-claude-code-and-codex/ (author Nikola Jurkovic)
- Numbers: 14.5%, 50.7%.
- Confidence: HIGH
- NUANCE FOR SCRIPT: the unlock isn't the agent-loop brand — it's the workflow the human engineers around it (specs, validation loops, stopping conditions). Supports "the model is not why" AND sharpens it: "the harness app is not why either — the workflow is."

### C6. OpenAI dev blog: 25-hour uninterrupted GPT-5.3-Codex run — 13M tokens, 30k LOC — credited to the loop, not the model
- "Codex ran for about 25 hours uninterrupted," "used about 13M tokens," "generated about 30k lines of code" (GPT-5.3-Codex, Extra High reasoning). Success factor was "less about model intelligence" and more about "the agent loop the model operates inside."
- Harness pattern documented: durable project memory in markdown — prompt.md (spec freeze), plan.md (milestones + acceptance criteria + validation commands), implement.md (execution runbook), documentation.md (live status log). "After milestones, it ran verification commands and repaired failures before continuing" (lint, typecheck, tests, build).
- Source: https://developers.openai.com/blog/run-long-horizon-tasks-with-codex
- Numbers: 25 hours; 13M tokens; 30,000 LOC; 4 memory files.
- Confidence: HIGH
- This is the single best source for the episode thesis. OpenAI's own developer blog saying the loop matters more than the model.

### C7. Harness engineering: ~1M LOC, ~1,500 PRs, 3 engineers, zero human-written code in 5 months
- "Over the past five months..." — "the repository contains on the order of a million lines of code"; "roughly 1,500 pull requests have been opened and merged with a small team of just three engineers driving Codex. This translates to an average throughput of 3.5 PRs per engineer per day" (team since grown to seven). "Every line of code—application logic, tests, CI configuration, documentation, observability, and internal tooling—has been written by Codex." "We estimate that we built this in about 1/10th the time it would have taken to write the code by hand." First commit late August 2025.
- Run length in production practice: "We regularly see single Codex runs work on a single task for upwards of six hours."
- Fix philosophy: when something failed, the question was never "try harder" — it was "what capability is missing, and how do we make it both legible and enforceable for the agent?"
- Source: https://openai.com/index/harness-engineering/ (mirror used for quotes: https://jaytaylor.com/notes/node/1770842156000.html)
- Numbers: ~1,000,000 LOC; ~1,500 PRs; 3→7 engineers; 3.5 PRs/engineer/day; 1/10th time; 5 months; 6-hour runs.
- Confidence: HIGH

### C8. The Codex harness is a named, productized layer: App Server = bidirectional JSON-RPC (Feb 4, 2026)
- Harness defined as "the agent loop and logic that underlies all Codex experiences" (web, CLI, IDE extension, macOS app). App Server is "a client-friendly, bidirectional JSON-RPC API." Primitives: Item (atomic I/O unit), Turn (one unit of agent work), Thread (durable container). "Codex creates, resumes, forks, and archives threads, and persists the event history." They tried MCP first; "maintaining MCP semantics... proved difficult," so they built a JSON-RPC protocol mirroring the TUI loop. Use cases: "turn Codex into a code reviewer, an SRE agent, or a coding assistant."
- Source: https://openai.com/index/unlocking-the-codex-harness/ (mirror: https://github.com/newton20/harness-engineering-kb/blob/master/raw/openai-com-index-unlocking-the-codex-harness.md)
- Confidence: HIGH
- Episode angle: OpenAI literally extracted the harness into a protocol — the harness is the product.

### C9. /goal mode: durable objectives, "multiple hours" unattended, GA May 21, 2026
- Official docs: /goal lets you "give Codex a durable objective for long-running work"; "Codex can work independently for multiple hours without needing your input." Good goals need "one objective and one stopping condition" and "a clear success condition and validation loop" — "Codex should know what 'done' means before it starts." Lifecycle: /goal <objective>, /goal pause|resume|clear.
- Shipped experimental in Codex CLI 0.128.0 (April 30, 2026); "Goal mode is no longer an experimental feature and is available in the Codex app, IDE extension, and CLI" (May 21, 2026). /goal on mobile June 9, 2026.
- Sources: https://developers.openai.com/codex/use-cases/follow-goals ; https://developers.openai.com/codex/changelog
- Confidence: HIGH
- Episode angle: OpenAI turned "write a good workflow prompt" into a first-class primitive — objective + stopping condition + validation loop is exactly the harness pattern, baked into the CLI.

### C10. Thread automations: scheduled wake-ups of the same thread (April 16, 2026)
- Changelog (Codex app 26.415): "Thread automations can wake up the same thread on a schedule" — check a long-running process, watch for updates, continue a follow-up loop without starting from scratch. Same release: in-app browser w/ page commenting, Computer Use for macOS apps, GitHub PR inspection/review workflows.
- Source: https://developers.openai.com/codex/changelog
- Confidence: HIGH
- Episode angle: unattended ≠ one long run; it's also cron-style re-entry with preserved context.

### C11. GPT-5.3-Codex (Feb 5, 2026): 25% faster, benchmark sweep, and it helped build itself
- "GPT-5.3-Codex... advances both the frontier coding performance of GPT-5.2-Codex and the reasoning... of GPT-5.2, together in one model, which is also 25% faster." SWE-Bench Pro (Public) 56.8% (vs 56.4% GPT-5.2-Codex); Terminal-Bench 2.0 77.3%; OSWorld-Verified 64.7% (+26.5 pts vs GPT-5.2-Codex); fewest output tokens of any prior model. "The first model that was instrumental in creating itself" — Codex team used early versions to debug its own training, manage deployment, diagnose evals.
- Sources: https://openai.com/index/introducing-gpt-5-3-codex/ ; https://www.neowin.net/news/openai-debuts-gpt-53-codex-25-faster-and-setting-new-coding-benchmark-records/ ; https://www.datacamp.com/blog/gpt-5-3-codex
- Numbers: 25% faster; 56.8%; 77.3%; 64.7% (+26.5); Feb 5, 2026.
- Confidence: HIGH
- NOTE: SWE-Bench Pro moved only 0.4 pts (56.4→56.8) while OSWorld jumped 26.5 pts — model gains are plateauing on pure SWE tasks while agentic/computer-use gains explode. Supports "the model is not why" framing.

### C12. GPT-5.3-Codex-Spark: 1,000+ tok/s on Cerebras (Feb 12, 2026)
- Smaller real-time variant; "1,000+ tokens/sec on Cerebras hardware" (WSE-3, 4T transistors); vs ~65 tok/s for standard GPT-5.3-Codex (~15x). First OpenAI flagship deployment on non-NVIDIA inference at scale. Research preview for ChatGPT Pro.
- Sources: https://openai.com/index/introducing-gpt-5-3-codex-spark/ ; https://www.servethehome.com/openai-gpt-5-3-codex-spark-now-running-at-1k-tokens-per-second-on-big-cerebras-chips/
- Numbers: 1,000+ tok/s; ~15x; Feb 12, 2026.
- Confidence: HIGH on launch/speed; MEDIUM on the 65 tok/s baseline (third-party).

### C13. GPT-5.5 became the recommended Codex model April 23, 2026; autonomy got guardrails, not just length
- Changelog: "GPT-5.5 is now available in Codex as OpenAI's newest frontier model" (April 23). Same wave: in-app browser for local dev servers; "automatic approval reviews route risky actions through a reviewer agent" BEFORE execution ("approval reviews happen before the request runs" to keep "blast radius small"); April 30: `--full-auto` flag deprecated in favor of explicit permission profiles (sandbox CLI selection, working-dir controls). GPT-5.4-mini positioned for "lighter exploration and subagent work."
- Sources: https://developers.openai.com/codex/changelog ; https://www.developersdigest.tech/blog/codex-changelog-april-2026
- Confidence: HIGH (official changelog) / MEDIUM on third-party paraphrases.
- Episode angle: as runs got longer, OpenAI removed the YOLO flag and added a reviewer-agent gate — long autonomy required MORE harness, not less.

### C14. Parallel orchestration is the daily-driver pattern (April–June 2026)
- GPT-5.5-era Codex workflow: work "on four problems in parallel — running the test suite, drafting docs from a code diff, and proposing refactors in three separate sandboxes — while the engineer reviews and merges" (third-party guide describing current Codex app workflow). Official changelog corroborates the infrastructure: worktree creation + thread coordination for local projects and worktrees (May 29, 2026), branch selection + environment setup scripts on mobile (June 9, 2026), Codex profile screen with usage stats and token activity charts.
- Sources: https://tosea.ai/blog/openai-codex-complete-guide-2026 (MEDIUM) ; https://developers.openai.com/codex/changelog (HIGH)
- Confidence: MEDIUM (the "four problems" picture) / HIGH (worktrees + thread coordination features).

### C15. Competitive harness portability: "Migrate to Codex" from Claude Code (June 9, 2026)
- Changelog, Codex app 26.608 (June 9, 2026): "Added Migrate to Codex flows for importing supported setup from Claude Code and Claude Cowork."
- Source: https://developers.openai.com/codex/changelog
- Confidence: HIGH
- Episode angle: harness configs (skills, MCP, instructions) are now the asset vendors fight over — further proof the workflow layer is where the value lives.

### C16 (minor). $100/month Pro tier for "longer, high-intensity Codex sessions" (April 30, 2026)
- Source: https://www.developersdigest.tech/blog/codex-changelog-april-2026 (third-party summary of ChatGPT release notes)
- Confidence: MEDIUM (not verified against primary).

---

## Numbers cheat-sheet (for visuals)

- **7 hours** — GPT-5-Codex unattended (Sept 2025)
- **>24 hours** — GPT-5.1-Codex-Max internal (Nov 2025)
- **2h40m** — METR's independent 50% time horizon for that same model
- **6.6 hrs** — METR 50% horizon for GPT-5.2 (high) — highest ever reported
- **25 hours / 13M tokens / 30k LOC** — documented GPT-5.3-Codex single run
- **1M LOC / 1,500 PRs / 3 engineers / 3.5 PRs/eng/day / 5 months / 0 human-written lines / 1/10th the time** — harness-engineering experiment
- **6 hours** — "regular" single-task Codex runs inside OpenAI
- **14.5%** — bootstrap samples where Codex CLI beats METR's dumb default scaffold (i.e., it doesn't)
- **25% faster, 56.8% SWE-Bench Pro, 77.3% Terminal-Bench, 64.7% OSWorld (+26.5)** — GPT-5.3-Codex
- **0.4 pts** — total SWE-Bench Pro gain 5.2-Codex → 5.3-Codex (plateau evidence)
- **1,000+ tok/s vs ~65** — Codex-Spark on Cerebras

## Caveats / verification notes

- openai.com returns 403 to fetchers; official-post quotes were cross-verified via search snippets, VentureBeat/Neowin coverage, and mirrors (jaytaylor.com, harness-engineering-kb on GitHub). Treat exact wording of openai.com quotes as near-verbatim but re-check if quoting on screen.
- No model named "GPT-5.5-Codex" exists as of 2026-06-10. The 5.5-class situation: GPT-5.5 (general frontier model, April 23, 2026) is the recommended model *in* Codex; the dedicated codex line's latest are GPT-5.3-Codex and GPT-5.3-Codex-Spark. GPT-5.4-mini handles light/subagent work. (codersera May 2026 roundup also mentions GPT-5.6 rumors — unverified, excluded.)
- The "four problems in parallel" workflow description is from a third-party guide; the underlying features (worktrees, thread coordination, sandboxes) are official.
