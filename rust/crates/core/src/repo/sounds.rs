//! Sound library + per-page playlists repository.
//!
//! Ports `services/telegram.py`'s sounds / page-playlists sections onto the
//! `sounds` and `page_playlists` tables. Behavior contracts carried over:
//!   - playlist writes silently drop sound ids that aren't in the pool
//!     (keeps playlists coherent, matches the old JSON store),
//!   - playlists keep inactive sounds (toggling a sound off must not lose
//!     assignments) — the message builder filters them at read time,
//!   - list order is insertion order (the old store was an append-only list).

use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize, Serializer};

use crate::db::Db;

/// Serialize a timestamp in the legacy JSON-store format
/// (`%Y-%m-%dT%H:%M:%S.000Z`) so API payloads are byte-compatible with the
/// Python app's `_now()` output.
pub fn legacy_ts(dt: &DateTime<Utc>) -> String {
    dt.format("%Y-%m-%dT%H:%M:%S.000Z").to_string()
}

fn ser_legacy_ts<S: Serializer>(dt: &DateTime<Utc>, s: S) -> Result<S::Ok, S::Error> {
    s.serialize_str(&legacy_ts(dt))
}

/// One TikTok sound in the library. Serializes to the exact wire shape the
/// old app used: `{id, url, label, active, added_at}`.
#[derive(Debug, Clone, Serialize, Deserialize, sqlx::FromRow)]
pub struct Sound {
    pub id: String,
    pub url: String,
    pub label: String,
    pub active: bool,
    #[serde(rename = "added_at", serialize_with = "ser_legacy_ts")]
    pub created_at: DateTime<Utc>,
}

/// List sounds in insertion order, optionally filtering to active-only.
pub async fn list_sounds(db: &Db, active_only: bool) -> Result<Vec<Sound>, sqlx::Error> {
    let sql = if active_only {
        "SELECT id, url, label, active, created_at FROM sounds WHERE active = 1 ORDER BY rowid"
    } else {
        "SELECT id, url, label, active, created_at FROM sounds ORDER BY rowid"
    };
    sqlx::query_as::<_, Sound>(sql).fetch_all(db.reader()).await
}

pub async fn get_sound(db: &Db, id: &str) -> Result<Option<Sound>, sqlx::Error> {
    sqlx::query_as::<_, Sound>("SELECT id, url, label, active, created_at FROM sounds WHERE id = ?")
        .bind(id)
        .fetch_optional(db.reader())
        .await
}

/// Add a new sound (active, freshly minted 12-hex id). Returns the created row.
pub async fn add_sound(db: &Db, url: &str, label: &str) -> Result<Sound, sqlx::Error> {
    let id = super::new_id();
    let now = Utc::now();
    sqlx::query("INSERT INTO sounds (id, url, label, active, created_at) VALUES (?, ?, ?, 1, ?)")
        .bind(&id)
        .bind(url)
        .bind(label)
        .bind(now)
        .execute(db.writer())
        .await?;
    get_sound(db, &id).await?.ok_or(sqlx::Error::RowNotFound)
}

/// Remove a sound. Returns true if it existed. (Playlist rows referencing it
/// cascade away — same net effect as the old resolve-time skip.)
pub async fn remove_sound(db: &Db, id: &str) -> Result<bool, sqlx::Error> {
    let res = sqlx::query("DELETE FROM sounds WHERE id = ?")
        .bind(id)
        .execute(db.writer())
        .await?;
    Ok(res.rows_affected() > 0)
}

/// Set a sound's active flag. Returns true if the sound exists.
pub async fn toggle_sound(db: &Db, id: &str, active: bool) -> Result<bool, sqlx::Error> {
    let res = sqlx::query("UPDATE sounds SET active = ? WHERE id = ?")
        .bind(active)
        .bind(id)
        .execute(db.writer())
        .await?;
    Ok(res.rows_affected() > 0)
}

/// Update a sound's url and/or label (only provided fields). Returns true if
/// the sound exists.
pub async fn update_sound(
    db: &Db,
    id: &str,
    url: Option<&str>,
    label: Option<&str>,
) -> Result<bool, sqlx::Error> {
    let res = sqlx::query(
        "UPDATE sounds SET url = COALESCE(?, url), label = COALESCE(?, label) WHERE id = ?",
    )
    .bind(url)
    .bind(label)
    .bind(id)
    .execute(db.writer())
    .await?;
    Ok(res.rows_affected() > 0)
}

