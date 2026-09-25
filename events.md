time: [17:24] [09-01-26]
agent: [codex] [gpt-5] [caption_renderer_bridge]
worktree: [feat/typed-caption-render-plan-20260901] /private/tmp/content-lab-caption-renderer
type: [feature-request]: Typed caption render contract
area: [backend]: Dossier and Calendar caption bytes for Rail

Added `POST /api/burn/caption-render/v1` and the reusable typed renderer in
`services/caption_render.py`. The contract accepts the existing Rust
`CaptionStyle` wire fields, resolves only installed non-italic TikTokSans font
bytes, preserves explicit line breaks, returns deterministic 1080x1920 PNG
bytes plus caption/style/font/plan/artifact hashes, and fails closed on missing
fonts, unknown fields, incomplete styles, or clipped output. Verification:
`tests/test_caption_render_contract.py` 14 passed; Burn API regression selection
40 passed. No deployment, video mutation, phone action, scheduling, or post.
_________________________________________________________________________________

time: [17:00 EDT] [24-09-26]
agent: [Codex desktop] [gpt-6-astra]
worktree: [fix/source-master-loop-20260924] [/Users/ecfromthedc/dev/wt/source-master-loop]
type: [bug report] [durability]: Preserve full creator masters and safe retries
area: [backend] [testing]

Adversarial review of the long-master intake found that source-import mode still
preferred a Google Drive streaming derivative over its original `source` format,
the post-download capacity gate counted bytes already present on disk, and an
expired live runner could continue deleting or writing inside a retry's reused
artifact root. Source imports now prefer the original source format with bounded
fallbacks, reserve only the additional normalized output after download, and
cancel plus join an owned expired runner before allowing an idempotent retry.
The task registry also removes only the exact completed task so an old callback
cannot evict a replacement. The expanded source-import, yt-dlp, execution, and
production-image suites pass 82 tests; a live extractor probe selects the 28:22
Google Drive master as format `source` rather than the 137+140 derivative.

_________________________________________________________________________________

_________________________________________________________________________________

time: [17:51] [09-01-26]
agent: [codex] [gpt-5] [caption_renderer_bridge]
worktree: [feat/typed-caption-render-plan-20260901] /private/tmp/content-lab-caption-renderer
type: [feature-request]: Port-8002 typed caption runtime
area: [backend]: Rail-reachable caption render contract

Exposed the existing `content-lab.caption-render-request.v1` contract through
the posting Mac's standalone `burn_server.py` route at
`POST /api/burn/caption-render/v1`. The port-8002 route calls the same renderer
module as the hosted Burn router and preserves the same fail-closed schema and
artifact hashes. Verification: `tests/test_caption_render_contract.py` 15
passed, including the standalone FastAPI route. No video, phone, scheduler,
lease, or post mutation.
_________________________________________________________________________________

_________________________________________________________________________________

time: [17:58] [09-01-26]
agent: [codex] [gpt-5] [caption_renderer_bridge]
worktree: [feat/typed-caption-render-plan-20260901] /private/tmp/content-lab-caption-renderer
type: [feature-request]: Typed caption quality gate and local rollout
area: [backend]: Content Lab Burn runtime on port 8002

Extended the existing overlay quality gate to validate typed renders against
their exact Dossier position, alignment, and offset while preserving the legacy
centered burned_003 gate for untyped callers. Deployed the typed renderer and
gate to the existing launchd port-8002 runtime; health is 200 and both
middle/center and top/left bridge smokes passed. The top/left smoke produced
style SHA `sha256:512514a815f9516244f8aeff4d0f54fa36a068fd3642c3532a11633f1d0f939f`
and overlay SHA `sha256:dc3275888cc20abf27552254596daf27cc022a0d85cff5565c7547e5dc1d1d51`.
Verification: caption-render plus quality-gate tests 24 passed. No video,
scheduler, slot, lease, device, phone, or post mutation.
_________________________________________________________________________________

_________________________________________________________________________________

time: [19:22] [09-01-26]
agent: [codex] [gpt-5] [dossier_recipe_v4_bridge]
worktree: [fix/dossier-recipe-v4-caption-20260901] /private/tmp/content-lab-caption-renderer
type: [bug report]: Dossier recipe v4 registration seam
area: [backend]: Content Lab recipe, generation, and source execution

Added closed support for `dossier.recipe-spec.v4`, preserving the exact
Control Plane caption corpus, canonical sentiment register, and optional
slingshot share beside the existing typed production selection. Content Lab
does not choose or reinterpret the caption selection. Both AI generation and
sourced-video recut resolution now retain the exact v4 recipe bytes. Focused
verification: 53 recipe, generation, source-execution, and dossier-execution
tests passed. No deployment, provider generation, video mutation, scheduler,
slot, lease, device, phone, or post action.
_________________________________________________________________________________

_________________________________________________________________________________

time: [19:58] [09-01-26]
agent: [codex] [gpt-5] [shipstream_source_projection]
worktree: [fix/shipstream-source-projection-20260901] /private/tmp/content-lab-source-vault.hRWhhH
type: [bug report]: ShipStream page source missing from Dossier and recut execution
area: [backend]: Master Pages, ShipStream, and Content Lab source projection

Projected each exact Master Pages ShipStream vault source manifest into the
existing page-scoped source-library contract. Registered page masters and
explicitly page-bound historical posted cuts now appear as Dossier source
options and resolve through the recut executor without accepting superseded
generic masters, foreign pages, or refill-cut drift. Live Love Late Night Walks
and Things I Left Unsaid manifests parsed successfully with 17 and 12 exact
page sources. Focused control-plane, Dossier, source-library, and execution
verification: 105 passed. No generation, bucket write, scheduler, phone, slot,
lease, or post action.
_________________________________________________________________________________

_________________________________________________________________________________

time: [20:05] [09-01-26]
agent: [codex] [gpt-5] [shipstream_source_projection]
worktree: [fix/shipstream-source-projection-20260901] /private/tmp/content-lab-source-vault.hRWhhH
type: [review]: Source-manifest authority and availability hardening
area: [backend]: Dossier source catalog and recut execution

