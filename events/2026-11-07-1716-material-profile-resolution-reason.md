time: [17:16] [07-11-26]
agent: [codex desktop] [gpt-6]
worktree: [codex/contentlab-material-profile-zero-reason-20261107]
type: [bug report]
area: [backend] [testing]

Review of PR #370 found that its new `material_profile_missing` reason covered
only a profile disappearing after slideshow recipe resolution. The normal
resolver rejects a missing profile first, and capabilities still reported
`recipe_unresolved`. A regression using the real slideshow resolver failed
with that generic reason before the correction. The capabilities path now
reports `material_profile_missing` for a valid typed recipe with no matching
commissioned profile, without reading the Syzygy library; malformed specs and
unavailable runtime retain their existing reasons. The focused zero-plan file passed 41
tests, and the capability/slideshow/Northstar/Coffee/generation/seasonal set
passed 130. Pool/bootstrap/auth checks passed 33 tests and 55 subtests.
`git diff --check` passed. No provider, posting, deployment, or
live state changed. Root `AGENTS.md` now records this output contract.
