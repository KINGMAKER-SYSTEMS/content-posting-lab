# Facet 2 — Fable 5 + Claude Code: agentic orchestration, long-run autonomy, structured outputs

> Episode: "Fable 5 runs for hours unattended. The model is not why."
> Researched: 2026-06-10 (Fable 5 launched yesterday, 2026-06-09). All sources verified current.
> Thesis support: the model's duration claims are real, but every mechanism that makes unattended runs *safe and steerable* lives in the harness — hooks, subagents, background supervisor, task budgets, structured outputs, outcome loops. Those are workflow features, not model weights.

---

## A. The model: Fable 5 launch facts (primary: anthropic.com)

**Source:** https://www.anthropic.com/news/claude-fable-5-mythos-5 (official announcement, 2026-06-09)

- Launched **June 9, 2026**. "State-of-the-art on nearly all tested benchmarks of AI capability."
- **Pricing: $10 / $50 per million tokens (input/output)** — "less than half the price of Claude Mythos Preview."
- **From June 9 through June 22**, Fable 5 is "included on Pro, Max, Team, and seat-based Enterprise plans at no extra cost." (13-day free window — good visual.)
- Safeguards: trigger "on average, in less than **5% of sessions**"; "more than **95%** of Fable sessions involve no fallback at all." Fallback model is Opus 4.8. Cyber/bio/chem topics route to Opus 4.8.
- Mythos 5 = same underlying model, safeguards lifted for vetted cyberdefenders via Project Glasswing (US gov collab).
- Benchmarks: "first to break **90%** on our core analytics benchmark"; highest on Hebbia Finance Benchmark; highest on Cognition FrontierCode "even at medium effort."
- Long-run anecdotes (the duration story):
  - Stripe: "compressed months of engineering into days"; a codebase migration done in "**a day** that would otherwise have taken a whole team over **two months**."
  - Genomics research: "over **a week** of largely autonomous work."
  - Slay the Spire eval: with persistent file-based memory, reached the final act "**three times more often**" than Opus 4.8 → memory (a harness feature) tripled long-horizon performance.
  - Mythos 5 drug design: "accelerated aspects of the drug design process by around **ten times**."
- Anthropic internal usage line: "handling the complex multi-agent workflows our employees run daily in Claude Code" — i.e., even Anthropic frames the value through the Claude Code orchestration layer.

**Source:** https://www.anthropic.com/claude/fable (product page)

- "Run for **days at a time**: planning across stages, **delegating to sub-agents**, and **checking its own work**." ← the official duration claim, and note it's defined *in terms of harness primitives* (subagents, self-checks).
- "Days-long, complex, and asynchronous tasks previous models couldn't sustain"; "large migrations, complex implementations, and multi-day autonomous sessions."
- Self-validation: "writes its own tests to check its work"; uses "vision to check outputs against goals."
- Customer numbers: spreadsheet runs "**25–30% faster**" than Opus 4.8; one customer got in "**36 hours**" what a competitor model took "**four days**" to do.
- "Stays focused across millions of tokens in long-running tasks."
- 90% input-token discount via prompt caching; US-only inference at **1.1x** pricing multiplier; requires **30-day** data retention for safety monitoring.

