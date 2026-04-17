# Content Posting Lab

Unified local + Railway-deployed webapp for TikTok-style video generation, caption scraping, video clipping, caption burning, slideshow assembly, and distribution (Postiz, Telegram, Google Drive, Email, direct upload). Single FastAPI backend + React/TypeScript frontend, served from one process.

Production: https://risingtides-content-lab-production.up.railway.app (Railway, pro plan, us-east4, 1 replica, persistent volume at `/app/projects`).

## How to Run

```bash
# Backend + built frontend (production mode)
python app.py
# → http://127.0.0.1:8000

# Frontend dev mode (hot reload, proxies API to :8000)
cd frontend && npm run dev
# → http://localhost:5173
```

Both modes require the backend running. In dev mode, Vite proxies `/api/*` and `/ws/*` to `localhost:8000`.

**Production entrypoint is `python main.py`** (see `Dockerfile`, `railway.toml`, `Procfile`). `main.py` is a shim that imports `app` from `app.py` and runs uvicorn with `timeout_keep_alive=120` + `$PORT`. The `__main__` block inside `app.py` is for local dev only.

## Architecture Overview

```
python main.py → uvicorn → app.py:app (port $PORT, default 8000)
├── FastAPI backend (15 routers under /api/*)
│   ├── /api/video/*          → routers/video.py        (video generation)
│   ├── /api/captions/*       → routers/captions.py     (caption scraping, WebSocket)
│   ├── /api/clipper/*        → routers/clipper.py      (video clipping, SSE + WebSocket)
│   ├── /api/burn/*           → routers/burn.py         (caption burning, WebSocket + polling)
│   ├── /api/projects/*       → routers/projects.py     (project CRUD)
│   ├── /api/recreate/*       → routers/recreate.py     (prompt-from-video, WebSocket)
│   ├── /api/slideshow/*      → routers/slideshow.py    (image slideshow rendering)
│   ├── /api/postiz/*         → routers/postiz.py       (Postiz upload + scheduling)
│   ├── /api/telegram/*       → routers/telegram.py     (Telegram bot + poster groups)
│   ├── /api/roster/*         → routers/roster.py       (page roster + Notion sync)
│   ├── /api/upload/*         → routers/upload.py       (direct TikTok uploader)
│   ├── /api/drive/*          → routers/gdrive.py       (Google Drive integration)
│   ├── /api/email/*          → routers/email_routing.py (email routing rules)
│   ├── /api/debug/*          → routers/debug.py        (structured log stream + errors)
│   └── /api/health           → app.py                  (system health check)
├── Static file mounts
│   ├── /fonts/               → fonts/
│   ├── /output/              → output/
│   ├── /projects/            → projects/
│   ├── /caption-output/      → caption_output/
│   └── /burn-output/         → burn_output/
└── SPA fallback              → frontend/dist/index.html

React frontend (frontend/dist/ or dev server on :5173)
├── App.tsx              → Shell: header, project selector, 4 top-level stages
│                          (Home, Create, Captions, Distribute)
│                          Lazy-mounted stages + CSS display toggling.
│
├── pages/Home.tsx              → Project list + landing
├── pages/Create.tsx            → Container for Create sub-tabs
│    ├── /create              → Generate.tsx    (video generation)
│    ├── /create/clipper      → Clipper.tsx     (video clipping)
│    ├── /create/recreate     → Recreate.tsx    (prompt-from-video)
│    └── /create/slideshow    → Slideshow.tsx   (beat-synced slideshow)
├── pages/CaptionsStage.tsx     → Container for Captions sub-tabs
│    ├── /captions            → Captions.tsx    (TikTok caption scraping)
│    └── /captions/burn       → Burn.tsx        (caption burning)
└── pages/Distribution.tsx      → Roster + Telegram + Sounds + Uploads tabs (in one file)
     ├── /distribute          → roster view
     ├── /distribute/telegram → telegram config + posters
     ├── /distribute/sounds   → sound campaign library
     └── /distribute/uploads  → direct upload status
```

## Hard Constraints

