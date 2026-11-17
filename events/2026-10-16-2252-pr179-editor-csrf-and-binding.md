_________________________________________________________________________________
time: [2026-10-16]
agent: [Codex] [builder cx-l179]
worktree: [sec/lab-route-auth] [e2df-fr/lanes/l179]
type: [bug report]: PR #179 editor CSRF and operator binding repair
area: [backend] [testing] [review]

Marked editor timeline loading and OpenShot export as side-effect GETs because both may persist imported, migrated or cache-pruned projects. Access callers now require the existing same-site check on these routes. Poster bind/unbind use their declared Access login without also demanding the server Mini App agent key. Six real-handler regressions failed at reviewed head and passed with the repairs; compiling reverts reproduce both failures. Focused verification also exposed FastAPI's websocket RouteContext losing include prefixes in the shared route adapter; retain that context like HTTP and unwrap only for test type checks. The existing pre-body websocket regression kills its revert. Merged origin/main into the candidate branch, retaining both event histories and the shared Worker bearer capability check. Full-suite comparisons and exact evidence are recorded in the builder report; no PR merge, deployment, live provider call or posting operation was performed.
_________________________________________________________________________________
