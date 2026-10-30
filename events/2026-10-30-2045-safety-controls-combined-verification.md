# Safety controls combined verification

- Time: 2026-10-30 20:45 UTC
- Agent: codex-eric
- Worktree: /tmp/codex-eric-lab-301-current-main-audit
- Type: verification
- Area: protected safety controls view

The current-main300+301+307 composition tree85e3237fba6c3bfcb00cc422cded814716f2ba48 passed130 focused checks and its233-file broad run recorded6360 passed,22 skipped,8 failed in641.32 seconds. All eight named cases/reasons match independently replayed main8aa failures (stale renderer pin1, absent optional numpy4, nested approx1, Hailuo fallback-decode2). One additional NumPy-import-blocked file is explicitly excluded. No green full suite claimed. Before/after tracked manifests match5bee0f556bfe6e4d5c1c24e8d82149fe2cfd7b44093784147b4cdd7dae5d085e.

This composition includes exact reviewed301 runtime/test bytes and separate300/307 code; it is not a standalone301 full run. Required protected route/auth checks and Docker-selected backend imports pass. Feature stays default off; source review is ready with baseline/environment limits. Normal release image/build/activation checks remain separate, and no production/provider call occurred. Receipts: /tmp/lab300301307-broad-checks/, /tmp/lab300301307-compose-checks/, /tmp/lab300-main-eight-baseline-v2.log.