- **NO database** — filesystem + in-memory only. No SQLite, no Postgres. External state lives in Notion, Telegram, Google Drive, and Postiz.
- **NO authentication on the app itself** — single-user tool. Railway URL is a shared secret.
- **NO mobile responsive** — desktop-first.
- **Component library IS IN USE** (shadcn-ui under `frontend/src/components/ui/`, plus radix-ui primitives). Earlier constraint "NO component library" has been relaxed — shadcn Button/Card/Badge/Input are used throughout.
- **Provider roster is Replicate-only + xAI Grok** — FAL, Luma, Sora have been removed. Don't re-add them without a conversation. Adding Replicate models is fine (edit `providers/__init__.py`).
- **ZERO feature drops from the unified app** — every stage must keep working when touching shared infra (project_manager, app.py, workflowStore).
- **Legacy 3-server files are reference-only** (see Legacy Files section) — don't modify.

## Project Structure

All per-project data lives under `projects/{name}/` (persistent Railway volume mounted at `/app/projects`):
```
projects/{name}/
├── videos/          ← generated MP4s
├── captions/        ← scraped caption frames + CSVs
├── clips/           ← clipped video segments (per job_id subdirs, plus _staging_* for uploads)
├── burned/          ← final burned MP4s (per batch_id subdirs)
├── slideshow/       ← slideshow images + rendered outputs
├── recreate/        ← recreate-from-video working files
└── prompts.json     ← prompt history (auto-saved on generate, max 200)
```

`project_manager.py` (351 lines) handles all project CRUD, path resolution, and filesystem safety (sanitization, path traversal blocking).

## Backend

### Entry Point — `app.py` (276 lines) + `main.py` (17 lines)

- FastAPI with lifespan handler (creates output dirs, ensures default project, starts Telegram bot if token set).
- Structured logging via `debug_logger.py` (initialized in lifespan). Log stream exposed at `/api/debug/stream` (SSE).
- CORS: localhost dev ports by default, extend via `CORS_ORIGINS` env var.
- HTTP request middleware logs every `/api/*` request and any >1s request, skips static mounts.
- Mounts 15 routers + 5 static dirs + SPA fallback.
- `/api/health` returns `{status, ffmpeg, ytdlp, providers, postiz}`.

### Router: Video Generation — `routers/video.py` (569 lines)

Generates AI videos from text prompts using the providers in `providers/__init__.py`.

**Endpoints:**
- `GET /api/video/providers` — list available providers (filtered by which API keys are set)
- `GET /api/video/provider-schemas` — provider-specific parameter schemas
- `GET /api/video/prompts?project=` — prompt history
- `DELETE /api/video/prompts?project=` — clear prompt history
- `DELETE /api/video/file` — delete a single generated video
- `POST /api/video/generate` — submit a generation job (multipart)
- `GET /api/video/jobs` — list jobs
- `GET /api/video/jobs/{job_id}` — poll job status
- `DELETE /api/video/jobs/{job_id}` — cancel/remove job
- `GET /api/video/jobs/{job_id}/download-all` — ZIP of all completed videos
- `POST /api/video/bulk-download` — custom bulk ZIP
- `POST /api/video/bulk-delete` — bulk delete

**State:** in-memory `jobs` dict.

### Router: Caption Scraping — `routers/captions.py` (397 lines)

Scrapes TikTok profiles to extract burned-in caption text.

**Endpoints:**
- `WebSocket /api/captions/ws/{job_id}` — real-time scraping pipeline (status, urls_collected, frame_ready, ocr_*, all_complete)
- `GET /api/captions/export/{username}?project=` — download CSV
- `GET /api/captions/history?project=` — list past scrape sessions
- `POST /api/captions/rename-batch` — rename a scrape folder

**Pipeline:** yt-dlp listing → thumbnail download (batches of 5) → GPT-4.1 vision OCR (batches of 10) → CSV to `projects/{name}/captions/`. Sentiment analysis lives in `scraper/sentiment_analyzer.py`.

### Router: Video Clipper — `routers/clipper.py` (1311 lines)

Downloads or accepts uploaded videos and chops them into 9:16 short-form clips.

