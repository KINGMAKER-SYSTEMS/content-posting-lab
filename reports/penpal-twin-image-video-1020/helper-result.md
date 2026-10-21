# Current reviewed implementation

Scoped image-video is integrated into the existing typed slideshow admission and
runner path in this branch. Remote focused suite: **245 passed in4.85s**. Tests
capture Syzygy request and actual grade-call parameters; no live render occurred.
Both pages require brightness.85 exactly once, source words/audio remain blank,
and only their pinned originals are accepted. Missing source index/live metadata,
review/deployment and actual runtime render/admission remain pending.
Legacy profile and contract entries are unchanged; both prior executor files are
byte-identical to frozen233. See legacy-preservation.json and focused-tests.txt.

The initial provider report below is retained as historical evidence. Its Twin
gain1.0 assumption and stated integration TODOs were independently corrected by
the later reviewed implementation.

---

# PenPal / Twin image-video helper — result

## Created (this seat only)

- `services/control_plane_image_video.py` — pure, closed photo-to-MP4 preparation helper.
- `recipes/executors/syzygy-image-video.v1.json` — closed page -> ordered photo-plan executor config.
- `tests/test_control_plane_image_video.py` — 16 tests, all passing.
- `reports/penpal-twin-image-video-1020/helper-result.md` — this file.

No shared files were modified. `services/control_plane_slideshows.py`,
`routers/control_plane.py`, and `registry/contracts` remain owned by the other
seat. Nothing was committed, deployed, rendered, or installed.

## What the helper does

- **Strict closed config parsing** (`parse_plan_config`, `image_video_executor_contract`):
  exact top-level/object field sets, `syzygy_r2_library` source, single-plan
  selection/reservation, lane/subject regex, finite non-boolean numbers, focal
  in `[0,1]`, `cropMode == cover`, strict 64-hex SHA-256, positive bytes, and
  durations that sum exactly to the declared total.
- **Exact page-bound plan identity**: `image_video_plan(page_id)` returns the one
  ordered plan with a SHA-256 `signature` binding page, library id, ordered media
  (key, sha256, bytes, duration, focal, crop, ken_burns), total duration and
  brightness provenance. Deterministic and never permuted; max one plan per page.
- **Honest exhaustion**: `plan_image_video(page_id, unavailable_signatures)`
  returns `[]` once the plan's signature is reserved (or for unknown/foreign
  pages). No fake variety, no offset/stride synthesis.
- **Library identity / bytes / hash validation**
  (`validate_image_video_library`): every configured object's dedicated key must
  be observed with its exact bytes and SHA-256; missing/wrong/duplicate
  observations fail closed. Extras in the live library are ignored and never
  selected. Returns a canonical snapshot hash over the ordered configured objects.
- **Supported Syzygy photo spec** (`build_image_video_spec`): explicit per-media
  `type/library/duration/focal_x/focal_y/crop_mode/ken_burns`; no
  `template_slug`/`cuts`/flow-stage defaults; blank `hook`/`lyrics`/`captions`/
  `textCards`+`text_cards`/`lyrics_lrc`/`lyrics_required`/`uses_original_audio`,
  empty `effects`/`overlays`; `audio` is timing only (`section_start: 0`,
  `section_end: 12`) with no source URL.
- **Receipt with idempotency** (`submit_image_video_render`): injectable
  `request_json` callable (same signature as the existing slideshow
  `_request_json`), idempotency key scoped to page + recipe + plan signature.
  Returns `(job_id, quality)` only.

## Plan summary

| Page | Library | Media | KenBurns | Brightness (later boundary) |
|---|---|---|---|---|
| `acct:pen-pal3` | `syzygy:animals:pen-pal3` | 3 × 4s, focals (.05,.5) (.5,.5) (.7,.5) | `{dir:in, zoom:.025}` | 0.85 (once) |
| `acct:twin47996` | `syzygy:interiors:twin47996` | 1 × 12s, focal (.5,.5) | `{dir:none, zoom:0}` (zero KenBurns) | 1.0 (unchanged) |

Both are blank-text / no-source-audio. `.85` brightness is carried only as
`brightnessGainFromOriginals` provenance and is **never** embedded in the render
spec or the render receipt; it is applied exactly once by the existing
source-treatment boundary later (not implemented here, no fake receipt).

Runtime canonical dedicated keys reuse each original filename under the page's
own library: `animals/pen-pal3/photos/<samefilename>` and
`interiors/twin47996/photos/<samefilename>`. The original `kitten-flowers` /
`lamplit-apartment` keys are retained as `sourceKey` provenance only. Sources are
not yet indexed, so no runtime success is claimed.

## Verification

```
python -m pytest -q tests/test_control_plane_image_video.py
# 16 passed
```

Negative coverage includes: unknown/foreign page, wrong bytes/SHA, missing and
duplicate observations, extra objects never selected, single-plan exhaustion,
malformed/nonfinite/bool/negative config, wrong filename/lane/subject keys,
duration-sum mismatch, and silent receipt never claiming an applied gain.

## Remaining integration work (other seat)

1. In `resolve_slideshow_recipe` / SlideshowRecipe dispatch, add an
   `image-video` executor kind that calls `image_video_plan(page_id)` +
   `validate_image_video_library(page_id, observations)` +
   `build_image_video_spec(plan)` + `submit_image_video_render(...)`, gated on
   `executorId == "syzygy-image-video"` and a matching `contentEngine`/format.
2. Wire the later source-treatment boundary to apply
   `brightnessGainFromOriginals` (0.85 for PenPal, 1.0 for Twin) exactly once,
   and emit the real applied-video receipt — this helper deliberately does not.
3. Source indexing must populate the dedicated R2 keys before any runtime
   library validation can succeed; `_request_json`/`_runtime` still fail closed
   until `SYZYGY_API_URL`/`SYZYGY_API_KEY` + `CONTENT_LAB_GENERATION_MODE=ready`.

## Assumptions to confirm against the real Syzygy parser

- "zero KenBurns" is encoded `ken_burns: {"dir": "none", "zoom": 0}`.
- The photo parser sizes output via the `audio.section_start/section_end` timing
  section (mirroring the existing photo wire's `section_end`-driven duration),
  so `section_end` is set to the summed media duration (12).
- The render spec intentionally carries no brightness field, so the later
  grade is not double-applied by the renderer.


## Independent review and integration after helper completion

The original helper report above is historical provider output. Review corrected
Twin gain1.0 to the latest requested .85, reused the existing Syzygy HTTP transport,
and added rejection of forged/foreign current plans before any provider call.
The scoped config now uses existing executorId syzygy-slideshow. The new image-video
format extends the existing recipe/address/planning/typed admission/runner path;
original legacy JSON/profile definitions stay unchanged. Actual PNG/photo wire is
blank, transitions0, timing-only audio section0–12, with source audio disabled.
The existing run_color_correct boundary receives brightness-15% exactly once;
treatment receipt and pinned source provenance are produced only after that boundary
returns successfully and output is hashed. Tests use captured external boundaries
and do not claim a live media render, index, deployment, or posting admission.

Final catalog check: page-specific minimum/template counts are3 for PenPal and1 for Twin; both typed catalog fixtures verified.
