# Handoff: Publish Feature — Full Plan Ready for Implementation

**Date:** 2026-03-25
**Branch:** `main`
**Last commit:** `6dfb727` — `feat: add page roster system`
**Repo:** `KINGMAKER-SYSTEMS/content-posting-lab`

---

## Context

The explore/research phase is complete for the Publish feature. The goal is to transform the Publish tab from a simple roster viewer into a **full content distribution command center** with three integrated systems:

1. **Account Roster Table** — data table with inline editing
2. **Cloudflare Email Routing** — create/manage forwarding addresses per account
3. **TikTok Upload Engine** — queue and execute uploads via `tiktokautouploader`

---

## What Already Exists

### Roster System (built, working)
- **Backend:** `routers/roster.py` + `services/roster.py`
  - CRUD for pages in `page_roster.json` (filesystem, no DB)
  - Sync from Postiz API (fetches integrations, merges into roster)
  - Fields per page: `integration_id`, `name`, `provider`, `picture`, `project`, `drive_folder_url`, `drive_folder_id`, `added_at`, `updated_at`
- **Frontend:** `frontend/src/pages/Publish.tsx` (~430 lines)
  - Card-based layout grouped by project (assigned vs unassigned)
  - Project assignment dropdown per page
  - Drive folder URL input with save/cancel
  - Postiz connection status badge + sync button
  - Uses `Badge`, `Button`, `Input` from `@/components/ui/`
- **Types:** `frontend/src/types/api.ts` — `RosterPage`, `RosterResponse`, `RosterSyncResponse`, `PostizStatusResponse`
- **Store:** `workflowStore.ts` holds `rosterPages`, `rosterLoading`, `setRosterPages`, `setRosterLoading`
- **Router mount:** `app.py` line 135 — `app.include_router(roster_router, prefix="/api/roster")`

### Postiz Integration
- **Backend:** `routers/postiz.py` — status check endpoint
- **Router mount:** `app.py` line 134

---

## The Plan — 4 Phases

### Phase 1: Roster Table Redesign

Refactor `Publish.tsx` from card groups to a proper **data table**.

**Target columns:**

| Column | Type | Editable | Source |
|--------|------|----------|--------|
| Avatar | img | no | Postiz sync |
| Name | text | no | Postiz sync |
| Provider | badge | no | Postiz sync |
| Project | dropdown | yes | roster JSON |
| Drive Folder | URL input | yes | roster JSON |
| Email Alias | text | yes/auto | CF Email Routing (Phase 2) |
| Fwd Destination | text | read | CF Email Routing (Phase 2) |
| Cookie Status | badge | read | cookie file check (Phase 3) |
| Upload Queue | count | read | upload queue (Phase 3) |
| Actions | buttons | - | edit/delete/upload |

**Backend changes:**
- Extend `page_roster.json` schema: add `email_alias`, `email_rule_id`, `fwd_destination` fields to `services/roster.py` `set_page()`
- Add cookie status endpoint to `routers/roster.py`

**Frontend changes:**
- Replace `PageGroup` / `PageRow` card components with a `<table>` layout
- Inline edit mode: click cell -> input appears, Enter saves, Esc cancels
- Column sorting (name, provider, project)
- Filter by project dropdown
- Keep Postiz sync bar and status badge

**Design:** Tailwind v4, neo-brutalist consistent with app:
- Table: `border-2 border-border`, `divide-y divide-border`
- Cells: `px-3 py-2 text-sm`
- Status badges: existing `Badge` component

---

### Phase 2: Cloudflare Email Routing Integration

**API is fully REST — no CLI proxy needed.** All via `https://api.cloudflare.com/client/v4/`.

**CF API endpoints:**

| Operation | Endpoint |
|-----------|----------|
| Create routing rule | `POST /zones/{zone_id}/email/routing/rules` |
| List rules | `GET /zones/{zone_id}/email/routing/rules` |
| Update rule | `PUT /zones/{zone_id}/email/routing/rules/{rule_id}` |
| Delete rule | `DELETE /zones/{zone_id}/email/routing/rules/{rule_id}` |
| Create destination address | `POST /accounts/{account_id}/email/routing/addresses` |
| List destinations | `GET /accounts/{account_id}/email/routing/addresses` |

Auth: `Authorization: Bearer {CF_API_TOKEN}` header.

**Important:** Destination addresses must be verified (CF sends verification email) before rules activate. Destinations are account-level, shared across zones.

**New env vars in `.env`:**
```
CF_API_TOKEN=xxx
CF_ZONE_ID=xxx
CF_ACCOUNT_ID=xxx
CF_EMAIL_DOMAIN=yourdomain.com
```

