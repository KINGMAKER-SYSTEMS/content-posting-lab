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
