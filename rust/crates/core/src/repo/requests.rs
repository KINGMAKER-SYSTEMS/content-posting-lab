//! Mini App domain repository — content requests + poster/page resolution.
//!
//! Ports `services/content_requests.py` (the poster → agent request queue) and
//! the poster-resolution reads that `services/miniapp_auth.py` and
//! `services/poster_content.py` needed from the JSON stores. Rows live in the
//! `content_requests`, `posters`, `poster_users`, `poster_pages`, `pages`, and
//! `settings` tables (migrations 0001 + 0003).
//!
//! JSON parity note: timestamps serialize as `%Y-%m-%dT%H:%M:%S.000Z` — the
//! exact string the old Python `_now()` produced — so the Mini App frontend
//! and the external agent see byte-identical value formats.

use chrono::{DateTime, Utc};
use serde::{Serialize, Serializer};

use crate::db::Db;
use crate::model::Asset;
use crate::repo::new_id;

/// Lifecycle of a request as the agent works it (open → in_progress →
/// fulfilled/cancelled). Mirrors the CHECK constraint on the table.
pub const STATUSES: [&str; 4] = ["open", "in_progress", "fulfilled", "cancelled"];

/// Serialize a timestamp exactly like the old Python store: whole seconds with
/// a literal `.000Z` suffix.
fn ser_ts<S: Serializer>(dt: &DateTime<Utc>, s: S) -> Result<S::Ok, S::Error> {
    s.serialize_str(&format!("{}.000Z", dt.format("%Y-%m-%dT%H:%M:%S")))
}

fn ser_ts_opt<S: Serializer>(dt: &Option<DateTime<Utc>>, s: S) -> Result<S::Ok, S::Error> {
    match dt {
        Some(d) => ser_ts(d, s),
        None => s.serialize_none(),
    }
}

/// One content request. Serializes with the exact field names (and timestamp
/// format) the Python store emitted, so agent + Mini App clients need no change.
#[derive(Debug, Clone, Serialize, sqlx::FromRow)]
pub struct ContentRequest {
    pub id: String,
    pub poster_id: String,
    pub poster_name: Option<String>,
    pub page_id: Option<String>,
    pub page_name: Option<String>,
    pub text: String,
    pub quantity: Option<i64>,
    pub status: String,
    pub source: String,
    #[serde(serialize_with = "ser_ts")]
    pub created_at: DateTime<Utc>,
    #[serde(serialize_with = "ser_ts")]
    pub updated_at: DateTime<Utc>,
    #[serde(serialize_with = "ser_ts_opt")]
    pub fulfilled_at: Option<DateTime<Utc>>,
    pub agent_note: Option<String>,
}

/// Error from [`update_request`]: an unknown status is a caller error (400 in
/// the router), everything else is a DB failure.
#[derive(Debug, thiserror::Error)]
pub enum RequestUpdateError {
    /// Message matches the Python `ValueError` text byte-for-byte.
    #[error("invalid status '{0}'; expected one of ('open', 'in_progress', 'fulfilled', 'cancelled')")]
    InvalidStatus(String),
    #[error(transparent)]
    Db(#[from] sqlx::Error),
}

/// Input for [`add_request`] (a struct rather than 8 positional args).
#[derive(Debug, Default, Clone, Copy)]
pub struct NewRequest<'a> {
    pub poster_id: &'a str,
    pub poster_name: Option<&'a str>,
    pub text: &'a str,
    pub page_id: Option<&'a str>,
    pub page_name: Option<&'a str>,
    pub quantity: Option<i64>,
    pub source: &'a str,
}

/// Append a new content request (status = open). Text is stored trimmed,
/// matching the old store.
pub async fn add_request(db: &Db, req: NewRequest<'_>) -> Result<ContentRequest, sqlx::Error> {
    let id = new_id();
    let now = Utc::now();
    sqlx::query(
        "INSERT INTO content_requests
         (id, poster_id, poster_name, page_id, page_name, text, quantity, status,
          source, agent_note, created_at, updated_at, fulfilled_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, NULL, ?, ?, NULL)",
    )
    .bind(&id)
    .bind(req.poster_id)
    .bind(req.poster_name)
    .bind(req.page_id)
    .bind(req.page_name)
    .bind(req.text.trim())
    .bind(req.quantity)
    .bind(req.source)
    .bind(now)
    .bind(now)
    .execute(db.writer())
    .await?;
    get_request(db, &id).await?.ok_or(sqlx::Error::RowNotFound)
}

