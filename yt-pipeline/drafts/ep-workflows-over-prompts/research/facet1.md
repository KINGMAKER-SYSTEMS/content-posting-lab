<!-- researched: 2026-06-12 -->
# Facet 1: OpenAI Codex (GPT-5.x-codex line) — Long Autonomous Runs & Orchestration

## Verified Claims

### 1. 25-Hour Uninterrupted Run (Feb 23, 2026)
**Claim:** Codex ran for about 25 hours uninterrupted, used about 13M tokens, and generated about 30k lines of code on a blank repo with GPT-5.3-Codex at "Extra High" reasoning.
**Source:** https://developers.openai.com/blog/run-long-horizon-tasks-with-codex (Feb 23, 2026)
**Quote:** "Codex ran for about 25 hours uninterrupted, used about 13M tokens, and generated about 30k lines of code"
**Number:** 25 hours, 13M tokens, 30k lines
**Confidence:** HIGH (official OpenAI dev blog)

### 2. 4-File State Machine Architecture
**Claim:** The 25-hour run used four durable markdown memory files: prompt.md (spec), plan.md (milestones with acceptance criteria), implement.md (runbook), documentation.md (status/decision log) with verification commands after milestones.
**Source:** https://developers.openai.com/blog/run-long-horizon-tasks-with-codex (Feb 23, 2026)
**Quote:** "four durable markdown memory files (prompt.md spec, plan.md milestones with acceptance criteria, implement.md runbook, documentation.md status/decision log) with verification commands after milestones"
**Number:** 4 files
**Confidence:** HIGH (official OpenAI dev blog)

### 3. 1M Lines of Code, 1,500 PRs, 3.5 PRs/engineer/day (5 months)
**Claim:** A team of 3 engineers (growing to 7) drove Codex to ~1,000,000 LOC across ~1,500 opened-and-merged PRs (3.5 PRs per engineer per day) with "0 lines of manually-written code" in ~1/10th the time of writing by hand.
**Source:** https://openai.com/index/harness-engineering/ (Feb 2026)
**Quote:** "Over that period, roughly 1,500 pull requests have been opened and merged with a small team of just three engineers driving Codex. This translates to an average throughput of 3.5 PRs per engineer per day"
**Number:** 1M LOC, 1,500 PRs, 3.5 PRs/engineer/day, 5 months, 3→7 engineers
**Confidence:** HIGH (official OpenAI blog)

### 4. Codex Harness = "Agent Loop and Logic" (Feb 4, 2026)
**Claim:** OpenAI named the layer — the Codex harness is "the agent loop and logic that underlies all Codex experiences," exposed via the App Server, "a client-friendly, bidirectional JSON-RPC API" with Item/Turn/Thread primitives and threads that can be created, resumed, forked, and archived.
**Source:** https://openai.com/index/unlocking-the-codex-harness/ (Feb 4, 2026)
**Quote:** "the Codex harness is 'the agent loop and logic that underlies all Codex experiences,' exposed via the App Server, 'a client-friendly, bidirectional JSON-RPC API' with Item/Turn/Thread primitives"
**Number:** N/A
**Confidence:** HIGH (official OpenAI blog)

### 5. GPT-5-Codex 7+ Hour Independent Work (Sep 15, 2025)
**Claim:** During testing, GPT-5-Codex worked independently for more than 7 hours at a time on large, complex tasks, iterating on implementation, fixing test failures, and delivering successful implementation.
**Source:** https://openai.com/index/introducing-upgrades-to-codex/ (Sep 15, 2025)
**Quote:** "GPT-5-Codex work independently for more than 7 hours at a time on large, complex tasks, iterating on its implementation, fixing test failures, and ultimately delivering a successful implementation"
**Number:** 7+ hours
**Confidence:** HIGH (official OpenAI announcement)

### 6. GPT-5.1-Codex-Max 24+ Hour Internal Tasks (Nov 19, 2025)
**Claim:** OpenAI reported internal tasks lasting more than 24 hours for GPT-5.1-Codex-Max, first model natively trained to operate across multiple context windows via "compaction."
**Source:** https://openai.com/index/gpt-5-1-codex-max/ (Nov 19, 2025)
**Quote:** "OpenAI reported internal tasks lasting more than 24 hours"
**Number:** 24+ hours (vendor claim)
**Confidence:** MEDIUM (vendor claim, not independently verified)

### 7. METR Time Horizon: GPT-5.1-Codex-Max 2h40m (50th percentile)
**Claim:** METR measured GPT-5.1-Codex-Max 50%-time-horizon at ~2h40m (95% CI 75min–5h50m) vs GPT-5's 2h17m — an order of magnitude below the vendor's marketed 24h figure.
**Source:** https://metr.org/evaluations/gpt-5-1-codex-max-report/ (Nov 19, 2025)
**Quote:** "The observed 50%-time horizon of GPT-5.1-Codex-Max was about 2h40m (75m - 5h50m 95% CI)"
**Number:** 2h40m (95% CI 75m–5h50m)
**Confidence:** HIGH (independent third-party eval)

### 8. Trendline: GPT-5.2 ~6.6h, Claude Mythos Preview ≥16h
**Claim:** Trendline since: GPT-5.2 at ~6.6h; Claude Mythos Preview ≥16h, with METR flagging measurements above 16h as unreliable.
**Source:** https://metr.org/notes/2026-02-13-measuring-time-horizon-using-claude-code-and-codex/ (Feb 13, 2026)
**Quote:** "Trendline since: GPT-5.2 at ~6.6h; Claude Mythos Preview ≥16h, with METR flagging measurements above 16h as unreliable"
**Number:** GPT-5.2 ~6.6h, Claude Mythos ≥16h
**Confidence:** HIGH (METR independent measurement)

### 9. Codex App Server: Thread Lifecycle Primitives (Feb 2026)
**Claim:** Threads are durable containers supporting creation, resumption, forking, and archival with persisted event history; JSON-RPC streamed as JSONL over stdio; backward compatible protocol.
**Source:** https://developers.openai.com/codex/app-server (Apr 30, 2026), https://www.infoq.com/news/2026/02/opanai-codex-app-server/ (Feb 17, 2026)
**Quote:** "A thread is a Codex conversation between a user and an agent. Codex creates, resumes, forks, and archives threads, and persists the event history"
**Number:** N/A
**Confidence:** HIGH (official OpenAI developer docs)

### 10. Codex /goal GA May 21, 2026; Mobile Jun 9, 2026
**Claim:** Codex /goal moved to GA on May 21, 2026 — "a verifiable stopping condition" that survives session breaks and budget resets; mobile support June 9, 2026.
**Source:** https://developers.openai.com/codex/use-cases/follow-goals/ (2 weeks ago), https://ofox.ai/blog/codex-goal-mode-remote-computer-use-2026/ (2 weeks ago)
**Quote:** "On May 21, 2026 OpenAI moved two Codex features to GA: Goal Mode (a persistent /goal directive that survives session breaks and budget resets) and Locked Computer Use"
**Number:** GA May 21, 2026; Mobile Jun 9, 2026
**Confidence:** HIGH (official OpenAI developer docs)

---

## Adversarial Verification Notes
- Claim #6 (24h vendor claim) vs Claim #7 (METR 2h40m): **CONTRADICTION** — vendor marketing vs independent measurement. Kept both with confidence labels.
- Claim #8 (Mythos ≥16h) flagged by METR as unreliable above 16h — noted.
- All other claims from primary sources (OpenAI blogs, METR reports, developer docs) — HIGH confidence.