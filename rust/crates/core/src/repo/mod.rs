//! Typed repositories over [`Db`].
//!
//! Reads use the reader pool; writes use the writer pool. Queries are plain
//! `sqlx::query_as` (runtime-checked) rather than the compile-time `query!`
//! macro, so the workspace builds without a live DATABASE_URL or a checked-in
//! `.sqlx/` cache — simpler for an early-stage project. We can tighten to
//! compile-time checking once the schema settles.

use chrono::Utc;
use uuid::Uuid;

use crate::db::Db;
use crate::model::{
    Asset, AssetKind, AssetStatus, InventoryItem, Job, JobKind, JobStatus, Page, Poster, Project,
};

/// Generate a short, stable, URL-safe id (12 hex chars — matches the old
/// `_gen_id()` convention so nothing downstream is surprised).
pub fn new_id() -> String {
    Uuid::new_v4().simple().to_string()[..12].to_string()
}

// ── Projects ────────────────────────────────────────────────────────────────

pub async fn list_projects(db: &Db) -> Result<Vec<Project>, sqlx::Error> {
    sqlx::query_as::<_, Project>("SELECT name, created_at FROM projects ORDER BY name")
        .fetch_all(db.reader())
        .await
}

pub async fn create_project(db: &Db, name: &str) -> Result<Project, sqlx::Error> {
    let now = Utc::now();
    sqlx::query("INSERT INTO projects (name, created_at) VALUES (?, ?) ON CONFLICT(name) DO NOTHING")
        .bind(name)
        .bind(now)
        .execute(db.writer())
        .await?;
    Ok(Project {
        name: name.to_string(),
        created_at: now,
    })
}

// ── Assets ───────────────────────────────────────────────────────────────────

/// Insert a new asset row (typically status = queued, path = None).
pub async fn create_asset(
    db: &Db,
    project: &str,
    kind: AssetKind,
    parent_id: Option<&str>,
    meta: Option<&str>,
) -> Result<Asset, sqlx::Error> {
    let now = Utc::now();
    let id = new_id();
    sqlx::query(
        "INSERT INTO assets (id, project, kind, status, path, parent_id, meta, error, created_at, updated_at)
         VALUES (?, ?, ?, ?, NULL, ?, ?, NULL, ?, ?)",
    )
    .bind(&id)
    .bind(project)
    .bind(kind)
    .bind(AssetStatus::Queued)
    .bind(parent_id)
    .bind(meta)
    .bind(now)
    .bind(now)
    .execute(db.writer())
    .await?;

    get_asset(db, &id)
        .await?
        .ok_or_else(|| sqlx::Error::RowNotFound)
}

pub async fn get_asset(db: &Db, id: &str) -> Result<Option<Asset>, sqlx::Error> {
    sqlx::query_as::<_, Asset>("SELECT * FROM assets WHERE id = ?")
        .bind(id)
        .fetch_optional(db.reader())
        .await
}

/// List a project's assets, optionally filtered by kind. Newest first.
pub async fn list_assets(
    db: &Db,
    project: &str,
    kind: Option<AssetKind>,
) -> Result<Vec<Asset>, sqlx::Error> {
    match kind {
        Some(k) => {
            sqlx::query_as::<_, Asset>(
                "SELECT * FROM assets WHERE project = ? AND kind = ? ORDER BY created_at DESC",
            )
            .bind(project)
            .bind(k)
            .fetch_all(db.reader())
            .await
        }
        None => {
            sqlx::query_as::<_, Asset>(
                "SELECT * FROM assets WHERE project = ? ORDER BY created_at DESC",
            )
            .bind(project)
            .fetch_all(db.reader())
            .await
        }
    }
}

/// Mark an asset ready with its final file path.
pub async fn mark_asset_ready(db: &Db, id: &str, path: &str) -> Result<(), sqlx::Error> {
    sqlx::query("UPDATE assets SET status = ?, path = ?, updated_at = ? WHERE id = ?")
        .bind(AssetStatus::Ready)
        .bind(path)
        .bind(Utc::now())
        .bind(id)
        .execute(db.writer())
        .await?;
    Ok(())
}

/// Mark an asset failed with an error message.
pub async fn mark_asset_failed(db: &Db, id: &str, error: &str) -> Result<(), sqlx::Error> {
    sqlx::query("UPDATE assets SET status = ?, error = ?, updated_at = ? WHERE id = ?")
        .bind(AssetStatus::Failed)
        .bind(error)
        .bind(Utc::now())
        .bind(id)
        .execute(db.writer())
        .await?;
    Ok(())
}

