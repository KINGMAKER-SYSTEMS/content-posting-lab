# Private summary reader verification results

- Time: 2026-10-30 20:26 UTC
- Agent: codex-eric
- Worktree: /tmp/codex-eric-lab-307-current-main-audit
- Type: verification
- Area: private originality summary

The final eight-file protected focus passes90 checks, including Docker-selected backend imports and first-file stale/future refusals. Tracked before/after manifests match. Five semantic guard mutants in a separate source worktree are caught by actual assertion failures: digest, generation binding, age, reader shape and generation regression. The age mutant removes the puller age check and bypasses the actual reader's second age check; removing only the puller guard remains safely refused by the reader. An earlier existing-cache case also remained safely refused by the newer-generation fence, so a first-file case was added. All intermediate mutant receipts are preserved in /tmp/lab307-current-main-checks/. No claim that the redundant age predicates are independently necessary.

Runtime source is unchanged from the reviewed draft. Source restored in mutant worktree. Broad project, private producer/delivery staging and release image checks remain unverified; feature flags remain off and no live transport occurred.
