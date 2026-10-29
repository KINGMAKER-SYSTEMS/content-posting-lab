time: [14:18] [29-10-26]
agent: [codex] [gpt-6]
worktree: [codex/hailuo-768p-cost-20261029]
type: [bug report]: Hailuo automation resolution and operating cost
area: [backend]: generation admission, restart recovery, prepared delivery

Read-only production inspection at deployment 60db42476d84095e915fe9b4a698ca950b11fff7
confirmed that truck and boat catalog base inputs specify Hailuo 2.3 1080p/6s
while the old recipe note quotes $0.28. Three recent completed Replicate
predictions independently reported 1080P, six-second output. The live store
contained 58 completed Hailuo jobs created on 2026-10-29 with $0.49 attempt
rows ($28.42); those 58 initial runs at 768p/6s would be $16.24, a $12.18
price difference. This is a per-run comparison, not invoice reconciliation.
The observed total generation reservation ledger was $29.18 of the default
$175 UTC-day ceiling; no production setting or budget was changed.

New automated Hailuo jobs now pin effective 768p/6s provider options before
admission. Execution and restart reuse that private snapshot. Legacy jobs
without it keep the original model/input hash and paid prediction, including
1080p. Preserve published recipe/catalog bytes and versions. Prepared-final
rendering accepts upright 768-high crops and performs the existing final
1080x1920 delivery scale. Five-way cropping, model, prompts and other providers
remain unchanged.

Verification: 711 generation, budget, recovery, rendering, health and packaged
import tests passed; 4 provider framing tests passed. Real ffmpeg checks cover
1366x768 masters, five 432x768 crops and decoded captioned 1080x1920 finals.
The restart regression checks both new $0.28 and legacy $0.49 predictions and
one paid POST across restart. No paid generation was submitted by verification.
Docker daemon did not respond within five seconds; a full image build remains
outstanding before release.

Devlog pass: updated root ownership and minimum source geometry. No child
AGENTS.md exists in the affected paths and no child index changed. Historical
hashed recipe catalogs were intentionally retained for exact paid replay.
Local preparation only; not pushed, merged, deployed or verified on a fresh
live 768p generation.
