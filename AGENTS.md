# Content Lab Devlog

## Purpose

- Owns the Content Lab generation, source clipping, caption-bank, visual render,
  and asset preparation services used before distribution.

## Ownership

- `services/caption_render.py` owns the typed Dossier-to-render caption contract.
- `services/post_render.py` owns local prepared-final rendering and exact artifact
  receipts; control-plane admission remains downstream authority.
- `services/post_render_jobs.py` and `routers/post_renders.py` own durable pre-lease
  render jobs, authenticated page-scoped status/artifacts and source acquisition.
- `docs/post-render-setup.md` owns prepared-render storage, authentication and
  separate browser/machine ingress configuration before release.
- `services/source_treatment.py` owns actual applied-video provenance emitted by
  generation/source/recovery outputs through `routers/control_plane.py`.
- `services/caption_discipline.py` owns Content Lab's closed validation of the
  caption corpus/register selection already made by Dossier and Control Plane.
- `services/control_plane_source_imports.py` owns bounded public-HTTPS download,
  exact-byte hashing, media probing, and refillable-master normalization for
  page-scoped source-link intake.
- `routers/burn.py` exposes caption rendering and final video compositing.
- `burn_server.py` exposes the same typed caption-render route on the posting
  Mac's canonical port-8002 Burn runtime for Rail consumption.
- `events.md` is the repository's append-only chronological ledger.

## Local Contracts

- Caption rendering accepts the existing Rust `CaptionStyle` wire fields only.
- Dossier recipe v4 is the executable v3 production selection plus the exact
  Control Plane `captionDiscipline` wire object. Content Lab validates and
  preserves that immutable selection; it does not choose a corpus, sentiment,
  caption, or parallel taxonomy.
- For `sourced_video`, the exact Master Pages page and vault may project the
  page-bound `shipstream.source-manifest.v1` into the existing immutable
  `SourceDnaLibrary` contract. A valid page master is preferred; an explicitly
  page-bound historical-posted-cut recovery library is legal when the manifest
  states the original source is unavailable. Handle, Notion page id, niche,
  engine, delivery mode, format, SHA, byte count, duration, and R2 key must all
  match; no niche-wide or cross-page source fallback is allowed.
  Source-manifest reads are hard size-bounded; transport failure is reported as
  unavailable rather than falsely reported as missing. The selected Content
  Lab format must match the Master Pages niche before a source is displayed or
  executed.
- Capability and job execution dispatch only to the resolver named by the
  publication's closed content engine. A sourced-video publication never probes
  the AI-video resolver, and an unknown engine exposes no executor.
- Sourced-video capability quantity is the lesser of the executor's per-job
  ceiling and the unique source windows still reservable from durable job truth.
  Exhausted libraries remain visible with `maxQuantity: 0` so Control Plane can
  distinguish source exhaustion from an unregistered recipe. Job creation
  remains exact and all-or-nothing; it never silently returns fewer clips than
  requested.
- `POV — Scenic` is commissioned through the same page-scoped source recut
  executor as Night Core and Dirtbike. It may use only the exact source library
  bound to that Master Pages row; it may not borrow another page's footage or
  fall back to AI generation.
- Dossier projects the same ShipStream manifest's approved cuts as a separate
  page-scoped derivative library. Every displayed cut must retain its exact R2
  key, output SHA, parent SHA/type, source window, speed, output duration, media
  facts, and review record. Approved cuts never become source masters, never
  widen across pages, and never change the executable source-library version.
- Source-link intake requires the authenticated control-plane lane, one exact
  current Master Pages intent/hash, and the matching complete, commissioned
  sourced-video format/profile. `CONTENT_LAB_SOURCE_IMPORT_HOSTS` is a required
  comma-separated server-owned host allowlist; exact hosts and their real
  subdomains are accepted, while unconfigured, private, or unlisted hosts fail
  closed. Permanent source URLs may retain necessary platform parameters, but
  tracking parameters are removed and credential-bearing query parameters are
  rejected before durable storage. Intake runs through a two-job global gate
  with one active job per page, bounded TLS-verified yt-dlp/ffmpeg subprocess
  groups, disk/workspace/duration/byte preflights, and complete partial cleanup.
  It returns a durable job artifact only after normalization to muted
  H.264/yuv420p 1080x1920 at 30fps, preserving normalized and original download
  hashes, byte counts, and media facts. Source-import artifact URLs use only the
  configured `CONTENT_LAB_PUBLIC_ORIGIN`; the separate
  `CONTENT_LAB_CONTROL_PLANE_ORIGIN` remains the authority for reading page-vault
  media from Control Plane. Content Lab never admits that artifact into
  ShipStream or mutates the page source manifest. A repeat with
  the same request and idempotency key may resurrect only the exact
  `source_import_runtime_restarted` failure; it reuses the job id under the
  current runtime after cleaning its artifact root. Every other terminal
  failure remains terminal.
- A publishable caption render requires exact caption text and a complete page
  style: font, size, color, position, alignment, and line balance.
- Resolve fonts only from Content Lab's installed, advertised TikTokSans files.
  Unsupported, missing, or unreadable font bytes fail closed.
- Explicit caption line breaks must survive rendering. The line-balance control
  may add balanced breaks inside each explicit line but may not remove an
  explicit break or change word order.
- Return deterministic 1080x1920 overlay bytes plus caption, style, font, plan,
  and artifact hashes so downstream Rail receipts can bind the final post bytes.
- Legacy quality-gate calls retain the established centered burned_003 geometry.
  Typed calls validate the overlay against the exact declared position,
  alignment, and offset instead of forcing every page back to that legacy look.
