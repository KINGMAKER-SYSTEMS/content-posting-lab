# Content Posting Lab

Unified local webapp for TikTok-style video generation, caption scraping, and caption burning. Single FastAPI backend + React/TypeScript frontend, served from one process.

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

## Architecture Overview

```
python app.py (port 8000)
├── FastAPI backend
│   ├── /api/video/*      → routers/video.py      (video generation)
│   ├── /api/captions/*   → routers/captions.py    (caption scraping via WebSocket)
│   ├── /api/burn/*       → routers/burn.py        (caption burning via WebSocket)
│   ├── /api/projects/*   → routers/projects.py    (project CRUD)
│   └── /api/health       → app.py                 (system health check)
├── Static file mounts
│   ├── /fonts/           → fonts/
│   ├── /output/          → output/
│   ├── /projects/        → projects/
│   ├── /caption-output/  → caption_output/
│   └── /burn-output/     → burn_output/
└── SPA fallback          → frontend/dist/index.html

React frontend (frontend/dist/ or dev server on :5173)
├── App.tsx               → Shell with CSS-based tab switching (all pages always mounted)
├── pages/Generate.tsx    → Video generation UI
├── pages/Captions.tsx    → Caption scraping UI
├── pages/Burn.tsx        → Caption burning UI
└── pages/Projects.tsx    → Project management UI
```

## Hard Constraints

- **NO database** — filesystem + in-memory only. No SQLite, no Postgres.
- **NO authentication** — single-user local tool.
- **NO new video providers** — migrate existing 9, don't add new ones.
- **NO backend logic rewrites** — provider integrations and ffmpeg pipelines are final.
- **NO mobile responsive** — desktop-first.
- **NO component library** (shadcn/Radix/Chakra) — Tailwind CSS directly.
- **ZERO feature drops** — every control, endpoint, and UX flow from the legacy 3-server system must work identically.

## Project Structure

All data is organized per-project under `projects/{name}/`:
```
projects/{name}/
├── videos/         ← generated MP4s land here
├── captions/       ← scraped caption frames + CSVs
├── burned/         ← final burned MP4s
└── prompts.json    ← prompt history (auto-saved on generate)
```

The `project_manager.py` module handles all project CRUD, path resolution, and filesystem safety (sanitization, path traversal blocking).

## Backend

### Entry Point — `app.py` (174 lines)

- FastAPI with lifespan handler (creates output dirs, ensures default project)
- CORS for localhost dev servers
- Mounts 4 routers + static file directories
- SPA fallback serves `frontend/dist/index.html` for all non-API routes
- Health check at `/api/health` (ffmpeg, yt-dlp, provider key status)

### Router: Video Generation — `routers/video.py` (191 lines)

Generates AI videos from text prompts using multiple providers.

**Endpoints:**
- `GET /api/video/providers` — list available providers (filtered by which API keys are set)
- `POST /api/video/generate` — submit a generation job (multipart form: prompt, provider, count, duration, aspect_ratio, resolution, optional media file)
- `GET /api/video/jobs/{job_id}` — poll job status
- `GET /api/video/jobs/{job_id}/download-all` — ZIP download of all completed videos
- `GET /api/video/prompts?project=` — get prompt history for a project
- `DELETE /api/video/prompts?project=` — clear prompt history

**State:** In-memory `jobs` dict. Prompt history persisted to `projects/{name}/prompts.json` (max 200 entries).

**Flow:** POST /generate → creates job → spawns async tasks via `providers.base.generate_one()` → each task polls the provider API → downloads MP4 → optionally crops via ffmpeg → writes to `projects/{name}/videos/`.

### Router: Caption Scraping — `routers/captions.py` (282 lines)

Scrapes TikTok profiles to extract burned-in caption text from their videos.

**Endpoints:**
- `WebSocket /api/captions/ws/{job_id}` — real-time scraping pipeline
- `GET /api/captions/export/{username}?project=` — download captions CSV

**WebSocket protocol:** Client sends `{"action": "start", "profile_url": "...", "max_videos": 20, "sort": "latest", "project": "..."}`. Server streams events:
1. `status` — progress text
2. `urls_collected` — found N video URLs
3. `downloading` / `frame_ready` / `frame_error` — per-video frame extraction
4. `ocr_starting` / `ocr_started` / `ocr_done` — GPT-4.1 vision OCR per frame
5. `all_complete` — final results array + CSV path
6. `error` — pipeline failure

**Pipeline:** yt-dlp video listing → thumbnail download (batches of 5) → GPT-4.1 vision OCR (batches of 10) → write CSV to `projects/{name}/captions/`.

### Router: Caption Burning — `routers/burn.py` (765 lines)

Burns caption overlay PNGs onto videos using ffmpeg.

