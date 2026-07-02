//! Telegram distribution config repo — settings KV, forum-topic mappings,
//! schedule, poster↔user bindings, and the poster/page mutations the
//! /api/telegram surface needs beyond the read paths in `repo/mod.rs`.
//!
//! Replaces the parts of `telegram_config.json` that weren't already normalized
//! into first-class tables. Hard rules carried over from the Python service:
//!   - settings key `bot_token` is only the STORED token — the env var
//!     TELEGRAM_BOT_TOKEN always beats it at runtime (callers enforce this).
//!   - topic mappings are APPEND-ONLY: `set_topic` refuses to overwrite an
//!     existing (scope, owner, page) row unless `force = true` is explicit.

use chrono::{DateTime, Utc};
use serde::Serialize;

use crate::db::Db;
use crate::model::InventoryItem;

use super::new_id;

// ── Row types ────────────────────────────────────────────────────────────────

/// A forum-topic mapping. scope = 'staging' (owner_id = "") or 'poster'
/// (owner_id = poster_id).
#[derive(Debug, Clone, Serialize, sqlx::FromRow)]
pub struct Topic {
    pub scope: String,
    pub owner_id: String,
    pub page_id: String,
    pub topic_id: i64,
    pub topic_name: String,
    pub created_at: DateTime<Utc>,
}

/// The single-row (id = 1) distribution schedule.
#[derive(Debug, Clone, Serialize, sqlx::FromRow)]
pub struct Schedule {
    pub enabled: bool,
    pub forward_time: String,
    pub timezone: String,
    pub last_run: Option<String>,
}

/// Per-page inventory counts (total / pending / forwarded).
#[derive(Debug, Clone, Serialize, sqlx::FromRow)]
pub struct InventorySummary {
    pub page_id: String,
    pub total: i64,
    pub pending: i64,
    pub forwarded: i64,
}

/// The slice of a page row the Telegram surface needs (topic naming).
#[derive(Debug, Clone, Serialize, sqlx::FromRow)]
pub struct PageBrief {
    pub id: String,
    pub name: String,
    pub provider: Option<String>,
}

/// An active sound (label + URL) for the daily-batch summary message.
#[derive(Debug, Clone, Serialize, sqlx::FromRow)]
pub struct SoundBrief {
    pub label: String,
    pub url: String,
}

// ── Settings KV ──────────────────────────────────────────────────────────────
// Keys in use: staging_chat_id, staging_name, bot_token, bot_username
// (see migrations/0003 + import.rs).

pub async fn get_setting(db: &Db, key: &str) -> Result<Option<String>, sqlx::Error> {
    sqlx::query_scalar::<_, String>("SELECT value FROM settings WHERE key = ?")
        .bind(key)
        .fetch_optional(db.reader())
        .await
}

pub async fn set_setting(db: &Db, key: &str, value: &str) -> Result<(), sqlx::Error> {
    sqlx::query("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)")
        .bind(key)
        .bind(value)
        .bind(Utc::now())
        .execute(db.writer())
        .await?;
    Ok(())
}

pub async fn delete_setting(db: &Db, key: &str) -> Result<(), sqlx::Error> {
    sqlx::query("DELETE FROM settings WHERE key = ?")
        .bind(key)
        .execute(db.writer())
        .await?;
    Ok(())
}

// ── Staging group ────────────────────────────────────────────────────────────

/// The configured staging group, if any: (chat_id, name).
pub async fn staging_group(db: &Db) -> Result<Option<(i64, String)>, sqlx::Error> {
    let chat = get_setting(db, "staging_chat_id").await?;
    let Some(chat) = chat.and_then(|v| v.parse::<i64>().ok()) else {
        return Ok(None);
    };
    let name = get_setting(db, "staging_name").await?.unwrap_or_default();
    Ok(Some((chat, name)))
}

