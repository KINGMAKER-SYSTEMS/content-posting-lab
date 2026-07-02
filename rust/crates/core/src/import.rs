//! `clab migrate` — one-shot import of the legacy JSON state files into SQLite.
//!
//! Reads `telegram_config.json`, `page_roster.json`, and
//! `content_requests.json` from a directory (the Railway volume in prod) and
//! upserts everything into the relational schema. Idempotent: rows are keyed
//! by their original stable ids, so re-running refreshes rather than
//! duplicates. It does NOT delete rows absent from the JSON — it's a cutover
//! importer, not a sync engine. The JSON files are left untouched on disk as
//! the rollback snapshot.
//!
//! Everything runs in ONE transaction: either the whole snapshot lands or
//! nothing does.

use std::collections::BTreeMap;
use std::path::Path;

use chrono::{DateTime, Utc};
use serde_json::Value;
use sqlx::{Sqlite, Transaction};

use crate::db::Db;

/// What the import did — printed as the cutover report.
#[derive(Debug, Default)]
pub struct Report {
    /// rows upserted per table
    pub counts: BTreeMap<&'static str, u64>,
    /// data that needed judgement (missing chat_ids, stub pages, dropped fields)
    pub warnings: Vec<String>,
}

impl Report {
    fn bump(&mut self, table: &'static str) {
        *self.counts.entry(table).or_insert(0) += 1;
    }
    fn warn(&mut self, msg: impl Into<String>) {
        self.warnings.push(msg.into());
    }
}

impl std::fmt::Display for Report {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        writeln!(f, "── migrate report ──────────────────────────")?;
        for (table, n) in &self.counts {
            writeln!(f, "  {table:<24} {n:>6} rows")?;
        }
        if self.warnings.is_empty() {
            writeln!(f, "  no warnings")?;
        } else {
            writeln!(f, "  {} warning(s):", self.warnings.len())?;
            for w in &self.warnings {
                writeln!(f, "   ! {w}")?;
            }
        }
        Ok(())
    }
}

// ── JSON helpers ─────────────────────────────────────────────────────────────

fn s(v: &Value, key: &str) -> Option<String> {
    v.get(key).and_then(Value::as_str).map(str::to_string)
}

fn i(v: &Value, key: &str) -> Option<i64> {
    v.get(key).and_then(Value::as_i64)
}

/// Parse the legacy `%Y-%m-%dT%H:%M:%S.000Z` timestamps; fall back to now.
fn ts(v: &Value, key: &str) -> DateTime<Utc> {
    s(v, key)
        .and_then(|raw| DateTime::parse_from_rfc3339(&raw).ok())
        .map(|dt| dt.with_timezone(&Utc))
        .unwrap_or_else(Utc::now)
}

fn load_json(dir: &Path, name: &str, report: &mut Report) -> Option<Value> {
    let path = dir.join(name);
    if !path.exists() {
        report.warn(format!("{name}: not found — skipped"));
        return None;
    }
    match std::fs::read_to_string(&path).map_err(anyhow::Error::from).and_then(|raw| {
        serde_json::from_str::<Value>(&raw).map_err(anyhow::Error::from)
    }) {
        Ok(v) => Some(v),
        Err(e) => {
            report.warn(format!("{name}: unreadable ({e}) — skipped"));
            None
        }
    }
}

// ── The import ───────────────────────────────────────────────────────────────

/// Import all legacy JSON state from `dir`. See module docs.
pub async fn import_json_state(db: &Db, dir: &Path) -> anyhow::Result<Report> {
    let mut report = Report::default();
    let roster = load_json(dir, "page_roster.json", &mut report);
    let config = load_json(dir, "telegram_config.json", &mut report);
    let requests = load_json(dir, "content_requests.json", &mut report);

    let mut tx = db.writer().begin().await?;

    if let Some(roster) = &roster {
        import_pages(&mut tx, roster, &mut report).await?;
    }
    if let Some(config) = &config {
        import_telegram_config(&mut tx, config, &mut report).await?;
    }
    if let Some(requests) = &requests {
        import_content_requests(&mut tx, requests, &mut report).await?;
    }

    tx.commit().await?;
    Ok(report)
}

