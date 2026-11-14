time: [00:34] [06-11-26]
agent: [codex] [gpt-6]
worktree: [codex/lab-render-pin-behavior-20261106]
type: [issues]
area: [testing]

The lyric retirement suite pinned the SHA of the entire post renderer.
PR #362 changed only paired in-app frame timing, leaving the upright 768-pixel
minimum intact, but the stale whole-file pin made current main red. Removed
that duplicate assertion. `tests/test_post_render.py` exercises accepted
432x768 sources and rejects 430x766 before compositing, so the actual geometry
boundary remains covered by behavior tests.

Devlog pass: root AGENTS.md and its child index remain unchanged because this
removes a stale test oracle without changing product behavior or contracts.