// ── Page playlists ───────────────────────────────────────────────────────────

/// Playlists may target pages that only exist in an external system (Campaign
/// Hub sends arbitrary integration ids). The FK on `page_playlists` needs a
/// page row, so mint a stub — same pattern as the JSON importer.
async fn ensure_page(db: &Db, page_id: &str) -> Result<(), sqlx::Error> {
    let now = Utc::now();
    sqlx::query(
        "INSERT OR IGNORE INTO pages (id, name, source, created_at, updated_at)
         VALUES (?, ?, 'stub', ?, ?)",
    )
    .bind(page_id)
    .bind(page_id)
    .bind(now)
    .bind(now)
    .execute(db.writer())
    .await?;
    Ok(())
}

/// Ordered sound ids assigned to a page. Empty if unset.
pub async fn get_page_playlist(db: &Db, page_id: &str) -> Result<Vec<String>, sqlx::Error> {
    sqlx::query_scalar::<_, String>(
        "SELECT sound_id FROM page_playlists WHERE page_id = ? ORDER BY position",
    )
    .bind(page_id)
    .fetch_all(db.reader())
    .await
}

/// The full `{integration_id: [sound_id, ...]}` map.
pub async fn get_all_page_playlists(db: &Db) -> Result<BTreeMap<String, Vec<String>>, sqlx::Error> {
    let rows = sqlx::query_as::<_, (String, String)>(
        "SELECT page_id, sound_id FROM page_playlists ORDER BY page_id, position",
    )
    .fetch_all(db.reader())
    .await?;
    let mut map: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for (page_id, sound_id) in rows {
        map.entry(page_id).or_default().push(sound_id);
    }
    Ok(map)
}

/// Replace a page's playlist wholesale: preserves order, dedups, and silently
/// drops sound ids not present in the pool. Returns the saved list.
pub async fn set_page_playlist(
    db: &Db,
    page_id: &str,
    sound_ids: &[String],
) -> Result<Vec<String>, sqlx::Error> {
    ensure_page(db, page_id).await?;
    let valid: Vec<String> = sqlx::query_scalar("SELECT id FROM sounds")
        .fetch_all(db.reader())
        .await?;

    let mut seen = std::collections::HashSet::new();
    let mut cleaned: Vec<String> = Vec::new();
    for sid in sound_ids {
        if !sid.is_empty() && valid.iter().any(|v| v == sid) && seen.insert(sid.clone()) {
            cleaned.push(sid.clone());
        }
    }

    let mut tx = db.writer().begin().await?;
    sqlx::query("DELETE FROM page_playlists WHERE page_id = ?")
        .bind(page_id)
        .execute(&mut *tx)
        .await?;
    for (pos, sid) in cleaned.iter().enumerate() {
        sqlx::query("INSERT INTO page_playlists (page_id, sound_id, position) VALUES (?, ?, ?)")
            .bind(page_id)
            .bind(sid)
            .bind(pos as i64)
            .execute(&mut *tx)
            .await?;
    }
    tx.commit().await?;
    Ok(cleaned)
}

/// Append a sound to a page's playlist (idempotent). Returns the updated list,
/// or `None` when the sound id isn't in the pool (the caller 400s — this was a
/// `ValueError` in the old service).
pub async fn add_song_to_page(
    db: &Db,
    page_id: &str,
    sound_id: &str,
) -> Result<Option<Vec<String>>, sqlx::Error> {
    if get_sound(db, sound_id).await?.is_none() {
        return Ok(None);
    }
    ensure_page(db, page_id).await?;
    sqlx::query(
        "INSERT INTO page_playlists (page_id, sound_id, position)
         SELECT ?, ?, COALESCE(MAX(position) + 1, 0) FROM page_playlists WHERE page_id = ?
         ON CONFLICT (page_id, sound_id) DO NOTHING",
    )
    .bind(page_id)
    .bind(sound_id)
    .bind(page_id)
    .execute(db.writer())
    .await?;
    Ok(Some(get_page_playlist(db, page_id).await?))
}

/// Remove a sound from a page's playlist. Returns the updated list (empty if
/// the page had no playlist).
pub async fn remove_song_from_page(
    db: &Db,
    page_id: &str,
    sound_id: &str,
) -> Result<Vec<String>, sqlx::Error> {
    sqlx::query("DELETE FROM page_playlists WHERE page_id = ? AND sound_id = ?")
        .bind(page_id)
        .bind(sound_id)
        .execute(db.writer())
        .await?;
    get_page_playlist(db, page_id).await
}

