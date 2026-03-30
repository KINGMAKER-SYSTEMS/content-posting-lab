# Handoff: Telegram Bot Content Distribution Pipeline

**Date:** 2026-03-30
**Branch:** `claude/amazing-cray`
**Last commit:** `e4d3bcc` — `feat: add Telegram bot content distribution pipeline`
**Repo:** `KINGMAKER-SYSTEMS/content-posting-lab`

---

## What Was Built

A full Telegram bot integration that turns Telegram forum topics into a content staging + distribution system. Two-layer architecture:

1. **Staging Group** (agency-owned) — one topic per roster page, bot watches for manual media drops, tracks everything as inventory
2. **Poster Groups** (one per account holder) — videos forwarded daily from staging, sounds sent to General topic, fire-and-forget for posters

### Files Created

| File | Lines | Purpose |
|------|-------|---------|
| `services/telegram.py` | 408 | Data layer — config, inventory, posters, sounds, schedule (mirrors roster.py pattern) |
| `telegram_bot.py` | 465 | aiogram v3 bot worker — lifecycle, media handler, scheduler, utility functions |
| `routers/telegram.py` | 550 | 22 FastAPI endpoints — config, staging, posters, inventory, sounds, schedule, batch |
| `frontend/src/pages/Telegram.tsx` | ~930 | Full UI — 6 sections: bot config, staging group, posters, sounds, schedule, send |

### Files Modified

| File | Change |
|------|--------|
| `app.py` | Import + mount telegram router, start/stop bot in lifespan |
| `frontend/src/App.tsx` | Added Telegram tab to nav + CSS display switching |
| `frontend/src/types/api.ts` | Added 10 Telegram TypeScript interfaces |
| `requirements.txt` | Added `aiogram>=3.4` |
| `.gitignore` | Added `telegram_config.json` |

---

## Architecture

```
telegram_config.json (filesystem, atomic writes)
├── bot_token / bot_username
├── staging_group
│   ├── chat_id → Telegram supergroup
│   └── topics: { integration_id → { topic_id, topic_name } }
├── posters
│   ├── poster_id (slugified name)
│   │   ├── chat_id → poster's Telegram supergroup
│   │   ├── page_ids: [integration_id, ...]
│   │   └── topics: { integration_id → { topic_id, topic_name } }
├── inventory
│   └── { integration_id → [ { id, message_id, file_id, source, forwarded: {} } ] }
├── sounds: [ { id, url, label, active } ]
└── schedule: { enabled, forward_time, timezone, last_run }
```

**Routing flow:** `integration_id` (from Postiz roster) → staging topic → inventory → forward to poster's topic

**Bot runs as:** `asyncio.create_task()` inside FastAPI's event loop (aiogram v3 polling). Scheduler is a second async task that sleeps until configured forward time.

---

## Key Design Decisions

1. **aiogram v3 over python-telegram-bot** — async-native, no event loop conflicts with FastAPI
2. **Inventory watches staging topics** — bot handler catches all media dropped manually into staging topics, so inventory stays accurate regardless of how content arrives
3. **Dedup on message_id** — prevents double-inventory on bot restart/update replay
4. **Slug collision protection** — poster IDs auto-suffix `-2`, `-3` if name slugs collide
5. **Stale topic clearing** — changing staging group chat_id wipes old topic mappings
6. **Fire-and-forget for posters** — no emoji reactions, no deletion tracking. Posters delete after posting to TikTok, re-forward from staging if needed
7. **Sounds separate from videos** — sounds go to poster's General topic, videos go to page-specific topics. No pairing logic in V1 (future: campaign hub API)

---

## What's NOT Built Yet

1. **Campaign hub API integration** — sounds are manual input. Future: pull active sounds from the railway campaign tracking hub
2. **Sound-to-video pairing** — currently all posters get all sounds. Future: campaign-aware assignment based on which pages are on which campaigns
3. **Inventory cleanup** — no cap on inventory list size. May need periodic pruning for long-running deployments
4. **Tests** — no backend tests for telegram router/service, no frontend tests for Telegram.tsx

---

## To Test

1. `pip install aiogram` (or `pip install -r requirements.txt`)
2. Create bot via @BotFather → get token
3. Create Telegram supergroup → enable Topics → add bot as admin → get chat_id
4. Start app: `python app.py`
5. Go to Telegram tab in UI:
   - Paste bot token → Save → should show "Connected"
   - Paste staging group chat_id → Set Group → should show "Forum" + "Admin" badges
   - Click "Sync Topics" → should create one topic per roster page
   - Send a test video via "Send to Staging"
   - Create a poster, assign pages, forward content
   - Try "Run Batch Now"

---

## Publish Feature Plan (Previous Work)

The Publish feature phases 1-4 from the previous handoff are still valid and independent of this Telegram work:
- Phase 1: Roster table redesign (cards → data table)
- Phase 2: Cloudflare email routing
- Phase 3: TikTok upload engine
- Phase 4: Queue & scheduling

Archived previous handoff: `HANDOFF-publish-plan-2026-03-25.md`

---

## Next Steps

1. **Smoke test** — configure bot, staging group, posters, send/forward content end-to-end
2. **Fix any Telegram API quirks** — permissions, topic creation rate limits, message forwarding edge cases
3. **Campaign hub integration** — connect to railway deployment for automated sound assignment
4. **Continue Publish phases** — roster table redesign (Phase 1) is independent and ready to build