/// Transition an asset to processing (a job picked it up).
pub async fn mark_asset_processing(db: &Db, id: &str) -> Result<(), sqlx::Error> {
    sqlx::query("UPDATE assets SET status = ?, updated_at = ? WHERE id = ?")
        .bind(AssetStatus::Processing)
        .bind(Utc::now())
        .bind(id)
        .execute(db.writer())
        .await?;
    Ok(())
}

// ── Jobs ───────────────────────────────────────────────────────────────────

pub async fn create_job(
    db: &Db,
    project: &str,
    kind: JobKind,
    source_asset_id: Option<&str>,
    params: Option<&str>,
) -> Result<Job, sqlx::Error> {
    let now = Utc::now();
    let id = new_id();
    sqlx::query(
        "INSERT INTO jobs (id, project, kind, status, progress, source_asset_id, params, result_asset_ids, error, created_at, updated_at)
         VALUES (?, ?, ?, ?, 0.0, ?, ?, '[]', NULL, ?, ?)",
    )
    .bind(&id)
    .bind(project)
    .bind(kind)
    .bind(JobStatus::Queued)
    .bind(source_asset_id)
    .bind(params)
    .bind(now)
    .bind(now)
    .execute(db.writer())
    .await?;

    get_job(db, &id).await?.ok_or(sqlx::Error::RowNotFound)
}

pub async fn get_job(db: &Db, id: &str) -> Result<Option<Job>, sqlx::Error> {
    sqlx::query_as::<_, Job>("SELECT * FROM jobs WHERE id = ?")
        .bind(id)
        .fetch_optional(db.reader())
        .await
}

pub async fn list_jobs(db: &Db, project: &str) -> Result<Vec<Job>, sqlx::Error> {
    sqlx::query_as::<_, Job>("SELECT * FROM jobs WHERE project = ? ORDER BY created_at DESC")
        .bind(project)
        .fetch_all(db.reader())
        .await
}

/// Update progress (0.0..1.0) and flip to processing if still queued.
///
/// Guarded with `status NOT IN ('done','failed')`: progress writes are fired
/// from detached tasks and can land *after* finish_job/fail_job. Without the
/// guard a late write would knock a finished job back to <100%. Once terminal,
/// this is a no-op.
pub async fn update_job_progress(db: &Db, id: &str, progress: f64) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE jobs SET progress = ?, status = CASE WHEN status = 'queued' THEN 'processing' ELSE status END, updated_at = ?
         WHERE id = ? AND status NOT IN ('done','failed')",
    )
    .bind(progress.clamp(0.0, 1.0))
    .bind(Utc::now())
    .bind(id)
    .execute(db.writer())
    .await?;
    Ok(())
}

/// Mark a job done with the list of produced asset ids.
pub async fn finish_job(db: &Db, id: &str, result_asset_ids: &[String]) -> Result<(), sqlx::Error> {
    let json = serde_json::to_string(result_asset_ids).unwrap_or_else(|_| "[]".to_string());
    sqlx::query(
        "UPDATE jobs SET status = ?, progress = 1.0, result_asset_ids = ?, updated_at = ? WHERE id = ?",
    )
    .bind(JobStatus::Done)
    .bind(json)
    .bind(Utc::now())
    .bind(id)
    .execute(db.writer())
    .await?;
    Ok(())
}

pub async fn fail_job(db: &Db, id: &str, error: &str) -> Result<(), sqlx::Error> {
    sqlx::query("UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?")
        .bind(JobStatus::Failed)
        .bind(error)
        .bind(Utc::now())
        .bind(id)
        .execute(db.writer())
        .await?;
    Ok(())
}

/// Startup recovery: jobs (and the assets they were producing) that were
/// running when the process died are orphaned — their detached tokio tasks are
/// gone and can never reach a terminal state. Mark them failed so clients stop
/// polling forever. Call once on boot, before serving. Returns (#jobs, #assets)
/// reclaimed.
pub async fn recover_orphaned(db: &Db) -> Result<(u64, u64), sqlx::Error> {
    let now = Utc::now();
    let jobs = sqlx::query(
        "UPDATE jobs SET status = 'failed', error = 'interrupted by server restart', updated_at = ?
         WHERE status IN ('queued','processing')",
    )
    .bind(now)
    .execute(db.writer())
    .await?
    .rows_affected();

    let assets = sqlx::query(
        "UPDATE assets SET status = 'failed', error = 'interrupted by server restart', updated_at = ?
         WHERE status IN ('queued','processing')",
    )
    .bind(now)
    .execute(db.writer())
    .await?
    .rows_affected();

    Ok((jobs, assets))
}

// ── Pages (roster) ───────────────────────────────────────────────────────────

