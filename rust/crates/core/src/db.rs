//! SQLite data layer.
//!
//! This replaces `telegram_config.json` / `page_roster.json` — the hand-rolled
//! JSON blobs that the old app rewrote *in full* on every mutation, with no
//! locking, so concurrent writes silently clobbered each other (the root cause
//! of the lost-inventory / lost-watermark bugs).
//!
//! The one non-obvious SQLite gotcha (from the research pass): SQLite is a
//! single-writer database. A naive single connection pool with N>1 connections
//! causes write-lock starvation and `SQLITE_BUSY` storms under sqlx. The fix is
//! **two pools** — a WRITER pool capped at exactly 1 connection, and a READER
//! pool (read-only) with several connections, both in WAL mode with a busy
//! timeout. All writes go through the writer; reads fan out across readers.
//! Read-then-write transactions use `BEGIN IMMEDIATE` so they take the write
//! lock up front instead of deadlocking on a lock upgrade.

use std::str::FromStr;
use std::time::Duration;

use sqlx::sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteSynchronous};
use sqlx::SqlitePool;

/// Handle to the database: a single-writer pool plus a multi-reader pool.
///
/// Clone is cheap (both pools are `Arc` inside). Pass it around as app state.
#[derive(Clone)]
pub struct Db {
    writer: SqlitePool,
    reader: SqlitePool,
}

impl Db {
    /// Open (creating if needed) the SQLite database at `path`, run migrations,
    /// and return the two-pool handle.
    pub async fn connect(path: &str) -> Result<Self, sqlx::Error> {
        let base = SqliteConnectOptions::from_str(path)?
            .create_if_missing(true)
            .journal_mode(SqliteJournalMode::Wal)
            .synchronous(SqliteSynchronous::Normal)
            .busy_timeout(Duration::from_secs(5))
            .foreign_keys(true);

        // Single writer — SQLite only allows one at a time anyway, so making the
        // pool reflect that turns lock contention into a clean in-process queue.
        let writer = SqlitePoolOptions::new()
            .max_connections(1)
            .connect_with(base.clone())
            .await?;

        // Several readers, explicitly read-only.
        let reader = SqlitePoolOptions::new()
            .max_connections(8)
            .connect_with(base.read_only(true))
            .await?;

        let db = Self { writer, reader };
        db.migrate().await?;
        Ok(db)
    }

    /// Run embedded migrations (against the writer).
    async fn migrate(&self) -> Result<(), sqlx::Error> {
        sqlx::migrate!("./migrations")
            .run(&self.writer)
            .await
            .map_err(|e| sqlx::Error::Migrate(Box::new(e)))
    }

    /// Open a throwaway database at a fresh temp file (for tests). Returns the
    /// `Db` plus the `TempDir` guard — keep the guard alive for the DB's life.
    ///
    /// NB: this MUST be a file, not `:memory:` — each `:memory:` connection is a
    /// separate database, so the two-pool design (separate writer/reader
    /// connections) would see two empty unrelated DBs. The file is what lets the
    /// reader pool observe the writer's commits. Tests pin this invariant.
    #[cfg(test)]
    pub async fn connect_temp() -> Result<(Self, tempfile::TempDir), sqlx::Error> {
        let dir = tempfile::tempdir().map_err(sqlx::Error::Io)?;
        let path = dir.path().join("test.db");
        let url = format!("sqlite://{}", path.display());
        let db = Self::connect(&url).await?;
        Ok((db, dir))
    }

    /// The single-writer pool. Route **all** INSERT/UPDATE/DELETE here.
    pub fn writer(&self) -> &SqlitePool {
        &self.writer
    }

    /// The read-only pool. Route SELECTs here so they don't queue behind writes.
    pub fn reader(&self) -> &SqlitePool {
        &self.reader
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{AssetKind, JobKind};
    use crate::repo;

    /// The reader pool must observe the writer pool's committed rows (WAL +
    /// shared file). If this fails, the whole two-pool design is broken.
    #[tokio::test]
    async fn reader_sees_writer_commits() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        repo::create_project(&db, "p").await.unwrap();
        let projects = repo::list_projects(&db).await.unwrap();
        assert_eq!(projects.len(), 1, "reader pool didn't see the writer's commit");
        assert_eq!(projects[0].name, "p");
    }

    /// Hammer the writer with concurrent inserts. The old JSON-blob DB lost
    /// updates here (full-file read-rewrite, no lock). SQLite + the 1-writer
    /// pool must land ALL of them with zero lost updates and zero SQLITE_BUSY.
    #[tokio::test]
    async fn concurrent_writes_lose_nothing() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        repo::create_project(&db, "race").await.unwrap();