Hardened the projection after independent review. Current exact-page manifests
whose Notion id lives in source authority remain compatible, manifest format
must match the Content Lab format selected by Master Pages, chunked responses
stop at the byte ceiling, transient transport failure is distinct from missing
or invalid source, and async job execution moves source resolution off the
event loop. Recipe validation reuses the already-resolved library instead of
fetching it twice. Manifest-backed version-drift coverage now exercises the
catalog-to-executor path. Focused verification: 107 passed. No deploy,
generation, bucket write, scheduler, phone, slot, lease, or post action.
_________________________________________________________________________________

_________________________________________________________________________________

time: [20:48] [01-09-26]
agent: [codex] [gpt-5] [shipstream_approved_cut_projection]
worktree: [fix/shipstream-approved-cut-projection-20260901] /private/tmp/content-lab-approved-cuts.lyAYF1
type: [bug report]: Dossier falsely reports existing ShipStream cuts as missing
area: [backend]: ShipStream source manifest and Dossier ingredient catalog

Added a separate typed projection for page-scoped approved derivative clips in
the same ShipStream manifest already used for source resolution. Dossier now
receives each exact output SHA and R2 key with its parent source, cut window,
speed, media facts, and review record while keeping source masters as the only
recut authority. The live recovered manifests parse as 17 parents plus 18 cuts
for Love Late Night Walks and 12 parents plus 18 cuts for Things I Left Unsaid.
Focused Dossier, source-manifest, registry, recipe, generation, and source-
execution verification: 137 passed. No generation, bucket write, scheduler,
device, phone, slot, lease, or post action.
_________________________________________________________________________________

_________________________________________________________________________________

time: [21:09] [01-09-26]
agent: [codex] [gpt-5] [page_source_link_intake]
worktree: [feat/page-source-link-intake-20260901] /private/tmp/content-lab-source-link-intake
type: [feature-request]: Page-scoped source-link intake
area: [backend]: Control-plane source artifacts and Clipper downloader

Added an authenticated, idempotent source-import job for one exact current
Master Pages page and sourced-video format. Content Lab reuses the Clipper URL
downloader under public-HTTPS, time, and byte ceilings, records the exact
download hash and probed media, then center-crops/scales to muted H.264/yuv420p
1080x1920 at 30fps before exposing the normalized hash-bound bytes through the
existing job/status/artifact contract. The route does not write ShipStream,
admit content, generate media, or mutate any scheduler, device, slot, lease, or
post state. Focused verification: 48 passed.
_________________________________________________________________________________

_________________________________________________________________________________

time: [21:29] [01-09-26]
agent: [codex] [gpt-5] [source_link_intake_hardening]
worktree: [feat/page-source-link-intake-20260901] /private/tmp/content-lab-source-link-intake
type: [bug report]: Public source-link intake deployment gate
area: [backend]: Content Lab source download, normalization, and artifact transport

Hardened the page-scoped source-link API before public rollout. Intake now
requires a server-owned exact/subdomain host allowlist, canonical permanent
URLs without credential query parameters, a complete commissioned format,
bounded per-page/global admission, TLS-verified owned yt-dlp process groups,
workspace/disk/duration/output ceilings, failure cleanup, and configured-origin
artifact URLs. An exact repeat after `source_import_runtime_restarted` safely
requeues the same durable job; other failures stay terminal. Normalized and
original byte/hash/media lineage remains exact and the source artifact schema
is unchanged. Verification: 114 control-plane, format-contract, and downloader
tests passed; compileall and diff checks passed. The repository-wide suite still
stops during collection because the pre-existing `tests/test_cards_no_shell.py`
cannot import `factory.formats`. No deploy, storage write, generation,
scheduler, phone, slot, lease, or post action.
_________________________________________________________________________________

_________________________________________________________________________________

time: [21:36] [01-09-26]
agent: [codex] [gpt-5] [source_link_intake_hardening]
worktree: [feat/page-source-link-intake-20260901] /private/tmp/content-lab-source-link-intake
type: [bug report]: Recursive source-import workspace accounting
area: [backend]: Content Lab source downloader resource ceiling

Changed the source-import workspace meter to count every nested regular file,
including downloader fragment directories, while ignoring symlinks. Added a
regression proving a nested fragment overflow cancels the active downloader and
removes the complete partial workspace. Focused source-import verification: 30
passed; compileall and diff checks passed. No deploy, storage write, generation,
scheduler, phone, slot, lease, or post action.
_________________________________________________________________________________

_________________________________________________________________________________

time: [21:41] [01-09-26]
agent: [codex] [gpt-5] [source_link_intake_hardening]
worktree: [fix/source-import-artifact-origin-20260901] /private/tmp/content-lab-source-link-intake
type: [bug report]: Separate Content Lab artifact and Control Plane origins
area: [backend]: Source-import artifact transport

Separated the pinned public origin used for Content Lab's tokenized job
artifacts from the existing Control Plane origin used to retrieve authoritative
page-vault media. Source imports now require `CONTENT_LAB_PUBLIC_ORIGIN`, while
`CONTENT_LAB_CONTROL_PLANE_ORIGIN` retains its original responsibility. Focused
source-import, source-execution, and downloader verification: 45 passed;
compileall and diff checks passed. No storage write, generation, scheduler,
phone, slot, lease, or post action.
_________________________________________________________________________________

_________________________________________________________________________________

time: [16:19] [02-09-26]
agent: [codex desktop] [gpt-5.6-sol]
worktree: [fix/roster-completeness-proof-20260902] /private/tmp/content-lab-roster.L7umT8
type: [bug report]: Bind roster refresh completeness to the canonical projection
area: [backend]: Master Pages roster machine contract

The refresh route now reports the exact bounded canonical projection count,
legacy short snapshot version, full SHA-256 projection hash, and a server-owned
completeness flag from the same projection returned by the roster snapshot.
Raw Notion row count remains diagnostic and no longer stands in for canonical
page count. Errors or projection overflow force completeness false; a later
consumer read must match the count, version, and full hash before any
destructive alias cleanup is eligible. The response remains credential-free.

