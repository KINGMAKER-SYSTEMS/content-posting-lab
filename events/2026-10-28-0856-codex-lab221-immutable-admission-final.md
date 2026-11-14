time: [8:56am] [10-28-26] EDT
agent: [Codex desktop] [gpt-6.1-sol] [release owner]
worktree: [codex/lab221-immutable-admission-final]
type: [bug report]: Preserve upload media identity across admission and provider execution
area: [backend], [testing], [review]

New upload admissions bind a resolved path, file identity, and bounded SHA-256
digest before queue persistence. Provider execution stages and verifies those
same bytes, so symlink retargeting, hardlink aliases, and in-place rewrites
cannot silently change a held upload attempt. Admission hashing runs off the
request event loop and outside the queue write lock; queue wake survives request
cancellation without starting old queued jobs on service restart. Existing
legacy rows retain their reconciliation behavior. The upload test group passed
89/89 on the Mac mini. A wider suite reached 2626 passing tests before an
unrelated font validator test that also fails on the original PR head; a second
run encountered an independent generation-budget interaction after 2840 passes.
No provider Post, deployment, or merge occurred. Rollback is the prior PR head.
