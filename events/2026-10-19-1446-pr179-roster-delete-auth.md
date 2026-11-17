_________________________________________________________________________________
time: [02:46pm] [10-19-26]
agent: [codex desktop] [gpt-6.1-sol]
worktree: [codex/pr179-zip-guard-20261019]
type: [bug report]: Draft PR #179 roster deletion authentication
area: [backend] [testing] [review]

Independent review found that roster DELETE accepted APP_API_KEY, which the
browser bundle may expose. The route now requires CONTROL_PLANE_TOKEN as Bearer
or X-API-Key through the central route-auth dependency, and rejects APP_API_KEY
without deleting a page. The route table, devlog contract and HTTP regression
tests reflect the machine-only boundary. The integrated focused backend suite
passed 1,037 tests with one skip. This remains a local draft candidate pending
exact-head review, hosted checks, caller/Access ingress proof and release.
_________________________________________________________________________________
