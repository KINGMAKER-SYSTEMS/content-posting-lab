# Observe owned import descendants without spawning ps

Time: 2026-10-30T18:40:26.968606+00:00
Agent: Codex rail_review (model identity unavailable)
Worktree: /tmp/tides-lab-parentdeath-observation-1030
Type: test-only sandbox compatibility repair
Area: owned source-import process regression

The guarded macOS run refused to spawn ps with EPERM. A read-only native libproc query is available under that same guard. The test now records both live native process births before releasing the owner's ungraceful-exit handshake, then requires positive absence, terminal state or replacement birth for those original processes. Denied, short and unknown observations are failures, never death. Linux uses its native proc stat birth tick; patched parser controls do not claim native Linux execution. Unsupported platforms fail explicitly. The existing gated late-effect assertion remains.

The exact guarded production-Python3.11 file passes26 tests. Removing supervisor parent-death cleanup makes the death assertion fail and the released finite descendant writes its late marker. Treating a failed native read as death fails the error controls. Source restoration and final results are retained outside this repository in the consolidation review evidence. Production process code is unchanged; no full suite, provider call, numeric orphan signal, push, merge or deployment is included. Root review is required before publication.
