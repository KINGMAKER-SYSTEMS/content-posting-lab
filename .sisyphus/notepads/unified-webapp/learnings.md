# Test Infrastructure Setup - Learnings

## Task 3: Test Infrastructure Complete ✓

### What Was Built

**Python Backend (pytest)**
- `pytest.ini` - Config with `asyncio_mode = auto` for async test support
- `tests/conftest.py` - Fixtures for `sync_client` (TestClient) and `async_client` (httpx.AsyncClient)
- `tests/test_smoke.py` - 2 smoke tests: app startup + API docs endpoint
- `tests/e2e/test_smoke.py` - Placeholder for future pytest-based e2e tests
- Added `pytest` and `pytest-asyncio` to requirements.txt

**Frontend (vitest + Playwright)**
- `frontend/vitest.config.ts` - Vitest config with jsdom environment and globals enabled
- `frontend/src/App.test.tsx` - 2 smoke tests: component render + container check
- `frontend/src/test-utils.tsx` - Custom render wrapper (extensible for providers)
- `frontend/playwright.config.ts` - Playwright config with webServer auto-start for both Vite + FastAPI
- `frontend/src/__tests__/e2e/smoke.spec.ts` - Playwright e2e smoke tests
- Updated `frontend/package.json` with test scripts and dev dependencies (vitest, @testing-library/react, @playwright/test, jsdom)

**Build System**
- Updated `Makefile` with:
  - `make test` - Runs pytest + vitest (unit tests only, no e2e)
  - `make test-e2e` - Runs Playwright e2e tests (requires servers running)
- Both exit with code 0 on success

### Key Decisions

1. **Separate vitest.config.ts** - Vitest wasn't picking up test config from vite.config.ts, so created dedicated vitest.config.ts
2. **jsdom environment** - Required for React component testing in Node environment
3. **Playwright webServer config** - Auto-starts both Vite (port 5173) and FastAPI (port 8000) before running e2e tests
4. **Unit tests only in `make test`** - E2e tests require servers running, so separated into `make test-e2e`
5. **TestClient for sync tests** - FastAPI's TestClient is simpler for basic smoke tests than AsyncClient

### Test Results

```
✓ pytest: 2/2 passed (0.19s)
✓ vitest: 2/2 passed (745ms)
✓ make test: Exit code 0
```

### What's Ready for Next Tasks

- All three test frameworks wired and working
- Smoke tests verify basic functionality
- Ready to add feature tests as app grows
- Playwright config ready for full e2e test suite

### Notes for Future

- Playwright e2e tests can be run with `make test-e2e` once servers are running
- Test utilities (conftest.py, test-utils.tsx) are extensible for adding providers/mocks
- Consider adding coverage reporting later (pytest-cov, vitest coverage)
- Consider adding CI/CD integration (GitHub Actions) to run tests on push

## Provider Extraction - Learnings

### Structure
- `providers/base.py` holds shared utilities (download_video, crop_to_vertical, slugify, generate_one) + API_KEYS + OUTPUT_DIR
- Each provider module (grok, fal, luma, replicate, sora) exports `async def generate(prompt, params, client) -> str`
- `providers/__init__.py` exports PROVIDERS dict with metadata + `module` reference per provider
- `generate_one` in base.py uses lazy `from . import PROVIDERS` to avoid circular import

### Key Patterns
- `params` dict carries all generation parameters; each provider extracts what it needs
- `model_id` comes from `PROVIDERS[provider]["models"][0]` - FAL and Replicate use it, others ignore
- Sora is unique: downloads directly to disk via `params["dest"]`, returns empty string
- `PROVIDERS` dict has `module` key that must be filtered out when serializing to JSON in `/api/providers`
- server.py passes `jobs` dict to `generate_one` since state is no longer module-level in providers

### Gotchas
- The `providers/__init__.py` stub and `base.py` ABC stub already existed from prior work - had to overwrite
- LSP flags `from . import PROVIDERS` as unknown in base.py - expected, resolves at runtime
- `FORCE_LANDSCAPE` set and auto-crop logic lives in `generate_one` (base.py), not in individual providers

## Caption Router Migration - Learnings

### What Changed
- `routers/captions.py` populated with full pipeline from `caption_server.py`
- Added `project` param to WebSocket start message and export endpoint
- Project-scoped paths via `get_project_caption_dir(project) / username`
- Fallback to `caption_output/{username}` when no project specified

### Key Patterns
- `_ws_clients` dict at module level — shared across all WebSocket connections
- Lazy imports inside `_run_pipeline` for scraper modules (avoids import-time failures if deps missing)
- `results: list[dict | None]` initialized with `[None] * total` — indexed by video position
- Three-phase pipeline: URL listing → thumbnail batches of 5 → OCR batches of 10
- Router prefix `/api/captions` set in `app.py` line 132, so WS path is `/api/captions/ws/{job_id}`

### Gotchas
- WebSocket routes on APIRouter work with `prefix` — client connects to full prefixed path
- `sort` param collected but unused in pipeline (yt-dlp returns in posting order by default)