**Endpoints:**
- `GET /api/burn/videos?project=` — list available videos in project
- `GET /api/burn/captions?project=` — list caption CSVs in project
- `GET /api/burn/fonts` — list available font files
- `GET /api/burn/batches?project=` — list completed burn batches
- `POST /api/burn/overlay` — render a single text overlay PNG (Pillow)
- `GET /api/burn/batches/{batch_id}/{filename}` — serve a burned video file
- `GET /api/burn/batches/{batch_id}/download-all` — ZIP download of batch
- `DELETE /api/burn/batches/{batch_id}?project=` — delete a burn batch
- `WebSocket /api/burn/ws` — burn pipeline

**WebSocket protocol:** Client sends `{"pairs": [...], "project": "..."}`. Each pair has `videoPath`, optional `overlayPng` (base64 PNG from html2canvas), optional `colorCorrection`. Server streams:
1. `burning` — starting item N of M
2. `burned` — item complete (success/fail)
3. `complete` — batch finished, all results

**Burn process:** Receives base64 PNG overlay → writes temp file → ffmpeg composites overlay onto video → applies color correction filters → outputs to `projects/{name}/burned/{batch_id}/`.

### Router: Projects — `routers/projects.py` (252 lines)

**Endpoints:**
- `GET /api/projects/` — list all projects with stats
- `POST /api/projects/` — create new project
- `GET /api/projects/{name}` — get single project stats
- `DELETE /api/projects/{name}` — delete project and all contents
- `GET /api/projects/{name}/stats` — detailed per-directory stats

### Providers — `providers/` (489 lines total)

Each provider module implements an async generation function called by `providers/base.py:generate_one()`.

| Provider ID | Module | API | Notes |
|-------------|--------|-----|-------|
| `grok` | `grok.py` | xAI API | Direct URL return |
| `rep-minimax` | `replicate.py` | Replicate | MiniMax Hailuo 2.3, polling |
| `rep-wan` | `replicate.py` | Replicate | Wan 2.1 720p, polling |
| `rep-kling` | `replicate.py` | Replicate | Kling v2.1, polling |
| `fal-wan` | `fal.py` | FAL | Wan 2.5, polling |
| `fal-kling` | `fal.py` | FAL | Kling 2.5 turbo, polling |
| `fal-ovi` | `fal.py` | FAL | Ovi, polling |
| `luma` | `luma.py` | Luma API | Ray 2, polling |
| `sora` | `sora.py` | OpenAI API | Sora 2, polling |

`providers/base.py` (142 lines): `generate_one()` is the universal entry point. Handles provider dispatch, ffmpeg aspect ratio cropping, file download, and error handling.

API keys loaded from `.env`: `XAI_API_KEY`, `FAL_KEY`, `LUMA_API_KEY`, `REPLICATE_API_TOKEN`, `OPENAI_API_KEY`.

### Scraper Utilities — `scraper/` (716 lines total)

| File | Lines | Purpose |
|------|-------|---------|
| `frame_extractor.py` | 211 | yt-dlp video listing, thumbnail download, ffmpeg frame extraction |
| `caption_extractor.py` | 67 | GPT-4.1 vision API call to read captions from a frame image |
| `ocr_extractor.py` | 100 | Local Tesseract OCR fallback (crops center 60%, binarizes) |
| `tiktok_scraper.py` | 338 | Playwright browser scraping (alternative to yt-dlp, anti-detection) |

The caption extractor uses `gpt-4.1` (not `gpt-4o` — was changed after model discontinuation). The prompt instructs the model to ignore TikTok UI elements and only extract the burned-in caption overlay text.

## Frontend

### Tech Stack

- React 19, TypeScript, Vite 7.3
- Tailwind CSS v4.2 (via `@tailwindcss/vite` plugin)
- React Router DOM v7 (for URL management only — NOT for mount/unmount)
- Zustand v5 (global state)
- lightningcss (CSS transformer — converts oklch colors to rgb for browser compatibility)
- Vitest + Testing Library (unit tests)

### Key Architecture Decisions

**CSS-based tab switching (NOT React Router routes):** All 4 page components are rendered simultaneously in `App.tsx`. Active tab shown with `display: block`, others hidden with `display: none`. This keeps all components mounted at all times — WebSocket connections stay alive, form state persists, running jobs remain visible when switching tabs. React Router is only used for URL updates and the `useLocation` hook.

```tsx
// App.tsx — all pages always mounted
<main>
  <div style={{ display: pathname === '/' ? 'block' : 'none' }}><ProjectsPage /></div>
  <div style={{ display: pathname === '/generate' ? 'block' : 'none' }}><GeneratePage /></div>
  <div style={{ display: pathname === '/captions' ? 'block' : 'none' }}><CaptionsPage /></div>
  <div style={{ display: pathname === '/burn' ? 'block' : 'none' }}><BurnPage /></div>
</main>
```

