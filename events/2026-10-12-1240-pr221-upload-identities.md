_________________________________________________________________________________
time: [12:40pm] [10-12-26] EDT; actual UTC: 2026-10-12T16:40:00Z
agent: [Codex desktop with Mac mini tests] [gpt-6.1-sol] [focused builder]
worktree: [codex/upload-admission-20261012] /private/tmp/cpl-upload-admission-20261012
type: [bug report]: Reject duplicate upload job identities before queue admission
area: [backend], [testing], [review]

Fresh independent review found that a stored completed row followed by a queued row sharing the same job_id passed shape validation; process_queue would select the queued row while get_job returned the completed one, then loop without advancing. The private candidate now rejects repeated IDs as corrupt before either admitting or appending a new job and preserves the existing bytes. Synthetic duplicate-ID regression and the upload API/cookie/login suite passed 34/34 on an isolated Mac mini current-main checkout; frontend TypeScript/Vite build passed there with matching-lockfile existing dependencies. Logs: /tmp/cpl-upload-admission-tests-20261012-1640.log and /tmp/cpl-upload-admission-frontend-build-20261012-1640.log on macmini-ip. No upload, provider action, push, PR, merge or deployment occurred. Fresh independent head review remains required.
_________________________________________________________________________________
_________________________________________________________________________________
