# Facet 4 — JSON Schemas as Contracts/Ledgers Between Agent Pipeline Stages

> Episode: "Fable 5 runs for hours unattended. The model is not why."
> Researched: 2026-06-10. All sources fetched/verified today unless noted.
> Theme: structured outputs enforcement, schema-validated handoffs, determinism in multi-agent systems.

---

## 1. Vendor enforcement: constrained decoding makes the schema a hard guarantee

### OpenAI — Structured Outputs (Aug 6, 2024)
- Source: https://openai.com/index/introducing-structured-outputs-in-the-api/ (direct fetch 403'd; numbers verified via multiple secondary sources quoting announcement, incl. https://wal.sh/research/gpt-4o-2024-08-06.html and https://www.aibase.com/news/10859)
- **NUMBERS: 100% vs <40% vs 93%.** "With Structured Outputs, gpt-4o-2024-08-06 achieves 100% reliability in our evals, perfectly matching the output schemas." Old gpt-4-0613 with prompting alone: **less than 40%** on the same complex-schema eval. Training alone got the new model to **93%** — the last 7 points came from a "deterministic, engineering-based approach" (constrained decoding), not from a smarter model.
- KEY FRAMING FOR EPISODE: even OpenAI's own story is "the model got us to 93; the engineering harness got us to 100." The model is not why.
- Confidence: HIGH (numbers widely and consistently quoted; primary page geo-blocked to fetcher).

### Anthropic — Structured Outputs on Claude (beta Nov 2025 → GA)
- Source: https://platform.claude.com/docs/en/build-with-claude/structured-outputs (fetched 2026-06-10)
- Verbatim: "Structured outputs guarantee schema-compliant responses through constrained decoding: Always valid: No more `JSON.parse()` errors. Type safe: Guaranteed field types and required fields. Reliable: No retries needed for schema violations."
- Verbatim: "Structured outputs work by compiling your JSON schemas into a grammar that constrains Claude's output."
- Two features: JSON outputs (`output_config.format`) for final response; **strict tool use (`strict: true`)** for tool-call inputs — i.e., both ends of a handoff can be schema-locked.
- Old beta header `structured-outputs-2025-11-13` deprecated; now GA on Claude API for Fable 5, Mythos 5, Opus 4.8/4.7/4.6/4.5, Sonnet 4.6/4.5, Haiku 4.5. Also on Bedrock/Vertex for most.
- **NUMBERS:** compiled grammars cached **24 hours** from last use; limits: **20** strict tools/request, **24** optional params total, **16** union-typed params, **180 s** grammar compilation timeout.
- **Escape hatches (the guarantee is not absolute):** output can still violate schema on (1) safety refusals (`stop_reason: "refusal"` — refusals take precedence over grammar) and (2) `max_tokens` truncation. Also unsupported JSON Schema features: no recursive schemas, no `minimum`/`maximum`, no `minLength`/`maxLength`, `additionalProperties` must be `false`.
- Confidence: HIGH (primary vendor doc).

### Academic foundation — Outlines / FSM-guided generation
- Source: https://arxiv.org/abs/2307.09702 (Willard & Louf, "Efficient Guided Generation for Large Language Models", 2023)
- Verbatim: "the problem of neural text generation can be constructively reformulated in terms of transitions between the states of a finite-state machine." Framework allows "guaranteeing the structure of the generated text" via regex and context-free grammars, building "an index over a language model's vocabulary," model-agnostic, minimal overhead. Open-sourced as the Outlines library.
- This is the paper underneath everyone's "the model literally cannot emit an invalid token" claim.
- Confidence: HIGH.

### Overhead of grammar checking (visual-friendly numbers — verified by direct fetch)
- Source: https://letsdatascience.com/blog/structured-outputs-making-llms-return-reliable-json (Feb 11, 2026; fetched 2026-06-10)
- Verbatim: "Modern engines add under 50 microseconds per token for grammar checking, negligible next to the model's 10 to 50 millisecond inference time per token."
- **NUMBERS: <50 µs/token grammar masking vs 10–50 ms/token inference** (~200–1000x gap — the contract is essentially free at runtime). Also: XGrammar and llguidance both under **40 µs/token**; XGrammar "up to **100x speedup** over traditional grammar-constrained methods"; **99%** of vocabulary tokens are context-independent (precomputable masks) vs **1%** context-dependent.
- Confidence: MEDIUM (practitioner explainer, but consistent with the XGrammar/llguidance papers it cites).