**Zustand store for cross-tab state:** `workflowStore.ts` holds active project, notifications, job tracking counts, generate page jobs, and burn selection drafts. Active project is persisted to localStorage.

**WebSocket hook:** `useWebSocket.ts` handles connection lifecycle, auto-reconnect with exponential backoff, message queuing, and a `shouldReconnect` callback to prevent reconnect loops after pipeline completion.

### File Map

| File | Lines | Purpose |
|------|-------|---------|
| `App.tsx` | 310 | Shell — header, project selector, tab nav, CSS tab switching, health banner, toasts |
| `pages/Generate.tsx` | 644 | Video generation — provider select, prompt input, form controls, job cards with status polling, video preview/download, prompt history sidebar |
| `pages/Captions.tsx` | 837 | Caption scraping — TikTok username input, WebSocket pipeline, real-time log, frame thumbnails, results table with CSV export |
| `pages/Burn.tsx` | 1261 | Caption burning — video browser, caption source selector, html2canvas overlay rendering, color correction sliders, burn pipeline via WebSocket, batch management |
| `pages/Projects.tsx` | 316 | Project list — stats cards, create/delete, project grid |
| `stores/workflowStore.ts` | 218 | Zustand global state — active project, notifications, job counts, generate jobs, burn selections |
| `hooks/useWebSocket.ts` | 208 | WebSocket with reconnect, message queue, start payload memory, shouldReconnect guard |
| `types/api.ts` | 242 | TypeScript interfaces for all API requests/responses/WebSocket events |
| `components/` | 11 files | ConfirmModal, EmptyState, ErrorBoundary, FileBrowser, ProgressBar, ProjectSelector, StatusChip, TabNav, Toast, ToastContainer |

### Generate Page Details

- Fetches `/api/video/providers` on mount to populate provider dropdown
- Form fields: prompt (textarea), provider, count (1-50), duration (5/10s), aspect ratio (16:9/9:16/1:1), resolution (480p/720p/1080p)
- Submits via `POST /api/video/generate` (multipart form, supports optional image upload)
- Jobs tracked in zustand `generateJobs` array — survives tab switches
- Module-level `activePolls` Set drives polling loops (`GET /api/video/jobs/{id}`) — survives component re-renders
- Each job card shows per-video status chips, progress count, video preview (inline `<video>` tag), individual + batch download
- Prompt history in collapsible left sidebar — fetched from `/api/video/prompts`, click to fill form

### Captions Page Details

- Input: TikTok username (with or without `@` prefix)
- Controls: max videos (1-100), sort order (latest/popular)
- WebSocket connects to `/api/captions/ws/{jobId}` on scrape start
- Real-time log panel shows pipeline events as they stream
- Frame thumbnails displayed as base64 images from `frame_ready` events
- Results table shows video ID, extracted caption text, status
- CSV export via `/api/captions/export/{username}`
- `shouldReconnect` guard prevents reconnect loop after `all_complete`

### Burn Page Details

- Left panel: video browser (tree view of project videos), caption source selector (from scraped CSVs)
- Center: pairing interface — maps videos to captions with drag/reorder
- Right panel: overlay preview with html2canvas rendering, color correction sliders (brightness, contrast, saturation, sharpness, shadow, temperature, tint, fade)
- Font selector from `/api/burn/fonts`
- Burns via WebSocket `/api/burn/ws` — sends pairs with base64 PNG overlays
- Batch management: list previous burns, download ZIPs, delete batches
- Sequential processing (one video at a time) due to ffmpeg resource constraints

## Directory Layout