Focused roster verification passed 15 tests. The complete control-plane,
Notion, and roster regression set passed 133 tests after the full-hash
hardening. The repository-wide suite remains
stopped at collection by the pre-existing missing `factory.formats` package in
`tests/test_cards_no_shell.py`. Independent P1/P2 review is clean. No
generation, storage, scheduler, device, phone, slot, lease, or post mutation
occurred.
_________________________________________________________________________________

_________________________________________________________________________________

time: [03:09] [09-03-26]
agent: [codex desktop] [gpt-5.6-sol]
worktree: [codex/commission-pov-scenic-source] /private/tmp/content-lab-scenic.so3ESQ
type: [bug report]: Commission the existing POV Scenic source libraries
area: [backend]: Content Lab page-bound source execution

Changed POV Scenic from an undefined blocker to the existing commissioned
source recut executor. Each page remains bound to its own Master Pages identity
and ShipStream source library, with unique 6-8 second cut windows, exact source
lineage, 9:16 output, text scan, and no cross-page or AI fallback. Added a
positive execution regression for a page-owned Scenic library and retained the
separate AI resolver boundary. Focused Content Lab verification passed 75 tests;
the expanded control-plane suite was also run before release. No storage,
scheduler, device, phone, slot, lease, or post mutation occurred.
_________________________________________________________________________________

_________________________________________________________________________________

time: [21:05] [06-09-26]
agent: [codex desktop] [gpt-5]
worktree: [fix/source-capacity-aware-capabilities-20260906]
type: [bug report]: Bound source refill plans to reservable windows
area: [backend]: Content Lab sourced-video capability contract

Sourced-video capabilities now advertise only the unique immutable-master
windows that durable queued, running, and completed jobs have not reserved.
An exhausted library disappears from capabilities, while job creation remains
exact and all-or-nothing. This prevents Control Plane from requesting a batch
larger than remaining source inventory and losing the entire refill instead of
using the safe remainder. Added focused coverage for declining capacity and
complete exhaustion. The expanded Content Lab Control Plane suite passed 88
tests. No generation, storage, scheduler, device, phone, slot, lease, or post
mutation occurred.
_________________________________________________________________________________

_________________________________________________________________________________

time: [09:21pm] [06-09-26]
agent: [codex desktop] [gpt-5]
worktree: [fix/zero-capacity-contract-20260906]
type: [bug report]: Preserve exhausted source recipe identity
area: [backend]: Content Lab capability contract

Exhausted page-bound source recipes now remain visible to Control Plane with
`maxQuantity: 0`. This preserves exact recipe identity while allowing the
deployed consumer to classify source-library exhaustion separately from a
missing recipe and direct the operator to add a page source master. Job
creation remains exact and still rejects any quantity above current reservable
capacity. No generation, storage, scheduler, device, phone, slot, lease, or
post mutation occurred.
_________________________________________________________________________________
_________________________________________________________________________________

time: [01:21P] [11-09-26]
agent: [codex desktop] [gpt-6]
worktree: [caption-stroke-fix at origin/main]
type: [bug report]: Restore caption outline thickness
area: [backend]: Prepared-post caption rendering

The final-size Pillow renderer incorrectly scaled a 4 px preview stroke to 10 px,
which merged letters and adjacent lines into heavy black blocks. Prepared-post
caption rendering now uses the established 3 px final-resolution outline, and
the caption contract test pins that value. Fifteen focused caption-render tests
passed before deployment.
_________________________________________________________________________________
_________________________________________________________________________________

time: [07:05 am] [11-09-26]
agent: [codex desktop] [gpt-6]
worktree: [fix/reuse-source-windows-after-recipe-change-20260911]
type: [bug report]
area: [backend]

Completed source-DNA jobs reserved raw page-bound cut windows across every later
recipe revision, permanently exhausting finite masters even when a newly locked
recipe required different video treatment and new provenance. Completed outputs
now reserve windows only for the exact recipe-spec hash that produced them;
queued and running jobs remain exclusive across revisions, failed jobs still
release their windows, and page/library/hash isolation remains unchanged. The
source execution, dossier, generation, and format contract suites passed 56
checks. Local Docker release verification was unavailable because the Docker
daemon was not running; the Railway remote image build remains the release gate.
_________________________________________________________________________________
_________________________________________________________________________________

time: [07:16 am] [11-09-26]
agent: [codex desktop] [gpt-6]
worktree: [main]
type: [bug report]
area: [backend]

Merged PR #108 as 830c530. Railway served the changed recipe-scoped capacity
behavior: Missed Exit's current dossier recipe advertised ten windows while its
completed older recipe remained at zero. Two authenticated control-plane refill
jobs then produced and admitted ten page-bound assets; macOS Vision accepted
eight and rejected two. The scheduler materialized all five required Missed Exit
slots for September 11, and the page disappeared from the materializer blocker
set. Dashboard Confessions and Hopecore remain capacity-exhausted under their
exact current recipes and require separate source repair.
_________________________________________________________________________________
_________________________________________________________________________________

time: [07:43 am] [11-09-26]
agent: [codex desktop] [gpt-6]
worktree: [fix/release-stale-source-job-windows-20260911]
type: [bug report]
area: [backend]

Sourced-video capability calculation now applies the existing runtime-restart
fence before counting reserved source windows. Queued or running async jobs from
a dead process become failed with their bounded restart reason, releasing only
their unrendered windows; current-runtime work and completed outputs remain
unchanged. This prevents abandoned jobs from advertising permanent zero capacity.
The source execution, dossier, generation, and recipe suites passed 65 checks.
_________________________________________________________________________________

_________________________________________________________________________________

time: [6:28pm] [09-11-26]
agent: [Codex desktop] [GPT-6]
worktree: [fix/reopen-exhausted-source-jobs-20260911]
type: [bug report]
area: [backend]

A repaired Control Plane source route could not revive 106 post-render jobs
because Content Lab retained the terminal slot/hash job at three attempts even
when the caller supplied a fresh idempotency key. An identical exhausted
source_response_rejected job now reopens only for a new authenticated key; a
replay of that recovery key only observes the current job. The focused durable
post-render suite passed 32 checks.
_________________________________________________________________________________
_________________________________________________________________________________

time: [6:37pm] [09-11-26]
agent: [Codex desktop] [GPT-6]
worktree: [fix/accept-wan-near-vertical-20260911]
type: [bug report]
area: [backend]