## Projects Router - Learnings

### Endpoints Implemented
- `GET /api/projects` — lists all projects, auto-creates "quick-test" if none exist
- `POST /api/projects` — creates project, 201 on success, 409 on duplicate, 400 on bad name
- `GET /api/projects/{name}` — single project details, 404 if not found
- `DELETE /api/projects/{name}` — deletes project + contents, 404 if not found
- `GET /api/projects/{name}/stats` — detailed stats with per-file info, sizes, last_activity

### Key Patterns
- All filesystem ops delegated to `project_manager.py` — router is pure HTTP layer
- `sanitize_project_name` raises `ValueError` for bad input — caught and mapped to 400
- `create_project` raises `FileExistsError` — caught and mapped to 409
- `delete_project` returns `False` for not found — mapped to 404
- Pydantic `CreateProjectRequest` model for POST body validation
- `_get_dir_stats` and `_get_last_activity` are private helpers for the stats endpoint only

### Route Ordering
- `/{name}/stats` must be defined AFTER `/{name}` — FastAPI matches top-down, but since "stats" isn't a valid project name path param, both work. However, keeping stats after the base route is cleaner.

### Removed from Skeleton
- Removed `PUT /{project_id}` (update) — not in spec, no project metadata to update beyond filesystem

## Video Router Migration - Learnings

### What Changed
- `routers/video.py` populated with all 5 endpoints migrated from `server.py`
- `providers/base.py:generate_one` got optional `output_dir` and `url_prefix` params (backward compatible)
- Project-scoped paths: `projects/{name}/videos/{provider}/{slug}/{job_id}_{idx}.mp4`
- `project` param accepted as Form field (alongside other multipart form fields)

### Endpoints
- `GET /providers` — lists providers where API key is configured
- `POST /generate` — multipart form with prompt, provider, count, duration, aspect_ratio, resolution, media, project
- `GET /jobs` — all jobs list
- `GET /jobs/{job_id}` — single job status
- `GET /jobs/{job_id}/download-all` — ZIP download using project-scoped base dir

### Key Decisions
- `project` as `Form("quick-test")` not `Query` — keeps all params in same multipart form body
- `generate_one` params are optional with defaults matching old behavior — `server.py` still works unchanged
- `jobs` dict at module level in router — each router has its own job namespace
- Added `project` field to job dict (additive, doesn't change existing shape)
- URL prefix uses `/projects/{name}/videos/` — requires static mount in `app.py` to serve files

### Gotchas
- `app.py` currently mounts `/output` but NOT `/projects` — video file URLs won't resolve until a projects static mount is added
- The `generate` endpoint fires actual API calls — test with real API keys or expect provider errors in job status
- Old `server.py` and new `app.py` both work independently — they use same providers but different job dicts

## Burn Router Migration - Learnings

### Endpoints Implemented
- `GET /api/burn/videos?project={name}` — scans `projects/{name}/videos/**/*.mp4`
- `GET /api/burn/captions?project={name}` — scans `projects/{name}/captions/*/captions.csv`
- `GET /api/burn/fonts` — lists TikTokSans fonts (no Italic), not project-scoped
- `POST /api/burn/overlay` — burns overlay PNG onto video, outputs to `projects/{name}/burned/{batchId}/`
- `GET /api/burn/batches?project={name}` — lists completed burn batches with counts/timestamps
- `GET /api/burn/zip/{batch_id}?project={name}` — ZIP download of burned batch
- `WebSocket /api/burn/ws` — legacy batch burn with progress events (project in payload)

### Key Decisions
- Entire ffmpeg pipeline (build_filter_complex, build_color_only_filter, burn_video) copied verbatim — too complex/critical to refactor during migration
- Prefixed all helpers with `_` to indicate router-private scope
- `project` required on all endpoints except `/fonts` (fonts are global)
- WebSocket defaults to `quick-test` project if not specified in payload
- No legacy fallback to `output/` or `caption_output/` — clean break to project-scoped dirs

### FFmpeg Pipeline Preservation
- Color correction matrix math: brightness → contrast → saturate → temperature → tint (order matters)
- TikTok encode: H.264 High 4.2, CRF 18, 15Mbps maxrate, 30fps, AAC 128k
- Overlay path: base64 PNG → tempfile → ffmpeg -i overlay → composite
- Color-only path: uses -vf instead of -filter_complex (no overlay input)
- Always scales to 1080x1920 (TikTok vertical)

## Unified app wiring - Learnings
- Catch-all frontend routes can swallow `/api/projects` because the projects router exposes `GET /` under its prefix, which only matches `/api/projects/` by default.
- Adding an explicit `GET /api/projects` alias in `app.py` avoids SPA catch-all takeover and keeps the no-trailing-slash endpoint stable.
- Static mounts needed by the unified app are `/fonts`, `/projects`, `/output`, `/caption-output`, and `/burn-output`; using `check_dir=False` prevents import-time startup failures when directories are not present yet.
