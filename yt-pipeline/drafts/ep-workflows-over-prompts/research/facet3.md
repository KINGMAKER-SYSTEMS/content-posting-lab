# Facet 3 — Control-flow techniques in agent CLIs
## Loop commands, stop-hook conditions, the ralph-loop lineage, state files, and why re-prompting with locked state beats one big prompt

Episode: "Claude runs for hours unattended. The model is not why."
Researched: 2026-06-10. All sources fetched/verified today.

---

## A. ORIGINS — the Ralph Wiggum lineage (Geoffrey Huntley)

### A1. The whole technique is one line of bash
- Source: https://ghuntley.com/ralph/ (canonical post, July 2025)
- The loop: `while :; do cat PROMPT.md | claude-code ; done`
- HumanLayer's history records the original variant piping to Amp: `while :; do cat PROMPT.md | npx --yes @sourcegraph/amp ; done`
- Named after Ralph Wiggum (The Simpsons) — dumb, persistent, cheerfully keeps going.

### A2. Timeline (per HumanLayer "A Brief History of Ralph", https://www.humanlayer.dev/blog/brief-history-of-ralph)
- **June 19, 2025** — Huntley demos Ralph at a Twitter GC meetup, ~15 attendees. Mentions "overbaking": extended runs produce "bizarre emergent behavior, like post-quantum cryptography support."
- **July 2025** — canonical blog post at ghuntley.com/ralph/ (Tessl says the term was coined ~May 2025; the post formalized it in July).
- **September 2025** — CURSED lang (the esoteric programming language Ralph built over ~3 months) officially launches.
- **October 2025** — lightning talk at Claude Code Anonymous SF + 75-min "AI That Works" podcast.
- **December 2025** — Anthropic ships the official ralph-wiggum plugin for Claude Code.
- **January 1, 2026** — "Ralph Wiggum Showdown" livestream (Dex Horthy vs Geoff Huntley).

### A3. Headline numbers from the canonical post (ghuntley.com/ralph/)
- "$50k USD contract, delivered, MVP, tested + reviewed" for "**$297 USD**" in API costs.
- Y Combinator hackathon: "**6 repos**" shipped overnight in a while loop.
- Usable context: "approximately **170k** of context window to work with"; quality degrades around the "**147k–152k** mark."
- Key quote: "Only one thing per loop... essential to use as little of it as possible. **The more you use the context window, the worse the outcomes.**"
- Key quote on state files: "**The items that you want to allocate to the stack every loop are your plan and your specifications.**"