/// Set the staging group identity. Mirrors the Python behavior: switching to a
/// DIFFERENT chat_id clears the (now stale) staging topic mappings.
pub async fn set_staging_group(db: &Db, chat_id: i64, name: &str) -> Result<(), sqlx::Error> {
    let old = staging_group(db).await?.map(|(c, _)| c);
    set_setting(db, "staging_chat_id", &chat_id.to_string()).await?;
    set_setting(db, "staging_name", name).await?;
    if let Some(old_chat) = old {
        if old_chat != chat_id {
            sqlx::query("DELETE FROM topics WHERE scope = 'staging'")
                .execute(db.writer())
                .await?;
        }
    }
    Ok(())
}

// ── Topics (append-only mappings) ────────────────────────────────────────────

pub async fn list_topics(db: &Db, scope: &str, owner_id: &str) -> Result<Vec<Topic>, sqlx::Error> {
    sqlx::query_as::<_, Topic>(
        "SELECT * FROM topics WHERE scope = ? AND owner_id = ? ORDER BY page_id",
    )
    .bind(scope)
    .bind(owner_id)
    .fetch_all(db.reader())
    .await
}

pub async fn get_topic(
    db: &Db,
    scope: &str,
    owner_id: &str,
    page_id: &str,
) -> Result<Option<Topic>, sqlx::Error> {
    sqlx::query_as::<_, Topic>(
        "SELECT * FROM topics WHERE scope = ? AND owner_id = ? AND page_id = ?",
    )
    .bind(scope)
    .bind(owner_id)
    .bind(page_id)
    .fetch_optional(db.reader())
    .await
}

/// Map a page to a forum topic. APPEND-ONLY by default: an existing
/// (scope, owner, page) mapping is left untouched unless `force = true`.
/// Returns true if a row was written (inserted or force-updated).
pub async fn set_topic(
    db: &Db,
    scope: &str,
    owner_id: &str,
    page_id: &str,
    topic_id: i64,
    topic_name: &str,
    force: bool,
) -> Result<bool, sqlx::Error> {
    let sql = if force {
        "INSERT INTO topics (scope, owner_id, page_id, topic_id, topic_name, created_at)
         VALUES (?, ?, ?, ?, ?, ?)
         ON CONFLICT(scope, owner_id, page_id)
         DO UPDATE SET topic_id = excluded.topic_id, topic_name = excluded.topic_name"
    } else {
        "INSERT INTO topics (scope, owner_id, page_id, topic_id, topic_name, created_at)
         VALUES (?, ?, ?, ?, ?, ?)
         ON CONFLICT(scope, owner_id, page_id) DO NOTHING"
    };
    let res = sqlx::query(sql)
        .bind(scope)
        .bind(owner_id)
        .bind(page_id)
        .bind(topic_id)
        .bind(topic_name)
        .bind(Utc::now())
        .execute(db.writer())
        .await?;
    Ok(res.rows_affected() > 0)
}

/// Remove one topic mapping. Returns true if it existed.
pub async fn remove_topic(
    db: &Db,
    scope: &str,
    owner_id: &str,
    page_id: &str,
) -> Result<bool, sqlx::Error> {
    let res = sqlx::query("DELETE FROM topics WHERE scope = ? AND owner_id = ? AND page_id = ?")
        .bind(scope)
        .bind(owner_id)
        .bind(page_id)
        .execute(db.writer())
        .await?;
    Ok(res.rows_affected() > 0)
}

// ── Schedule (single row, id = 1) ────────────────────────────────────────────

pub async fn get_schedule(db: &Db) -> Result<Schedule, sqlx::Error> {
    sqlx::query_as::<_, Schedule>(
        "SELECT enabled, forward_time, timezone, last_run FROM schedule WHERE id = 1",
    )
    .fetch_one(db.reader())
    .await
}

/// Partial update — only the provided fields change. Returns the new schedule.
pub async fn update_schedule(
    db: &Db,
    enabled: Option<bool>,
    forward_time: Option<&str>,
    timezone: Option<&str>,
) -> Result<Schedule, sqlx::Error> {
    sqlx::query(
        "UPDATE schedule SET enabled = COALESCE(?, enabled),
                             forward_time = COALESCE(?, forward_time),
                             timezone = COALESCE(?, timezone)
         WHERE id = 1",
    )
    .bind(enabled)
    .bind(forward_time)
    .bind(timezone)
    .execute(db.writer())
    .await?;
    get_schedule(db).await
}