/// Remove a page's playlist entirely. Returns true if it existed.
pub async fn clear_page_playlist(db: &Db, page_id: &str) -> Result<bool, sqlx::Error> {
    let res = sqlx::query("DELETE FROM page_playlists WHERE page_id = ?")
        .bind(page_id)
        .execute(db.writer())
        .await?;
    Ok(res.rows_affected() > 0)
}

/// Resolve a page's playlist into full sound rows, in playlist order.
/// `active_only=true` filters deactivated sounds (message-builder view);
/// `active_only=false` keeps them (UI view, shown struck-through).
pub async fn resolve_playlist_sounds(
    db: &Db,
    page_id: &str,
    active_only: bool,
) -> Result<Vec<Sound>, sqlx::Error> {
    let sql = if active_only {
        "SELECT s.id, s.url, s.label, s.active, s.created_at
         FROM page_playlists pp JOIN sounds s ON s.id = pp.sound_id
         WHERE pp.page_id = ? AND s.active = 1 ORDER BY pp.position"
    } else {
        "SELECT s.id, s.url, s.label, s.active, s.created_at
         FROM page_playlists pp JOIN sounds s ON s.id = pp.sound_id
         WHERE pp.page_id = ? ORDER BY pp.position"
    };
    sqlx::query_as::<_, Sound>(sql)
        .bind(page_id)
        .fetch_all(db.reader())
        .await
}

// ── Poster helpers used by the sound-assignment send path ───────────────────

/// The page ids a poster owns, in assignment order (mirrors the old
/// `poster["page_ids"]` list).
pub async fn pages_for_poster(db: &Db, poster_id: &str) -> Result<Vec<String>, sqlx::Error> {
    sqlx::query_scalar::<_, String>(
        "SELECT page_id FROM poster_pages WHERE poster_id = ? ORDER BY rowid",
    )
    .bind(poster_id)
    .fetch_all(db.reader())
    .await
}

/// Persist a poster's auto-created "Campaign Sounds" topic id.
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

#[cfg(test)]
mod tests {
    use super::*;

    async fn db() -> (Db, tempfile::TempDir) {
        Db::connect_temp().await.unwrap()
    }

    #[tokio::test]
    async fn sound_crud_roundtrip() {
        let (db, _g) = db().await;
        let a = add_sound(&db, "https://t/1", "Artist - Song").await.unwrap();
        let b = add_sound(&db, "https://t/2", "B").await.unwrap();
        assert_eq!(a.id.len(), 12);
        assert!(a.active);

        // insertion order preserved
        let all = list_sounds(&db, false).await.unwrap();
        assert_eq!(
            all.iter().map(|s| s.id.as_str()).collect::<Vec<_>>(),
            vec![a.id.as_str(), b.id.as_str()]
        );

        // active filter
        assert!(toggle_sound(&db, &b.id, false).await.unwrap());
        let active = list_sounds(&db, true).await.unwrap();
        assert_eq!(active.len(), 1);
        assert_eq!(active[0].id, a.id);

        // partial update
        assert!(update_sound(&db, &a.id, None, Some("New Label")).await.unwrap());
        let a2 = get_sound(&db, &a.id).await.unwrap().unwrap();
        assert_eq!(a2.label, "New Label");
        assert_eq!(a2.url, "https://t/1", "url untouched by label-only update");

        // remove
        assert!(remove_sound(&db, &b.id).await.unwrap());
        assert!(!remove_sound(&db, &b.id).await.unwrap(), "second delete reports missing");
        assert!(!toggle_sound(&db, "nope", true).await.unwrap());
        assert!(!update_sound(&db, "nope", Some("u"), None).await.unwrap());
    }

    #[tokio::test]
    async fn sound_serializes_to_legacy_shape() {
        let (db, _g) = db().await;
        let s = add_sound(&db, "https://t/1", "L").await.unwrap();
        let v = serde_json::to_value(&s).unwrap();
        let obj = v.as_object().unwrap();
        // serde_json's Map is a BTreeMap → alphabetical; key ORDER is not part
        // of the wire contract, presence is.
        let mut keys: Vec<_> = obj.keys().map(String::as_str).collect();
        keys.sort_unstable();
        assert_eq!(keys, vec!["active", "added_at", "id", "label", "url"]);
        let ts = obj["added_at"].as_str().unwrap();
        assert!(ts.ends_with(".000Z"), "legacy timestamp format: {ts}");
    }