Wan I2V can return 704x1280 for a requested vertical clip. The prepared-post
gate treated that provider-native sizing as regeneration_required even though a
small center crop makes exact 9:16 delivery without stretching. The renderer now
accepts upright square-pixel inputs within three percent of 9:16, center-crops
only the excess edge, and still emits verified 1080x1920 output. The prepared
render and durable job suites passed 68 checks, including an actual 704x1280 MP4.
_________________________________________________________________________________

_________________________________________________________________________________
time: [11:39pm] [09-11-26]
agent: [Codex desktop] [GPT-6]
worktree: [fix/ffmpeg-source-refill-capacity-20260912]
type: [bug report]
area: [backend]

A burst of scheduler refill jobs started independent 1080x1920 libx264 processes in Content Lab and six were killed during encoder initialization. Serialized services.ffmpeg.run_color_correct through one process-local permit so asynchronous jobs wait instead of exhausting the production container. Added a concurrency regression; the focused FFmpeg suite passed 47/47.
_________________________________________________________________________________
_________________________________________________________________________________
time: [12:09am] [09-12-26]
agent: [codex desktop] [gpt-6]
worktree: [fix/source-treatment-artifact-receipts-20260912]
type: [bug report]
area: [backend]

Content Lab generated and sourced clips with the requested video treatment but omitted the producer treatment receipt from the artifact contract, so Control Plane excluded paid clips before scheduling. Wired source-treatment receipts into generated, sourced-video, slideshow, and inherited truck-recovery manifests. The artifact serializer can recover completed jobs only from persisted applied speed/crop plus the exact registered recipe hash. Focused generation, sourced-video, and receipt tests passed (56).
_________________________________________________________________________________
_________________________________________________________________________________
time: [12:11am] [09-12-26]
agent: [codex desktop] [gpt-6]
worktree: [fix/source-treatment-artifact-receipts-20260912]
type: [bug report]
area: [backend]

Correction to the preceding entry: this change wires receipts for generated, sourced-video, and slideshow outputs. It leaves truck-master recovery unchanged because that path must preserve its parent producer receipt rather than restate the current job recipe hash. Focused tests remain 56 passing.
_________________________________________________________________________________

## 2026-09-12 — Fleet visual admission

worktree: /Users/ecfromthedc/dev/wt/lab-fleet-vision

Added authenticated exact-job/page/SHA/byte-bound visual decisions, native every-frame OCR and GLM-4.6v-flash evidence before ready-clip admission. Decisions persist in job truth. Decoder/OCR/model/coverage failures remain unavailable. Docker installs Tesseract. Tests include real 1/30-second text and endpoint authentication/identity/persistence. Full deployment and live fleet acceptance remain integration-owned.

Source planning now skips original timestamps below 60 seconds and consumes the Worker's bounded global source-window exclusions before rendering. Native 1080x1920 throughput: 7s/210 frames in 228.38s with fixture model; 9s/270 frames completed OCR in 211.53s total with unavailable live model evidence. Flash API independently returned 429/provider 1305 overload. Decisions remain unavailable and paid jobs remain resumable; this is not a live-clean claim.

Final focused suite: 77 passing across visual admission, packaged imports, recipe/generation and source execution. Peer review repaired stale wrong-page cached decisions with full identity checks in the sweep and a recovery regression.

Follow-up review: capabilities expose current-recipe canonical source identities before the Worker exclusion limit. Multi-crop provider calls now require complete commissioned groups; the artifact endpoint preserves boundary groups for non-multiple quantities. Malformed candidate sets fail before artifact processing.

Expanded verification passes 105 tests including actual generated-job malformed crop-set regressions and source-identity capability checks. Candidate indices are normalized into order before whole-group transport.

## 2026-09-13 — Vision fallback transport honesty

worktree: /Users/ecfromthedc/dev/wt/lab-fleet-vision

Fixed the Lab GLM-to-Ollama vision ladder after adversarial review: httpx transport failures now trigger the fallback, and dual transport failure returns `vision_unavailable_all_providers` while naming the Ollama model that actually failed. Primary malformed or oversized responses now trigger the fallback as documented; if both legs fail, the decision remains unavailable. Added behavioral coverage for primary transport fallback, dual transport failure, and oversized-body fallback. Focused visual-admission suite: 22 passed, 1 existing warning.

## 2026-09-14 — Hosted vision fallback credential boundary

worktree: /Users/ecfromthedc/dev/wt/lab-fleet-vision

Added `CONTENT_LAB_VISION_FALLBACK_API_KEY` as an optional fallback-only credential. The primary Z.AI key is never forwarded to the fallback host; unset fallback credentials produce no Authorization header. Corrected deployment truth: production is Railway at `https://risingtides-content-lab-production.up.railway.app`, so the configured fallback endpoint must be reachable from the Railway container.

## 2026-09-14 — Configurable primary vision endpoint

worktree: /Users/ecfromthedc/dev/wt/lab-fleet-vision

Added the optional `CONTENT_LAB_VISION_URL` override, restricted to the server-owned `api.z.ai` and `open.bigmodel.cn` chat-completions URLs. The selected URL host is recorded in the decision model block. HTTP 429/provider-1305 primary responses remain unavailable and trigger the existing fallback ladder; unallowlisted endpoints fail closed. No API key values are read or logged.

## 2026-09-14 — OpenAI-compatible vision fallback

worktree: /Users/ecfromthedc/dev/wt/lab-fleet-vision

Added the allow-listed `gpt-4o-mini` fallback on OpenAI's compatible chat-completions endpoint. Fallback requests retain image data-URL parts, omit provider-specific thinking controls, use the fallback-only credential, and record `name`, `provider`, and fallback status in every decision. Added request-shape and provider attribution coverage.

## 2026-09-14 — Remove Lab posting obstacles without bypassing evidence

worktree: /Users/ecfromthedc/dev/wt/lab-fleet-vision

Capabilities now expose the commissioned format route implied by a page's current Master Pages niche/engine before its first registered Dossier publication, while registered versions remain authoritative. Transient visual-admission/provider/runtime failures now persist as retryable `scan_pending` decisions; positive text and artifact identity refusals remain unchanged. Added coverage for bootstrap capabilities and retry behavior.
_________________________________________________________________________________

