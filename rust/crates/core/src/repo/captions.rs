//! Caption bank repository — scraped TikTok captions as first-class rows.
//!
//! The CSV files on disk remain the parity contract for the old
//! export/history endpoints; these rows are the durable, queryable copy that
//! lets a future burn path pair captions to clips by mood/intent instead of
//! `i % len`. A re-scrape of the same (project, source) replaces that
//! source's rows wholesale (see [`replace_source_captions`]).

use chrono::Utc;

use super::new_id;
use crate::db::Db;
use crate::model::Caption;

/// Insert a single caption row.
pub async fn add_caption(
    db: &Db,
    project: &str,
    source: &str,
    text: &str,
    mood: Option<&str>,
) -> Result<Caption, sqlx::Error> {
    let id = new_id();
    sqlx::query(
        "INSERT INTO captions (id, project, source, text, mood, created_at) VALUES (?, ?, ?, ?, ?, ?)",
    )
    .bind(&id)
    .bind(project)
    .bind(source)
    .bind(text)
    .bind(mood)
    .bind(Utc::now())
    .execute(db.writer())
    .await?;
    sqlx::query_as::<_, Caption>("SELECT * FROM captions WHERE id = ?")
        .bind(&id)
        .fetch_one(db.reader())
        .await
}

/// List a project's captions in insertion order, optionally filtered to one
/// scraped source (TikTok username).
pub async fn list_captions(
    db: &Db,
    project: &str,
    source: Option<&str>,
) -> Result<Vec<Caption>, sqlx::Error> {
    match source {
        Some(s) => {
            sqlx::query_as::<_, Caption>(
                "SELECT * FROM captions WHERE project = ? AND source = ? ORDER BY rowid",
            )
            .bind(project)
            .bind(s)
            .fetch_all(db.reader())
            .await
        }
        None => {
            sqlx::query_as::<_, Caption>("SELECT * FROM captions WHERE project = ? ORDER BY rowid")
                .bind(project)
                .fetch_all(db.reader())
                .await
        }
    }
}

