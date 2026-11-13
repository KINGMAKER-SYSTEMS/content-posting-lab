time: [20:01] [04-11-26]
agent: [codex desktop]
worktree: [codex/lab-seasonal-legacy-map-guard-20261104]
type: [bug report]
area: [testing]

Excluded the additive POV Seasonal format from the legacy authority-map digest.
Both original digest pins match again. The dedicated Christmas generation
test still checks format selection, provider options, and the cost contract.

The two digest assertions failed on main `878c167c`. With this change, 111
lyric, UGC, curated, Coffee, and generation tests passed.
