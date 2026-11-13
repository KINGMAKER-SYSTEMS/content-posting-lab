time: [00:28] [06-11-26]
agent: [codex] [gpt-6]
worktree: [codex/lab-pool-catalog-baseline-20261106]
type: [issues]
area: [testing]

Two pool tests on main still pinned full catalog hashes from 08db4f5.
The additive POV Seasonal format and later Christmas slideshow admission changed
those catalogs independently of pool page handling, so both tests failed on
untouched main. The tests now compare each unlisted page against the same
page with pool classification disabled and check that no pool-only ingredient
status appears. The truck-specific selection version stays pinned.

Devlog pass: root AGENTS.md and its child index remain unchanged because this
repairs the test oracle and changes no product behavior or durable contract.