pub async fn set_last_run(db: &Db, timestamp: &str) -> Result<(), sqlx::Error> {
    sqlx::query("UPDATE schedule SET last_run = ? WHERE id = 1")
        .bind(timestamp)
        .execute(db.writer())
        .await?;
    Ok(())
}

// ── Poster ↔ Telegram user bindings (Mini App identity) ─────────────────────

pub async fn poster_user_ids(db: &Db, poster_id: &str) -> Result<Vec<i64>, sqlx::Error> {
    sqlx::query_scalar::<_, i64>(
        "SELECT telegram_user_id FROM poster_users WHERE poster_id = ? ORDER BY telegram_user_id",
    )
    .bind(poster_id)
    .fetch_all(db.reader())
    .await
}

/// Bind a Telegram user id (and optional @username) to a poster. A user acts
/// as exactly ONE poster (UNIQUE on user id): rebinding moves them — this is
/// the deterministic replacement for the old first-match-in-dict resolution.
pub async fn bind_user(
    db: &Db,
    poster_id: &str,
    user_id: i64,
    username: Option<&str>,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO poster_users (poster_id, telegram_user_id) VALUES (?, ?)
         ON CONFLICT(telegram_user_id) DO UPDATE SET poster_id = excluded.poster_id",
    )
    .bind(poster_id)
    .bind(user_id)
    .execute(db.writer())
    .await?;
    if let Some(u) = username {
        let clean = u.trim_start_matches('@');
        sqlx::query("UPDATE posters SET telegram_username = ?, updated_at = ? WHERE id = ?")
            .bind(clean)
            .bind(Utc::now())
            .bind(poster_id)
            .execute(db.writer())
            .await?;
    } else {
        touch_poster(db, poster_id).await?;
    }
    Ok(())
}

/// Unbind a user from a poster. Returns true if the binding existed.
pub async fn unbind_user(db: &Db, poster_id: &str, user_id: i64) -> Result<bool, sqlx::Error> {
    let res = sqlx::query("DELETE FROM poster_users WHERE poster_id = ? AND telegram_user_id = ?")
        .bind(poster_id)
        .bind(user_id)
        .execute(db.writer())
        .await?;
    if res.rows_affected() > 0 {
        touch_poster(db, poster_id).await?;
    }
    Ok(res.rows_affected() > 0)
}

pub async fn poster_username(db: &Db, poster_id: &str) -> Result<Option<String>, sqlx::Error> {
    sqlx::query_scalar::<_, Option<String>>("SELECT telegram_username FROM posters WHERE id = ?")
        .bind(poster_id)
        .fetch_optional(db.reader())
        .await
        .map(|v| v.flatten())
}

// ── Poster mutations (beyond the upsert in repo/mod.rs) ─────────────────────

async fn touch_poster(db: &Db, poster_id: &str) -> Result<(), sqlx::Error> {
    sqlx::query("UPDATE posters SET updated_at = ? WHERE id = ?")
        .bind(Utc::now())
        .bind(poster_id)
        .execute(db.writer())
        .await?;
    Ok(())
}

/// Partial poster update (name and/or chat_id). Returns true if the poster
/// exists.
pub async fn update_poster(
    db: &Db,
    poster_id: &str,
    name: Option<&str>,
    chat_id: Option<i64>,
) -> Result<bool, sqlx::Error> {
    let res = sqlx::query(
        "UPDATE posters SET name = COALESCE(?, name),
                            chat_id = COALESCE(?, chat_id),
                            updated_at = ?
         WHERE id = ?",
    )
    .bind(name)
    .bind(chat_id)
    .bind(Utc::now())
    .bind(poster_id)
    .execute(db.writer())
    .await?;
    Ok(res.rows_affected() > 0)
}

/// Delete a poster. poster_pages / poster_users rows cascade via FK; their
/// forum-topic mappings are removed explicitly (topics has no poster FK).
/// Returns true if the poster existed.
pub async fn delete_poster(db: &Db, poster_id: &str) -> Result<bool, sqlx::Error> {
    sqlx::query("DELETE FROM topics WHERE scope = 'poster' AND owner_id = ?")
        .bind(poster_id)
        .execute(db.writer())
        .await?;
    let res = sqlx::query("DELETE FROM posters WHERE id = ?")
        .bind(poster_id)
        .execute(db.writer())
        .await?;
    Ok(res.rows_affected() > 0)
}

