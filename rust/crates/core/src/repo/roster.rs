//! Page roster repository — the port of `services/roster.py`.
//!
//! The old store was `page_roster.json`: one dict of page entries with a fixed
//! key vocabulary (see `KNOWN_KEYS`). Here the queried/joined fields are real
//! columns on `pages` and everything else rides in the `extra` JSON column
//! (exactly how `import.rs` migrated the file). Reads reconstruct the original
//! JSON entry shape — column values win over `extra`, `id` becomes
//! `integration_id`, timestamps use the legacy `.000Z` format — so the React
//! frontend sees byte-identical entries.

use std::collections::HashMap;

use chrono::{DateTime, Utc};
use serde_json::{Map, Value};

use crate::db::Db;
use crate::repo::sounds::legacy_ts;

/// Every key a roster entry can carry (the fixed vocabulary from the old
/// `set_page`). Writes rebuild entries from this list — unknown keys are
/// dropped, exactly like the Python service did.
const KNOWN_KEYS: &[&str] = &[
    "name",
    "provider",
    "picture",
    "project",
    "drive_folder_url",
    "drive_folder_id",
    "email_alias",
    "email_rule_id",
    "fwd_destination",
    "source",
    "tiktok_url",
    "signup_email",
    "fwd_address",
    "password",
    "poster_name",
    "group",
    "group_label",
    "account_type",
    "notes",
    "notion_page_id",
    "status",
    "pipeline",
    "page_type",
    "sounds_reference",
    "go_live_date",
    "r2_prefix",
    "r2_bucket",
];

/// Keys that map to real columns on `pages` (rest live in `extra`).
const COLUMN_KEYS: &[&str] = &[
    "name",
    "project",
    "r2_prefix",
    "provider",
    "poster_name",
    "drive_folder_url",
    "drive_folder_id",
    "r2_bucket",
    "source",
    "status",
    "notion_page_id",
    "tiktok_url",
];

