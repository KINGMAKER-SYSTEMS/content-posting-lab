# Keep source import expiry retryable during cleanup

- Time: 2026-10-30T05:09:27.206715+00:00
- Agent: Codex /root with independent Lab review
- Worktree: codex-review/import-expiry-cancel-retry
- Type: claim
- Area: Lab source import cancellation and exact-request replay

Claim F-LAB-EXPIRY-CANCEL-RETRY-1030, dependent on PR296 at 4bc857b594a103cb14c9ee171b60938ae1361124. No open duplicate found. An offline barrier test passes on main but fails on PR296 and its exact source-only PR298 composition: canceling an expiring status request during download cleanup replaces the restart cancellation reason, leaving same-key replay terminal. Preserve the original restart intent while draining owned cleanup; keep ordinary cancellations terminal, exact attempt fencing, private roots, claim lifetime, and mixed-version rollout limitations. No production, providers, credentials, phones, or deployment.
