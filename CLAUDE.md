# Content Posting Lab

> **Last updated:** 2026-04-27
> **Status:** Deployed to Railway. Active development on Telegram distribution and Sound Assignments.

## ⚠️ ABN VIDEO PIPELINE — READ BEFORE ANY EPISODE / COMPILE / RENDER WORK

An ABN (Agentic Builder News) episode is **NOT one renderer's output.** It is
**heterogeneous asset layers composited together.** There are several *source
types*, and **OpenShot is the compiler** that stacks them. None of the sources is
"the renderer," none is legacy, none is slop to delete.

| Source type | Produces | Tool |
|---|---|---|
| **Remotion** | terminal / code-graphics shots | `yt-pipeline/remotion/` |
| **Webscroll** | real browser footage of source pages (Playwright; scroll today, → authentic mouse interaction) | `tools/gh-capture/capture_nav.cjs` |
| **CSS animation** | rasterized seekable-HTML kinetic type/stat/quote cards | `tools/seekable-html-video/render_seekable.cjs` |
| **B-roll / still** | ambient plates, screenshots, title cards | `broll_library/`, capture |
| **VO** | narration — **Pocket-TTS built-in English voice ONLY** (no clone, no cloud TTS) | `abn_factory._voice` |
| **Music bed** | ducked-under-VO track | `branding/` |
| **➜ OpenShot** | **THE COMPILER — composites all of the above into the final mp4** | `services/openshot_bridge.py` |

