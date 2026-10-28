_________________________________________________________________________________

time:  [03:34pm] [10-19-26]
agent: [codex] [gpt-6.1-sol]
worktree: [codex/burn-import-tos-auth-20261019]
type:  [bug report]: Content Lab TOS import credential gap
area:  [backend]: Burn import authentication

Required the server-only CONTROL_PLANE_TOKEN Bearer credential before the
TOS Supabase read or project caption CSV write. Missing, wrong, browser, and
unconfigured credentials are refused; focused Burn and capability tests pass.
No live import or production mutation was performed.
_________________________________________________________________________________
