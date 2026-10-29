time: [14:49] [29-10-26]
agent: [codex] [gpt-6]
worktree: [codex/hailuo-768p-release-evidence-20261029]
type: [workflow]: Release Hailuo cost correction, PR #282
area: [backend]: production generation and paid-output recovery

Merged PR #282 at 1e57fc2960d59f3a26ec2308d6f8d5ebdfefe75f.
Railway deployment 90d4aa89-30da-4da5-9f2b-debe12109cd0 passed the Docker
app-import build step and served successfully at 14:40 America/New_York.
Subsequent deployment 4aa0d062-f7a4-4ae5-8b56-d187b7146279 at cdb70c0
contains the fix. Public /api/health returned 200 and status ok; gcc is absent
from the production image. All 27 current Hailuo page recipes resolve new
jobs to 768p, six seconds, $0.28. Local validation remains 715 passing checks.

The last observed 1080p prediction, nn2aspgkgxrmw0d1h3tsqv38jm, was submitted
at 14:38:45 before release. Existing job cpl-0fe761f586b6451e for
acct:backroaddriver recovered that same paid prediction after replacement and
completed at 14:44:13 with five clips. No replacement paid prediction was
submitted for that recovery. Waiting for the normal refill to observe the
first new 768p provider request; no extra paid test generation was created.

Devlog pass: AGENTS.md already records the new options snapshot, economical
Hailuo tier and 768-high prepared-source support from PR #282. It is unchanged
because release adds no contract or structure; no child index changed.
