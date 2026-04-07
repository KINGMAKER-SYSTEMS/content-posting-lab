# Telegram Bot Distribution Pipeline — Setup Guide

## Overview

The system uses two layers of Telegram groups to stage and distribute content:

```
STAGING GROUP (you own this)          POSTER GROUPS (one per account holder)
+---------------------------------+   +---------------------------+
| General topic                   |   | General topic             |
| [Page A] topic  ← upload here   |→→| [Page A] topic ← lands here
| [Page B] topic  ← upload here   |→→| [Page B] topic ← lands here
| [Page C] topic                  |   +---------------------------+
+---------------------------------+
                                      +---------------------------+
                                  →→→ | Another poster's group    |
                                      | [Page A] topic            |
                                      +---------------------------+
```

**Staging group** = your agency's content vault. One topic per roster page. You (or your team) drop videos into the right topic manually.

**Poster groups** = one group per person who posts to TikTok. The bot forwards videos from staging into their group, organized by page. They post and delete.

---

## Prerequisites

- The app running: `python app.py` → `http://127.0.0.1:8000`
- At least one page in your Roster (synced from Postiz)
- A Telegram account that can create groups

---

## Step 1: Create the Bot

1. Open Telegram → search for **@BotFather**
2. Send `/newbot`
3. Choose a name (e.g., "Content Lab Bot")
4. Choose a username (e.g., `content_lab_dev_bot`) — must end in `bot`
5. BotFather gives you a token like: `7123456789:AAH_some_long_string_here`
6. **Copy the full token**

### Configure the token in the app

1. Open the app → **Telegram** tab
2. Paste the token into the "Bot Token" field
3. Click **Save**
4. You should see: **Connected** with the bot's username displayed

> The token is stored in `telegram_config.json` at the project root (gitignored).

---

## Step 2: Create the Staging Group

This is the group where your team uploads raw content.

### Create the group

1. In Telegram, tap **New Group**
2. Add your bot (`@content_lab_dev_bot` or whatever you named it) as a member
3. Name it something like "Content Staging"
4. After creation, tap the group name → **Edit** (pencil icon)
5. Scroll down → **Topics** → toggle **ON**
   - This converts the group to a "Forum" supergroup
   - You'll see a "General" topic appear automatically

### Promote the bot to Admin

1. In the group → tap group name → **Members**
2. Find the bot → tap → **Promote to Admin**
3. Enable these permissions:
   - **Manage Topics** (required — bot creates topics per page)
   - **Post Messages** (required — bot sends content to topics)
   - **Change Group Info** (recommended)
4. Save

### Get the chat ID

The chat ID is a negative number like `-1001234567890`. To find it:

**Option A — Use @RawDataBot:**
1. Add `@RawDataBot` to the group temporarily
2. It will post a JSON blob — look for `"id": -100XXXXXXXXXX`
3. Copy that number (including the minus sign)
4. Remove @RawDataBot from the group

**Option B — Use the Telegram API directly:**
1. Send any message in the group
2. Open: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Find the message — the `chat.id` field is your chat ID

### Set the staging group in the app

1. In the app → Telegram tab → **Staging Group** section
2. Paste the chat ID (e.g., `-1001234567890`)
3. Click **Set Group**
4. You should see two green badges: **Forum** and **Admin**
   - If you see errors, the bot isn't admin or topics aren't enabled

### Sync topics

1. Click **Sync Topics**
2. The bot creates one topic in the staging group for each page in your Roster
3. Topics are named: `Page Name (provider)` — e.g., "Backroad Videos (tiktok)"
4. You'll see the topic list appear in the UI with inventory counts (all zero initially)

> If you add new pages to your Roster later, click Sync Topics again. It only creates missing topics — existing ones are untouched.

---

## Step 3: Create Poster Groups

Each person who posts content to TikTok gets their own Telegram group.

### For each poster:

1. In Telegram, tap **New Group**
2. Add the bot as a member
3. Name it for the poster (e.g., "Jake — Content Drop")
4. Enable **Topics** (same as staging: Edit → Topics → ON)
5. Promote the bot to **Admin** with same permissions:
   - Manage Topics
   - Post Messages
6. Get the chat ID (same method as above)

### Register the poster in the app

1. Telegram tab → **Posters** section
2. Click **Add Poster**
3. Enter:
   - **Name**: poster's name (e.g., "Jake")
   - **Chat ID**: their group's chat ID (e.g., `-1001987654321`)
4. **Assign pages**: check which roster pages this poster handles
   - e.g., if Jake posts for "Backroad Videos" and "Editorial Stills", check both
5. Click **Save**
6. The bot auto-creates topics in the poster's group — one per assigned page

### Add the poster to their group

The poster (the actual human) needs to be a member of their group to see forwarded content. Add them manually via Telegram.

---

