# Content Lab Devlog

## Purpose

- Owns the Content Lab generation, source clipping, caption-bank, visual render,
  and asset preparation services used before distribution.

## Ownership

- `services/caption_render.py` owns the typed Dossier-to-render caption contract.
- `routers/burn.py` exposes caption rendering and final video compositing.
- `burn_server.py` exposes the same typed caption-render route on the posting
  Mac's canonical port-8002 Burn runtime for Rail consumption.
- `events.md` is the repository's append-only chronological ledger.

## Local Contracts

- Caption rendering accepts the existing Rust `CaptionStyle` wire fields only.
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

## Work Guidance

- Reuse the current TikTokSans fonts and production Burn geometry. Do not add a
  parallel caption-style vocabulary or silently substitute a font.
- Keep browser preview, backend render, and Rail consumption on one versioned
  contract; unknown fields and clipped output are errors, not fallbacks.
- The hosted API and the port-8002 Burn runtime must call the same renderer
  module and return the same versioned schema and hashes.

## Verification

- Run `pytest -q tests/test_caption_render_contract.py` for the typed caption
  contract and `pytest -q tests/test_burn_and_captions_api.py` for Burn API
  regressions. Run `pytest -q tests/test_burn_quality_gate.py` for legacy and
  typed overlay placement gates.

## Child devlog Index
