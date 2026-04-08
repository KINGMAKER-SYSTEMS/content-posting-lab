# PRD: Pipeline Navigation Restructure

## Problem

The app's navigation is chaotic and unintuitive to anyone who didn't build it:

1. **Tools dropdown** — 6 flat items (Generate, Clipper, Recreate, Captions, Burn, Slideshow) with no grouping, no workflow order, no sense of pipeline. Users must already know which tool does what and in what order.
2. **Projects page** — Full dedicated page (`/`) that's dead space after initial setup. You visit once to create a project, then the header selector handles switching. Wasted real estate.
3. **Disconnected stages** — Distribution tab (Telegram/Roster/Sounds/Uploads) is a separate top-level nav item with no visual connection to the content creation tools that feed into it.

The actual workflow is a pipeline: **create content → caption it → ship it**. The UI should reflect that.

## Solution

Replace the entire navigation structure with **4 pipeline stages**, each using the sub-tab architecture already proven in the Distribution restructure.

### New Navigation

```
┌──────────────────────────────────────────────────────────┐
│  Content Posting Lab              [Project Selector ▾]   │
├──────────┬───────────┬────────────┬──────────────────────┤
│   Home   │  Create   │  Captions  │     Distribute       │
└──────────┴───────────┴────────────┴──────────────────────┘
```

4 top-level tabs. 3 of them have sub-tabs. Left-to-right = the workflow.

### Tab Breakdown

#### 1. Home (`/`)

**Purpose:** Mission control. What's happening right now, quick actions, project management.

**Sections (single page, no sub-tabs):**

- **Project Summary Card** — Active project name, file counts (videos, clips, captions, burned), disk usage if available. Create/delete/switch projects inline (replaces the dedicated Projects page).
- **Pipeline Status** — Live indicators showing what's running across all stages:
  - "3 videos generating (Grok, FAL)" 
  - "Caption scrape active: @username"
  - "Burn job: 5/12 complete"
  - "Upload queue: 3 pending"
  - Empty state: "Nothing running — start by creating content"
- **Recent Activity Feed** — Last 8-10 completed items across all stages. Each entry: thumbnail (if available), description, timestamp, link to jump to the relevant stage. Sources: completed generate jobs, finished scrapes, burned videos, uploaded content.
- **Quick Launch Grid** — 4 large cards mapping to the pipeline stages:
  - "Generate Video" → `/create`
  - "Clip Video" → `/create/clipper`
  - "Scrape Captions" → `/captions`
  - "Burn Captions" → `/captions/burn`
  
  Each card shows a brief description + the active badge count if something's running.

**Data sources:** Zustand store (all existing fields: `videoRunningCount`, `captionJobActive`, `recreateJobActive`, `burnReadyCount`, `generateJobs`, `uploadJobs`, `uploadStats`), plus `/api/projects` for project list, `/api/projects/{name}/stats` for detailed stats.

#### 2. Create (`/create`)

**Purpose:** Everything that produces raw content assets.

**Sub-tabs:**
| Sub-tab | Route | Current Page | Badge |
|---------|-------|-------------|-------|
| Generate | `/create` (default) | `Generate.tsx` (1,315 lines) | `videoRunningCount` |
| Clipper | `/create/clipper` | `Clipper.tsx` (1,076 lines) | — |
| Recreate | `/create/recreate` | `Recreate.tsx` (685 lines) | `recreateJobActive` → "LIVE" |
| Slideshow | `/create/slideshow` | `Slideshow.tsx` (787 lines) | — |

**Architecture:** Same pattern as Distribution — parent `CreatePage.tsx` holds shared state (minimal — these pages are mostly independent), renders sub-tab nav + CSS `display:block/none` toggling.

**State lifting:** Unlike Distribution where Publish+Telegram shared roster state, the Create tools are independent. The parent component is thin — just sub-tab routing. Each existing page component becomes a sub-tab component with zero internal changes.

#### 3. Captions (`/captions`)

**Purpose:** Everything that adds text overlays to content.

**Sub-tabs:**
| Sub-tab | Route | Current Page | Badge |
|---------|-------|-------------|-------|
| Scrape | `/captions` (default) | `Captions.tsx` (855 lines) | `captionJobActive` → "LIVE" |
| Burn | `/captions/burn` | `Burn.tsx` (1,128 lines) | `burnReadyCount` |

**Architecture:** Parent `CaptionsPage.tsx` with sub-tab nav. Thin wrapper — both tools are independent. Existing page components become sub-tab components unchanged.

#### 4. Distribute (`/distribute`)

**Purpose:** Ship content to platforms and manage poster network.