/// List requests, newest first, optionally filtered by poster and/or status.
pub async fn list_requests(
    db: &Db,
    poster_id: Option<&str>,
    status: Option<&str>,
) -> Result<Vec<ContentRequest>, sqlx::Error> {
    sqlx::query_as::<_, ContentRequest>(
        "SELECT * FROM content_requests
         WHERE (?1 IS NULL OR poster_id = ?1)
           AND (?2 IS NULL OR status = ?2)
         ORDER BY created_at DESC",
    )
    .bind(poster_id)
    .bind(status)
    .fetch_all(db.reader())
    .await
}

pub async fn get_request(db: &Db, id: &str) -> Result<Option<ContentRequest>, sqlx::Error> {
    sqlx::query_as::<_, ContentRequest>("SELECT * FROM content_requests WHERE id = ?")
        .bind(id)
        .fetch_optional(db.reader())
        .await
}

/// Update a request's status / agent note. Returns the updated entry, or
/// `None` if the id doesn't exist. Setting status to `fulfilled` stamps
/// `fulfilled_at`; every update stamps `updated_at`.
pub async fn update_request(
    db: &Db,
    id: &str,
    status: Option<&str>,
    agent_note: Option<&str>,
) -> Result<Option<ContentRequest>, RequestUpdateError> {
    if let Some(s) = status {
        if !STATUSES.contains(&s) {
            return Err(RequestUpdateError::InvalidStatus(s.to_string()));
        }
    }
    let now = Utc::now();
    let res = sqlx::query(
        "UPDATE content_requests SET
           status       = COALESCE(?1, status),
           fulfilled_at = CASE WHEN ?1 = 'fulfilled' THEN ?2 ELSE fulfilled_at END,
           agent_note   = COALESCE(?3, agent_note),
           updated_at   = ?2
         WHERE id = ?4",
    )
    .bind(status)
    .bind(now)
    .bind(agent_note)
    .bind(id)
    .execute(db.writer())
    .await?;
    if res.rows_affected() == 0 {
        return Ok(None);
    }
    Ok(get_request(db, id).await?)
}

// ── Poster resolution (Mini App auth) ────────────────────────────────────────

/// Poster row including the Mini App identity column (`telegram_username`,
/// added in migration 0003 and absent from `model::Poster`).
#[derive(Debug, Clone, sqlx::FromRow)]
pub struct MiniappPoster {
    pub id: String,
    pub name: String,
    pub chat_id: i64,
    pub sounds_topic_id: Option<i64>,
    pub telegram_username: Option<String>,
}

const POSTER_COLS: &str = "id, name, chat_id, sounds_topic_id, telegram_username";

pub async fn get_poster_mini(db: &Db, id: &str) -> Result<Option<MiniappPoster>, sqlx::Error> {
    sqlx::query_as::<_, MiniappPoster>(&format!(
        "SELECT {POSTER_COLS} FROM posters WHERE id = ?"
    ))
    .bind(id)
    .fetch_optional(db.reader())
    .await
}

pub async fn list_posters_mini(db: &Db) -> Result<Vec<MiniappPoster>, sqlx::Error> {
    sqlx::query_as::<_, MiniappPoster>(&format!(
        "SELECT {POSTER_COLS} FROM posters ORDER BY name"
    ))
    .fetch_all(db.reader())
    .await
}

/// Bind a Telegram user id to a poster (idempotent; a uid maps to exactly one
/// poster — the UNIQUE constraint keeps later bindings from stealing it).
pub async fn bind_poster_user(db: &Db, poster_id: &str, uid: i64) -> Result<(), sqlx::Error> {
    sqlx::query("INSERT OR IGNORE INTO poster_users (poster_id, telegram_user_id) VALUES (?, ?)")
        .bind(poster_id)
        .bind(uid)
        .execute(db.writer())
        .await?;
    Ok(())
}

