# Content Lab — Rust rewrite

A lean rewrite of the content-posting-lab, rebuilding the core loop the right
way instead of porting the accumulated mess. Lives under `rust/` alongside the
legacy Python app, which stays the deployed system until this spine is fully
fleshed out.

## Why a rewrite

Two pains drove it:

1. **Distribution was a mess.** ~45 Telegram endpoints, half of them recovery
   band-aids (scan-inventory, discover-topics, dedup) that existed only to
   repair state the brittle "forward every message-id in a range + advance one
   shared watermark" logic kept corrupting — including silently losing content
   on any transient forward failure. That's why the bot was kept stopped in prod.
2. **generate → burn was unintuitive.** Media handed off through one-shot
   browser state that evaporated on refresh; Burn *guessed* which files to use by
   folder (silently widening a 3-of-12 selection to all 12); and every "Use in
   Burn" button pointed at a `/burn` route the app never even mounted.

Both are information-architecture / data-model problems. The rewrite fixes them
at the root rather than reskinning them.

## Architecture

```
rust/
├── crates/
│   ├── core/     domain model + SQLite data layer (clab_core)
│   ├── media/    ffmpeg / ffprobe orchestration (shell-out, no C bindings)
│   └── api/      Axum binary: HTTP + SSE (+ will serve the React SPA)
```

- **Axum 0.8 + SQLite (sqlx 0.8)**. SQLite replaces the hand-rolled JSON-blob
  config that was rewritten in full on every mutation with no locking. Uses a
  **two-pool** setup (1-conn WAL writer + read-only reader pool) — the
  community fix for sqlx single-writer lock starvation.
- **ffmpeg by shell-out** via `tokio::process` with `-progress pipe:1` parsing
  (no libav bindings — the pragmatic, deploy-friendly choice).
- **teloxide 0.17** for Telegram. Append-only sends; per-row inventory.

### The asset model (fixes the flow)

Every piece of media is a durable row with a stable id and lineage:

```text
generated --clip--> clip --burn--> burned     (parent_id chains them)
```

Burn consumes explicit **asset ids**, not folder paths — so the handoff is
reload-safe and can't silently widen a selection.

### Per-row inventory (fixes distribution)

Each delivered Telegram message is one `telegram_inventory` row. Forwarding
walks pending rows and marks each individually — never a message-id range, never
a shared watermark. A failed forward simply leaves that row pending for retry.
The data-loss bug is impossible by construction.

## Status

Full content loop works end-to-end (prompt → generated video → 9:16 clips →
burned → distributed), all durable assets with lineage. Spine is hardened
(adversarial-reviewed, 11 bugs fixed) and the generation front door is in
(reviewed, 5 bugs fixed). 26 tests, clippy clean.

- [x] Workspace + two-pool SQLite + migrations
- [x] Projects + durable Asset model + Jobs (SQLite, survive restarts; orphan
      recovery on boot)
- [x] **Generate** (provider trait + xAI/Replicate adapters; submit→poll→download)
- [x] Clip pipeline (long video → 9:16 shorts), SSE progress
- [x] Burn pipeline (caption PNG overlay + color correction), full lineage
- [x] Telegram distribution: roster/posters CRUD, append-only send, atomic
      per-row claim+forward (concurrency-safe, no double-send/loss)
- [x] Hardening: path-injection closed, body-size limits, download caps,
      load-bearing claims test-backed (200-way concurrent write, failed-forward
      stays pending, claim non-overlap)

Cut as feature creep (confirmed against the live code): slideshow + recreate
(removes the librosa porting problem), direct TikTok/IG upload (ToS-risky, off
the live path), Postiz (already removed upstream).

### Next

- TikTok caption scraping (shell out to yt-dlp + GPT-4.1 vision OCR over reqwest)
- Source upload endpoint (so clip sources land under data/ rather than manual copy)
- Forum-topic setup endpoint (teloxide create_topic → store topic rows)
- Sound library sync (Campaign Hub + Notion + GPT-4.1-mini fuzzy match)
- Serve the React SPA from Axum; rework the frontend flow to match the new
  asset-id-based handoff
- Optional Python sidecar for the TikTok stealth-scrape fallback only

## Run

```bash
cargo run -p api            # serves on :8000 (PORT to override)
# DB: ./data/content_lab.db locally, RAILWAY_VOLUME_MOUNT_PATH in prod
# TELEGRAM_BOT_TOKEN enables distribution (absent → those endpoints 400)
cargo test                  # unit tests
cargo clippy                # lints (clean)
```

System deps on PATH: `ffmpeg`, `yt-dlp`.
