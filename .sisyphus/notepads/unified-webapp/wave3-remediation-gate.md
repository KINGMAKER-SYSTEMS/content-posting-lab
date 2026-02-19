# Wave 3 Remediation Gate (Atlas)

Date: 2026-02-18
Scope: Fix critical Wave 3 plan deviations before continuing to Wave 4/5 tasks.

## Why this gate exists

Wave 3 was marked complete in git history, but the implementation currently misses core acceptance criteria from Task 13 and Task 14. If we continue into Wave 4/5 without fixing these, integration and testing work will stack on top of unstable app-shell behavior and incorrect project wiring.

## Hard gate rule

Do not proceed with Tasks 20-26 until all remediation items R1-R8 pass verification.

## Remediation items

### R1 - Restore app shell contract (Task 13)

- Files: `frontend/src/App.tsx`
- Required:
  - Root route `/` must render Projects tab content.
  - Top nav must expose exactly 4 workflow tabs: Projects, Generate, Captions, Burn.
  - Remove standalone Home tab/landing page from primary flow.
- Done when:
  - Visiting `/` shows Projects page, not a separate marketing/landing page.
  - Tab bar shows 4 tabs only and routing works for all 4.

### R2 - Wire project selector in top nav (Task 13)

- Files: `frontend/src/App.tsx`, `frontend/src/components/ProjectSelector.tsx`, `frontend/src/stores/workflowStore.ts`
- Required:
  - Add project selector directly into persistent top nav.
  - Selector reads projects from `GET /api/projects`.
  - Selector updates global active project for all tabs.
  - Add "New Project" quick action in nav (can open existing create modal route/state).
- Done when:
  - Active project is visible in nav at all times.
  - Switching project in nav immediately scopes Generate/Captions/Burn behavior.

### R3 - Fix project type contract mismatch

- Files: `frontend/src/types/api.ts`, `frontend/src/pages/Projects.tsx`, any project-related hooks/components
- Required:
  - Align `Project` type with backend response shape from `routers/projects.py`:
    - `name`, `path`, `video_count`, `caption_count`, `burned_count`
  - Remove usage of non-existent fields (`id`, `created_at`, `updated_at`) or introduce explicit adapter layer.
  - Fix create-project response handling (`{ project: ... }` wrapper).
- Done when:
  - Project cards render real API data only.
  - No reliance on fake IDs/timestamps.

### R4 - Remove mock fallback project data

- Files: `frontend/src/pages/Projects.tsx`
- Required:
  - Delete hardcoded fallback projects ("Music Video Campaign", "Product Launch").
  - Replace with proper error state + toast.
- Done when:
  - API failure shows explicit error UX, never fabricated projects.

### R5 - Use real project stats in Projects page (Task 14)

- Files: `frontend/src/pages/Projects.tsx`, optional helper hook
- Required:
  - Project card counts must come from API-backed fields.
  - Dashboard aggregate totals must sum actual project counts.
- Done when:
  - Totals and per-project counters are non-hardcoded.

### R6 - Fix EmptyState prop mismatch

- Files: `frontend/src/components/EmptyState.tsx`, `frontend/src/pages/Projects.tsx` (and other callers)
- Required:
  - Align callsites with actual EmptyState API, or extend component API consistently.
  - Keep usage type-safe.
- Done when:
  - No runtime/typing mismatch for EmptyState usage.

### R7 - Remove implementation notes from production component

- Files: `frontend/src/pages/Burn.tsx`
- Required:
  - Remove long in-code commentary block in `handleBurn`.
  - Keep only concise, necessary comments.
- Done when:
  - Burn page logic is readable and production-clean.

### R8 - Tighten store typing and persistence

- Files: `frontend/src/stores/workflowStore.ts`
- Required:
  - Replace `Record<string, any>` and `data: any` with concrete types.
  - On startup, hydrate `activeProject` from localStorage.
- Done when:
  - Active project survives refresh and types are explicit.

## Verification checklist (required before closing gate)

Run and record outcomes:

1. Frontend build

```bash
cd frontend && npm run build
```

2. Backend smoke tests

```bash
pytest tests/test_smoke.py -q
```

3. Manual route and nav verification

- `/` opens Projects page.
- Top nav has 4 tabs only.
- Nav project selector updates active project globally.
- Refresh preserves active project.

4. Error UX verification

- Stop API server and confirm Projects page shows real error state (no fake project cards).

## Known test infrastructure note

- `pytest tests -q` currently fails in `tests/e2e/test_smoke.py` because `page` fixture is unavailable in pytest context.
- This is pre-existing test wiring debt and should be addressed in Wave 5 testing tasks, not as part of this remediation gate.

## Suggested execution order

R3 -> R4 -> R6 -> R1 -> R2 -> R5 -> R8 -> R7

Rationale: fix data contracts first, then app shell wiring, then polish/cleanup.
