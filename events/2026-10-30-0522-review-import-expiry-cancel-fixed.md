# Preserve the original import cancellation through cleanup

- Time: 2026-10-30T05:22:10.605648+00:00
- Agent: Codex /root, independent lab_review, worker_review and rail_review
- Worktree: codex-review/import-expiry-cancel-retry
- Type: fix and verification
- Area: source import expiry and exact-request retry

Retain only the initial CancelledError while the existing child-cleanup join drains. This preserves expiry-first restart identity; ordinary-first cancellation stays terminal, including a later tagged expiry. A late cancellation after workspace, timeout or storage failure retains the existing ordinary-terminal fallback; no exact error-text preservation is claimed there. Router retry eligibility, attempt identity, claim lifetime, private paths and retention policy are unchanged. The parent PR296 mixed-version rollout limitation remains.

The real status/runner/download-join/store/same-key replay probe passed on main and failed on PR296 and its source-only PR298 composition before this fix. Final required and related import, retention, health, smoke and Docker-selection tests: 130 passed on Python 3.14.7. Independent peer checks: 68 passed, plus 11 causal/completed helper controls; the final extra ordinary-first/later-expiry permanent case was separately reviewed. Runtime reversion produces four behavioral failures; late-tag capture produces three fatal-error retryability failures; adopting a late tag after empty ordinary cancellation fails the final added real-path control. Restored tests pass. Source SHA-256 remains 2530ffb24f8aaad7b1662aab7c04bbd6f1629afeeac458ee43df8492deba97fd. No full-suite, live-service, phone, merge or deployment claim.
