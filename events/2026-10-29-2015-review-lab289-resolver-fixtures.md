# Keep capability fixtures on the current resolver seam

- Time: 2026-10-29T20:15:56.560374-07:00
- Agent: Codex review lead
- Worktree: /tmp/tides-lab289-fixture-card-1030
- Type: claim
- Area: Lab tests

Claim F-LAB289-CAPABILITY-FIXTURE-SEAM-1030. PR289 changes the capability resolver seam while existing fixtures still patch the old function. Exact-head offline verification found132 capability snapshot failures and2 tiny/exhausted-master failures. Repair fixtures without changing expected behavior or production code. PR286 and289 are alternative implementations; this follow-up supports reviewing289, not stacking both.
