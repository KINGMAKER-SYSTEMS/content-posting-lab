<!-- researched: 2026-06-12 -->
# Facet 2: Anthropic Fable 5 & Claude Code — Agentic Orchestration

## Verified Claims

### 1. Fable 5 Launch: June 9, 2026 — "Capabilities Exceed Any Model Generally Available"
**Claim:** Claude Fable 5 launched June 9, 2026 at $10/$50 per million input/output tokens, included on Pro, Max, Team, and seat-based Enterprise plans through June 22. Anthropic: "capabilities exceed those of any model we've ever made generally available."
**Source:** https://www.anthropic.com/news/claude-fable-5-mythos-5 (Jun 9, 2026)
**Quote:** "capabilities exceed those of any model we've ever made generally available"
**Number:** $10/$50 per M tokens, free through Jun 22
**Confidence:** HIGH (official Anthropic announcement)

### 2. Autonomy Verbs Tied to Harness, Not Raw Model
**Claim:** Anthropic's duration claim is harness-framed verbatim: "Run Claude Fable 5 in an agent harness like Claude Code or Claude Managed Agents, and it can work for days at a time: planning across stages, delegating to sub-agents, and checking its own work." The autonomy verbs are explicitly tied to the harness.
**Source:** https://www.anthropic.com/claude/fable (Jun 9, 2026)
**Quote:** "Run Claude Fable 5 in an agent harness like Claude Code or Claude Managed Agents, and it can work for days at a time: planning across stages, delegating to sub-agents, and checking its own work."
**Number:** "days at a time"
**Confidence:** HIGH (official Anthropic product page)

### 3. Slay the Spire: Persistent File-Based Memory 3x Performance Uplift vs Opus 4.8
**Claim:** Giving Fable 5 access to persistent file-based memory improved its performance three times more than for Opus 4.8; Fable also reached the game's final act three times more often. Two distinct 3x figures.
**Source:** https://www.anthropic.com/news/claude-fable-5-mythos-5 (Jun 9, 2026)
**Quote:** "giving it access to persistent file-based memory improved its performance three times more than for Opus 4.8; Fable also reached the game's final act three times more often"
**Number:** 3x performance improvement, 3x final act reach
**Confidence:** HIGH (official Anthropic announcement)

### 4. Stripe: 50M-Line Ruby Migration in 1 Day vs 2+ Months
**Claim:** In a 50-million-line Ruby codebase, the model performed a codebase-wide migration in a day that would otherwise have taken a whole team over two months by hand.
**Source:** https://www.anthropic.com/news/claude-fable-5-mythos-5 (Jun 9, 2026)
**Quote:** "In a 50-million-line Ruby codebase, the model performed a codebase-wide migration in a day that would otherwise have taken a whole team over two months by hand."
**Number:** 50M lines, 1 day vs 2+ months
**Confidence:** HIGH (official Anthropic announcement, customer case study)

### 5. Dynamic Workflows: JavaScript Orchestration Scripts Running 10s-100s of Subagents
**Claim:** "When a workflow kicks off, Claude plans dynamically based on your prompt, breaks it into subtasks, and fans the work out across subagents running in parallel. Results are checked before they're folded in." A workflow is "an orchestration script Claude writes for your task and runs across many subagents in the background." Dynamic workflows are JavaScript scripts that orchestrate subagents at scale.
**Source:** https://claude.com/blog/introducing-dynamic-workflows-in-claude-code (Jun 9, 2026), https://code.claude.com/docs/en/workflows (Jun 2026)
**Quote:** "A workflow is an orchestration script Claude writes for your task and runs across many subagents in the background"
**Number:** 10s-100s of parallel subagents
**Confidence:** HIGH (official Anthropic blog + docs)

