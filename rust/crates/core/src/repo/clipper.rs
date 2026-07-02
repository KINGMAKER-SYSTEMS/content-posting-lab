//! Clipper job management repository — the wave-2 port of the "clip jobs"
//! surface in `routers/clipper.py` (`GET/DELETE/PATCH /api/clipper/jobs*`).
//!
//! The old app kept clip jobs as directories on disk (`projects/<p>/clips/<job_id>/`)
//! with a `job_meta.json` sidecar for the rename label. Here a clip job is a
//! `jobs` row (`kind = 'clip'`) and its produced clips are `assets` rows
//! (`kind = 'clip'`, `parent_id` = the source asset) whose ids live in
//! `jobs.result_asset_ids`. No sidecar file: the rename label rides in the
//! job's own `params` JSON (merged in, not overwritten — the clip-length/trim
//! params the runner wrote stay intact).
//!
//! This module only *reads and reshapes* `jobs`/`assets` rows — it does not
//! run clip jobs (that's the wave-3 runner, `POST /api/jobs/clip`).

use serde_json::{Map, Value};

use crate::db::Db;
use crate::model::{Asset, AssetKind, Job, JobKind};

/// A clip job reshaped for the management endpoints, plus its clip assets.
pub struct ClipJobWithAssets {
    pub job: Job,
    pub clips: Vec<Asset>,
}

/// List every clip job (`kind = 'clip'`) in a project, newest first, each with
/// its produced clip assets resolved. Jobs with zero result assets (still
/// running, or a runner that produced nothing) are included — the route layer
/// decides whether to filter them, mirroring the Python behavior of skipping
/// job dirs with no `clip_*.mp4` files.
pub async fn list_clip_jobs(db: &Db, project: &str) -> Result<Vec<ClipJobWithAssets>, sqlx::Error> {
    let jobs = sqlx::query_as::<_, Job>(
        "SELECT * FROM jobs WHERE project = ? AND kind = ? ORDER BY created_at DESC",
    )
    .bind(project)
    .bind(JobKind::Clip)
    .fetch_all(db.reader())
    .await?;

    let mut out = Vec::with_capacity(jobs.len());
    for job in jobs {
        let clips = result_assets(db, &job).await?;
        out.push(ClipJobWithAssets { job, clips });
    }
    Ok(out)
}

/// Fetch one clip job (kind = 'clip') by id, scoped to a project — `None` if
/// missing or if it belongs to a different project / job kind (so a caller
/// can't rename/delete another project's job by guessing an id).
pub async fn get_clip_job(
    db: &Db,
    project: &str,
    job_id: &str,
) -> Result<Option<ClipJobWithAssets>, sqlx::Error> {
    let job = sqlx::query_as::<_, Job>(
        "SELECT * FROM jobs WHERE id = ? AND project = ? AND kind = ?",
    )
    .bind(job_id)
    .bind(project)
    .bind(JobKind::Clip)
    .fetch_optional(db.reader())
    .await?;
    match job {
        None => Ok(None),
        Some(job) => {
            let clips = result_assets(db, &job).await?;
            Ok(Some(ClipJobWithAssets { job, clips }))
        }
    }
}

/// Resolve a job's `result_asset_ids` JSON array into the actual clip Asset
/// rows (skips any id that no longer resolves rather than failing the whole
/// list — an asset can be pruned independently of its job row).
async fn result_assets(db: &Db, job: &Job) -> Result<Vec<Asset>, sqlx::Error> {
    let ids: Vec<String> = serde_json::from_str(&job.result_asset_ids).unwrap_or_default();
    let mut clips = Vec::with_capacity(ids.len());
    for id in ids {
        if let Some(asset) = sqlx::query_as::<_, Asset>(
            "SELECT * FROM assets WHERE id = ? AND kind = ?",
        )
        .bind(&id)
        .bind(AssetKind::Clip)
        .fetch_optional(db.reader())
        .await?
        {
            clips.push(asset);
        }
    }
    Ok(clips)
}

