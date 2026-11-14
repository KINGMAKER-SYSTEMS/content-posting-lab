time: [20:02] [08-11-26] America/New_York
agent: [Codex desktop] [GPT-6]
worktree: [codex/lab-video-durable-receipt-20261107] [/Users/risingtidesdev/dev/.worktrees/lab-video-durable-receipt-20261107]
type: [bug report]
area: [backend] [testing]

Single and bulk UI video deletion previously ignored a failed jobs.json save,
returned success, and could unlink files while stale job state remained on disk.
Both routes now stage file paths, persist the removed job set first, and only
then unlink files. A failed save restores in-memory jobs and returns 503 with
files untouched. Regression tests for both failure paths failed before the fix
and pass now. The bulk success test also verifies an empty jobs.json survives a
simulated restart. `tests/test_video_api.py` passed 44/44 and `git diff --check`
passed. Root AGENTS.md records the delete ordering and failure contract. No
provider, deployment, or live data was exercised.
