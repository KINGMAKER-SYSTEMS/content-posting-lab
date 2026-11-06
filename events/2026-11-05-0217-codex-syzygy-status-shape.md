time: [02:17] [05-11-26]
agent: [codex] [gpt-6]
worktree: [codex/lab-syzygy-status-shape-20261105]
type: [bug report]
area: [backend]

Reject a missing, null, non-string or blank Syzygy render status with the
existing generic SyzygyError instead of polling for fifteen minutes and
misreporting a render timeout. Unknown nonempty statuses retain bounded
polling for compatibility. The error contains no response contents.

The five malformed-response cases failed promptly before the fix because
they attempted another sleep. The positive unknown-status case completes
through the next done response. All 46 related slideshow, Coffee, library,
render-key replay and production-image import tests passed. Independent
review confirmed that failure preserves the Lab job and Syzygy render keys
and performs no automatic render resubmission.

Devlog pass: AGENTS.md and its child index remain unchanged because this
restores the existing upstream response contract without changing ownership,
public schemas, dependencies or render identity. The pre-existing untracked
job-limit-repro.py remains untouched and is excluded from delivery.
