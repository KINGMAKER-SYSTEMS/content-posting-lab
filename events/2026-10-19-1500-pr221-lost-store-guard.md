_________________________________________________________________________________
time: [03:00pm] [10-19-26]
agent: [codex desktop] [gpt-6.1-sol]
worktree: [codex/pr221-upload-reconcile-20261019]
type: [bug report]: Draft PR #221 replay guards after independent review
area: [backend] [testing] [review]

Independent exact-tree review found two remaining replay routes. A missing
upload job store was treated as an empty queue after a possible prior Post, and
a quarantined same-account attempt with a damaged status did not block another
video's provider call. The queue now requires explicit empty-file provisioning
and refuses missing history on submission, processing, listing and polling;
quarantined same-account rows remain execution barriers. Restart/lost-file and
different-video regressions exercise both refusals without publication.

This is a local draft candidate pending fresh exact-head independent review,
hosted checks, provider receipt reconciliation and live proof. No push, merge,
deployment or publication occurred.
_________________________________________________________________________________
