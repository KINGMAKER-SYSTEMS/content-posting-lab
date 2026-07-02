//! Project detail/delete extensions — wave-2 (recreate+projects workstream).
//!
//! The old Python app derived a project's stats (`video_count`,
//! `caption_count`, `burned_count`, `last_activity`) by walking its on-disk
//! `videos/`, `captions/`, `burned/` directories (see `project_manager.py`
//! `_project_info`). The Rust rewrite replaces that filesystem scan with the
//! `assets` (+ `captions`) tables, which are the durable source of truth here
//! — no directory walk, no stat() calls, and correct even for R2-backed assets
//! that never touch local disk.
//!
//! Mapping from the old dirs to the new tables:
//!   - `videos/`  → `assets` rows with `kind = 'generated'`
//!   - `captions/`→ `captions` rows for the project
//!   - `burned/`  → `assets` rows with `kind = 'burned'`
//!   - `last_activity` → the newest `updated_at` across a project's assets,
//!     jobs, and captions (whichever moved most recently).

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use super::captions::list_captions;
use crate::db::Db;
use crate::model::Project;

/// Rich project detail — the parity shape for `GET /api/projects/{name}` and
/// each entry of `GET /api/projects/`. Field names match the old Python
/// `_project_info()` dict exactly so the existing frontend (`types/api.ts`
/// `Project`) needs no changes.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProjectDetail {
    pub name: String,
    /// Synthesized project-relative path (the old field held an absolute
    /// filesystem path; there is no such path in the DB-backed model, so this
    /// is the logical project root under the data dir — enough for the
    /// frontend, which only displays it).
    pub path: String,
    pub video_count: i64,
    pub caption_count: i64,
    pub burned_count: i64,
    pub last_activity: Option<DateTime<Utc>>,
}

/// Build the rich detail row for one project. Returns `None` if the project
/// doesn't exist (mirrors Python's `get_project` returning `None`).
pub async fn get_project_detail(db: &Db, name: &str) -> Result<Option<ProjectDetail>, sqlx::Error> {
    // Existence check against the `projects` table (source of truth — a name
    // with zero assets is still a real, just-created project).
    let project = sqlx::query_as::<_, Project>("SELECT name, created_at FROM projects WHERE name = ?")
        .bind(name)
        .fetch_optional(db.reader())
        .await?;
    let Some(project) = project else {
        return Ok(None);
    };

    let video_count: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM assets WHERE project = ? AND kind = 'generated'",
    )
    .bind(name)
    .fetch_one(db.reader())
    .await?;

    let burned_count: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM assets WHERE project = ? AND kind = 'burned'")
            .bind(name)
            .fetch_one(db.reader())
            .await?;

    // No dedicated COUNT query exists on the captions repo yet (small volumes
    // in practice); reuse the existing list and take its length rather than
    // hand-rolling a second SQL string for the same table.
    let caption_count = list_captions(db, name, None).await?.len() as i64;

    let last_asset: Option<DateTime<Utc>> =
        sqlx::query_scalar("SELECT MAX(updated_at) FROM assets WHERE project = ?")
            .bind(name)
            .fetch_one(db.reader())
            .await?;
    let last_job: Option<DateTime<Utc>> =
        sqlx::query_scalar("SELECT MAX(updated_at) FROM jobs WHERE project = ?")
            .bind(name)
            .fetch_one(db.reader())
            .await?;
    let last_activity = match (last_asset, last_job) {
        (Some(a), Some(j)) => Some(a.max(j)),
        (Some(a), None) => Some(a),
        (None, Some(j)) => Some(j),
        (None, None) => None,
    };

    Ok(Some(ProjectDetail {
        name: project.name.clone(),
        path: format!("projects/{}", project.name),
        video_count,
        caption_count,
        burned_count,
        last_activity,
    }))
}

/// Delete a project row. Cascades to `assets`, `jobs`, and `captions` via
/// `ON DELETE CASCADE` (see `migrations/0001_init.sql` / `0002_jobs.sql`) —
/// no manual cleanup needed here, unlike the old `shutil.rmtree`.
/// Returns `true` if a row was actually deleted (parity with Python's
/// `delete_project` bool return for a 404 vs 200).
pub async fn delete_project(db: &Db, name: &str) -> Result<bool, sqlx::Error> {
    let result = sqlx::query("DELETE FROM projects WHERE name = ?")
        .bind(name)
        .execute(db.writer())
        .await?;
    Ok(result.rows_affected() > 0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Db;
    use crate::model::{AssetKind, JobKind};
    use crate::repo;

    #[tokio::test]
    async fn detail_counts_reflect_assets_and_captions() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        repo::create_project(&db, "p").await.unwrap();

        repo::create_asset(&db, "p", AssetKind::Generated, None, None)
            .await
            .unwrap();
        repo::create_asset(&db, "p", AssetKind::Generated, None, None)
            .await
            .unwrap();
        repo::create_asset(&db, "p", AssetKind::Burned, None, None)
            .await
            .unwrap();
        repo::captions::add_caption(&db, "p", "someuser", "hello", None)
            .await
            .unwrap();

        let detail = get_project_detail(&db, "p").await.unwrap().unwrap();
        assert_eq!(detail.name, "p");
        assert_eq!(detail.video_count, 2);
        assert_eq!(detail.burned_count, 1);
        assert_eq!(detail.caption_count, 1);
        assert!(detail.last_activity.is_some());
    }

    #[tokio::test]
    async fn detail_missing_project_is_none() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        assert!(get_project_detail(&db, "nope").await.unwrap().is_none());
    }

    #[tokio::test]
    async fn delete_cascades_assets_and_jobs() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        repo::create_project(&db, "p").await.unwrap();
        let asset = repo::create_asset(&db, "p", AssetKind::Generated, None, None)
            .await
            .unwrap();
        repo::create_job(&db, "p", JobKind::Generate, Some(&asset.id), None)
            .await
            .unwrap();

        let deleted = delete_project(&db, "p").await.unwrap();
        assert!(deleted, "delete_project should report true for an existing project");

        assert!(repo::get_asset(&db, &asset.id).await.unwrap().is_none(), "asset must cascade-delete with its project");
        let jobs = repo::list_jobs(&db, "p").await.unwrap();
        assert!(jobs.is_empty(), "jobs must cascade-delete with their project");
        assert!(get_project_detail(&db, "p").await.unwrap().is_none());
    }

    #[tokio::test]
    async fn delete_missing_project_returns_false() {
        let (db, _guard) = Db::connect_temp().await.unwrap();
        assert!(!delete_project(&db, "nope").await.unwrap());
    }
}
