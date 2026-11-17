time: [02:36pm] [10-19-26]
agent: [Codex desktop] [gpt-6.1-sol]
worktree: [codex/pr179-zip-guard-20261019]
type: [bug report]: Content Lab PR #179 large ZIP download
area: [frontend]: Bounded browser memory

Replaced the split-origin Blob download with a user-activated save picker and
response-body stream piped to disk. Same-origin downloads retain a direct
browser-managed navigation. Browsers without the picker fail explicitly on
split-origin downloads; same-origin ingress remains their supported path. The
separate CORS and Access-cookie prerequisites remain release holds.
