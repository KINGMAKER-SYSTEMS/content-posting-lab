# The Local Studio Pattern

**FastAPI + React Local Workbench**

A Python-powered local workflow tool with a React frontend, designed for single-user multi-stage file processing pipelines with AI agent co-pilot support.

> FastAPI backend handles heavy processing (APIs, ffmpeg, scraping), React frontend provides a tabbed workflow UI, the filesystem IS the database, and everything is designed to be readable by both humans and AI coding agents.

---

## Core Principles

### 1. Filesystem-as-Database

Projects are directories. Outputs are files. State is scannable with `ls`. No ORM, no migrations, no schema versioning. An AI agent in your terminal can `ls projects/drake-release/videos/` and understand what's there.

### 2. Python Does the Work, React Does the UI

Backend handles anything that touches external APIs, binary processing (ffmpeg, Pillow), or system tools (yt-dlp). Frontend handles user interaction, state, and presentation. They meet at a JSON API boundary.

### 3. Tabbed Workflow with Artifact Passing

Each tab is a pipeline stage. Outputs from stage N automatically appear as inputs in stage N+1. Project scoping prevents cross-contamination between workflows.

### 4. Zero-Infrastructure Local Tool

No Docker, no database server, no Redis, no message queue. `make dev` and you're working. Designed to run inside a code editor's browser panel alongside a terminal.

### 5. Agent-Interoperable by Default

Every API returns structured JSON. Every file has a predictable path. A coding agent can interact via filesystem (`ls`, `cat`, `mv`) or via API (`curl`). The tool doesn't care if the user is clicking buttons or an AI is calling endpoints.

### 6. Two-Mode Startup

`make dev` for development (Vite HMR + FastAPI hot reload, two processes). `python app.py` for usage (serves pre-built React bundle, one process). Same codebase, different ergonomics depending on whether you're building or using.

---

## Architecture

```
Backend (Python/FastAPI)          Frontend (Vite/React/TypeScript)
┌──────────────────────┐          ┌──────────────────────────┐
│  app.py               │          │  App Shell               │
│  ├── routers/         │  JSON    │  ├── Projects Tab        │
│  │   ├── stage_a.py   │◄────────►│  ├── Stage A Tab         │
│  │   ├── stage_b.py   │  + WS    │  ├── Stage B Tab         │
│  │   ├── stage_c.py   │          │  └── Stage C Tab         │
│  │   └── projects.py  │          │                          │
│  ├── project_manager  │          │  Zustand Store           │
│  └── /api/health      │          │  ├── activeProject       │
└──────────┬───────────┘          │  ├── jobs/artifacts      │
           │                       │  └── notifications       │
           ▼                       └──────────────────────────┘
┌──────────────────────┐
│  projects/            │  ◄── The "Database"
│  ├── campaign-a/      │
│  │   ├── stage_a_out/ │
│  │   ├── stage_b_out/ │
│  │   └── stage_c_out/ │
│  └── campaign-b/      │
│      └── ...          │
└──────────────────────┘
```

---

## Template Structure

```
my-tool/
├── app.py                  # FastAPI entry, routers, health check, static serving
├── routers/                # One router per workflow stage
│   ├── __init__.py
│   ├── stage_a.py          # Stage A endpoints (REST + optional WS)
│   ├── stage_b.py          # Stage B endpoints
│   ├── stage_c.py          # Stage C endpoints
│   └── projects.py         # Project CRUD
├── project_manager.py      # Filesystem CRUD for project directories
├── frontend/               # Vite + React + TypeScript + Tailwind
│   ├── package.json
│   ├── vite.config.ts      # API proxy to FastAPI in dev mode
│   ├── tsconfig.json
│   ├── index.html
│   └── src/
│       ├── main.tsx
│       ├── App.tsx          # Tab shell + routing + project selector
│       ├── pages/           # One page per tab/stage
│       │   ├── Projects.tsx
│       │   ├── StageA.tsx
│       │   ├── StageB.tsx
│       │   └── StageC.tsx
│       ├── stores/          # Zustand for cross-tab state
│       │   └── workflowStore.ts
│       ├── hooks/           # Shared React hooks
│       │   ├── useWebSocket.ts
│       │   ├── usePolling.ts
│       │   └── useProject.ts
│       ├── components/      # Reusable UI components
│       │   ├── ProgressBar.tsx
│       │   ├── Toast.tsx
│       │   ├── FileBrowser.tsx
│       │   ├── EmptyState.tsx
│       │   ├── ConfirmModal.tsx
│       │   └── ErrorBoundary.tsx
│       └── types/           # TypeScript API contracts
│           └── api.ts
├── projects/               # The "database" — one dir per project (gitignored)
├── tests/                  # pytest API tests
│   ├── conftest.py
│   └── test_*.py
├── Makefile                # dev, build, start, test, clean
├── requirements.txt
└── .env                    # API keys and config (gitignored)
```

