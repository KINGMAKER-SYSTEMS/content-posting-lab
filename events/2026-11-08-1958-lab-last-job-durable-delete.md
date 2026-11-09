time: [19:58] [08-11-26] America/New_York
agent: [Codex desktop] [GPT-6]
worktree: [codex/lab-video-durable-receipt-20261107] [/Users/risingtidesdev/dev/.worktrees/lab-video-durable-receipt-20261107]
type: [bug report]
area: [backend] [testing]

Deleting the final UI video job left its old jobs.json intact because the save
helper skipped empty mappings. Reproduced through DELETE followed by restart
reload: the removed job returned. The helper now writes an empty mapping when
the project already has a jobs file, while never-used projects remain file-free.
Added an HTTP delete and restart regression. It failed before the fix and passed
afterward; the focused video API suite passed 42/42 and `git diff --check` passed.
Root AGENTS.md records the durable empty-state contract. No provider, deployment,
or live data was exercised.