**API surface (Anthropic claude-api skill, cached 2026-05-26 + Models API):**
- Model ID `claude-fable-5`. **1M context window, 128K max output.**
- Adaptive thinking only (`thinking: {type: "adaptive"}`); `budget_tokens`, `temperature`, `top_p`, `top_k` all removed (400). Explicit `thinking: {type: "disabled"}` also 400s on Fable 5 — omit the param instead.
- Effort levels: `low | medium | high | xhigh | max`. **`xhigh` is the default in Claude Code** and recommended for coding/agentic work.
- **Task Budgets (beta, header `task-budgets-2026-03-13`)**: `output_config.task_budget {type: "tokens", total: N}` — model sees a *running countdown* for the whole agentic loop and self-moderates. **Minimum 20,000 tokens.** Distinct from `max_tokens` (hard cap the model can't see). This is THE "how do you let it run for hours without a runaway bill" primitive.
- Server-side compaction (beta `compact-2026-01-12`) auto-summarizes history near the context limit — long sessions don't die at the window edge.

**Secondary (analyst framing):** https://futurumgroup.com/insights/claude-fable-5-is-most-consequential-where-software-is-built/ — "the consequential capability in Claude Fable 5 is **duration**, particularly for what a coding agent sustains unattended"; "the natural unit of delegation shifts from a single prompt or pull request to a bounded work block the agent owns end-to-end."

---

## B. The harness: Claude Code orchestration (primary: code.claude.com)

### Subagents — https://code.claude.com/docs/en/sub-agents

- "Each subagent runs in its own context window with a custom system prompt, specific tool access, and independent permissions." Defined as markdown files in `.claude/agents/` with YAML frontmatter; body = system prompt.
- Frontmatter fields include: `description, prompt, tools, disallowedTools, model, permissionMode, mcpServers, hooks, maxTurns, skills, initialPrompt, memory, effort, background, isolation, color` (also settable via `--agents` JSON flag).
- **Per-subagent model routing**: `model: sonnet | opus | haiku | fable | <full id> | inherit` — route cheap work to Haiku, hard work to Fable. Resolution order: `CLAUDE_CODE_SUBAGENT_MODEL` env → per-invocation param → frontmatter → main model.
- Per-subagent `effort` override (`low`→`max`).
- `isolation: worktree` runs the subagent in a temporary git worktree branched from the default branch; auto-cleaned if no changes.
- **Foreground vs background**: "Foreground subagents block the main conversation… Background subagents run concurrently while you continue working. They run with the permissions already granted in the session and **auto-deny any tool call that would otherwise prompt**." ← the unattended-safety mechanism: a background worker can't escalate privileges mid-run.
- **Ctrl+B** backgrounds a running task; `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` kills all background functionality.
- Fresh isolated context: subagent "does not see your conversation history" — only a composed delegation message. Exception: **forks** (`/fork`, default-on from v2.1.161) inherit the full conversation but still return only the final result.
- Stopped subagents auto-resume in the background if they receive a `SendMessage`.

### Hooks — https://code.claude.com/docs/en/hooks

- "Hooks are user-defined shell commands, HTTP endpoints, or LLM prompts that execute automatically at specific points in Claude Code's lifecycle."
- **33 distinct hook events** across session, per-turn, agentic-loop, async, and team lifecycles (SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, SubagentStart/Stop, TaskCreated/TaskCompleted, TeammateIdle, PreCompact/PostCompact, Stop…).
- **5 handler types**: command, HTTP, MCP tool, prompt (single-turn LLM), agent (subagent w/ tools, experimental).
- Exit-code protocol: **exit 0** = continue (JSON output for fine-grained control), **exit 2** = blocking error, stderr fed back to Claude; anything else = non-blocking.
- `Stop` / `SubagentStop` hooks can "prevent Claude from stopping, continue the conversation" — i.e., deterministic code decides when the agent is done, not the model. Core unattended-run primitive.
- Async hooks: `async: true` (non-blocking background) and `asyncRewake: true` (wakes Claude on exit 2).
- Numbers: default timeout **600s** most events (30s UserPromptSubmit, 10s MessageDisplay); hook output cap **10,000 chars**; 6 config scopes (user/project/local/managed-policy/plugin/frontmatter).

### Agent teams — https://code.claude.com/docs/en/agent-teams

- Experimental; gated behind `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`. Requires Claude Code **v2.1.32+**.
- Architecture: **team lead + teammates + shared task list + mailbox**. "Teammates work independently, each in its own context window, and communicate directly with each other" — unlike subagents which "only report back to the main agent."
- "Task claiming uses **file locking** to prevent race conditions" — coordination is plain engineering, not model magic.
- Tasks have 3 states (pending/in progress/completed) + dependencies; "when a teammate completes a task that other tasks depend on, blocked tasks unblock without manual intervention."
- Guidance numbers: "Start with **3-5 teammates** for most workflows"; "**5-6 tasks per teammate** keeps everyone productive"; "Token costs scale linearly."
- Quality gates via hooks: `TeammateIdle` exit 2 = "send feedback and keep the teammate working"; `TaskCompleted` exit 2 = block completion. → unattended quality enforcement is hook code.
- Plan-approval workflow: teammate stays read-only in plan mode until lead approves; lead approves autonomously per your criteria.
- Limits: one team per lead, no nested teams, lead is fixed, no session resumption for in-process teammates.
- Storage: `~/.claude/teams/{team}/config.json` + `~/.claude/tasks/{team}/`.
- Warning that supports thesis: "Letting a team run unattended for too long increases the risk of wasted effort" — Anthropic itself says monitoring/steering matters.

### Background agents / agent view — https://code.claude.com/docs/en/agent-view

- `claude agents` = "one screen for all your background sessions: what's running, what needs your input, and what's done."
- "Each background session is a full Claude Code conversation that **keeps running without a terminal attached**."
- "A separate **supervisor process** runs them, so you can close agent view, close your shell… and your dispatched work keeps going." Sessions persist on disk through auto-updates and supervisor restarts; **survive machine sleep** (processes resume on wake).
- Edit isolation: "Before editing files, Claude moves the session into an isolated git worktree under `.claude/worktrees/`, so parallel sessions can read the same checkout but each writes to its own."
- From v2.1.161: rows show a `done/total` count (e.g. `2/5`) of parallel work items (subagents, background shells, monitors) and name the longest-running item.
- Dispatch from agent view, `/bg` inside a session, or `claude --bg` from the shell; `!`-prefixed input dispatches a raw shell job.

### Structured outputs — https://platform.claude.com/docs/en/build-with-claude/structured-outputs.md

- Two features: **JSON outputs** (`output_config.format`) and **strict tool use** (`strict: true`); usable independently or together.
- "Structured outputs guarantee schema-compliant responses through **constrained decoding**." Guarantees: "Always valid — no more JSON.parse() errors"; "Type safe"; "Reliable — no retries needed for schema violations."
- **Claude Fable 5 is on the GA supported-model list** (alongside Mythos 5, Opus 4.8/4.7/4.6, Sonnet 4.6/4.5, Opus 4.5, Haiku 4.5).
- "Compiled grammars are cached for **24 hours** from last use." Cache invalidates on schema-structure or tool-set change (name/description changes don't).
- Limits: **20 strict tools/request, 24 optional params, 16 union-typed params**; no recursive schemas, no min/max numeric constraints, `additionalProperties` must be `false`.
- Caveats: schema can be violated on refusal (`stop_reason: "refusal"`) or `max_tokens` truncation. HIPAA: no PHI in schema definitions (cached separately).
- Workflow relevance: this is how orchestration scripts get machine-readable hand-offs between agents (e.g., a StructuredOutput-style tool call as the only thing the parent reads).

### Managed Agents + outcomes (API-side long-run harness — claude-api skill docs, platform.claude.com/docs/en/managed-agents/*)

- Server-managed agent loop with per-session containers; sessions stream SSE events; built-in context compaction + prompt caching.
- **Outcomes**: `user.define_outcome` with a rubric → "iterate → grade → revise loop until the artifact meets the rubric" — separate grader model, `max_iterations` default **3, max 20**. The grade-and-revise loop is harness code, not the model.
- Webhooks auto-disable after **~20 consecutive failed deliveries**; multiagent sessions cap at **25 concurrent threads**, roster max **20 agents**, one level of delegation only.
- Official migration-guide framing (skill, shared/model-migration.md): for long-horizon work "give the full task specification up front in a single well-specified initial turn and run at high effort… in Claude Code, `/goal` sets direction for the run; with Managed Agents, state what 'done' looks like via an Outcome."

---

## C. Ecosystem confirmations (same-day availability)

- GitHub: "Claude Fable 5 is generally available for GitHub Copilot" (2026-06-09) — https://github.blog/changelog/2026-06-09-claude-fable-5-is-generally-available-for-github-copilot/
- AWS: Fable 5 on AWS w/ built-in safeguards — https://aws.amazon.com/blogs/aws/anthropic-claude-fable-5-on-aws-mythos-class-capabilities-with-built-in-safeguards-now-available/
- Microsoft: "Claude Fable 5 available today in Microsoft Foundry: Powering the next era of autonomous agents" — https://azure.microsoft.com/en-us/blog/claude-fable-5-available-today-in-microsoft-foundry-powering-the-next-era-of-autonomous-agents/
- Press: TechCrunch (2026-06-09) frames Fable 5 as "a version of Mythos the public can access today."

---

## D. Numbers for visuals (quick-grab)

| Number | What it is |
|---|---|
| June 9, 2026 | Fable 5 launch date |
| $10 / $50 per MTok | Fable 5 pricing (½ the Mythos Preview price) |
| <5% / >95% | sessions triggering safeguards / sessions with no fallback |
| 1M / 128K | context window / max output tokens |
| days at a time | official autonomous-run claim (anthropic.com/claude/fable) |
| 1 week | genomics "largely autonomous work" run |
| 2 months → 1 day | Stripe migration compression |
| 3× | final-act completion w/ file-based memory vs Opus 4.8 |
| 25–30% | faster spreadsheet runs vs Opus 4.8 |
| 36 hrs vs 4 days | customer task vs competitor model |
| 33 | Claude Code hook lifecycle events |
| 5 | hook handler types |
| exit code 2 | "block and feed back to Claude" |
| 20,000 | minimum task_budget tokens (the loop countdown) |
| 3–5 / 5–6 | recommended teammates / tasks-per-teammate |
| 24 hr / 20 | structured-output grammar cache / strict tools per request |
| 3 (max 20) | default/max outcome grader iterations |
| v2.1.32 / v2.1.161 | agent teams min version / fork-default + done-total counters |

## E. Narrative line this facet supports

Anthropic's own duration claim is *defined by harness verbs* — "planning across stages, delegating to sub-agents, checking its own work." Every mechanism that converts raw model capability into hours of safe unattended runtime is workflow infrastructure: Stop/SubagentStop hooks decide "done," background subagents auto-deny privilege escalation, the supervisor process keeps sessions alive without a terminal, task budgets put a countdown on spend, file-locked task lists coordinate teams, structured outputs make hand-offs machine-checkable, and outcome rubrics grade-and-revise. The 3× Slay-the-Spire result is the cleanest proof: same model, add file-based memory (a harness feature), triple the long-horizon performance. The model raised the ceiling; the workflow is the floor that lets you walk away.
