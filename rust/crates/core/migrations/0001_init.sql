-- Content Lab — initial schema.
--
-- Replaces telegram_config.json + page_roster.json (the JSON blobs the old app
-- rewrote in full on every mutation, with no locking → lost updates). A real
-- relational schema makes the buggy "recovery" endpoints (scan-inventory,
-- discover-topics, dedup, audit-dupes) unnecessary by construction.

-- ── Content production ───────────────────────────────────────────────────────

CREATE TABLE projects (
    name        TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL
);

-- Every piece of media is a durable row with a stable id + lineage.
-- generated ──clip──▶ clip ──burn──▶ burned   (parent_id chains them)
CREATE TABLE assets (
    id          TEXT PRIMARY KEY,
    project     TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('generated','clip','burned','source')),
    status      TEXT NOT NULL CHECK (status IN ('queued','processing','ready','failed')),
    path        TEXT,                 -- project-relative path / R2 key; NULL until ready
    parent_id   TEXT REFERENCES assets(id) ON DELETE SET NULL,
    meta        TEXT,                 -- JSON: provider, prompt, clip index, etc.
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX idx_assets_project_kind ON assets(project, kind);
CREATE INDEX idx_assets_status       ON assets(status);
CREATE INDEX idx_assets_parent       ON assets(parent_id);

-- Scraped caption bank. First-class rows (not a positional CSV) so a future
-- version can pair captions to clips by mood/intent instead of `i % len`.
CREATE TABLE captions (
    id          TEXT PRIMARY KEY,
    project     TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
    source      TEXT NOT NULL,        -- TikTok username scraped from
    text        TEXT NOT NULL,
    mood        TEXT,                 -- sad|hype|love|funny|chill (optional)
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_captions_project ON captions(project, source);

-- ── Distribution (Telegram) ──────────────────────────────────────────────────
-- The old config tangled posters, topics, inventory, sounds and schedule into
-- one nested JSON object. Here they are normalized tables with real foreign
-- keys, so an update to one never rewrites (and clobbers) the others.

-- A page = one TikTok/IG account we run content for (the roster).
-- `project` is a SOFT reference (plain column, not a FK): pages are synced from
-- Notion independently of content projects and must not require a project to
-- exist first. The app links them when both are present.
CREATE TABLE pages (
    id            TEXT PRIMARY KEY,   -- stable account id (was Postiz integration_id)
    name          TEXT NOT NULL,
    project       TEXT,
    r2_prefix     TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- A poster = a person who runs a set of pages, with their own Telegram group.
CREATE TABLE posters (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    chat_id         INTEGER NOT NULL,            -- their supergroup
    sounds_topic_id INTEGER,                     -- "Campaign Sounds" topic
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Which poster owns which page (a page has exactly one poster).
CREATE TABLE poster_pages (
    poster_id   TEXT NOT NULL REFERENCES posters(id) ON DELETE CASCADE,
    page_id     TEXT NOT NULL REFERENCES pages(id)   ON DELETE CASCADE,
    PRIMARY KEY (poster_id, page_id)
);

-- Forum-topic mappings. Real, stored topic ids — NEVER discovered by
-- brute-forcing message_thread_id ranges (that probe-and-delete behavior is
-- exactly the destructive bug we're killing). scope = 'staging' (shared group)
-- or 'poster' (a poster's own group).
CREATE TABLE topics (
    scope       TEXT NOT NULL CHECK (scope IN ('staging','poster')),
    owner_id    TEXT NOT NULL,        -- '' for staging, else poster_id
    page_id     TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    topic_id    INTEGER NOT NULL,
    topic_name  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (scope, owner_id, page_id)
);

-- Content inventory: ONE row per real Telegram message we placed. The old app
-- inferred content by forwarding every integer message_id in a range and
-- advancing a single shared watermark even on failed forwards → silent loss +
-- double-sends. Here each delivered item is an explicit row, and forwarding is
-- tracked per-row, never by range.
CREATE TABLE telegram_inventory (
    id              TEXT PRIMARY KEY,
    page_id         TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    asset_id        TEXT REFERENCES assets(id) ON DELETE SET NULL,
    staging_msg_id  INTEGER,          -- message id in the staging topic
    forwarded_to    TEXT,             -- poster_id it was forwarded to (NULL = pending)
    forwarded_msg_id INTEGER,
    created_at      TEXT NOT NULL,
    forwarded_at    TEXT
);
CREATE INDEX idx_inv_page      ON telegram_inventory(page_id);
CREATE INDEX idx_inv_pending   ON telegram_inventory(page_id, forwarded_to);

-- Sound library (TikTok sound URLs synced from Campaign Hub + Notion).
CREATE TABLE sounds (
    id          TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    label       TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

-- Per-page sound playlist (ordered). Replaces page_playlists{} in the JSON.
CREATE TABLE page_playlists (
    page_id     TEXT NOT NULL REFERENCES pages(id)  ON DELETE CASCADE,
    sound_id    TEXT NOT NULL REFERENCES sounds(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    PRIMARY KEY (page_id, sound_id)
);

-- Single-row distribution schedule (id = 1).
CREATE TABLE schedule (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    enabled       INTEGER NOT NULL DEFAULT 0,
    forward_time  TEXT NOT NULL DEFAULT '09:00',
    timezone      TEXT NOT NULL DEFAULT 'America/New_York',
    last_run      TEXT
);
INSERT INTO schedule (id, enabled) VALUES (1, 0);