/// Resolve a Telegram user to the poster they act as.
///
/// Matching priority (same as the old `get_poster_for_user`):
///   1) explicit `poster_users` binding (strongest)
///   2) `telegram_username` match — case-insensitive, leading `@` stripped
pub async fn poster_for_user(
    db: &Db,
    user_id: Option<i64>,
    username: Option<&str>,
) -> Result<Option<MiniappPoster>, sqlx::Error> {
    if let Some(uid) = user_id {
        let hit = sqlx::query_as::<_, MiniappPoster>(&format!(
            "SELECT {POSTER_COLS} FROM posters
             WHERE id = (SELECT poster_id FROM poster_users WHERE telegram_user_id = ?)"
        ))
        .bind(uid)
        .fetch_optional(db.reader())
        .await?;
        if hit.is_some() {
            return Ok(hit);
        }
    }
    if let Some(uname) = username.filter(|u| !u.is_empty()) {
        let wanted = uname.trim_start_matches('@').to_lowercase();
        for poster in list_posters_mini(db).await? {
            let stored = poster
                .telegram_username
                .as_deref()
                .unwrap_or("")
                .trim_start_matches('@')
                .to_lowercase();
            if !stored.is_empty() && stored == wanted {
                return Ok(Some(poster));
            }
        }
    }
    Ok(None)
}

// ── Pages + content federation reads ─────────────────────────────────────────

/// Roster page row with the columns the Mini App summary needs (the Notion
/// `poster_name` assignment plus provider/status, absent from `model::Page`).
#[derive(Debug, Clone, sqlx::FromRow)]
pub struct MiniappPage {
    pub id: String,
    pub name: String,
    pub project: Option<String>,
    pub provider: Option<String>,
    pub status: Option<String>,
    pub poster_name: Option<String>,
}

pub async fn list_pages_mini(db: &Db) -> Result<Vec<MiniappPage>, sqlx::Error> {
    sqlx::query_as::<_, MiniappPage>(
        "SELECT id, name, project, provider, status, poster_name FROM pages ORDER BY name",
    )
    .fetch_all(db.reader())
    .await
}

/// Pages manually assigned to a poster (the `page_ids` override in the old
/// JSON store, now `poster_pages`).
pub async fn assigned_page_ids(db: &Db, poster_id: &str) -> Result<Vec<String>, sqlx::Error> {
    sqlx::query_scalar::<_, String>("SELECT page_id FROM poster_pages WHERE poster_id = ?")
        .bind(poster_id)
        .fetch_all(db.reader())
        .await
}

/// Read one app setting (e.g. the stored `bot_token` that `clab migrate`
/// imports — env var still beats it at runtime).
pub async fn get_setting(db: &Db, key: &str) -> Result<Option<String>, sqlx::Error> {
    sqlx::query_scalar::<_, String>("SELECT value FROM settings WHERE key = ?")
        .bind(key)
        .fetch_optional(db.reader())
        .await
}

