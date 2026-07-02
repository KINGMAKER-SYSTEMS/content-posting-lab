-- Durable job model.
--
-- The old app tracked generate/clip/burn jobs in in-memory dicts that vanished
-- on restart (a deploy mid-batch lost all status, leaving the UI polling a job
-- that no longer existed). Here a job is a row, so progress survives restarts
-- and the UI can always reconcile.

CREATE TABLE jobs (
    id          TEXT PRIMARY KEY,
    project     TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('clip','burn','generate')),
    status      TEXT NOT NULL CHECK (status IN ('queued','processing','done','failed')),
    progress    REAL NOT NULL DEFAULT 0.0,   -- 0.0..1.0
    -- The asset this job consumes (clip: the source; burn: the clip). Optional.
    source_asset_id TEXT REFERENCES assets(id) ON DELETE SET NULL,
    -- JSON request params (clip length, trim range, caption/style, etc.)
    params      TEXT,
    -- JSON array of asset ids this job produced.
    result_asset_ids TEXT NOT NULL DEFAULT '[]',
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX idx_jobs_project ON jobs(project, kind);
CREATE INDEX idx_jobs_status  ON jobs(status);
