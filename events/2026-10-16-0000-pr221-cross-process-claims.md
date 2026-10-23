_________________________________________________________________________________
time: [2026-10-16] UTC
agent: [Codex] [cx-l221 builder]
worktree: [codex/upload-admission-20261012] lane l221
type: [bug report]: Reject invalid new upload records and prove cross-process claims
area: [backend], [testing], [review]

Review F1 found that an empty video path was accepted and saved, then rejected by every later strict queue read. New records now pass the existing store validator before persistence; invalid requests return 400 and preserve existing bytes. Tests prove rejection with missing/existing stores and a subsequent valid mocked upload completing. Review F2 found that removing flock escaped the prior suite. Spawned-process tests now pause the first claim before save and compete with a second claim or cancellation. Removing flock causes two fake provider calls or a stale successful cancellation; real locking passes. The reviewed-head empty-path tests fail and the updated 45-case upload/login/cookie/capability suite passes. Merged origin/main 713a0eb into this PR branch, retaining both sides of the events.md append conflict. No real uploader, login, publication, deployment or main-branch merge was performed; hosted upload remains disabled.
_________________________________________________________________________________