/// Rendered (ready) videos for a project — generated / clip / burned assets,
/// newest first. This replaces the old filesystem walk of projects/<name>/.
pub async fn ready_videos_for_project(db: &Db, project: &str) -> Result<Vec<Asset>, sqlx::Error> {
    sqlx::query_as::<_, Asset>(
        "SELECT * FROM assets
         WHERE project = ? AND status = 'ready'
           AND kind IN ('generated','clip','burned')
         ORDER BY created_at DESC",
    )
    .bind(project)
    .fetch_all(db.reader())
    .await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::AssetKind;
    use crate::repo;

    async fn seed_poster(db: &Db, id: &str, name: &str) {
        repo::upsert_poster(db, id, name, -100).await.unwrap();
    }

    async fn set_username(db: &Db, id: &str, username: &str) {
        sqlx::query("UPDATE posters SET telegram_username = ? WHERE id = ?")
            .bind(username)
            .bind(id)
            .execute(db.writer())
            .await
            .unwrap();
    }

    // ── Request lifecycle ────────────────────────────────────────────────

    #[tokio::test]
    async fn add_request_starts_open_and_trims_text() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        let r = add_request(
            &db,
            NewRequest {
                poster_id: "p1",
                poster_name: Some("Seffra"),
                text: "  need 5 more  ",
                page_id: Some("pg1"),
                page_name: Some("Page One"),
                quantity: Some(5),
                source: "miniapp",
            },
        )
        .await
        .unwrap();
        assert_eq!(r.status, "open");
        assert_eq!(r.text, "need 5 more");
        assert_eq!(r.quantity, Some(5));
        assert_eq!(r.source, "miniapp");
        assert!(r.fulfilled_at.is_none());
        assert!(r.agent_note.is_none());
    }

    #[tokio::test]
    async fn lifecycle_open_in_progress_fulfilled() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        let r = add_request(&db, NewRequest { poster_id: "p1", text: "x", source: "miniapp", ..Default::default() })
            .await
            .unwrap();

        let r = update_request(&db, &r.id, Some("in_progress"), Some("on it"))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(r.status, "in_progress");
        assert_eq!(r.agent_note.as_deref(), Some("on it"));
        assert!(r.fulfilled_at.is_none(), "fulfilled_at only stamps on fulfilled");

        let r = update_request(&db, &r.id, Some("fulfilled"), None)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(r.status, "fulfilled");
        assert!(r.fulfilled_at.is_some(), "fulfilled must stamp fulfilled_at");
        assert_eq!(
            r.agent_note.as_deref(),
            Some("on it"),
            "a None note must not clear the stored one"
        );
    }

    #[tokio::test]
    async fn cancelled_is_a_valid_terminal_state() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        let r = add_request(&db, NewRequest { poster_id: "p1", text: "x", source: "miniapp", ..Default::default() })
            .await
            .unwrap();
        let r = update_request(&db, &r.id, Some("cancelled"), None)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(r.status, "cancelled");
        assert!(r.fulfilled_at.is_none());
    }

    #[tokio::test]
    async fn invalid_status_is_rejected_with_python_message() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        let r = add_request(&db, NewRequest { poster_id: "p1", text: "x", source: "miniapp", ..Default::default() })
            .await
            .unwrap();
        let err = update_request(&db, &r.id, Some("weird"), None)
            .await
            .unwrap_err();
        assert_eq!(
            err.to_string(),
            "invalid status 'weird'; expected one of ('open', 'in_progress', 'fulfilled', 'cancelled')"
        );
        // And the row is untouched.
        assert_eq!(get_request(&db, &r.id).await.unwrap().unwrap().status, "open");
    }

    #[tokio::test]
    async fn update_unknown_id_returns_none() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        let out = update_request(&db, "nope", Some("fulfilled"), None).await.unwrap();
        assert!(out.is_none());
    }

    #[tokio::test]
    async fn list_filters_by_poster_and_status_newest_first() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        let a = add_request(&db, NewRequest { poster_id: "p1", text: "a", source: "miniapp", ..Default::default() })
            .await
            .unwrap();
        // Force distinct created_at ordering (sub-second inserts can tie).
        sqlx::query("UPDATE content_requests SET created_at = '2026-01-01T00:00:00.000Z' WHERE id = ?")
            .bind(&a.id)
            .execute(db.writer())
            .await
            .unwrap();
        let b = add_request(&db, NewRequest { poster_id: "p1", text: "b", source: "miniapp", ..Default::default() })
            .await
            .unwrap();
        let c = add_request(&db, NewRequest { poster_id: "p2", text: "c", source: "miniapp", ..Default::default() })
            .await
            .unwrap();
        update_request(&db, &b.id, Some("fulfilled"), None).await.unwrap();

        let all = list_requests(&db, None, None).await.unwrap();
        assert_eq!(all.len(), 3);
        assert_eq!(all.last().unwrap().id, a.id, "oldest last (newest first)");

        let p1 = list_requests(&db, Some("p1"), None).await.unwrap();
        assert_eq!(p1.len(), 2);
        assert!(p1.iter().all(|r| r.poster_id == "p1"));

        let open = list_requests(&db, None, Some("open")).await.unwrap();
        assert_eq!(open.len(), 2);
        assert!(open.iter().any(|r| r.id == c.id));

        let p1_open = list_requests(&db, Some("p1"), Some("open")).await.unwrap();
        assert_eq!(p1_open.len(), 1);
        assert_eq!(p1_open[0].id, a.id);
    }

    #[tokio::test]
    async fn serializes_with_python_field_names_and_timestamp_format() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        let r = add_request(&db, NewRequest { poster_id: "p1", text: "x", source: "miniapp", ..Default::default() })
            .await
            .unwrap();
        let v = serde_json::to_value(&r).unwrap();
        for key in [
            "id", "poster_id", "poster_name", "page_id", "page_name", "text",
            "quantity", "status", "source", "created_at", "updated_at",
            "fulfilled_at", "agent_note",
        ] {
            assert!(v.get(key).is_some(), "missing field {key}");
        }
        let ts = v["created_at"].as_str().unwrap();
        assert!(
            ts.ends_with(".000Z") && ts.len() == 24,
            "timestamp must match Python's %Y-%m-%dT%H:%M:%S.000Z, got {ts}"
        );
        assert!(v["fulfilled_at"].is_null());
    }

    // ── User → poster resolution ─────────────────────────────────────────

    #[tokio::test]
    async fn uid_binding_wins_over_username() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        seed_poster(&db, "seffra", "Seffra").await;
        seed_poster(&db, "gigi", "Gigi").await;
        bind_poster_user(&db, "seffra", 777).await.unwrap();
        set_username(&db, "gigi", "someuser").await;

        // uid points at seffra even though the username matches gigi.
        let p = poster_for_user(&db, Some(777), Some("someuser")).await.unwrap().unwrap();
        assert_eq!(p.id, "seffra");
    }

    #[tokio::test]
    async fn username_fallback_is_case_insensitive_and_strips_at() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        seed_poster(&db, "gigi", "Gigi").await;
        set_username(&db, "gigi", "@GiGi_TT").await;

        let p = poster_for_user(&db, Some(999), Some("gigi_tt")).await.unwrap().unwrap();
        assert_eq!(p.id, "gigi", "unbound uid must fall through to username");
        let p = poster_for_user(&db, None, Some("@GIGI_tt")).await.unwrap().unwrap();
        assert_eq!(p.id, "gigi");
    }

    #[tokio::test]
    async fn unknown_user_resolves_to_none() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        seed_poster(&db, "gigi", "Gigi").await;
        assert!(poster_for_user(&db, Some(1), Some("nobody")).await.unwrap().is_none());
        assert!(poster_for_user(&db, None, None).await.unwrap().is_none());
        // Empty stored username must not match an empty query username.
        assert!(poster_for_user(&db, None, Some("")).await.unwrap().is_none());
    }

    // ── Content reads ────────────────────────────────────────────────────

    #[tokio::test]
    async fn ready_videos_excludes_unready_and_source_kinds() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        repo::create_project(&db, "proj").await.unwrap();
        let ready = repo::create_asset(&db, "proj", AssetKind::Burned, None, None).await.unwrap();
        repo::mark_asset_ready(&db, &ready.id, "projects/proj/burned/j1/burned_000.mp4")
            .await
            .unwrap();
        // queued clip → excluded; ready source → excluded by kind.
        repo::create_asset(&db, "proj", AssetKind::Clip, None, None).await.unwrap();
        let src = repo::create_asset(&db, "proj", AssetKind::Source, None, None).await.unwrap();
        repo::mark_asset_ready(&db, &src.id, "long.mp4").await.unwrap();

        let vids = ready_videos_for_project(&db, "proj").await.unwrap();
        assert_eq!(vids.len(), 1);
        assert_eq!(vids[0].id, ready.id);
    }

    #[tokio::test]
    async fn settings_and_assignments_read_back() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        sqlx::query("INSERT INTO settings (key, value, updated_at) VALUES ('bot_token', 'tok123', ?)")
            .bind(Utc::now())
            .execute(db.writer())
            .await
            .unwrap();
        assert_eq!(get_setting(&db, "bot_token").await.unwrap().as_deref(), Some("tok123"));
        assert!(get_setting(&db, "nope").await.unwrap().is_none());

        seed_poster(&db, "seffra", "Seffra").await;
        repo::upsert_page(&db, "pg1", "Page One", Some("proj")).await.unwrap();
        repo::assign_page(&db, "seffra", "pg1").await.unwrap();
        assert_eq!(assigned_page_ids(&db, "seffra").await.unwrap(), vec!["pg1".to_string()]);
    }
}