**Sub-tabs:** (already built — rename route from `/distribution` to `/distribute`)
| Sub-tab | Route | Current Component |
|---------|-------|------------------|
| Roster | `/distribute` (default) | `RosterTab.tsx` |
| Telegram | `/distribute/telegram` | `TelegramTab.tsx` |
| Sounds | `/distribute/sounds` | `SoundsTab.tsx` |
| Uploads | `/distribute/uploads` | `UploadsTab.tsx` |

**Changes:** Rename route prefix from `/distribution` to `/distribute`. No internal changes.

---

## Navigation Design

### Top Nav Bar

Replace the current header + nav structure (header → health banner → tab bar) with a cleaner single-bar layout:

```
┌────────────────────────────────────────────────────────────────┐
│ [Logo] Content Posting Lab    Home  Create  Captions  Distrib │
│                                          [Project Selector ▾] │
└────────────────────────────────────────────────────────────────┘
```

- **Pipeline tabs** replace: Projects tab + Tools dropdown + Distribution tab
- **Active tab** gets bottom border highlight (existing pattern)
- **Badges** on tabs show running job counts (existing pattern, moved from dropdown items to top-level tabs)
- **Tab badges aggregate:** Create tab shows combined badge if any sub-tool is active. Captions tab shows combined badge if scrape or burn active.
- **Project selector** stays in header (already works well there)
- **"+ New Project" button** moves to Home page (no longer in header — reduces header clutter)

### Sub-Tab Nav

Reuse the existing sub-tab pattern from Distribution:
- Horizontal tab bar below the main nav
- CSS `display:block/none` toggling (components stay mounted)
- Lazy mounting via `visitedTabs` Set
- Active sub-tab gets underline highlight
- Badges on individual sub-tabs for granular status

### Health Banner

Stays as-is — appears below nav when health issues detected. No changes.

---

## What Gets Deleted

| File | Reason |
|------|--------|
| `pages/Projects.tsx` (324 lines) | Merged into Home page |

All other existing page files are **preserved as-is** — they become sub-tab content components. No internal modifications to any existing page component.

## What Gets Created

| File | Purpose | Est. Lines |
|------|---------|-----------|
| `pages/Home.tsx` | Dashboard — project summary, pipeline status, recent activity, quick launch | ~400 |
| `pages/Create.tsx` | Parent wrapper — sub-tab nav + CSS toggling for Generate/Clipper/Recreate/Slideshow | ~80 |
| `pages/CaptionsStage.tsx` | Parent wrapper — sub-tab nav + CSS toggling for Scrape/Burn | ~60 |

## What Gets Modified

| File | Change |
|------|--------|
| `App.tsx` | Replace all nav tabs + page mounting with 4 pipeline stages. Remove Tools dropdown logic. Update lazy mount paths. |
| `pages/Distribution.tsx` | Rename route handling from `/distribution` to `/distribute` |
| `stores/workflowStore.ts` | Add `recentActivity` array for Home page feed (optional — can use existing fields initially) |
| `App.test.tsx` | Update tab assertions for new nav structure |

## Constraints

- **ZERO feature drops** — every button, input, form, WebSocket, API call works identically
- **No internal page rewrites** — Generate.tsx, Clipper.tsx, Captions.tsx, Burn.tsx, Recreate.tsx, Slideshow.tsx keep their exact internal implementations
- **Same architecture** — CSS display toggling, lazy mount, Zustand, no new libraries
- **No mobile responsive** — desktop-first (existing constraint)
- **Existing design system** — neobrutalism theme, shadcn components, same CSS variables

## Implementation Order

1. Create `Home.tsx` (new dashboard page)
2. Create `Create.tsx` (thin sub-tab wrapper for Generate/Clipper/Recreate/Slideshow)
3. Create `CaptionsStage.tsx` (thin sub-tab wrapper for Scrape/Burn)
4. Rename Distribution routes `/distribution` → `/distribute`
5. Rewrite `App.tsx` nav — replace Tools dropdown + Projects tab + Distribution tab with 4 pipeline tabs
6. Rewrite `App.tsx` page mounting — 4 lazy-mounted stage components instead of 7 individual pages
7. Delete `Projects.tsx`
8. Update tests
9. Build + test verification

## Success Criteria

- New user opens app → sees Home with clear "what do I do" quick launch cards
- Pipeline stages read left to right as a workflow
- Zero confusion about which tool to use for what
- All existing functionality preserved — nothing moves, breaks, or disappears
- Running jobs visible from any tab via top-level badges
- Tab switching preserves all state (same CSS toggling architecture)
