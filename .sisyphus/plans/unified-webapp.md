# Unified Content Posting Lab

## TL;DR

> **Quick Summary**: Merge 3 separate FastAPI servers (video gen, caption scrape, caption burn) into a single unified FastAPI app with a React + TypeScript tabbed frontend. Add a project/campaign concept to group workflow artifacts and prevent stale video reuse.
> 
> **Deliverables**:
> - Single `app.py` FastAPI entry point replacing `server.py`, `caption_server.py`, `burn_server.py`
> - React + TypeScript frontend with 4-tab workflow (Projects → Generate → Captions → Burn)
> - Project-scoped directory system (`projects/{name}/videos/`, `captions/`, `burned/`)
> - Agent-friendly API (structured JSON, filesystem access via `projects/`)
> - Full test suite (pytest API + vitest components + Playwright E2E)
> - Single startup: `make dev` (development) or `python app.py` (usage)
> 
> **Estimated Effort**: Large
> **Parallel Execution**: YES — 6 waves
> **Critical Path**: Skeleton → Video Router + Project Model → App Shell → Cross-tab Integration → E2E Test → Verification

---

## Context

### Original Request
User runs 3 separate FastAPI servers (ports 8000, 8001, 8002) as a sequential TikTok content pipeline: generate AI videos → scrape captions from TikTok profiles → burn captions onto generated videos. They want a single unified webapp with one startup command, a modern React frontend, and a project/campaign concept to group artifacts and avoid reusing stale content across campaigns.

### Interview Summary
**Key Discussions**:
- **Backend language**: Confirmed Python/FastAPI — user initially considered Bun/Node but agreed the existing Python backend logic (6 provider integrations, ffmpeg pipelines, GPT-4o OCR) is too valuable to rewrite
- **Frontend stack**: Vite + React + TypeScript — user wants modern DX (HMR, TypeScript, components) and a presentable UI
- **Project concept**: Directory-based (`projects/{name}/`) to solve the #1 pain point of accidentally reusing stale videos
- **Tab structure**: 4 tabs — Projects dashboard → Generate → Captions → Burn, with project selection scoping all downstream tabs
- **Agent interactivity**: Both filesystem (`ls projects/campaign/`) and API endpoints — Claude in Cursor terminal can interact with content
- **Legacy compat**: Keep `/ws/burn` WebSocket endpoint
- **Tests**: Full suite — API + component + E2E

**Research Findings**:
- All 3 servers combined expose 13 endpoints (11 REST + 2 WebSocket)
- Current static mounts conflict (all 3 apps mount `/` independently)
- Burn UI already uses REST POST for burning (legacy WS endpoint unused by current UI)
- `html2canvas` CDN used in burn UI for overlay PNG generation — must survive React migration
- `sort` param in caption server is accepted but ignored by yt-dlp backend
- Video gen server uses polling (no WS), caption server uses WS, burn server uses REST POST

### Metis Review
**Identified Gaps** (addressed):
- **Data migration**: Existing `output/`, `caption_output/`, `burn_output/` dirs will remain read-only. New work goes into `projects/`. A "legacy" project auto-imports old data on first run.
- **Project lifecycle**: Explicit "New Project" action. Projects can be deleted (with confirmation). Filesystem-safe name validation. Default "Quick Test" project available.
- **Cross-project isolation**: Burn tab only shows videos/captions from selected project. No cross-project references in V1.
- **CORS for dev**: FastAPI CORS middleware configured for `http://localhost:5173` during development.
- **Startup validation**: Health check for ffmpeg, yt-dlp, API keys on server boot. UI shows clear diagnostic if deps missing.
- **WebSocket reconnection**: React hooks with auto-reconnect logic and job ID resume.
- **Concurrent jobs**: Keep current behavior (no explicit limits). Document in UI that parallel jobs may compete for resources.

---

## Work Objectives

### Core Objective
Unify 3 independent FastAPI servers into one process with a professional React + TypeScript tabbed UI and project-scoped artifact management, enabling a streamlined single-tab workflow for TikTok content creation.

### Concrete Deliverables
- `app.py` — unified FastAPI entry point with 4 routers
- `routers/video.py`, `routers/captions.py`, `routers/burn.py`, `routers/projects.py`
- `providers/` — extracted video generation provider modules
- `frontend/` — Vite + React + TypeScript app (4 tabs, shared state, WebSocket support)
- `projects/` — project-scoped output directories
- `tests/` — pytest API tests, vitest component tests, Playwright E2E tests
- `Makefile` — `make dev`, `make build`, `make install`, `make test`

### Definition of Done
- [ ] `make dev` starts unified app accessible at `http://localhost:5173` (dev) or `http://localhost:8000` (built)
- [ ] Full workflow completes: create project → generate video → scrape captions → burn → output in `projects/{name}/burned/`
- [ ] All 13 original endpoints work under unified routing
- [ ] All tests pass: `make test` exits 0
- [ ] UI is presentable — clean design, clear labels, error states, progress indicators

