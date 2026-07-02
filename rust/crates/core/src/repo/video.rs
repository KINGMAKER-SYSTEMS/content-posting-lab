//! Prompt-history repository for the video-generation UI.
//!
//! The old Python router (`routers/video.py`) kept this in a per-project
//! `prompts.json` file (newest-first, capped at 200 entries) via
//! `services/json_store.atomic_save`. There is no dedicated SQLite table for
//! it, and adding one is a migration — out of scope for this wave (see the
//! video-workstream file-ownership rules). Instead this rides on the
//! `settings` key/value table that already exists (migration 0003) and is
//! already used the same way by `repo::telegram_cfg` for scalar config: one
//! row per project, keyed `video_prompts:{project}`, whose value is a JSON
//! array of [`PromptEntry`]. Same one-row-per-key shape, same atomicity
//! (a single `INSERT OR REPLACE`), just a list instead of a scalar.

use chrono::Utc;
use serde::{Deserialize, Serialize};

use crate::db::Db;

/// One saved prompt-history entry (mirrors the Python dict shape exactly —
/// the frontend's `PromptEntry` type in `frontend/src/pages/Generate.tsx`
/// reads these fields verbatim).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PromptEntry {
    pub prompt: String,
    pub provider: String,
    pub count: u32,
    pub duration: u32,
    pub aspect_ratio: String,
    pub resolution: String,
    #[serde(default)]
    pub negative_prompt: Option<String>,
    #[serde(default)]
    pub has_media: bool,
    pub job_id: String,
    pub timestamp: String,
}

/// Matches the Python router's `MAX_PROMPT_HISTORY`.
const MAX_PROMPT_HISTORY: usize = 200;

fn setting_key(project: &str) -> String {
    format!("video_prompts:{project}")
}

/// Read a project's prompt history, newest first. Empty if never written or
/// if the stored JSON is somehow corrupt (mirrors the Python router's
/// best-effort `_read_prompts`, which swallows decode errors).
pub async fn list_prompts(db: &Db, project: &str) -> Result<Vec<PromptEntry>, sqlx::Error> {
    let raw = sqlx::query_scalar::<_, String>("SELECT value FROM settings WHERE key = ?")
        .bind(setting_key(project))
        .fetch_optional(db.reader())
        .await?;
    Ok(match raw {
        Some(json) => serde_json::from_str(&json).unwrap_or_default(),
        None => Vec::new(),
    })
}

/// Prepend a new entry and cap the list at [`MAX_PROMPT_HISTORY`] (same
/// insert-at-front + truncate behavior as the Python `_save_prompt`).
pub async fn save_prompt(db: &Db, project: &str, entry: PromptEntry) -> Result<(), sqlx::Error> {
    let mut prompts = list_prompts(db, project).await?;
    prompts.insert(0, entry);
    prompts.truncate(MAX_PROMPT_HISTORY);
    let json = serde_json::to_string(&prompts).unwrap_or_else(|_| "[]".to_string());
    sqlx::query("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)")
        .bind(setting_key(project))
        .bind(json)
        .bind(Utc::now())
        .execute(db.writer())
        .await?;
    Ok(())
}

/// Clear a project's prompt history entirely (DELETE /api/video/prompts).
pub async fn clear_prompts(db: &Db, project: &str) -> Result<(), sqlx::Error> {
    sqlx::query("DELETE FROM settings WHERE key = ?")
        .bind(setting_key(project))
        .execute(db.writer())
        .await?;
    Ok(())
}

/// Delete a job row outright (`DELETE /api/video/jobs/{id}`). The shared
/// `repo::` job-lifecycle functions (create/update_progress/finish/fail) never
/// delete — that's a video-router-only operation carried over from the old
/// Python `delete_job`, so it lives here rather than in the shared jobs
/// section of `repo/mod.rs`.
pub async fn delete_job_row(db: &Db, job_id: &str) -> Result<(), sqlx::Error> {
    sqlx::query("DELETE FROM jobs WHERE id = ?")
        .bind(job_id)
        .execute(db.writer())
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(prompt: &str, job_id: &str) -> PromptEntry {
        PromptEntry {
            prompt: prompt.to_string(),
            provider: "grok".to_string(),
            count: 1,
            duration: 10,
            aspect_ratio: "9:16".to_string(),
            resolution: "720p".to_string(),
            negative_prompt: None,
            has_media: false,
            job_id: job_id.to_string(),
            timestamp: Utc::now().to_rfc3339(),
        }
    }

    #[tokio::test]
    async fn empty_when_never_written() {
        let (db, _g) = Db::connect_temp().await.unwrap();
        assert!(list_prompts(&db, "quick-test").await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn save_prepends_newest_first() {
        let (db, _g) = Db::connect_temp().await.unwrap();
        save_prompt(&db, "p", entry("first", "job-1")).await.unwrap();
        save_prompt(&db, "p", entry("second", "job-2")).await.unwrap();

        let all = list_prompts(&db, "p").await.unwrap();
        assert_eq!(all.len(), 2);
        assert_eq!(all[0].prompt, "second", "most recent goes first");
        assert_eq!(all[1].prompt, "first");
    }

    #[tokio::test]
    async fn projects_are_isolated() {
        let (db, _g) = Db::connect_temp().await.unwrap();
        save_prompt(&db, "proj-a", entry("a-prompt", "job-a")).await.unwrap();
        save_prompt(&db, "proj-b", entry("b-prompt", "job-b")).await.unwrap();

        let a = list_prompts(&db, "proj-a").await.unwrap();
        let b = list_prompts(&db, "proj-b").await.unwrap();
        assert_eq!(a.len(), 1);
        assert_eq!(b.len(), 1);
        assert_eq!(a[0].prompt, "a-prompt");
        assert_eq!(b[0].prompt, "b-prompt");
    }

    #[tokio::test]
    async fn caps_at_max_history() {
        let (db, _g) = Db::connect_temp().await.unwrap();
        for i in 0..(MAX_PROMPT_HISTORY + 10) {
            save_prompt(&db, "p", entry(&format!("prompt-{i}"), &format!("job-{i}"))).await.unwrap();
        }
        let all = list_prompts(&db, "p").await.unwrap();
        assert_eq!(all.len(), MAX_PROMPT_HISTORY);
        // newest entry (last inserted) is still first
        assert_eq!(all[0].prompt, format!("prompt-{}", MAX_PROMPT_HISTORY + 9));
    }

    #[tokio::test]
    async fn clear_removes_the_row() {
        let (db, _g) = Db::connect_temp().await.unwrap();
        save_prompt(&db, "p", entry("x", "job-x")).await.unwrap();
        assert_eq!(list_prompts(&db, "p").await.unwrap().len(), 1);
        clear_prompts(&db, "p").await.unwrap();
        assert!(list_prompts(&db, "p").await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn delete_job_row_removes_it() {
        use crate::model::JobKind;
        use crate::repo;

        let (db, _g) = Db::connect_temp().await.unwrap();
        repo::create_project(&db, "p").await.unwrap();
        let job = repo::create_job(&db, "p", JobKind::Generate, None, None).await.unwrap();
        assert!(repo::get_job(&db, &job.id).await.unwrap().is_some());

        delete_job_row(&db, &job.id).await.unwrap();
        assert!(repo::get_job(&db, &job.id).await.unwrap().is_none());
    }
}
