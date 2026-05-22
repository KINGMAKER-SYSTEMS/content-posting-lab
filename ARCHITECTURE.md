# Content Posting Lab — Architecture

> Source-of-truth document for what this app is, what it does, and how the pieces fit together.
> Last verified: 2026-04-30 against the worktree at the time of writing.
> If something here disagrees with the code, the code wins — fix this doc.

---

## 1. What this is in one paragraph

The Content Posting Lab is internal tooling for Rising Tides, a social media marketing agency that runs TikTok and Instagram UGC campaigns for major record labels. The lab covers the **production half** (AI video generation, TikTok caption scraping, 9:16 clipping, caption burning, slideshow rendering) and the **distribution half** (page roster management, Cloudflare email aliases, R2 storage, Telegram sound-assignment messaging, direct TikTok/IG uploads). It is a single FastAPI process serving a React 19 SPA from the same origin, deployed on Railway with persistent state on a mounted volume.

The lab is **two systems sharing a front door**: a "Flow Stage" pipeline where the lab is a passthrough (mint email, ping Slack, get out of the way — Jay's external system handles delivery), and a "King Maker" pipeline where the lab is the full engine (provision storage + topic + poster, generate content, send sound assignments, push uploads).

---

## 2. The two pipelines (the most important thing to understand)

| | **Flow Stage** | **King Maker** |
|---|---|---|
| Who delivers content | Jay's external Flow Stage tooling | The lab + assigned poster |
| Storage | None (lab does nothing) | Cloudflare R2 (legacy: Google Drive) |
| Email alias forwards to | `jay@risingtidesent.com` | `glitch@risingtidesent.com` |
| Telegram topic created? | No | Yes — per-page topic in poster's supergroup |
| Poster assigned? | No | Yes — resolved from `poster_name` field |
| Sound assignments? | No | Yes — main bot sends HTML to "Campaign Sounds" topic |
| Lab's job after intake | Mint alias, Slack ping, status flip | Full provisioning + ongoing content + delivery |
| Kanban stages | New → In Production → Live → Complete | New → In Production → Delivered to Poster → Live → Complete |

**The shared step:** both pipelines hit the same intake endpoint (`POST /api/pipeline/intake`), which creates a Notion row (`status: "New — Pending Setup"`) and mints a Cloudflare email alias (`acct-XXXX@risingtidesviral.com`). The destination of that alias forwarding is the only place the pipelines diverge during intake.

After intake, **`POST /api/pipeline/{integration_id}/setup`** is the branch point. Flow Stage takes the short path. King Maker takes the full path (poster resolution → topic creation → R2 prefix → status flip).

---

## 3. Deployment topology

```
Railway
└── Single FastAPI process (python app.py)
    ├── Serves /api/* (15 routers)
    ├── Serves /ws/* (WebSocket endpoints in captions, burn, clipper, recreate)
    ├── Serves /fonts, /projects, /output, /caption-output, /burn-output (StaticFiles)
    ├── SPA fallback: serves frontend/dist/index.html for unknown paths
    ├── Lifespan: starts the Telegram bot if TELEGRAM_BOT_TOKEN is set
    └── Reads/writes Railway volume at RAILWAY_VOLUME_MOUNT_PATH
        ├── telegram_config.json       — bot token, posters, sounds, page playlists
        ├── page_roster.json           — Postiz integration ID → project + Drive
        ├── email_rules.json           — Cloudflare routing rules
        ├── upload_jobs.json           — TT/IG upload queue state
        └── projects/<name>/           — per-project videos, clips, captions, burns
```

Frontend dev runs separately at `npm run dev` (port 5173) with a Vite proxy to `:8000`. In production both are served from the same origin.

---

## 4. External integrations (every one, verified from the code)

| Integration | Env var(s) | What it does | Used by |
|---|---|---|---|
| **xAI Grok** | `XAI_API_KEY` | `grok-imagine-video` AI video generation | `providers/grok.py` |
| **Replicate** | `REPLICATE_API_TOKEN` | MiniMax Hailuo 2.3, Wan 2.2 (t2v/i2v/i2v-fast), PrunaAI P-Video (landscape + vertical) | `providers/replicate.py` |
| **OpenAI** | `OPENAI_API_KEY` | GPT-4.1 caption OCR, GPT-4.1-mini fuzzy sound matching | `services/captions.py`, `services/campaign_hub.py` |
| **Notion CRM** | `NOTION_API_KEY`, `NOTION_PAGES_DB`, `NOTION_CAMPAIGNS_DB`, `NOTION_SOUND_CUTOFF` | Canonical page roster (PAGES_DB), active campaigns + TikTok Sound Links (CAMPAIGNS_DB), filter cutoff date | `services/notion.py`, `services/notion_pages.py` |
| **Cloudflare Email Routing** | `CF_API_TOKEN`, `CF_ACCOUNT_ID`, `CF_ZONE_ID`, `CF_EMAIL_DOMAIN` | Mint per-page email aliases under `risingtidesviral.com`, route to verified destinations | `services/email_routing.py` |
| **Cloudflare R2** | `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_ENDPOINT` | Per-page object storage prefixes (`accounts/{integration_id}/`) — King Maker storage | `services/r2.py` |
| **Google Drive (legacy)** | `GOOGLE_SERVICE_ACCOUNT_JSON` or `GOOGLE_SERVICE_ACCOUNT_FILE` | Per-page folders. Being phased out for King Maker; existing pages keep their `drive_folder_url` until migrated | `services/gdrive.py` |
| **Slack** | `SLACK_WEBHOOK_URL` | `post_flow_stage_handoff` — pings Jay only on Flow Stage intake | `services/slack.py` |
| **Telegram** | `TELEGRAM_BOT_TOKEN` (env beats stored token in config) | Single send-only main bot. `create_forum_topic` + `send_text_to_topic` (HTML, no preview) | `telegram_bot.py`, `services/telegram.py` |
| **Campaign Hub** | (separate Rising Tides service, base URL hardcoded with override) | Source of truth for which campaigns are ACTIVE (`completion_status` filter). Synced every 15 min | `services/campaign_hub.py` |
| **Email forwarding destinations** | `EMAIL_HANDOFF_TO_FLOW_STAGE` (default `jay@`), `EMAIL_HANDOFF_TO_KING_MAKER` (default `glitch@`) | Where CF aliases route — pipeline-dependent | `services/email_send.py` |
| **Railway Volume** | `RAILWAY_VOLUME_MOUNT_PATH` | Mount path for persistent JSON config + project data | `project_manager.py`, `services/telegram.py`, etc. |
| **System binaries (PATH)** | — | `ffmpeg` (clipping/burning/audio mux), `yt-dlp` (TikTok download), `tesseract` (OCR fallback) | `services/ffmpeg.py`, scrape scripts |

**Removed / not wired (don't get tricked by stale references):**
- Postiz — router was deleted, not registered in `app.py`
- Sounds bot — there is no separate sounds bot; the main bot handles all sends
- Staging group / forwarding subsystem — deleted in this PR (issue #39)
- FAL / Luma — `CLAUDE.md` mentions both but no provider modules exist

---

## 5. Backend file map

### Routers (15 routers, ~150 endpoints)

| Router | Lines | Endpoint count | Purpose |
|---|---|---|---|
| `routers/clipper.py` | 1592 | 15 | 9:16 video clipping — upload, batch trim, R2 upload init/complete, processing pipeline (SSE + WebSocket) |
| `routers/burn.py` | 1030 | 9 | Caption burning — overlay generation, batch ops, WebSocket-driven progress, zip download |
| `routers/slideshow.py` | 992 | 23 | Slideshow renderer — image upload, render v1/v2, audio mux, sound prep with beat sync, format presets |
| `routers/pipeline.py` | 986 | 9 | Page intake + provisioning + transitions + workspace + health + R2-presigned uploads + topic-forwarding |
| `routers/video.py` | 963 | 13 | AI video generation — providers, schemas, generate, jobs, bulk download/delete, color correction |
| `routers/telegram.py` | 708 | 30+ | Bot config, posters CRUD, page assignment, sound library, sound sync (Notion/Hub/combined), page playlists, sound-assignments send |
| `routers/recreate.py` | 416 | 4 | Recreate workflow — caption → prompt (LLM) → video generation (WebSocket) |
| `routers/captions.py` | 397 | 4 | TikTok caption scraping (WebSocket-driven) + history + rename |
| `routers/roster.py` | 345 | 9 | Page roster CRUD, project assignment, dedup, sync from Postiz, sync from Notion |
| `routers/projects.py` | 252 | 7 | Project CRUD + stats + legacy import |
| `routers/email_routing.py` | 203 | 8 | Cloudflare email rules CRUD, auto-create per-page alias, destinations |
| `routers/debug.py` | 192 | 6 | Logs (tail + stream), errors, health, job lookup, log clear |
| `routers/upload.py` | 180 | 8 | TT/IG direct upload jobs — submit, list, cancel, cookies, login trigger, queue stats |
| `routers/gdrive.py` | 161 | 7 | Drive folder ops — list, count, upload, batch upload, delete, inventory |

### Services

| Service | Purpose |
|---|---|
| `services/telegram.py` | `telegram_config.json` data layer — posters, sounds, page playlists, message builder (`build_poster_message` produces HTML with clickable song links per poster) |
| `services/notion_pages.py` | Notion PAGES_DB integration — fetch/parse, create intake page, sync into local roster, status updates, email field writeback |
| `services/notion.py` | Notion CAMPAIGNS_DB query — fetches campaigns + TikTok Sound Links for sound matching |
| `services/campaign_hub.py` | Hub-as-source-of-truth + sound matching with AI fuzzy fallback (GPT-4.1-mini bridges naming differences) |
| `services/r2.py` | R2 storage — bucket client, key conventions (`upload_key`, `clip_key`, `account_key`), per-page prefix create + list |
| `services/gdrive.py` | Drive API client — folder ops, file upload/download, OAuth via service account |
| `services/email_routing.py` | Cloudflare API client — list/create/update/delete rules, mint aliases, list destinations |
| `services/email_send.py` | Pipeline → CF destination resolver (`destination_for_pipeline`) |
| `services/poster_router.py` | Phase 1 poster matching — name normalization + fuzzy (first-name + initial) match against `poster_name` from Notion |
| `services/upload.py` | TT/IG upload engine — cookie-based auth, headless browser submit, stealth toggle, job queue, background worker |
| `services/roster.py` | `page_roster.json` data layer |
| `services/slack.py` | Slack incoming webhook — `post_flow_stage_handoff` and generic `post_message` |
| `services/sound_cache.py` | Cached sound metadata (BPM, beat timestamps for slideshow beat-sync) |
| `services/ffmpeg.py` | ffmpeg primitives — color correction, audio mux, format conversion |
| `services/cropper.py` | Video cropping helpers (dual / triptych) |
| `services/captions.py` | OCR + caption extraction helpers |

### Core files

| File | Lines | Purpose |
|---|---|---|
| `app.py` | 297 | FastAPI entry, router registration, lifespan (starts/stops main TG bot), CORS, request logging middleware, static mounts, SPA fallback |
| `telegram_bot.py` | 178 | aiogram send-only bot — `start_bot`, `create_forum_topic`, `send_text_to_topic`, `validate_group`, background Notion sync task |
| `project_manager.py` | 351 | Project CRUD, path utilities, sanitization, default project bootstrap |
| `debug_logger.py` | 234 | Structured logging setup with rotation, in-memory ring buffer for /api/debug |

---

## 6. Frontend

### Tech stack
- React 19 + TypeScript + Vite 7
- Tailwind v4 (`@tailwindcss/vite`, lightningcss for oklch→rgb fallback)
- React Router DOM v7 (URL only — most pages stay mounted, CSS `display: none` for tab switching to preserve state)
- Zustand for global state (`workflowStore`)
- shadcn/ui components (Button, Card, Badge, Input, Dialog, etc.)
- @phosphor-icons/react for icons

### Top-level routes (verified from `frontend/src/App.tsx`)

| Route | Page | What it does |
|---|---|---|
| `/` | `HomePage` | Project dashboard, recent activity, project switcher |
| `/create/*` | `CreatePage` | Content workshop. Internal tabs: **Generate** (AI video), **Clipper** (9:16 cuts), **Recreate** (caption→prompt→video), **Slideshow** (image+audio renders) |
| `/captions/*` | `CaptionsStagePage` | Wraps `CaptionsPage` (TikTok scrape) + `BurnPage` (caption burning onto videos) |
| `/distribute/*` | `DistributionPage` | 5 sub-tabs (see below) |
| `/pipeline` | `PipelinePage` | Kanban view of pages by status + intake form |
| `/pipeline/:integration_id` | `PipelineWorkspacePage` | Per-page workspace — R2 objects, telegram topic, cookie status, alias, Drive folder |

### Distribution sub-tabs (`frontend/src/pages/distribution/`)

| Sub-tab | File | What it does |
|---|---|---|
| **Roster** | `RosterTab.tsx` | Page CRUD, project assignment, sync from Notion, dedup tools |
| **Email** | `EmailTab.tsx` | CF aliases per page, destinations management, single-page assign+sync chain (assigns poster, then immediately syncs the topic) |
| **Telegram** | `TelegramTab.tsx` | **Posters only** — add/remove posters, assign pages, set up topic folders, send sounds (post staging-rip cleanup) |
| **Sounds** | `SoundsTab.tsx` | Sound library — sync from Hub+Notion, manual add/edit, send sound assignments to all posters |
| **Uploads** | `UploadsTab.tsx` | TT/IG direct upload queue — jobs, status, cookie management, login trigger |

The Distribution page has a persistent **StatusBar** at top (3 cards: Bot status, Services, Quick Stats) shared across all sub-tabs.

---

## 7. The Telegram subsystem (post staging-rip)

After issue #39, Telegram is **dramatically slimmer** than it used to be. There is exactly ONE thing the lab does with Telegram: send sound-assignment messages to posters.

### What survives
- **Single send-only main bot** (aiogram). No Dispatcher, no polling, no event handlers.
- **`create_forum_topic`** — used by pipeline setup to make per-page topics in poster supergroups
- **`send_text_to_topic`** — HTML mode, web preview disabled — used to send sound assignments
- **`validate_group`** — used to check that a poster's chat is admin'd + forum-enabled before adding it
- **Background Notion sync task** (`_run_notion_sync`) — every 15 min, pulls active campaigns from Hub, matches them to Notion sound URLs (with AI fuzzy fallback), updates the local sound library

### What was deleted in this PR
| Removed | Why |
|---|---|
| Staging group + topics-per-page in shared group | Was a content CDN — caused two mass-spam incidents |
| Inventory tracking (per-message ledger of staged content) | Tied to staging group |
| Daily forwarding batch | Forwarded staged content to poster groups → mass-spam risk |
| Scan / discover topic features | Pyrogram MTProto client for topic discovery — only needed for staging group |
| Sounds bot (`SOUNDS_BOT_TOKEN`) | Now using main bot for everything — fewer moving parts |
| Destructive ops (`delete_message`) | Not needed without staging; was the failure mode |

### Sound-assignments flow (the only Telegram send the lab does)

```
Campaign Hub  ─┐
               ├─→ services/campaign_hub.sync_sound_status()
Notion CRM   ──┘    ├─ if Notion + Hub names match → use the link
                    └─ else → GPT-4.1-mini fuzzy match
                              └─→ telegram_config.sounds (added/deactivated)
                                  └─→ page_playlists[integration_id] = [sound_id, ...]
                                      └─→ services.telegram.build_poster_message(poster_id)
                                          └─→ HTML message: "🎵 Today's sounds for {pages}"
                                              with clickable song links
                                              └─→ telegram_bot.send_text_to_topic(
                                                      poster.chat_id,
                                                      poster.sounds_topic_id,
                                                      html
                                                  )
```

Triggered by:
- `POST /api/telegram/sound-assignments/send/{poster_id}` — single poster
- `POST /api/telegram/sound-assignments/send-all` — every poster

---

## 8. Persistent state (Railway volume)

All runtime config and data lives on the volume at `RAILWAY_VOLUME_MOUNT_PATH`. Atomic writes (tmp + rename) protect against partial writes during deploy/crash.

| File | Owned by | Contents |
|---|---|---|
| `telegram_config.json` | `services/telegram.py` | bot_token, bot_username, posters[], sounds[], page_playlists{} |
| `page_roster.json` | `services/roster.py` | Postiz integrations + project + Drive folder mappings |
| `email_rules.json` | `services/email_routing.py` | Cloudflare rules (mirror of CF state) |
| `upload_jobs.json` | `services/upload.py` | TT/IG upload queue jobs |
| `projects/<name>/` | `project_manager.py` | Per-project videos, clips, captions, burns, slideshows |

**Source-of-truth boundaries to respect:**
- **Notion PAGES_DB** owns the page roster (canonical). `page_roster.json` is a denormalized cache + per-page extra fields the lab needs (Drive folder, R2 prefix, alias, topic, etc).
- **Notion CAMPAIGNS_DB** owns the TikTok Sound Link per campaign.
- **Campaign Hub** owns "what's active." The lab does not decide; it polls.
- **Cloudflare** owns email routing state. `email_rules.json` is a mirror.
- **Telegram** owns chat/topic IDs. The lab persists references but Telegram is authoritative.
- **R2** owns object storage. The lab persists prefix names, not the objects themselves.

---

## 9. Background workers

There is exactly one background task running in-process (started in `app.py` lifespan):

**`_run_notion_sync`** in `telegram_bot.py`
- Sleeps 30 seconds after boot, then loops every `NOTION_SYNC_INTERVAL` seconds (default 900 = 15 min)
- Calls `services.campaign_hub.sync_sound_status(notion_campaigns=...)`
- If Hub is configured: pulls active campaigns, fetches their Notion sound URLs, updates `telegram_config.sounds` (adds new active sounds, deactivates ones that are no longer active)
- AI fuzzy match (GPT-4.1-mini) bridges naming differences between Hub campaign names and Notion entries

The TT/IG upload queue worker (`services.upload.process_queue`) is started on demand when a job is submitted, not as a long-running loop.

---

## 10. Realtime channels

The lab uses several flavors of realtime, by feature:

| Feature | Mechanism | Endpoint |
|---|---|---|
| Caption scraping progress | WebSocket | `/api/captions/ws/{job_id}` |
| Caption burn progress | WebSocket | `/api/burn/ws` |
| Clipper batch processing | WebSocket | `/api/clipper/ws/{job_id}` |
| Recreate (caption→prompt→video) | WebSocket | `/api/recreate/ws/{job_id}` |
| Video generation status | HTTP polling | `GET /api/video/jobs/{job_id}` |
| Clipper batch status | HTTP polling | `GET /api/clipper/process-batch/{job_id}` |
| Slideshow render status | HTTP polling | `GET /api/slideshow/job/{job_id}` |
| Debug log stream | SSE | `GET /api/debug/stream` |

In-memory job state is lost on restart; files on disk (and Telegram messages, R2 objects, etc.) survive.

---

## 11. Pipeline lifecycle (the King Maker happy path)

```
1. POST /api/pipeline/intake
   ├─ Notion: create page (status="New — Pending Setup", default password)
   ├─ Mint CF email alias (acct-XXXX@risingtidesviral.com → glitch@)
   └─ Returns: integration_id, alias, notion_page_id

2. (manual or auto) POST /api/pipeline/{integration_id}/setup
   ├─ Step 1: write email back to Notion
   ├─ Step 2: resolve poster from page.poster_name
   ├─ Step 3: assign_page_to_poster (telegram_config.json)
   ├─ Step 4: create forum topic in poster's supergroup → set_poster_topic
   ├─ Step 5: r2.create_account_prefix(integration_id)
   └─ Step 6: status flip → "In Production"

3. (during life) Lab generates content via /create tabs
   ├─ Saved under projects/<name>/
   └─ Optionally uploaded to R2 via /api/clipper/r2/upload-init + /upload-complete

4. (daily) POST /api/telegram/sound-assignments/send/{poster_id}
   └─ Personalized HTML message lands in poster's "Campaign Sounds" topic

5. (poster works) Poster sees sound + opens topic for their pages → grabs content from R2/Drive → posts on TikTok

6. (manual) POST /api/pipeline/{integration_id}/transition with new status
   └─ Notion + local mirror updated
```

For Flow Stage, step 2 short-circuits: write email to Notion, post to Slack, flip status to "In Production", done. Steps 3-6 do not apply.

---

## 12. Conventions worth knowing

- **All async** — FastAPI throughout, no sync I/O in route handlers
- **Project-scoped data** — most content endpoints accept `?project=` query param
- **Atomic JSON writes** — every config save uses tmp + rename
- **No DB ORM** — JSON files on the Railway volume; switch to Postgres only if scale demands
- **Bot token priority** — env var (`TELEGRAM_BOT_TOKEN`) beats stored token in `telegram_config.json`. Lets ops override without redeploys.
- **Append-only topic mappings** — `set_poster_topic` does NOT overwrite existing mappings unless `force=True` is passed (prevents accidental remapping bugs)
- **Single-page assign + sync chain** — the Email tab assigns one page at a time and immediately syncs the topic. Batch assigns deliberately do NOT auto-sync (per `routers/telegram.py:726-727` warning) because of race conditions in topic creation when many pages are assigned at once.
- **Frontend tab persistence** — top-level pages use CSS `display: none` for tab switching to preserve in-flight job state across navigation. Only Distribution sub-tabs use URL routing because they're independent.

---

## 13. What lives where (quick lookup table)

| If you need to... | Look in... |
|---|---|
| Add a new AI video provider | `providers/__init__.py` + new module in `providers/` |
| Change how email aliases are minted | `routers/pipeline.py:_mint_random_alias`, `services/email_routing.py` |
| Change pipeline intake fields | `routers/pipeline.py:IntakeRequest`, `services/notion_pages.py:create_intake_page` |
| Change sound-assignment message format | `services/telegram.py:build_poster_message` |
| Add a Notion field to roster pages | `services/notion_pages.py:parse_page` + `RosterPage` type in `frontend/src/types/api.ts` |
| Debug a stuck upload | `services/upload.py:_run_upload_sync`, `routers/upload.py`, `upload_jobs.json` |
| Investigate why a poster isn't getting sounds | `routers/telegram.py:send_assignments_to_poster`, `services/telegram.py:build_poster_message`, `telegram_bot.py:send_text_to_topic` |
| Add a new sub-tab to Distribution | `frontend/src/pages/distribution/`, then wire in `frontend/src/pages/Distribution.tsx` |

---

## 14. Known gaps and rough edges

- **5 active campaigns** consistently fail Notion sound matching as of last sync (Liam St John, In Color, Alex Nicol, Gregory Alan Isakov, Matilda Lyn) — either missing Sound Links in Notion or names diverge enough that AI matcher fails
- **Drive → R2 migration** is incomplete — older King Maker pages still have `drive_folder_url` set. Both code paths exist; new pages get R2, old pages keep Drive until manually migrated
- **Pyrogram dependency** may still be in `requirements.txt` even though no code uses it after the staging-rip — worth removing
- **`CLAUDE.md` is stale** — references Postiz, FAL, Luma, sounds bot, staging group, daily batch — none exist
- **`page_roster.json` and Notion PAGES_DB** can drift — `POST /api/roster/sync-notion` is the resync mechanism but is manual

---

## 15. Where to look first when something breaks

| Symptom | First place to check |
|---|---|
| Bot won't send | `/api/telegram/status` — `bot_running: false` means token missing or invalid |
| Sound sync ran 0 changes | Hub config → Notion config → name matching log lines (`logger.info("sound sync: ...")`) |
| Pipeline intake fails | Notion API permissions for PAGES_DB + CF Email Routing has verified destination for the pipeline |
| Email alias minted but nobody got it | CF rules list (`/api/email/rules`) + the destination address verification status |
| Upload job stuck "queued" | `services/upload.py:process_queue` and cookie status (`/api/upload/cookies`) |
| Topic creation fails | Bot must be admin in the poster's supergroup AND the group must have `is_forum: true` |
| Frontend has stale data | The `display: none` tab persistence may be holding old state — check workflowStore + page-level useEffect deps |
