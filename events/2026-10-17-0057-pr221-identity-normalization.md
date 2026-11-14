_________________________________________________________________________________
time: 2026-10-17T04:57:14.851551+00:00
agent: [Codex] [builder]
worktree: [codex/content-lab-job-key-lookup] /Users/seenorising/rt-base-wt/e2df-fr/lanes/l223
type: [bug report]: Share create and receipt lookup identity normalization
area: [backend], [testing], [review]

Round 2 review found that a numeric top-level pageId accepted by create was still rejected by receipt lookup. Creation, lookup and request hashing now share one normalization helper for pageId, lane, engine, lockedRecipeId, recipeVersion and policyHash, preserving create's existing coercions and exact lane comparison. A registered numeric-page request now recovers its original queued job without changing store bytes or adding a runner start. The numeric policyHash regression remains covered, alongside trimmed recipe identity fields and invalid scope refusals. The focused suite passed 122 tests; reinstating the raw lookup scope comparison compiles and fails the numeric-page recovery assertion. Current main was already merged, so this fix is an ordinary separate commit.
_________________________________________________________________________________
