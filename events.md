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