time: [03:46pm] [16-09-26]
agent: [codex desktop] [gpt-6]
worktree: [fix/dossier-capacity-20260916] /private/tmp/content-lab-caption-capacity-20260916
type: [bug report]: Restore sourced-video refill after dossier revisions
area: [backend]: Content Lab capability and source-window authority

Completed sourced-video jobs now reserve windows only for the exact locked
recipe that produced them, while active jobs still reserve windows across
revisions. Sourced capabilities also return their immutable source identities
so Control Plane can calculate cross-page exclusions before job creation. The
focused source, dossier, and source-library suite passed 53 tests.
_________________________________________________________________________________
_________________________________________________________________________________

time: [04:11pm] [16-09-26]
agent: [codex desktop] [gpt-6]
worktree: [fix/source-window-master-binding-20260916] /private/tmp/content-lab-caption-capacity-20260916
type: [bug report]: Bind shared-library exclusions to one master clip
area: [backend]: Content Lab source cut planning

Source-window exclusions now accept an exact master SHA so separate clips that
share one library URL do not suppress each other's timelines. Legacy exclusions
without a master SHA stay broad. The focused source and dossier suites passed
69 tests.
_________________________________________________________________________________
_________________________________________________________________________________

time:      [05:10pm] [09-16-26]
agent:     [codex desktop] [gpt-6]
worktree:  [fix/source-import-active-deadline-20260916]
type:      [bug report]
area:      [backend]

Content Lab status now retires a page-source import that remains active beyond
its 20-minute bounded download and normalization runtime, using the existing
idempotent runtime-restart path. This prevents a dead running job from holding
the page's only dossier import slot and the two-job global capacity indefinitely.
The exact source-import suite passes 12 tests and the production-image import
suite passes 2 tests. A local Docker build could not start because Docker Desktop
was not running; the repository's packaged-app import checks passed.
_________________________________________________________________________________
_________________________________________________________________________________

time:      [05:26pm] [09-16-26]
agent:     [codex desktop] [gpt-6]
worktree:  [fix/source-import-restart-clock-20260916]
type:      [bug report]
area:      [backend]

Corrected the page-source import deadline to measure from restartedAt when an
old durable job is resumed. The first live rollout exposed that createdAt stays
immutable across a restart; using it as the active-runtime clock immediately
expired the resumed job on every status poll. The regression now proves an
expired job is retired once, restarts under the same idempotency key, and then
remains queued inside its fresh runtime window. All 12 source-import tests pass.
_________________________________________________________________________________
_________________________________________________________________________________

time:      [05:36pm] [09-16-26]
agent:     [codex desktop] [gpt-6]
worktree:  [fix/source-import-exact-copy-20260916]
type:      [bug report]
area:      [backend]

Page source intake now retains an input byte-for-byte when it already satisfies
the refillable master contract: 1080x1920 H.264/yuv420p at 30 fps with no audio.
The live Healing and Soul inputs both have exactly those facts, so re-encoding
them consumed minutes without changing their deliverable shape. Nonconforming
inputs still use the bounded normalization path and the final contract check.
The exact-copy regression plus all 12 source-import route tests pass, and both
production-image import checks pass.
_________________________________________________________________________________

_________________________________________________________________________________

time:      [05:51pm] [09-16-26]
agent:     [codex desktop] [gpt-6]
worktree:  [fix/source-import-public-no-auth-20260916]
type:      [bug report]
area:      [backend]

Page source intake now bypasses every shared platform and browser cookie store.
This lane already accepts only validated public, permanent URLs, so loading a
stale or oversized TikTok cookie jar added latency without adding access. The
regression proves source-import yt-dlp calls use verified TLS, one owned process
group, and no cookie flags. All 9 downloader diagnostics and all 12 page-source
import route tests pass. The broader local media-service suite could not run its
2.5 GB free-space preflight because this Mac had under 400 MB free.
_________________________________________________________________________________

_________________________________________________________________________________

time:      [06:06pm] [09-16-26]
agent:     [codex desktop] [gpt-6]
worktree:  [fix/accept-imported-source-authority-20260916]
type:      [bug report]
area:      [backend]

Content Lab now projects ShipStream `content_lab_page_source_import` masters as
exact page-bound source libraries when the authority repeats the active Control
Plane page id, page handle, Notion page id, and replacement eligibility. This
aligns the Dossier reader with the source-registration producer while rejecting
a different page id. The source-manifest and Dossier ingredient suites pass 36
tests.
_________________________________________________________________________________
_________________________________________________________________________________

time:      [06:27pm] [09-16-26]
agent:     [codex desktop] [gpt-6]
worktree:  [fix/accept-imported-source-authority-20260916] /private/tmp/content-lab-caption-capacity-20260916
type:      [bug report]: Restore capacity for short imported page masters
area:      [backend]: Content Lab source cut planning

Exact page-bound ShipStream masters now begin at their first immutable frame instead of receiving the raw-source 60-second skip a second time. Deterministic cut duration selection uses only durations that fit the remaining master bytes, so a valid 7.5-second imported master advertises and executes one cut instead of zero. Raw original-source libraries retain the 60-second minimum. The source execution, source library, and dossier execution suites pass 76 tests.
_________________________________________________________________________________

_________________________________________________________________________________

time:      [10:21] [09-18-26]
agent:     [codex desktop] [gpt-5]
worktree:  [fix/visual-replicate-fallback-20260918] /tmp/content-lab-live.VMR5lI/repo
type:      [bug report]: Unblock fail-closed visual admission for automatic source recuts
area:      [backend]: Content Lab visual admission and bucket replenishment

Live Control Plane and Content Lab evidence showed that automatic sourced-video
replenishment is producing new six-second outputs from the same approved page
masters with distinct output hashes. Those completed recuts remained outside
ready buckets because Z.ai was rate-limited and the configured OpenAI fallback
had exhausted quota. Visual admission now uses the already-configured Replicate
token as a third, fixed-host fallback through the official
google/gemini-2.5-flash model. It sends every sampled frame in bounded batches
of at most ten, validates strict JSON verdicts, polls only fixed Replicate
prediction URLs, and still fails closed on missing, invalid, incomplete, or
uncertain evidence. Existing two-provider error attribution is unchanged when
Replicate is not configured. Visual admission, sourced-video execution, and
production-image import verification pass 96 tests.
_________________________________________________________________________________