### Must Have
- All 6 video generation providers working (Grok, FAL, Luma, Replicate, Sora, and all sub-models)
- WebSocket progress for caption scraping
- Project-scoped isolation (Burn tab ONLY shows current project's content)
- Single startup command for both dev and usage modes
- Agent-friendly JSON API responses on all endpoints
- Startup health check (ffmpeg, yt-dlp on PATH; API key diagnostics)

### Must NOT Have (Guardrails)
- **NO database** — filesystem + in-memory only. No SQLite, no Postgres.
- **NO authentication** — single-user local tool. No login, no sessions, no JWT.
- **NO new video providers** — migrate existing 6, don't add new ones.
- **NO backend logic rewrites** — reorganize into routers, don't rewrite provider integrations or ffmpeg pipelines.
- **NO mobile responsive design** — desktop-first, Cursor internal browser is the target.
- **NO component library (shadcn/Radix/Chakra)** — use Tailwind CSS directly for styling. Keep dependencies minimal.
- **NO real-time collaboration** — no WebRTC, no CRDT, no multi-user state sync.
- **NO video editing features** — no trim, crop, filters beyond existing burn overlay.
- **NO caption editing UI** — captions come from CSV. Edit externally if needed.
- **NO CLI tool** — API is already curl-able. Don't build a dedicated CLI.

---

## Verification Strategy

> **ZERO HUMAN INTERVENTION** — ALL verification is agent-executed. No exceptions.

### Test Decision
- **Infrastructure exists**: NO (new project)
- **Automated tests**: YES (Tests-after — build first, test after each wave)
- **Frameworks**: pytest (API), vitest + React Testing Library (components), Playwright (E2E)
- **External API mocking**: Mock all provider APIs in tests (Grok, FAL, Luma, Replicate, Sora, OpenAI). No real API calls in tests.

### QA Policy
Every task MUST include agent-executed QA scenarios.
Evidence saved to `.sisyphus/evidence/task-{N}-{scenario-slug}.{ext}`.

| Deliverable Type | Verification Tool | Method |
|------------------|-------------------|--------|
| FastAPI endpoints | Bash (curl/httpx) | Send requests, assert status + response JSON |
| WebSocket flows | Bash (websocat) or Python test | Connect, send messages, assert event sequence |
| React components | vitest + Testing Library | Render, interact, assert DOM |
| Full workflow | Playwright | Navigate tabs, fill forms, assert progress, capture screenshots |
| File system outputs | Bash (ls/cat) | Verify files exist at expected paths with expected content |

---

## Execution Strategy

### Target File Structure

```
content-posting-lab/
├── app.py                     # Unified FastAPI entry point
├── routers/
│   ├── __init__.py
│   ├── video.py               # Video gen routes (from server.py)
│   ├── captions.py            # Caption scrape routes (from caption_server.py)
│   ├── burn.py                # Burn routes (from burn_server.py)
│   └── projects.py            # Project CRUD (NEW)
├── providers/                 # Extracted from server.py
│   ├── __init__.py
│   ├── base.py                # Shared: download, crop, slugify
│   ├── grok.py
│   ├── fal.py
│   ├── luma.py
│   ├── replicate.py
│   └── sora.py
├── frontend/                  # Vite + React + TypeScript
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── index.html
│   ├── src/
│   │   ├── main.tsx
│   │   ├── App.tsx
│   │   ├── types/
│   │   │   └── api.ts         # All API contract types
│   │   ├── hooks/
│   │   │   ├── useWebSocket.ts
│   │   │   ├── usePolling.ts
│   │   │   └── useProject.ts
│   │   ├── stores/
│   │   │   └── workflowStore.ts
│   │   ├── components/
│   │   │   ├── TabShell.tsx
│   │   │   ├── ProjectSelector.tsx
│   │   │   ├── ProgressBar.tsx
│   │   │   ├── Toast.tsx
│   │   │   ├── FileBrowser.tsx
│   │   │   └── ErrorBoundary.tsx
│   │   └── pages/
│   │       ├── Projects.tsx
│   │       ├── Generate.tsx
│   │       ├── Captions.tsx
│   │       └── Burn.tsx
│   └── dist/                  # Build output (gitignored)
├── scraper/                   # UNCHANGED
├── projects/                  # Project data directories (gitignored)
├── output/                    # Legacy — read-only after migration
├── caption_output/            # Legacy — read-only after migration
├── burn_output/               # Legacy — read-only after migration
├── fonts/                     # UNCHANGED
├── static/                    # Legacy UIs kept for backward compat
├── tests/
│   ├── conftest.py
│   ├── test_video_api.py
│   ├── test_captions_api.py
│   ├── test_burn_api.py
│   ├── test_projects_api.py
│   └── e2e/
│       └── test_full_workflow.py
├── Makefile
├── .env
└── requirements.txt
```

### Parallel Execution Waves

```
Wave 1 (Foundation — 6 parallel tasks):
├── Task 1: Unified FastAPI app skeleton + Makefile [quick]
├── Task 2: Vite + React + TypeScript scaffold [quick]
├── Task 3: TypeScript API type definitions [quick]
├── Task 4: Project directory model + Python path utilities [quick]
├── Task 5: Test infrastructure setup (pytest + vitest + playwright configs) [quick]
└── Task 6: Extract video gen providers into modules [unspecified-high]

Wave 2 (Backend Migration — 6 parallel tasks):
├── Task 7: Video generation router (depends: 1, 4, 6) [unspecified-high]
├── Task 8: Caption scraping router (depends: 1, 4) [unspecified-high]
├── Task 9: Burn router (depends: 1, 4) [unspecified-high]
├── Task 10: Project CRUD API (depends: 1, 4) [unspecified-high]
├── Task 11: Unified app wiring — CORS, mounts, health, startup (depends: 1, 7, 8, 9, 10) [deep]
└── Task 12: Legacy compatibility + data migration shim (depends: 4, 11) [unspecified-high]

Wave 3 (Frontend Build — 6 parallel tasks):
├── Task 13: App shell — routing, tab nav, project context (depends: 2, 3) [visual-engineering]
├── Task 14: Projects tab — dashboard, CRUD, project list (depends: 13) [visual-engineering]
├── Task 15: Generate tab — prompt form, provider picker, job progress (depends: 13) [visual-engineering]
├── Task 16: Captions tab — profile input, WS progress, results table (depends: 13) [visual-engineering]
├── Task 17: Burn tab — video/caption picker, pairing, overlay, burn progress (depends: 13) [visual-engineering]
└── Task 18: Shared UI components — progress, toasts, file browsers, errors (depends: 2) [visual-engineering]

Wave 4 (Integration & Polish — 5 parallel tasks):
├── Task 19: Cross-tab artifact flow + notifications (depends: 14-17) [deep]
├── Task 20: UI design polish — professional appearance (depends: 14-17, 18) [visual-engineering]
├── Task 21: Error handling + startup validation UI (depends: 11, 13) [unspecified-high]
├── Task 22: Dev scripts + build pipeline (depends: 1, 2, 11) [quick]
└── Task 23: WebSocket reconnection + resilience (depends: 16, 17) [unspecified-high]

Wave 5 (Testing — 3 parallel tasks):
├── Task 24: API tests — all endpoints via pytest (depends: 5, 11) [unspecified-high]
├── Task 25: Frontend component tests — all tabs via vitest (depends: 5, 14-17) [unspecified-high]
└── Task 26: E2E workflow test — Playwright (depends: 5, 19) [deep]

Wave FINAL (Verification — 4 parallel tasks):
├── Task F1: Plan compliance audit (oracle)
├── Task F2: Code quality review (unspecified-high)
├── Task F3: Real manual QA (unspecified-high + playwright skill)
└── Task F4: Scope fidelity check (deep)

Critical Path: Task 1 → Task 7 → Task 11 → Task 13 → Task 15 → Task 19 → Task 26 → F1-F4
Parallel Speedup: ~65% faster than sequential
Max Concurrent: 6 (Waves 1, 2, 3)
```

### Dependency Matrix

| Task | Depends On | Blocks | Wave |
|------|------------|--------|------|
| 1 | — | 7, 8, 9, 10, 11, 22 | 1 |
| 2 | — | 13, 18, 22 | 1 |
| 3 | — | 13, 14-17 | 1 |
| 4 | — | 7, 8, 9, 10, 12 | 1 |
| 5 | — | 24, 25, 26 | 1 |
| 6 | — | 7 | 1 |
| 7 | 1, 4, 6 | 11 | 2 |
| 8 | 1, 4 | 11 | 2 |
| 9 | 1, 4 | 11 | 2 |
| 10 | 1, 4 | 11, 14 | 2 |
| 11 | 1, 7, 8, 9, 10 | 12, 21, 22, 24 | 2 |
| 12 | 4, 11 | — | 2 |
| 13 | 2, 3 | 14, 15, 16, 17, 21 | 3 |
| 14 | 13, 10 | 19, 20, 25 | 3 |
| 15 | 13 | 19, 20, 25 | 3 |
| 16 | 13 | 19, 20, 23, 25 | 3 |
| 17 | 13 | 19, 20, 23, 25 | 3 |
| 18 | 2 | 20 | 3 |
| 19 | 14-17 | 26 | 4 |
| 20 | 14-17, 18 | — | 4 |
| 21 | 11, 13 | — | 4 |
| 22 | 1, 2, 11 | — | 4 |
| 23 | 16, 17 | — | 4 |
| 24 | 5, 11 | — | 5 |
| 25 | 5, 14-17 | — | 5 |
| 26 | 5, 19 | F1-F4 | 5 |

### Agent Dispatch Summary

| Wave | # Parallel | Tasks → Agent Category |
|------|------------|----------------------|
| 1 | **6** | T1-T5 → `quick`, T6 → `unspecified-high` |
| 2 | **6** | T7-T10 → `unspecified-high`, T11 → `deep`, T12 → `unspecified-high` |
| 3 | **6** | T13-T18 → `visual-engineering` |
| 4 | **5** | T19 → `deep`, T20 → `visual-engineering`, T21, T23 → `unspecified-high`, T22 → `quick` |
| 5 | **3** | T24, T25 → `unspecified-high`, T26 → `deep` |
| FINAL | **4** | F1 → `oracle`, F2-F3 → `unspecified-high`, F4 → `deep` |

---

## TODOs

### Wave 1 — Foundation

- [x] 1. Unified FastAPI App Skeleton + Makefile

  **What to do**:
  - Create `app.py` as the unified FastAPI entry point with lifespan handler
  - Create `routers/__init__.py` and empty router files (`video.py`, `captions.py`, `burn.py`, `projects.py`) each with an `APIRouter` and appropriate prefix
  - Create `providers/__init__.py` and `providers/base.py` with shared utilities placeholder
  - Create `Makefile` with targets: `dev`, `build`, `install`, `test`, `clean`
  - `make dev` should use `concurrently` (or Python subprocess) to launch both `uvicorn app:app --reload --port 8000` and `cd frontend && npm run dev`
  - `make install` should `pip install -r requirements.txt && cd frontend && npm install`
  - `app.py` should include router registration, CORS middleware (allow `localhost:5173`), and a `GET /api/health` endpoint that checks: ffmpeg on PATH, yt-dlp on PATH, which API keys are configured in `.env`
  - Add `GET /` catch-all that serves `frontend/dist/index.html` if it exists (production mode), else returns 404 with helpful message

  **Must NOT do**:
  - Do NOT copy any business logic yet — routers are empty shells with just the APIRouter defined
  - Do NOT add authentication middleware
  - Do NOT install a database

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: []
    - Pure Python scaffolding, no specialized domain knowledge needed

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Tasks 2-6)
  - **Blocks**: Tasks 7, 8, 9, 10, 11, 22
  - **Blocked By**: None

  **References**:
  - `server.py:1-40` — Current imports and app setup pattern (FastAPI, uvicorn, dotenv, CORS)
  - `server.py:644-650` — Current static mount pattern to replicate for unified app
  - `caption_server.py:1-20` — Caption server imports showing WebSocket usage pattern
  - `burn_server.py:1-30` — Burn server imports showing Pillow, ffmpeg, font dependencies
  - `burn_server.py:636-660` — Multiple static mount points to consolidate

  **Acceptance Criteria**:
  - [ ] `python -c "from app import app; print(app.title)"` prints app title
  - [ ] `python -c "from routers.video import router"` imports without error
  - [ ] `python -c "from routers.projects import router"` imports without error
  - [ ] `make install` completes without errors (after frontend scaffold exists)

  **QA Scenarios**:
  ```
  Scenario: Health endpoint returns system status
    Tool: Bash (curl)
    Preconditions: app.py exists, ffmpeg and yt-dlp on PATH
    Steps:
      1. Start server: `uvicorn app:app --port 8000 &`
      2. Wait 2s for startup
      3. `curl -s http://localhost:8000/api/health | python -m json.tool`
      4. Assert response contains: {"status": "ok", "ffmpeg": true, "ytdlp": true}
      5. Kill server
    Expected Result: JSON with status "ok" and dependency checks
    Failure Indicators: Connection refused, missing keys in response, ffmpeg/ytdlp false
    Evidence: .sisyphus/evidence/task-1-health-endpoint.json

  Scenario: Production fallback serves React bundle or helpful 404
    Tool: Bash (curl)
    Preconditions: app.py running, frontend/dist/ does NOT exist yet
    Steps:
      1. Start server: `uvicorn app:app --port 8000 &`
      2. `curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/`
      3. Assert: HTTP 404 (no built frontend yet)
      4. Kill server
    Expected Result: 404 when no built frontend, 200 when frontend/dist/index.html exists
    Failure Indicators: 500 error, stack trace in response
    Evidence: .sisyphus/evidence/task-1-production-fallback.txt
  ```

  **Commit**: YES (groups with Wave 1)
  - Message: `feat(foundation): scaffold unified FastAPI app with routers, health check, Makefile`
  - Files: `app.py`, `routers/*.py`, `providers/__init__.py`, `providers/base.py`, `Makefile`

- [x] 2. Vite + React + TypeScript Scaffold

  **What to do**:
  - Run `npm create vite@latest frontend -- --template react-ts` to scaffold
  - Configure `vite.config.ts` with proxy: `/api` → `http://localhost:8000`, `/ws` → `ws://localhost:8000` (WebSocket proxy)
  - Install Tailwind CSS v4 via `npm install tailwindcss @tailwindcss/vite`
  - Configure Tailwind in vite config plugin array and add `@import "tailwindcss"` to `src/index.css`
  - Set up basic `App.tsx` with placeholder routing (react-router-dom) and 4 tab routes: `/`, `/generate`, `/captions`, `/burn`
  - Add `.gitignore` entry for `frontend/node_modules/` and `frontend/dist/`
  - Verify `npm run dev` starts on port 5173 and `npm run build` outputs to `frontend/dist/`

  **Must NOT do**:
  - Do NOT install shadcn, Radix, Chakra, or any component library
  - Do NOT build actual page content — just route placeholders
  - Do NOT add state management yet

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: [`frontend-ui-ux`]
    - `frontend-ui-ux`: Vite + React scaffold with Tailwind is core frontend setup

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Tasks 1, 3-6)
  - **Blocks**: Tasks 13, 18, 22
  - **Blocked By**: None

  **References**:
  - `static/index.html:1-30` — Current video gen UI structure to understand what React will replace
  - `static/burn/index.html:1-10` — CDN imports (html2canvas) that need React equivalents
  - Vite docs: https://vite.dev/guide/ — Project scaffolding
  - Vite proxy docs: https://vite.dev/config/server-options.html#server-proxy — API proxy config

  **Acceptance Criteria**:
  - [ ] `cd frontend && npm run dev` starts Vite on port 5173
  - [ ] `cd frontend && npm run build` produces `frontend/dist/index.html`
  - [ ] Navigating to `http://localhost:5173/` shows placeholder app with 4 tab links
  - [ ] Tailwind classes work (e.g., `className="bg-gray-900 text-white"` renders correctly)

  **QA Scenarios**:
  ```
  Scenario: Vite dev server starts with API proxy configured
    Tool: Bash
    Preconditions: frontend/ scaffold exists, npm install completed
    Steps:
      1. `cd frontend && npm run dev &`
      2. Wait 3s
      3. `curl -s http://localhost:5173/` — assert contains `<div id="root">`
      4. Kill Vite
    Expected Result: Vite serves React app, page contains root div
    Failure Indicators: Connection refused, missing root div, build errors in terminal
    Evidence: .sisyphus/evidence/task-2-vite-dev.txt

  Scenario: Production build generates static files
    Tool: Bash
    Preconditions: frontend/ scaffold exists
    Steps:
      1. `cd frontend && npm run build`
      2. `ls frontend/dist/index.html` — assert file exists
      3. `ls frontend/dist/assets/` — assert JS and CSS files present
    Expected Result: dist/ contains index.html + hashed asset files
    Failure Indicators: Build fails, dist/ empty, missing assets
    Evidence: .sisyphus/evidence/task-2-vite-build.txt
  ```

  **Commit**: YES (groups with Wave 1)
  - Message: `feat(frontend): scaffold Vite + React + TypeScript with Tailwind and routing`
  - Files: `frontend/`

- [x] 3. TypeScript API Type Definitions

  **What to do**:
  - Create `frontend/src/types/api.ts` with TypeScript interfaces for ALL API contracts across all 3 routers
  - Video API types: `Provider`, `GenerateRequest`, `GenerateResponse`, `Job`, `VideoEntry` (match shapes from `server.py:553-630`)
  - Caption API types: `CaptionWSMessage` (all event types: `status`, `urls_collected`, `downloading`, `frame_ready`, `ocr_done`, `all_complete`, `error`), `CaptionResult`, `ExportResponse`
  - Burn API types: `VideoFile`, `CaptionSource`, `CaptionRow`, `BurnRequest`, `BurnResponse`, `BurnBatch`, `FontInfo`
  - Project API types: `Project`, `CreateProjectRequest`, `ProjectListResponse`
  - Health API types: `HealthResponse` (status, ffmpeg, ytdlp, providers)
  - WebSocket message types: discriminated unions using `event` field for type-safe message handling

  **Must NOT do**:
  - Do NOT use `any` types — every field must be properly typed
  - Do NOT add runtime validation (Zod, etc.) — types only for now

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: []
    - Pure TypeScript type definitions, no runtime code

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Tasks 1, 2, 4-6)
  - **Blocks**: Tasks 13, 14, 15, 16, 17
  - **Blocked By**: None

  **References**:
  - `server.py:553-560` — Provider shape: `{id, name, key_id, pricing, models}`
  - `server.py:563-608` — Generate endpoint params and job shape: `{id, prompt, provider, count, videos:[...]}`
  - `server.py:610-630` — Job response and download endpoint shapes
  - `caption_server.py:22-160` — ALL WebSocket event shapes (status, urls_collected, downloading, frame_ready, ocr_done, all_complete, error)
  - `burn_server.py:464-560` — Videos, captions, fonts, burn-overlay, batches response shapes
  - `burn_server.py:561-600` — Legacy WS burn event shapes (burning, burned, complete, error)

  **Acceptance Criteria**:
  - [ ] `cd frontend && npx tsc --noEmit` passes with zero errors
  - [ ] All 13 endpoint request/response shapes have corresponding TypeScript interfaces
  - [ ] WebSocket event types use discriminated unions (e.g., `type CaptionEvent = StatusEvent | UrlsCollectedEvent | ...`)

  **QA Scenarios**:
  ```
  Scenario: TypeScript types compile without errors
    Tool: Bash
    Preconditions: frontend/ exists with types/api.ts
    Steps:
      1. `cd frontend && npx tsc --noEmit`
      2. Assert exit code 0
      3. `grep -c "interface\|type" frontend/src/types/api.ts` — assert >= 15 type definitions
    Expected Result: Zero TS errors, at least 15 type/interface definitions
    Failure Indicators: TS compilation errors, missing types for endpoints
    Evidence: .sisyphus/evidence/task-3-tsc-check.txt
  ```

  **Commit**: YES (groups with Wave 1)
  - Message: `feat(types): define TypeScript API contracts for all endpoints and WebSocket events`
  - Files: `frontend/src/types/api.ts`

- [x] 4. Project Directory Model + Python Path Utilities

  **What to do**:
  - Create `project_manager.py` (top-level module) with:
    - `PROJECTS_DIR = BASE_DIR / "projects"` constant
    - `create_project(name: str) -> Path` — validates name (filesystem-safe: alphanumeric, hyphens, underscores only, max 100 chars), creates `projects/{name}/videos/`, `projects/{name}/captions/`, `projects/{name}/burned/`, returns project path
    - `list_projects() -> list[dict]` — returns `[{name, created, video_count, caption_count, burned_count}]` by scanning `projects/`
    - `get_project(name: str) -> dict | None` — returns single project info or None
    - `delete_project(name: str) -> bool` — removes directory tree (with safety check: name must exist under `projects/`)
    - `get_project_video_dir(name: str) -> Path` — returns `projects/{name}/videos/`
    - `get_project_caption_dir(name: str) -> Path` — returns `projects/{name}/captions/`
    - `get_project_burn_dir(name: str) -> Path` — returns `projects/{name}/burned/`
    - `sanitize_project_name(name: str) -> str` — strips unsafe chars, lowercases, replaces spaces with hyphens
  - Create `projects/` directory and add to `.gitignore`
  - Ensure a "quick-test" default project is auto-created on first startup if no projects exist

  **Must NOT do**:
  - Do NOT add database persistence — filesystem only
  - Do NOT allow `..`, `/`, or other path traversal in project names
  - Do NOT add project metadata files (JSON manifests) — derive all info from directory scanning

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: []
    - Pure Python file I/O, no external deps

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Tasks 1-3, 5-6)
  - **Blocks**: Tasks 7, 8, 9, 10, 12
  - **Blocked By**: None

  **References**:
  - `server.py:479-532` — Current output path logic: `output/{provider}/{prompt_slug}/{job_id}_{index}.mp4` — this is what we're replacing with project-scoped paths
  - `caption_server.py:56` — Current caption output: `caption_output/{username}/` — replacing with `projects/{name}/captions/{username}/`
  - `burn_server.py:22-24` — Current dir constants: `VIDEO_DIR`, `CAPTION_DIR`, `BURN_DIR` — replacing with project-scoped equivalents

  **Acceptance Criteria**:
  - [ ] `python -c "from project_manager import create_project; print(create_project('test-campaign'))"` creates directory and prints path
  - [ ] `python -c "from project_manager import list_projects; print(list_projects())"` returns list with test-campaign
  - [ ] `python -c "from project_manager import sanitize_project_name; print(sanitize_project_name('Drake Release!!!'))"` returns `"drake-release"`
  - [ ] Path traversal attempt `create_project("../../etc")` raises ValueError

  **QA Scenarios**:
  ```
  Scenario: Create and list projects
    Tool: Bash (python)
    Preconditions: project_manager.py exists
    Steps:
      1. `python -c "from project_manager import create_project, list_projects; create_project('test-one'); create_project('test-two'); print(list_projects())"`
      2. Assert output contains both project names
      3. `ls projects/test-one/` — assert contains: videos/ captions/ burned/
      4. `ls projects/test-two/` — assert contains: videos/ captions/ burned/
    Expected Result: Both projects created with correct subdirectory structure
    Failure Indicators: Missing subdirectories, import errors
    Evidence: .sisyphus/evidence/task-4-project-crud.txt

  Scenario: Path traversal is blocked
    Tool: Bash (python)
    Preconditions: project_manager.py exists
    Steps:
      1. `python -c "from project_manager import create_project; create_project('../../etc/passwd')" 2>&1`
      2. Assert output contains "ValueError" or similar error
      3. `ls projects/` — assert no suspicious directories created
    Expected Result: ValueError raised, no directory created outside projects/
    Failure Indicators: Directory created, no error raised
    Evidence: .sisyphus/evidence/task-4-path-traversal.txt
  ```

  **Commit**: YES (groups with Wave 1)
  - Message: `feat(projects): add project directory model with CRUD, path utilities, and name sanitization`
  - Files: `project_manager.py`

- [x] 5. Test Infrastructure Setup

  **What to do**:
  - **pytest**: Add `tests/conftest.py` with `httpx.AsyncClient` fixture for FastAPI test client, project fixture that creates/cleans temp project dir. Add `pytest.ini` or `pyproject.toml` section with `asyncio_mode = auto`.
  - **vitest**: In `frontend/`, install `vitest @testing-library/react @testing-library/jest-dom jsdom`. Add vitest config in `vite.config.ts` (test environment: jsdom). Create `frontend/src/test-utils.tsx` with custom render wrapper that includes providers (Router, etc.).
  - **Playwright**: Install `@playwright/test` in frontend/. Create `frontend/playwright.config.ts` pointing at `http://localhost:5173` with `webServer` config to auto-start both Vite and FastAPI. Create `tests/e2e/` dir.
  - Add `make test` target that runs: `pytest tests/ && cd frontend && npm test && npx playwright test`
  - Install `pytest-asyncio` and `httpx` as test deps in requirements.txt
  - Create one smoke test per framework to verify setup works:
    - `tests/test_smoke.py`: assert FastAPI app has routes
    - `frontend/src/App.test.tsx`: assert App renders without crash
    - `tests/e2e/test_smoke.py`: assert homepage loads (Playwright)

  **Must NOT do**:
  - Do NOT write actual feature tests — just verify the frameworks are wired correctly
  - Do NOT mock external APIs yet — that's for Task 24

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Tasks 1-4, 6)
  - **Blocks**: Tasks 24, 25, 26
  - **Blocked By**: None (but runs best after Task 1 and 2 exist for imports)

  **References**:
  - FastAPI testing docs: https://fastapi.tiangolo.com/tutorial/testing/
  - Vitest docs: https://vitest.dev/guide/
  - Playwright docs: https://playwright.dev/docs/intro

  **Acceptance Criteria**:
  - [ ] `pytest tests/test_smoke.py` passes
  - [ ] `cd frontend && npx vitest run src/App.test.tsx` passes
  - [ ] `make test` runs all 3 test frameworks sequentially and exits 0

  **QA Scenarios**:
  ```
  Scenario: All three test frameworks run successfully
    Tool: Bash
    Preconditions: All test configs exist, dependencies installed
    Steps:
      1. `pytest tests/test_smoke.py -v` — assert 1 passed
      2. `cd frontend && npx vitest run` — assert tests passed
      3. `make test` — assert exit code 0
    Expected Result: All smoke tests pass across 3 frameworks
    Failure Indicators: Import errors, config errors, framework not found
    Evidence: .sisyphus/evidence/task-5-test-infra.txt
  ```

  **Commit**: YES (groups with Wave 1)
  - Message: `test(infra): set up pytest, vitest, and Playwright with smoke tests`
  - Files: `tests/`, `frontend/src/App.test.tsx`, `frontend/playwright.config.ts`, `frontend/src/test-utils.tsx`

- [x] 6. Extract Video Generation Providers into Modules

  **What to do**:
  - Extract each provider's generate + poll + download logic from `server.py` into separate modules under `providers/`:
    - `providers/grok.py` — Grok/xAI: submit generation, poll `/v1/videos/{request_id}`, extract URL (from `server.py:44-93`)
    - `providers/fal.py` — FAL queue: submit, poll status, fetch result for Wan/Kling/Ovi models (from `server.py:95-160`)
    - `providers/luma.py` — Luma Dream Machine: create generation, poll state, read assets.video (from `server.py:162-212`)
    - `providers/replicate.py` — Replicate: create prediction, poll, parse output (from `server.py:213-283`)
    - `providers/sora.py` — OpenAI Sora: create video, poll status, stream content bytes (from `server.py:284-400`)
  - `providers/base.py` — Extract shared utilities from `server.py`:
    - `download_video(url, dest_path)` (from `server.py:401-430`)
    - `crop_to_vertical(input_path, output_path)` (from `server.py:431-466`)
    - `slugify(text)` (from `server.py:467-478`)
    - `generate_one(job, video_entry, provider, prompt, ...)` — the orchestrator that calls provider → download → crop → update entry (from `server.py:479-550`)
  - Each provider module should export a single async function: `async def generate(prompt, params, httpx_client) -> str` that returns the video URL or file path
  - `providers/__init__.py` should export a `PROVIDERS` dict mapping provider ID → module
  - Preserve ALL existing error handling, retry logic, and parameter mapping from `server.py`

  **Must NOT do**:
  - Do NOT change provider behavior — exact same API calls, same error handling
  - Do NOT add new providers
  - Do NOT abstract away provider differences into a generic interface that loses provider-specific capabilities

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: []
    - Careful extraction refactoring, needs to preserve complex async logic across 6 providers

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Tasks 1-5)
  - **Blocks**: Task 7
  - **Blocked By**: None

  **References**:
  - `server.py:44-93` — Grok provider: HTTP POST to `https://api.x.ai/v1/videos/generations`, poll loop, video URL extraction
  - `server.py:95-160` — FAL provider: queue submission to `queue.fal.run/{model}`, status polling, result fetching
  - `server.py:162-212` — Luma provider: generation creation, state polling until `completed`, asset URL
  - `server.py:213-283` — Replicate provider: prediction creation, output parsing (string vs list), URL extraction
  - `server.py:284-400` — Sora/OpenAI provider: video creation, status polling, content byte streaming to MP4
  - `server.py:401-466` — Shared utilities: `_download_video`, `_crop_to_vertical` (ffmpeg subprocess)
  - `server.py:467-550` — `_generate_one` orchestrator: provider dispatch → download → crop → update job entry
  - `server.py:553-560` — Provider configuration (API key checks, model lists, pricing info)

  **Acceptance Criteria**:
  - [ ] `python -c "from providers import PROVIDERS; print(list(PROVIDERS.keys()))"` lists all provider IDs
  - [ ] `python -c "from providers.base import download_video, crop_to_vertical, slugify"` imports without error
  - [ ] Each provider module has a single `async def generate(...)` entry point
  - [ ] No business logic remains in `server.py` after extraction — only the FastAPI app shell and route handlers reference providers

  **QA Scenarios**:
  ```
  Scenario: All provider modules import and expose generate function
    Tool: Bash (python)
    Preconditions: providers/ directory populated
    Steps:
      1. `python -c "from providers.grok import generate; print('grok ok')"`
      2. `python -c "from providers.fal import generate; print('fal ok')"`
      3. `python -c "from providers.luma import generate; print('luma ok')"`
      4. `python -c "from providers.replicate import generate; print('replicate ok')"`
      5. `python -c "from providers.sora import generate; print('sora ok')"`
      6. `python -c "from providers.base import download_video, crop_to_vertical, slugify; print('base ok')"`
      7. Assert all print "ok"
    Expected Result: All 6 modules import cleanly, expose expected functions
    Failure Indicators: ImportError, missing function, syntax errors
    Evidence: .sisyphus/evidence/task-6-provider-imports.txt

  Scenario: Provider registry maps all provider IDs
    Tool: Bash (python)
    Preconditions: providers/__init__.py exists
    Steps:
      1. `python -c "from providers import PROVIDERS; ids = sorted(PROVIDERS.keys()); print(ids)"`
      2. Assert output contains at least: grok, fal-wan, fal-kling, luma, replicate-minimax, sora (or equivalent IDs from server.py)
    Expected Result: All provider IDs from server.py are present in PROVIDERS dict
    Failure Indicators: Missing providers, wrong IDs
    Evidence: .sisyphus/evidence/task-6-provider-registry.txt
  ```

  **Commit**: YES (groups with Wave 1)
  - Message: `refactor(providers): extract video generation providers from server.py into modules`
  - Files: `providers/*.py`

### Wave 2 — Backend Migration

- [x] 7. Video Generation Router

  **What to do**:
  - Populate `routers/video.py` with all video generation endpoints migrated from `server.py`:
    - `GET /api/video/providers` — list configured providers (from `server.py:553`)
    - `POST /api/video/generate` — submit generation job. Accept `project` query param to scope output to `projects/{name}/videos/`. Use `project_manager.get_project_video_dir()` for output paths instead of flat `output/` (from `server.py:563`)
    - `GET /api/video/jobs` — list all jobs (from `server.py:617`)
    - `GET /api/video/jobs/{job_id}` — get single job (from `server.py:610`)
    - `GET /api/video/jobs/{job_id}/download-all` — ZIP download (from `server.py:622`)
  - Use provider modules from `providers/` (Task 6) instead of inline provider logic
  - In-memory `jobs: dict` stays in this router module (module-level variable)
  - Update `_generate_one` calls to write to project-scoped paths: `projects/{name}/videos/{provider}/{slug}/{job_id}_{idx}.mp4`
  - Preserve ALL existing behavior: multipart media upload, base64 encoding, count/duration clamping

  **Must NOT do**:
  - Do NOT change provider API call logic — just import from providers/
  - Do NOT add job persistence (still in-memory)
  - Do NOT change the job dict shape — clients expect the same JSON

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES (parallel with Tasks 8, 9, 10)
  - **Parallel Group**: Wave 2
  - **Blocks**: Task 11
  - **Blocked By**: Tasks 1, 4, 6

  **References**:
  - `server.py:553-650` — All video gen endpoints to migrate
  - `server.py:36` — In-memory jobs dict structure
  - `server.py:563-608` — Generate endpoint with multipart handling, provider dispatch
  - `server.py:479-550` — `_generate_one` orchestrator (now calls providers/ modules)
  - `project_manager.py` (Task 4) — `get_project_video_dir(name)`

  **Acceptance Criteria**:
  - [ ] `curl http://localhost:8000/api/video/providers` returns JSON array of providers
  - [ ] `curl -X POST http://localhost:8000/api/video/generate -F "prompt=test" -F "provider=grok" -F "project=test-campaign"` returns `{job_id, count}`
  - [ ] Generated video lands in `projects/test-campaign/videos/grok/test/{job_id}_0.mp4`

  **QA Scenarios**:
  ```
  Scenario: Providers endpoint returns configured providers
    Tool: Bash (curl)
    Preconditions: Unified server running on 8000, at least one API key in .env
    Steps:
      1. `curl -s http://localhost:8000/api/video/providers | python -m json.tool`
      2. Assert response is JSON array
      3. Assert each item has "id", "name" fields
    Expected Result: Array of provider objects with configured keys only
    Failure Indicators: Empty array when keys exist, 404, 500
    Evidence: .sisyphus/evidence/task-7-providers.json

  Scenario: Generate endpoint writes to project-scoped path
    Tool: Bash (curl + ls)
    Preconditions: Server running, "test-campaign" project exists, at least one provider configured
    Steps:
      1. `curl -X POST http://localhost:8000/api/video/generate -F "prompt=test ocean waves" -F "project=test-campaign" -F "count=1"`
      2. Extract job_id from response
      3. Poll `GET /api/video/jobs/{job_id}` until status complete (or timeout 120s)
      4. `find projects/test-campaign/videos/ -name "*.mp4"` — assert at least 1 file
    Expected Result: MP4 file exists under projects/test-campaign/videos/
    Failure Indicators: File in old output/ dir, no file created, job stuck
    Evidence: .sisyphus/evidence/task-7-project-scoped-gen.txt
  ```

  **Commit**: YES (groups with Wave 2 backend)
  - Message: `refactor(video): migrate video generation endpoints to unified router with project-scoped paths`
  - Files: `routers/video.py`

- [x] 8. Caption Scraping Router

  **What to do**:
  - Populate `routers/captions.py` with caption scraping endpoints migrated from `caption_server.py`:
    - `WebSocket /api/captions/ws/{job_id}` — real-time scraping progress. Same protocol: client sends `{"action":"start","profile_url":"...","max_videos":N,"sort":"..."}`, server emits events (status, urls_collected, downloading, frame_ready, ocr_done, all_complete, error)
    - `GET /api/captions/export/{username}` — CSV download (from `caption_server.py:186`)
  - Update pipeline to write to project-scoped paths: `projects/{name}/captions/{username}/captions.csv` and `projects/{name}/captions/{username}/frames/`
  - Accept `project` param in WebSocket start message: `{"action":"start","profile_url":"...","project":"campaign-name",...}`
  - Keep WebSocket client registry: `_ws_clients: dict[str, list[WebSocket]]` as module-level variable
  - Preserve ALL existing behavior: yt-dlp URL listing, thumbnail fetching (batches of 5), GPT-4o OCR (batches of 10), CSV writing
  - Import from `scraper/` modules (frame_extractor, caption_extractor) — these are UNCHANGED

  **Must NOT do**:
  - Do NOT modify scraper/ modules
  - Do NOT change the WebSocket event protocol — clients expect same event names and shapes
  - Do NOT add a REST fallback for the WebSocket flow

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES (parallel with Tasks 7, 9, 10)
  - **Parallel Group**: Wave 2
  - **Blocks**: Task 11
  - **Blocked By**: Tasks 1, 4

  **References**:
  - `caption_server.py:22-160` — Full pipeline: broadcast helper, URL collection, thumbnail batching, OCR batching, CSV writing
  - `caption_server.py:163-190` — WebSocket endpoint and export endpoint
  - `scraper/frame_extractor.py` — `list_profile_videos()`, `get_thumbnail()` — imported unchanged
  - `scraper/caption_extractor.py` — `extract_caption()` — imported unchanged
  - `project_manager.py` (Task 4) — `get_project_caption_dir(name)`

  **Acceptance Criteria**:
  - [ ] WebSocket connection to `ws://localhost:8000/api/captions/ws/test123` succeeds
  - [ ] Sending start message with `project` field triggers pipeline that writes to `projects/{name}/captions/`
  - [ ] `GET /api/captions/export/{username}?project={name}` returns CSV from project-scoped path

  **QA Scenarios**:
  ```
  Scenario: WebSocket connects and responds to start message
    Tool: Bash (websocat or python websockets)
    Preconditions: Unified server running
    Steps:
      1. Connect to `ws://localhost:8000/api/captions/ws/test-job`
      2. Send: `{"action":"start","profile_url":"https://www.tiktok.com/@testuser","max_videos":1,"project":"test-campaign"}`
      3. Assert first received message has `"event":"status"` field
    Expected Result: WebSocket accepts connection, emits status event on start
    Failure Indicators: Connection refused, no response, invalid JSON
    Evidence: .sisyphus/evidence/task-8-ws-connect.txt

  Scenario: Caption CSV lands in project directory
    Tool: Bash
    Preconditions: Caption scraping completed for a profile in project "test-campaign"
    Steps:
      1. `find projects/test-campaign/captions/ -name "captions.csv"`
      2. Assert at least one CSV found
      3. `head -1 <csv_path>` — assert header: "video_id,video_url,caption,error"
    Expected Result: CSV exists in project-scoped path with correct headers
    Failure Indicators: CSV in old caption_output/ dir, missing headers
    Evidence: .sisyphus/evidence/task-8-caption-csv.txt
  ```

  **Commit**: YES (groups with Wave 2 backend)
  - Message: `refactor(captions): migrate caption scraping endpoints to unified router with project-scoped paths`
  - Files: `routers/captions.py`

- [x] 9. Burn Router

  **What to do**:
  - Populate `routers/burn.py` with burn endpoints migrated from `burn_server.py`:
    - `GET /api/burn/videos` — list videos from current project's `videos/` dir (from `burn_server.py:464`)
    - `GET /api/burn/captions` — list caption CSVs from current project's `captions/` dir (from `burn_server.py:470`)
    - `GET /api/burn/fonts` — list available fonts from `fonts/` dir (from `burn_server.py:475`)
    - `POST /api/burn/overlay` — burn one video with overlay PNG + optional color correction (from `burn_server.py:480`). Output to `projects/{name}/burned/`
    - `GET /api/burn/batches` — list completed burn batches in current project (from `burn_server.py:512`)
    - `GET /api/burn/zip/{batch_id}` — ZIP download of burned batch (from `burn_server.py:532`)
    - `WebSocket /api/burn/ws` — legacy WS endpoint, kept for backward compat (from `burn_server.py:561`)
  - All endpoints accept `project` query param to scope to `projects/{name}/`
  - `GET /api/burn/videos` scans `projects/{name}/videos/**/*.mp4` (NOT global `output/`)
  - `GET /api/burn/captions` scans `projects/{name}/captions/*/captions.csv` (NOT global `caption_output/`)
  - Preserve ALL existing ffmpeg pipeline: overlay compositing, color correction, H.264/AAC encoding, 1080x1920 scaling
  - Preserve `html2canvas` PNG overlay decoding (base64 data URL → temp file → ffmpeg)

  **Must NOT do**:
  - Do NOT change the ffmpeg encoding pipeline
  - Do NOT remove the legacy `/api/burn/ws` endpoint
  - Do NOT add parallel burn processing (keep sequential per burn_server.py design)

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES (parallel with Tasks 7, 8, 10)
  - **Parallel Group**: Wave 2
  - **Blocks**: Task 11
  - **Blocked By**: Tasks 1, 4

  **References**:
  - `burn_server.py:464-560` — All REST endpoints (videos, captions, fonts, burn-overlay, batches, zip)
  - `burn_server.py:561-620` — Legacy WebSocket endpoint and burn pipeline
  - `burn_server.py:200-400` — ffmpeg pipeline: overlay compositing, color correction, encoding params
  - `burn_server.py:22-30` — Directory constants to replace with project-scoped paths
  - `burn_server.py:475-478` — Font discovery logic (scan `fonts/` for TikTokSans non-italic)
  - `project_manager.py` (Task 4) — `get_project_burn_dir(name)`, `get_project_video_dir(name)`, `get_project_caption_dir(name)`

  **Acceptance Criteria**:
  - [ ] `curl http://localhost:8000/api/burn/videos?project=test-campaign` returns JSON with videos from project dir
  - [ ] `curl http://localhost:8000/api/burn/captions?project=test-campaign` returns JSON with captions from project dir
  - [ ] `curl http://localhost:8000/api/burn/fonts` returns font list
  - [ ] POST to `/api/burn/overlay` with valid video path and project creates burned MP4 in `projects/{name}/burned/`

  **QA Scenarios**:
  ```
  Scenario: Burn videos endpoint lists only current project videos
    Tool: Bash (curl)
    Preconditions: Server running, project "A" has videos, project "B" has different videos
    Steps:
      1. `curl -s "http://localhost:8000/api/burn/videos?project=A" | python -m json.tool`
      2. Assert videos array contains only files from projects/A/videos/
      3. `curl -s "http://localhost:8000/api/burn/videos?project=B" | python -m json.tool`
      4. Assert videos array contains only files from projects/B/videos/
      5. Assert no overlap between the two responses
    Expected Result: Videos are project-scoped, no cross-contamination
    Failure Indicators: Videos from wrong project appear, empty when files exist
    Evidence: .sisyphus/evidence/task-9-scoped-videos.json

  Scenario: Burn overlay produces output in project directory
    Tool: Bash (curl + ls)
    Preconditions: Server running, project "test-campaign" has at least one video
    Steps:
      1. Get video path from `GET /api/burn/videos?project=test-campaign`
      2. `curl -X POST http://localhost:8000/api/burn/overlay -H "Content-Type: application/json" -d '{"batchId":"test-batch","index":0,"videoPath":"<path>","project":"test-campaign"}'`
      3. Assert response `{"ok": true}`
      4. `ls projects/test-campaign/burned/test-batch/burned_000.mp4` — assert exists
    Expected Result: Burned MP4 in project-scoped burned/ directory
    Failure Indicators: File in old burn_output/, 500 error, missing file
    Evidence: .sisyphus/evidence/task-9-burn-output.txt
  ```

  **Commit**: YES (groups with Wave 2 backend)
  - Message: `refactor(burn): migrate burn endpoints to unified router with project-scoped paths`
  - Files: `routers/burn.py`

- [x] 10. Project CRUD API

  **What to do**:
  - Populate `routers/projects.py` with project management endpoints:
    - `GET /api/projects` — list all projects with stats (video count, caption count, burned count, created date)
    - `POST /api/projects` — create new project (body: `{"name": "campaign-name"}`)
    - `GET /api/projects/{name}` — get single project details
    - `DELETE /api/projects/{name}` — delete project and all its contents
    - `GET /api/projects/{name}/stats` — detailed stats (total file sizes, last activity)
  - Use `project_manager.py` (Task 4) for all filesystem operations
  - Return 409 Conflict if creating a project that already exists
  - Return 404 if accessing/deleting a project that doesn't exist
  - Auto-create "quick-test" project on first GET /api/projects if no projects exist

  **Must NOT do**:
  - Do NOT add project metadata persistence beyond filesystem scanning
  - Do NOT add project sharing, permissions, or ownership
  - Do NOT add project templates or presets

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES (parallel with Tasks 7, 8, 9)
  - **Parallel Group**: Wave 2
  - **Blocks**: Tasks 11, 14
  - **Blocked By**: Tasks 1, 4

  **References**:
  - `project_manager.py` (Task 4) — All CRUD functions: `create_project`, `list_projects`, `get_project`, `delete_project`

  **Acceptance Criteria**:
  - [ ] `curl http://localhost:8000/api/projects` returns project list
  - [ ] `curl -X POST http://localhost:8000/api/projects -H "Content-Type: application/json" -d '{"name":"drake-release"}'` returns 201
  - [ ] `curl -X POST ... -d '{"name":"drake-release"}'` again returns 409
  - [ ] `curl -X DELETE http://localhost:8000/api/projects/drake-release` returns 200
  - [ ] `curl http://localhost:8000/api/projects/nonexistent` returns 404

  **QA Scenarios**:
  ```
  Scenario: Full project CRUD lifecycle
    Tool: Bash (curl)
    Preconditions: Unified server running
    Steps:
      1. `curl -X POST http://localhost:8000/api/projects -H "Content-Type: application/json" -d '{"name":"test-crud"}'` — assert 201
      2. `curl http://localhost:8000/api/projects` — assert "test-crud" in list
      3. `curl http://localhost:8000/api/projects/test-crud` — assert project details returned
      4. `curl -X DELETE http://localhost:8000/api/projects/test-crud` — assert 200
      5. `curl http://localhost:8000/api/projects/test-crud` — assert 404
      6. `ls projects/test-crud 2>&1` — assert "No such file or directory"
    Expected Result: Create → List → Get → Delete → Gone lifecycle works
    Failure Indicators: Wrong status codes, directory not cleaned up, stale data
    Evidence: .sisyphus/evidence/task-10-project-crud.json
  ```

  **Commit**: YES (groups with Wave 2 backend)
  - Message: `feat(projects): add project CRUD API endpoints`
  - Files: `routers/projects.py`

- [ ] 11. Unified App Wiring — CORS, Static Mounts, Health, Startup

  **What to do**:
  - Wire ALL routers into `app.py`: `app.include_router(video_router)`, `app.include_router(captions_router)`, `app.include_router(burn_router)`, `app.include_router(projects_router)`
  - Configure CORS middleware: allow origins `["http://localhost:5173", "http://localhost:8000", "http://127.0.0.1:5173", "http://127.0.0.1:8000"]`
  - Configure static mounts on the main app object:
    - `/fonts` → `fonts/` directory
    - `/projects` → `projects/` directory (for serving video/burned files directly)
    - `/output` → `output/` directory (legacy read-only)
    - `/caption-output` → `caption_output/` directory (legacy read-only)
    - `/burn-output` → `burn_output/` directory (legacy read-only)
    - Catch-all: serve `frontend/dist/` if it exists (production mode)
  - Add lifespan handler that:
    - Checks ffmpeg on PATH (run `ffmpeg -version`)
    - Checks yt-dlp on PATH (run `yt-dlp --version`)
    - Logs which API keys are configured
    - Creates `projects/` dir if it doesn't exist
    - Creates "quick-test" default project if no projects exist
    - Logs startup summary to console
  - Verify the full app starts and all routes resolve: `uvicorn app:app --reload --port 8000`

  **Must NOT do**:
  - Do NOT add authentication middleware
  - Do NOT add rate limiting
  - Do NOT add request logging middleware (keep startup simple)

  **Recommended Agent Profile**:
  - **Category**: `deep`
  - **Skills**: []
    - Integration task touching all routers, static mounts, and startup logic. Needs careful attention to route conflicts.

  **Parallelization**:
  - **Can Run In Parallel**: NO — depends on all Wave 2 router tasks
  - **Parallel Group**: Sequential (after Tasks 7, 8, 9, 10)
  - **Blocks**: Tasks 12, 21, 22, 24
  - **Blocked By**: Tasks 1, 7, 8, 9, 10

  **References**:
  - `server.py:644-650` — Current static mount for `/output` and `/` (video gen)
  - `caption_server.py:195-200` — Current static mounts for `/files` (captions)
  - `burn_server.py:636-660` — Current static mounts for `/video`, `/fonts`, `/burned`, `/` (burn)
  - FastAPI CORS: https://fastapi.tiangolo.com/tutorial/cors/
  - FastAPI lifespan: https://fastapi.tiangolo.com/advanced/events/

  **Acceptance Criteria**:
  - [ ] `uvicorn app:app --port 8000` starts without errors
  - [ ] `curl http://localhost:8000/api/health` returns health JSON
  - [ ] `curl http://localhost:8000/api/video/providers` returns providers
  - [ ] `curl http://localhost:8000/api/projects` returns project list
  - [ ] `curl http://localhost:8000/api/burn/fonts` returns fonts
  - [ ] All WebSocket endpoints connectable

  **QA Scenarios**:
  ```
  Scenario: Unified server has all routes from all 3 original servers
    Tool: Bash (curl)
    Preconditions: Unified server running on 8000
    Steps:
      1. `curl -s http://localhost:8000/api/video/providers` — assert 200
      2. `curl -s http://localhost:8000/api/captions/export/nonexistent` — assert 404 (not 500)
      3. `curl -s http://localhost:8000/api/burn/fonts` — assert 200
      4. `curl -s http://localhost:8000/api/projects` — assert 200
      5. `curl -s http://localhost:8000/api/health` — assert 200 with expected fields
    Expected Result: All route groups respond (200 or expected error codes)
    Failure Indicators: 404 on valid routes, route conflicts, 500 errors
    Evidence: .sisyphus/evidence/task-11-all-routes.json

  Scenario: Startup validates dependencies and logs summary
    Tool: Bash
    Preconditions: ffmpeg and yt-dlp on PATH
    Steps:
      1. `uvicorn app:app --port 8000 2>&1 | head -20`
      2. Assert output contains dependency check results
      3. Assert output contains configured provider names
    Expected Result: Startup log shows dep checks and provider summary
    Failure Indicators: Silent startup with no diagnostics, crash on missing dep
    Evidence: .sisyphus/evidence/task-11-startup-log.txt
  ```

  **Commit**: YES (groups with Wave 2 backend)
  - Message: `feat(app): wire all routers, CORS, static mounts, health check, startup validation`
  - Files: `app.py`

- [ ] 12. Legacy Compatibility + Data Migration Shim

  **What to do**:
  - Add a "legacy import" endpoint or startup routine that:
    - Scans `output/` for existing MP4s and makes them browsable under a "legacy" project
    - Scans `caption_output/` for existing CSVs and makes them browsable under the same legacy project
    - Scans `burn_output/` for existing burned files
    - Does NOT copy/move files — creates symlinks or just makes the old dirs readable via API
  - Add API endpoint `POST /api/projects/import-legacy` that creates a "legacy-imports" project and symlinks old content into it
  - Keep old static mounts (`/output`, `/caption-output`, `/burn-output`) as read-only for backward compat
  - Ensure old HTML UIs at `/static/` are still servable (mount `static/` directory) for emergency fallback

  **Must NOT do**:
  - Do NOT delete or move original files in `output/`, `caption_output/`, `burn_output/`
  - Do NOT auto-migrate on startup — user must explicitly trigger import
  - Do NOT create complex migration tracking

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO — depends on unified app wiring
  - **Parallel Group**: Sequential (after Task 11)
  - **Blocks**: None
  - **Blocked By**: Tasks 4, 11

  **References**:
  - `server.py:645` — Current `/output` static mount
  - `caption_server.py:196` — Current `/files` static mount
  - `burn_server.py:636-642` — Current `/video`, `/burned` static mounts
  - `burn_server.py:658` — Current `/` static mount for burn UI

  **Acceptance Criteria**:
  - [ ] Old videos in `output/` are accessible via `GET /output/{path}`
  - [ ] Old captions in `caption_output/` are accessible via `GET /caption-output/{path}`
  - [ ] `POST /api/projects/import-legacy` creates "legacy-imports" project with old content accessible
  - [ ] Old HTML UIs still load at `/static/index.html`, `/static/captions/index.html`, `/static/burn/index.html`

  **QA Scenarios**:
  ```
  Scenario: Legacy content remains accessible
    Tool: Bash (curl + ls)
    Preconditions: Old output/, caption_output/ dirs have content from previous usage
    Steps:
      1. `curl -s http://localhost:8000/output/` — assert directory listing or file accessible
      2. `curl -s http://localhost:8000/static/index.html` — assert HTML content
      3. `curl -s http://localhost:8000/static/captions/index.html` — assert HTML content
    Expected Result: All legacy paths still resolve
    Failure Indicators: 404 on legacy paths, broken static serving
    Evidence: .sisyphus/evidence/task-12-legacy-compat.txt
  ```

  **Commit**: YES (groups with Wave 2 backend)
  - Message: `feat(legacy): add backward compat for old output dirs and static UIs, legacy import endpoint`
  - Files: `app.py` (mount additions), `routers/projects.py` (import-legacy endpoint)

### Wave 3 — Frontend Build

- [ ] 13. App Shell — Routing, Tab Navigation, Project Context, Shared State

  **What to do**:
  - Build the main `App.tsx` layout: persistent top nav bar with project selector + 4 tab navigation (Projects, Generate, Captions, Burn)
  - Use `react-router-dom` for tab routing: `/` (Projects), `/generate` (Generate), `/captions` (Captions), `/burn` (Burn)
  - Create `stores/workflowStore.ts` using Zustand (or React context) for shared state:
    - `activeProject: string | null` — currently selected project name
    - `jobs: { video: Record<string, Job>, caption: Record<string, CaptionJob> }` — active jobs across tabs
    - `notifications: Notification[]` — cross-tab toast queue
    - `setActiveProject(name)`, `addNotification(msg)`, etc.
  - Create `hooks/useProject.ts` — hook that reads active project from store and provides project-scoped API calls
  - Top nav: project dropdown (fetches from `GET /api/projects`), active project name shown prominently, "New Project" quick-action button
  - Tab badges: show count of active/completed items (e.g., "Generate (3)" if 3 jobs running)
  - Store persists `activeProject` to `localStorage` so it survives page refresh
  - Dark mode by default (Tailwind `dark` class on `<html>`)

  **Must NOT do**:
  - Do NOT build tab page content — just the shell with `<Outlet />` for react-router
  - Do NOT add animations or transitions yet
  - Do NOT install a component library

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
  - **Skills**: [`frontend-ui-ux`]
    - Core frontend architecture: routing, state management, layout

  **Parallelization**:
  - **Can Run In Parallel**: NO — must complete before tab pages
  - **Parallel Group**: Wave 3 start (Task 18 can run in parallel)
  - **Blocks**: Tasks 14, 15, 16, 17, 21
  - **Blocked By**: Tasks 2, 3

  **References**:
  - `frontend/src/types/api.ts` (Task 3) — Type definitions for API responses
  - `static/index.html:1-50` — Current video gen UI structure (dark theme, layout patterns to improve upon)
  - `static/burn/index.html:1-30` — Current burn UI structure
  - Zustand docs: https://github.com/pmndrs/zustand — Lightweight state management

  **Acceptance Criteria**:
  - [ ] App renders with 4 clickable tabs and project selector in top nav
  - [ ] Clicking tabs changes URL and renders different `<Outlet>` content
  - [ ] Selecting a project persists to localStorage and survives refresh
  - [ ] Active project shown in nav bar at all times
  - [ ] Dark mode applied by default

  **QA Scenarios**:
  ```
  Scenario: Tab navigation works and persists project selection
    Tool: Playwright
    Preconditions: Frontend running on localhost:5173, server on 8000
    Steps:
      1. Navigate to http://localhost:5173/
      2. Assert page has 4 tab links visible: "Projects", "Generate", "Captions", "Burn"
      3. Click "Generate" tab — assert URL is /generate
      4. Click "Captions" tab — assert URL is /captions
      5. Select a project from dropdown (or create one if none exist)
      6. Refresh page — assert same project still selected
    Expected Result: Tabs route correctly, project persists across refresh
    Failure Indicators: Tabs don't navigate, project resets on refresh, broken layout
    Evidence: .sisyphus/evidence/task-13-app-shell.png (screenshot)
  ```

  **Commit**: YES (groups with Wave 3 frontend)
  - Message: `feat(frontend): build app shell with tab routing, project context, dark mode`
  - Files: `frontend/src/App.tsx`, `frontend/src/stores/`, `frontend/src/hooks/`

- [ ] 14. Projects Tab — Dashboard, CRUD, Project List

  **What to do**:
  - Build `pages/Projects.tsx` as the dashboard/home tab
  - Project list: card grid showing each project with name, video count, caption count, burned count, created date
  - "New Project" form: text input with name validation (shows sanitized preview), create button
  - Project actions: select (makes it active), delete (with confirmation modal)
  - Empty state: "No projects yet. Create your first project to get started." with prominent CTA
  - Selected project highlighted in the grid
  - Quick stats: total videos, captions, burned across all projects
  - API calls: `GET /api/projects`, `POST /api/projects`, `DELETE /api/projects/{name}`
  - On project select: update Zustand store → nav bar updates → other tabs scope to this project

  **Must NOT do**:
  - Do NOT add project search/filter (overkill for <50 projects)
  - Do NOT add project thumbnails or preview images
  - Do NOT add project import/export

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
  - **Skills**: [`frontend-ui-ux`]

  **Parallelization**:
  - **Can Run In Parallel**: YES (parallel with Tasks 15, 16, 17 — all depend on 13)
  - **Parallel Group**: Wave 3 (after Task 13)
  - **Blocks**: Tasks 19, 20, 25
  - **Blocked By**: Tasks 13, 10

  **References**:
  - `frontend/src/types/api.ts` (Task 3) — `Project`, `ProjectListResponse` types
  - `routers/projects.py` (Task 10) — API endpoints this page calls

  **Acceptance Criteria**:
  - [ ] Projects page shows list of existing projects with stats
  - [ ] Creating a new project appears in list without page refresh
  - [ ] Deleting a project shows confirmation, then removes from list
  - [ ] Selecting a project updates the nav bar project selector

  **QA Scenarios**:
  ```
  Scenario: Create and select a project from dashboard
    Tool: Playwright
    Preconditions: Frontend + server running
    Steps:
      1. Navigate to http://localhost:5173/
      2. Click "New Project" or find the create form
      3. Type "test-dashboard" in name input
      4. Click create button
      5. Assert "test-dashboard" appears in project grid
      6. Click on "test-dashboard" card
      7. Assert nav bar project selector shows "test-dashboard"
      8. Navigate to /generate — assert Generate tab loads (proves project context set)
    Expected Result: Project created, appears in list, becomes active project
    Failure Indicators: API error on create, project not appearing, nav not updating
    Evidence: .sisyphus/evidence/task-14-projects-tab.png
  ```

  **Commit**: YES (groups with Wave 3 frontend)
  - Message: `feat(frontend): build Projects dashboard with CRUD and project selection`
  - Files: `frontend/src/pages/Projects.tsx`

- [ ] 15. Generate Tab — Prompt Form, Provider Picker, Job Progress, Video Gallery

  **What to do**:
  - Build `pages/Generate.tsx` porting functionality from `static/index.html`
  - Provider selector: dropdown populated from `GET /api/video/providers`, show provider name and model info
  - Prompt form: textarea for prompt, number inputs for count (1-20) and duration (1-15), aspect ratio selector, resolution selector, optional media upload (drag & drop or file picker)
  - Job submission: POST to `/api/video/generate` with `project` param from active project
  - Job progress: poll `GET /api/video/jobs/{job_id}` every 2s, show progress for each video in the job (queued → generating → downloading → complete/error)
  - Video gallery: grid of completed videos with thumbnail preview (use `<video>` tag with poster or first frame), download button per video, "Download All" ZIP button
  - Job history: list of all jobs for current project with status chips
  - Guard: if no project selected, show message "Select a project first" with link to Projects tab

  **Must NOT do**:
  - Do NOT change the polling interval or switch to WebSocket for video gen
  - Do NOT add video editing capabilities
  - Do NOT add prompt suggestions or AI prompt enhancement

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
  - **Skills**: [`frontend-ui-ux`]

  **Parallelization**:
  - **Can Run In Parallel**: YES (parallel with Tasks 14, 16, 17)
  - **Parallel Group**: Wave 3 (after Task 13)
  - **Blocks**: Tasks 19, 20, 25
  - **Blocked By**: Task 13

  **References**:
  - `static/index.html:1-700` — FULL current video gen UI (the complete reference for what to port to React). Key sections:
    - Lines ~100-250: Form layout with provider selection, prompt input, options
    - Lines ~300-450: Job progress display with per-video status
    - Lines ~500-600: Polling loop and status update logic
    - Lines ~450-500: Video gallery with download links
  - `frontend/src/types/api.ts` (Task 3) — `Provider`, `GenerateRequest`, `Job`, `VideoEntry` types
  - `routers/video.py` (Task 7) — API endpoints this page calls

  **Acceptance Criteria**:
  - [ ] Provider dropdown populated from API
  - [ ] Submitting a generation job shows real-time polling progress
  - [ ] Completed videos appear in gallery with playable `<video>` elements
  - [ ] "No project selected" guard prevents generation without active project

  **QA Scenarios**:
  ```
  Scenario: Generate tab loads providers and guards project selection
    Tool: Playwright
    Preconditions: Frontend + server running, at least one API key configured
    Steps:
      1. Clear localStorage (no active project)
      2. Navigate to http://localhost:5173/generate
      3. Assert "Select a project first" message is visible
      4. Go to Projects tab, select a project
      5. Return to Generate tab
      6. Assert provider dropdown has options
      7. Assert prompt textarea is visible and enabled
    Expected Result: Guards work, providers load, form is usable after project selection
    Failure Indicators: Form shows without project, empty provider dropdown, guard missing
    Evidence: .sisyphus/evidence/task-15-generate-tab.png
  ```

  **Commit**: YES (groups with Wave 3 frontend)
  - Message: `feat(frontend): build Generate tab with provider picker, job progress, video gallery`
  - Files: `frontend/src/pages/Generate.tsx`

- [ ] 16. Captions Tab — Profile Input, WebSocket Progress, Results Table, CSV Export

  **What to do**:
  - Build `pages/Captions.tsx` porting functionality from `static/captions/index.html`
  - Profile input: URL input for TikTok profile, max videos slider (1-50), sort selector (latest/popular)
  - Create `hooks/useWebSocket.ts` — generic WebSocket hook with auto-reconnect, typed message handling, connection status indicator
  - WebSocket progress display: connect to `/api/captions/ws/{job_id}`, handle all event types:
    - `status` → show status text
    - `urls_collected` → show count of found videos
    - `downloading` → progress bar (X of Y)
    - `frame_ready` → show thumbnail preview (base64 image from `b64` field)
    - `ocr_done` → show extracted caption text next to thumbnail
    - `all_complete` → show results summary, enable export
    - `error` → show error message in red
  - Results table: video thumbnail | video URL (link) | extracted caption text | error status
  - Export button: `GET /api/captions/export/{username}?project={name}` to download CSV
  - Send `project` field in WebSocket start message
  - Guard: if no project selected, show message with link to Projects tab

  **Must NOT do**:
  - Do NOT add caption editing in the results table (read-only)
  - Do NOT change the WebSocket event protocol
  - Do NOT add batch scraping of multiple profiles at once

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
  - **Skills**: [`frontend-ui-ux`]

  **Parallelization**:
  - **Can Run In Parallel**: YES (parallel with Tasks 14, 15, 17)
  - **Parallel Group**: Wave 3 (after Task 13)
  - **Blocks**: Tasks 19, 20, 23, 25
  - **Blocked By**: Task 13

  **References**:
  - `static/captions/index.html` — FULL current caption UI (complete reference for React port). Key sections:
    - Profile URL input form
    - WebSocket connection and event handling logic
    - Thumbnail grid with OCR results
    - Export CSV functionality
  - `caption_server.py:22-160` — WebSocket event protocol (all event names and payload shapes)
  - `frontend/src/types/api.ts` (Task 3) — `CaptionWSMessage`, `CaptionResult` types

  **Acceptance Criteria**:
  - [ ] WebSocket connection indicator shows connected/disconnected state
  - [ ] Entering a TikTok URL and clicking Start shows real-time progress
  - [ ] Thumbnails appear as frames are extracted
  - [ ] Caption text appears as OCR completes
  - [ ] Export CSV button works

  **QA Scenarios**:
  ```
  Scenario: Caption tab connects WebSocket and shows progress events
    Tool: Playwright
    Preconditions: Frontend + server running, active project selected
    Steps:
      1. Navigate to http://localhost:5173/captions
      2. Assert connection status indicator visible
      3. Enter a TikTok profile URL
      4. Click Start
      5. Assert progress events appear (status text updates, progress bar moves)
      6. Wait for completion or assert at least one OCR result row appears
    Expected Result: Real-time progress from WebSocket displayed in UI
    Failure Indicators: WebSocket fails to connect, no progress shown, stuck state
    Evidence: .sisyphus/evidence/task-16-captions-tab.png
  ```

  **Commit**: YES (groups with Wave 3 frontend)
  - Message: `feat(frontend): build Captions tab with WebSocket progress and results table`
  - Files: `frontend/src/pages/Captions.tsx`, `frontend/src/hooks/useWebSocket.ts`

- [ ] 17. Burn Tab — Video/Caption Picker, Pairing UI, Overlay, Burn Progress

  **What to do**:
  - Build `pages/Burn.tsx` porting functionality from `static/burn/index.html`
  - This is the most complex tab — it combines video selection, caption selection, pairing, overlay preview, and burn execution
  - **Video picker**: list videos from `GET /api/burn/videos?project={name}`, with thumbnail previews, multi-select
  - **Caption picker**: list caption sources from `GET /api/burn/captions?project={name}`, show username + caption count per source, expandable to see individual captions
  - **Pairing UI**: drag-and-drop or sequential auto-pair (match video N with caption N). Show pairing grid: video thumbnail | caption text | preview button
  - **Overlay preview**: use `html2canvas` (install via npm) to render caption text as styled overlay on video frame. Show live preview of text position/style.
  - **Style controls**: font selector (from `GET /api/burn/fonts`), font size slider, position (top/center/bottom), text color
  - **Burn execution**: for each pair, POST to `/api/burn/overlay` with overlay PNG (from html2canvas capture) + video path + project name. Show per-video progress (burning X of Y).
  - **Results**: burned video gallery with playback, "Download All" ZIP button (`GET /api/burn/zip/{batch_id}?project={name}`)
  - Guard: if no project selected OR no videos/captions in project, show appropriate empty states

  **Must NOT do**:
  - Do NOT add video trimming or editing
  - Do NOT change the burn pipeline (still sequential POST per pair)
  - Do NOT add custom font upload

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
  - **Skills**: [`frontend-ui-ux`]

  **Parallelization**:
  - **Can Run In Parallel**: YES (parallel with Tasks 14, 15, 16)
  - **Parallel Group**: Wave 3 (after Task 13)
  - **Blocks**: Tasks 19, 20, 23, 25
  - **Blocked By**: Task 13

  **References**:
  - `static/burn/index.html:1-1400` — FULL current burn UI (the MOST complex UI, complete reference). Key sections:
    - Lines ~100-400: Video listing and selection
    - Lines ~400-700: Caption source listing and pairing logic
    - Lines ~700-900: Font loading and text overlay styling
    - Lines ~900-1100: html2canvas overlay capture and preview
    - Lines ~1100-1300: Burn execution loop (parallel fetch to /api/burn-overlay)
    - Lines ~1300-1400: Results gallery and ZIP download
  - `burn_server.py:464-560` — API endpoints this page calls
  - `frontend/src/types/api.ts` (Task 3) — `VideoFile`, `CaptionSource`, `BurnRequest`, `BurnResponse`, `BurnBatch`, `FontInfo` types
  - html2canvas: https://html2canvas.hertzen.com/ — for overlay PNG generation

  **Acceptance Criteria**:
  - [ ] Video picker shows videos from active project only
  - [ ] Caption picker shows caption sources from active project only
  - [ ] Pairing UI allows matching videos with captions
  - [ ] Overlay preview renders text on video frame
  - [ ] Burn execution shows per-video progress
  - [ ] Burned videos appear in results gallery with playback

  **QA Scenarios**:
  ```
  Scenario: Burn tab lists project-scoped content and enables pairing
    Tool: Playwright
    Preconditions: Frontend + server running, active project has at least 1 video and 1 caption CSV
    Steps:
      1. Navigate to http://localhost:5173/burn
      2. Assert video list shows videos from current project
      3. Assert caption list shows caption sources from current project
      4. Select a video and a caption
      5. Assert pairing preview shows video thumbnail with caption text overlay
    Expected Result: Project-scoped content listing, visual pairing preview
    Failure Indicators: Videos from other projects shown, empty lists when content exists, broken overlay
    Evidence: .sisyphus/evidence/task-17-burn-tab.png
  ```

  **Commit**: YES (groups with Wave 3 frontend)
  - Message: `feat(frontend): build Burn tab with video/caption picker, overlay preview, burn execution`
  - Files: `frontend/src/pages/Burn.tsx`

- [ ] 18. Shared UI Components — Progress, Toasts, File Browsers, Error States

  **What to do**:
  - Build reusable components in `frontend/src/components/`:
    - `ProgressBar.tsx` — determinate (X of Y) and indeterminate (spinner) modes, with label and percentage
    - `Toast.tsx` + `ToastContainer.tsx` — notification toasts that stack (success/error/info variants), auto-dismiss after 5s, dismiss button
    - `FileBrowser.tsx` — generic file list component with thumbnail support (for video gallery reuse across Generate and Burn tabs)
    - `ErrorBoundary.tsx` — React error boundary with user-friendly fallback UI (not stack traces)
    - `EmptyState.tsx` — reusable empty state with icon, message, and optional CTA button
    - `ConfirmModal.tsx` — confirmation dialog for destructive actions (delete project, etc.)
    - `StatusChip.tsx` — colored status indicators (queued, running, complete, error)
    - `VideoPlayer.tsx` — lightweight video player wrapper with play/pause, used in galleries
  - All components should use Tailwind CSS, accept standard props, be typed with TypeScript
  - Dark mode compatible (use Tailwind dark: variants or CSS variables)

  **Must NOT do**:
  - Do NOT install a component library (Radix, shadcn, Chakra, etc.)
  - Do NOT add Storybook
  - Do NOT over-engineer — these are simple utility components

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
  - **Skills**: [`frontend-ui-ux`]

  **Parallelization**:
  - **Can Run In Parallel**: YES (can start alongside Task 13)
  - **Parallel Group**: Wave 3 (with Task 13)
  - **Blocks**: Task 20
  - **Blocked By**: Task 2

  **References**:
  - `static/index.html` — Current video gallery HTML patterns to improve upon
  - `static/burn/index.html` — Current burn progress display patterns

  **Acceptance Criteria**:
  - [ ] All components render without errors in isolation
  - [ ] ProgressBar shows correct percentage and label
  - [ ] Toast appears, auto-dismisses, and can be manually dismissed
  - [ ] ErrorBoundary catches render errors and shows fallback UI
  - [ ] All components are dark-mode compatible

  **QA Scenarios**:
  ```
  Scenario: Shared components render correctly
    Tool: vitest + React Testing Library
    Preconditions: Components exist in frontend/src/components/
    Steps:
      1. Render ProgressBar with value=50, max=100 — assert "50%" visible
      2. Render Toast with message="Test" — assert visible, wait 5s, assert hidden
      3. Render EmptyState with message="No items" — assert message visible
      4. Render StatusChip with status="complete" — assert green color class
    Expected Result: All components render with correct content and styling
    Failure Indicators: Render errors, wrong content, missing styles
    Evidence: .sisyphus/evidence/task-18-shared-components.txt
  ```

  **Commit**: YES (groups with Wave 3 frontend)
  - Message: `feat(frontend): build shared UI components (progress, toasts, file browser, error states)`
  - Files: `frontend/src/components/*.tsx`

### Wave 4 — Integration & Polish

- [ ] 19. Cross-Tab Artifact Flow + Notifications

  **What to do**:
  - Wire up the key workflow transitions between tabs:
    - When a video generation job completes → update Zustand store → show toast "3 videos ready in {project}" → Burn tab's video picker auto-refreshes
    - When caption scraping completes → update store → show toast "{N} captions scraped from @{username}" → Burn tab's caption picker auto-refreshes
    - When a burn batch completes → show toast "Batch burned: {N} videos"
  - Add tab badge counts that update in real-time:
    - Generate tab: "(3 running)" if jobs active
    - Captions tab: "(scraping...)" if WS active
    - Burn tab: "(5 ready)" count of paired items ready to burn
  - Add "Quick action" buttons in Generate and Captions results:
    - "Use in Burn →" button on completed video jobs that switches to Burn tab and pre-selects those videos
    - "Use in Burn →" button on completed caption scrapes that switches to Burn tab and pre-selects that caption source
  - Use Zustand store subscriptions to trigger cross-tab updates

  **Must NOT do**:
  - Do NOT add desktop Notification API (browser push notifications)
  - Do NOT add sound effects
  - Do NOT auto-navigate to Burn tab on completion (just notify, let user decide)

  **Recommended Agent Profile**:
  - **Category**: `deep`
  - **Skills**: [`frontend-ui-ux`]
    - Cross-tab state management requires careful reactive programming

  **Parallelization**:
  - **Can Run In Parallel**: NO — needs all 4 tab pages complete
  - **Parallel Group**: Wave 4 (after Tasks 14-17)
  - **Blocks**: Task 26
  - **Blocked By**: Tasks 14, 15, 16, 17

  **References**:
  - `frontend/src/stores/workflowStore.ts` (Task 13) — Zustand store to extend
  - `frontend/src/pages/Generate.tsx` (Task 15) — Job completion events to hook into
  - `frontend/src/pages/Captions.tsx` (Task 16) — WebSocket `all_complete` event to hook into
  - `frontend/src/pages/Burn.tsx` (Task 17) — Video/caption pickers to auto-refresh

  **Acceptance Criteria**:
  - [ ] Completing a video gen job shows toast notification
  - [ ] Burn tab video picker auto-refreshes when new videos generated
  - [ ] "Use in Burn →" button from Generate tab switches to Burn with pre-selection
  - [ ] Tab badges show real-time counts

  **QA Scenarios**:
  ```
  Scenario: Video generation completion triggers cross-tab update
    Tool: Playwright
    Preconditions: Frontend + server running, active project selected
    Steps:
      1. Navigate to /generate, start a video generation job
      2. Wait for job to complete
      3. Assert toast notification appears with completion message
      4. Assert tab badge on Generate tab updates
      5. Navigate to /burn
      6. Assert the newly generated video appears in the video picker without manual refresh
    Expected Result: Cross-tab state flows automatically from Generate to Burn
    Failure Indicators: No toast, burn tab shows stale data, manual refresh needed
    Evidence: .sisyphus/evidence/task-19-cross-tab-flow.png
  ```

  **Commit**: YES (groups with Wave 4)
  - Message: `feat(frontend): add cross-tab artifact flow, notifications, tab badges, quick actions`
  - Files: `frontend/src/stores/workflowStore.ts`, `frontend/src/pages/*.tsx`, `frontend/src/App.tsx`

- [ ] 20. UI Design Polish — Professional Appearance

  **What to do**:
  - Apply consistent design language across all 4 tabs:
    - **Color palette**: Dark mode with charcoal background (#0f0f0f or similar), accent color for CTAs, muted grays for secondary text
    - **Typography**: Clean sans-serif (Inter or system font stack), clear hierarchy (h1/h2/h3 sizes)
    - **Spacing**: Consistent padding/margins using Tailwind spacing scale
    - **Cards**: Rounded corners, subtle borders or shadows for content sections
    - **Form inputs**: Styled inputs/selects/textareas with focus rings, proper sizing
    - **Buttons**: Primary (accent color), secondary (outline), danger (red), disabled states
  - Tab navigation: active tab highlighted, hover states, smooth transitions
  - Project selector: polished dropdown with project name + quick stats
  - Video thumbnails: aspect-ratio-correct previews, loading skeleton states
  - Progress indicators: smooth animated progress bars, not jumpy
  - Ensure the UI feels like a professional internal tool, not a prototype

  **Must NOT do**:
  - Do NOT add animations/transitions that slow down the workflow
  - Do NOT change functionality — purely visual refinement
  - Do NOT install a design system or component library

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
  - **Skills**: [`frontend-ui-ux`]
    - Pure design/styling work

  **Parallelization**:
  - **Can Run In Parallel**: YES (parallel with Tasks 19, 21, 22, 23)
  - **Parallel Group**: Wave 4
  - **Blocks**: None
  - **Blocked By**: Tasks 14, 15, 16, 17, 18

  **References**:
  - `static/index.html` — Current styling to improve upon
  - `static/burn/index.html` — Current burn UI styling patterns
  - Tailwind CSS docs: https://tailwindcss.com/docs

  **Acceptance Criteria**:
  - [ ] Consistent color palette across all tabs
  - [ ] No unstyled or default-browser-styled elements visible
  - [ ] All interactive elements have hover/focus/active states
  - [ ] Loading states show skeleton placeholders (not blank screens)
  - [ ] A non-technical person would describe the UI as "clean" and "professional"

  **QA Scenarios**:
  ```
  Scenario: Visual quality audit across all tabs
    Tool: Playwright (screenshots)
    Preconditions: Frontend running with sample data in a project
    Steps:
      1. Navigate to / (Projects) — take full-page screenshot
      2. Navigate to /generate — take full-page screenshot
      3. Navigate to /captions — take full-page screenshot
      4. Navigate to /burn — take full-page screenshot
      5. Visually verify: dark mode, consistent spacing, no unstyled elements, clear typography
    Expected Result: All 4 tabs have consistent, professional dark-mode design
    Failure Indicators: Inconsistent colors, unstyled inputs, bright/white sections in dark mode
    Evidence: .sisyphus/evidence/task-20-polish-projects.png, task-20-polish-generate.png, task-20-polish-captions.png, task-20-polish-burn.png
  ```

  **Commit**: YES (groups with Wave 4)
  - Message: `style(frontend): apply consistent dark mode design polish across all tabs`
  - Files: `frontend/src/**/*.tsx`, `frontend/src/index.css`

- [ ] 21. Error Handling + Startup Validation UI

  **What to do**:
  - Add a startup health banner in the app shell:
    - On mount, call `GET /api/health`
    - If ffmpeg missing: yellow warning banner "ffmpeg not found — video generation and burning will fail"
    - If yt-dlp missing: yellow warning banner "yt-dlp not found — caption scraping will fail"
    - If no API keys: info banner "No video providers configured. Add API keys to .env"
    - Banner dismissable but re-checks on page load
  - Add error handling to all API calls:
    - Network errors: "Cannot connect to server. Is it running on port 8000?"
    - 404 errors: "Project not found" / "Job not found" with helpful context
    - 500 errors: Show error detail from response body, not raw stack trace
    - API key errors: "Provider X authentication failed. Check your API key in .env"
  - Add error states to each tab:
    - Generate: failed job shows provider-specific error message
    - Captions: WebSocket error event shows message with retry button
    - Burn: failed burn shows ffmpeg error in human-readable form
  - All errors use the Toast component (Task 18) for non-blocking notifications

  **Must NOT do**:
  - Do NOT add error reporting/telemetry
  - Do NOT add automatic retry logic for API calls (except WebSocket reconnect which is Task 23)

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: [`frontend-ui-ux`]

  **Parallelization**:
  - **Can Run In Parallel**: YES (parallel with Tasks 19, 20, 22, 23)
  - **Parallel Group**: Wave 4
  - **Blocks**: None
  - **Blocked By**: Tasks 11, 13

  **References**:
  - `app.py` (Task 11) — `/api/health` endpoint response shape
  - `frontend/src/components/Toast.tsx` (Task 18) — Toast component for error display

  **Acceptance Criteria**:
  - [ ] Missing ffmpeg shows warning banner
  - [ ] Network error shows helpful "server not running" message
  - [ ] 500 errors show clean error message (not raw JSON or stack trace)
  - [ ] Each tab has appropriate error states

  **QA Scenarios**:
  ```
  Scenario: Health check banner shows when dependencies missing
    Tool: Playwright
    Preconditions: Frontend running, server running but ffmpeg renamed/removed temporarily
    Steps:
      1. Navigate to http://localhost:5173/
      2. Assert yellow/orange warning banner appears mentioning "ffmpeg"
      3. Assert banner is dismissable (click X)
      4. Refresh — assert banner reappears
    Expected Result: Dependency warning shown clearly, persists across refreshes
    Failure Indicators: No banner when dep missing, crash instead of warning
    Evidence: .sisyphus/evidence/task-21-health-banner.png
  ```

  **Commit**: YES (groups with Wave 4)
  - Message: `feat(frontend): add startup health check UI and comprehensive error handling`
  - Files: `frontend/src/App.tsx`, `frontend/src/pages/*.tsx`

- [ ] 22. Dev Scripts + Build Pipeline

  **What to do**:
  - Finalize `Makefile` with all targets:
    - `make install` — `pip install -r requirements.txt && cd frontend && npm install && npx playwright install chromium`
    - `make dev` — start FastAPI (uvicorn with reload) + Vite dev server concurrently. Use Python subprocess or npm `concurrently` package. Both processes in foreground with prefixed output (`[api]` and `[ui]`).
    - `make build` — `cd frontend && npm run build` (produces frontend/dist/)
    - `make start` — `python app.py` (serves built frontend, single process)
    - `make test` — `pytest tests/ -v && cd frontend && npx vitest run && npx playwright test`
    - `make clean` — remove `frontend/dist/`, `frontend/node_modules/`, `__pycache__/`, `.pytest_cache/`
  - Update `app.py` to auto-detect and serve `frontend/dist/` when it exists (production mode)
  - Add `__main__` block to `app.py`: `if __name__ == "__main__": uvicorn.run("app:app", host="0.0.0.0", port=8000)`
  - Update `.gitignore` with: `frontend/node_modules/`, `frontend/dist/`, `projects/`, `__pycache__/`, `.pytest_cache/`

  **Must NOT do**:
  - Do NOT add Docker or docker-compose
  - Do NOT add CI/CD configuration
  - Do NOT add environment-specific config files

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES (parallel with Tasks 19-21, 23)
  - **Parallel Group**: Wave 4
  - **Blocks**: None
  - **Blocked By**: Tasks 1, 2, 11

  **References**:
  - `app.py` (Task 1/11) — Entry point to add `__main__` block and frontend serving
  - `Makefile` (Task 1) — Initial Makefile to finalize

  **Acceptance Criteria**:
  - [ ] `make install` installs all Python and Node dependencies
  - [ ] `make dev` starts both servers with labeled output
  - [ ] `make build && make start` serves React app at `http://localhost:8000/`
  - [ ] `make test` runs all test suites
  - [ ] `make clean` removes build artifacts

  **QA Scenarios**:
  ```
  Scenario: Full dev cycle with Makefile
    Tool: Bash
    Preconditions: Clean checkout, Python and Node installed
    Steps:
      1. `make install` — assert exit 0
      2. `make dev &` — assert both "[api]" and "[ui]" output visible
      3. Wait 5s, `curl http://localhost:5173/` — assert React app loads
      4. `curl http://localhost:8000/api/health` — assert API responds
      5. Kill make dev
      6. `make build` — assert `frontend/dist/index.html` exists
      7. `make start &` — `curl http://localhost:8000/` — assert React app served from FastAPI
    Expected Result: All Makefile targets work for full development and production cycle
    Failure Indicators: Missing targets, port conflicts, build failures
    Evidence: .sisyphus/evidence/task-22-dev-scripts.txt
  ```

  **Commit**: YES (groups with Wave 4)
  - Message: `chore(devx): finalize Makefile, build pipeline, gitignore, production serving`
  - Files: `Makefile`, `app.py`, `.gitignore`

- [ ] 23. WebSocket Reconnection + Resilience

  **What to do**:
  - Enhance `hooks/useWebSocket.ts` with robust reconnection logic:
    - Auto-reconnect on disconnect with exponential backoff (1s, 2s, 4s, 8s, max 30s)
    - Max reconnect attempts: 10, then show "Connection lost" permanent error
    - Connection status states: `connecting`, `connected`, `reconnecting`, `disconnected`, `error`
    - On reconnect: re-send last start message to resume job tracking
    - Queue outbound messages while reconnecting (send when connected)
  - Add visual connection indicator in Captions and Burn tabs:
    - Green dot = connected
    - Yellow dot + "Reconnecting..." = reconnecting
    - Red dot + "Disconnected" = failed
  - Handle edge cases:
    - Tab goes to background (Page Visibility API) → don't reconnect unnecessarily
    - Server restart → reconnect picks up from last known state
    - Multiple tabs open → each has own WS connection (no shared WS)

  **Must NOT do**:
  - Do NOT add WebSocket to the video generation tab (it uses polling)
  - Do NOT change server-side WS protocol
  - Do NOT add heartbeat/ping-pong (server doesn't support it)

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES (parallel with Tasks 19-22)
  - **Parallel Group**: Wave 4
  - **Blocks**: None
  - **Blocked By**: Tasks 16, 17

  **References**:
  - `frontend/src/hooks/useWebSocket.ts` (Task 16) — Initial WS hook to enhance
  - `caption_server.py:163-180` — Server-side WS handler (client registry pattern)
  - `burn_server.py:561-600` — Legacy WS handler

  **Acceptance Criteria**:
  - [ ] WS auto-reconnects when server restarts
  - [ ] Connection indicator shows correct state transitions
  - [ ] No errors thrown during reconnection attempts
  - [ ] Reconnection stops after max attempts

  **QA Scenarios**:
  ```
  Scenario: WebSocket reconnects after server restart
    Tool: Playwright + Bash
    Preconditions: Frontend + server running, caption scrape in progress
    Steps:
      1. Navigate to /captions, start a scrape job
      2. Assert WS connected (green indicator)
      3. Kill the FastAPI server process
      4. Assert indicator changes to yellow "Reconnecting..."
      5. Restart FastAPI server
      6. Wait up to 30s — assert indicator returns to green "Connected"
    Expected Result: Automatic reconnection with visual feedback
    Failure Indicators: Stuck on disconnected, errors in console, no visual indicator
    Evidence: .sisyphus/evidence/task-23-ws-reconnect.png
  ```

  **Commit**: YES (groups with Wave 4)
  - Message: `feat(frontend): add WebSocket auto-reconnection with exponential backoff and status indicators`
  - Files: `frontend/src/hooks/useWebSocket.ts`, `frontend/src/pages/Captions.tsx`, `frontend/src/pages/Burn.tsx`

### Wave 5 — Testing

- [ ] 24. API Tests — All Endpoints via pytest

  **What to do**:
  - Write pytest tests for ALL unified API endpoints using httpx AsyncClient:
  - **Project tests** (`tests/test_projects_api.py`):
    - Create project → 201, correct dir structure
    - Create duplicate → 409
    - List projects → includes created project
    - Get project → correct stats
    - Delete project → 200, dir removed
    - Get deleted project → 404
  - **Video tests** (`tests/test_video_api.py`):
    - Get providers → 200, array of objects with id/name
    - Generate with missing prompt → 422
    - Generate with invalid provider → 400
    - Get nonexistent job → 404
    - List jobs → 200, array
  - **Caption tests** (`tests/test_captions_api.py`):
    - Export nonexistent username → 404
    - WebSocket connects and accepts start message
  - **Burn tests** (`tests/test_burn_api.py`):
    - List videos for project → 200
    - List captions for project → 200
    - List fonts → 200, non-empty array
    - List batches → 200
    - Burn overlay with missing video → error
  - **Health test**: GET /api/health → 200 with expected fields
  - Mock external APIs (Grok, FAL, etc.) — do NOT make real API calls
  - Use temp directories for project fixtures (create in setup, cleanup in teardown)

  **Must NOT do**:
  - Do NOT make real API calls to video providers
  - Do NOT test ffmpeg/yt-dlp execution (those are integration tests)
  - Do NOT test frontend — that's Task 25

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES (parallel with Tasks 25, 26)
  - **Parallel Group**: Wave 5
  - **Blocks**: None
  - **Blocked By**: Tasks 5, 11

  **References**:
  - `tests/conftest.py` (Task 5) — Test fixtures
  - `app.py` (Task 11) — The unified app to test
  - `routers/*.py` (Tasks 7-10) — All endpoints to test

  **Acceptance Criteria**:
  - [ ] `pytest tests/ -v` passes with 0 failures
  - [ ] Coverage: every endpoint has at least one success + one error test
  - [ ] No real external API calls made during tests
  - [ ] Tests run in <30 seconds

  **QA Scenarios**:
  ```
  Scenario: pytest suite passes
    Tool: Bash
    Preconditions: All API code and test files exist
    Steps:
      1. `pytest tests/ -v --tb=short`
      2. Assert exit code 0
      3. Assert output shows at least 15 tests passed
      4. Assert output shows 0 failed
    Expected Result: All API tests pass
    Failure Indicators: Test failures, import errors, fixture issues
    Evidence: .sisyphus/evidence/task-24-api-tests.txt
  ```

  **Commit**: YES (groups with Wave 5)
  - Message: `test: add comprehensive API tests for all endpoints`
  - Files: `tests/test_*.py`

- [ ] 25. Frontend Component Tests — All Tabs via vitest

  **What to do**:
  - Write vitest + React Testing Library tests for all 4 tab pages:
  - **Projects.test.tsx**: renders project list, create form works, delete confirmation shows
  - **Generate.test.tsx**: renders provider dropdown, form submission calls API, progress display updates
  - **Captions.test.tsx**: renders profile input, WS connection established, progress events update UI
  - **Burn.test.tsx**: renders video/caption pickers, pairing UI works, burn button calls API
  - **App.test.tsx**: tab navigation works, project selector persists
  - Mock all API calls using MSW (Mock Service Worker) or vitest mocks
  - Mock WebSocket using `vitest-websocket-mock` or custom mock
  - Test user interactions: click, type, select, verify DOM updates

  **Must NOT do**:
  - Do NOT test visual styling (that's QA/screenshot territory)
  - Do NOT make real API calls
  - Do NOT test component internals (test behavior, not implementation)

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: [`frontend-ui-ux`]

  **Parallelization**:
  - **Can Run In Parallel**: YES (parallel with Tasks 24, 26)
  - **Parallel Group**: Wave 5
  - **Blocks**: None
  - **Blocked By**: Tasks 5, 14, 15, 16, 17

  **References**:
  - `frontend/src/pages/*.tsx` (Tasks 14-17) — Components to test
  - `frontend/src/test-utils.tsx` (Task 5) — Test render wrapper
  - React Testing Library: https://testing-library.com/docs/react-testing-library/intro/

  **Acceptance Criteria**:
  - [ ] `cd frontend && npx vitest run` passes with 0 failures
  - [ ] Each tab page has at least 3 test cases
  - [ ] All mocks properly configured (no real API calls)

  **QA Scenarios**:
  ```
  Scenario: vitest suite passes
    Tool: Bash
    Preconditions: All frontend code and test files exist
    Steps:
      1. `cd frontend && npx vitest run --reporter=verbose`
      2. Assert exit code 0
      3. Assert output shows at least 12 tests passed
    Expected Result: All component tests pass
    Failure Indicators: Test failures, mock issues, render errors
    Evidence: .sisyphus/evidence/task-25-component-tests.txt
  ```

  **Commit**: YES (groups with Wave 5)
  - Message: `test: add React component tests for all tabs`
  - Files: `frontend/src/pages/*.test.tsx`, `frontend/src/App.test.tsx`

- [ ] 26. E2E Workflow Test — Playwright

  **What to do**:
  - Write Playwright E2E test for the full workflow: create project → generate video → scrape captions → burn → verify output
  - `tests/e2e/test_full_workflow.py` (or `.ts` if using Playwright JS):
    1. Navigate to app, create a new project "e2e-test"
    2. Switch to Generate tab, verify providers load, submit a generation job (mock provider or use a fast one)
    3. Wait for job completion, verify video appears in gallery
    4. Switch to Captions tab (note: may need to mock TikTok scraping for reliability)
    5. Switch to Burn tab, verify generated video appears in picker
    6. If captions available: create a pair, execute burn, verify burned output
    7. Verify files exist on disk: `projects/e2e-test/videos/`, `projects/e2e-test/burned/`
    8. Clean up: delete project "e2e-test"
  - Mock external APIs to avoid flakiness and costs
  - Configure Playwright to auto-start both Vite and FastAPI (using playwright.config webServer)
  - Target execution time: <5 minutes

  **Must NOT do**:
  - Do NOT make real API calls to video providers
  - Do NOT require real TikTok profile scraping
  - Do NOT test edge cases (that's F3 verification wave)

  **Recommended Agent Profile**:
  - **Category**: `deep`
  - **Skills**: [`playwright`]
    - Complex multi-step E2E test requiring browser automation expertise

  **Parallelization**:
  - **Can Run In Parallel**: YES (parallel with Tasks 24, 25)
  - **Parallel Group**: Wave 5
  - **Blocks**: F1-F4
  - **Blocked By**: Tasks 5, 19

  **References**:
  - `frontend/playwright.config.ts` (Task 5) — Playwright configuration
  - All frontend pages (Tasks 14-17) — What the test navigates through
  - All API endpoints (Tasks 7-10) — What the test exercises

  **Acceptance Criteria**:
  - [ ] `npx playwright test` (or `pytest tests/e2e/`) passes
  - [ ] Full workflow (create → generate → burn) executes end-to-end
  - [ ] Test runs in <5 minutes
  - [ ] Test cleans up after itself (no orphaned projects)

  **QA Scenarios**:
  ```
  Scenario: E2E test passes
    Tool: Bash
    Preconditions: Full app built and runnable
    Steps:
      1. `cd frontend && npx playwright test` (or `pytest tests/e2e/ -v`)
      2. Assert exit code 0
      3. Assert test output shows workflow steps completing
    Expected Result: Full workflow E2E test passes
    Failure Indicators: Timeout, element not found, API errors
    Evidence: .sisyphus/evidence/task-26-e2e-test.txt
  ```

  **Commit**: YES (groups with Wave 5)
  - Message: `test: add end-to-end workflow test with Playwright`
  - Files: `tests/e2e/` or `frontend/tests/`

---

## Final Verification Wave

> 4 review agents run in PARALLEL. ALL must APPROVE. Rejection → fix → re-run.

- [ ] F1. **Plan Compliance Audit** — `oracle`
  Read the plan end-to-end. For each "Must Have": verify implementation exists (read file, curl endpoint, run command). For each "Must NOT Have": search codebase for forbidden patterns — reject with file:line if found. Check evidence files exist in .sisyphus/evidence/. Compare deliverables against plan.
  Output: `Must Have [N/N] | Must NOT Have [N/N] | Tasks [N/N] | VERDICT: APPROVE/REJECT`

- [ ] F2. **Code Quality Review** — `unspecified-high`
  Run `python -m py_compile app.py` + `npx tsc --noEmit` (in frontend/) + `make test`. Review all changed files for: `as any`/`@ts-ignore`, empty catches, console.log in prod, commented-out code, unused imports. Check AI slop: excessive comments, over-abstraction, generic names.
  Output: `Build [PASS/FAIL] | Lint [PASS/FAIL] | Tests [N pass/N fail] | VERDICT`

- [ ] F3. **Real Manual QA** — `unspecified-high` (+ `playwright` skill)
  Start from clean state. Execute EVERY QA scenario from EVERY task — follow exact steps, capture evidence. Test cross-tab integration (generate → burn flow). Test edge cases: empty project, missing API keys, no ffmpeg. Save to `.sisyphus/evidence/final-qa/`.
  Output: `Scenarios [N/N pass] | Integration [N/N] | Edge Cases [N tested] | VERDICT`

- [ ] F4. **Scope Fidelity Check** — `deep`
  For each task: read "What to do", read actual diff. Verify 1:1 — everything in spec was built, nothing beyond spec was built. Check "Must NOT do" compliance. Detect cross-task contamination. Flag unaccounted changes.
  Output: `Tasks [N/N compliant] | Contamination [CLEAN/N issues] | VERDICT`

---

## Commit Strategy

| After Task(s) | Message | Verification |
|------------|---------|--------------|
| 1-6 | `feat(foundation): scaffold unified app, frontend, providers, project model, test infra` | `python -c "from app import app"` + `cd frontend && npm run build` |
| 7-9 | `refactor(routers): migrate video, captions, burn endpoints to unified routers` | `curl localhost:8000/api/video/providers` returns JSON |
| 10-12 | `feat(projects): add project CRUD, path scoping, legacy compat, unified wiring` | `curl localhost:8000/api/projects` + `curl localhost:8000/api/health` |
| 13-18 | `feat(frontend): build React app shell and all 4 tabs with shared components` | `cd frontend && npm run build && npm test` |
| 19-23 | `feat(integration): cross-tab flow, polish, error handling, dev scripts, WS resilience` | `make dev` starts both servers, UI loads at localhost:5173 |
| 24-26 | `test: add API tests, component tests, and E2E workflow test` | `make test` exits 0 |

---

## Success Criteria

### Verification Commands
```bash
make dev           # Expected: FastAPI on 8000 + Vite on 5173, both running
make build         # Expected: frontend/dist/ created with production bundle
python app.py      # Expected: serves React bundle at http://localhost:8000/
make test          # Expected: all pytest + vitest + playwright tests pass
curl localhost:8000/api/health  # Expected: {"status":"ok","ffmpeg":true,"ytdlp":true,"providers":[...]}
curl localhost:8000/api/projects  # Expected: {"projects":[...]}
```

### Final Checklist
- [ ] All "Must Have" present (6 providers, WS progress, project isolation, single startup, agent API, health check)
- [ ] All "Must NOT Have" absent (no database, no auth, no new providers, no backend rewrites, no mobile, no component library)
- [ ] All tests pass (`make test` exits 0)
- [ ] UI is presentable — clean dark-mode design, clear labels, progress indicators, error states
- [ ] Full workflow works: create project → generate → scrape → burn → output in `projects/{name}/burned/`
