time: [05:02] [08-11-26]
agent: [codex] [gpt-6.1-sol]
worktree: [codex/ugc-slideshow-niche-20261108]
type: [bug report]
area: [backend] [testing]

An independent review found that Content Lab advertised `ugc-slideshow` for
arbitrary trimmed page niches while Control Plane PR #1352 admits only its
controlled Master Pages vocabulary. A mistyped niche such as `TRUK` could be
offered in the Lab catalog and accepted by its recipe resolver, then refused
by Control Plane at publish. Limited the shared Lab format-admission helper to
the exact known page spellings, still excluding Coffee and meme; the PenPal and
Twin photo tuple gates remain unchanged.

The new unknown-niche regression failed before the fix and passed afterward.
The focused Dossier, recipe, slideshow, image, and Coffee suite passed 122 tests
with one skip. The Lab's known-page spellings were compared directly with
Control Plane's `NICHE_CANONICAL.page` at PR #1352 and matched exactly. No paid
generation, account configuration, posting, deployment, or live page changed.
Devlog pass: the root contract was narrowed; child index and other docs remain
unchanged because structure and ownership did not change.
