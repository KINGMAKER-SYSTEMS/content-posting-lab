time: [12:25pm] [10-28-26]
agent: [codex] [gpt-6.1-sol]
worktree: [codex/lab-baseline-test-reliability-20261028]
type: [refactor]: Content Lab baseline test reliability
area: [testing]: corruption and slideshow capability fixtures

Made the deeply nested authority fixture malformed on every supported Python
JSON decoder, including Python 3.14, so it continues to exercise startup
recovery rather than silently inserting a valid document. Updated the
slideshow planner test double to accept the existing reservation keyword
arguments while retaining the live-library cache assertions. Focused Mac mini
tests passed: 5 tests on the source-identical worktree.
