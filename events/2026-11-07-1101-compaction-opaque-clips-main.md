time: [11:01] [07-11-26]
agent: [codex desktop] [gpt-6]
worktree: [codex/compaction-opaque-clips-main-20261107]
type: [workflow]
area: [backend] [testing]

Extracted the exact two-file fix from draft PR #337's f73c5b8 onto current
main af7c55e6a4cd23b299c0bffa2ca014011fb9c4ef without carrying its
parent #336 or #331 branches. Completed jobs with a non-list `clips` container
stay live with their idempotency mapping while the compaction cursor continues
to the next job. A valid list keeps the existing per-entry SHA filtering.

The focused compaction module passed 55 tests. The required managed-photo,
image-video, and compaction group passed 98 tests with one skip. The canonical
checkout and its local data were not changed. No provider call, live job-store
mutation, deployment, or post occurred.