## Step 4: Upload Content to Staging

Now the pipeline is ready. To stage content:

1. Open the **staging group** in Telegram
2. Navigate to the topic for the page you want (e.g., "Backroad Videos (tiktok)")
3. **Drop a video** (or photo/document) into that topic
4. The bot detects it automatically and adds it to inventory

You can verify in the app:
- Telegram tab → Staging section shows inventory counts per topic
- Numbers update: `total` goes up, `pending` shows items not yet forwarded

> You can also send content via the API: **POST /api/telegram/staging/{integration_id}/send** with a file upload. But manual drops into the Telegram topic are the primary workflow.

---

## Step 5: Forward Content to Posters

### Manual forwarding

In the app → Telegram tab → click **Forward** next to a poster. This forwards all pending inventory for their assigned pages into their group topics.

### Scheduled daily forwarding

1. Telegram tab → **Schedule** section
2. Set the **time** (e.g., `09:00`)
3. Set the **timezone** (e.g., `America/New_York`)
4. Toggle **Enabled** → ON
5. Click **Save**

Every day at that time, the bot:
1. Forwards all pending videos to each poster's page-specific topics
2. Sends active sound URLs to each poster's General topic
3. Posts a summary message: "Daily Content Drop — 5 new videos, 2 sounds"

### Run batch manually

Click **Run Batch Now** to trigger the daily batch immediately (useful for testing).

---

## Entity Map — All IDs at a Glance

```
telegram_config.json
│
├── bot_token: "7123456789:AAH_..."        ← from @BotFather
├── bot_username: "content_lab_dev_bot"     ← auto-detected on connect
│
├── staging_group
│   ├── chat_id: -1001234567890            ← your staging supergroup
│   └── topics
│       ├── "integration-id-1"             ← roster page ID (from Postiz)
│       │   ├── topic_id: 12              ← message_thread_id in staging group
│       │   └── topic_name: "Backroad (tiktok)"
│       └── "integration-id-2"
│           ├── topic_id: 14
│           └── topic_name: "Editorial (instagram)"
│
├── posters
│   └── "jake"                             ← slugified poster name
│       ├── name: "Jake"
│       ├── chat_id: -1001987654321        ← jake's poster supergroup
│       ├── page_ids: ["integration-id-1"] ← which pages jake handles
│       └── topics
│           └── "integration-id-1"
│               ├── topic_id: 8            ← message_thread_id in jake's group
│               └── topic_name: "Backroad"
│
├── inventory
│   └── "integration-id-1": [              ← one array per page
│       {
│         id: "a1b2c3d4e5f6",             ← app-generated UUID
│         message_id: 99999,               ← telegram msg ID in staging topic
│         file_id: "AgACAgIAAxkB...",      ← telegram's media reference
│         media_type: "video",
│         file_name: "clip_001.mp4",
│         caption: null,
│         source: "manual",                ← dropped into telegram topic
│         added_at: "2026-03-30T...",
│         forwarded: {                     ← empty = pending, filled = sent
│           "jake": {
│             poster_id: "jake",
│             message_id: 88888,           ← new msg ID in jake's group
│             forwarded_at: "2026-03-30T..."
│           }
│         }
│       }
│     ]
│
├── sounds: [                              ← manual sound list
│     { id: "...", url: "https://...", label: "Summer Vibe", active: true }
│   ]
│
└── schedule
    ├── enabled: true
    ├── forward_time: "09:00"
    ├── timezone: "America/New_York"
    └── last_run: "2026-03-30T13:00:00Z"
```

---

## Permissions Checklist

| Entity | Group | Role | Required Permissions |
|--------|-------|------|---------------------|
| **Bot** | Staging group | Admin | Manage Topics, Post Messages |
| **Bot** | Each poster group | Admin | Manage Topics, Post Messages |
| **You/team** | Staging group | Member+ | Upload media to topics |
| **Poster (human)** | Their poster group | Member | View forwarded content |

The bot does NOT need:
- Delete messages
- Ban users
- Invite users via link
- Pin messages
- Manage voice chats

---

## Troubleshooting

**"Group must be a forum"** — Topics aren't enabled. Edit group → Topics → ON.

**"Bot is not an administrator"** — Promote bot to admin in that group.

**Sync Topics does nothing** — Your Roster has no pages. Sync Roster from Postiz first.

**Videos not appearing in inventory** — Check that:
1. You're uploading to a topic that was created by Sync Topics (not General)
2. The bot is still an admin in the staging group
3. The app is running (`python app.py`) — the bot polls for messages while the server is up

**Forward fails silently** — The poster's group may have been deleted, or the bot was removed. Re-validate the poster's chat ID.

**Scheduler not firing** — The scheduler runs as an async task inside the app process. If you restart the app, the scheduler restarts too. Check `last_run` in the UI.
