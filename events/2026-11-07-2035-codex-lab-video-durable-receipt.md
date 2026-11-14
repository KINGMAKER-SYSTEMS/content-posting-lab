time: [20:35] [07-11-26] America/New_York
agent: [Codex desktop] [GPT-6]
worktree: [codex/lab-video-durable-receipt-20261107] [/Users/risingtidesdev/dev/.worktrees/lab-video-durable-receipt-20261107]
type: [bug report]
area: [backend] [testing]

The operator video API previously started generation without saving its queued
job and could return done with a file URL after the completion jobs.json write
failed. A new job now saves before a task can debit or call a provider; save
failure returns 503 without starting it. A completed file whose receipt cannot
be saved stays processing with its path retained in memory; job reads and bulk
download retry the same write, and only a successful save exposes done.

The three new/updated focused regressions failed before the change and passed
after it. The video, atomic-write, and budget suites passed 180/180; the
generation restart/retry/budget suite passed 62/62; and the final route,
raw-submission, and atomic-write checks passed 50/50. Independent read-only
review found no false-ready leak but did identify a remaining crash window:
if the process exits while completion persistence still fails, the queued
receipt lacks the paid artifact path and restart cannot recover it. That needs
a separate durable recovery design. No paid provider, Railway deployment, or
live artifact was exercised. The root AGENTS.md now states the current
before-spend and before-ready persistence contract; there is no child devlog.