pub async fn set_poster_sounds_topic(
    db: &Db,
    poster_id: &str,
    topic_id: i64,
) -> Result<(), sqlx::Error> {
    sqlx::query("UPDATE posters SET sounds_topic_id = ?, updated_at = ? WHERE id = ?")
        .bind(topic_id)
        .bind(Utc::now())
        .bind(poster_id)
        .execute(db.writer())
        .await?;
    Ok(())
}

/// Remove a page assignment (the caller separately removes the topic mapping
/// and, best-effort, the Telegram topic itself). Returns true if it existed.
pub async fn unassign_page(db: &Db, poster_id: &str, page_id: &str) -> Result<bool, sqlx::Error> {
    let res = sqlx::query("DELETE FROM poster_pages WHERE poster_id = ? AND page_id = ?")
        .bind(poster_id)
        .bind(page_id)
        .execute(db.writer())
        .await?;
    if res.rows_affected() > 0 {
        touch_poster(db, poster_id).await?;
    }
    Ok(res.rows_affected() > 0)
}

/// The pages a poster runs (assignment order = insertion order).
pub async fn poster_page_ids(db: &Db, poster_id: &str) -> Result<Vec<String>, sqlx::Error> {
    sqlx::query_scalar::<_, String>(
        "SELECT page_id FROM poster_pages WHERE poster_id = ? ORDER BY rowid",
    )
    .bind(poster_id)
    .fetch_all(db.reader())
    .await
}

// ── Pages (read/ensure helpers for the Telegram surface) ────────────────────

/// Insert a placeholder page row if it doesn't exist (so FK-dependent rows —
/// topics, poster_pages, inventory — can reference pages the roster sync
/// hasn't landed yet; same convention as the JSON import).
pub async fn ensure_page(db: &Db, page_id: &str, name: &str) -> Result<(), sqlx::Error> {
    let now = Utc::now();
    sqlx::query(
        "INSERT OR IGNORE INTO pages (id, name, project, r2_prefix, created_at, updated_at)
         VALUES (?, ?, NULL, NULL, ?, ?)",
    )
    .bind(page_id)
    .bind(name)
    .bind(now)
    .bind(now)
    .execute(db.writer())
    .await?;
    Ok(())
}

pub async fn list_pages_brief(db: &Db) -> Result<Vec<PageBrief>, sqlx::Error> {
    sqlx::query_as::<_, PageBrief>("SELECT id, name, provider FROM pages ORDER BY name")
        .fetch_all(db.reader())
        .await
}

pub async fn page_name(db: &Db, page_id: &str) -> Result<Option<String>, sqlx::Error> {
    sqlx::query_scalar::<_, String>("SELECT name FROM pages WHERE id = ?")
        .bind(page_id)
        .fetch_optional(db.reader())
        .await
}

// ── Inventory ────────────────────────────────────────────────────────────────

/// Per-page inventory counts. pending = not yet forwarded (claimed-inflight
/// rows count as non-pending, which is correct: they're being handled).
pub async fn inventory_summary(db: &Db) -> Result<Vec<InventorySummary>, sqlx::Error> {
    sqlx::query_as::<_, InventorySummary>(
        "SELECT page_id,
                COUNT(*)                                             AS total,
                SUM(CASE WHEN forwarded_to IS NULL THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN forwarded_to IS NULL THEN 0 ELSE 1 END) AS forwarded
         FROM telegram_inventory GROUP BY page_id ORDER BY page_id",
    )
    .fetch_all(db.reader())
    .await
}

