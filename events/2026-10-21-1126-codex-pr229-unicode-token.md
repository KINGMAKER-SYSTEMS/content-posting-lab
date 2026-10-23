_________________________________________________________________________________

time: [11:26am] [10-21-26]
agent: [codex] [gpt-6.1-sol]
worktree: [codex/pr229-unicode-token-20261021]
type: [bug report]: PR #229 TOS import credential guard
area: [backend]: shared control-plane Bearer comparator

The shared Bearer guard now compares UTF-8 bytes so a non-ASCII invalid token
returns 401 instead of raising TypeError/500. A TOS import regression calls
the route with an already-decoded Unicode header and asserts refusal before
any Supabase read or project write. The same guard serves the existing
control-plane capability route. No import, publication, or deployment occurred.
_________________________________________________________________________________
