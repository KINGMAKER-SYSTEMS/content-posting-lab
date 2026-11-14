time: [06:20] [09-11-26]
agent: [Codex desktop] [GPT-6]
worktree: [codex/lab-slideshow-source-pins-20261109]
type: [bug report]: Generic Syzygy slideshow source integrity
area: [backend] [testing]

A same-size source replacement after job creation completed the generic
slideshow runner under its original key-and-size plan. The new regression
failed with status completed, then passed when ordinary photo plans pinned
the indexed SHA-256 and the runner compared live index and streamed bytes
before render submission. The Syzygy listing addition is the prerequisite;
unindexed objects are excluded and older listings without the hash field are
unavailable. Pre-hash queued jobs fail with `slideshow_source_pin_missing`.
Focused slideshow tests: 139 passed, 1 skipped; related capability,
reservation and runner tests: 272 passed with 55 subtests. No production job,
provider call, posting or deployment was performed.