/// Roster fields that get real columns; everything else lands in `extra` JSON.
const PAGE_COLUMNS: &[&str] = &[
    "integration_id", "name", "project", "r2_prefix", "provider", "poster_name",
    "drive_folder_url", "drive_folder_id", "r2_bucket", "source", "status",
    "notion_page_id", "tiktok_url", "added_at", "updated_at",
];

async fn import_pages(
    tx: &mut Transaction<'_, Sqlite>,
    roster: &Value,
    report: &mut Report,
) -> anyhow::Result<()> {
    let Some(pages) = roster.get("pages").and_then(Value::as_object) else {
        report.warn("page_roster.json: no pages object");
        return Ok(());
    };
    for (id, entry) in pages {
        // Lossless remainder: every key that didn't get a column.
        let extra: serde_json::Map<String, Value> = entry
            .as_object()
            .map(|o| {
                o.iter()
                    .filter(|(k, v)| !PAGE_COLUMNS.contains(&k.as_str()) && !v.is_null())
                    .map(|(k, v)| (k.clone(), v.clone()))
                    .collect()
            })
            .unwrap_or_default();
        let extra_json = if extra.is_empty() { None } else { Some(Value::Object(extra).to_string()) };

        sqlx::query(
            "INSERT OR REPLACE INTO pages
             (id, name, project, r2_prefix, provider, poster_name, drive_folder_url,
              drive_folder_id, r2_bucket, source, status, notion_page_id, tiktok_url,
              extra, created_at, updated_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        )
        .bind(id)
        .bind(s(entry, "name").unwrap_or_else(|| id.clone()))
        .bind(s(entry, "project"))
        .bind(s(entry, "r2_prefix"))
        .bind(s(entry, "provider"))
        .bind(s(entry, "poster_name"))
        .bind(s(entry, "drive_folder_url"))
        .bind(s(entry, "drive_folder_id"))
        .bind(s(entry, "r2_bucket"))
        .bind(s(entry, "source"))
        .bind(s(entry, "status"))
        .bind(s(entry, "notion_page_id"))
        .bind(s(entry, "tiktok_url"))
        .bind(extra_json)
        .bind(ts(entry, "added_at"))
        .bind(ts(entry, "updated_at"))
        .execute(&mut **tx)
        .await?;
        report.bump("pages");
    }
    Ok(())
}

/// Insert a placeholder page row so FK-dependent rows (topics, inventory,
/// poster_pages) referencing a page missing from the roster still import.
async fn ensure_page(
    tx: &mut Transaction<'_, Sqlite>,
    page_id: &str,
    referenced_by: &str,
    report: &mut Report,
) -> anyhow::Result<()> {
    let res = sqlx::query(
        "INSERT OR IGNORE INTO pages (id, name, source, created_at, updated_at)
         VALUES (?, ?, 'stub', ?, ?)",
    )
    .bind(page_id)
    .bind(page_id)
    .bind(Utc::now())
    .bind(Utc::now())
    .execute(&mut **tx)
    .await?;
    if res.rows_affected() > 0 {
        report.bump("pages (stubs)");
        report.warn(format!(
            "page '{page_id}' referenced by {referenced_by} but not in the roster — created stub"
        ));
    }
    Ok(())
}

async fn import_telegram_config(
    tx: &mut Transaction<'_, Sqlite>,
    config: &Value,
    report: &mut Report,
) -> anyhow::Result<()> {
    let now = Utc::now();

    // settings: staging group identity + bot identity
    let staging = config.get("staging_group").cloned().unwrap_or(Value::Null);
    let mut settings: Vec<(&str, Option<String>)> = vec![
        ("staging_chat_id", i(&staging, "chat_id").map(|v| v.to_string())),
        ("staging_name", s(&staging, "name")),
        ("bot_username", s(config, "bot_username")),
        ("bot_token", s(config, "bot_token")),
    ];
    for (key, value) in settings.drain(..) {
        if let Some(value) = value {
            sqlx::query("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)")
                .bind(key)
                .bind(value)
                .bind(now)
                .execute(&mut **tx)
                .await?;
            report.bump("settings");
        }
    }

    // staging topics → topics(scope='staging'). The per-topic
    // last_forwarded_id watermark is deliberately dropped: forwarding is
    // per-inventory-row in the new model (the watermark WAS the data-loss bug).
    let mut dropped_watermarks = 0u32;
    if let Some(topics) = staging.get("topics").and_then(Value::as_object) {
        for (page_id, topic) in topics {
            let Some(topic_id) = i(topic, "topic_id") else {
                report.warn(format!("staging topic for '{page_id}' has no topic_id — skipped"));
                continue;
            };
            ensure_page(tx, page_id, "a staging topic", report).await?;
            sqlx::query(
                "INSERT OR REPLACE INTO topics (scope, owner_id, page_id, topic_id, topic_name, created_at)
                 VALUES ('staging', '', ?, ?, ?, ?)",
            )
            .bind(page_id)
            .bind(topic_id)
            .bind(s(topic, "topic_name").unwrap_or_default())
            .bind(now)
            .execute(&mut **tx)
            .await?;
            report.bump("topics");
            if i(topic, "last_forwarded_id").unwrap_or(0) > 0 {
                dropped_watermarks += 1;
            }
        }
    }
    if dropped_watermarks > 0 {
        report.warn(format!(
            "dropped {dropped_watermarks} last_forwarded_id watermark(s) — retired by design; \
             forwarding is tracked per inventory row now"
        ));
    }

    // posters (+ pages, topics, user bindings)
    if let Some(posters) = config.get("posters").and_then(Value::as_object) {
        for (poster_id, poster) in posters {
            // ponytail: chat_id 0 = "no group configured"; bot code must refuse
            // to send to 0. Beats making the column nullable across the stack.
            let chat_id = i(poster, "chat_id").unwrap_or_else(|| {
                report.warn(format!("poster '{poster_id}' has no chat_id — imported as 0 (unconfigured)"));
                0
            });
            sqlx::query(
                "INSERT OR REPLACE INTO posters
                 (id, name, chat_id, sounds_topic_id, telegram_username, created_at, updated_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?)",
            )
            .bind(poster_id)
            .bind(s(poster, "name").unwrap_or_else(|| poster_id.clone()))
            .bind(chat_id)
            .bind(i(poster, "sounds_topic_id"))
            .bind(s(poster, "telegram_username").map(|u| u.trim_start_matches('@').to_string()))
            .bind(ts(poster, "added_at"))
            .bind(ts(poster, "updated_at"))
            .execute(&mut **tx)
            .await?;
            report.bump("posters");

            for page_id in poster
                .get("page_ids")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
            {
                ensure_page(tx, page_id, &format!("poster '{poster_id}'"), report).await?;
                sqlx::query("INSERT OR REPLACE INTO poster_pages (poster_id, page_id) VALUES (?, ?)")
                    .bind(poster_id)
                    .bind(page_id)
                    .execute(&mut **tx)
                    .await?;
                report.bump("poster_pages");
            }

            if let Some(topics) = poster.get("topics").and_then(Value::as_object) {
                for (page_id, topic) in topics {
                    let Some(topic_id) = i(topic, "topic_id") else { continue };
                    ensure_page(tx, page_id, &format!("poster '{poster_id}' topic"), report).await?;
                    sqlx::query(
                        "INSERT OR REPLACE INTO topics (scope, owner_id, page_id, topic_id, topic_name, created_at)
                         VALUES ('poster', ?, ?, ?, ?, ?)",
                    )
                    .bind(poster_id)
                    .bind(page_id)
                    .bind(topic_id)
                    .bind(s(topic, "topic_name").unwrap_or_default())
                    .bind(now)
                    .execute(&mut **tx)
                    .await?;
                    report.bump("topics");
                }
            }

            for uid in poster
                .get("telegram_user_ids")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(Value::as_i64)
            {
                let res = sqlx::query(
                    "INSERT OR IGNORE INTO poster_users (poster_id, telegram_user_id) VALUES (?, ?)",
                )
                .bind(poster_id)
                .bind(uid)
                .execute(&mut **tx)
                .await?;
                if res.rows_affected() > 0 {
                    report.bump("poster_users");
                } else {
                    report.warn(format!(
                        "telegram user {uid} already bound to another poster — kept first binding \
                         (was nondeterministic in the JSON store)"
                    ));
                }
            }
        }
    }

    // inventory → telegram_inventory. One row per real message; media details
    // (file_id etc.) ride in meta JSON so the bot can re-send without re-upload.
    if let Some(inventory) = config.get("inventory").and_then(Value::as_object) {
        for (page_id, items) in inventory {
            ensure_page(tx, page_id, "inventory", report).await?;
            for item in items.as_array().into_iter().flatten() {
                let Some(item_id) = s(item, "id") else { continue };
                let fwd = item.get("forwarded").filter(|f| f.as_object().is_some_and(|o| !o.is_empty()));
                let meta: serde_json::Map<String, Value> = item
                    .as_object()
                    .map(|o| {
                        o.iter()
                            .filter(|(k, v)| {
                                !["id", "added_at", "forwarded", "message_id"].contains(&k.as_str())
                                    && !v.is_null()
                            })
                            .map(|(k, v)| (k.clone(), v.clone()))
                            .collect()
                    })
                    .unwrap_or_default();

                sqlx::query(
                    "INSERT OR REPLACE INTO telegram_inventory
                     (id, page_id, asset_id, staging_msg_id, forwarded_to, forwarded_msg_id,
                      meta, created_at, forwarded_at)
                     VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)",
                )
                .bind(&item_id)
                .bind(page_id)
                .bind(i(item, "message_id"))
                .bind(fwd.and_then(|f| s(f, "poster_id")))
                .bind(fwd.and_then(|f| i(f, "message_id")))
                .bind(if meta.is_empty() { None } else { Some(Value::Object(meta).to_string()) })
                .bind(ts(item, "added_at"))
                .bind(fwd.map(|f| ts(f, "forwarded_at")))
                .execute(&mut **tx)
                .await?;
                report.bump("telegram_inventory");
            }
        }
    }

    // sounds + per-page playlists
    if let Some(sounds) = config.get("sounds").and_then(Value::as_array) {
        for sound in sounds {
            let Some(id) = s(sound, "id") else { continue };
            sqlx::query(
                "INSERT OR REPLACE INTO sounds (id, url, label, active, created_at)
                 VALUES (?, ?, ?, ?, ?)",
            )
            .bind(&id)
            .bind(s(sound, "url").unwrap_or_default())
            .bind(s(sound, "label").unwrap_or_default())
            .bind(sound.get("active").and_then(Value::as_bool).unwrap_or(true))
            .bind(ts(sound, "added_at"))
            .execute(&mut **tx)
            .await?;
            report.bump("sounds");
        }
    }
    if let Some(playlists) = config.get("page_playlists").and_then(Value::as_object) {
        for (page_id, sound_ids) in playlists {
            ensure_page(tx, page_id, "a playlist", report).await?;
            for (pos, sound_id) in sound_ids
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .enumerate()
            {
                let res = sqlx::query(
                    "INSERT OR REPLACE INTO page_playlists (page_id, sound_id, position)
                     SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM sounds WHERE id = ?)",
                )
                .bind(page_id)
                .bind(sound_id)
                .bind(pos as i64)
                .bind(sound_id)
                .execute(&mut **tx)
                .await?;
                if res.rows_affected() > 0 {
                    report.bump("page_playlists");
                } else {
                    report.warn(format!(
                        "playlist for '{page_id}' references unknown sound '{sound_id}' — dropped \
                         (matches the old store's silent-drop behavior)"
                    ));
                }
            }
        }
    }

    // schedule (single row, id=1)
    if let Some(schedule) = config.get("schedule") {
        sqlx::query(
            "UPDATE schedule SET enabled = ?, forward_time = ?, timezone = ?, last_run = ? WHERE id = 1",
        )
        .bind(schedule.get("enabled").and_then(Value::as_bool).unwrap_or(false))
        .bind(s(schedule, "forward_time").unwrap_or_else(|| "09:00".into()))
        .bind(s(schedule, "timezone").unwrap_or_else(|| "America/New_York".into()))
        .bind(s(schedule, "last_run"))
        .execute(&mut **tx)
        .await?;
        report.bump("schedule");
    }

    Ok(())
}

