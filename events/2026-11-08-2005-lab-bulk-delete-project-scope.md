time: [20:05] [08-11-26] America/New_York
agent: [Codex desktop] [GPT-6]
worktree: [codex/lab-video-durable-receipt-20261107] [/Users/risingtidesdev/dev/.worktrees/lab-video-durable-receipt-20261107]
type: [bug report]
area: [backend] [testing]

Bulk deletion accepted globally known job IDs without checking that the job
belonged to the requested project. A request for project A could therefore
delete project B's media and memory state while leaving B's jobs.json unchanged.
The route now skips foreign-project IDs like missing IDs. The regression failed
before the fix, then verified A's matching deletion and B's unchanged media,
durable JSON, and restart-loaded state. The full video API suite passed 45/45;
`git diff --check` passed. Root AGENTS.md records the project boundary. No
provider, deployment, or live data was exercised.
