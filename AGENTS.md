# Content Lab Devlog

## Purpose

- Owns the Content Lab generation, source clipping, caption-bank, visual render,
  and asset preparation services used before distribution.

## Ownership

- `services/visual_admission.py` owns exact-byte pre-caption OCR/vision-provider decisions;
  the authenticated job visual-admission endpoint queues a bounded background
  whole-job sweep and persists algorithm/byte-bound decisions before
  Control Plane can admit ready video into R2. Every decoded native-resolution
  frame receives Tesseract OCR at the configured `CONTENT_LAB_OCR_LONG_EDGE`
  working edge (default `0`, native); the primary GLM-4.6v-flash provider receives up to
  16 native frames per batch, and a named OpenAI `gpt-4o-mini` or Ollama
  `qwen2.5vl:7b` fallback may receive the same
  batch when the primary is unavailable. If both configured providers are
  unavailable and `REPLICATE_API_TOKEN` exists, the fixed official
  `google/gemini-2.5-flash` Replicate model receives bounded batches of at most
  ten of those same JPEG frames under a short natural-language JSON contract;
  all batches must return a complete valid verdict. A transient pending verdict
  stops the rest of that job's sweep so provider trouble cannot monopolize the
  serialized scanner ahead of other pages.
  `CONTENT_LAB_VISION_FALLBACK_URL` (default
  `https://api.openai.com/v1/chat/completions`) and
  `CONTENT_LAB_VISION_FALLBACK_MODEL` (default `gpt-4o-mini`) configure that fallback; the optional
  `CONTENT_LAB_VISION_FALLBACK_API_KEY` is sent only to the fallback host. The
  optional `CONTENT_LAB_VISION_URL` primary override accepts only the two
  server-owned Z.ai/Zhipu chat-completions URLs and records the answering URL
  host in the decision model block. HTTP 429/provider-1305 primary responses
  are unavailable and trigger the fallback; other hosts fail closed. The
  decision records the answering model, provider, fallback flag and reason. Both
  detectors and complete coverage are required for clean. Production runs on
  Railway at `https://risingtides-content-lab-production.up.railway.app`; the
  fallback endpoint must be reachable from the Railway container or the gate
  remains fail closed. Authenticated job sweeps enter one process-wide executor
  so concurrent Control Plane polls queue behind the single scanner instead of
  persisting `scanner_busy_retry`. Each executor turn scans at most one artifact
  and requeues remaining finalizable artifacts at the tail, preventing a large
  batch from blocking a one-output page. A restart safely makes the prior
  runtime's queued sweep eligible for resubmission.
- Source cut duration accounts for the saved playback speed so both normal and
  fixed recuts deliver 6-11 seconds; preserve the saved speed and video treatment.
  Normalize cut timestamps and extend a fractional missing tail frame to the
  planned output duration; source-window provenance remains unchanged.
- Source cut planning honors the immutable original 60-second minimum for raw
  source libraries and the Worker-supplied global source-window exclusions
  before rendering. Exact page-bound ShipStream masters are already extracted
  immutable bytes, so they may use their first frame; applying the raw-source
  offset again would make every short imported page master unusable. Sourced
  capabilities expose the current recipe’s canonical source identities so the
  Worker filters reservations before its bounded exclusion limit; the Worker
  binds each exclusion to the exact master SHA when one source URL represents
  a multi-file library. Legacy exclusions without a master SHA remain broad.
  The Worker retains final atomic reservation authority for races. Budgets, missing
  tools/credentials, uncertain replies, and changed bytes fail closed.
  This detector has bounded recall; it does not prove semantic absence of all text.
- OCR candidates require Tesseract word confidence of at least 80 and at least
  two alphanumeric characters. Isolated layout glyphs and low-confidence
  guesses are not positive text evidence. A frame without recognized words is
  checked again at 180 degrees under the same deadline. Every candidate frame
  receives its own single-frame visual check, including brief candidates between
  uniformly sampled frames; only visual corroboration may reject it as text. A
  scenery batch must not dilute a transient text candidate. All remaining batches
  of at most 16 frames must be clean within the existing cumulative byte/time
  budgets, and their provider/frame evidence is retained. The algorithm id
  versions these rules so cached decisions from the glyph-based detector are
  rescanned against the same original bytes. Confidence is detector evidence,
  not a calibrated probability or a guarantee of text absence.
  Each batch retains its answering provider, including the configured fallback.