_________________________________________________________________________________

time:      [10:43] [09-18-26]
agent:     [codex desktop] [gpt-5]
worktree:  [fix/serialize-visual-sweeps-20260918] /tmp/content-lab-live.VMR5lI/repo
type:      [bug report]: Serialize automatic visual-admission sweeps
area:      [backend]: Content Lab admission throughput

The first live autonomous poll after the Replicate fallback deployed proved a
second blocker: Control Plane concurrently requested several completed jobs,
while Content Lab allowed only one scanner. The winning sweep ran and the rest
persisted `scan_pending` with `scanner_busy_retry`, including Soul's new
same-master six-second recut. Authenticated whole-job sweeps now enter a single
process-wide executor. They remain page and byte bound, execute one at a time,
do not duplicate while queued in the current runtime, and become eligible for
resubmission immediately after a process restart. Visual admission,
sourced-video execution, and production-image import verification pass 97
tests, including an overlap regression that proves peak scanner concurrency is
one.
_________________________________________________________________________________

_________________________________________________________________________________

time:      [10:59] [09-18-26]
agent:     [codex desktop] [gpt-5]
worktree:  [fix/replicate-verdict-contract-20260918] /tmp/content-lab-live.VMR5lI/repo
type:      [bug report]: Require complete Replicate visual verdicts
area:      [backend]: Content Lab visual admission

The first serialized live sweep reached Replicate but its response stopped in
the middle of the reason string under the old pseudo-JSON union prompt. Strict
parsing correctly refused that evidence. A live bounded probe established that
the same official model returns complete JSON when given a natural-language
two-field schema, a reason limit, and a 1024-token ceiling. The fixed prompt
retains strict verdict parsing and complete sampled-frame coverage. A transient
pending verdict now stops the rest of that job's sweep so provider trouble does
not spend calls or hold the serialized scanner ahead of other pages. Visual
admission, sourced-video execution, and production-image import verification
pass 98 tests.
_________________________________________________________________________________

_________________________________________________________________________________

time:      [11:35] [09-18-26]
agent:     [codex desktop] [gpt-5]
worktree:  [fix/fair-visual-sweep-queue-20260918] /tmp/content-lab-live.VMR5lI/repo
type:      [bug report]: Make visual-admission queue fair across pages
area:      [backend]: Content Lab admission throughput

Live inspection after serialized scanning showed fourteen active whole-job
sweeps. Ten-output jobs could hold the only scanner for minutes while a
one-output sourced recut waited behind the entire batch. A sweep executor turn
now scans one nonfinal artifact. A final decision requeues any remaining work
at the tail under the same runtime-bound sweep id; a transient pending decision
stops until the next authenticated poll. This preserves one scanner and exact
full-frame QA while giving small depleted pages a turn between large batches.
Visual admission, sourced-video execution, and production-image import
verification pass 99 tests, including one-artifact tail-requeue and peak-one
concurrency regressions.
_________________________________________________________________________________

_________________________________________________________________________________

time:      [6:56pm] [09-22-26]
agent:     [codex desktop] [gpt-5]
worktree:  [codex/dossier-syzygy-timeout] /tmp/content-lab-probe.Wlgd6c
type:      [bug report]: Restore fleet-wide Dossier loading
area:      [backend]: Content Lab Dossier ingredient catalog

Live Control Plane probes showed every tested Dossier timing out on the shared
ingredient catalog while fonts, captions, and ShipStream manifests remained
available. The catalog queried both live Syzygy slideshow libraries for every
page, including unrelated AI and sourced-video pages, and each query could hold
15 seconds behind the Control Plane's five-second request budget. The catalog
now reads Syzygy only for the page's exact Master Pages slideshow niche and
engine, and bounds that one relevant read to two seconds. Verification: 15
Dossier ingredient tests and 88 recipe, generation, source, and slideshow
execution tests passed. No production deployment or service mutation occurred.
_________________________________________________________________________________

_________________________________________________________________________________
time: [05:09 EDT] [24-09-26]
agent: [Codex desktop] [gpt-6-astra]
worktree: [codex/page-scoped-recipe-reads] [/tmp/rt-lab-registry.WCseqY]
type: [bug report] [refactor]: Shared Content Lab capability-read congestion
area: [backend] [testing]

Railway live stack sampling found all 40 AnyIO request workers inside capability reads, predominantly reopening all 402 immutable recipe publications. The last 300 upstream requests to capabilities, Dossier ingredients and format contracts timed out at about three seconds; a container-local authenticated registry read also timed out while health answered. The volume has 143 GiB free, so this is not disk exhaustion. Recipe listing now coalesces cold reads and reuses each unchanged file only after checking its device/inode/size/mtime/ctime; additions, removals, corruption and edits remain immediately visible, and selected records are copied before returning to callers. Exact page/Notion identity and ambiguous-alias checks are unchanged. All 166 targeted recipe, Dossier, generation, source, format and production-image-import tests passed. A separate read-only process on the production volume returned identical results for 16 concurrent page registry probes, improving 2.787s to 0.405s. No production source, settings, content, or posting authority was changed during this test. Root AGENTS records cache freshness/copy ownership; no child boundary changed. Separately, Replicate rejected Dallas and Rhett refills for insufficient credit; no purchase or provider substitution was made. Release and authenticated post-deploy proof follow separately.


_________________________________________________________________________________
time: [05:20 EDT] [24-09-26]
agent: [Codex desktop] [gpt-6-astra]
worktree: [codex/coalesce-capability-job-reads] [/tmp/rt-lab-registry.WCseqY]
type: [bug report] [refactor]: Capability job-history decode herd
area: [backend] [testing]

