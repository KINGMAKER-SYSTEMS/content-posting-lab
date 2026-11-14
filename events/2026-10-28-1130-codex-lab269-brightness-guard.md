time: [11:30am] [10-28-26]
agent: [codex] [gpt-6.1-sol]
worktree: [codex/lab269-brightness-guard]
type: [bug report]: Content Lab PR #269 fractional brightness page look
area: [backend]: per-cut grade admission

Nearest 0.001 rounding could turn a legal 0.5006 page look into a brighter
0.501 cut. Quantized brightness downward while retaining the Worker's lower
0.04 admission band; contrast, saturation and warmth keep their prior rounding.
Added fractional-look and band regression coverage. On the Mac mini,
`pytest -q tests/test_clip_variation.py tests/test_source_pool_roles.py`
passed 21 tests. This is an isolated candidate, not a pushed or deployed fix.
