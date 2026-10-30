# Private summary pull current-reader verification

- Time: 2026-10-30 20:23 UTC
- Agent: codex-eric
- Worktree: /tmp/codex-eric-lab-307-current-main-audit
- Type: test coverage and current-main integration
- Area: private originality summary

Locally integrated main8aa46b0 with our draft307 headbe650c2. The originality reader from278 is now on main; producer/private delivery staging remains unverified and the feature stays off by default. Existing runtime bytes remain unchanged.

Added durable puller/current-reader seam tests using the repository's existing redacted captured summary fixture: exact bytes/private permissions; hash, generation binding, future/stale time, invalid shape and fetch refusal preserve the prior file; an older generation cannot replace a newer one; a destination symlink is refused without changing its target.

Seven related test files pass86 checks under the known-directory read deny/network-contained environment. This is focused evidence, not full-project or production delivery acceptance. Separate-source guard mutants are the next check. No live R2/provider call, credential read, configuration activation or deployment.
