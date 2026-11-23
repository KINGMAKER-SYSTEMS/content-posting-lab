# Source montage executor: draft for review

Status: **draft, not wired.** Branch `draft/source-montage-executor`. No registry profile,
format contract, router path or Worker rule refers to it, so live behaviour is unchanged.

## What it makes

One silent, text-free 1080x1920 H.264 body at 30 fps, **5-9 s** long (the Worker's
sourced-video admission bound), made of **2-4 slices** of at least 1.5 s each, each slice
from a different moment of the page's own masters. The campaign sound and caption are
added downstream as they are today.

## How it keeps the existing rules

| Rule | How the draft follows it |
|---|---|
| Never reuse footage (owner 2026-11-12) | Every slice comes from a window chosen by `plan_source_cuts`. The whole window is reserved, not just the slice, so supply is spent faster but nothing is reused. |
| One planner for every cut path (`test_source_cut_path_census.py`) | The module calls `plan_source_cuts` and never constructs `SourceCut` or passes `clip_start_ms=`. |
| Executor re-checks queued cuts | `montage_is_planned` runs `source_cut_is_planned` on every window and checks each slice sits inside it; `render_montage` refuses a plan that fails. |
| Delivery encode | `tiktok_delivery_v1` video settings, audio settings dropped, `-an`, output rate pinned to 30. |
| Deterministic | Window seed `montage\0<job seed>`, slice offset from sha256(job seed, slot id). Same job, same frames. |

Files: `services/source_montage.py`, `recipes/executors/source-dna-montage.v1.json`
(not referenced by the registry), `tests/test_source_montage_draft.py` (7 tests, including a
real ffmpeg render probed for codec, size, frame count, no audio, 5-9 s, <= 32 MB).

## What wiring it in would take (not done; needs owner decisions)

1. **Registry:** `ALLOWED_BINDINGS` holds one executor kind per engine
   (`services/content_engine_registry.py:35-41`, enforced at `:119-124`). A montage needs either a
   second kind for `sourced_video` (change the binding to a set of kinds) or a new engine name.
2. **Recipe resolution:** `resolve_source_recipe` reads a fixed `EXECUTOR_PATH` and refuses any kind
   but `source_dna_recut` (`services/control_plane_sources.py:49-53`, `:308-312`).
3. **Job shape:** a job stores one output per planned cut (`sourceCuts`). A montage is several
   windows to one output: either a `montageSlices` list per output, or a job-level flag that groups
   `sourceCuts`. The ledger (`routers/control_plane.py` `_source_dna_cut_ledger`) must reserve every
   window of a montage.
4. **Runner:** `_run_dossier_source_inner` renders and probes per cut (5.0-9.0/10.0 s check at
   `routers/control_plane.py:4903-4935`). A montage branch would call `render_montage` once per
   output and reuse the same duration probe and QA gate (`policy_for_source_cut`, expected frames
   from `plan.total_frames`).
5. **Census test:** the new runner site must sit in a function that calls `source_cut_is_planned`
   (it does, through `montage_is_planned`); confirm the census accepts that indirection.
6. **Pins:** a commissioned profile means a new format contract with
   `creativeAuthority = {kind, id: source-dna-montage, version: sha256 of the executor file}` and
   updated slug sets in `tests/test_content_format_contracts.py` and `tests/test_no_silent_zero_plan.py`.
7. **Worker (separate repo):** nothing needed while bodies stay 5-9 s. Longer montages need
   `control-plane-worker/src/domain/sourceVideoBounds.js` changed by its owner.

## Questions for the owners

- Is reserving whole windows for 1.5-3 s slices acceptable, or should the ledger learn slice-level
  reservations (more supply, more code)?
- Second kind under `sourced_video`, or a new engine?
- Which pages, if any, should get a montage format first?
- Should montage slices carry the existing per-cut transforms (speed, crop, flip, scrambler)?
  The draft applies none.
