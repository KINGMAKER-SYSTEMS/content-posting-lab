time: [14:12] [07-11-26]
agent: [codex desktop] [gpt-6]
worktree: [codex/contentlab-material-profile-zero-reason-20261107]
type: [bug report]
area: [backend] [testing]

The Control Plane capability path was calling the live Syzygy library loader
with a missing material profile, then reporting `library_load_failed` instead
of the actionable `material_profile_missing` reason. Resolve and check the
profile before the networked library read. A regression failed before the fix
and now verifies that no library read occurs for a missing profile. The
current-main zero-plan sweep also omitted the commissioned `pov-seasonal`
generated format; add its existing prompt-inventory reason to restore coverage.
The focused capability, slideshow, Coffee, Northstar, generation, and seasonal
suite passed 129 tests. No provider, posting, deployment, or live state changed.
Root `AGENTS.md` already defines the zero-reason and verification contracts;
its child index and those contracts are unchanged.
