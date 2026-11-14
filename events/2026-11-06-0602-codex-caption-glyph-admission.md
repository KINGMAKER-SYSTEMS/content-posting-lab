time: [06:02] [06-11-26]
agent: [codex desktop] [gpt-6]
worktree: [codex/caption-glyph-admission-20261106]
type: [bug report]
area: [backend],[testing]

Current main silently removed characters the selected TikTokSans font could
not draw, and an all-unsupported caption fell back to drawing missing-glyph
boxes. A two-case regression failed before the change. The renderer now checks
the installed font's character table and refuses the whole caption before
drawing as CAPTION_GLYPH_MISSING; punctuation-only text refuses as
CAPTION_NO_TEXT. The old emoji-stripping tests were replaced with fail-closed
coverage for every advertised font, both Burn APIs, explicit saved lines, and
ordinary-caption byte parity. The existing prepared-render and caption suites
passed 388 tests; the burn quality gate passed 29. An independent read-only
review found no actionable defect. The scope excludes draft PR #226's
unresolved aggregate ink gate and Worker error classification. No live page,
provider, deployment, or posting state changed.
