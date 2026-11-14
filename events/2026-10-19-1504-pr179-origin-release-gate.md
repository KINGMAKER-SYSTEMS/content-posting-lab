_________________________________________________________________________________
time: [03:04pm] [10-19-26]
agent: [codex desktop] [gpt-6.1-sol]
worktree: [codex/pr179-zip-guard-20261019]
type: [workflow]: Draft PR #179 same-origin Access release gate
area: [review] [writing]

Independent exact-tree review confirmed that the split-origin ZIP helper's
credentialed fetch does not cover ordinary Lab UI requests. The existing
browser calls omit cross-origin credentials, and unrelated-host Access cookies
also face SameSite restrictions. The route-auth release contract now requires
the operator frontend and API on one Access-protected origin; a split-origin
release requires separate browser credential, CORS and cookie verification.
The current Railway root serves the frontend, but the planned custom-domain
Access ingress, Hub/Worker credentials, hosted checks and live behavior remain
unverified. No push, merge or deployment occurred in this documentation pass.
_________________________________________________________________________________