/// A full roster row: columns + the lossless `extra` remainder.
#[derive(Debug, Clone, sqlx::FromRow)]
pub struct RosterPage {
    pub id: String,
    pub name: String,
    pub project: Option<String>,
    pub r2_prefix: Option<String>,
    pub provider: Option<String>,
    pub poster_name: Option<String>,
    pub drive_folder_url: Option<String>,
    pub drive_folder_id: Option<String>,
    pub r2_bucket: Option<String>,
    pub source: Option<String>,
    pub status: Option<String>,
    pub notion_page_id: Option<String>,
    pub tiktok_url: Option<String>,
    pub extra: Option<String>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

fn opt(v: &Option<String>) -> Value {
    v.as_ref().map(|s| Value::String(s.clone())).unwrap_or(Value::Null)
}

impl RosterPage {
    /// Reconstruct the legacy roster-entry JSON: `extra` keys first, then
    /// column values on top, with every known key present (null when unset).
    pub fn to_value(&self) -> Value {
        let mut obj: Map<String, Value> = self
            .extra
            .as_deref()
            .and_then(|raw| serde_json::from_str::<Value>(raw).ok())
            .and_then(|v| v.as_object().cloned())
            .unwrap_or_default();

        // Known keys not backed by a column: present-with-null when absent,
        // matching the entry shape the old set_page always wrote.
        for key in KNOWN_KEYS {
            if !COLUMN_KEYS.contains(key) {
                obj.entry(key.to_string()).or_insert(Value::Null);
            }
        }

        obj.insert("integration_id".into(), Value::String(self.id.clone()));
        obj.insert("name".into(), Value::String(self.name.clone()));
        obj.insert("provider".into(), opt(&self.provider));
        obj.insert("project".into(), opt(&self.project));
        obj.insert("drive_folder_url".into(), opt(&self.drive_folder_url));
        obj.insert("drive_folder_id".into(), opt(&self.drive_folder_id));
        obj.insert("source".into(), opt(&self.source));
        obj.insert("tiktok_url".into(), opt(&self.tiktok_url));
        obj.insert("poster_name".into(), opt(&self.poster_name));
        obj.insert("notion_page_id".into(), opt(&self.notion_page_id));
        obj.insert("status".into(), opt(&self.status));
        obj.insert("r2_prefix".into(), opt(&self.r2_prefix));
        obj.insert("r2_bucket".into(), opt(&self.r2_bucket));
        obj.insert("added_at".into(), Value::String(legacy_ts(&self.created_at)));
        obj.insert("updated_at".into(), Value::String(legacy_ts(&self.updated_at)));
        Value::Object(obj)
    }
}

const SELECT_PAGE: &str = "SELECT id, name, project, r2_prefix, provider, poster_name,
    drive_folder_url, drive_folder_id, r2_bucket, source, status, notion_page_id,
    tiktok_url, extra, created_at, updated_at FROM pages";

/// All roster pages, in insertion order (the old dict preserved it).
pub async fn list_pages_full(db: &Db) -> Result<Vec<RosterPage>, sqlx::Error> {
    sqlx::query_as::<_, RosterPage>(&format!("{SELECT_PAGE} ORDER BY rowid"))
        .fetch_all(db.reader())
        .await
}

pub async fn get_page_full(db: &Db, id: &str) -> Result<Option<RosterPage>, sqlx::Error> {
    sqlx::query_as::<_, RosterPage>(&format!("{SELECT_PAGE} WHERE id = ?"))
        .bind(id)
        .fetch_optional(db.reader())
        .await
}

/// Pages assigned to a project.
pub async fn list_pages_for_project(
    db: &Db,
    project: &str,
) -> Result<Vec<RosterPage>, sqlx::Error> {
    sqlx::query_as::<_, RosterPage>(&format!("{SELECT_PAGE} WHERE project = ? ORDER BY rowid"))
        .bind(project)
        .fetch_all(db.reader())
        .await
}

/// Extract a Google Drive folder id from a URL
/// (`.../folders/<id>` with optional query string).
pub fn parse_drive_folder_id(url: &str) -> Option<String> {
    let idx = url.find("folders/")? + "folders/".len();
    let id: String = url[idx..]
        .chars()
        .take_while(|c| c.is_ascii_alphanumeric() || *c == '_' || *c == '-')
        .collect();
    if id.is_empty() {
        None
    } else {
        Some(id)
    }
}

fn as_opt_string(v: Option<&Value>) -> Option<String> {
    match v {
        Some(Value::String(s)) => Some(s.clone()),
        Some(Value::Null) | None => None,
        Some(other) => Some(other.to_string()),
    }
}

/// Create or update a page entry with the old `set_page` merge semantics:
/// for each known key, a key present in `data` wins (even when null),
/// otherwise the existing value is kept. Unknown keys are dropped. Auto-parses
/// `drive_folder_id` when `drive_folder_url` changed. Returns the saved row.
pub async fn set_page(
    db: &Db,
    integration_id: &str,
    data: &Map<String, Value>,
) -> Result<RosterPage, sqlx::Error> {
    let existing = get_page_full(db, integration_id).await?;
    let existing_entry: Map<String, Value> = existing
        .as_ref()
        .map(|p| p.to_value().as_object().cloned().unwrap_or_default())
        .unwrap_or_default();

    let mut entry: Map<String, Value> = Map::new();
    for key in KNOWN_KEYS {
        let value = if data.contains_key(*key) {
            data.get(*key).cloned().unwrap_or(Value::Null)
        } else {
            existing_entry.get(*key).cloned().unwrap_or(Value::Null)
        };
        entry.insert(key.to_string(), value);
    }

    // Auto-parse drive folder id when the URL changed (same rule as Python).
    let url = as_opt_string(entry.get("drive_folder_url"));
    let old_url = existing.as_ref().and_then(|p| p.drive_folder_url.clone());
    if let Some(url) = url.filter(|u| !u.is_empty()) {
        if Some(&url) != old_url.as_ref() {
            if let Some(folder_id) = parse_drive_folder_id(&url) {
                entry.insert("drive_folder_id".into(), Value::String(folder_id));
            }
        }
    }

    let name = as_opt_string(entry.get("name")).unwrap_or_default();
    let extra: Map<String, Value> = entry
        .iter()
        .filter(|(k, v)| !COLUMN_KEYS.contains(&k.as_str()) && !v.is_null())
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    let extra_json = if extra.is_empty() {
        None
    } else {
        Some(Value::Object(extra).to_string())
    };

    let created_at = existing.as_ref().map(|p| p.created_at).unwrap_or_else(Utc::now);
    let updated_at = Utc::now();

    sqlx::query(
        "INSERT INTO pages
           (id, name, project, r2_prefix, provider, poster_name, drive_folder_url,
            drive_folder_id, r2_bucket, source, status, notion_page_id, tiktok_url,
            extra, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(id) DO UPDATE SET
           name = excluded.name, project = excluded.project,
           r2_prefix = excluded.r2_prefix, provider = excluded.provider,
           poster_name = excluded.poster_name,
           drive_folder_url = excluded.drive_folder_url,
           drive_folder_id = excluded.drive_folder_id,
           r2_bucket = excluded.r2_bucket, source = excluded.source,
           status = excluded.status, notion_page_id = excluded.notion_page_id,
           tiktok_url = excluded.tiktok_url, extra = excluded.extra,
           updated_at = excluded.updated_at",
    )
    .bind(integration_id)
    .bind(&name)
    .bind(as_opt_string(entry.get("project")))
    .bind(as_opt_string(entry.get("r2_prefix")))
    .bind(as_opt_string(entry.get("provider")))
    .bind(as_opt_string(entry.get("poster_name")))
    .bind(as_opt_string(entry.get("drive_folder_url")))
    .bind(as_opt_string(entry.get("drive_folder_id")))
    .bind(as_opt_string(entry.get("r2_bucket")))
    .bind(as_opt_string(entry.get("source")))
    .bind(as_opt_string(entry.get("status")))
    .bind(as_opt_string(entry.get("notion_page_id")))
    .bind(as_opt_string(entry.get("tiktok_url")))
    .bind(extra_json)
    .bind(created_at)
    .bind(updated_at)
    .execute(db.writer())
    .await?;

    get_page_full(db, integration_id)
        .await?
        .ok_or(sqlx::Error::RowNotFound)
}

/// Remove a page. Returns true if it existed. FK cascades take the page's
/// topics, playlist, poster assignment, and inventory rows with it.
pub async fn remove_page(db: &Db, integration_id: &str) -> Result<bool, sqlx::Error> {
    let res = sqlx::query("DELETE FROM pages WHERE id = ?")
        .bind(integration_id)
        .execute(db.writer())
        .await?;
    Ok(res.rows_affected() > 0)
}

// ── Cross-table reads for the duplicates / dedup endpoints ──────────────────

/// `{page_id: (topic_id, topic_name)}` for the shared staging group.
pub async fn staging_topics(db: &Db) -> Result<HashMap<String, (i64, String)>, sqlx::Error> {
    let rows = sqlx::query_as::<_, (String, i64, String)>(
        "SELECT page_id, topic_id, topic_name FROM topics WHERE scope = 'staging'",
    )
    .fetch_all(db.reader())
    .await?;
    Ok(rows.into_iter().map(|(p, id, name)| (p, (id, name))).collect())
}

/// Per-page inventory tallies: `{page_id: (total, pending, forwarded)}`.
pub async fn inventory_counts(db: &Db) -> Result<HashMap<String, (i64, i64, i64)>, sqlx::Error> {
    let rows = sqlx::query_as::<_, (String, i64, i64, i64)>(
        "SELECT page_id, COUNT(*),
                COALESCE(SUM(forwarded_to IS NULL), 0),
                COALESCE(SUM(forwarded_to IS NOT NULL), 0)
         FROM telegram_inventory GROUP BY page_id",
    )
    .fetch_all(db.reader())
    .await?;
    Ok(rows.into_iter().map(|(p, t, pe, f)| (p, (t, pe, f))).collect())
}

/// Reassign every inventory row from one page to another (dedup merge).
/// Returns how many rows moved.
pub async fn move_inventory(db: &Db, from: &str, to: &str) -> Result<u64, sqlx::Error> {
    let res = sqlx::query("UPDATE telegram_inventory SET page_id = ? WHERE page_id = ?")
        .bind(to)
        .bind(from)
        .execute(db.writer())
        .await?;
    Ok(res.rows_affected())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn map(v: Value) -> Map<String, Value> {
        v.as_object().cloned().unwrap()
    }

    #[tokio::test]
    async fn set_page_merges_like_the_old_store() {
        let (db, _g) = Db::connect_temp().await.unwrap();

        let page = set_page(
            &db,
            "acct:one",
            &map(json!({
                "name": "One",
                "provider": "tiktok",
                "signup_email": "a@b.c",
                "bogus_key": "dropped",
            })),
        )
        .await
        .unwrap();
        assert_eq!(page.name, "One");

        let v = page.to_value();
        assert_eq!(v["integration_id"], "acct:one");
        assert_eq!(v["signup_email"], "a@b.c");
        assert!(v.get("bogus_key").is_none(), "unknown keys dropped");
        assert!(v["project"].is_null(), "known keys present-with-null");
        assert!(v["added_at"].as_str().unwrap().ends_with(".000Z"));

        // Partial update: untouched fields survive, provided null overwrites.
        let page = set_page(
            &db,
            "acct:one",
            &map(json!({"project": "proj-a", "signup_email": null})),
        )
        .await
        .unwrap();
        let v = page.to_value();
        assert_eq!(v["project"], "proj-a");
        assert_eq!(v["name"], "One", "existing field preserved");
        assert!(v["signup_email"].is_null(), "explicit null wins");
    }

    #[tokio::test]
    async fn drive_folder_id_autoparses_on_url_change() {
        let (db, _g) = Db::connect_temp().await.unwrap();
        let page = set_page(
            &db,
            "pg",
            &map(json!({
                "name": "P",
                "drive_folder_url": "https://drive.google.com/drive/u/0/folders/1AbC_d-9?usp=sharing"
            })),
        )
        .await
        .unwrap();
        assert_eq!(page.drive_folder_id.as_deref(), Some("1AbC_d-9"));

        // unchanged URL → drive_folder_id untouched
        let page = set_page(&db, "pg", &map(json!({"project": "x"}))).await.unwrap();
        assert_eq!(page.drive_folder_id.as_deref(), Some("1AbC_d-9"));
    }

    #[test]
    fn parse_drive_folder_id_variants() {
        for (url, want) in [
            ("https://drive.google.com/drive/folders/1AbCdEf", Some("1AbCdEf")),
            ("https://drive.google.com/drive/folders/1AbCdEf?usp=sharing", Some("1AbCdEf")),
            ("https://drive.google.com/drive/u/0/folders/1Ab-c_D", Some("1Ab-c_D")),
            ("https://example.com/nope", None),
            ("folders/", None),
        ] {
            assert_eq!(parse_drive_folder_id(url).as_deref(), want, "{url}");
        }
    }

    #[tokio::test]
    async fn list_and_project_filter_and_remove() {
        let (db, _g) = Db::connect_temp().await.unwrap();
        set_page(&db, "a", &map(json!({"name": "A", "project": "p1"}))).await.unwrap();
        set_page(&db, "b", &map(json!({"name": "B", "project": "p2"}))).await.unwrap();
        set_page(&db, "c", &map(json!({"name": "C", "project": "p1"}))).await.unwrap();

        let all = list_pages_full(&db).await.unwrap();
        assert_eq!(all.iter().map(|p| p.id.as_str()).collect::<Vec<_>>(), vec!["a", "b", "c"]);

        let p1 = list_pages_for_project(&db, "p1").await.unwrap();
        assert_eq!(p1.len(), 2);

        assert!(remove_page(&db, "b").await.unwrap());
        assert!(!remove_page(&db, "b").await.unwrap());
        assert!(get_page_full(&db, "b").await.unwrap().is_none());
    }

    #[tokio::test]
    async fn inventory_moves_and_counts() {
        let (db, _g) = Db::connect_temp().await.unwrap();
        set_page(&db, "keep", &map(json!({"name": "K"}))).await.unwrap();
        set_page(&db, "dupe", &map(json!({"name": "K "}))).await.unwrap();
        let now = Utc::now();
        for (id, page, fwd) in [
            ("i1", "dupe", None),
            ("i2", "dupe", Some("seffra")),
            ("i3", "keep", None),
        ] {
            sqlx::query(
                "INSERT INTO telegram_inventory (id, page_id, forwarded_to, created_at) VALUES (?, ?, ?, ?)",
            )
            .bind(id)
            .bind(page)
            .bind(fwd)
            .bind(now)
            .execute(db.writer())
            .await
            .unwrap();
        }

        let counts = inventory_counts(&db).await.unwrap();
        assert_eq!(counts["dupe"], (2, 1, 1));
        assert_eq!(counts["keep"], (1, 1, 0));

        assert_eq!(move_inventory(&db, "dupe", "keep").await.unwrap(), 2);
        let counts = inventory_counts(&db).await.unwrap();
        assert_eq!(counts["keep"], (3, 2, 1));
        assert!(!counts.contains_key("dupe"));
    }

    #[tokio::test]
    async fn staging_topics_map() {
        let (db, _g) = Db::connect_temp().await.unwrap();
        set_page(&db, "pg", &map(json!({"name": "P"}))).await.unwrap();
        sqlx::query(
            "INSERT INTO topics (scope, owner_id, page_id, topic_id, topic_name, created_at)
             VALUES ('staging', '', 'pg', 7, 'P', ?)",
        )
        .bind(Utc::now())
        .execute(db.writer())
        .await
        .unwrap();

        let topics = staging_topics(&db).await.unwrap();
        assert_eq!(topics["pg"], (7, "P".to_string()));
    }
}