/// Like `repo::add_inventory` but records the media details (file_id,
/// file_name, media_type, caption, source) in the `meta` JSON column — the
/// file_id is what lets the bot re-send media without re-uploading.
pub async fn add_inventory_with_meta(
    db: &Db,
    page_id: &str,
    asset_id: Option<&str>,
    staging_msg_id: Option<i64>,
    meta_json: &str,
) -> Result<InventoryItem, sqlx::Error> {
    let now = Utc::now();
    let id = new_id();
    sqlx::query(
        "INSERT INTO telegram_inventory (id, page_id, asset_id, staging_msg_id, forwarded_to, forwarded_msg_id, created_at, forwarded_at, meta)
         VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, ?)",
    )
    .bind(&id)
    .bind(page_id)
    .bind(asset_id)
    .bind(staging_msg_id)
    .bind(now)
    .bind(meta_json)
    .execute(db.writer())
    .await?;
    sqlx::query_as::<_, InventoryItem>("SELECT * FROM telegram_inventory WHERE id = ?")
        .bind(&id)
        .fetch_one(db.writer())
        .await
}

// ── Sounds (read-only peek for the daily-batch summary) ─────────────────────
// The sounds CRUD surface belongs to the sounds workstream; the daily batch
// only needs the active list for its summary message.

