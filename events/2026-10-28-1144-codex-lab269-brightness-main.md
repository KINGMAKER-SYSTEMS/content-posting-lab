time: [11:44am] [10-28-26]
agent: [codex] [gpt-6.1-sol]
worktree: [codex/lab269-brightness-main]
type: [gh actions]: Content Lab PR #270 repair rebased after parent #269 merged
area: [backend]: brightness admission

Parent PR #269 merged into main before its fractional-look brightness repair.
Cherry-picked the independently reviewed fix onto current main 15e44ca,
including the focused regression and owning AGENTS contract. The resulting
branch is a local candidate for release coordination; no push, merge, deploy,
or live outcome is claimed. Python compilation, diff hygiene, and 1,500
standalone cut-grade invariant cases passed. Pytest is unavailable in this
checkout, so hosted or full-suite verification remains pending.