**Endpoints:**
- `POST /api/clipper/upload` — single file upload
- `POST /api/clipper/upload-stream` — streaming raw binary upload (handles files >3GB, required for Railway's proxy limits)
- `POST /api/clipper/stage-streamed` — finalize a streamed upload into staging
- `POST /api/clipper/upload-batch` — multi-file upload with thumbnail + ffprobe
- `POST /api/clipper/download-url` — download from URL (yt-dlp)
- `POST /api/clipper/trim-batch` — set per-source trim in/out
- `POST /api/clipper/process-batch` — **start** a process job (returns job_id)
- `GET /api/clipper/process-batch/{job_id}` — SSE stream of progress events
- `WebSocket /api/clipper/ws/{job_id}` — alternative real-time pipeline
- `GET /api/clipper/jobs?project=` — list completed clip jobs
- `DELETE /api/clipper/jobs/{job_id}?project=` — delete job
- `GET /api/clipper/jobs/{job_id}/download-all?project=` — ZIP

**Note:** process-batch was split into POST (start) + GET SSE (stream) to avoid Railway's proxy timeout on long-lived POSTs. PR #22 (2026-04-17) added streaming uploads past Railway's proxy size limits.

### Router: Caption Burning — `routers/burn.py` (1171 lines)

Burns caption overlay PNGs onto videos using ffmpeg.

**Endpoints:**
- `GET /api/burn/videos?project=` — list project videos
- `GET /api/burn/captions?project=` — list caption CSVs
- `GET /api/burn/fonts` — list fonts
- `POST /api/burn/overlay` — enqueue a single burn (accepts base64 PNG overlay, fires background task)
- `GET /api/burn/batch-status/{batch_id}` — poll endpoint for burn progress (frontend polls ~every 2.5s until done)
- `GET /api/burn/batches?project=` — list past batches
- `GET /api/burn/zip/{batch_id}` — ZIP download of a batch
- `WebSocket /api/burn/ws` — legacy WebSocket burn pipeline

**State:** in-memory `_burn_jobs[batch_id][idx]` tracks queued / burning / done / error per pair. Dict resets on deploy — no persistence.

**Security note:** `POST /overlay` takes `videoPath` from client and joins it to the project dir. Currently missing a resolve/prefix check (see `routers/video.py:228` for the correct pattern). Flagged for fix.

### Router: Recreate — `routers/recreate.py` (416 lines)

Takes a video and produces a matching generation prompt + re-runs through video providers.

**Endpoints:**
- `POST /api/recreate/generate-prompt` — GPT prompt from video frame
- `WebSocket /api/recreate/ws/{job_id}` — real-time recreate pipeline
- `GET /api/recreate/jobs` — list jobs
- `DELETE /api/recreate/jobs/{job_id}` — delete

### Router: Slideshow — `routers/slideshow.py` (992 lines)

Assembles image slideshows with music, beat-sync, meme mode, and reusable formats.

**Key endpoints:** image upload/list/delete, audio upload/list/delete, format CRUD, `POST /render`, `POST /render-v2` (beat-synced from campaign hub videos), `POST /render-meme`, `GET /job/{job_id}`, `GET /renders`, `GET /project-videos`, `GET /captions`, `POST /sounds/prepare`, `GET /sounds/{sound_id}/audio`.

Uses `librosa` (required in requirements.txt) for beat detection.

### Router: Projects — `routers/projects.py` (252 lines)

**Endpoints:** `GET /`, `POST /`, `GET /{name}`, `DELETE /{name}`, `GET /{name}/stats`, `POST /import-legacy`.

`app.py` also mounts a `GET /api/projects` (no trailing slash) alias.

### Router: Postiz — `routers/postiz.py` (211 lines)

Uploads burned videos to Postiz and schedules posts.

**Endpoints:** `GET /status`, `GET /integrations`, `GET /videos`, `POST /upload`, `POST /posts`.

### Router: Telegram — `routers/telegram.py` (1541 lines — biggest router)

Two-layer Telegram bot system (staging group + poster groups) for distributing content to posters. See memory: [Telegram pipeline](../.claude/projects/-Users-risingtidesdev-dev-content-posting-lab/memory/project_telegram_pipeline.md).

**Major endpoint groups:**
- Bot token / status: `GET /status`, `PUT /bot-token`, `DELETE /bot-token`
- Staging group: `PUT/GET /staging-group`, `POST /staging-group/sync-topics`, `POST/GET /staging-group/scan-inventory`, `POST/GET /staging-group/discover-topics`, topic CRUD
- Posters (poster groups): `GET/POST /posters`, `PUT/DELETE /posters/{id}`, `POST /posters/reset-defaults`, page assignment, topic sync & discovery
- Sends: `POST /send`, `POST /send-batch`, `POST /assign-batch`, `POST /forward/{integration_id}`
- Inventory: `GET /inventory/{id}`, `GET /inventory`, `DELETE /inventory/scan`, `GET /log`
- Sounds (campaign library): full CRUD, `POST /sounds/sync-notion`, `POST /sounds/sync-hub`, `POST /sounds/sync`, `POST /sounds/forward/{poster_id}`, `POST /sounds/forward-all`
- Schedule: `GET/PUT /schedule`, `POST /batch/run`

Bot lifecycle runs in `app.py` lifespan (`services.telegram.get_bot_token()` + `telegram_bot.start_bot/stop_bot`).

### Router: Roster — `routers/roster.py` (351 lines)

Manages the page roster (creator integrations) and syncs with Notion.

**Endpoints:** `GET /`, `GET /project/{name}`, `PUT /{integration_id}`, `DELETE /{integration_id}`, `GET /duplicates`, `POST /dedup`, `POST /sync`.

### Router: Upload — `routers/upload.py` (180 lines)

Direct-to-TikTok uploader (uses `tiktokautouploader`, which is stripped from the prod Dockerfile — only available locally).

**Endpoints:** `POST /submit`, `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel`, `GET /cookies`, `GET /cookies/{account_name}`, `POST /login/{account_name}`, `GET /stats`.

### Router: Google Drive — `routers/gdrive.py` (161 lines)

**Endpoints:** `GET /status`, `GET /folder/{id}`, `GET /folder/{id}/count`, `POST /upload`, `POST /upload-batch`, `DELETE /file/{id}`, `GET /inventory`.

### Router: Email Routing — `routers/email_routing.py` (203 lines)

Rule-based routing of inbound content emails to projects/destinations.

**Endpoints:** `GET /status`, `GET/POST /rules`, `PUT/DELETE /rules/{id}`, `POST /auto-create`, `GET/POST /destinations`.

### Router: Debug — `routers/debug.py` (192 lines)

Observability API for the structured logger.

**Endpoints:** `GET /logs`, `GET /stream` (SSE), `GET /jobs/{job_id}`, `GET /errors`, `GET /health`, `POST /clear`.

### Providers — `providers/` (3 modules + registry)

```python
# providers/__init__.py — PROVIDERS registry
"grok"         → grok.py       (xAI)        grok-imagine-video,   ~$5/10s
"hailuo"       → replicate.py  (Replicate)  minimax/hailuo-2.3,   ~$0.28/video (forces 16:9)
"wan-t2v"      → replicate.py  (Replicate)  wan-video/wan-2.2-t2v-fast,  ~$0.06/sec
"wan-i2v"      → replicate.py  (Replicate)  wan-video/wan-2.2-i2v-a14b,  ~$0.06/sec
"wan-i2v-fast" → replicate.py  (Replicate)  wan-video/wan-2.2-i2v-fast,  ~$0.06/sec
```

Health check also reports `pruna-pvideo` and `pruna-pvideo-vertical` — these flow through the Replicate provider module with different model IDs set at call time (see PR #21).

`providers/base.py` (238 lines): `generate_one()` universal entry point. Handles provider dispatch, download, optional center-crop to 9:16, and multi-crop modes (`dual` / `triptych` / `both`) for landscape-only providers (`FORCE_LANDSCAPE = {"hailuo"}`).

API keys (loaded from `.env` or Railway env):
- `XAI_API_KEY` — Grok
- `REPLICATE_API_TOKEN` — all Replicate + Pruna providers
- `OPENAI_API_KEY` — GPT-4.1 caption OCR + recreate prompt generation

Additional service keys: `POSTIZ_API_KEY`, `NOTION_API_KEY`, `NOTION_CAMPAIGNS_DB`, Telegram bot token (stored via `services/telegram.py`), Google Drive creds.

### Services — `services/` (2128 lines total)

Business logic extracted out of routers:

| File | Lines | Purpose |
|------|-------|---------|
| `campaign_hub.py` | 358 | Notion-backed sound campaign state machine |
| `captions.py` | 64 | Shared caption CSV helpers |
| `cropper.py` | 23 | Video crop helpers |
| `email_routing.py` | 175 | Email rule engine + destination routing |
| `gdrive.py` | 211 | Google Drive client wrapper |
| `notion.py` | 187 | Notion API client |
| `roster.py` | 118 | Roster dedup + Notion sync logic |
| `sound_cache.py` | 182 | Local sound file cache |
| `telegram.py` | 526 | Token storage, staging + poster group helpers |
| `upload.py` | 284 | TikTok upload queue logic (lazy `tiktokautouploader` import) |

### Scraper Utilities — `scraper/` (994 lines)

| File | Lines | Purpose |
|------|-------|---------|
| `frame_extractor.py` | 310 | yt-dlp video listing, thumbnail + ffmpeg frame extraction |
| `caption_extractor.py` | 83 | GPT-4.1 vision API for caption OCR |
| `ocr_extractor.py` | 100 | Tesseract OCR fallback (unguarded top-level `import pytesseract` — landmine, pytesseract stripped from prod) |
| `sentiment_analyzer.py` | 163 | GPT sentiment + topic classification |
| `tiktok_scraper.py` | 338 | Playwright browser scraping (stripped from prod) |

### Dev-only modules (`debug_logger.py`, `telegram_bot.py`, `project_manager.py`)

- `debug_logger.py` (~200 lines) — structured JSON logger with per-job correlation. Uses `str | None` syntax — **requires Python 3.10+** (prod uses `python:3.10-slim`).
- `telegram_bot.py` (~1000 lines) — aiogram + pyrogram-tg bot runtime, invoked from app.py lifespan.

## Frontend

### Tech Stack

- React 19, TypeScript 5.9, Vite 7.3
- Tailwind CSS v4.2 (via `@tailwindcss/vite` plugin)
- shadcn-ui (`frontend/src/components/ui/`) + radix-ui primitives
- `lucide-react` icons, `class-variance-authority` + `clsx` + `tailwind-merge` for class composition
- React Router DOM v7 (for URL management and nested stage routing)
- Zustand v5 (global state)
- `html2canvas` (burn overlay rendering)
- `lightningcss` (converts oklch → rgb at build time for browser compat)
- Vitest + Testing Library + Playwright (unit + e2e)

### Key Architecture Decisions

**Hybrid mounting strategy:**
- **Top-level stages** (Home / Create / Captions / Distribute) are **lazy-mounted on first visit** and then kept mounted via CSS `display` toggling. See `App.tsx:40-311`.
- **Sub-tabs inside Create and Captions** follow the same lazy-then-sticky pattern (`Create.tsx`, `CaptionsStage.tsx`).
- Home is always mounted (landing page).

This keeps WebSocket connections alive, form state intact, and running jobs visible when switching tabs — but defers heavy pages (Burn, Clipper, Slideshow) until first click.

**Zustand store** (`stores/workflowStore.ts`, 333 lines) holds active project, notifications, job tracking counts (`videoRunningCount`, `captionJobActive`, `recreateJobActive`, `burnReadyCount`, etc.), generate/upload jobs, roster cache, and burn selection drafts. Active project is persisted to localStorage.

**WebSocket hook** (`hooks/useWebSocket.ts`, 242 lines) handles connection lifecycle, auto-reconnect with exponential backoff, message queuing, start-payload memory for reconnects, and a `shouldReconnect` callback to prevent reconnect loops after pipeline completion.

**API base URL helper:** `frontend/src/lib/api.ts` (`apiUrl()`) — used everywhere instead of hardcoded `/api/...` so the frontend can be pointed at a different backend if needed.

### Page File Map

| File | Lines | Purpose |
|------|-------|---------|
| `App.tsx` | 323 | Shell: header, project selector, 4 stage tabs, health banner, toast container, lazy stage mounting |
| `pages/Home.tsx` | 544 | Landing / project list with stats, quick create, delete confirm |
| `pages/Create.tsx` | 96 | Container for 4 Create sub-tabs (Generate, Clipper, Recreate, Slideshow) |
| `pages/Generate.tsx` | 1315 | Video generation UI — provider select, prompt, form, job cards, prompt history |
| `pages/Clipper.tsx` | 1121 | Video clipping — upload/URL ingest, per-source trim timeline, SSE processing, results grid, send-to-burn |
| `pages/Recreate.tsx` | 685 | Prompt-from-video + re-run through providers |
| `pages/Slideshow.tsx` | 1292 | Beat-synced slideshow — images, audio, formats, render queue, meme mode |
| `pages/CaptionsStage.tsx` | 82 | Container for 2 Caption sub-tabs (Scrape, Burn) |
| `pages/Captions.tsx` | 855 | TikTok caption scraping — username input, WebSocket pipeline, results table, CSV export |
| `pages/Burn.tsx` | 1546 | Caption burning — video browser, pairing, html2canvas overlay, color correction, batch-status polling |
| `pages/Distribution.tsx` | 1119 | Distribute stage — roster, Telegram config + posters + sounds, direct uploads |
| `stores/workflowStore.ts` | 333 | Zustand global state |
| `hooks/useWebSocket.ts` | 242 | WebSocket with reconnect + queue |
| `hooks/useProject.ts` | 10 | Project selector hook |
| `types/api.ts` | 717 | TypeScript contracts for all API requests/responses/WebSocket events |
| `components/` | — | Shared UI (ConfirmModal, EmptyState, ErrorBoundary, FileBrowser, LazyVideo, ProgressBar, ProjectSelector, StatusChip, TabNav, Toast, ToastContainer, AssignToPagesDialog, `ui/` shadcn primitives) |

### Stage navigation map

```
/                       → Home (landing + projects)
/create                 → Generate (default sub-tab)
/create/clipper         → Clipper
/create/recreate        → Recreate
/create/slideshow       → Slideshow
/captions               → Scrape (default sub-tab)
/captions/burn          → Burn
/distribute             → Roster (default sub-tab)
/distribute/telegram    → Telegram config
/distribute/sounds      → Sounds library
/distribute/uploads     → Direct uploads
```

## Directory Layout

```
content-posting-lab/
├── app.py                     # FastAPI wiring (276 lines)
├── main.py                    # Production entrypoint shim (17 lines)
├── project_manager.py         # Project CRUD + path safety (351 lines)
├── debug_logger.py            # Structured logger (Python 3.10+)
├── telegram_bot.py            # aiogram + pyrogram bot runtime
├── Dockerfile                 # 2-stage: Node build → Python 3.10-slim runtime
├── railway.toml               # Railway config (builder=dockerfile, startCommand=python main.py)
├── Procfile                   # web: python main.py
├── requirements.txt           # Python deps (playwright/pytesseract/tiktokautouploader stripped in Dockerfile)
├── routers/                   # 15 router modules
├── providers/                 # grok.py, replicate.py, base.py, __init__.py (registry)
├── services/                  # 10 business-logic modules
├── scraper/                   # yt-dlp + OCR + sentiment + playwright
├── frontend/                  # React 19 + Vite 7 + Tailwind v4 + shadcn
├── tests/                     # pytest suite
├── fonts/                     # TikTokSans + Montserrat
├── projects/                  # Per-project data (persistent Railway volume → /app/projects)
├── static/                    # Legacy UIs (reference only)
├── .env                       # API keys (gitignored)
├── .agentignore               # Archived handoff docs
└── Legacy root-level files    # server.py, caption_server.py, burn_server.py (reference only)
```

## Testing

```bash
# Frontend
cd frontend && npm test              # Vitest unit tests
cd frontend && npm run test:e2e      # Playwright e2e

# Backend
python -m pytest tests/ -v
```

**Frontend tests:** `App.test.tsx`, `Burn.test.tsx`, `Captions.test.tsx`, `Generate.test.tsx`, `Home.test.tsx`, `Recreate.test.tsx`.

**Backend tests:** `tests/test_smoke.py`, `tests/test_projects_api.py`, `tests/test_video_api.py`, `tests/test_burn_and_captions_api.py`, `tests/test_recreate_api.py`, `tests/test_replicate_text_removal.py`, `tests/e2e/test_smoke.py`.

Build (`cd frontend && npm run build`) is clean; output ~655 kB / 189 kB gzip (single chunk — no route code splitting).

## Prerequisites

```bash
pip install -r requirements.txt
brew install ffmpeg tesseract yt-dlp    # system deps (macOS)
```

**System binaries on PATH:**
- `ffmpeg` + `ffprobe` — all video workflows
- `yt-dlp` — TikTok listing, URL ingest
- `tesseract` — optional OCR fallback

**Required API keys** (see `.env.example`):
- `XAI_API_KEY` — Grok
- `REPLICATE_API_TOKEN` — Hailuo, Wan variants, Pruna variants
- `OPENAI_API_KEY` — GPT-4.1 caption OCR + recreate prompt
- `POSTIZ_API_KEY` — Postiz uploads
- `NOTION_API_KEY` + `NOTION_CAMPAIGNS_DB` — campaign hub sync
- Telegram bot token (set via `PUT /api/telegram/bot-token`, stored in `telegram_config.json`)
- Google Drive service account (for `/api/drive/*`)

**Python 3.10+ required** (PEP 604 union syntax in `debug_logger.py`). Dockerfile uses `python:3.10-slim-bookworm`.

## Deployment (Railway)

- **Builder:** Dockerfile (2-stage: Node build → Python 3.10-slim runtime).
- **Start command:** `python main.py` (port from `$PORT`).
- **Volume:** mounted at `/app/projects` via dashboard config. `railway.toml`'s `mountPath = "/data"` is **misaligned** with the actual mount — dashboard override wins, but the toml is misleading. Fix planned.
- **Proxy limits:** Railway's proxy enforces timeouts + body size limits. Clipper and burn have dedicated workarounds:
  - `POST /api/clipper/upload-stream` for files >3GB
  - `POST /api/clipper/process-batch` + `GET .../process-batch/{job_id}` split for long SSE
  - `uvicorn ... timeout_keep_alive=120` in `main.py` (must exceed Railway's 60s keep-alive)
- **Dockerfile strips heavy-ish deps** from prod: `playwright`, `playwright-stealth`, `pytesseract`, `tiktokautouploader`. Local dev installs them. Any prod-side code that imports these must use `try/except ImportError`.

## Conventions and Gotchas

- **All async** — FastAPI throughout, no sync blocking (except `subprocess.run` for ffmpeg version checks in health).
- **In-memory job state** — `jobs`, `_burn_jobs`, recreate/upload dicts all reset on deploy. Files on disk survive; poll UIs can get stuck if `batch_id` from a previous deploy comes back empty (see burn).
- **No inter-service HTTP** — routers read project directories directly via filesystem.
- **Real-time transport mix:**
  - WebSocket → captions, burn (legacy path), clipper (legacy path), recreate
  - SSE (StreamingResponse) → clipper process-batch, debug log stream
  - HTTP polling → video jobs, burn batch-status, upload jobs
- **Project-scoped everything** — most endpoints accept `?project=` query param; frontend sends active project name on every request.
- **Font defaults:** `fonts/TikTokSans16pt-Bold.ttf` for burn, `fonts/Montserrat-*` available. White text with black stroke by default.
- **Color correction** in burn uses ffmpeg `eq` and `colorbalance` filters — slider values map directly.
- **Tailwind v4 oklch colors** → lightningcss converts to rgb at build time.
- **shadcn in use** — `components/ui/button.tsx`, `card.tsx`, `badge.tsx`, `input.tsx`, etc. are the primary building blocks. Don't reinvent these.
- **Structured logging** — use `debug_logger` helpers (job_id correlation) for any long-running pipeline; surfaces in `/api/debug/stream`.

## Legacy Files (DO NOT MODIFY)

Old 3-server system files remain for reference — **not imported anywhere**:

- `server.py` — original video generation server
- `caption_server.py` — original caption scraper
- `burn_server.py` — original caption burn server
- `static/index.html`, `static/captions/index.html`, `static/burn/index.html` — legacy UIs
- `test_full_pipeline.py`, `test_ocr_only.py`, `test_pipeline.py` (at repo root) — legacy pipeline probes; real tests live in `tests/`

These are authoritative reference for feature parity. If the new app behaves differently from these files, the new app has a bug — but modify with care and never touch them as part of unrelated work.

## Dev Workflow

1. **Create a project** — Home tab. Projects are workspaces; all generated/scraped/clipped/burned content is scoped to one.
2. **Generate videos** — `/create`. Pick provider, write prompt, set params, submit. Videos land in `projects/{name}/videos/`.
3. **Clip source videos** — `/create/clipper`. Upload or paste URLs, trim in/out, set clip length, process into 9:16 clips. Output in `projects/{name}/clips/{job_id}/`. Can send clips directly to Burn.
4. **Recreate** — `/create/recreate`. Feed an existing video, get a GPT-generated prompt, re-run through providers.
5. **Slideshow** — `/create/slideshow`. Assemble image slideshows with beat-synced audio, meme mode, or reusable formats.
6. **Scrape captions** — `/captions`. Enter TikTok username, scrape frames via yt-dlp, extract captions via GPT-4.1 vision. CSV to `projects/{name}/captions/`.
7. **Burn captions onto videos** — `/captions/burn`. Pair videos with captions, customize overlay (font, color, FX sliders), render. Output in `projects/{name}/burned/{batch_id}/`.
8. **Distribute** — `/distribute`. Manage the page roster (Notion-synced), configure Telegram posters, push batches to staging group or directly forward to poster groups, upload to Postiz, or use the direct TikTok uploader.

Result: final captioned, color-corrected videos distributed to the right creators on the right platforms.