---

## Tech Stack

| Layer | Choice | Why |
|-------|--------|-----|
| Backend | Python + FastAPI | Async, great ecosystem for APIs/processing/scraping/AI |
| Frontend | Vite + React + TypeScript | Modern DX, HMR, type safety, component architecture |
| Styling | Tailwind CSS (no component libraries) | Utility-first, no dependency bloat, dark mode built-in |
| State (client) | Zustand + localStorage | Lightweight, persists across refresh, cross-tab reactive |
| State (server) | In-memory dicts | Simple, no persistence needed for job tracking |
| Data | Filesystem directories | Zero setup, agent-readable, `ls`-able |
| Real-time | WebSocket (streaming ops) + Polling (async jobs) | Match the transport to the operation type |
| Testing | pytest + vitest + Playwright | Full stack coverage, one command |
| Dev tooling | Makefile | Universal, no extra dependencies |

---

## Key Patterns

### Project Scoping

Every API endpoint accepts a `project` parameter. Every file operation writes to `projects/{name}/`. The UI enforces project selection before any workflow action.

```python
# Backend
@router.get("/api/stage_a/items")
async def list_items(project: str):
    project_dir = get_project_dir(project, "stage_a_output")
    return scan_directory(project_dir)
```

```typescript
// Frontend — project automatically injected by hook
const { data } = useProjectAPI<Item[]>("/api/stage_a/items")
```

### Cross-Tab Artifact Flow

When a stage completes, it updates the shared Zustand store. Downstream tabs react automatically.

```typescript
// Stage A completion → Zustand update → Stage C auto-refreshes
const store = useWorkflowStore()

// In Stage A, on job complete:
store.addNotification("3 items ready for Stage C")
store.invalidateStage("stage_c") // triggers re-fetch in Stage C tab

// In Stage C, items list re-fetches automatically
```

### Two-Mode Serving

```python
# app.py — serves React bundle if it exists, otherwise API-only
frontend_dist = Path("frontend/dist")
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
```

```makefile
# Makefile
dev:    # Two processes, hot reload on both sides
	uvicorn app:app --reload --port 8000 & cd frontend && npm run dev

start:  # Single process, serves built bundle
	python app.py
```

### Health Check on Startup

```python
@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "dependencies": {
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "yt_dlp": shutil.which("yt-dlp") is not None,
        },
        "providers": [p for p in PROVIDERS if p.is_configured()],
    }
```

Frontend shows a warning banner if any dependency is missing — actionable messages, not stack traces.

### Agent Interoperability

The tool is designed so an AI coding agent in your terminal can participate in the workflow:

```bash
# Agent can browse project content
ls projects/campaign-a/videos/
ls projects/campaign-a/captions/

# Agent can read data
cat projects/campaign-a/captions/creator/captions.csv

# Agent can interact via API
curl http://localhost:8000/api/projects
curl http://localhost:8000/api/stage_a/items?project=campaign-a

# Agent can move/organize files
mv projects/campaign-a/videos/draft_01.mp4 projects/campaign-a/videos/final_01.mp4
```

---

## When to Use This Pattern

**Good fit:**
- Single-user local tools
- Multi-stage file processing pipelines
- Tools you use alongside a code editor / AI assistant
- Workflows where outputs from one stage feed into the next
- Projects where you want zero infrastructure overhead

**Not a fit:**
- Multi-user / collaborative tools (need auth, real database)
- High-scale production services (need proper queues, persistence)
- Tools that need offline/mobile support
- Simple CRUD apps (overkill for basic data entry)

---

## Getting Started with a New Tool

1. **Define your stages** — What are the 2-4 sequential steps in your workflow?
2. **Define your artifacts** — What does each stage produce? (files, CSVs, images, videos)
3. **Scaffold the backend** — One router per stage + project router + health check
4. **Scaffold the frontend** — One tab per stage + Projects tab
5. **Wire the flow** — Stage N outputs appear in Stage N+1 inputs via project-scoped directories
6. **Add polish** — Dark mode, progress indicators, error states, toasts
7. **Add tests** — API tests (pytest), component tests (vitest), workflow test (Playwright)
