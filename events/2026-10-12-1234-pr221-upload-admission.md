_________________________________________________________________________________
time: [12:34pm] [10-12-26] EDT; actual UTC: 2026-10-12T16:34:00Z
agent: [Codex desktop with Mac mini tests] [gpt-6.1-sol] [focused builder]
worktree: [codex/upload-admission-20261012] /private/tmp/cpl-upload-admission-20261012
type: [bug report]: Refuse upload jobs without executor and durable receipt storage
area: [backend], [frontend], [testing]

The private candidate makes /api/upload/submit return 503 before creating a job on Railway because this in-process queue has no cross-worker claim or original-attempt reconciliation; local admission refuses a missing uploader or corrupt queue without changing the existing file. The Distribution action shows the API's unavailable reason instead of a false queued success. Local checkout-relative storage stays supported. Mac mini isolated-worktree upload API/cookie/login tests pass 26/26 without browser or upload calls. Frontend build is unverified because this isolated checkout has no node_modules and the Mac mini has no npm binary; no production upload, push, PR, merge or deployment occurred. Rollback is to revert this admission/UI change while preserving the existing queue file; enabling hosted uploads additionally requires provisioning the provider runtime, a volume-backed queue, cross-worker claims and reconciliation of original attempts and any prior queue.
_________________________________________________________________________________
_________________________________________________________________________________
