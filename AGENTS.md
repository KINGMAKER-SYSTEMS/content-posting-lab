# Content Lab Devlog

## Purpose

- Owns the Content Lab generation, source clipping, caption-bank, visual render,
  and asset preparation services used before distribution.

## Ownership

- `services/caption_render.py` owns the typed Dossier-to-render caption contract.
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
  typed overlay placement gates. Run the control-plane recipe, generation, and
  source-execution test files together when changing a Dossier recipe schema.

## Child devlog Index
