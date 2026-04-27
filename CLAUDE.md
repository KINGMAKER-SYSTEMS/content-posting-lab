# Content Posting Lab

> **Last updated:** 2026-04-27
> **Status:** Deployed to Railway. Active development on Telegram distribution and Sound Assignments.

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
| `page_roster.json` | Postiz integrations + project + Drive folder mappings |
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
| `telegram.py` | 1541 | 30+ | Bot, staging, posters, sounds, schedule, inventory |
| `email_routing.py` | 203 | 8 | Cloudflare email routing rules |
| `upload.py` | 180 | 8 | TikTok/IG direct uploads |
| `gdrive.py` | 161 | 7 | Google Drive folder ops |
| `debug.py` | 192 | 7 | Logs, errors, health, diagnostics |

### Services (`services/`)

| File | Purpose |
|---|---|
| `telegram.py` | `telegram_config.json` data access (posters, sounds, staging, schedule) |
| `roster.py` | `page_roster.json` data access |
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
| `NOTION_SOUND_CUTOFF` | Filter Notion campaigns by created_time (default `2026-03-01`) |
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

- **Bot occasionally exhibited destructive delete-and-repost behavior in inventory tracking.** Bot is currently kept stopped between deploys for safety. Investigation pending.
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