pub async fn list_pages(db: &Db) -> Result<Vec<Page>, sqlx::Error> {
    sqlx::query_as::<_, Page>("SELECT * FROM pages ORDER BY name")
        .fetch_all(db.reader())
        .await
}

pub async fn get_page(db: &Db, id: &str) -> Result<Option<Page>, sqlx::Error> {
    sqlx::query_as::<_, Page>("SELECT * FROM pages WHERE id = ?")
        .bind(id)
        .fetch_optional(db.reader())
        .await
}

/// Upsert a page (roster entry).
pub async fn upsert_page(
    db: &Db,
    id: &str,
    name: &str,
    project: Option<&str>,
) -> Result<Page, sqlx::Error> {
    let now = Utc::now();
    sqlx::query(
        "INSERT INTO pages (id, name, project, r2_prefix, created_at, updated_at)
         VALUES (?, ?, ?, NULL, ?, ?)
         ON CONFLICT(id) DO UPDATE SET name = excluded.name, project = excluded.project, updated_at = excluded.updated_at",
    )
    .bind(id)
    .bind(name)
    .bind(project)
    .bind(now)
    .bind(now)
    .execute(db.writer())
    .await?;
    get_page(db, id).await?.ok_or(sqlx::Error::RowNotFound)
}

// ── Posters ────────────────────────────────────────────────────────────────

pub async fn list_posters(db: &Db) -> Result<Vec<Poster>, sqlx::Error> {
    sqlx::query_as::<_, Poster>("SELECT * FROM posters ORDER BY name")
        .fetch_all(db.reader())
        .await
}

pub async fn get_poster(db: &Db, id: &str) -> Result<Option<Poster>, sqlx::Error> {
    sqlx::query_as::<_, Poster>("SELECT * FROM posters WHERE id = ?")
        .bind(id)
        .fetch_optional(db.reader())
        .await
}

pub async fn upsert_poster(
    db: &Db,
    id: &str,
    name: &str,
    chat_id: i64,
) -> Result<Poster, sqlx::Error> {
    let now = Utc::now();
    sqlx::query(
        "INSERT INTO posters (id, name, chat_id, sounds_topic_id, created_at, updated_at)
         VALUES (?, ?, ?, NULL, ?, ?)
         ON CONFLICT(id) DO UPDATE SET name = excluded.name, chat_id = excluded.chat_id, updated_at = excluded.updated_at",
    )
    .bind(id)
    .bind(name)
    .bind(chat_id)
    .bind(now)
    .bind(now)
    .execute(db.writer())
    .await?;
    get_poster(db, id).await?.ok_or(sqlx::Error::RowNotFound)
}

/// Assign a page to a poster (idempotent).
pub async fn assign_page(db: &Db, poster_id: &str, page_id: &str) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO poster_pages (poster_id, page_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
    )
    .bind(poster_id)
    .bind(page_id)
    .execute(db.writer())
    .await?;
    Ok(())
}

/// The poster that owns a page (reverse lookup), if any.
pub async fn poster_for_page(db: &Db, page_id: &str) -> Result<Option<Poster>, sqlx::Error> {
    sqlx::query_as::<_, Poster>(
        "SELECT p.* FROM posters p
         JOIN poster_pages pp ON pp.poster_id = p.id
         WHERE pp.page_id = ?",
    )
    .bind(page_id)
    .fetch_optional(db.reader())
    .await
}

// ── Telegram inventory (per-row, never range-forwarded) ──────────────────────

/// Record a delivered item for a page. One row per real message — this is the
/// data model that makes the old delete-and-repost range-forward bug impossible.
pub async fn add_inventory(
    db: &Db,
    page_id: &str,
    asset_id: Option<&str>,
    staging_msg_id: Option<i64>,
) -> Result<InventoryItem, sqlx::Error> {
    let now = Utc::now();
    let id = new_id();
    sqlx::query(
        "INSERT INTO telegram_inventory (id, page_id, asset_id, staging_msg_id, forwarded_to, forwarded_msg_id, created_at, forwarded_at)
         VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL)",
    )
    .bind(&id)
    .bind(page_id)
    .bind(asset_id)
    .bind(staging_msg_id)
    .bind(now)
    .execute(db.writer())
    .await?;
    sqlx::query_as::<_, InventoryItem>("SELECT * FROM telegram_inventory WHERE id = ?")
        .bind(&id)
        .fetch_one(db.writer())
        .await
}

