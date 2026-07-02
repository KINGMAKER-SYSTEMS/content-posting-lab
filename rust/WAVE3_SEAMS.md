# Wave 3 punch-list — seam requests surfaced during wave 2

Deferred cleanly (nothing here blocks the wave-2 merge; all tests green). Grouped
by where they land in wave 3 (frontend rework + the three deferred job-runner
endpoints) or as standalone cleanups.

## Job-runner endpoints deferred from wave 2 (the reason wave 2 had no jobs.rs contention)
- `POST /api/video/generate`, `POST /api/clipper/process-batch`, `POST /api/burn/overlay`
  — the legacy runner shapes. The engine already exists (`POST /api/jobs/{generate,clip,burn}`);
  wave 3 decides the final shape as it reworks the frontend to asset-ids, then either
  adds thin adapters or migrates the frontend off the legacy paths.

## Frontend-visible mismatches to resolve in the React rework
- **clipper `download-url`**: now returns yt-dlp *metadata* (`title/duration/width/height/...`),
  not Python's old `{batch_id, files:[...]}` download-then-stage shape. `Clipper.tsx` expects
  the old contract. Either the frontend adapts to metadata-then-stage, or a wave-3 step adds
  the download+stage on top.
- **`/api/video/jobs`** reports a single synthetic `"generating"` entry for in-flight jobs
  instead of the old per-clip status dict (that granularity lives in the deferred generate runner).

## Standalone cleanups (do independently, any time)
- **ZIP writer dedup**: burn, clipper, and video each hand-rolled an identical ZIP_STORED +
  CRC32 writer because `api/Cargo.toml` was owned by the spa-proxy agent that wave. Pull one
  `crate::zip` helper (or add the `zip` crate) and delete the three copies.
- **axum `multipart` feature**: clipper hand-rolled a `multipart/form-data` single-field parser
  for cookie upload. Enable the cargo feature and delete the parser.
- **`media::ffmpeg::color_correct` primitive**: `/api/video/color-correct` currently rides
  `burn_overlay`, which bakes in TikTok encode args (`-r 30`, AAC re-encode). Add a preset that
  preserves source framerate + `-c:a copy` (Python's `STANDARD_ENCODE_ARGS`) so color-correcting
  a generated video doesn't silently change its framerate/audio codec.
- **`stage-streamed` body limit**: the global 100MB `DefaultBodyLimit` in main.rs blocks the
  multi-GB source uploads this endpoint is meant to take. Needs a per-route limit override AND
  true streaming (not `Bytes` buffering) to avoid OOM. main.rs seam.
- **ABN WebSocket proxy**: the reverse proxy buffers-then-streams (fine for ABN's SSE-style
  stream), but true WS-upgrade endpoints aren't handled. Confirm whether ABN uses a real WS
  upgrade anywhere; add upgrade passthrough if so.
- **`burn/import-tos` Supabase fetch**: endpoint validates config + matches the contract shape
  but the actual Supabase REST pull is stubbed ("not yet wired"). Wire it when the TOS import
  path is needed.

## Contract notes
- CUTLIST reclassified: `staging-group/discover-topics`, `staging-group/scan-inventory`, and
  `DELETE /inventory/scan` moved KEEP→CUT — the relational schema makes these brute-force
  recovery endpoints unnecessary (the contract harness caught the misclassification).
- Error-body convention split stands: `{"error":...}` app-wide EXCEPT miniapp uses FastAPI's
  `{"detail":...}` (external agent contract). Decide if unifying matters before cutover.