/// Replace ALL captions for a (project, source) with a fresh scrape's rows —
/// atomic (single transaction on the writer), so a reader never sees a
/// half-replaced bank. `rows` are `(text, mood)` pairs; returns the count
/// inserted.
pub async fn replace_source_captions(
    db: &Db,
    project: &str,
    source: &str,
    rows: &[(String, Option<String>)],
) -> Result<usize, sqlx::Error> {
    let mut tx = db.writer().begin().await?;
    sqlx::query("DELETE FROM captions WHERE project = ? AND source = ?")
        .bind(project)
        .bind(source)
        .execute(&mut *tx)
        .await?;
    let now = Utc::now();
    for (text, mood) in rows {
        sqlx::query(
            "INSERT INTO captions (id, project, source, text, mood, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        )
        .bind(new_id())
        .bind(project)
        .bind(source)
        .bind(text)
        .bind(mood.as_deref())
        .bind(now)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    Ok(rows.len())
}

/// Rename a scraped source (batch) within a project — keeps the DB rows in
/// step when the on-disk username folder is renamed. Returns rows moved.
pub async fn rename_source(
    db: &Db,
    project: &str,
    old: &str,
    new: &str,
) -> Result<u64, sqlx::Error> {
    let res = sqlx::query("UPDATE captions SET source = ? WHERE project = ? AND source = ?")
        .bind(new)
        .bind(project)
        .bind(old)
        .execute(db.writer())
        .await?;
    Ok(res.rows_affected())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::repo;

    async fn db_with_project(name: &str) -> (Db, tempfile::TempDir) {
        let (db, guard) = Db::connect_temp().await.unwrap();
        repo::create_project(&db, name).await.unwrap();
        (db, guard)
    }

    #[tokio::test]
    async fn add_and_list_roundtrip() {
        let (db, _g) = db_with_project("p").await;
        let a = add_caption(&db, "p", "user1", "first caption", Some("hype")).await.unwrap();
        assert_eq!(a.project, "p");
        assert_eq!(a.source, "user1");
        assert_eq!(a.text, "first caption");
        assert_eq!(a.mood.as_deref(), Some("hype"));

        // mood is optional (nullable) end-to-end
        let b = add_caption(&db, "p", "user1", "no mood", None).await.unwrap();
        assert_eq!(b.mood, None);

        let all = list_captions(&db, "p", None).await.unwrap();
        assert_eq!(all.len(), 2);
        // insertion order preserved
        assert_eq!(all[0].text, "first caption");
        assert_eq!(all[1].text, "no mood");
    }

    #[tokio::test]
    async fn list_filters_by_source() {
        let (db, _g) = db_with_project("p").await;
        add_caption(&db, "p", "alice", "a", None).await.unwrap();
        add_caption(&db, "p", "bob", "b", None).await.unwrap();
        add_caption(&db, "p", "alice", "a2", None).await.unwrap();

        let alice = list_captions(&db, "p", Some("alice")).await.unwrap();
        assert_eq!(alice.len(), 2);
        assert!(alice.iter().all(|c| c.source == "alice"));
        assert_eq!(list_captions(&db, "p", Some("bob")).await.unwrap().len(), 1);
        assert_eq!(list_captions(&db, "p", None).await.unwrap().len(), 3);
    }

    #[tokio::test]
    async fn replace_swaps_a_sources_rows_wholesale() {
        let (db, _g) = db_with_project("p").await;
        add_caption(&db, "p", "user1", "stale 1", Some("sad")).await.unwrap();
        add_caption(&db, "p", "user1", "stale 2", None).await.unwrap();
        // another source must be untouched by the replace
        add_caption(&db, "p", "other", "keep me", None).await.unwrap();

        let fresh = vec![
            ("new 1".to_string(), Some("funny".to_string())),
            ("new 2".to_string(), None),
            ("new 3".to_string(), Some("chill".to_string())),
        ];
        let n = replace_source_captions(&db, "p", "user1", &fresh).await.unwrap();
        assert_eq!(n, 3);

        let rows = list_captions(&db, "p", Some("user1")).await.unwrap();
        assert_eq!(rows.len(), 3, "old rows must be gone, exactly the fresh set remains");
        assert_eq!(rows[0].text, "new 1");
        assert_eq!(rows[0].mood.as_deref(), Some("funny"));
        assert_eq!(rows[1].mood, None);

        let other = list_captions(&db, "p", Some("other")).await.unwrap();
        assert_eq!(other.len(), 1, "replace must not touch other sources");
    }

    #[tokio::test]
    async fn replace_with_empty_clears_source() {
        let (db, _g) = db_with_project("p").await;
        add_caption(&db, "p", "user1", "old", None).await.unwrap();
        let n = replace_source_captions(&db, "p", "user1", &[]).await.unwrap();
        assert_eq!(n, 0);
        assert!(list_captions(&db, "p", Some("user1")).await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn rename_source_moves_only_that_batch() {
        let (db, _g) = db_with_project("p").await;
        repo::create_project(&db, "q").await.unwrap();
        add_caption(&db, "p", "old_name", "one", None).await.unwrap();
        add_caption(&db, "p", "old_name", "two", None).await.unwrap();
        // same source name in a DIFFERENT project must not be renamed
        add_caption(&db, "q", "old_name", "other project", None).await.unwrap();

        let moved = rename_source(&db, "p", "old_name", "new_name").await.unwrap();
        assert_eq!(moved, 2);
        assert!(list_captions(&db, "p", Some("old_name")).await.unwrap().is_empty());
        assert_eq!(list_captions(&db, "p", Some("new_name")).await.unwrap().len(), 2);
        assert_eq!(list_captions(&db, "q", Some("old_name")).await.unwrap().len(), 1);
    }
}
