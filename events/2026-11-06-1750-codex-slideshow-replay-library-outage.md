time: [17:50] [06-11-26]
agent: [codex desktop] [gpt-6]
worktree: [codex/lab-slideshow-idempotency-20261106]
type: [bug report]
area: [backend] [testing]

On current canonical main 5a9db0d, an exact idempotent Syzygy slideshow
job retry read the live library before checking the durable job receipt.
An unavailable library made that retry return 409 instead of the original
job. The new regression failed with that 409 before the patch. The create
path now checks the saved key under the jobs lock before library IO and uses
the same replay and runtime-recovery helper for the post-read race check.
An exact retry returns the original job during a library outage; a changed
request still conflicts and a new key still reports the outage. The focused
slideshow, Dossier, and receipt suite passed 147 tests. A broader recipe,
generation, and source suite passed 136 tests before its unrelated real FFmpeg
render cases were stopped under active host disk pressure; no full-suite pass
is claimed. No live library, provider, posting, deployment, or production
job state changed. The root AGENTS.md owns the updated durable retry rule;
its child index remains unchanged.
