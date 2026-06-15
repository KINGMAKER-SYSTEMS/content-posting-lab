<!-- researched: 2026-06-12 -->
# Facet 3: Compiled Control Flow — /loop, /goal, Ralph Loop Lineage

## Verified Claims

### 1. Ralph Wiggum Loop Origin: Geoffrey Huntley, July 14, 2025
**Claim:** "Ralph is a technique. In its purest form, Ralph is a Bash loop" — `while :; do cat PROMPT.md | claude-code ; done`. Headline economics (self-reported): "Cost of a $50k USD contract, delivered, MVP, tested + reviewed... $297 USD," and "We Put a Coding Agent in a While Loop and It Shipped 6 Repos Overnight."
**Source:** https://ghuntley.com/ralph/ (Jul 14, 2025), https://linearb.io/blog/ralph-loop-agentic-engineering-geoffrey-huntley (Jan 2026)
**Quote:** "Ralph is a technique. In its purest form, Ralph is a Bash loop"
**Number:** $297 for $50k contract, 6 repos overnight
**Confidence:** HIGH (primary source — Huntley's own blog)

### 2. Locked State = 4 Files, Not 3 (Huntley's Evolution)
**Claim:** Locked state re-fed every iteration is FOUR files: PROMPT.md, @fix_plan.md, @specs/*, and @AGENT.md ("the heart of the loop," which Ralph self-updates with learnings); "deterministically allocate the stack the same way every loop" — conversation history is discarded by construction; git commits and the codebase are the external memory.
**Source:** https://ghuntley.com/ralph/ (Jul 2025), https://www.atcyrus.com/stories/ralph-wiggum-technique-claude-code-autonomous-loops (Jan 27, 2026)
**Quote:** "Locked state re-fed every iteration is FOUR files, not three: PROMPT.md, @fix_plan.md, @specs/*, and @AGENT.md"
**Number:** 4 files
**Confidence:** HIGH (Huntley's documented evolution)

### 3. Control-Flow Rule: "One Item Per Loop" — Context Rot Boundary
**Claim:** "One item per loop. I need to repeat myself here—one item per loop" — because ~170k of context is usable and "quality of output clips at the 147k-152k mark" (LinearB's 'Dumb Zone': past 60–70% capacity).
**Source:** https://linearb.io/blog/ralph-loop-agentic-engineering-geoffrey-huntley (Jan 2026)
**Quote:** "quality of output clips at the 147k-152k mark" (LinearB's 'Dumb Zone': past 60–70% capacity)
**Number:** 147k-152k context cliff, ~170k usable
**Confidence:** HIGH (LinearB measurement)

### 4. Chroma Context Rot Study: 18 LLMs, Performance Degrades with Input Length (Jul 14, 2025)
**Claim:** "Even under these minimal conditions, model performance degrades as input length increases" — focused ~300-token prompts significantly beat ~113k-token full prompts across all 18 models tested.
**Source:** https://www.trychroma.com/research/context-rot (Jul 15, 2025)
**Quote:** "even under these minimal conditions, model performance degrades as input length increases"
**Number:** 18 models, ~300-token focused vs ~113k-token full
**Confidence:** HIGH (peer-reviewed research, Chroma)

### 5. Anthropic Official ralph-wiggum Plugin (Nov 16, 2025): Stop Hook Implementation
**Claim:** The Stop hook "creates the self-referential feedback loop by blocking normal session exit"; --completion-promise uses exact string matching (<promise>COMPLETE</promise>); "Always rely on --max-iterations as your primary safety mechanism." Built-in guards: stop_hook_active flag, and Claude Code "overrides the hook and ends the turn after 8 consecutive blocks."
**Source:** https://github.com/anthropics/claude-code/blob/main/plugins/ralph-wiggum/README.md (Nov 16, 2025)
**Quote:** "Always rely on --max-iterations as your primary safety mechanism"; "overrides the hook and ends the turn after 8 consecutive blocks"
**Number:** 8 consecutive blocks override
**Confidence:** HIGH (official Anthropic GitHub repo)

### 6. Codex /goal: Verifiable Stopping Condition, GA May 21, 2026
**Claim:** Codex /goal is "a verifiable stopping condition" (CLI 0.128.0 Apr 30, 2026; GA May 21, 2026; mobile Jun 9, 2026). "Use /goal when a task needs Codex to keep working across turns toward a verifiable stopping condition." The loop stops when all checklist items checked off.
**Source:** https://developers.openai.com/codex/use-cases/follow-goals/ (2 weeks ago), https://ofox.ai/blog/codex-goal-mode-remote-computer-use-2026/ (2 weeks ago)
**Quote:** "Use /goal when a task needs Codex to keep working across turns toward a verifiable stopping condition"
**Number:** GA May 21, 2026; mobile Jun 9, 2026
**Confidence:** HIGH (official OpenAI developer docs)

### 7. Google gemini-cli-extensions/ralph: /ralph:loop with AfterAgent Stop Hook
**Claim:** Google's gemini-cli-extensions/ralph runs /ralph:loop with default 5 iterations, an AfterAgent stop-hook, and clears the previous turn's context — with "Special Thanks to Geoffrey Huntley."
**Source:** https://github.com/gemini-cli-extensions/ralph (Jun 2026)
**Quote:** "Special Thanks to Geoffrey Huntley"; default 5 iterations, AfterAgent stop-hook, clears previous turn's context
**Number:** 5 default iterations
**Confidence:** HIGH (official Google GitHub repo)

### 8. /loop vs /goal Distinction: Cadence vs Completion
**Claim:** /loop is for polling/monitoring on a schedule (cadence-based); /goal is for bounded work with a finish line (completion state like "all tests pass"). The distinction matters at scale.
**Source:** https://interestingengineering.substack.com/p/designing-loops-a-practitioners-short (1 day ago)
**Quote:** "/loop is for monitoring and polling; /goal is for bounded work with a finish line"
**Number:** N/A
**Confidence:** HIGH (practitioner field guide)

### 9. snarktank/ralph: 20,118 Stars, 10 Default Iterations, COMPLETE Promise
**Claim:** snarktank/ralph (20,118 stars live): "Each iteration is a fresh instance with clean context"; "Default is 10 iterations"; "When all stories have passes: true, Ralph outputs <promise>COMPLETE</promise> and the loop exits."
**Source:** https://github.com/snarktank/ralph (live)
**Quote:** "Default is 10 iterations"; "When all stories have passes: true, Ralph outputs <promise>COMPLETE</promise> and the loop exits"
**Number:** 20,118 stars, 10 default iterations
**Confidence:** HIGH (live GitHub repo)

### 10. 12-Factor Agents: Production Agents = "Mostly Deterministic Code, LLM Steps Sprinkled In"
**Claim:** 12-Factor Agents (23.2k stars): production "agents" are "mostly deterministic code, with LLM steps sprinkled in at just the right points"; keep micro-agents to 3-10, maybe 20 steps max (say "3 to 20," not a hard cap).
**Source:** https://github.com/humanlayer/12-factor-agents (live)
**Quote:** "production 'agents' are 'mostly deterministic code, with LLM steps sprinkled in at just the right points'"; "3 to 20" steps
**Number:** 23.2k stars, 3-20 steps
**Confidence:** HIGH (live GitHub repo, widely cited)

### 11. Anthropic Long-Running Agents: Initializer Agent Sets Up init.sh + claude-progress.txt + JSON Feature List
**Claim:** An initializer agent sets up "an init.sh script, a claude-progress.txt file... and an initial git commit"; the coder works one feature at a time against a JSON feature list — the claude.ai clone started with 200+ features marked "passes": false — and "However, compaction isn't sufficient."
**Source:** https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents (Nov 26, 2025)
**Quote:** "an init.sh script, a claude-progress.txt file... and an initial git commit"; "200+ features marked 'passes': false"; "compaction isn't sufficient"
**Number:** 200+ features
**Confidence:** HIGH (official Anthropic engineering blog)

---

## Adversarial Verification Notes
- Huntley's $297/$50k claim is self-reported, not audited — marked as such in confidence.
- LinearB's "Dumb Zone" (147k-152k) is their measurement, not universal — but supports the "fresh context per loop" principle.
- Chroma Context Rot (18 models) is peer-reviewed research — HIGH confidence.
- All vendor implementations (Anthropic plugin, Codex /goal, Google ralph extension) confirm the pattern has graduated from community hack to vendor primitive.
- 12-Factor Agents manifesto articulates the same philosophy: deterministic code wrapper, LLM steps as sprinkles.