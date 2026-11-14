time: [22:08] [08-11-26]
agent: [codex desktop] [gpt-6.1]
worktree: [codex/lab-pricing-asyncio-fixture-20261108]
type: [bug report]
area: [testing]

The generation-budget pricing tests replaced `create_task` on the shared
`asyncio` module, interfering with unrelated FastAPI/AnyIO tasks. Both tests
now replace only `routers.video.asyncio` with a delegating proxy that captures
the provider coroutine while preserving real asyncio behavior elsewhere. No
production code or live provider calls were involved. The pricing file passed
65 tests; the prescribed five-file budget/video gate passed 155 tests.
`py_compile` and `git diff --check` passed.
