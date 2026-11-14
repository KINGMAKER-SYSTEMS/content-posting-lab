_________________________________________________________________________________
time: [11:53am] [10-21-26] EDT UTC-04:00
agent: [Codex desktop] [gpt-6.1-sol] [release owner]
worktree: [codex/upload-admission-20261012] [/private/tmp/contentlab-pr221-unknown-account-20261021]
type: [bug report]: Preserve ambiguous upload attempts with an unknown account
area: [backend], [testing], [review]

Independent review reproduced a second provider Post from Content Lab PR #221 when a quarantined prior `uploading` row lacked `account_name` but still held the same video path. The upload guard now compares that row's real video path before both new admission and queued execution, regardless of the new account. A row without a usable account or video target holds all upload claims for operator reconciliation. Unrelated known video paths remain available, and the original stored row and quarantine evidence are preserved. Builder verification passed 79 upload tests and `git diff --check`; no provider call, publication, production write, merge, or deployment occurred. This draft still needs independent review of the exact integrated head, current-base checks, hosted release gates, and local-runtime/live evidence before release. Rollback is the prior code revision while retaining the original attempt history; never replay an ambiguous Post.
_________________________________________________________________________________