```
content-posting-lab/
├── app.py                     # Unified FastAPI entry point (port 8000)
├── project_manager.py         # Project CRUD, path utils, sanitization
├── routers/
│   ├── video.py               # Video generation router
│   ├── captions.py            # Caption scraping router (WebSocket)
│   ├── burn.py                # Caption burning router (WebSocket)
│   └── projects.py            # Project management router
├── providers/
│   ├── base.py                # generate_one() universal entry, ffmpeg crop
│   ├── grok.py                # xAI Grok provider
│   ├── fal.py                 # FAL providers (Wan, Kling, Ovi)
│   ├── replicate.py           # Replicate providers (MiniMax, Wan, Kling)
│   ├── luma.py                # Luma Dream Machine provider
│   └── sora.py                # OpenAI Sora 2 provider
├── scraper/
│   ├── frame_extractor.py     # yt-dlp listing + thumbnail download
│   ├── caption_extractor.py   # GPT-4.1 vision OCR
│   ├── ocr_extractor.py       # Tesseract OCR fallback
│   └── tiktok_scraper.py      # Playwright browser scraping (alternative)
├── frontend/
│   ├── src/
│   │   ├── App.tsx            # Shell + CSS tab switching
│   │   ├── pages/
│   │   │   ├── Generate.tsx   # Video generation UI (644 lines)
│   │   │   ├── Captions.tsx   # Caption scraping UI (837 lines)
│   │   │   ├── Burn.tsx       # Caption burning UI (1261 lines)
│   │   │   └── Projects.tsx   # Project management UI (316 lines)
│   │   ├── stores/
│   │   │   └── workflowStore.ts  # Zustand global state
│   │   ├── hooks/
│   │   │   ├── useWebSocket.ts   # WebSocket with reconnect
│   │   │   └── useProject.ts     # Project hook
│   │   ├── types/api.ts       # All TypeScript API contracts
│   │   └── components/        # 11 shared UI components
│   ├── package.json           # React 19, Tailwind v4, Vite 7.3, Zustand 5
│   └── vite.config.ts         # lightningcss, dev proxy to :8000
├── tests/                     # Backend tests (11 tests)
├── fonts/                     # TikTokSans + Montserrat
├── projects/                  # Per-project data (videos, captions, burned)
├── static/                    # Legacy UIs (reference only, do not modify)
├── .env                       # API keys (gitignored)
└── requirements.txt           # Python deps
```

## Testing

```bash
# Frontend (18 tests)
cd frontend && npm test

# Backend (11 tests)
python -m pytest tests/ -v
```

**Frontend tests:** `App.test.tsx` (3), `Generate.test.tsx` (3), `Captions.test.tsx` (5), `Burn.test.tsx` (4), `Projects.test.tsx` (3)

**Backend tests:** `test_smoke.py` (2), `test_projects_api.py` (3), `test_video_api.py` (2), `test_burn_and_captions_api.py` (2), `e2e/test_smoke.py` (2)

All 29 tests pass. Build (`npm run build`) is clean.

## Prerequisites

```bash
pip install -r requirements.txt
brew install ffmpeg tesseract    # system deps (macOS)
```

**System dependencies:**
- `ffmpeg` + `ffprobe` on PATH (video processing — all three workflows)
- `yt-dlp` on PATH (TikTok video listing and thumbnail download)
- `tesseract` on PATH (optional OCR fallback)

**API keys in `.env`:**
- `XAI_API_KEY` — Grok video generation
- `FAL_KEY` — FAL providers (Wan, Kling, Ovi)
- `LUMA_API_KEY` — Luma Dream Machine
- `REPLICATE_API_TOKEN` — Replicate providers (MiniMax, Wan, Kling)
- `OPENAI_API_KEY` — Sora 2 video generation AND GPT-4.1 caption OCR

## Legacy Files (DO NOT MODIFY)

The old 3-server system files are still in the repo for reference:
- `server.py` — original video generation server (port 8000)
- `caption_server.py` — original caption scraper (port 8001)
- `burn_server.py` — original caption burn server (port 8002)
- `static/index.html` — legacy generate UI
- `static/captions/index.html` — legacy captions UI
- `static/burn/index.html` — legacy burn UI

These are the authoritative reference for feature parity. If the new app behaves differently from these files, the new app has a bug.

## Conventions and Gotchas

- **All async** — FastAPI throughout, no sync blocking.
- **In-memory job state** — restart loses job tracking (files on disk survive).
- **No inter-server HTTP** — burn router reads video/caption directories directly via filesystem.
- **WebSocket for real-time** — captions and burn use WebSocket streaming. Video generation uses HTTP polling.
- **Project-scoped everything** — all endpoints accept `?project=` query param. Frontend sends active project name with every request.
- **Font:** `fonts/TikTokSans16pt-Bold.ttf` is the default burn font. White text with black stroke.
- **Color correction** in burn uses ffmpeg `eq` and `colorbalance` filters — the sliders in the UI map directly to ffmpeg filter params.
- **Tailwind v4 uses oklch colors** — lightningcss in vite.config.ts converts these to rgb at build time for browser compatibility.
- **Tab switching preserves all state** — components never unmount. CSS display toggling, not React Router mount/unmount.

## Dev Workflow

1. **Generate videos** — Generate tab. Pick provider, write prompt, set params, generate. Videos saved to `projects/{name}/videos/`.
2. **Scrape captions** — Captions tab. Enter TikTok username, scrape and extract captions via GPT-4.1 vision. CSVs saved to `projects/{name}/captions/`.
3. **Burn captions onto videos** — Burn tab. Pair videos with captions, customize overlay, burn. Output in `projects/{name}/burned/`.
4. Result: final captioned videos ready to post.