- `services/caption_render.py` owns the typed Dossier-to-render caption contract.
- `services/post_render.py` owns local prepared-final rendering and exact artifact
  receipts; control-plane admission remains downstream authority.
- `services/post_render_jobs.py` and `routers/post_renders.py` own durable pre-lease
  render jobs, authenticated page-scoped status/artifacts and source acquisition.
- `docs/post-render-setup.md` owns prepared-render storage, authentication and
  separate browser/machine ingress configuration before release.
- `services/source_treatment.py` owns actual applied-video provenance emitted by
  generation/source/recovery outputs through `routers/control_plane.py`.
- `services/generation_recovery.py` owns private provider checkpoints and
  cross-process generation/store locks. Checkpoints are not admission authority.
- `services/caption_discipline.py` owns Content Lab's closed validation of the
  caption corpus/register selection already made by Dossier and Control Plane.
- `services/control_plane_source_imports.py` owns bounded public-HTTPS download,
  exact-byte hashing, media probing, and refillable-master normalization for
  page-scoped source-link intake.
- `routers/burn.py` exposes caption rendering and final video compositing.
- `services/roster_public.py` owns what `/api/roster`, `/api/pipeline` and
  `/api/email` may serialise: an allowlist of roster row fields plus a
  credential-key scrub (`CredentialGuardRoute`) applied to every response of
  those routers. `allow_credential_keys` exemptions are pinned by a test and
  may only echo values the request itself created. The Slack pipeline handoff
  says "see Notion" instead of carrying the password. Signup email, password, forwarding address, email
  alias/rule/destination and notes stay in the roster cache and never cross
  HTTP there. It also owns the roster machine credential (CONTROL_PLANE_TOKEN
  or APP_API_KEY) required on roster routes no unauthenticated UI calls.