async fn import_content_requests(
    tx: &mut Transaction<'_, Sqlite>,
    requests: &Value,
    report: &mut Report,
) -> anyhow::Result<()> {
    for req in requests
        .get("requests")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let Some(id) = s(req, "id") else { continue };
        let status = s(req, "status").unwrap_or_else(|| "open".into());
        if !["open", "in_progress", "fulfilled", "cancelled"].contains(&status.as_str()) {
            report.warn(format!("content request '{id}' has unknown status '{status}' — skipped"));
            continue;
        }
        sqlx::query(
            "INSERT OR REPLACE INTO content_requests
             (id, poster_id, poster_name, page_id, page_name, text, quantity, status,
              source, agent_note, created_at, updated_at, fulfilled_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        )
        .bind(&id)
        .bind(s(req, "poster_id").unwrap_or_default())
        .bind(s(req, "poster_name"))
        .bind(s(req, "page_id"))
        .bind(s(req, "page_name"))
        .bind(s(req, "text").unwrap_or_default())
        .bind(i(req, "quantity"))
        .bind(&status)
        .bind(s(req, "source").unwrap_or_else(|| "miniapp".into()))
        .bind(s(req, "agent_note"))
        .bind(ts(req, "created_at"))
        .bind(ts(req, "updated_at"))
        .bind(s(req, "fulfilled_at"))
        .execute(&mut **tx)
        .await?;
        report.bump("content_requests");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture_dir() -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("page_roster.json"),
            serde_json::json!({
                "version": 1,
                "pages": {
                    "pg1": {
                        "integration_id": "pg1", "name": "Page One", "provider": "tiktok",
                        "project": "proj-a", "poster_name": "Seffra", "source": "notion",
                        "signup_email": "x@y.z", "notes": "hand-run",
                        "added_at": "2026-04-01T10:00:00.000Z", "updated_at": "2026-04-02T10:00:00.000Z"
                    },
                    "pg2": { "integration_id": "pg2", "name": "Page Two" }
                }
            })
            .to_string(),
        )
        .unwrap();
        std::fs::write(
            dir.path().join("telegram_config.json"),
            serde_json::json!({
                "version": 1,
                "bot_username": "rt_bot",
                "staging_group": {
                    "chat_id": -100111, "name": "Staging",
                    "topics": {
                        "pg1": {"topic_id": 5, "topic_name": "Page One", "last_forwarded_id": 42},
                        // page NOT in roster → stub expected
                        "ghost": {"topic_id": 9, "topic_name": "Ghost"}
                    }
                },
                "posters": {
                    "seffra": {
                        "poster_id": "seffra", "name": "Seffra", "chat_id": -100222,
                        "page_ids": ["pg1"], "sounds_topic_id": 3,
                        "topics": {"pg1": {"topic_id": 7, "topic_name": "Page One"}},
                        "telegram_user_ids": [777], "telegram_username": "@seff",
                        "added_at": "2026-03-01T00:00:00.000Z", "updated_at": "2026-03-01T00:00:00.000Z"
                    },
                    "nogroup": {"poster_id": "nogroup", "name": "No Group", "telegram_user_ids": [777]}
                },
                "inventory": {
                    "pg1": [
                        {"id": "inv1", "file_id": "F1", "file_name": "a.mp4", "media_type": "video",
                         "message_id": 100, "chat_id": -100111, "source": "manual",
                         "added_at": "2026-04-03T00:00:00.000Z",
                         "forwarded": {"poster_id": "seffra", "message_id": 900,
                                        "forwarded_at": "2026-04-04T00:00:00.000Z"}},
                        {"id": "inv2", "file_id": "F2", "media_type": "video",
                         "message_id": 101, "forwarded": {}}
                    ]
                },
                "sounds": [
                    {"id": "snd1", "url": "https://tiktok.com/s/1", "label": "Song A", "active": true},
                    {"id": "snd2", "url": "https://tiktok.com/s/2", "label": "Song B", "active": false}
                ],
                "page_playlists": {"pg1": ["snd2", "snd1", "missing-sound"]},
                "schedule": {"enabled": true, "forward_time": "10:30", "timezone": "America/New_York",
                              "last_run": "2026-04-05T09:00:00.000Z"}
            })
            .to_string(),
        )
        .unwrap();
        std::fs::write(
            dir.path().join("content_requests.json"),
            serde_json::json!({
                "version": 1,
                "requests": [
                    {"id": "req1", "poster_id": "seffra", "text": "need 5 more", "quantity": 5,
                     "status": "open", "source": "miniapp",
                     "created_at": "2026-04-06T00:00:00.000Z", "updated_at": "2026-04-06T00:00:00.000Z"},
                    {"id": "req2", "poster_id": "seffra", "text": "?", "status": "weird"}
                ]
            })
            .to_string(),
        )
        .unwrap();
        dir
    }

    async fn count(db: &Db, table: &str) -> i64 {
        sqlx::query_scalar::<_, i64>(&format!("SELECT COUNT(*) FROM {table}"))
            .fetch_one(db.reader())
            .await
            .unwrap()
    }

    #[tokio::test]
    async fn full_import_lands_every_store() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        let dir = fixture_dir();
        let report = import_json_state(&db, dir.path()).await.unwrap();

        assert_eq!(count(&db, "pages").await, 3, "2 roster pages + 1 ghost stub");
        assert_eq!(count(&db, "posters").await, 2);
        assert_eq!(count(&db, "poster_pages").await, 1);
        assert_eq!(count(&db, "topics").await, 3, "2 staging + 1 poster topic");
        assert_eq!(count(&db, "telegram_inventory").await, 2);
        assert_eq!(count(&db, "sounds").await, 2);
        assert_eq!(count(&db, "page_playlists").await, 2, "missing-sound dropped");
        assert_eq!(count(&db, "content_requests").await, 1, "bad-status row skipped");
        assert_eq!(count(&db, "poster_users").await, 1, "uid 777 bound once, dup warned");
        assert_eq!(count(&db, "settings").await, 3, "staging chat/name + bot_username");

        // The forwarded item kept its forwarding facts; the pending one is pending.
        let fwd: Option<String> = sqlx::query_scalar(
            "SELECT forwarded_to FROM telegram_inventory WHERE id = 'inv1'",
        )
        .fetch_one(db.reader())
        .await
        .unwrap();
        assert_eq!(fwd.as_deref(), Some("seffra"));
        let pending: Option<String> = sqlx::query_scalar(
            "SELECT forwarded_to FROM telegram_inventory WHERE id = 'inv2'",
        )
        .fetch_one(db.reader())
        .await
        .unwrap();
        assert!(pending.is_none(), "empty forwarded{{}} must import as pending");

        // file_id survives in meta for re-sends.
        let meta: Option<String> =
            sqlx::query_scalar("SELECT meta FROM telegram_inventory WHERE id = 'inv1'")
                .fetch_one(db.reader())
                .await
                .unwrap();
        assert!(meta.unwrap().contains("\"file_id\":\"F1\""));

        // Lossless roster remainder.
        let extra: Option<String> =
            sqlx::query_scalar("SELECT extra FROM pages WHERE id = 'pg1'")
                .fetch_one(db.reader())
                .await
                .unwrap();
        let extra: Value = serde_json::from_str(&extra.unwrap()).unwrap();
        assert_eq!(extra["signup_email"], "x@y.z");
        assert_eq!(extra["notes"], "hand-run");

        // Playlist order preserved (snd2 first).
        let first: String = sqlx::query_scalar(
            "SELECT sound_id FROM page_playlists WHERE page_id = 'pg1' AND position = 0",
        )
        .fetch_one(db.reader())
        .await
        .unwrap();
        assert_eq!(first, "snd2");

        // Warnings called the judgement calls out.
        let joined = report.warnings.join("\n");
        assert!(joined.contains("ghost"), "stub page warned");
        assert!(joined.contains("nogroup"), "chat_id-less poster warned");
        assert!(joined.contains("watermark"), "dropped watermark warned");
        assert!(joined.contains("777"), "duplicate user binding warned");
    }

    #[tokio::test]
    async fn import_is_idempotent() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        let dir = fixture_dir();
        import_json_state(&db, dir.path()).await.unwrap();
        let first: i64 = count(&db, "pages").await
            + count(&db, "posters").await
            + count(&db, "topics").await
            + count(&db, "telegram_inventory").await
            + count(&db, "sounds").await
            + count(&db, "page_playlists").await
            + count(&db, "content_requests").await
            + count(&db, "poster_users").await;
        import_json_state(&db, dir.path()).await.unwrap();
        let second: i64 = count(&db, "pages").await
            + count(&db, "posters").await
            + count(&db, "topics").await
            + count(&db, "telegram_inventory").await
            + count(&db, "sounds").await
            + count(&db, "page_playlists").await
            + count(&db, "content_requests").await
            + count(&db, "poster_users").await;
        assert_eq!(first, second, "re-running migrate must not duplicate rows");
    }

    #[tokio::test]
    async fn missing_files_warn_but_succeed() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        let dir = tempfile::tempdir().unwrap();
        let report = import_json_state(&db, dir.path()).await.unwrap();
        assert_eq!(report.warnings.len(), 3, "one warning per missing file");
        assert!(report.counts.is_empty());
    }
}
