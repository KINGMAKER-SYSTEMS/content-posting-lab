time: [21:13] [04-11-26]
agent: [Codex desktop]
worktree: [codex/lab-seasonal-legacy-map-guard-20261104]
type: [bug report]
area: [testing]

Reproduced three stale format-authority pins on main edf6a6a. In both
registry maps only the UGC slideshow entry differs from the reviewed #351
base. The UGC scope test now compares all other entries with that base,
including the #350 H3 Highspeed change. The legacy digest pins the new
UGC entry and leaves POV Seasonal to its own contract tests.

All three focused regressions passed with the updated guards.