PR #150 merged as 11ddc57; branch deleted; Railway deployment 336ad350-c7cf-4a0e-9c3a-3f8600f043a5 and Docker app-import succeeded. Authenticated format registry and Healing/Chase catalogs initially returned 200 in 79/262/189ms; both live Dossiers reopened. Sustained verification caught renewed timeouts on the next capability burst, so that release is not full outage resolution. Live stack sampling found concurrent capability calls still decoding the entire 28 MB jobs store while recipe readers waited. Capability planning now shares a file-identity-checked read-only history snapshot; transaction writers still use fresh mutable loads and existing locks. All 171 focused tests pass, including immediate progress updates, same-size replacement with retained mtime, corrupt/missing history, and 120 concurrent reads decoding once. A separate read-only production-volume benchmark produced identical complete capability responses, improving 16 calls from 8.999s to 1.893s. Root AGENTS records read-only ownership; no child boundaries changed. No paid work, provider substitution, account changes or phone action performed. Follow-up live burst verification remains required.


_________________________________________________________________________________
time: [09:52am] [24-09-26]
agent: [Codex desktop] [gpt-6-astra]
worktree: [codex/silhouette-static-still-20260924] [/Users/smathdaddy-macbook/content-posting-lab-silhouette-still]
type: [feature-request] [refactor]: Replace silhouette I2V with a static generated photo
area: [backend] [research] [testing]

Replicate model research selected official Black Forest Labs FLUX.2 Pro for the
silhouette format's portrait-native photorealistic stills. The prompt family now
produces exactly two adult lovers embracing beside one complete pickup in a field,
keeps the upper 45 percent as clean caption sky, and rejects anatomy, vehicle,
count, text, and geometry failures. Content Lab retains the exact generated JPG
and byte hash, then holds it without pan, zoom, interpolation, or subject motion
in a seven-second 1080x1920 MP4 so downstream captions, sounds, QA, and posting
remain unchanged. The WAN I2V provider, motion prompt, and anchor pool are removed
from this format. The focused provider, contract, Dossier, and ffmpeg suites pass
86 tests. A broader run reached 1,535 passes and 23 skips before the machine filled
its temporary disk; its two genuine failures are pre-existing Agentic Broadcast
Network card-background promotion tests, unrelated to the files changed here.
No production deployment or paid Replicate generation was performed; no local
Replicate credential is configured.
_________________________________________________________________________________
_________________________________________________________________________________
time: [17:26 EDT] [24-09-26]
agent: [Codex desktop] [gpt-6-astra]
worktree: [feat/source-start-floor-20260924] [/Users/ecfromthedc/dev/wt/source-master-loop]
type: [feature]: Durable per-page earliest source timestamp
area: [backend] [source recut planning]

Sourced-video recipes may now carry an optional `sourceStartMs` scalar in the
existing production-controls map. The recut planner combines that page choice
with the established raw-library/page-master minimum, so every refill skips the
same unusable lead-in while absent controls preserve current behavior. The
control is bounded to the supported two-hour master duration and invalid values
make the recipe unexecutable. This deliberately leaves the shared executor
catalog bytes unchanged, avoiding a fleet-wide catalog-version invalidation.
The focused source-execution suite passes: 46 tests.

_________________________________________________________________________________
time: [08:12 EDT] [25-09-26]
agent: [Claude Code] [claude-opus-5-5]
worktree: [fix/ai-gen-transient-retry] [/Users/ecfromthedc/dev/wt/lab-gen-retry]
type: [bug report]: AI refill `provider_generation_failed` classified; transient Replicate faults retried
area: [backend] [providers] [testing]

All 70 `provider_generation_failed` AI refills since 2026-09-20 in Control Plane
D1 were joined to Railway provider logs (69 classified, 1 log expired). 52 were
Replicate account credit: 43 HTTP 402 insufficient credit plus 9 HTTP 429
throttles that Replicate applies while credit is under $5. The throttles are now
retried, but credit exhaustion itself needs an operator top-up/auto-reload. The other 17 were transient:
6 submission 5xx, 6 600-second prediction timeouts, 3 transport faults (two on
the poll of an already-created prediction), 1 "interrupted (code: PA)", and 1
provider-side moderation 401. There was no retry anywhere in the generation
path. Replicate generation now retries only faults that cannot create a second
paid prediction, cancels timed-out predictions, and persists a closed
`providerFailure` class in the job store without changing the status response
the Control Plane validates strictly. New tests fail on main and pass here. No
paid generation, purchase, provider substitution or deploy was performed.

_________________________________________________________________________________
time: [15:52 EDT] [26-09-25]
agent: [Claude Code] [claude-opus-5-5]
worktree: [fix/recipe-reregister-identical-bytes] [/Users/ecfromthedc/dev/wt/lab-recipe-reregister]
type: [bug report]: identical-bytes recipe re-registration no longer 409s
area: [backend] [control-plane] [testing]

A content-neutral Dossier relock of page rhett re-sent the exact recipe bytes
already registered for its tuple under a new dossier revision and idempotency
key. `register_recipe` compared the whole stored record, so those two fields
alone produced `409 recipe tuple is already registered with different bytes`
on every retry, and the Worker could only recover by forcing a new recipe
version. Byte-identical re-registration now succeeds, advances the stored
revision/key atomically and keeps the superseded pairs; replays of any known
pair are answered without a rewrite; different bytes still 409. Validation,
including exact pinned-legacy catalog binding, runs before the store as
before. The recipe test file goes from 3 failed / 18 passed on main to 21
passed; the six recipe, generation and execution test files pass: 151 tests.
No deploy was performed.

_________________________________________________________________________________
time: [18:46 EDT] [26-09-25]
agent: [Claude Code] [claude-opus-5-5]
worktree: [fix/frontend-no-default-password] [/Users/ecfromthedc/dev/wt/lab-fe-default-pw]
type: [security]: default account password removed from the bundle; intake fails closed
area: [frontend] [backend] [security] [testing]

