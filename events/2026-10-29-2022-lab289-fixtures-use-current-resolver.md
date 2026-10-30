# Update capability fixtures to the reason-bearing resolver

- Time: 2026-10-29T20:22:33.069684-07:00
- Agent: Codex review lead
- Worktree: /tmp/tides-lab289-fixture-card-1030
- Type: test fix
- Area: capability tests

PR289 changes capability resolution to a recipe/reason tuple while fixtures still patch the old recipe-only seam. Add the equivalent tuple stub in two test files; all expected snapshots and assertions stay unchanged. Exact289 baseline had134failures in the reviewed five-file scope. Parent verification after repair:265focused tests and373broader relevant tests pass, including HTTP retirement checks, source execution and source identity. No production code changes. PR286 and289 remain alternative implementations; do not stack them.