/// Has this (page, asset) pair already been sent to staging? Used to make
/// send_asset idempotent so a double-click / retried request doesn't create a
/// second inventory row (and thus a second forwarded copy to the poster).
pub async fn find_inventory_for_asset(
    db: &Db,
    page_id: &str,
    asset_id: &str,
) -> Result<Option<InventoryItem>, sqlx::Error> {
    sqlx::query_as::<_, InventoryItem>(
        "SELECT * FROM telegram_inventory WHERE page_id = ? AND asset_id = ? ORDER BY created_at LIMIT 1",
    )
    .bind(page_id)
    .bind(asset_id)
    .fetch_optional(db.reader())
    .await
}

/// Items for a page that have not yet been forwarded to any poster.
pub async fn pending_inventory(db: &Db, page_id: &str) -> Result<Vec<InventoryItem>, sqlx::Error> {
    sqlx::query_as::<_, InventoryItem>(
        "SELECT * FROM telegram_inventory WHERE page_id = ? AND forwarded_to IS NULL ORDER BY created_at",
    )
    .bind(page_id)
    .fetch_all(db.reader())
    .await
}

/// Atomically CLAIM a page's pending items for forwarding to `poster_id`, and
/// return exactly the rows this caller claimed.
///
/// This is the fix for the concurrent-double-forward bug: instead of "read
/// pending (reader) → forward → mark (writer)" — where two callers both read
/// the same pending set and both forward — we mark the rows claimed in a single
/// writer statement first (a sentinel `inflight:{poster}` in `forwarded_to`),
/// then only forward the rows we actually claimed. The second concurrent caller
/// claims zero rows. After a successful Telegram forward we promote the sentinel
/// to the real poster id via `confirm_forwarded`; if forwarding fails we release
/// the claim back to pending via `release_claim`.
pub async fn claim_pending_inventory(
    db: &Db,
    page_id: &str,
    poster_id: &str,
) -> Result<Vec<InventoryItem>, sqlx::Error> {
    let sentinel = format!("inflight:{poster_id}");
    // Single serialized writer statement claims all currently-pending rows.
    sqlx::query(
        "UPDATE telegram_inventory SET forwarded_to = ? WHERE page_id = ? AND forwarded_to IS NULL",
    )
    .bind(&sentinel)
    .bind(page_id)
    .execute(db.writer())
    .await?;

    // Return exactly the rows we just claimed.
    sqlx::query_as::<_, InventoryItem>(
        "SELECT * FROM telegram_inventory WHERE page_id = ? AND forwarded_to = ? ORDER BY created_at",
    )
    .bind(page_id)
    .bind(&sentinel)
    .fetch_all(db.writer())
    .await
}

/// Promote a claimed (inflight) row to confirmed-forwarded after Telegram
/// accepted the forward. Guarded so it only affects a still-inflight row.
pub async fn confirm_forwarded(
    db: &Db,
    item_id: &str,
    poster_id: &str,
    forwarded_msg_id: i64,
) -> Result<(), sqlx::Error> {
    let sentinel = format!("inflight:{poster_id}");
    sqlx::query(
        "UPDATE telegram_inventory SET forwarded_to = ?, forwarded_msg_id = ?, forwarded_at = ?
         WHERE id = ? AND forwarded_to = ?",
    )
    .bind(poster_id)
    .bind(forwarded_msg_id)
    .bind(Utc::now())
    .bind(item_id)
    .bind(&sentinel)
    .execute(db.writer())
    .await?;
    Ok(())
}

/// Release a claimed (inflight) row back to pending — used when the Telegram
/// forward failed, so a later retry can pick it up. Never silently drops.
pub async fn release_claim(db: &Db, item_id: &str, poster_id: &str) -> Result<(), sqlx::Error> {
    let sentinel = format!("inflight:{poster_id}");
    sqlx::query(
        "UPDATE telegram_inventory SET forwarded_to = NULL WHERE id = ? AND forwarded_to = ?",
    )
    .bind(item_id)
    .bind(&sentinel)
    .execute(db.writer())
    .await?;
    Ok(())
}

/// Mark a single inventory row forwarded. Per-row, idempotent — the watermark
/// can never advance past items that didn't actually send.
pub async fn mark_forwarded(
    db: &Db,
    item_id: &str,
    poster_id: &str,
    forwarded_msg_id: i64,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE telegram_inventory SET forwarded_to = ?, forwarded_msg_id = ?, forwarded_at = ? WHERE id = ?",
    )
    .bind(poster_id)
    .bind(forwarded_msg_id)
    .bind(Utc::now())
    .bind(item_id)
    .execute(db.writer())
    .await?;
    Ok(())
}

// ── Wave-1 domain repositories (one module per workstream) ─────────────────
pub mod captions;
pub mod requests;
pub mod roster;
pub mod sounds;
pub mod telegram_cfg;

// ── Wave-2 domain repositories ─────────────────────────────────────────────
pub mod burn;
pub mod clipper;
pub mod projects_ext;
pub mod video;