- `routers/email_routing.py` routes that can change where a page's mail goes
  (POST /destinations, DELETE /rules/{id}, POST /auto-create) require
  `require_control_plane_auth` (CONTROL_PLANE_TOKEN only, never the
  browser-bundled APP_API_KEY; fails closed when unset), normalise the
  destination once and send that value to Cloudflare, honour the optional EMAIL_DESTINATION_DOMAINS allowlist, and
  auto-create never re-points an alias a roster page records without
  `replace: true`. Pipeline mint-alias calls the CF service directly and is
  not gated by these routes.
  GET /destinations (the team's real inbox addresses) requires the same
  credential; only GET /status stays anonymous.
- `services/slack.py` pipeline handoffs never carry the login email, password
  or free-text notes; those fields point at Notion.
- `routers/pipeline.py` /intake refuses (generic 409) an anonymous intake for
  a handle that already has a roster page (either `acct:` id form) or with a
  `notion_page_id` of a roster page that is not an unfinished step-1
  placeholder (placeholder completion must carry its own alias);
  CONTROL_PLANE_TOKEN may override. `notion_page_id` is canonicalised once
  (`services.notion_pages.canonical_notion_page_id`, 32 lowercase hex; else
  400) and that value is used for the check and every Notion call;
  `_patch_page` refuses non-canonical ids before any request. Alias-collision 409s never echo the alias.
- `burn_server.py` exposes the same typed caption-render route on the posting
  Mac's canonical port-8002 Burn runtime for Rail consumption.
- `events.md` is the repository's append-only chronological ledger.

## Local Contracts

- The `/api/` key middleware (`app.py` `_AUTH_SKIP`) exempts only `/api/health`
  and `/api/miniapp/*`, which verifies Telegram `initData` itself.
  `/api/telegram/*` needs the key like every other `/api/` route: the bot
  long-polls, so there is no webhook route to exempt. The UI sends the key
  through `frontend/src/lib/api.ts` (`fetchApi` / `withApiKey`).
- `POST /api/telegram/send` delivers only a media file whose real path is under
  `projects/<project>/<videos|clips|burned|recreate|slideshow-images>/` or the
  legacy `output`/`burn_output` dirs. The volume root beside those dirs holds
  the roster, cookies and Telegram config and is never sendable.
  `/send-batch` and `/assign-batch` take one batch-dir segment and skip files
  that resolve outside it. `routers/video.py` accepts `project` only as one
  directory name under the projects root and checks containment on real paths.
- Path containment uses `services.fsutil.is_within` (real paths, component-wise),
  never a string prefix: project `p` is a string prefix of
  `projects/page_roster.json`. `/api/burn/overlay` also takes `batchId` as one
  directory name. While APP_API_KEY is unset these server-side checks are the
  only guard on `/api/*`.
- `project_manager.is_reserved_volume_dir` names the service-state dirs on the
  projects volume (`_post_render`, `control_plane_generated`,
  `control_plane_recipes`, `agenticnews_assets`, `lost+found`, and any name
  starting with `_`). `sanitize_project_name` refuses them, project listings
  skip them, and `/send` never serves from them. Put new private volume roots
  under a leading-underscore name.
- Format-contract HTTP reads use a separate two-permit thread limiter, not the
  shared synchronous endpoint pool. Preserve per-request registry validation,
  authentication, current-file visibility and fail-closed errors; disk reads
  must remain off the event loop.
- Atomic JSON stores use compact one-shot encoding; preserve flush, fsync,
  atomic replacement and failure cleanup so large job histories do not stall API writes.
  Async production runners perform progress writes in worker threads.
- Recipe listing reuses unchanged file bytes after checking every current file's
  inode, size and timestamps; additions, edits and removals apply on the next read.
  Callers receive independent matching rows, not mutable shared cache entries.
- A registered recipe tuple's bytes are immutable: different bytes under the
  same page/recipe/engine/version remain a 409. Byte-identical bytes from a
  later Dossier lock succeed and advance the stored dossier revision and
  idempotency key atomically, keeping the superseded pairs in
  `priorRegistrations`; replaying any known pair returns success without a
  rewrite. Jobs keep the revision they were created under.
- Capability planning shares one read-only job-history snapshot per unchanged
  file identity. Job creation/progress/admission still load fresh mutable data
  under their existing transaction lock; never mutate the capability snapshot.
- Generated capability planning stops after enough distinct available prompts
  to reach the delivery ceiling; it still searches through reservations and
  duplicate prompts to prove partial capacity or exhaustion. This does not
  change generation quantities, reservation semantics or source recut policy.

- Caption rendering accepts the shared `CaptionStyle` wire fields only. A saved
  caption layout may supply exact line breaks and final-frame outline width;
  otherwise the established outline remains 3 px at 1080x1920.
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
  Every sourced-video capability also returns the unique immutable source URLs
  used by those masters so Control Plane can exclude already-used cross-page
  windows before it creates paid work.
  A source window is a time frame: master SHA, start and length. Every
  whole-second start (plus one ending on the last frame) at every allowed
  length is a distinct clip. Queued, running and completed jobs reserve their
  exact time frames across recipe revisions and library versions of the same
  master bytes, so a new recipe cuts new time frames instead of re-cutting
  delivered ones; failed jobs release theirs. Plans prefer footage that
  overlaps earlier cuts least, break ties by a per-job seed recorded as
  `cutPlanSeed`, and never hold two overlapping cuts of one master.
  Exhausted libraries remain visible with `maxQuantity: 0` so Control Plane can
  distinguish source exhaustion from an unregistered recipe; job creation then
  answers 409 `master_windows_exhausted`. Job creation remains exact and
  all-or-nothing; it never silently returns fewer clips than requested.
  Legacy async jobs without recoverable checkpoints fail closed after runtime
  replacement; generated jobs with durable provider identity retain their
  original prompt reservations while the same job resumes. Source-window
  reservation and source-import restart rules remain independent.
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
  hashes, byte counts, and media facts. An input already matching that exact
  refillable contract is retained byte-for-byte without another encode.
  Source-import artifact URLs use only the
  configured `CONTENT_LAB_PUBLIC_ORIGIN`; the separate
  `CONTENT_LAB_CONTROL_PLANE_ORIGIN` remains the authority for reading page-vault
  media from Control Plane. Source imports never load shared platform/browser
  cookie stores because their allowlisted permanent URLs are public. Content Lab never admits that artifact into
  ShipStream or mutates the page source manifest. A repeat with
  the same request and idempotency key may resurrect only the exact
  `source_import_runtime_restarted` failure; it reuses the job id under the
  current runtime after cleaning its artifact root. A status read retires an
  active import after its six-hour bounded runtime, measured from the latest
  restart, and sends it through that same idempotent restart path. Every other
  terminal failure remains terminal.
- ShipStream `content_lab_page_source_import` manifests are executable only
  when their authority repeats the exact Control Plane page id, page handle,
  Notion page id, and replacement eligibility required by the active Master
  Pages projection.
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
- The Pipeline intake password comes only from the server-side
  `DEFAULT_INTAKE_PASSWORD`, read per request. Unset or blank makes
  `/api/pipeline/mint-alias` and `/intake` return 503
  `intake_password_not_configured` before any alias, Notion or roster write.
  Never add a fallback default or ship an account password in the frontend.
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
  near-9:16 input at least 1080 pixels high. Exact and chroma-aligned frames
  scale directly; native provider frames within three percent of 9:16 are
  center-cropped at the edge and scaled to 1080x1920 before caption composition
  so provider sizing cannot stretch or block otherwise usable vertical media.
  Delivery never changes the grade, speed, font or caption style. Missing/unspecified input pixel
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
  The artifact serializer also reconstructs that receipt for a completed
  generated, sourced-video, or slideshow job only when the persisted clip
  already carries its applied speed and crop and the exact registered recipe
  still matches the job's recipe hash. This recovers paid outputs created while
  receipt serialization was missing without asserting treatment for older
  receiptless clips.
  Recovery crops inherit proven parent video treatment; they never assert the
  current desired treatment for historical bytes. `sourceRecipeTreatment` is
  recipe context and does not claim a caption overlay already exists.
  Truck replenishment reuses a preserved master only when its exact producer
  receipt proves the requested video grade, speed and crop. Missing, malformed
  or mismatched treatment skips that master and allows normal fresh generation;
  caption-only changes still permit reuse. This does not grant content approval.
- `services.ffmpeg.run_color_correct` serializes its ffmpeg subprocesses within
  each service process so simultaneous asynchronous refill jobs cannot exhaust
  container memory during 1080x1920 libx264 encoding.
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
  Downloaded attempt `source.mp4` files are disposable scratch and are removed
  after successful or failed execution, including partial fetches. Cleanup must
  preserve final artifacts, receipts and the original attempt outcome.
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
  After a repaired source route, a fresh authenticated idempotency key may
  reopen the identical job only when it exhausted all attempts with
  `source_response_rejected`; replaying that recovery key only observes the
  current job.

- Replicate video generation (`providers/replicate.py`) retries only faults
  that cannot buy a second prediction: submission HTTP 429 (honouring
  `retry_after`, capped), 500 and 503 (no prediction id returned in every
  observed case) or a never-established connection, at most four
  submissions; 502/504 gateway results may hide a created prediction and
  are terminal; poll transport/429/5xx/unparseable
  replies on the same prediction within its 600-second deadline; and one
  resubmission of Replicate's "Prediction interrupted (code: PA)". A read or
  write fault after the submission may have reached Replicate is not retried.
  402 insufficient credit, validation and provider-side failures are terminal.
  Deadline or persistent poll loss cancels the prediction. Model, input and
  recipe-pinned provider never change between attempts.
- A zero-output provider failure keeps the terminal `provider_generation_failed`
  status contract and additionally persists `providerFailure` (closed `class`,
  provider, model, call index, prediction id, bounded detail) in the job store
  only; the Control Plane's strict status schema is unchanged.
- AI generation reserves prompt combinations only for queued or running jobs.
  A confirmed Replicate moderation refusal (class `moderation`, prediction id,
  "Replicate failed:") retains its prediction and may be retried at most twice
  for that planned call (`services/moderation_retry.py`), each time with the
  next fixed, deterministic prompt rewording; provider, model, safety settings
  and the immutable prompt plan never change and no alternate engine is used.
  Retries draw on a per-page UTC-day budget
  (`CONTENT_LAB_MODERATION_RETRY_DAILY_BUDGET`, default 6, 0 disables),
  counted from every job's durable `generationAttempts` rows; first attempts
  never consume it. Each retry has its own prediction checkpoint key `i:k`.
  A retried clip records the sent `promptHash`, the plan's `basePromptHash`
  and `promptVariant`; recovery accepts a variant hash only from a succeeded
  attempt row of that call. Each attempt records its estimated cost, and the
  job keeps `moderationRetryCostUsd`. When retries or budget are spent (or the
  refusal is an auth failure inside the moderation check), the refusal stays
  terminal with a named `terminal` reason in `providerFailures`, and only the
  other distinct candidates of the original plan continue; the status contract
  is unchanged. Persist every failure in the private job store and count only
  successful calls as completed. Credit (402), provider auth (401/403),
  transport, ambiguous submission and other failures are never retried and
  still stop the remaining plan.
  Provider failures preserve already treated, claimed outputs as a
  completed underfilled batch, retaining the provider error and planned/completed
  call counts. Artifact count stays truthful; downstream refill plans the deficit
  with a new job rather than replaying the failed provider call. All-zero-output
  failures, explicit cancellation and changed page strategy retain their failure behavior.
  Final clip admission applies the same active-only prompt rule while retaining
  completed output SHA rejection and within-job prompt exclusion.
  Completed prompts remain approved inputs for fresh rendering; prompt text is
  not consumable media inventory. A new refill uses a new durable job, while
  idempotent replay returns the original job and downstream output-byte checks
  continue to prevent duplicate asset admission. Source-video window reservations
  remain unchanged.
- New Replicate generation jobs durably record submission intent before the
  request and the accepted prediction id before polling. Resumption validates
  the exact model/input hash and polls that same id; an intent with no known id
  is ambiguous and must never be submitted again. The existing one-time PA
  interruption retry remains bounded across restarts. Processing deadlines do
  not reset; an already-completed prediction may still be read after downtime.
- Application shutdown pauses checkpointed generation instead of deleting its
  paid artifacts. Authenticated, exact-page status/idempotency reads resume the
  same active job after replacement. A kernel-held per-job lock prevents two
  runtimes driving it, and the existing JSON transaction lock also serializes
  cross-process writers. Lock-file presence alone is not ownership. Explicit
  cancellation and legacy jobs without checkpoints remain terminal.
- Recovery verifies preserved artifact paths, sizes and hashes and rechecks
  the current page intent, recipe and executor. Completed calls/crop sets are
  retained exactly; unfinished candidates render into a separate directory so
  they cannot overwrite claimed bytes. Incomplete crop sets are never admitted
  as complete calls. Corrupt recovery evidence remains private for inspection;
  it is not served or replaced by fresh paid work. No provider safety, caption,
  sound, treatment, public status schema or downstream QA rule changes.
- `silhouette-truck` is a still-first format. FLUX.2 Pro creates one native 9:16
  photograph of exactly two embracing adult silhouettes beside one complete
  pickup in a field; WAN/I2V, anchor drift, and synthetic motion are not legal
  substitutes. Retain the exact provider image and its hash in the artifact
  manifest, then hold that image without pan, zoom, or interpolation in a
  seven-second 1080x1920 MP4 so the existing caption, sound, QA, and posting
  contracts remain unchanged.
- `recipes/generation/silhouette_stills.v1.json` versions only the silhouette
  replacement. Keep the shared prompt catalog and unrelated format/profile
  versions unchanged so switching silhouettes does not invalidate other pages.

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

- Run `pytest -q tests/test_visual_admission.py` for observed boat-frame OCR
  noise, short readable text, rotated single-frame text, complete coverage,
  unavailable providers, exact-byte binding, and cached-decision behavior.

- Run `pytest -q tests/test_production_image_imports.py` for isolated imports from
  the Dockerfile's backend file selection. Build the Docker image for release;
  its app-import smoke check must pass before deployment.
- Run `pytest -q tests/test_generation_restart_recovery.py tests/test_replicate_generation_retry.py`
  for paid-request identity, ambiguous submission, shutdown, crop recovery,
  processing deadlines and cross-process checkpoint transaction checks.
- Run `pytest -q tests/test_generation_moderation_retry.py tests/test_generation_moderation_isolation.py`
  for bounded varied moderation retries, the daily retry budget, fail-fast
  credit/auth classes and restart of a retried call.
- Run `pytest -q tests/test_post_render_jobs.py tests/test_source_treatment.py`
  for durable crash/retry/lock/auth/source-grant and applied-video provenance checks.
- Run `pytest -q tests/test_post_render.py` for prepared rendering, actual MP4
  decode, treatment-once preservation, byte-budget retry and bounded failures.

- Run `pytest -q tests/test_no_shipped_default_password.py` after `npm run build`
  in `frontend/`; it fails if a retired credential (stored only as a SHA-256
  digest) appears in backend or other repository sources or the built bundle.
  Run `pytest -q tests/test_pipeline_api.py` for the intake-password 503 path.

- Run `pytest -q tests/test_caption_render_contract.py` for the typed caption
  contract and `pytest -q tests/test_burn_and_captions_api.py` for Burn API
  regressions. Run `pytest -q tests/test_burn_quality_gate.py` for legacy and
  typed overlay placement gates. Run the control-plane recipe, generation, and
  source-execution test files together when changing a Dossier recipe schema.

## Child devlog Index