/// The `label` a clip job was renamed to, if any — read back out of the job's
/// `params` JSON (`{"label": "..."}` merged in by [`rename_clip_job`]).
pub fn job_label(job: &Job) -> Option<String> {
    let params = job.params.as_deref()?;
    let v: Value = serde_json::from_str(params).ok()?;
    v.get("label")?.as_str().map(str::to_string)
}

/// Rename a clip job: merge `{"label": new_label}` into the job's existing
/// `params` JSON (preserving whatever the runner already stored there — clip
/// length, trim window, etc.) Returns `false` if the job doesn't exist / isn't
/// a clip job in this project.
pub async fn rename_clip_job(
    db: &Db,
    project: &str,
    job_id: &str,
    new_label: &str,
) -> Result<bool, sqlx::Error> {
    let Some(existing) = get_clip_job(db, project, job_id).await? else {
        return Ok(false);
    };
    let mut map: Map<String, Value> = existing
        .job
        .params
        .as_deref()
        .and_then(|p| serde_json::from_str::<Value>(p).ok())
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default();
    map.insert("label".to_string(), Value::String(new_label.to_string()));
    let merged = serde_json::to_string(&Value::Object(map)).unwrap_or_else(|_| "{}".to_string());

    sqlx::query("UPDATE jobs SET params = ?, updated_at = ? WHERE id = ? AND project = ? AND kind = ?")
        .bind(&merged)
        .bind(chrono::Utc::now())
        .bind(job_id)
        .bind(project)
        .bind(JobKind::Clip)
        .execute(db.writer())
        .await?;
    Ok(true)
}