The Pipeline intake modal printed the shared default TikTok account password
as literal text in three places, so Vite compiled it into the public JS bundle
(3 occurrences in the live production bundle). It has been in the frontend
since the Pipeline tab landed (#38, 2026-04-29). The backend copy moved to the
`DEFAULT_INTAKE_PASSWORD` env var on 2026-06-19 but kept a guessable hard-coded
fallback, and that variable is unset in production, so intakes since then
wrote the fallback to Notion while the UI still named the old literal. The
modal now refers to "the team's standard intake password"; `/mint-alias` and
`/intake` read the env var per request and return 503
`intake_password_not_configured` before minting an alias or writing Notion or
roster state when it is unset or blank. The fallback literal is gone and
`.env.example` documents the variable. `tests/test_no_shipped_default_password.py`
stores only SHA-256 digests of both retired values and fails if either appears
in backend sources, other repository text or a built `frontend/dist`. Both
values remain in git history (including the messages of 0649233 and 4e03918)
and previously served bundles; the old literal must be rotated by the
operator, and history was not rewritten. Until the operator sets
`DEFAULT_INTAKE_PASSWORD`, intake is refused. No deploy was performed.

_________________________________________________________________________________
time: [15:25 EDT] [25-09-26]
agent: [Claude Code] [claude-opus-5-5]
worktree: [fix/replenish-unique-cuts] [/Users/ecfromthedc/dev/wt/lab-unique-cuts]
type: [bug report]: sourced replenish re-cut the same time frames after every recipe revision
area: [backend] [testing]

Sourced-video cut planning walked one fixed 9-second grid (+3 s/+6 s phases,
then a 6-second grid) from the page floor, with each slot's length a pure hash
of library and master. Completed cuts reserved their positions only for the
exact recipe version that made them, so every dossier republish re-cut the same
time frames from the first slot; and a grid id (`sha:start`) and a 6-second id
(`sha:start:6000`) could name the same time frame, so one version could cut it
twice. Control Plane D1 (read-only, 2026-09-25) holds page masters whose
identical original windows were cut 3-5 times, e.g. ourbriefhourstogether
0-6 s four times across three recipe versions (twice within one) and 18-26 s
four times across four. The planner now enumerates every whole-second start at every allowed
length, excludes every time frame already cut from the same master under any
recipe or library version, prefers the least-overlapping fresh footage, and
orders ties by a per-job seed recorded as `cutPlanSeed`. Exhaustion answers 409
`master_windows_exhausted`. Output-SHA duplicate checks in Control Plane are
unchanged. Separately, the `duplicates` counts on Worker source_replenish rows
are mostly the Worker's own multi-pass continuation re-counting clips the same
Lab job admitted in an earlier pass (admitted + duplicates equals that job's
asset count on every row checked); that accounting is Worker-side and not

_________________________________________________________________________________
time: [18:00 EDT] [26-09-25]
agent: [Claude Code] [claude-opus-5-5]
worktree: [fix/roster-routes-auth] [/Users/ecfromthedc/dev/wt/lab-roster-auth]
type: [security]: /api/roster no longer serialises account credentials
area: [backend] [security] [testing]

Production does not set APP_API_KEY, so the /api key middleware is off and
every /api/roster route answers the public internet. GET /api/roster/, GET
/api/roster/project/{name}, PUT /api/roster/{id} and POST
/api/roster/sync-notion and /sync returned full roster rows, including the
Notion signup email and password, forwarding address and Cloudflare email
alias/rule/destination. Every roster row now leaves through an allowlist
(`services/roster_public.py`) and every roster router JSON body through a
credential-key scrub; the credentials stay in the roster cache for the
server-side code that uses them. DELETE /api/roster/{id}, which no UI calls,
now requires CONTROL_PLANE_TOKEN or APP_API_KEY and fails closed when neither
is configured. The operator UI sends no credential, so the routes it calls
stay unauthenticated; their responses are credential-free. The UI's email
alias, signup and forward columns now read "hidden pending operator auth"
(the Create/Mint alias buttons are hidden with them, since the alias state is
unknown), and alias removal reports that it is disabled pending operator auth
instead of silently doing nothing. POST /dedup and PUT /{id} remain anonymous
pending operator auth. The key scrub normalises spellings and also removes
notes, pwd/pw/pass, passcode, recovery/backup codes, email and login keys. New HTTP tests fail on main (12 of 16) and pass here.
/api/pipeline and /api/email still serialise full roster rows and are not
changed here. No deploy was performed.

_________________________________________________________________________________
time: [19:45 EDT] [26-09-25]
agent: [Claude Code] [claude-opus-5-5]
worktree: [fix/telegram-auth] [/Users/ecfromthedc/dev/seats/LAB-TELEGRAM-AUTH]
type: [security]: /api/telegram behind the API key; /send confined to media
area: [backend] [frontend] [security] [testing]

`app.py` exempted every `/api/telegram/` route from the API-key middleware and
the Telegram router has no auth of its own, so with APP_API_KEY set anyone
could still replace or delete the bot token, repoint the staging group and
`POST /api/telegram/send` any file under the repo root to that chat. The
Railway volume is mounted at /app/projects, so that included the roster
(passwords), cookies.txt, control_plane_jobs.json and telegram_config.json.
The bot long-polls; no webhook route exists, so the prefix is no longer
exempt (only /api/health and the initData-verified /api/miniapp/ remain).
`/send` now accepts only a media file whose real path is inside a project
media dir or the legacy output dirs; `/send-batch` and `/assign-batch` refuse
a traversing batch id and skip files that resolve outside the batch. The
video router's `project` parameter is one safe directory name, and its
string-prefix containment checks (which let `videos-evil/` pass as inside
`videos/`) are real-path checks. The Distribution and Slideshow Telegram calls
now send the key through `withApiKey`. Earlier reviews report APP_API_KEY
unset in production; while it is, the middleware stays inert there, and the
`/send` confinement is what applies. Campaign Hub's sound-assignment proxies call
/api/telegram without a key and will need one if APP_API_KEY is set. New tests
fail on main (150 of 163) and pass here. No deploy was performed.
Follow-up in the same branch: `/api/burn/overlay` checked its source with a
string prefix, so project `p` (a prefix of `projects/page_roster.json`) or `c`
(`cookies.txt`) queued a copy of that file into
`projects/<p>/burned/<batch>/burned_000.mp4`, which /send and the /projects
mount then serve. It now uses the shared real-path `services.fsutil.is_within`
and takes `batchId` as one directory name. A sweep of routers/ and services/
found no other string-prefix filesystem containment. New burn tests fail on
main (10 of 13) and pass here.
