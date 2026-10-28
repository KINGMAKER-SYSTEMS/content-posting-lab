_________________________________________________________________________________
time: [09:08am] [10-28-26]
agent: [codex desktop] [gpt-6.1-sol]
worktree: [detached PR #221 head a009b937]
type: [bug report]: Content Lab PR #221 non-regular upload source
area: [backend]: upload admission and staging

Refused FIFO/device upload sources before bounded identity hashing and before provider staging. Source opens are nonblocking and checked again by descriptor type, so a path swap to a FIFO cannot stall the worker waiting for a writer. Added admission and pre-provider FIFO regressions and updated the root AGENTS contract. No shared PR branch, deployment, or publication changed.
_________________________________________________________________________________
