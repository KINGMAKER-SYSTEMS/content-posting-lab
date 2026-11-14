time: [04:16] [14-11-26]
agent: [claude] [opus 5.5]
worktree: [codex/network-fresh-footage-1112-01a1cfd6]
type: [merge]
area: [testing]

Landing PR #379 (unused master moments only; no Scrambler reuse allowance;
queued legacy cuts rechecked under the jobs lock). Merged current main into
the branch twice (through #226). The reviewer rerun the PR body required now
passed: 279 targeted network-fresh/reader/harness/planner tests, and
tools/verify_scrambler_reader_mutants.py with a green baseline and all 23
mutants behaviorally red. Full backend suite at c175574: 7,524 passed,
24 skipped, 6 failed; the golden-hash and two Christmas-POV failures reproduce
on main, the two capability p95/p99 perf tests and the packaged-import test
were load flakes (load average about 40) that passed or failed the same way on
main when rerun alone. Ported PR #238's four fresh-window scenarios as
tests/test_source_freshness_priority.py (fixture isolation added); they pass
under the #379 planner, so #238 is superseded. No provider, deploy or live
job was exercised.
