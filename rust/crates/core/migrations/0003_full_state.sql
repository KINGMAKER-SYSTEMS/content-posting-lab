-- Wave 0: absorb ALL remaining JSON state, so `clab migrate` can retire
-- telegram_config.json / page_roster.json / content_requests.json in one move.
-- (email rules live in Cloudflare's API, not local state — no table needed.)

-- ── App settings (key/value) ────────────────────────────────────────────────
-- The odds and ends of telegram_config.json that aren't rows: staging group
-- identity, bot username, stored bot token (env var still beats it at runtime).
CREATE TABLE settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- ── Pages: full roster shape ────────────────────────────────────────────────
-- Columns for what the app queries/joins; everything else (email alias,
-- credentials, group labels, pipeline echo-fields from Notion) rides in
-- `extra` as JSON so the migration is lossless without 20 dead columns.
ALTER TABLE pages ADD COLUMN provider         TEXT;
ALTER TABLE pages ADD COLUMN poster_name      TEXT;   -- Notion "Poster" assignment
ALTER TABLE pages ADD COLUMN drive_folder_url TEXT;
ALTER TABLE pages ADD COLUMN drive_folder_id  TEXT;
ALTER TABLE pages ADD COLUMN r2_bucket        TEXT;
ALTER TABLE pages ADD COLUMN source           TEXT;   -- 'notion' = synced, else manual
ALTER TABLE pages ADD COLUMN status           TEXT;   -- Notion pipeline status
ALTER TABLE pages ADD COLUMN notion_page_id   TEXT;
ALTER TABLE pages ADD COLUMN tiktok_url       TEXT;
ALTER TABLE pages ADD COLUMN extra            TEXT;   -- JSON: remaining roster fields

-- ── Poster ↔ Telegram user bindings (Mini App auth) ────────────────────────
-- Replaces posters[].telegram_user_ids. UNIQUE on user id: a Telegram user
-- acts as exactly one poster (the old first-match-in-dict-order resolution
-- was nondeterministic when a uid appeared twice).
CREATE TABLE poster_users (
    poster_id        TEXT    NOT NULL REFERENCES posters(id) ON DELETE CASCADE,
    telegram_user_id INTEGER NOT NULL UNIQUE,
    PRIMARY KEY (poster_id, telegram_user_id)
);

-- Replaces posters[].telegram_username (single optional @handle).
ALTER TABLE posters ADD COLUMN telegram_username TEXT;

-- ── Inventory media details ─────────────────────────────────────────────────
-- file_id/file_name/media_type/caption/source from the old inventory items.
-- file_id matters: it lets the bot re-send media without re-uploading.
ALTER TABLE telegram_inventory ADD COLUMN meta TEXT;

-- ── Content requests (Mini App → agent queue) ───────────────────────────────
CREATE TABLE content_requests (
    id           TEXT PRIMARY KEY,
    poster_id    TEXT NOT NULL,
    poster_name  TEXT,
    page_id      TEXT,
    page_name    TEXT,
    text         TEXT NOT NULL,
    quantity     INTEGER,
    status       TEXT NOT NULL DEFAULT 'open'
                 CHECK (status IN ('open','in_progress','fulfilled','cancelled')),
    source       TEXT NOT NULL DEFAULT 'miniapp',
    agent_note   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    fulfilled_at TEXT
);
CREATE INDEX idx_requests_status ON content_requests(status, created_at);
CREATE INDEX idx_requests_poster ON content_requests(poster_id, created_at);
