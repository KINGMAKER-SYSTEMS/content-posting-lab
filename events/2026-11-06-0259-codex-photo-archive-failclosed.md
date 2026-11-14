time: [02:59] [06-11-26] America/New_York
agent: [codex desktop] [gpt-6]
worktree: [codex/managed-photo-reservation-handoff-20261104] content-lab-photo-reservations-20261104
type: [bug report]
area: [backend] [testing] [review]

Independent combined-tree review found that a malformed archived slideshow
entry could be skipped and a photo plan reused. Photo recipes now refuse
non-dict rows, missing scope, or invalid signatures before planning, even
when the bad row names another scope. Direct reservations, ordinary photo
jobs and capacity reads return 503 rather than treating the archive as empty.
Non-photo slideshow behavior is unchanged. Five mounted corruption variants
exercise all three callers. The focused combined-tree suite passed 162 tests,
one existing fixture skip, and 55 subtests; Coffee slideshow tests passed.

Devlog pass: AGENTS.md now names the malformed-history refusal; its child
index is unchanged. No live page, provider, or posting action occurred.
