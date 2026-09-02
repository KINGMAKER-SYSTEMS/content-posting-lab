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