**Hard rules — do not break these:**
1. **Remotion is a live layer source, not "the old pipeline."** It was demoted from
   compositor to one input. Do **not** rip it out. (This mistake has rebuilt
   episodes 3× — don't repeat it.)
2. **OpenShot is the sanctioned compiler.** The closed asset vocabulary lives in
   code: `services/openshot_bridge.py` → `SOURCE_TYPES`. Tag clips with `source`
   (`remotion`/`webscroll`/`css`/…). Final episodes flow through the bridge /
   `services/editor_render.choose_renderer()` (OpenShot, ffmpeg-layered fallback).
   **Do not invent a fourth ad-hoc ffmpeg build script** — extend the existing path.
3. **VO is Pocket-TTS built-in voice only.** Guarded by `tests/test_abn_factory_voice.py`.

Full SOP, asset locations, interim `build_epN.py` scripts, and known hazards:
**`docs/PIPELINE.md`** — read it before touching the pipeline.

## Team & Repo Routing

**This repo is the canonical home for the `Content-hub` Linear team.** Linear issue prefix: `CON-NNN`.

Team is the routing primitive — not project. Workspace-wide map:

| Linear team | Canonical GitHub repo |
|---|---|
| Campaign Hub | `Risingtides-dev/risingtides-campaign-hub` |
| Sales-Agents | `Risingtides-dev/sales-agent` |
| Ocean-OS    | `Risingtides-dev/ocean-os` |
| Content-hub | `KINGMAKER-SYSTEMS/content-posting-lab` *(this repo)* |

**Rules for agents working on `CON-NNN` issues:**

- Use **only** `KINGMAKER-SYSTEMS/content-posting-lab` for implementation, branches, commits, PRs, and code investigation.
- If a Linear issue mentions or links to a different repo, **flag it as misrouted** and state which repo it should belong to — do not start implementation.
- Before coding, inspect the issue title, description, parent/related issues, existing GitHub links, and recent PRs to confirm repo fit. If still ambiguous, **stop and ask** instead of guessing.
- Post all implementation updates back to the Linear issue: branch name, PR link, merge status, and any required follow-up steps.
- Never open PRs in `risingtides-campaign-hub`, `sales-agent`, or `ocean-os` for Content-hub work.

**Hard rule:** do not guess the repository. Do not silently switch repositories. If cross-repo work is genuinely required, state that explicitly on the Linear issue before proceeding.

## What This Is

Internal tooling for Rising Tides — a social media marketing agency running TikTok/Instagram UGC influencer campaigns. The lab covers the full content production + distribution loop:

1. **Generate** AI videos
2. **Scrape** TikTok captions for inspiration
3. **Clip** long-form videos into 9:16 shorts
4. **Burn** captions onto videos
5. **Distribute** via Telegram (to internal posters) and Postiz (publishing)
6. **Manage** the page roster, posters, and sound library

It is **not** a single-user local tool — despite the original positioning. It is **deployed**, **multi-user**, and stores persistent data on a Railway volume.

## Live Deployment

| Component | URL |
|---|---|
| Backend + frontend (single FastAPI process) | https://risingtides-content-lab-production.up.railway.app |
| Railway volume (persistent state) | `RAILWAY_VOLUME_MOUNT_PATH` (typically `/app/projects`) |

Local dev: `python app.py` on port 8000. Frontend dev mode: `cd frontend && npm run dev` on port 5173 with Vite proxy.

## Architecture

```
python app.py (port 8000)
└── FastAPI (single process)
    ├── Lifespan: starts Telegram bot if token configured
    ├── Routers (all under /api/*):
    │   ├── /api/video/*       → AI video generation
    │   ├── /api/captions/*    → TikTok caption scraping (WebSocket)
    │   ├── /api/clipper/*     → 9:16 video clipping (SSE + WebSocket)
    │   ├── /api/burn/*        → Caption burning (WebSocket)
    │   ├── /api/projects/*    → Project CRUD
    │   ├── /api/recreate/*    → Recreate workflow (WebSocket)
    │   ├── /api/postiz/*      → Postiz publishing integration
    │   ├── /api/roster/*      → Page roster (Postiz integrations)
    │   ├── /api/slideshow/*   → Slideshow renderer
    │   ├── /api/telegram/*    → Telegram bot, posters, sounds, distribution
    │   ├── /api/email/*       → Email routing rules (Cloudflare)
    │   ├── /api/upload/*      → TikTok/IG direct upload jobs
    │   ├── /api/drive/*       → Google Drive integration
    │   └── /api/debug/*       → Logs and diagnostics
    ├── Static mounts: /fonts, /projects, /output, /caption-output, /burn-output
    └── SPA fallback: serves frontend/dist/index.html

frontend (React 19 + Vite + Tailwind v4 + shadcn/ui)
└── Pages: Home, Generate, Captions, CaptionsStage, Clipper, Burn,
          Recreate, Slideshow, Create, Distribution
```

## Persistent State (Railway Volume)

All runtime config and data lives on the Railway volume, mounted at `RAILWAY_VOLUME_MOUNT_PATH`. Falls back to repo root locally.

| File | Purpose |
|---|---|
| `telegram_config.json` | Bot token, staging group, posters, page assignments, sounds, schedule, inventory |
| `page_roster.json` | Postiz integrations + project + Drive folder mappings (Master Pages cache) |
| `content_requests.json` | Mini App content-request queue (poster → agent) |
| `email_rules.json` | Email routing rules |
| `projects/` | Per-project videos, clips, captions, burns |

These files are the source of truth at runtime. Railway env vars override stored values where applicable (e.g., `TELEGRAM_BOT_TOKEN` env beats stored token).

## Source of Truth Boundaries

| System | Owns |
|---|---|
| Postiz | Social platform integrations (TikTok/IG accounts) |
| Page roster (`page_roster.json`) | Maps Postiz integration IDs → projects → Drive folders |
| Campaign Hub (separate service) | Active campaign list, financial data, creator roster |
| Notion CRM | Client relationships, TikTok Sound Links per campaign |
| Telegram config (`telegram_config.json`) | Posters, staging group, sounds pool, inventory, schedule |

The lab does **not** own active-campaign status — Campaign Hub does. The lab pulls active campaigns from Hub when syncing the sound library.

## Telegram Distribution System

The Telegram pipeline is a major feature. Three group types:

1. **Staging group** (one shared group with topics-per-page) — content arrives here first
2. **Poster groups** (one supergroup per poster, with topics-per-page they own) — content forwards here
3. **Campaign Sounds topic** (per-poster) — daily sound link assignments

### Posters (8 currently)

Each poster is a person who manages a set of pages. They have:
- A dedicated Telegram supergroup (forum-enabled)
- A "Campaign Sounds" topic in their group for sound assignments
- Topics per page they own (mirroring staging group structure)
- A `page_ids[]` list — which pages they run (1 page → 1 poster)

Default posters seeded on first boot: Seffra, Gigi, Johnny Balik, Sam Hudgen, Jake Balik, Eric Cromartie, John Smathers. Seeno was added manually.

### Sound library

A pool of TikTok sound URLs synced from:
- **Campaign Hub** → which campaigns are active (`completion_status != "completed"`)
- **Notion CRM** → the TikTok Sound Link for each campaign
- **AI fuzzy matching** (GPT-4.1-mini) → bridges name spelling differences between systems

Sync endpoint: `POST /api/telegram/sounds/sync`. Returns matched/unmatched/AI counts and surfaces campaigns whose Notion entry can't be found.

### Bot lifecycle

- **aiogram** — primary bot (send, receive, forum topic management)
- **pyrogram** (optional) — used for topic discovery (lists all forum topics in a group)
- Bot token: `TELEGRAM_BOT_TOKEN` env var > `telegram_config.json` stored value
- Auto-starts in `app.py` lifespan if token present

## Telegram Mini App (per-poster client)

A lightweight client posters open from Telegram — served standalone by the SPA
under any `/m*` path (no admin shell). It re-federates content delivery from
**per-page** to **per-poster**: a poster sees the union of content across all
the pages they run, and can request more from the attached agent.

- **Which pages a poster runs** comes from the **Notion Master Pages** roster
  (`page_roster.json`, synced from `NOTION_PAGES_DB`): each page's `poster_name`
  is resolved to a registered poster via `services/poster_router.py`. Pages on
  the poster's `page_ids` are unioned in as a manual override.
- **Content** = rendered videos from each page's `project` folder, served via
  the `/projects` static mount. Aggregation lives in `services/poster_content.py`
  (read-only; it does **not** send anything — existing per-page forwarding is
  untouched).
- **Auth**: Telegram `initData` is HMAC-validated against the bot token
  (`services/miniapp_auth.py`), then the Telegram user is resolved to a poster
  via `telegram_user_ids` / `telegram_username` bindings on the poster record.
  Bind a user with `POST /api/telegram/posters/{poster_id}/users`.
- **Content requests** live in `content_requests.json` (`services/content_requests.py`).
  Posters file them via `POST /api/miniapp/requests`; the external agent reads
  the queue via `GET /api/miniapp/agent/requests` and marks them
  `in_progress`/`fulfilled` via `PATCH /api/miniapp/agent/requests/{id}`
  (gated by `X-Agent-Key` when `MINIAPP_AGENT_KEY` is set).
- Local dev without Telegram: run with `MINIAPP_DEV_AUTH=1` and open
  `/m?dev=<poster_id>` (sends an `X-Dev-Poster-Id` header).

## Backend File Map

### Routers (`routers/`)

| File | Lines | Endpoints | Purpose |
|---|---|---|---|
| `video.py` | 963 | 6 | AI video generation (9 providers) |
| `captions.py` | 397 | 2 | TikTok caption scraping (WebSocket) |
| `clipper.py` | 1592 | 10 | 9:16 video clipping (SSE + WebSocket) |
| `burn.py` | 1030 | 9 | Caption burning (WebSocket) |
| `projects.py` | 252 | 5 | Project CRUD |
| `recreate.py` | 416 | 4 | Recreate workflow (caption→prompt→video) |
| `postiz.py` | 211 | 5 | Postiz integration: status, integrations, videos, upload, posts |
| `roster.py` | 351 | 7 | Page roster: list, project filter, set, delete, dedup, sync |
| `slideshow.py` | 992 | 23 | Slideshow renderer (images, audio, formats) |
| `telegram.py` | 1541 | 30+ | Bot, staging, posters, sounds, schedule, inventory, user binding |
| `miniapp.py` | ~190 | 6 | Telegram Mini App: per-poster content + content-request intake (initData auth) |
| `email_routing.py` | 203 | 8 | Cloudflare email routing rules |
| `upload.py` | 180 | 8 | TikTok/IG direct uploads |
| `gdrive.py` | 161 | 7 | Google Drive folder ops |
| `debug.py` | 192 | 7 | Logs, errors, health, diagnostics |

### Services (`services/`)

| File | Purpose |
|---|---|
| `telegram.py` | `telegram_config.json` data access (posters, sounds, staging, schedule, user bindings) |
| `roster.py` | `page_roster.json` data access |
| `poster_content.py` | Per-poster content federation: resolve a poster → their Master-Pages → rendered videos |
| `content_requests.py` | `content_requests.json` data access — Mini App content-request intake queue |
| `miniapp_auth.py` | Telegram Mini App `initData` HMAC validation + user→poster resolution |
| `notion.py` | Query Notion CRM for campaigns + TikTok Sound Links |
| `campaign_hub.py` | Hub-as-source-of-truth + Notion sound matching with AI fuzzy fallback |
| `gdrive.py` | Google Drive API client |
| `upload.py` | TikTok/IG direct upload service |
| `email_routing.py` | Cloudflare email routing API client |
| `r2.py` | Cloudflare R2 storage |
| `ffmpeg.py` | ffmpeg primitives |
| `cropper.py` | Video cropping helpers |
| `captions.py` | Caption extraction helpers |
| `sound_cache.py` | Cached sound metadata |

### Core files

| File | Lines | Purpose |
|---|---|---|
| `app.py` | 276 | FastAPI entry, router registration, lifespan, CORS, request logging |
| `telegram_bot.py` | 1010 | Bot session, send/forward primitives, topic management, daily batch |
| `project_manager.py` | ~300 | Project CRUD, path utilities, sanitization |
| `debug_logger.py` | ~200 | Structured logging setup |

## Frontend

### Tech stack
- React 19, TypeScript, Vite 7
- Tailwind CSS v4 (`@tailwindcss/vite`, lightningcss for oklch→rgb)
- React Router DOM v7 (URL only, not for mount/unmount)
- Zustand for global state (`workflowStore`)
- shadcn/ui components (Button, Card, Badge, Input, etc.)

### Page structure (`frontend/src/pages/`)

| Page | Purpose |
|---|---|
| `Home.tsx` | Dashboard / project landing |
| `Generate.tsx` | AI video generation |
| `Captions.tsx` | Caption scraping (legacy single-page flow) |
| `CaptionsStage.tsx` | Caption staging |
| `Clipper.tsx` | 9:16 video clipping |
| `Burn.tsx` | Caption burning |
| `Recreate.tsx` | Recreate workflow |
| `Slideshow.tsx` | Slideshow renderer |
| `Create.tsx` | Unified create flow |
| `Distribution.tsx` | Distribution hub — wraps 4 sub-tabs |

### Distribution sub-tabs (`frontend/src/pages/distribution/`)

| Sub-tab | File | Purpose |
|---|---|---|
| Roster | `RosterTab.tsx` | Page roster CRUD, project assignment, Drive folder mapping |
| Telegram | `TelegramTab.tsx` | Bot config, staging group, posters, page assignment to posters, topic sync |
| Sounds | `SoundsTab.tsx` | Sound library: sync from Hub/Notion, manual add/edit, forward to posters |
| Uploads | `UploadsTab.tsx` | TikTok/IG direct upload jobs and status |

### Tab switching

Some pages use CSS-based tab switching (display:none for inactive) to preserve state across tabs. Distribution sub-tabs use URL routing (`/distribute/roster`, `/distribute/telegram`, etc.).

## Environment Variables

### Required

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | Sora 2 generation, GPT-4.1 caption OCR, GPT-4.1-mini sound matching |

### AI providers (set those you use)

| Variable | Provider |
|---|---|
| `XAI_API_KEY` | Grok |
| `FAL_KEY` | FAL (Wan, Kling, Ovi) |
| `LUMA_API_KEY` | Luma Dream Machine |
| `REPLICATE_API_TOKEN` | Replicate (MiniMax, Wan, Kling) |

### Integrations

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram bot (overrides stored token) |
| `NOTION_API_KEY` | Notion CRM access |
| `NOTION_CAMPAIGNS_DB` | Notion campaigns database ID |
| `NOTION_PAGES_DB` | Notion Master Pages database ID (account roster + Poster assignment) |
| `NOTION_SOUND_CUTOFF` | Filter Notion campaigns by created_time (default `2026-03-01`) |
| `MINIAPP_INITDATA_MAX_AGE` | Max age (s) of Mini App `initData` auth_date; `0` disables (default `86400`) |
| `MINIAPP_AGENT_KEY` | If set, Mini App agent endpoints require matching `X-Agent-Key` |
| `MINIAPP_DEV_AUTH` | `1` enables the `X-Dev-Poster-Id` bypass — **local dev only** |
| `CAMPAIGN_HUB_URL` | Campaign Hub base URL (default deployed URL) |
| `POSTIZ_API_KEY` | Postiz API |
| `R2_ACCESS_KEY` / `R2_SECRET_KEY` / `R2_BUCKET` | Cloudflare R2 |
| `GOOGLE_OAUTH_*` | Google Drive OAuth |
| `CLOUDFLARE_*` | Email routing |

### Infrastructure

| Variable | Purpose |
|---|---|
| `RAILWAY_VOLUME_MOUNT_PATH` | Volume mount path (set by Railway) |
| `CORS_ORIGINS` | Additional CORS origins (comma-separated) |
| `PORT` | Auto-set by Railway |

## Key Technical Decisions

- **`telegram_config.json` and `page_roster.json` are the live DB.** Atomic writes (tmp + rename). They live on the Railway volume so they survive deploys and restarts.
- **Bot token priority: env var > stored config.** Lets ops override without redeploys.
- **Source-of-truth split for sounds:** Campaign Hub owns "what's active," Notion owns "what's the URL," AI bridges naming differences.
- **Forum topics for organization.** Both staging group and poster groups use Telegram forum topics — one topic per page. Posters' Sound Assignment topics are separate.
- **Append-only topic mappings by default.** Topic creation never overwrites existing mappings unless `force=True` is explicitly passed (prevents accidental remapping bugs).
- **In-memory job state** — restart loses background job tracking, but files on disk and Telegram messages survive.
- **Single FastAPI process** — frontend served from same origin as API in production. No separate Node server.

## Conventions

- **All async** — FastAPI throughout, no sync I/O in routes
- **Project-scoped data** — most endpoints accept `?project=` query param
- **WebSocket for real-time** — captions, burn, clipper-pipeline; HTTP polling for video gen; SSE for clipper-batch
- **Atomic JSON writes** — every config save uses tmp file + rename
- **No DB ORM** — JSON files on volume; switch to Postgres if scale demands

## What NOT To Do

- Don't break the bot token priority (env over config)
- Don't write to `telegram_config.json` non-atomically (use `save_config`)
- Don't overwrite existing topic mappings without `force=True`
- Don't bypass `services.telegram` — go through the data layer, not raw JSON
- Don't add destructive bot operations (delete + repost) — append-only sends are safer
- Don't auto-trigger forwards or sends on app startup — manual or schedule-driven only

## Pending Work / Known Issues

- **RESOLVED — destructive delete-and-repost behavior in inventory tracking.** Root cause: the old `_scan_topic_fallback()` in `telegram_bot.py` brute-forced topic scanning by forwarding every message back into the same staging group to read its media, then deleting the copy; a crash/rate-limit/permission failure between forward and delete orphaned content and desynced inventory. Fix (in place): the fallback is now inert (returns "scan unavailable", never forwards/deletes/sends); real backfill goes through the read-only Pyrogram scanner; the live `forward_new_messages()` path checkpoints its high-water mark per successful forward and caps it on a blocking error so no message is orphaned or re-forwarded. Pinned by `tests/test_telegram_bot_safety.py` (6 tests). Bot is safe to run.
- **5 active campaigns unmatched** in latest sound sync (Liam St John, In Color, Alex Nicol, Gregory Alan Isakov, Matilda Lyn) — either missing TikTok Sound Links in Notion or names differ enough that AI matcher fails.
- **Sound Assignments feature** — in development. Per-page sound playlists, with sends grouped per poster by their pages. UI lives in Campaign Hub (separate repo); lab provides backend data + endpoints + send primitives.

## Dev Workflow

```bash
# Backend (also serves built frontend)
python app.py
# → http://127.0.0.1:8000

# Frontend dev (hot reload)
cd frontend && npm run dev
# → http://localhost:5173 (proxies /api/*, /ws/* to :8000)

# Tests
python -m pytest tests/ -v
cd frontend && npm test
```

System deps: `ffmpeg`, `yt-dlp`, `tesseract` on PATH.

## Legacy / Reference

Old 3-server system files (kept for parity reference, do not modify):
- `server.py`, `caption_server.py`, `burn_server.py`
- `static/index.html`, `static/captions/index.html`, `static/burn/index.html`