- Do not publish or queue a TikTok post from Content Lab without explicit user
  authorization.
- The machine roster refresh and roster snapshot share one sanitized canonical
  Master Pages projection. Refresh returns that projection's exact count and
  full SHA-256 content hash plus a server-owned completeness flag; consumers may perform
  destructive alias retirement only when a subsequent snapshot matches both.
  Raw Notion row count is diagnostic only and must never stand in for the
  projected page count.

- Prepared-post rendering accepts an immutable slot/source/caption/treatment
  request. The source must already have the requested video grade, speed and
  crop, proven by source-bound applied-video evidence. Caption style belongs to
  final rendering and may change without regenerating an otherwise matching
  source. Unknown or different video provenance requires regeneration. This renderer adds the typed caption and
  delivery encoding only; it never repeats grade, crop or speed.
- Prepared artifacts require source-byte verification and upright square-pixel
  9:16 input at least 1080 pixels high, allowing at most two horizontal source
  pixels of chroma-alignment rounding. Delivery encoding scales the complete
  frame to 1080x1920 before caption composition; it never changes the selected
  crop, grade, speed, font or caption style. Missing/unspecified input pixel
  aspect retains the full coded frame and gets explicit square-pixel metadata
  during encoding; an explicit non-square ratio remains rejected. Artifacts require
  real final H.264/yuv420p probing, complete video decode, and a QA frame extracted
  from the final MP4. Exact receipt bytes bind the final/source/caption/treatment
  and QA hashes to the slot, page, program, account and device. The control-plane
  artifact store owns admission; a renderer receipt alone is not ready authority.
- Prepared delivery caps final video at 22 MiB and QA JPEG at 2 MiB. One automatic
  bitrate retry may fit the video budget without changing content or treatment.
  Persistent overflow or failed decode yields no artifact result. Subprocesses
  have bounded output and process-group lifetime and cannot use network protocols.

- New generated and sourced outputs emit `content-lab.source-treatment.v1`
  evidence after actual rendering and output hashing: source SHA, normalized
  video treatment, full source recipe context, recipe hash and generation job.
  Recovery crops inherit proven parent video treatment; they never assert the
  current desired treatment for historical bytes. `sourceRecipeTreatment` is
  recipe context and does not claim a caption overlay already exists.
  Truck replenishment reuses a preserved master only when its exact producer
  receipt proves the requested video grade, speed and crop. Missing, malformed
  or mismatched treatment skips that master and allows normal fresh generation;
  caption-only changes still permit reuse. This does not grant content approval.
- Durable preparation is exposed at `/api/control-plane/v1/post-renders`.
  Every request requires the existing control-plane bearer and exact
  `X-RT-Page-Id`; enqueue also requires `Idempotency-Key`. Status, retries,
  provenance updates and final/QA/receipt downloads use the same page boundary.
  No endpoint accepts a source URL or local path.
- `CONTENT_LAB_POST_RENDER_ROOT` must name persistent private storage. Two OS
  worker permits bound concurrency; per-job locks are explicitly unlocked before
  close. SQLite WAL holds requests, attempts and idempotency records. Restart
  recovers a completed hash-bound output before rerendering; partial attempts
  remain unservable. Transient retries back off and stop after three attempts.
- Prepared source fetch uses only the configured HTTPS
  `CONTENT_LAB_POST_RENDER_SOURCE_ORIGIN` (production machine ingress:
  `https://content-buckets.risingtidesviral.com`)
  and fixed pending-artifact source route, authenticated with server-owned
  `CONTROL_PLANE_SERVICE_ID` / `CONTROL_PLANE_SERVICE_SECRET`. Redirects,
  unexpected MIME/encoding, missing lengths and source-SHA mismatches fail.
  This setting never falls back to `CONTENT_LAB_CONTROL_PLANE_ORIGIN`, which
  remains the browser ingress for existing page-vault media. Source preparation
  does not acquire a phone lease.
- Missing or mismatched source-video evidence becomes a per-job
  `regeneration_needed` state, leaving other jobs available. A zero-attempt job
  may accept an authenticated provenance revision while its immutable slot,
  source, caption and requested treatment stay identical. Original enqueue
  idempotency records remain immutable; replay returns current job status.

## Work Guidance

- The production Docker image uses explicit COPY paths. Include every required
  backend module and import `app` during the image build; checkout-only imports
  are not proof that the packaged service can start.
- Reuse the current TikTokSans fonts and production Burn geometry. Do not add a
  parallel caption-style vocabulary or silently substitute a font.
- Keep browser preview, backend render, and Rail consumption on one versioned
  contract; unknown fields and clipped output are errors, not fallbacks.
- The hosted API and the port-8002 Burn runtime must call the same renderer
  module and return the same versioned schema and hashes.

## Verification

- Run `pytest -q tests/test_production_image_imports.py` for isolated imports from
  the Dockerfile's backend file selection. Build the Docker image for release;
  its app-import smoke check must pass before deployment.
- Run `pytest -q tests/test_post_render_jobs.py tests/test_source_treatment.py`
  for durable crash/retry/lock/auth/source-grant and applied-video provenance checks.
- Run `pytest -q tests/test_post_render.py` for prepared rendering, actual MP4
  decode, treatment-once preservation, byte-budget retry and bounded failures.

- Run `pytest -q tests/test_caption_render_contract.py` for the typed caption
  contract and `pytest -q tests/test_burn_and_captions_api.py` for Burn API
  regressions. Run `pytest -q tests/test_burn_quality_gate.py` for legacy and
  typed overlay placement gates. Run the control-plane recipe, generation, and
  source-execution test files together when changing a Dossier recipe schema.

## Child devlog Index