/// Delete a clip job: removes its clip Asset rows and the job row itself.
/// Caller is responsible for deleting the on-disk files / R2 objects (that
/// needs the resolved asset paths, which the route layer already has from
/// [`get_clip_job`] before calling this). Returns `false` if the job wasn't
/// found (nothing deleted).
pub async fn delete_clip_job(db: &Db, project: &str, job_id: &str) -> Result<bool, sqlx::Error> {
    let Some(existing) = get_clip_job(db, project, job_id).await? else {
        return Ok(false);
    };
    let mut tx = db.writer().begin().await?;
    for clip in &existing.clips {
        sqlx::query("DELETE FROM assets WHERE id = ?")
            .bind(&clip.id)
            .execute(&mut *tx)
            .await?;
    }
    sqlx::query("DELETE FROM jobs WHERE id = ? AND project = ? AND kind = ?")
        .bind(job_id)
        .bind(project)
        .bind(JobKind::Clip)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(true)
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

    async fn make_clip_job(db: &Db, project: &str, n_clips: usize) -> Job {
        let source = repo::create_asset(db, project, AssetKind::Source, None, None)
            .await
            .unwrap();
        let job = repo::create_job(db, project, JobKind::Clip, Some(&source.id), None)
            .await
            .unwrap();
        let mut ids = Vec::new();
        for _ in 0..n_clips {
            let clip = repo::create_asset(db, project, AssetKind::Clip, Some(&source.id), None)
                .await
                .unwrap();
            repo::mark_asset_ready(db, &clip.id, &format!("projects/{project}/clips/{}/clip.mp4", job.id))
                .await
                .unwrap();
            ids.push(clip.id);
        }
        repo::finish_job(db, &job.id, &ids).await.unwrap();
        repo::get_job(db, &job.id).await.unwrap().unwrap()
    }

    #[tokio::test]
    async fn list_returns_job_with_its_clip_assets() {
        let (db, _g) = db_with_project("p").await;
        let job = make_clip_job(&db, "p", 3).await;

        let jobs = list_clip_jobs(&db, "p").await.unwrap();
        assert_eq!(jobs.len(), 1);
        assert_eq!(jobs[0].job.id, job.id);
        assert_eq!(jobs[0].clips.len(), 3);
    }

    #[tokio::test]
    async fn list_is_scoped_to_project_and_kind() {
        let (db, _g) = db_with_project("p").await;
        repo::create_project(&db, "q").await.unwrap();
        make_clip_job(&db, "p", 1).await;
        make_clip_job(&db, "q", 1).await;
        // a non-clip job in the same project must not show up
        repo::create_job(&db, "p", JobKind::Burn, None, None).await.unwrap();

        let jobs = list_clip_jobs(&db, "p").await.unwrap();
        assert_eq!(jobs.len(), 1, "must only see project p's clip jobs");
    }

    #[tokio::test]
    async fn get_clip_job_scopes_by_project() {
        let (db, _g) = db_with_project("p").await;
        repo::create_project(&db, "q").await.unwrap();
        let job = make_clip_job(&db, "p", 1).await;

        assert!(get_clip_job(&db, "p", &job.id).await.unwrap().is_some());
        assert!(
            get_clip_job(&db, "q", &job.id).await.unwrap().is_none(),
            "a job must not be fetchable under a different project"
        );
        assert!(get_clip_job(&db, "p", "nonexistent").await.unwrap().is_none());
    }

    #[tokio::test]
    async fn rename_merges_label_without_clobbering_existing_params() {
        let (db, _g) = db_with_project("p").await;
        let source = repo::create_asset(&db, "p", AssetKind::Source, None, None).await.unwrap();
        let job = repo::create_job(
            &db,
            "p",
            JobKind::Clip,
            Some(&source.id),
            Some(r#"{"clip_length":15.0}"#),
        )
        .await
        .unwrap();

        let ok = rename_clip_job(&db, "p", &job.id, "My Cool Batch").await.unwrap();
        assert!(ok);

        let reloaded = repo::get_job(&db, &job.id).await.unwrap().unwrap();
        assert_eq!(job_label(&reloaded).as_deref(), Some("My Cool Batch"));
        // original param survived the merge
        let v: Value = serde_json::from_str(reloaded.params.as_deref().unwrap()).unwrap();
        assert_eq!(v.get("clip_length").and_then(Value::as_f64), Some(15.0));
    }

    #[tokio::test]
    async fn rename_nonexistent_job_returns_false() {
        let (db, _g) = db_with_project("p").await;
        assert!(!rename_clip_job(&db, "p", "nope", "x").await.unwrap());
    }

    #[tokio::test]
    async fn delete_removes_job_and_its_clip_assets() {
        let (db, _g) = db_with_project("p").await;
        let job = make_clip_job(&db, "p", 2).await;
        let clip_ids: Vec<String> = {
            let with_assets = get_clip_job(&db, "p", &job.id).await.unwrap().unwrap();
            with_assets.clips.iter().map(|a| a.id.clone()).collect()
        };

        let ok = delete_clip_job(&db, "p", &job.id).await.unwrap();
        assert!(ok);

        assert!(repo::get_job(&db, &job.id).await.unwrap().is_none());
        for id in clip_ids {
            assert!(repo::get_asset(&db, &id).await.unwrap().is_none(), "clip asset must be gone too");
        }
    }

    #[tokio::test]
    async fn delete_nonexistent_job_returns_false_and_is_a_noop() {
        let (db, _g) = db_with_project("p").await;
        let job = make_clip_job(&db, "p", 1).await;
        assert!(!delete_clip_job(&db, "p", "nope").await.unwrap());
        // untouched
        assert!(repo::get_job(&db, &job.id).await.unwrap().is_some());
    }

    #[test]
    fn job_label_none_when_params_missing_or_unlabeled() {
        let job = Job {
            id: "j".into(),
            project: "p".into(),
            kind: JobKind::Clip,
            status: crate::model::JobStatus::Done,
            progress: 1.0,
            source_asset_id: None,
            params: None,
            result_asset_ids: "[]".into(),
            error: None,
            created_at: chrono::Utc::now(),
            updated_at: chrono::Utc::now(),
        };
        assert_eq!(job_label(&job), None);

        let job_no_label = Job { params: Some(r#"{"clip_length":7}"#.into()), ..job };
        assert_eq!(job_label(&job_no_label), None);
    }
}
