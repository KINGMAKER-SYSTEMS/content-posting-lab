time: [02:31pm] [10-19-26]
agent: [Codex desktop] [gpt-6.1-sol]
worktree: [codex/pr179-zip-guard-20261019]
type: [bug report]: Content Lab PR #179 split-origin ZIP download
area: [frontend] [backend]: Access-authenticated downloads

Replaced Burn's direct ZIP navigation with a credentialed fetch and local Blob
download so a split-deployed browser supplies its Origin. The route auth guard
admits cross-site or same-site Fetch Metadata only with an exact configured
Origin; navigation without Origin and unlisted origins remain refused. Added
the corresponding auth and browser tests and documented the CORS and Access
cookie prerequisites. The prior event entry remains unchanged.