        const N: usize = 200;
        let mut handles = Vec::new();
        for _ in 0..N {
            let db = db.clone();
            handles.push(tokio::spawn(async move {
                repo::create_asset(&db, "race", AssetKind::Generated, None, None)
                    .await
                    .map(|a| a.id)
            }));
        }
        let mut ids = std::collections::HashSet::new();
        for h in handles {
            let id = h.await.unwrap().expect("insert under concurrency failed");
            ids.insert(id);
        }
        // Every spawn produced a row, all ids unique, all persisted.
        assert_eq!(ids.len(), N, "lost or duplicated rows under concurrency");
        let stored = repo::list_assets(&db, "race", Some(AssetKind::Generated))
            .await
            .unwrap();
        assert_eq!(stored.len(), N, "DB has fewer rows than were inserted");
    }

    /// new_id() must not collide across many calls (it's a primary key).
    #[tokio::test]
    async fn ids_are_unique_at_scale() {
        let mut seen = std::collections::HashSet::new();
        for _ in 0..50_000 {
            assert!(seen.insert(repo::new_id()), "new_id collision");
        }
    }

    /// The data-loss claim, through the REAL repo path (not hand SQL): deliver
    /// 3 items, forward #1 and #3, leave #2's forward "failing". #2 must remain
    /// pending so a retry re-sends it — never silently dropped.
    #[tokio::test]
    async fn failed_forward_leaves_item_pending() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        repo::upsert_page(&db, "pg", "Page", None).await.unwrap();

        let i1 = repo::add_inventory(&db, "pg", None, Some(1001)).await.unwrap();
        let _i2 = repo::add_inventory(&db, "pg", None, Some(1002)).await.unwrap();
        let i3 = repo::add_inventory(&db, "pg", None, Some(1003)).await.unwrap();

        assert_eq!(repo::pending_inventory(&db, "pg").await.unwrap().len(), 3);

        // Simulate the forward loop: #1 and #3 succeed on Telegram, #2 "fails"
        // (we simply don't mark it).
        repo::mark_forwarded(&db, &i1.id, "poster", 5001).await.unwrap();
        repo::mark_forwarded(&db, &i3.id, "poster", 5003).await.unwrap();

        let pending = repo::pending_inventory(&db, "pg").await.unwrap();
        assert_eq!(pending.len(), 1, "exactly one item should remain pending");
        assert_eq!(
            pending[0].staging_msg_id,
            Some(1002),
            "the FAILED item (#2) must be the one still pending — not silently lost"
        );
    }

    /// A forwarded row must not reappear as pending (idempotency of the query).
    #[tokio::test]
    async fn forwarded_items_are_not_pending() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        repo::upsert_page(&db, "pg", "Page", None).await.unwrap();
        let i = repo::add_inventory(&db, "pg", None, Some(1)).await.unwrap();
        repo::mark_forwarded(&db, &i.id, "poster", 9).await.unwrap();
        assert!(repo::pending_inventory(&db, "pg").await.unwrap().is_empty());
    }

    /// The claim pattern: two concurrent claims of the same page's pending items
    /// must partition the rows — the same row is never claimed by both (which is
    /// what would cause a double-forward). Total claimed == total pending.
    #[tokio::test]
    async fn concurrent_claims_do_not_overlap() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        repo::upsert_page(&db, "pg", "Page", None).await.unwrap();
        for n in 0..20 {
            repo::add_inventory(&db, "pg", None, Some(1000 + n)).await.unwrap();
        }

        // Two posters race to claim the same page's pending set.
        let db1 = db.clone();
        let db2 = db.clone();
        let (a, b) = tokio::join!(
            tokio::spawn(async move { repo::claim_pending_inventory(&db1, "pg", "poster_a").await.unwrap() }),
            tokio::spawn(async move { repo::claim_pending_inventory(&db2, "pg", "poster_b").await.unwrap() }),
        );
        let a = a.unwrap();
        let b = b.unwrap();

        // No row id appears in both claim sets, and together they cover all 20.
        let ids_a: std::collections::HashSet<_> = a.iter().map(|i| i.id.clone()).collect();
        let ids_b: std::collections::HashSet<_> = b.iter().map(|i| i.id.clone()).collect();
        assert!(ids_a.is_disjoint(&ids_b), "same row claimed by both callers → would double-forward");
        assert_eq!(ids_a.len() + ids_b.len(), 20, "claims didn't cover all pending rows exactly once");
    }

    /// confirm promotes an inflight claim to forwarded; release returns it to
    /// pending. A confirmed row is not pending; a released row is.
    #[tokio::test]
    async fn claim_confirm_and_release() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        repo::upsert_page(&db, "pg", "Page", None).await.unwrap();
        repo::add_inventory(&db, "pg", None, Some(1)).await.unwrap();
        repo::add_inventory(&db, "pg", None, Some(2)).await.unwrap();

        let claimed = repo::claim_pending_inventory(&db, "pg", "pst").await.unwrap();
        assert_eq!(claimed.len(), 2);
        // After claiming, nothing is "pending" (they're inflight).
        assert!(repo::pending_inventory(&db, "pg").await.unwrap().is_empty());

        // Confirm #1 (forwarded ok), release #2 (forward failed).
        repo::confirm_forwarded(&db, &claimed[0].id, "pst", 5001).await.unwrap();
        repo::release_claim(&db, &claimed[1].id, "pst").await.unwrap();

        let pending = repo::pending_inventory(&db, "pg").await.unwrap();
        assert_eq!(pending.len(), 1, "the released (failed) item must be pending again");
        assert_eq!(pending[0].id, claimed[1].id);
    }

    /// send idempotency support: find_inventory_for_asset locates a prior send.
    #[tokio::test]
    async fn find_inventory_for_asset_dedup() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        repo::create_project(&db, "p").await.unwrap();
        repo::upsert_page(&db, "pg", "Page", None).await.unwrap();
        let asset = repo::create_asset(&db, "p", AssetKind::Burned, None, None).await.unwrap();

        assert!(repo::find_inventory_for_asset(&db, "pg", &asset.id).await.unwrap().is_none());
        repo::add_inventory(&db, "pg", Some(&asset.id), Some(99)).await.unwrap();
        let found = repo::find_inventory_for_asset(&db, "pg", &asset.id).await.unwrap();
        assert!(found.is_some(), "a prior send for (page,asset) must be findable for idempotency");
    }

    /// A progress write that lands AFTER a job is terminal must be a no-op
    /// (the detached-spawn race). Job stays done@1.0.
    #[tokio::test]
    async fn progress_after_terminal_is_noop() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        repo::create_project(&db, "p").await.unwrap();
        let job = repo::create_job(&db, "p", JobKind::Clip, None, None).await.unwrap();
        repo::finish_job(&db, &job.id, &["a".into()]).await.unwrap();

        // A late, out-of-order progress write arrives.
        repo::update_job_progress(&db, &job.id, 0.42).await.unwrap();

        let j = repo::get_job(&db, &job.id).await.unwrap().unwrap();
        assert_eq!(j.status, crate::model::JobStatus::Done, "late progress must not un-terminal a job");
        assert!((j.progress - 1.0).abs() < 1e-9, "late progress must not drop a done job below 100%");
    }

    /// Restart recovery flips orphaned queued/processing jobs+assets to failed.
    #[tokio::test]
    async fn recover_orphaned_marks_failed() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        repo::create_project(&db, "p").await.unwrap();
        // A job left mid-flight, plus an asset mid-encode.
        let job = repo::create_job(&db, "p", JobKind::Clip, None, None).await.unwrap();
        repo::update_job_progress(&db, &job.id, 0.3).await.unwrap(); // → processing
        let asset = repo::create_asset(&db, "p", AssetKind::Clip, None, None).await.unwrap();
        repo::mark_asset_processing(&db, &asset.id).await.unwrap();

        let (jobs, assets) = repo::recover_orphaned(&db).await.unwrap();
        assert_eq!(jobs, 1);
        assert_eq!(assets, 1);

        let j = repo::get_job(&db, &job.id).await.unwrap().unwrap();
        assert_eq!(j.status, crate::model::JobStatus::Failed);
        let a = repo::get_asset(&db, &asset.id).await.unwrap().unwrap();
        assert_eq!(a.status, crate::model::AssetStatus::Failed);
    }

    /// Jobs created concurrently each get a row; finish/fail transitions stick.
    #[tokio::test]
    async fn job_lifecycle_transitions() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        repo::create_project(&db, "p").await.unwrap();
        let job = repo::create_job(&db, "p", JobKind::Clip, None, None)
            .await
            .unwrap();
        repo::update_job_progress(&db, &job.id, 0.5).await.unwrap();
        let mid = repo::get_job(&db, &job.id).await.unwrap().unwrap();
        assert_eq!(mid.status, crate::model::JobStatus::Processing);
        assert!((mid.progress - 0.5).abs() < 1e-9);

        repo::finish_job(&db, &job.id, &["a".into(), "b".into()])
            .await
            .unwrap();
        let done = repo::get_job(&db, &job.id).await.unwrap().unwrap();
        assert_eq!(done.status, crate::model::JobStatus::Done);
        assert!((done.progress - 1.0).abs() < 1e-9);
        assert_eq!(done.result_asset_ids, r#"["a","b"]"#);
    }
}
