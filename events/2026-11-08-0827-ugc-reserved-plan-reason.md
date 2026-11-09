time: [08:27] [08-11-26]
agent: [codex desktop] [gpt-6]
worktree: [codex/ugc-slideshow-photo-only-20261108]
type: [bug report]
area: [backend] [testing]

When every eligible UGC photo was reserved, planning returned no job but its
zero-plan explanation incorrectly reported that the template exceeded the
library. A new regression reproduced that mismatch before the fix. The shared
generic clean-video explainer now compares the full eligible set with the
template size: a true shortage reports template_media_exceeds_library, while
enough eligible media held by reservations reports generic_media_exhausted.
The existing mixed-library shortage test and 147 focused slideshow,
reservation, zero-plan, Northstar-library, and Coffee tests passed. The root
devlog now records this reason contract. No provider, posting,
deployment, or live state changed.
