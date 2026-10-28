_________________________________________________________________________________

time: [07:41pm] [10-19-26]
agent: [codex] [gpt-6.1-sol]
worktree: [codex/burn-import-tos-auth-20261019]
type: [bug report]: TOS corpus credential dependency
area: [backend]: Burn import service credential

The machine-authenticated TOS import now requires the server-held
SUPABASE_SERVICE_KEY. A generic SUPABASE_KEY cannot silently substitute for it;
missing configuration refuses before any Supabase read or caption-bank write.
The linked corpus access migration remains held until the deployed credential
role, producer, and live rollback path are verified.
_________________________________________________________________________________
