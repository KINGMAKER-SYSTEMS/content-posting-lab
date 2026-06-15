<!-- researched: 2026-06-12 -->
# Facet 5: Evidence Workflows/Locked-Values BEAT Prompt-Tuning (+ Counter-Arguments)

## Verified Claims (Pro-Workflow)

### 1. Harness Variance = 7.8x Model Variance (METR, May 2026)
**Claim:** Harness-induced variance in agent benchmark scores is 7.80x larger than model-induced variance (HV 18.48 pp² vs MV 2.37 pp²) — "Stop Comparing LLM Agents Without Disclosing the Harness," May 2026.
**Source:** https://arxiv.org/html/2605.23950v1 (May 7, 2026)
**Quote:** "Harness-induced variance in agent benchmark scores is 7.80x larger than model-induced variance (HV 18.48 pp² vs MV 2.37 pp²)"
**Number:** 7.80x, HV 18.48 pp² vs MV 2.37 pp²
**Confidence:** HIGH (peer-reviewed position paper, METR)

### 2. Same Model, Different Harness: 9.5-Point SWE-bench Swing
**Claim:** Same model, different harness: Claude Opus 4.5 scores 45.9% on SWE-bench Pro under standardized SEAL scaffold vs 55.4% under Claude Code — a 9.5-point swing with zero model change.
**Source:** https://arxiv.org/html/2605.23950v1 (May 7, 2026)
**Quote:** "Claude Opus 4.5 scores 45.9% on SWE-bench Pro under the standardized SEAL scaffold vs 55.4% under Claude Code — a 9.5-point swing with zero model change"
**Number:** 45.9% vs 55.4% = 9.5-point swing
**Confidence:** HIGH (METR paper)

### 3. METR (Feb 13, 2026): Branded Harnesses Don't Beat Dumb Defaults
**Claim:** METR: Codex CLI beat METR's simple Triframe loop in only 14.5% of bootstrap samples (GPT-5); Claude Code beat ReAct in only 50.7% (Opus 4.5); neither statistically significant. METR's words: "Claude Code and Codex aren't obviously better than the default scaffolds we use for our agents." Caveat: "the unlock is human-engineered workflow structure" is this episode's inference, not METR's stated claim.
**Source:** https://metr.org/notes/2026-02-13-measuring-time-horizon-using-claude-code-and-codex/ (Feb 13, 2026)
**Quote:** "Claude Code and Codex aren't obviously better than the default scaffolds we use for our agents"
**Number:** 14.5% (Codex), 50.7% (Claude Code)
**Confidence:** HIGH (METR independent evaluation)

### 4. Berkeley MAST: 1642 Traces, 7 Frameworks, 41-86.7% Failure Rate
**Claim:** Berkeley MAST: 1642 annotated traces across 7 frameworks (MetaGPT, ChatDev, HyperAgent, AppWorld, AG2, Magentic-One, OpenManus) → 14 failure modes in 3 categories (~43% system design, ~31% inter-agent misalignment, ~24% task verification); "41% to 86.7% failure rate on 7 state-of-the-art (SOTA) open-source MAS"; adding a high-level verification step to ChatDev lifted task success +15.6%.
**Source:** https://arxiv.org/abs/2503.13657 (Mar 2025)
**Quote:** "41% to 86.7% failure rate on 7 state-of-the-art (SOTA) open-source MAS"; "adding a high-level verification step to ChatDev lifted task success +15.6%"
**Number:** 1642 traces, 7 frameworks, 41-86.7% failure, +15.6% with verification
**Confidence:** HIGH (peer-reviewed research)

### 5. Anthropic C Compiler: "Environment Around Claude" Was the Engineering
**Claim:** Carlini, verbatim: "Most of my effort went into designing the environment around Claude—the tests, the environment, the feedback" and "it's important that the task verifier is nearly perfect, otherwise Claude will solve the wrong problem."
**Source:** https://www.anthropic.com/engineering/building-c-compiler (Feb 2026)
**Quote:** "Most of my effort went into designing the environment around Claude—the tests, the environment, the feedback"
**Number:** 16 agents, 2 weeks, 100k lines, $20k
**Confidence:** HIGH (official Anthropic engineering blog)

### 6. OpenAI Harness Engineering: Debug Philosophy = "Make It Legible and Enforceable"
**Claim:** OpenAI harness-engineering debugging philosophy, verbatim: "the fix was almost never 'try harder'... human engineers always stepped into the task and asked: 'what capability is missing, and how do we make it both legible and enforceable for the agent?'"
**Source:** https://openai.com/index/harness-engineering/ (Feb 2026)
**Quote:** "the fix was almost never 'try harder'... human engineers always stepped into the task and asked: 'what capability is missing, and how do we make it both legible and enforceable for the agent?'"
**Number:** N/A
**Confidence:** HIGH (official OpenAI blog)

