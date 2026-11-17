_________________________________________________________________________________
time: [2026-10-16]
agent: [Codex] [builder cx-l179]
worktree: [sec/lab-route-auth] [e2df-fr/lanes/l179]
type: [testing]: Exercise roster contracts with the real Worker credential
area: [testing] [review]

The full base/head gate exposed three existing roster authorization assertions that inherited the legacy handler-test auth bypass on this branch. Opted the roster module into real route authentication and supplied its valid Worker bearer when testing the separate missing-lane error. The roster, capability-auth and new editor/binding regression set passes 27 tests. No production behavior changed; the final full comparison is recorded in the builder report. No PR merge or deployment was performed.
_________________________________________________________________________________