### 6. Claude Code Hooks: 30 Lifecycle Events, 5 Handler Types, Exit Code 2 Blocking
**Claim:** Claude Code has 30 lifecycle events, 5 handler types; exit code 2 is a blocking error whose "stderr text is fed back to Claude as an error message"; Stop hook exit 2 "Prevents Claude from stopping, continues the conversation" — "when is the agent done" is decided by deterministic hook code.
**Source:** https://code.claude.com/docs/en/hooks.md (Apr 1, 2026)
**Quote:** "When a TaskCreated hook exits with code 2, the task is not created and the stderr message is fed back to the model as feedback"; "Exit 2 or decision: 'block' prevents the subagent from stopping"
**Number:** 30 events, 5 handler types, exit code 2 = block
**Confidence:** HIGH (official Anthropic docs)

### 7. Subagents: Independent Context Windows, Custom System Prompts, Tool Allowlists
**Claim:** Subagents each run in their own context window with a custom system prompt, tool allowlists, and independent permissions; model resolution is deterministic: env var → invocation param → frontmatter → main conversation's model; worktree isolation runs them in temporary git worktrees, auto-cleaned if no changes.
**Source:** https://code.claude.com/docs/en/sub-agents (Jun 2026)
**Quote:** "Subagents each run in their own context window with a custom system prompt, tool allowlists, and independent permissions"
**Number:** N/A
**Confidence:** HIGH (official Anthropic docs)

### 8. Agent Teams (Experimental v2.1.32+): Lead + Teammates + Shared Task List + Mailbox
**Claim:** Agent teams = lead + teammates + shared task list + mailbox; "Task claiming uses file locking to prevent race conditions"; quality gates are hook code — TeammateIdle exit 2 keeps a teammate working, TaskCompleted exit 2 blocks completion.
**Source:** https://code.claude.com/docs/en/agent-teams (Jun 2026)
**Quote:** "Task claiming uses file locking to prevent race conditions"; "TeammateIdle exit 2 keeps a teammate working, TaskCompleted exit 2 blocks completion"
**Number:** N/A
**Confidence:** HIGH (official Anthropic docs, experimental flag noted)

### 9. Task Budgets (Beta): Model Sees Running Token Countdown, Self-Moderates
**Claim:** Task Budgets give the model a running token countdown for the whole agentic loop and self-moderates (minimum 20,000 tokens) — distinct from max_tokens, an enforced per-response ceiling the model is NOT aware of.
**Source:** https://platform.claude.com/docs/en/about-claude/models/migration-guide.md (Jun 2026)
**Quote:** "the model sees a running token countdown for the whole agentic loop and self-moderates (minimum 20,000 tokens)"
**Number:** 20,000 token minimum
**Confidence:** HIGH (official Anthropic docs)

### 10. C Compiler Build: 16 Parallel Agents, 2 Weeks, ~2,000 Sessions, $20k, 100k Lines
**Claim:** Over nearly 2,000 Claude Code sessions and $20,000 in API costs, 16 Opus 4.6 agents produced a 100,000-line Rust compiler that builds Linux 6.9 on x86, ARM, RISC-V. Carlini: "Most of my effort went into designing the environment around Claude—the tests, the environment, the feedback" and "it's important that the task verifier is nearly perfect, otherwise Claude will solve the wrong problem."
**Source:** https://www.anthropic.com/engineering/building-c-compiler (Feb 2026)
**Quote:** "Over nearly 2,000 Claude Code sessions and $20,000 in API costs, the agent team produced a 100,000-line compiler that can build Linux 6.9 on x86, ARM, and RISC-V"
**Number:** 16 agents, 2 weeks, ~2,000 sessions, $20k, 100k lines
**Confidence:** HIGH (official Anthropic engineering blog)

---

## Adversarial Verification Notes
- Claim #2 (harness-framed autonomy) directly supports our thesis: autonomy verbs explicitly tied to harness, not model weights.
- Claim #10 (C compiler) — Carlini quote confirms "environment around Claude" (harness) was the engineering effort, not prompt tuning.
- All claims from primary Anthropic sources (announcements, docs, engineering blog) — HIGH confidence.
- Agent Teams marked experimental — noted in confidence.