_________________________________________________________________________________
time: [2026-10-16]
agent: [Codex] [builder cx-l179]
worktree: [sec/lab-route-auth] [e2df-fr/lanes/l179]
type: [testing]: Keep merged capability credential tests on real authentication
area: [testing] [review]

The capability-auth regression brought in from main must opt out of this branch's legacy handler-test auth bypass. Marked it real_route_auth so its unauthorized-data-read assertion exercises the shared Worker dependency. The production bearer requirement is unchanged. The focused auth, editor and Telegram set passed 1,044 tests with one skip; full-suite comparison remains in the builder report. Corrected the OpenShot route table's unchanged UI-caller column while retaining its new WRITE classification. No merge or deployment was performed.
_________________________________________________________________________________