pub async fn active_sounds(db: &Db) -> Result<Vec<SoundBrief>, sqlx::Error> {
    sqlx::query_as::<_, SoundBrief>(
        "SELECT label, url FROM sounds WHERE active = 1 ORDER BY created_at",
    )
    .fetch_all(db.reader())
    .await
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::repo;

    #[tokio::test]
    async fn settings_kv_roundtrip() {
        let (db, _g) = Db::connect_temp().await.unwrap();
        assert!(get_setting(&db, "bot_token").await.unwrap().is_none());
        set_setting(&db, "bot_token", "123:abc").await.unwrap();
        assert_eq!(get_setting(&db, "bot_token").await.unwrap().as_deref(), Some("123:abc"));
        set_setting(&db, "bot_token", "456:def").await.unwrap();
        assert_eq!(get_setting(&db, "bot_token").await.unwrap().as_deref(), Some("456:def"));
        delete_setting(&db, "bot_token").await.unwrap();
        assert!(get_setting(&db, "bot_token").await.unwrap().is_none());
    }

    #[tokio::test]
    async fn staging_group_change_clears_staging_topics_only() {
        let (db, _g) = Db::connect_temp().await.unwrap();
        repo::upsert_page(&db, "pg1", "Page One", None).await.unwrap();
        repo::upsert_poster(&db, "seffra", "Seffra", 100).await.unwrap();
        set_staging_group(&db, -100, "Group A").await.unwrap();
        set_topic(&db, "staging", "", "pg1", 11, "Page One", false).await.unwrap();
        set_topic(&db, "poster", "seffra", "pg1", 22, "Page One", false).await.unwrap();

        // Same chat id → topics survive.
        set_staging_group(&db, -100, "Group A renamed").await.unwrap();
        assert!(get_topic(&db, "staging", "", "pg1").await.unwrap().is_some());

        // Different chat id → staging topics cleared, poster topics untouched.
        set_staging_group(&db, -200, "Group B").await.unwrap();
        assert!(get_topic(&db, "staging", "", "pg1").await.unwrap().is_none());
        assert!(get_topic(&db, "poster", "seffra", "pg1").await.unwrap().is_some());
        assert_eq!(staging_group(&db).await.unwrap(), Some((-200, "Group B".into())));
    }

    /// THE append-only invariant: an existing mapping is never overwritten
    /// without force=true. (This is the guard against the old remapping bugs.)
    #[tokio::test]
    async fn set_topic_is_append_only() {
        let (db, _g) = Db::connect_temp().await.unwrap();
        repo::upsert_page(&db, "pg1", "Page One", None).await.unwrap();

        assert!(set_topic(&db, "staging", "", "pg1", 111, "orig", false).await.unwrap());
        // Second write WITHOUT force: refused, mapping unchanged.
        assert!(!set_topic(&db, "staging", "", "pg1", 999, "stomp", false).await.unwrap());
        let t = get_topic(&db, "staging", "", "pg1").await.unwrap().unwrap();
        assert_eq!(t.topic_id, 111);
        assert_eq!(t.topic_name, "orig");
        // WITH force: explicit remap allowed.
        assert!(set_topic(&db, "staging", "", "pg1", 999, "remapped", true).await.unwrap());
        let t = get_topic(&db, "staging", "", "pg1").await.unwrap().unwrap();
        assert_eq!(t.topic_id, 999);
    }

    #[tokio::test]
    async fn topic_scopes_are_independent() {
        let (db, _g) = Db::connect_temp().await.unwrap();
        repo::upsert_page(&db, "pg1", "Page One", None).await.unwrap();
        repo::upsert_poster(&db, "gigi", "Gigi", 5).await.unwrap();
        set_topic(&db, "staging", "", "pg1", 1, "s", false).await.unwrap();
        set_topic(&db, "poster", "gigi", "pg1", 2, "p", false).await.unwrap();
        assert_eq!(list_topics(&db, "staging", "").await.unwrap().len(), 1);
        assert_eq!(list_topics(&db, "poster", "gigi").await.unwrap().len(), 1);
        assert!(remove_topic(&db, "staging", "", "pg1").await.unwrap());
        assert!(!remove_topic(&db, "staging", "", "pg1").await.unwrap());
        assert!(get_topic(&db, "poster", "gigi", "pg1").await.unwrap().is_some());
    }

    #[tokio::test]
    async fn schedule_partial_updates() {
        let (db, _g) = Db::connect_temp().await.unwrap();
        let s = get_schedule(&db).await.unwrap();
        assert!(!s.enabled);
        assert_eq!(s.forward_time, "09:00");
        assert_eq!(s.timezone, "America/New_York");
        assert!(s.last_run.is_none());

        let s = update_schedule(&db, Some(true), None, None).await.unwrap();
        assert!(s.enabled);
        assert_eq!(s.forward_time, "09:00", "unset fields must not change");

        let s = update_schedule(&db, None, Some("14:30"), Some("America/Chicago")).await.unwrap();
        assert!(s.enabled, "enabled must survive a time-only update");
        assert_eq!(s.forward_time, "14:30");
        assert_eq!(s.timezone, "America/Chicago");

        set_last_run(&db, "2026-07-02T09:00:00Z").await.unwrap();
        let s = get_schedule(&db).await.unwrap();
        assert_eq!(s.last_run.as_deref(), Some("2026-07-02T09:00:00Z"));
    }

    #[tokio::test]
    async fn user_binding_moves_between_posters() {
        let (db, _g) = Db::connect_temp().await.unwrap();
        repo::upsert_poster(&db, "a", "A", 1).await.unwrap();
        repo::upsert_poster(&db, "b", "B", 2).await.unwrap();

        bind_user(&db, "a", 777, Some("@johnny")).await.unwrap();
        assert_eq!(poster_user_ids(&db, "a").await.unwrap(), vec![777]);
        assert_eq!(poster_username(&db, "a").await.unwrap().as_deref(), Some("johnny"));

        // A user acts as exactly one poster: rebinding MOVES the binding.
        bind_user(&db, "b", 777, None).await.unwrap();
        assert!(poster_user_ids(&db, "a").await.unwrap().is_empty());
        assert_eq!(poster_user_ids(&db, "b").await.unwrap(), vec![777]);

        assert!(unbind_user(&db, "b", 777).await.unwrap());
        assert!(!unbind_user(&db, "b", 777).await.unwrap());
        assert!(poster_user_ids(&db, "b").await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn poster_update_and_delete() {
        let (db, _g) = Db::connect_temp().await.unwrap();
        repo::upsert_page(&db, "pg1", "Page One", None).await.unwrap();
        repo::upsert_poster(&db, "jake", "Jake", 10).await.unwrap();
        repo::assign_page(&db, "jake", "pg1").await.unwrap();
        set_topic(&db, "poster", "jake", "pg1", 5, "Page One", false).await.unwrap();
        bind_user(&db, "jake", 42, None).await.unwrap();

        // Partial update: only chat_id changes.
        assert!(update_poster(&db, "jake", None, Some(99)).await.unwrap());
        let p = repo::get_poster(&db, "jake").await.unwrap().unwrap();
        assert_eq!(p.name, "Jake");
        assert_eq!(p.chat_id, 99);
        assert!(!update_poster(&db, "ghost", Some("x"), None).await.unwrap());

        // Delete removes the poster plus its topics / assignments / bindings.
        assert!(delete_poster(&db, "jake").await.unwrap());
        assert!(!delete_poster(&db, "jake").await.unwrap());
        assert!(repo::get_poster(&db, "jake").await.unwrap().is_none());
        assert!(get_topic(&db, "poster", "jake", "pg1").await.unwrap().is_none());
        assert!(poster_page_ids(&db, "jake").await.unwrap().is_empty());
        assert!(poster_user_ids(&db, "jake").await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn page_assignment_lifecycle() {
        let (db, _g) = Db::connect_temp().await.unwrap();
        repo::upsert_poster(&db, "sam", "Sam", 3).await.unwrap();
        // ensure_page lets an assignment land before the roster sync does.
        ensure_page(&db, "pgX", "Placeholder X").await.unwrap();
        ensure_page(&db, "pgX", "should not overwrite").await.unwrap();
        assert_eq!(page_name(&db, "pgX").await.unwrap().as_deref(), Some("Placeholder X"));

        repo::assign_page(&db, "sam", "pgX").await.unwrap();
        assert_eq!(poster_page_ids(&db, "sam").await.unwrap(), vec!["pgX".to_string()]);
        assert!(unassign_page(&db, "sam", "pgX").await.unwrap());
        assert!(!unassign_page(&db, "sam", "pgX").await.unwrap());
        assert!(poster_page_ids(&db, "sam").await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn inventory_summary_counts_pending_vs_forwarded() {
        let (db, _g) = Db::connect_temp().await.unwrap();
        repo::upsert_page(&db, "pg1", "One", None).await.unwrap();
        repo::upsert_page(&db, "pg2", "Two", None).await.unwrap();
        let a = repo::add_inventory(&db, "pg1", None, Some(1)).await.unwrap();
        repo::add_inventory(&db, "pg1", None, Some(2)).await.unwrap();
        repo::add_inventory(&db, "pg2", None, Some(3)).await.unwrap();
        repo::mark_forwarded(&db, &a.id, "poster", 900).await.unwrap();

        let s = inventory_summary(&db).await.unwrap();
        assert_eq!(s.len(), 2);
        let pg1 = s.iter().find(|x| x.page_id == "pg1").unwrap();
        assert_eq!((pg1.total, pg1.pending, pg1.forwarded), (2, 1, 1));
        let pg2 = s.iter().find(|x| x.page_id == "pg2").unwrap();
        assert_eq!((pg2.total, pg2.pending, pg2.forwarded), (1, 1, 0));
    }

    #[tokio::test]
    async fn inventory_with_meta_is_pending_and_claimable() {
        let (db, _g) = Db::connect_temp().await.unwrap();
        repo::upsert_page(&db, "pg1", "One", None).await.unwrap();
        let meta = r#"{"file_name":"burned_000.mp4","media_type":"video","source":"api"}"#;
        let item = add_inventory_with_meta(&db, "pg1", None, Some(4242), meta).await.unwrap();
        assert_eq!(item.staging_msg_id, Some(4242));
        assert!(item.forwarded_to.is_none());

        // Flows through the standard claim→confirm path untouched.
        let claimed = repo::claim_pending_inventory(&db, "pg1", "pst").await.unwrap();
        assert_eq!(claimed.len(), 1);
        repo::confirm_forwarded(&db, &claimed[0].id, "pst", 7).await.unwrap();
        assert!(repo::pending_inventory(&db, "pg1").await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn active_sounds_filters_inactive() {
        let (db, _g) = Db::connect_temp().await.unwrap();
        sqlx::query("INSERT INTO sounds (id, url, label, active, created_at) VALUES ('s1','u1','L1',1,'2026-01-01'), ('s2','u2','L2',0,'2026-01-02')")
            .execute(db.writer())
            .await
            .unwrap();
        let s = active_sounds(&db).await.unwrap();
        assert_eq!(s.len(), 1);
        assert_eq!(s[0].label, "L1");
    }
}
