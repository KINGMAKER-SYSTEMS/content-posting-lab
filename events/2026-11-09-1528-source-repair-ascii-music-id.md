time: [15:28] [09-11-26] America/New_York
agent: [codex desktop] [gpt-6]
worktree: [codex/source-repair-ascii-music-id-20261109] [/tmp/content-lab-ascii-music-20261109]
type: [bug report]
area: [backend] [testing]

The source-duration repair validator accepted non-ASCII Unicode digits for
`approved_music_id` despite its stated 6-20 ASCII-digit contract. A six-digit
Arabic-Indic regression failed before the fix; the validator now requires ASCII
digits. The source-repair and Lab integration suites pass 37 tests. Root
`AGENTS.md` records the input rule; its child index is unchanged. This is a
local branch result only: no provider call, deployment, or live repair occurred.
