time: [20:47] [07-11-26] America/New_York
agent: [Codex desktop] [GPT-6]
worktree: [codex/lab-video-durable-receipt-20261107] [/Users/risingtidesdev/dev/.worktrees/lab-video-durable-receipt-20261107]
type: [bug report]
area: [backend] [testing]

Followed up the crash gap recorded at 20:35 on this branch. A completed UI
video now writes an atomic per-output receipt before updating jobs.json. The
receipt records its entry and the exact size and SHA-256 of the final file,
provider master/image, and every crop. When jobs.json remains queued after a
completion-write failure, restart verifies that receipt and restores the paid
artifact without another provider request. New jobs no longer infer completion
from a partial file alone; older jobs keep their existing recovery rule. If
both writes fail, the in-memory result stays processing and reads retry the
write while the process is alive. A symlinked receipt directory is refused.

Focused video, atomic-write, and budget suites passed 184/184 after the final
path check. Restart, provider retry, and budget suites passed 62/62 before that
path-only follow-up. Python compilation and git diff whitespace checks passed.
No paid provider, Railway deployment, or live artifact was exercised. Root
AGENTS.md now records the completion-receipt and path contracts; there is no
child devlog for these files.
