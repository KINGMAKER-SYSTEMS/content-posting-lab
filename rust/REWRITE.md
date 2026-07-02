# The 100% Rewrite Plan

> Written 2026-07-01. The spine under this directory (recovered from the
> 2026-05-29..31 session) was a *strangler* slice — prove the hard parts,
> leave Python live. This document is the other strategy: rewrite the whole
> lab so the Rust binary **fully replaces** the Python app at cutover, no
> hybrid period, no piecemeal endpoint-by-endpoint migration.

## What "100%" means (and doesn't)

100% = on cutover day, `python app.py` is retired and one Rust binary serves
everything the frontend and posters actually use. It does **not** mean
porting every line of Python:

- **Cut, not ported** (decided in the 2026-05-29 recon, still right):
  slideshow (drags in librosa beat-detection, output was never wired into
  distribution), direct TikTok/IG upload (ToS-risky, humans post from
  Telegram), Postiz remnants (already removed server-side), the legacy
  3-server files, Tesseract OCR path (already dead).
- **Out of scope — separate system**: the ABN episode factory
  (`/api/agenticnews`, `/api/pipeline`, `abn_factory`, `services/openshot_bridge.py`,
  yt-pipeline). It rides in this repo and this frontend, but it is a
  different product with a hard Python dependency (libopenshot bindings)
  and its own SOP. The rewrite treats it as an **external service**: the
  Rust app reverse-proxies `/api/agenticnews/*` and `/api/pipeline/*` to a
  slim Python process. Deleting or porting it is a separate decision.
- **Python sidecars kept** (verified no Rust equivalent): pyrogram MTProto
  scanner (forum-topic listing + read-only history backfill), Playwright+
  stealth TikTok popular-sort scraping fallback. Both become tiny stdio/HTTP
  CLIs the Rust app shells out to — same pattern as ffmpeg.

## Target surface (from the frontend, measured 2026-07-01)

Real call counts per prefix: telegram 33, burn 22, video 19, clipper 13,
projects 11, roster 10, captions 6, recreate 5, email 5, miniapp 4,
health 3. (agenticnews 21 + pipeline 9 + slideshow 19 + upload 5 are
proxied or cut per above.) Step one of execution is freezing this as a
contract: export the FastAPI `/openapi.json`, delete the endpoints nothing
calls (e.g. the dead `/burn` route), and that spec is the parity target.

## Architecture

Cargo workspace, extending what's here:

| Crate | Owns |
|---|---|
| `clab_core` | domain model, SQLite (sqlx 0.8, two-pool WAL), migrations, repo layer — **all** state, not just assets/jobs |
| `media` | ffmpeg/ffprobe/yt-dlp shell-outs, crop math, color correction |
| `providers` | video-gen Provider trait + xAI/Replicate adapters (move out of `api`) |
| `integrations` | one shared reqwest client: Notion, Campaign Hub, Cloudflare (email), OpenAI (vision OCR + fuzzy match), R2 (aws-sdk-s3), Drive (service account, yup-oauth2) |
| `bot` | teloxide: send/forward/topics/polling, schedule loop, sidecar bridge to pyrogram |
| `api` | Axum: routes, SSE/WebSocket, miniapp initData HMAC auth, SPA + static serving (tower-http), reverse proxy to ABN |
| `sidecars/` (not a crate) | pyrogram scanner, playwright scraper — pinned venv, spawned as child processes |

`crates/web` (the Trunk/Leptos experiment) gets deleted — the decision was
keep React, and the React rework is part of this plan.

## The real 100% move: one database

The piecemeal slice put assets/jobs in SQLite but left
`telegram_config.json` / `page_roster.json` / `content_requests.json` /
`email_rules.json` as live Python state. The full rewrite migrates **all**
of it: posters, page assignments, topics, sounds, schedule, inventory,
user bindings, roster, content requests, email rules — proper tables with
foreign keys (poster→pages→topics→inventory is a real relational shape
that JSON has been faking).

`clab migrate` subcommand: reads the JSON files off the Railway volume,
imports idempotently, prints a diff report. Run it at cutover; JSON files
stay on disk untouched as the rollback snapshot.

## Frontend: fix the flow in the same stroke

Keep React/Vite/Tailwind, but the rewrite is the moment to fix what the
recon flagged, because the new API makes the fixes natural:

- Generate→clip→burn handoff moves to **asset IDs in the URL** (assets are
  durable rows now) — kills the one-shot Zustand fields that evaporate on
  refresh, kills the folder-guessing and silent selection-widening.
- The dead `/burn` route gets a real mounted page.
- `Distribution.tsx` (1123-line god component, ~40 useState) splits along
  the new API seams: roster / posters / sounds / inventory.

## Parity + cutover

1. **Contract tests**: golden request/response tests generated from the
   frozen OpenAPI spec, run against both apps in CI. Divergence = bug or a
   deliberately documented improvement.
2. **Shadow run**: deploy the Rust binary to Railway next to Python, same
   volume (read-only DB for the shadow), mirror real traffic for a few
   days, diff.
3. **Cutover**: drain in-flight Python jobs (they're in-memory anyway),
   run `clab migrate`, flip the domain to the Rust service. Python image
   stays tagged on Railway for one-command rollback.

## Execution waves (this is the "wider")

Once the schema + error types + DTOs are locked (wave 0), the modules are
independent — each wave fans out to parallel sessions with disjoint file
ownership:

- **Wave 0** — full SQLite schema, `clab migrate`, DTO crate, frozen
  OpenAPI contract, endpoint cut-list. *(serial, everything depends on it)*
- **Wave 1** *(parallel)* — captions scraping (yt-dlp + GPT vision +
  playwright sidecar) · sound library sync (Hub + Notion + fuzzy match) ·
  telegram full surface (topics setup, schedule, inventory, bindings) ·
  integrations crate (email, drive, R2) · miniapp auth + routes.
- **Wave 2** *(parallel)* — SPA/static serving + ABN reverse proxy ·
  recreate (thin composition over captions+generate) · debug/health
  (tracing + log tail endpoint) · contract-test harness.
- **Wave 3** — frontend rework (asset-id flow, Distribution split, dead
  routes) against the running Rust API.
- **Wave 4** — shadow run, parity fixes, migrate, cutover.

Scale reference: the recovered spine (~40% of the live surface, including
all the hardest parts — job engine, ffmpeg, telegram send-path, providers)
took one long session. Waves 1–2 are wider but shallower; wave 3 is the
biggest unknown because it's product work, not porting.

## Standing risk note

The 2026-05-29 adversarial verifier flagged the strategic risk honestly:
a full rewrite of a working deployed app trades a system you own for new
footguns, and the headline win (real DB) was achievable in Python. The
mitigations are baked in above — frozen contract, shadow parity run,
one-command rollback, and the Python image kept warm until the Rust app
has survived real traffic.