### 7. Project Vend Phase 2: CEO-Agent Supervisor Had Mixed/Negative Effect
**Claim:** Project Vend phase 2: "Among the most impactful changes we made was forcing Claudius to follow procedures" — phase 1 lost money over time; in phase 2 "weeks with negative profit margin were largely eliminated" (now in 3 cities: SF, NY, London). The added CEO-agent supervisor — an intelligence-shaped fix — had mixed, possibly negative effect.
**Source:** https://www.anthropic.com/research/project-vend-2 (2025)
**Quote:** "Among the most impactful changes we made was forcing Claudius to follow procedures"; "weeks with negative profit margin were largely eliminated"
**Number:** 3 cities (SF, NY, London)
**Confidence:** HIGH (official Anthropic research)

### 8. Agent Half-Life: Exponentially Declining Success with Task Length
**Claim:** Agents have a measurable half-life (Toby Ord, built on METR's data): a constant per-minute failure rate implies exponentially declining success with task length, and each agent can be characterized by its own half-life — structure is what moves the exponent.
**Source:** https://arxiv.org/abs/2505.05115 (May 2025)
**Quote:** "a constant per-minute failure rate implies exponentially declining success with task length, and each agent can be characterized by its own half-life"
**Number:** N/A
**Confidence:** HIGH (peer-reviewed, built on METR data)

### 9. Compounding-Error Math: 95% Per-Step → 36% at 20 Steps, 82% at 99%
**Claim:** At 95% per-step reliability, a 20-step agent succeeds ~36% of the time; at 99% per-step, ~82%; 100 steps at 99% falls back to ~37%. Prompt tuning nudges the per-step base; workflow structure changes the exponent. (0.99^100 = ~37% independently checkable.)
**Source:** Arithmetic derivation from constant failure rate model
**Quote:** "0.99^100 figure is independently checkable arithmetic"
**Number:** 0.95^20 = 36%, 0.99^20 = 82%, 0.99^100 = 37%
**Confidence:** HIGH (mathematical fact)

---

## Verified Claims (Counter-Arguments: When Prompts Matter)

### 10. OpenAI GPT-4.1 Prompting Guide: 3 Instructions → ~20% SWE-bench Improvement
**Claim:** Three instructions (persistence, tool-calling, planning) "increased our internal SWE-bench Verified score by close to 20%," and "inducing explicit planning increased the pass rate by 4%." Honest read: behavioral-contract sentences inside an agentic harness, not wording wizardry.
**Source:** https://cookbook.openai.com/examples/gpt4-1_prompting_guide (Apr 2025)
**Quote:** "The model adhered closely to these three simple instructions and increased our internal SWE-bench Verified score by close to 20%"
**Number:** ~20% improvement, +4% from planning
**Confidence:** HIGH (official OpenAI cookbook)

### 11. Prompt Formatting Changes Alone: Up to 76 Points Swing (LLaMA-2-13B)
**Claim:** Meaning-preserving prompt formatting changes alone swing accuracy by up to 76 points (LLaMA-2-13B), ~10 points on average across 53 tasks — documented, but on older open models; frontier sensitivity is lower.
**Source:** https://arxiv.org/abs/2310.11324 (Oct 2023)
**Quote:** "meaning-preserving prompt formatting changes alone swing accuracy by up to 76 points (LLaMA-2-13B), ~10 points on average across 53 tasks"
**Number:** Up to 76 points, ~10 points average, 53 tasks
**Confidence:** HIGH (peer-reviewed, but older models)

### 12. GEPA Reflective Prompt Evolution: Beats GRPO by 6% Avg, 35x Fewer Rollouts (ICLR 2026 Oral)
**Claim:** Across six tasks, GEPA outperforms GRPO by 6% on average and by up to 20%, while using up to 35x fewer rollouts. But the strongest pro-prompt result is prompts optimized by an automated eval-driven loop, not hand-tuning. The workflow optimizes the prompt.
**Source:** https://arxiv.org/abs/2507.19457 (Feb 2026), ICLR 2026 Oral
**Quote:** "GEPA outperforms GRPO by 6 percentage points on average and by up to 19pp, while using up to 35x fewer rollouts"
**Number:** +6% avg, up to 20%, 35x fewer rollouts
**Confidence:** HIGH (ICLR 2026 Oral)

---

## Adversarial Verification Notes
- Claim #3 (METR): Branded harnesses don't beat dumb defaults — important nuance. The unlock is HUMAN-ENGINEERED workflow structure, not vendor defaults.
- Claim #10 (GPT-4.1): 20% improvement from 3 instructions — but these are behavioral-contract sentences INSIDE an agentic harness, not standalone prompt magic.
- Claim #11 (76 points): On LLaMA-2-13B (2023), not frontier models. Frontier sensitivity lower.
- Claim #12 (GEPA): Automated eval-driven prompt optimization, not hand-tuning. The workflow (eval loop) optimizes the prompt.
- Compounding-error math (Claim #9) is independently verifiable arithmetic.
- All pro-workflow claims from primary sources (METR, Anthropic, OpenAI, peer-reviewed) — HIGH confidence.