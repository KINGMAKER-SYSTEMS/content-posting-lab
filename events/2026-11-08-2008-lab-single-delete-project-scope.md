time: [20:08] [08-11-26] America/New_York
agent: [Codex desktop] [GPT-6]
worktree: [codex/lab-video-durable-receipt-20261107] [/Users/risingtidesdev/dev/.worktrees/lab-video-durable-receipt-20261107]
type: [bug report]
area: [backend] [testing]

Single-job DELETE accepted a cached global ID without checking its owner
project, allowing a request naming project A to remove project B's job and
media. It now returns 404 before mutation when the job belongs to another
project. The regression failed before the fix, then verified B remained in
memory, on disk, and after restart; the matching project route still deletes it
and persists the empty state. The video API suite passed 46/46 and
`git diff --check` passed. Root AGENTS.md records both delete routes' project
boundaries. No provider, deployment, or live data was exercised.