### OpenAI docs (primary, fetched 2026-06-10) — the guarantee in vendor language
- Source: https://developers.openai.com/api/docs/guides/structured-outputs
- Verbatim: "Structured Outputs is a feature that ensures the model will always generate responses that adhere to your supplied JSON Schema, so you don't need to worry about the model omitting a required key, or hallucinating an invalid enum value."
- Comparison table: JSON mode gives valid JSON but **no** schema adherence; Structured Outputs gives both. "We recommend always using Structured Outputs instead of JSON mode when possible."
- Listed benefits: reliable type-safety (no validation/retry loops), explicit refusals (programmatically detectable), simpler prompting.
- Confidence: HIGH.

---

## 2. The counterpoint: schema conformance ≠ answer quality

### BAML — "Structured Outputs Create False Confidence"
- Source: https://boundaryml.com/blog/structured-outputs-create-false-confidence (updated Dec 21; fetched 2026-06-10)
- Core thesis: constrained decoding forces models to "prioritize complying with your output format over returning a high-quality response."
- Banana-receipt example: GPT-5.2 returned quantity `1` under structured outputs but the correct `0.46` (kg) with free-form output + schema-aligned parsing.
- **NUMBERS (BFCL benchmark, gpt-4o): 93.63%** accuracy with schema-aligned parsing of free-form output vs **91.37%** with constrained "Function Calling Strict."
- Other claims: constrained decoding can't refuse garbage input (elephant photo as receipt → still emits a valid-looking receipt), and JSON-escaped reasoning fields degrade chain-of-thought.
- Use in episode as the honest tension: the contract guarantees the envelope, not the letter. You still need verification stages — which is itself an argument for pipeline/workflow design.
- Confidence: HIGH that the source makes these claims; MEDIUM on generalizability (vendor of a competing approach — BAML sells schema-aligned parsing).

---

## 3. Multi-agent failure literature: handoffs are where systems die

### MAST — "Why Do Multi-Agent LLM Systems Fail?" (arXiv 2503.13657, UC Berkeley et al.)
- Sources: https://arxiv.org/abs/2503.13657 and https://arxiv.org/html/2503.13657v3 (fetched 2026-06-10)
- **NUMBERS:**
  - "Our empirical analysis reveals **41% to 86.7% failure rate** on 7 state-of-the-art (SOTA) open-source MAS."
  - MAST-Data: **1,642 annotated traces** (paper headline: "1600+") across **7** popular MAS frameworks; **14** unique failure modes in **3** categories; inter-annotator agreement **kappa = 0.88**.
  - Category split: **FC1 system design issues 44.0%**, **FC2 inter-agent misalignment 32.15%**, **FC3 task verification 23.85%** of failures.
  - Interventions: ChatDev **+9.4%** overall task success from workflow adjustments; **+15.6%** on ProgramDev from adding verification steps.