**New backend: `routers/email_routing.py`**
- `GET /api/email/rules` — list all routing rules
- `POST /api/email/rules` — create rule (alias -> destination)
- `PUT /api/email/rules/{rule_id}` — update
- `DELETE /api/email/rules/{rule_id}` — delete
- `GET /api/email/destinations` — list verified destinations
- `POST /api/email/destinations` — add new destination (triggers verification)

**New service: `services/email_routing.py`** — CF API client

**Frontend:** Email column in roster table shows alias or "Create" button. "Create" auto-generates `accountname@domain.com`, calls API, links in roster. Modal for destination address management.

---

### Phase 3: TikTok Upload Engine

**Core dependency:** `tiktokautouploader` (pip package)
**Source repo:** `github.com/Risingtides-dev/TikTokAutoUploader`

**Key function:**
```python
from tiktokautouploader import upload_tiktok

result = upload_tiktok(
    video='path/to/video.mp4',
    description='Caption text',
    accountname='myaccount',       # maps to TK_cookies_{name}.json
    hashtags=['#fyp', '#viral'],
    sound_name='trending_sound',   # optional
    sound_aud_vol='mix',           # main/mix/background
    schedule='15:00',              # optional HH:MM
    day=5,                         # optional, up to 10 days out
    copyrightcheck=False,
    headless=True,
    stealth=True,
    proxy=None                     # optional dict with server/username/password
)
# Returns "Completed" or "Error"
```

**Architecture:** Library is **synchronous Playwright** (Phantomwright stealth engine). Must run in **background thread pool** — cannot block FastAPI async event loop. Same pattern as existing video generation jobs in `routers/video.py`.

**Cookie system:** First run per account opens non-headless browser for manual TikTok login. Saves `TK_cookies_{accountname}.json`. Reuses after. Has built-in expiry checking.

**Captcha handling:** Auto-solves via Roboflow inference SDK (built into the library).

**New backend: `routers/upload.py`**
```
POST /api/upload/submit              — queue upload job
GET  /api/upload/jobs                — list all jobs
GET  /api/upload/jobs/{job_id}       — poll single job
POST /api/upload/jobs/{job_id}/cancel — cancel queued job
GET  /api/upload/cookies             — list cookie files + expiry status
POST /api/upload/login/{account}     — trigger login flow (non-headless browser)
```

**Job states:** `queued` -> `uploading` -> `completed` | `failed` | `cancelled`

**Queue behavior:**
- Max 1 concurrent upload (browser resource + TikTok rate limit)
- FIFO with configurable delay between uploads (default random 5-15 min)
- Jobs persisted to `upload_jobs.json` (filesystem, no DB)

**Inspired by repo's `TelegramAutomation/Fancy_Upload.py`:**
- Random 6-9 hour intervals for anti-rate-limit
- Sequential processing, status tracking (streak, last upload, next time)

**Frontend:** Upload panel below roster table. Upload button per row or bulk select. Queue view with pending/active/completed jobs. Form: description, hashtags, sound, schedule, stealth toggle.

---

### Phase 4: Queue & Scheduling (future)
- Batch upload: select videos -> distribute across accounts
- Schedule uploads for specific times
- Upload history with success/fail tracking
- Telegram bot notifications (optional, from existing Fancy_Upload.py pattern)

---

## Files to Touch

**Backend — modify:**
- `services/roster.py` — extend schema for email + cookie fields
- `routers/roster.py` — add cookie status endpoint
- `app.py` — mount new routers (`email_routing`, `upload`)
- `requirements.txt` — add `tiktokautouploader`
- `.env` — add CF_API_TOKEN, CF_ZONE_ID, CF_ACCOUNT_ID, CF_EMAIL_DOMAIN

**Backend — create:**
- `routers/email_routing.py` — Cloudflare Email Routing API proxy
- `routers/upload.py` — TikTok upload engine with job queue
- `services/email_routing.py` — CF API client logic
- `services/upload.py` — upload queue management, cookie helpers

**Frontend — modify:**
- `frontend/src/pages/Publish.tsx` — rewrite from cards to data table
- `frontend/src/types/api.ts` — add email routing + upload types
- `frontend/src/stores/workflowStore.ts` — add upload queue state

**Frontend — create (if needed):**
- Components for upload form, queue view, email management modal

---

## Hard Constraints (from CLAUDE.md)

- **NO database** — filesystem + in-memory only
- **NO authentication** — single-user local tool
- **NO component library** — Tailwind CSS directly (exception: existing `@/components/ui/` components already in use — `Badge`, `Button`, `Input` — keep using them)
- **ZERO feature drops** — existing roster functionality must work identically
- **Tab switching preserves state** — components never unmount (CSS display toggling)
- **All async** — FastAPI throughout, no sync blocking in event loop (use thread pool for sync libs)

---

## Recommended Start

**Phase 1 first.** The table layout creates the column slots that Phases 2 and 3 fill in. Start by refactoring `Publish.tsx` from cards to table, then extend the backend schema for the new fields.