    #[tokio::test]
    async fn playlist_set_dedups_and_drops_unknown() {
        let (db, _g) = db().await;
        let a = add_sound(&db, "u1", "A").await.unwrap();
        let b = add_sound(&db, "u2", "B").await.unwrap();

        let saved = set_page_playlist(
            &db,
            "pg1",
            &[b.id.clone(), a.id.clone(), b.id.clone(), "ghost".into(), String::new()],
        )
        .await
        .unwrap();
        assert_eq!(saved, vec![b.id.clone(), a.id.clone()], "order kept, dup + unknown dropped");
        assert_eq!(get_page_playlist(&db, "pg1").await.unwrap(), saved);

        // replace shrinks
        let saved = set_page_playlist(&db, "pg1", std::slice::from_ref(&a.id)).await.unwrap();
        assert_eq!(saved, vec![a.id.clone()]);

        let map = get_all_page_playlists(&db).await.unwrap();
        assert_eq!(map.len(), 1);
        assert_eq!(map["pg1"], vec![a.id.clone()]);
    }

    #[tokio::test]
    async fn add_remove_song_semantics() {
        let (db, _g) = db().await;
        let a = add_sound(&db, "u1", "A").await.unwrap();
        let b = add_sound(&db, "u2", "B").await.unwrap();

        assert!(add_song_to_page(&db, "pg", "ghost").await.unwrap().is_none(), "unknown → None");

        let list = add_song_to_page(&db, "pg", &a.id).await.unwrap().unwrap();
        assert_eq!(list, vec![a.id.clone()]);
        // idempotent
        let list = add_song_to_page(&db, "pg", &a.id).await.unwrap().unwrap();
        assert_eq!(list, vec![a.id.clone()]);
        let list = add_song_to_page(&db, "pg", &b.id).await.unwrap().unwrap();
        assert_eq!(list, vec![a.id.clone(), b.id.clone()]);

        let list = remove_song_from_page(&db, "pg", &a.id).await.unwrap();
        assert_eq!(list, vec![b.id.clone()]);
        // removing from a page with no playlist → empty, no error
        assert!(remove_song_from_page(&db, "empty", &a.id).await.unwrap().is_empty());

        assert!(clear_page_playlist(&db, "pg").await.unwrap());
        assert!(!clear_page_playlist(&db, "pg").await.unwrap());
    }

    #[tokio::test]
    async fn resolve_filters_inactive_but_keeps_assignment() {
        let (db, _g) = db().await;
        let a = add_sound(&db, "u1", "A").await.unwrap();
        let b = add_sound(&db, "u2", "B").await.unwrap();
        set_page_playlist(&db, "pg", &[a.id.clone(), b.id.clone()]).await.unwrap();
        toggle_sound(&db, &a.id, false).await.unwrap();

        let active = resolve_playlist_sounds(&db, "pg", true).await.unwrap();
        assert_eq!(active.len(), 1);
        assert_eq!(active[0].id, b.id);

        let all = resolve_playlist_sounds(&db, "pg", false).await.unwrap();
        assert_eq!(all.len(), 2, "inactive stays assigned");
        assert!(!all[0].active);

        // assignment survives a toggle round-trip
        toggle_sound(&db, &a.id, true).await.unwrap();
        assert_eq!(resolve_playlist_sounds(&db, "pg", true).await.unwrap().len(), 2);
    }

    #[tokio::test]
    async fn poster_pages_and_sounds_topic() {
        let (db, _g) = db().await;
        crate::repo::upsert_poster(&db, "seffra", "Seffra", -100).await.unwrap();
        ensure_page(&db, "pg1").await.unwrap();
        ensure_page(&db, "pg2").await.unwrap();
        crate::repo::assign_page(&db, "seffra", "pg1").await.unwrap();
        crate::repo::assign_page(&db, "seffra", "pg2").await.unwrap();

        assert_eq!(pages_for_poster(&db, "seffra").await.unwrap(), vec!["pg1", "pg2"]);

        set_poster_sounds_topic(&db, "seffra", 42).await.unwrap();
        let p = crate::repo::get_poster(&db, "seffra").await.unwrap().unwrap();
        assert_eq!(p.sounds_topic_id, Some(42));
    }
}
