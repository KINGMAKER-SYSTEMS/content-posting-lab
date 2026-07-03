# Remaining parity gaps → 100% (post wave-3-runner merge)

## STATUS (2026-07-03, rust-rewrite @ 4409bb9)

DONE this session:
- #1 advanced video params threading (GenParams.extra + replicate builders) ✓
- #2 delete_job path + #3-review delete_file + color-correct confine (unified confine_project_video) ✓
- #4 color_correct source-preserving preset wired ✓
- #5 burn/import-tos Supabase fetch ported ✓
- #6 stage-streamed body limit + true streaming ✓
- Adversarial-review CRITICAL fixes: (a) uploaded absolute source paths accepted (relativize_under_data);
  (b) batch_id path-traversal write primitive closed (ensure_valid_batch_id).
- 277 workspace tests green, clippy clean, contract harness 21 OK / 0 MISSING / 0 SHAPE-MISMATCH.

ALSO DONE (later loop iterations):
- #7 ZIP writer dedup — clipper's copy folded onto core::build_zip_stored (video keeps its _errors.txt variant). ✓
- #8 contract-harness `?project=` fixtures — resolve_target appends a default project; single-mode 24 OK / 4 ERROR (all auth/config 503s) / 0 missing / 0 shape-mismatch. ✓
- #9 ABN WebSocket passthrough — NOT NEEDED: ABN's /stream is SSE (text/event-stream), zero WS endpoints; the proxy's buffer-then-stream already handles it. Verified by inspection. ✓

REMAINING (deliberately skipped — cosmetic, zero behavior impact):
- #3 provider_schemas advertises 3 dropped models (pruna×2, wan-i2v-fast). Harmless dead JSON — the
  frontend picker reads /providers (curated set only) and only looks up schemas by selected id, so these
  are never read. YAGNI; leaving it. (Trim if a future dev finds it confusing.)

Everything with user-visible behavior is at parity. Next phase = wave 4 (shadow run on Railway +
contracttest `diff` vs live Python + `clab migrate` + cutover w/ rollback) — needs a human (prod deploy
+ go/no-go decision), not autonomous dev.

---
## Original gap list (for reference)


Tracking the delta between the Rust binary and the original Python app, so the
existing React frontend runs unchanged. Ordered by parity impact. Items marked
BLOCKED-ON-REVIEW touch files the wave3-runner-review workflow is reading; apply
them in the post-review editing pass to avoid invalidating the review.

## 1. HIGH — video generate drops all advanced model params  [BLOCKED-ON-REVIEW: video.rs, jobs.rs, providers/*]
The frontend renders per-model controls from `/api/video/provider-schemas` and
sends them in the generate multipart form. Python (`routers/video.py:354-373`)
collects them into an `extra` dict and forwards to the provider. Rust
`providers/replicate.rs::build_input` HARDCODES them (e.g. `num_frames: 81`,
`prompt_optimizer: true`) and `GenParams` has no slot — so num_frames,
sample_shift, sample_steps, go_fast, interpolate_output, optimize_prompt,
negative_prompt, lora_weights/scale (+_2), frames_per_second are all silently
ignored.

Fix:
- `providers/mod.rs`: add `pub extra: serde_json::Map<String, serde_json::Value>` to `GenParams`.
- `routes/video.rs` generate: collect these multipart fields into `extra`, coercing
  each value via "parse as JSON, else keep string" (so `"121"`→121, `"true"`→true,
  `"motion, morphing"`→string). Keys: num_frames, frames_per_second, sample_shift,
  sample_steps, go_fast, interpolate_output, optimize_prompt, negative_prompt,
  lora_weights_transformer, lora_scale_transformer, lora_weights_transformer_2,
  lora_scale_transformer_2. (crop_mode already handled separately.)
- `routes/jobs.rs` start_generate (JSON path): default `extra` to empty map.
- `providers/replicate.rs`: port the three builders faithfully, reading from
  `p.extra` with the Python defaults as fallback. Source of truth
  `providers/replicate.py:13-85`:
  - hailuo `minimax/hailuo-2.3`: duration snap (>=8→10 else 6; 1080p→6), resolution
    guard, `prompt_optimizer = extra.optimize_prompt ?? true`, `first_frame_image` if image.
  - wan-t2v `wan-video/wan-2.2-t2v-fast`: num_frames?81, frames_per_second?16,
    sample_shift?12, go_fast?true, interpolate_output?true, negative_prompt (omit if empty),
    lora_weights_transformer(+scale?1), lora_weights_transformer_2(+scale?1).
  - wan-i2v `wan-video/wan-2.2-i2v-a14b`: requires image; num_frames min(?81,100),
    frames_per_second min(?16,24), sample_steps?40, sample_shift?5, go_fast?false,
    negative_prompt (omit if empty).
- `last_image`: intentionally NOT threaded — only the dropped wan-i2v-fast consumed
  it; no curated model does. Leave parsed-but-inert (documented).

## 2. LOW — delete_job path mismatch for generate outputs  [BLOCKED-ON-REVIEW: video.rs]
Generate assets land in `projects/{p}/generated/{job_id}/` but `delete_job`'s
`project_video_dir` containment check targets `projects/{p}/videos/`, so
`files_removed` stays 0 (DB row deletes fine; files leak). Fix the resolution to
match where generate writes. Predates wave 3; now live.

## 3. COSMETIC — provider_schemas advertises 3 dropped models
`provider_schemas` still lists pruna-pvideo, pruna-pvideo-vertical, wan-i2v-fast.
Harmless: the frontend model PICKER comes from `/api/video/providers` (curated set
only); schemas are looked up by selected id, so these are never read. Trim for
tidiness when touching video.rs (optional).

## 4. MEDIUM — /api/video/color-correct changes framerate + re-encodes audio
Rides `burn_overlay` (TIKTOK_ENCODE: `-r 30`, AAC 192k). Color-correcting a
generated video silently changes fps + audio codec. Python uses source-preserving
STANDARD_ENCODE_ARGS (`-c:a copy`, no `-r`). Add a `media::ffmpeg` cc-only preset
that preserves source fps + copies audio; point `run_cc_encode` at it. (media crate
+ video.rs.)

## 5. MEDIUM — burn/import-tos Supabase pull stubbed
Endpoint validates config + matches shape but the Supabase REST fetch returns "not
yet wired". External-client feature. Wire when the TOS import path is exercised.

## 6. MEDIUM — clipper/stage-streamed body limit + true streaming
Global 100MB `DefaultBodyLimit` blocks the multi-GB source uploads this endpoint
exists for; also buffers into `Bytes` (OOM risk). Needs a per-route limit override
+ streamed write. main.rs seam + clipper.rs.

## 7. CLEANUP (non-parity) — ZIP writer dedup
burn/clipper/video each hand-rolled an identical stored-ZIP + CRC32 writer (Cargo.toml
was owned by another agent during wave 2). Pull one `crate::zip` helper (or add the
`zip` crate) and delete the three copies.

## 8. TEST INFRA — contract harness query-param fixtures
`contracttest single` hits burn/{videos,batches,captions} without `?project=` → 400
(they 200 with one). Give the harness a default `?project=` fixture so these verify
OK instead of ERROR. Also unblocks a cleaner wave-4 `diff` run.

## 9. VERIFY — ABN WebSocket upgrade passthrough
Proxy buffers-then-streams (fine for ABN SSE). Confirm ABN uses no true WS upgrade;
add upgrade passthrough only if it does.

---
Cutover (wave 4) is gated on 1, 2, 4 (user-visible behavior) + whatever the review
confirms. 5/6/9 are edge features; 3/7/8 are tidiness/infra.