- Reading for the episode: roughly a third of multi-agent failure is literally agents mis-handing things to each other, and ~44% more is bad system design — i.e., >3/4 of failures are harness problems, not model problems. (Careful phrasing: that's the taxonomy of failures observed, not causal attribution.)
- Confidence: HIGH (peer-style arXiv paper; exact percentages from v3 HTML figure).

### GitHub Blog — "Multi-agent workflows often fail. Here's how to engineer ones that don't."
- Source: https://github.blog/ai-and-ml/generative-ai/multi-agent-workflows-often-fail-heres-how-to-engineer-ones-that-dont/ (Gwen Davis, Feb 24, 2026; fetched 2026-06-10)
- Verbatim: "**Typed schemas are table stakes in multi-agent workflows. Without them, nothing else works.**"
- Verbatim: "Most agent failures are action failures."
- Root causes named: "shared state, ordering assumptions, implicit handoffs, and non-deterministic behavior"; data drift between agents ("field names change, data types don't match, formatting shifts").
- Prescription: "treat agents like code, not chat interfaces" — typed schemas, constrained action schemas, MCP-enforced contracts that validate inputs/outputs before execution.
- Confidence: HIGH (primary fetch, verbatim quotes).

---

## 4. Schemas as inter-stage contracts (the microservices analogy, formalized)

### TianPan.co — "Contract Testing for AI Pipelines: Schema-Validated Handoffs Between AI Components"
- Source: https://tianpan.co/blog/2026-04-20-contract-testing-ai-pipelines (Apr 20, 2026; fetched 2026-06-10)
- Verbatim: "The average enterprise AI system experiences nearly **five pipeline failures per month** — each taking **over twelve hours** to resolve. The dominant cause isn't poor model quality. It's data quality and schema contract violations: **64% of AI risk lives at the schema layer**."
- Verbatim: "Unlike an API breaking change that throws a 422 and fails loudly, schema drift in AI pipelines often produces outputs that parse without error." (silent failure mode)
- Verbatim: "Naive JSON prompting fails **15–20%** of the time under production load; constrained decoding pushes that to **near-zero**."
- Method: adapt consumer-driven contract testing from microservices — downstream stages declare required schemas first; upstream stages verify compliance in CI; test shape (field exists, correct type), never exact values, because model outputs legitimately vary.
- Confidence: MEDIUM (practitioner blog; the 64%/5-failures/12-hours stats appear to summarize an industry report not independently verified here; the framing is excellent regardless).

### OpenAI Agents SDK — handoffs are typed tool calls
- Source: https://openai.github.io/openai-agents-python/handoffs/ (fetched 2026-06-10)
- Verbatim: "Handoffs are represented as tools to the LLM. So if there's a handoff to an agent named `Refund Agent`, the tool would be called `transfer_to_refund_agent`."
- Verbatim (on `input_type`): "The SDK exposes that schema to the model as the handoff tool's `parameters`, **validates the returned JSON locally**, and passes the parsed value to `on_handoff`." (e.g., a Pydantic `EscalationData{reason: str}` model — the baton passed between agents is itself schema-validated)
- Plus `input_filter` / `HandoffInputData`: deterministic, code-level control over exactly what conversation state crosses the handoff boundary (e.g., strip all tool calls from history).
- Confidence: HIGH (primary vendor doc).

---

## 5. The "ledger" pattern — explicit state objects instead of vibes

### Microsoft Magentic-One — Task Ledger + Progress Ledger
- Source: https://www.microsoft.com/en-us/research/articles/magentic-one-a-generalist-multi-agent-system-for-solving-complex-tasks/ (fetched 2026-06-10)
- The Orchestrator agent maintains two explicit ledgers:
  - **Task Ledger**: "facts, guesses, and the current plan" — the durable contract of what's known.
  - **Progress Ledger**: "current progress, task assignment to agents" — the Orchestrator "creates a Progress Ledger where it self-reflects on task progress and checks whether the task is completed," updating it after each agent completes a subtask.
- **NUMBER: stall count > 2** — if no progress for more than 2 consecutive checks, the outer loop fires: "it can update the Task Ledger and create a new plan." The ledger, not the model, is what makes long unattended runs recoverable.
- Benchmarks: statistically comparable to prior SOTA on GAIA and AssistantBench (human reference: GAIA ~92%, WebArena ~78%).
- Confidence: HIGH (primary Microsoft Research page).

### Anthropic — "Building Effective Agents" (Dec 19, 2024)
- Source: https://www.anthropic.com/engineering/building-effective-agents (fetched 2026-06-10)
- Verbatim definitions: workflows are "systems where LLMs and tools are orchestrated through **predefined code paths**"; agents "dynamically direct their own processes."
- Verbatim: "workflows offer **predictability and consistency** for well-defined tasks, whereas agents are the better option when flexibility and model-driven decision-making are needed at scale."
- Prompt chaining pattern: "each LLM call processes the output of the previous one," with "**programmatic checks (see 'gate')**" between steps — i.e., schema/validation gates between pipeline stages, straight from the vendor's canonical agents essay.
- Warns of agents' "potential for compounding errors" → sandboxing + guardrails.
- Confidence: HIGH (primary, and the founding text of the workflows-over-prompts argument).

---

## 6. Weakly-sourced but quotable practitioner numbers (use with care)

### Agentmelt — structured output vs text-parsing action success
- Source: https://agentmelt.com/blog/ai-agent-structured-output-guide/ (Max Zeshut, June 9, 2026 — yesterday; fetched 2026-06-10)
- Verbatim: "agents that use structured output have **95-99% action success rates** compared to **70-85%** for agents that rely on parsing unstructured text."
- Also: parsing-based production agents fail on formatting errors in "30–60%" of cases; structured output + validation + retries reduces downstream crashes "90%+"; function-calling validation retry latency 50–200 ms.
- **NO CITATIONS given for any of these.** Treat as practitioner folklore — directionally consistent with OpenAI's 100-vs-<40 eval, but do not present as research.
- Confidence: LOW.

---

## 7. Second-pass additions (2026-06-10, independent verification round)

### Research: "Talk Freely, Execute Strictly: Schema-Gated Agentic AI" (arXiv 2603.06394, Mar 6, 2026)
- Source: https://arxiv.org/abs/2603.06394 (fetched 2026-06-10)
- Semi-structured interviews with **18 experts across 10 industrial R&D organizations**; **20 systems** evaluated across **5 architectural groups**; **15 independent sessions, 3 LLM families**; inter-model agreement Krippendorff's **α=0.80** (execution determinism) and **α=0.98** (conversational flexibility).
- Empirical **Pareto front**: no surveyed system achieves both high deterministic execution AND high conversational flexibility; a "convergence zone" exists between generative and workflow-centric extremes.
- Core principle, verbatim: "nothing runs unless the complete action — including cross-step dependencies — validates against a machine-checkable specification." Schema gating at the WORKFLOW level, not just per-tool.
- Three operational principles: clarification-before-execution, constrained plan-act orchestration, tool-to-workflow-level gating.
- Confidence: HIGH (primary research paper, current-year).

### The format-restriction dispute, both sides with numbers
- "Let Me Speak Freely?" (https://arxiv.org/abs/2408.02442, Aug 2024): "we observe a significant decline in LLMs reasoning abilities under format restrictions"; stricter constraint → bigger degradation. The canonical anti-structure citation. Confidence HIGH that paper claims this.
- dottxt rebuttal "Say What You Mean" (https://blog.dottxt.ai/say-what-you-mean.html): re-ran with corrected methodology, structured WON every task — **GSM8K 0.78 vs 0.77; Last Letter 0.77 vs 0.73; Shuffle Objects 0.44 vs 0.41**. Five flaws found in original incl. different prompts per condition and conflating JSON-mode with true constrained generation. Key quote: "Structured generation is not the same thing as JSON-mode." Confidence HIGH (numbers fetched verbatim).
- Pairs with the BAML counterpoint in §2: the honest framing is "contract guarantees the envelope, not the letter — which is why pipelines also need verification gates."

### XGrammar primary paper (arXiv 2411.15100)
- Source: https://arxiv.org/abs/2411.15100 (fetched 2026-06-10)
- Verbatim from abstract: "XGrammar can achieve up to **100x speedup** over existing solutions."
- Mechanism: vocabulary split into context-independent tokens (pre-validated, cached masks) vs context-dependent (runtime check); persistent stack; overlapped with GPU execution. Confirms §1's <50µs practitioner number is grounded in primary work.
- Confidence: HIGH.

### JSONSchemaBench (arXiv 2501.10868, Feb 2025)
- Source: https://arxiv.org/abs/2501.10868 (fetched 2026-06-10)
- **10,000 real-world JSON schemas**; evaluates **6** constrained-decoding frameworks: Guidance, Outlines, Llamacpp, XGrammar, OpenAI, Gemini — across efficiency, constraint coverage, and output quality. Found real coverage gaps (mirrors Anthropic's documented schema-dialect limits).
- Confidence: HIGH.

### Claude Agent SDK — structured output from whole agent RUNS (not just single calls)
- Source: https://code.claude.com/docs/en/agent-sdk/structured-outputs (fetched 2026-06-10)
- Verbatim: "The agent can use any tools it needs to complete the task, and you still get validated JSON matching your schema at the end."
- Verbatim: "the SDK validates the output against it, **re-prompting on mismatch**. If validation does not succeed within the retry limit, the result is an error instead of structured data."
- Failure is a typed result, not silent: subtype `error_max_structured_output_retries`. Zod/Pydantic round-trip for end-to-end typing.
- META: this very research note was produced by a subagent whose ONLY channel back to its orchestrator is a schema-validated StructuredOutput call. The hours-unattended property is the ledger's doing.
- Confidence: HIGH (primary vendor doc).

### AWS Bedrock structured outputs (Feb 6, 2026)
- Source: https://aws.amazon.com/blogs/machine-learning/structured-outputs-on-amazon-bedrock-schema-compliant-ai-responses/ (fetched 2026-06-10)
- Same architecture industry-wide: schema validated → grammar compiled → **cached 24 hours per account** → constrained sampling. **Nine model providers** covered.
- Cascade example: booking function expects `passengers: int`, model emits `passengers: "two"` — valid JSON, "semantically wrong for your function signature." `strict: true` eliminates the class.
- Confidence: HIGH (primary vendor blog; shows OpenAI/Anthropic/Google/AWS have all converged on the identical mechanism).

### Salesforce Engineering — Agentforce Agent Graph, "guided determinism"
- Source: https://engineering.salesforce.com/agentforces-agent-graph-toward-guided-determinism-with-hybrid-reasoning/ (fetched 2026-06-10)
- Topology level defines "the graph of agents — what agentic nodes exist, **the contracts between them**, and the transitions that govern information flow"; FSMs manage state transitions; LLM reasons inside the rails.
- Killer quote: "**Reliability demands architecture, not LLM alchemy.**"
- Confidence: HIGH on quotes; MEDIUM on publish date (fetch reported Oct 2024; Agent Graph material reads 2025-era).

### Collin Wilkins — "LLM Structured Outputs: Schema Validation for Real Pipelines" (Jan 17, 2026, updated May 2026)
- Source: https://collinwilkins.com/articles/structured-output (fetched 2026-06-10)
- Contract framing, verbatim: "**Schemas become contracts. Prompt authors, application developers, data consumers — everyone reasons against the same shape.**"
- Verbatim: "Syntax errors become impossible by construction."
- Trust boundary: "Everything the model returns is untrusted until it clears validation" — staged untrusted zone (generate → parse → validate) walled off from downstream.
- Confidence: MEDIUM-HIGH (practitioner, but precise and current).

### MAST re-verification (this session)
- https://arxiv.org/abs/2503.13657 abstract confirms: **14 unique failure modes, 3 categories** ("system design issues, inter-agent misalignment, task verification"), **κ=0.88**, **1600+ annotated traces, 7 frameworks**, 150-trace taxonomy development set. The 41–86.7% failure-rate range is in the paper body (v3 HTML), not the abstract — keep but attribute to body.

### Magentic-One re-verification (this session)
- Microsoft Research page confirms: Task Ledger = "facts, guesses, and the current plan"; Progress Ledger tracks current progress and per-agent assignments; **stall count > 2** → outer loop → "it can update the Task Ledger and create a new plan."

---

## Synthesis for the episode

The through-line: every serious source — OpenAI, Anthropic, Microsoft Research, GitHub, the Berkeley MAST team — converges on the same move. You don't make a long-running agent reliable by making the model smarter; you make it reliable by hardening the seams: (1) constrained decoding turns the schema into a physical impossibility of malformed output (100% vs <40%); (2) typed handoffs make agent-to-agent batons validated artifacts, not prose (GitHub: "typed schemas are table stakes"); (3) explicit ledgers (Magentic-One's task/progress ledgers, stall-count > 2 replanning) make state durable and recoverable across hours; (4) verification gates between stages catch what schemas can't (MAST: +15.6% from adding verification; BAML: valid JSON can still be a wrong answer). The schema is the contract; the ledger is the memory; the model is just the worker between them.

Best numbers for visuals:
- **100% vs <40%** (OpenAI, schema adherence with vs without constrained decoding)
- **93% → 100%** (training got OpenAI to 93; engineering closed the gap)
- **41–86.7%** failure rates of SOTA multi-agent systems (MAST)
- **44.0% / 32.15% / 23.85%** failure-category split (design / inter-agent misalignment / verification)
- **1,642 traces, 14 failure modes, kappa 0.88** (MAST rigor)
- **+15.6%** task success from adding verification steps (MAST intervention)
- **64% of AI risk at the schema layer; 15–20% → near-zero** JSON failure (TianPan, medium confidence)
- **stall count > 2** triggers ledger update + replan (Magentic-One)
- **24-hour** grammar cache, **180 s** compile timeout, **20** strict tools (Anthropic limits)
- **93.63% vs 91.37%** — the counterpoint: constrained decoding slightly WORSE on BFCL (BAML)
- **<50 µs vs 10–50 ms** per token — the contract costs ~nothing at runtime (medium confidence)
- **up to 100x** constrained-decoding speedup (XGrammar, primary paper)
- **10,000 schemas / 6 frameworks** (JSONSchemaBench)
- **18 experts / 10 orgs / 20 systems / α=0.80** + the determinism-flexibility Pareto front (schema-gated paper, Mar 2026)
- **0.78 vs 0.77 / 0.77 vs 0.73 / 0.44 vs 0.41** — structured ≥ unstructured when prompts held equal (dottxt)
- **95–99% vs 70–85%** action success structured vs parsed (Agentmelt — LOW confidence, label practitioner-reported)
- **9 model providers**, 24h grammar cache on Bedrock — whole industry converged on one mechanism
