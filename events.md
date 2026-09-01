_________________________________________________________________________________

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