### A4. State-file architecture (the "locked state" that gets re-fed every iteration)
Per ghuntley.com/ralph/ + snarktank/ralph + thomas-wiegold writeup:
1. `PROMPT.md` — the unchanging instruction set piped in each loop
2. `@fix_plan.md` (or `prd.json` / TODO) — prioritized list of incomplete items, "kept up to date with learnings"
3. `@specs/*` — technical specifications defining correct patterns
4. Git history + codebase itself = the real memory
- thomas-wiegold framing: "Each iteration starts with the exact same allocated context: the same PROMPT.md, the same AGENTS.md, the same specs/*.md files." The model is "deterministically bad in an undeterministic world... reliably mediocre at every iteration, and over time it grinds the codebase into shape."
  (https://thomas-wiegold.com/blog/ralph-loop-how-recursive-ai-agents-work/)

### A5. Huntley's economics + caveats
- LinearB masterclass writeup (https://linearb.io/blog/ralph-loop-agentic-engineering-geoffrey-huntley):
  - "**$10.42 USD per hour**" to run Sonnet 4.5 in a bash loop — under minimum wage.
  - "Smart zone vs dumb zone": performance deteriorates measurably past **60–70%** of context capacity; Ralph avoids the dumb zone by "deliberately reallocating the full specification with each iteration."
  - One-item philosophy: "pick the best, most important item and only do one."
  - Compaction failure mode: when sliding-window compaction evicts tokens, critical specs get lost — "the tower falls over."
- ghuntley.com/real/ (Feb 27, 2026): "the cost of software development is $10.42 an hour, which is less than minimum wage"; anonymous founder: "20ish people now do about 30x the output of what having more than 60 did 3 years ago"; "50k LOC" referenced re: Canva CTO.
- Oversight caveat (via Tessl, https://tessl.io/blog/unpacking-the-unpossible-logic-of-ralph-wiggumstyle-ai-coding/): "**I see LLMs as an amplifier of operator skill, and if you just set it off and run away, you're not going to get a great outcome.**"
- Tessl analysis: works best for mechanical, verifiable work — migrations, refactors, dependency updates, test cleanup — where success is checked programmatically (tests/linters/type-checkers, "things that can't lie").

---

## B. THE PRIMITIVES — how CLIs implement loop control

### B1. Claude Code Stop hook (the goal/stop-condition primitive)
- Source: https://code.claude.com/docs/en/hooks
- Stop event fires when Claude finishes a turn. A hook can return `{"decision": "block", "reason": "Tests are still failing. Please fix them before stopping."}` — or just `exit 2` with text on stderr — and Claude receives the reason and **keeps working instead of stopping**.
- Input includes `stop_hook_active` boolean to prevent infinite recursion, plus `tool_use_count` and `tool_use_duration` (seconds) — i.e., the hook can gate on effort spent.
- Also: `SubagentStop`, `StopFailure`, `PreToolUse`, `PostToolUse` events.

### B2. Anthropic's official ralph-wiggum plugin (Dec 2025)
- Source: https://github.com/anthropics/claude-code/blob/main/plugins/ralph-wiggum/README.md
- Implemented as a Stop hook (`hooks/stop-hook.sh`) that intercepts exit and re-feeds the SAME prompt. "The prompt never changes between iterations."
- Command: `/ralph-loop "<prompt>" --max-iterations <n> --completion-promise "<text>"`
- Completion = exact string match, conventionally `<promise>COMPLETE</promise>`. Primary safety = `--max-iterations`, not the promise.
- `/cancel-ralph` to bail. Credits Huntley/ghuntley.com/ralph explicitly.
- NOTE / critique (Josh Owens, https://joshowens.dev/ralph-wiggum-subagents/): the official plugin loops **inside the same session** — no context reset — "causing the same context rot problem it was designed to solve." His adaptation: parent agent holds big picture, child subagents get scoped single tasks with fresh context. Quote: "**Fresh context means better reasoning. Scoped work means no tangents.**"

### B3. OpenAI Codex CLI `/goal` — Ralph as a first-class vendor primitive
- Codex CLI **0.128.0, shipped April 30, 2026**: persisted `/goal` workflows — app-server APIs, model tools, runtime continuation, TUI controls for create/pause/resume/clear. (https://developers.openai.com/codex/changelog, https://developers.openai.com/codex/use-cases/follow-goals)
- Behavior: set a durable objective; Codex loops Plan→Act→Test→Review→Iterate until model-side audit logic self-evaluates the goal complete, or the configured **token budget** runs out.
- Real-usage anecdote: a16z GP **Andrew Chen** left Codex /goal on an eGPU/Mac device-driver project overnight — "**Fourteen hours later, it was still chipping away**" (https://www.mindstudio.ai/blog/codex-goal-ralph-loop-14-hour-autonomous-task).
- Contrast: /goal is in-session (one growing context window), NOT fresh-context-per-iteration like the bash loop (thomas-wiegold).

### B4. Gemini CLI official ralph extension
- Source: https://github.com/gemini-cli-extensions/ralph (official gemini-cli-extensions org)
- `/ralph:loop "task" --max-iterations N --completion-promise "DONE"` — default **5** iterations; `AfterAgent` hook in `hooks/stop-hook.sh` decides continue-vs-halt; memory cleared between iterations so the agent reads file state, not chat history. README: "Inspired by Geoffrey Huntley's article *Ralph*" + cites Anthropic Engineering's long-running-harness research.

### B5. Other CLIs (per thomas-wiegold survey)
- Cursor: `cursor-agent` headless CLI loops natively; Aider wrappable via `bash -c 'aider --yes-always --message "$(cat PROMPT.md)"'`; Goose has a first-class Ralph tutorial w/ cross-model review; Copilot CLI works for shorter programmatic runs.

---

## C. WHY RE-PROMPTING WITH LOCKED STATE BEATS ONE BIG PROMPT

### C1. Chroma "Context Rot" report — the empirical backbone
- Source: https://www.trychroma.com/research/context-rot (published **July 14, 2025**; Hong, Troynikov, Huber)
- Tested **18 LLMs** (GPT-4.1, Claude 4, Gemini 2.5, Qwen3...). Performance degrades as input grows even on trivial tasks.
- "**Across all models, we see significantly higher performance on focused prompts compared to full prompts.**"
- LongMemEval: full prompts ~**113k tokens** vs focused ~**300 tokens** — focused wins across all 18 models.
- Distractors compound; logically-structured haystacks paradoxically WORSE than shuffled ones.

### C2. Anthropic Engineering: "Effective harnesses for long-running agents"
- Source: https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents (**Nov 26, 2025**)
- Architecture: an **initializer agent** (first run: creates `init.sh`, `claude-progress.txt`, initial git commit) + a **coding agent** that makes incremental progress session after session.
- State = persistent structured files, esp. a JSON feature list — the claude.ai-clone example had **200+ features**, each with description, verification steps, and a boolean `passes` field.
- Every session starts with the same 4 steps: confirm directory, read git log, read feature list, verify dev server.
- Key line: "**Compaction isn't sufficient**" — agents need "a way for agents to quickly understand the state of work" via persistent progress files.
- One-shotting fails: agents "run out of context mid-implementation, leaving subsequent sessions to guess at incomplete work."

### C3. Dex Horthy / HumanLayer — "ruthless context resets" + RPI
- Source: https://linearb.io/blog/dex-horthy-humanlayer-rpi-methodology-ralph-loop
- RPI = Research → Plan → Implement, with deliberate frequent context resets instead of accumulating chat history. (Works for brownfield; for greenfield, spec-based loop execution wins.)
- Numbers: Ralph loops hit ~**90% code completion** on sponsored projects; ~**$10–12/hour** to run Sonnet indefinitely; **6 servers × 6–8 hours ≈ $600** in GCP credits; "**50,000+ lines of working code**" in single overnight runs.
- Quotes: "**You simply cannot outsource the thinking.**" / "The only way to build great experiences in AI is to find a thing that is right on the boundary of what the model is capable of, where it gets it right some of the time."

---

## D. REAL USAGE REPORTS

### D1. Geocodio (Jan 27, 2026) — https://www.geocod.io/code-and-coordinates/2026-01-27-ralph-loops
- Engineer **Mathias Hansen** built **two complete applications over one weekend** (Friday 4pm → Sunday evening) with "very little hands-on work."
- Loop: bash `while [ $ITERATION -lt $MAX_ITERATIONS ]`, `cat PROMPT.md | claude --print`, grep for "COMPLETE"; configured max **50** iterations; one feature finished by iteration **12**.
- State: `prd.json` (stories with boolean `"passes": false` flipped true), `progress.txt`, `PROMPT.md`. One run = "15 atomic commits, each implementing exactly one user story"; one iteration: "47 passed" tests, zero static-analysis errors.
- Cost reality: "**Ralph will absolutely _destroy_ your usage limits**" — colleague Sylvester Damgaard exhausted **two Claude Max subscriptions ($400/month total) in a few days**.

### D2. Ryan Carson / snarktank-ralph — the virality vector
- Repo: https://github.com/snarktank/ralph — "**20.1k stars, 2k forks**." Bash script (`ralph.sh`) spawning **a new AI instance with clean context per iteration** (Amp or Claude Code); `prd.json` + append-only `progress.txt`; default **10** iterations; stops on `<promise>COMPLETE</promise>`.
- README design rule: "each PRD item should be small enough to complete in one context window. If a task is too big, the LLM runs out of context before finishing and produces poor code."
- Carson's article went viral: "**Woke up to 690,000+ views on my Ralph article**" (https://x.com/ryancarson/status/2008950489904472501); ~**865,000+ views** by late Jan 2026 (Grokipedia/secondary).

### D3. Other named usage
- Huntley: built CURSED (esoteric programming language) over **3 months** of loop runs (multiple sources).
- Ashley Hindle: built **Fuel**, a multi-agent orchestrator inspired by Ralph loops (Geocodio).
- Integration-test refactor via Ralph: runtime **4 min → 2 sec** (atcyrus.com).
- atcyrus cost guidance: typical 50-iteration loop on a medium codebase = **$50–100+**; "Start with lower iteration limits (10-20) for new tasks"; "a $100 loop that saves 20 hours of work is worth it." Workflow inversion quote: "Prompt + success criteria → Claude loops autonomously → Human reviews final result." (https://www.atcyrus.com/stories/ralph-wiggum-technique-claude-code-autonomous-loops)
- Blake Crosley runs overnight autonomous loops (https://blakecrosley.com/blog/ralph-agent-architecture) — frames Ralph as solving 3 problems at once: context exhaustion (fresh 200K window/iteration), state persistence (filesystem as memory), task continuity (stop hooks gating exit).

---

## E. EPISODE-USEFUL SYNTHESIS
- The thesis writes itself: every vendor converged on the same control-flow shape — **a loop, a stop condition, and state files on disk** — because the bottleneck was never the model, it was context management. Anthropic (Stop hooks + ralph plugin, Dec 2025), OpenAI (/goal, Apr 30 2026), Google (gemini ralph extension) all shipped it as a first-class primitive within ~9 months of one guy's bash one-liner.
- Number arsenal for visuals: $297 vs $50k. $10.42/hr. 18 models. 113k vs 300 tokens. 147k–152k degradation point. 60–70% dumb zone. 20.1k stars. 690k views. 14 hours. 50 iterations. 200+ features. 90% completion. 50,000 LOC overnight. 4min→2sec. Two $200 subscriptions torched in days.
- Tension/nuance to keep it honest: official Anthropic plugin loops in-session (Josh Owens critique — no context reset); Codex /goal also one growing window; the purist bash loop remains the only true fresh-context variant. And Huntley himself: "amplifier of operator skill... set it off and run away, you're not going to get a great outcome."
