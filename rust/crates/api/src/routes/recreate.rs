//! Recreate — metadata endpoints only (the caption→prompt helper + job
//! listing). Frozen contract (`rust/contract/CUTLIST.md` KEEP list /
//! `openapi.json`):
//!   - `POST /api/recreate/generate-prompt`
//!   - `GET /api/recreate/jobs`
//!   - `DELETE /api/recreate/jobs/{job_id}`
//!
//! NOT ported here: the actual recreate *runner* (download source video →
//! extract first/last frame → remove burned-in text via Replicate → surface
//! progress over a WebSocket at `/api/recreate/ws/{job_id}`). That's a
//! multi-minute external-API pipeline with its own progress-broadcast and
//! filesystem lifecycle — out of scope for this metadata-only wave (see
//! mission brief: "NOT the generate runner"). `GET /jobs` therefore always
//! returns `{"jobs": []}` today, because nothing yet creates a recreate job
//! row — that is CORRECT parity for an unimplemented feature, not a bug.
//!
//! ## How a "recreate job" is modeled
//!
//! The old Python app had no `jobs` table — a "job" was just a directory of
//! extracted/cleaned frame images under `projects/<name>/recreate/<job_id>/`.
//! The Rust rewrite's job model (`clab_core::model::JobKind`) only knows
//! `Clip | Burn | Generate` — there is no `Recreate` variant, and the mission
//! brief is explicit that a recreate job is a `Generate` job whose `params`
//! records where it came from (a marker only the wave-3 runner will ever set,
//! since building that runner is out of scope here). We tag it with
//! `"source": "recreate"` in the job's `params` JSON and filter on that in
//! `list_recreate_jobs` / `delete_recreate_job` below, so once the wave-3
//! runner lands, its jobs show up here for free — no schema change, no new
//! `JobKind` variant, no migration.

use axum::extract::{Path, Query, State};
use axum::routing::{delete, get, post};
use axum::{Json, Router};
use serde::Deserialize;

use super::ApiError;
use crate::state::AppState;

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/api/recreate/generate-prompt", post(generate_prompt))
        .route("/api/recreate/jobs", get(list_jobs))
        .route("/api/recreate/jobs/{job_id}", delete(delete_job))
}

/// The marker written into a recreate-originated job's `params` JSON. Kept as
/// a `LIKE` substring match (rather than `json_extract`) so this has zero
/// dependency on SQLite's JSON1 extension being compiled into the sqlx build.
const RECREATE_MARKER: &str = "\"source\":\"recreate\"";

// ── POST /api/recreate/generate-prompt ──────────────────────────────────────

/// Mirrors Python's `GeneratePromptRequest` exactly (routers/recreate.py).
#[derive(Deserialize)]
struct GeneratePromptRequest {
    first_frame: String,
    last_frame: String,
}

/// Same system prompt as the Python endpoint, verbatim — this is the actual
/// product behavior (what kind of prompt gets written), not an implementation
/// detail, so it must match exactly rather than being "equivalent."
const PROMPT_GEN_SYSTEM: &str = "You are a video generation prompt writer. You will receive the first and \
last frames of a short TikTok-style video. Write one plain, minimal \
image-to-video prompt that follows the frames. Keep it under 30 words. \
Name only the main subject, simple camera motion, and implied action. \
Avoid style stacks, hashtags, brand names, creator names, emotions, \
marketing language, and detailed scene invention. Do NOT mention \
'first frame' or 'last frame'. Output ONLY the prompt text.";

/// Use GPT-4o vision to write a video generation prompt from clean frames.
/// Parity with Python's `generate_prompt`: same model, same system prompt,
/// same two-image user message, same `{"prompt": "..."}` response shape.
async fn generate_prompt(
    Json(req): Json<GeneratePromptRequest>,
) -> Result<Json<serde_json::Value>, ApiError> {
    if req.first_frame.is_empty() || req.last_frame.is_empty() {
        return Err(ApiError::BadRequest(
            "Both first_frame and last_frame are required".into(),
        ));
    }
    for (label, uri) in [("first_frame", &req.first_frame), ("last_frame", &req.last_frame)] {
        if !uri.starts_with("data:image/") {
            return Err(ApiError::BadRequest(format!(
                "{label} must be a data:image/... URI, got: {}...",
                &uri.chars().take(60).collect::<String>()
            )));
        }
    }

    let messages = vec![
        serde_json::json!({"role": "system", "content": PROMPT_GEN_SYSTEM}),
        serde_json::json!({
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": req.first_frame}},
                {"type": "image_url", "image_url": {"url": req.last_frame}},
                {"type": "text", "text": "Write a video generation prompt for recreating this video."},
            ]
        }),
    ];

    let prompt = integrations::openai::chat("gpt-4o", messages)
        .await
        .map_err(|e| ApiError::Other(e.context("openai generate-prompt call failed")))?;

    Ok(Json(serde_json::json!({ "prompt": prompt.trim() })))
}

// ── GET /api/recreate/jobs ───────────────────────────────────────────────────

#[derive(Deserialize)]
struct JobsQuery {
    #[serde(default = "default_project")]
    project: String,
}

fn default_project() -> String {
    "quick-test".to_string()
}

/// List recreate-originated jobs for a project. A job counts as a recreate
/// job when its `kind = 'generate'` and its `params` carries the recreate
/// marker (see module docs) — nothing sets that marker yet (the runner is a
/// separate, later wave), so this legitimately returns an empty list today.
async fn list_jobs(
    State(st): State<AppState>,
    Query(q): Query<JobsQuery>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let jobs: Vec<clab_core::model::Job> = sqlx::query_as(
        "SELECT * FROM jobs \
         WHERE project = ? AND kind = 'generate' AND params LIKE ? \
         ORDER BY created_at DESC",
    )
    .bind(&q.project)
    .bind(format!("%{RECREATE_MARKER}%"))
    .fetch_all(st.db.reader())
    .await
    .map_err(ApiError::from)?;

    Ok(Json(serde_json::json!({ "jobs": jobs })))
}

// ── DELETE /api/recreate/jobs/{job_id} ──────────────────────────────────────

#[derive(Deserialize)]
struct DeleteJobQuery {
    #[serde(default = "default_project")]
    #[allow(dead_code)] // parity with Python's query param; not needed to scope the delete (job_id is already a PK)
    project: String,
}

/// Delete a recreate job row. 404s if the job doesn't exist or isn't a
/// recreate-tagged job (mirrors Python's 404 when the job directory is
/// missing) — never deletes an arbitrary clip/burn/generate job by id.
async fn delete_job(
    State(st): State<AppState>,
    Path(job_id): Path<String>,
    Query(_q): Query<DeleteJobQuery>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let existing: Option<(String,)> = sqlx::query_as(
        "SELECT id FROM jobs WHERE id = ? AND kind = 'generate' AND params LIKE ?",
    )
    .bind(&job_id)
    .bind(format!("%{RECREATE_MARKER}%"))
    .fetch_optional(st.db.reader())
    .await
    .map_err(ApiError::from)?;

    if existing.is_none() {
        return Err(ApiError::NotFound);
    }

    sqlx::query("DELETE FROM jobs WHERE id = ?")
        .bind(&job_id)
        .execute(st.db.writer())
        .await
        .map_err(ApiError::from)?;

    Ok(Json(serde_json::json!({ "deleted": true, "job_id": job_id })))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use serde_json::Value;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;
    use tokio::sync::Mutex;
    use tower::ServiceExt;

    /// generate-prompt hits the real OPENAI_BASE_URL env var — serialize
    /// tests that touch it (same reasoning as email.rs's ENV_LOCK).
    static ENV_LOCK: Mutex<()> = Mutex::const_new(());

    async fn test_state() -> AppState {
        let path = std::env::temp_dir().join(format!("recreate-test-{}.db", uuid::Uuid::new_v4()));
        let db = clab_core::Db::connect(&format!("sqlite://{}", path.display()))
            .await
            .unwrap();
        AppState::new(db, None)
    }

    async fn call(st: &AppState, req: Request<Body>) -> (StatusCode, Value) {
        let app = router().with_state(st.clone());
        let resp = app.oneshot(req).await.unwrap();
        let status = resp.status();
        let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let body = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).unwrap()
        };
        (status, body)
    }

    fn get_req(uri: &str) -> Request<Body> {
        Request::builder().uri(uri).body(Body::empty()).unwrap()
    }

    /// Insert a job row directly (bypassing repo::create_job, which doesn't
    /// know about the recreate marker) so tests can set up both
    /// recreate-tagged and plain generate jobs.
    async fn insert_job(st: &AppState, id: &str, project: &str, params: &str) {
        sqlx::query(
            "INSERT INTO jobs (id, project, kind, status, progress, source_asset_id, params, result_asset_ids, error, created_at, updated_at)
             VALUES (?, ?, 'generate', 'done', 1.0, NULL, ?, '[]', NULL, ?, ?)",
        )
        .bind(id)
        .bind(project)
        .bind(params)
        .bind(chrono::Utc::now())
        .bind(chrono::Utc::now())
        .execute(st.db.writer())
        .await
        .unwrap();
    }

    #[tokio::test]
    async fn list_jobs_empty_when_nothing_tagged() {
        let st = test_state().await;
        clab_core::repo::create_project(&st.db, "p").await.unwrap();
        // A plain (non-recreate) generate job must NOT show up here.
        insert_job(&st, "job1", "p", r#"{"model":"grok"}"#).await;

        let (status, body) = call(&st, get_req("/api/recreate/jobs?project=p")).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["jobs"].as_array().unwrap().len(), 0);
    }

    #[tokio::test]
    async fn list_jobs_returns_only_recreate_tagged() {
        let st = test_state().await;
        clab_core::repo::create_project(&st.db, "p").await.unwrap();
        insert_job(&st, "plain1", "p", r#"{"model":"grok"}"#).await;
        insert_job(&st, "rec1", "p", r#"{"source":"recreate","video_url":"https://x"}"#).await;

        let (status, body) = call(&st, get_req("/api/recreate/jobs?project=p")).await;
        assert_eq!(status, StatusCode::OK);
        let jobs = body["jobs"].as_array().unwrap();
        assert_eq!(jobs.len(), 1);
        assert_eq!(jobs[0]["id"], "rec1");
    }

    #[tokio::test]
    async fn list_jobs_scoped_to_project() {
        let st = test_state().await;
        clab_core::repo::create_project(&st.db, "a").await.unwrap();
        clab_core::repo::create_project(&st.db, "b").await.unwrap();
        insert_job(&st, "rec-a", "a", r#"{"source":"recreate"}"#).await;
        insert_job(&st, "rec-b", "b", r#"{"source":"recreate"}"#).await;

        let (_, body) = call(&st, get_req("/api/recreate/jobs?project=a")).await;
        let jobs = body["jobs"].as_array().unwrap();
        assert_eq!(jobs.len(), 1);
        assert_eq!(jobs[0]["id"], "rec-a");
    }

    #[tokio::test]
    async fn delete_recreate_job_ok_then_404() {
        let st = test_state().await;
        clab_core::repo::create_project(&st.db, "p").await.unwrap();
        insert_job(&st, "rec1", "p", r#"{"source":"recreate"}"#).await;

        let (status, body) = call(
            &st,
            Request::builder()
                .method("DELETE")
                .uri("/api/recreate/jobs/rec1?project=p")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["deleted"], true);

        let (status, _) = call(
            &st,
            Request::builder()
                .method("DELETE")
                .uri("/api/recreate/jobs/rec1?project=p")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn delete_refuses_non_recreate_job() {
        let st = test_state().await;
        clab_core::repo::create_project(&st.db, "p").await.unwrap();
        insert_job(&st, "plain1", "p", r#"{"model":"grok"}"#).await;

        let (status, _) = call(
            &st,
            Request::builder()
                .method("DELETE")
                .uri("/api/recreate/jobs/plain1?project=p")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::NOT_FOUND);

        // The plain job must still exist — delete_job must not have deleted it.
        let still_there: Option<(String,)> = sqlx::query_as("SELECT id FROM jobs WHERE id = ?")
            .bind("plain1")
            .fetch_optional(st.db.reader())
            .await
            .unwrap();
        assert!(still_there.is_some());
    }

    /// Minimal HTTP mock for the OpenAI upstream (same pattern as
    /// routes/email.rs's `spawn_mock`; duplicated because cfg(test) items
    /// don't cross module/crate boundaries cleanly here).
    async fn spawn_mock<F>(handler: F) -> (String, tokio::task::JoinHandle<()>)
    where
        F: Fn(&str) -> (u16, String) + Send + Sync + 'static,
    {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let handle = tokio::spawn(async move {
            loop {
                let Ok((mut sock, _)) = listener.accept().await else { break };
                let mut buf = Vec::new();
                let mut tmp = [0u8; 4096];
                let head_end = loop {
                    match sock.read(&mut tmp).await {
                        Ok(0) | Err(_) => break None,
                        Ok(n) => {
                            buf.extend_from_slice(&tmp[..n]);
                            if let Some(p) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                                break Some(p + 4);
                            }
                        }
                    }
                };
                let Some(head_end) = head_end else { continue };
                let head = String::from_utf8_lossy(&buf[..head_end]).to_string();
                let content_len = head
                    .lines()
                    .find_map(|l| {
                        let (k, v) = l.split_once(':')?;
                        k.eq_ignore_ascii_case("content-length")
                            .then(|| v.trim().parse::<usize>().ok())
                            .flatten()
                    })
                    .unwrap_or(0);
                while buf.len() < head_end + content_len {
                    match sock.read(&mut tmp).await {
                        Ok(0) | Err(_) => break,
                        Ok(n) => buf.extend_from_slice(&tmp[..n]),
                    }
                }
                let (status, body) = handler(&head);
                let resp = format!(
                    "HTTP/1.1 {status} MOCK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
                    body.len(),
                    body
                );
                let _ = sock.write_all(resp.as_bytes()).await;
                let _ = sock.shutdown().await;
            }
        });
        (base, handle)
    }

    #[tokio::test]
    async fn generate_prompt_rejects_non_data_uri() {
        let st = test_state().await;
        let (status, body) = call(
            &st,
            Request::builder()
                .method("POST")
                .uri("/api/recreate/generate-prompt")
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({"first_frame": "not-a-data-uri", "last_frame": "data:image/png;base64,AA"})
                        .to_string(),
                ))
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert!(body["error"].as_str().unwrap().contains("first_frame"));
    }

    #[tokio::test]
    async fn generate_prompt_rejects_empty_frames() {
        let st = test_state().await;
        let (status, _) = call(
            &st,
            Request::builder()
                .method("POST")
                .uri("/api/recreate/generate-prompt")
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({"first_frame": "", "last_frame": ""}).to_string(),
                ))
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn generate_prompt_happy_path_via_mock() {
        let _guard = ENV_LOCK.lock().await;
        let (base, handle) = spawn_mock(|_head| {
            (
                200,
                serde_json::json!({
                    "choices": [{"message": {"content": "A woman turns to face the camera and smiles."}}]
                })
                .to_string(),
            )
        })
        .await;
        std::env::set_var("OPENAI_BASE_URL", &base);
        std::env::set_var("OPENAI_API_KEY", "test-key");

        let st = test_state().await;
        let (status, body) = call(
            &st,
            Request::builder()
                .method("POST")
                .uri("/api/recreate/generate-prompt")
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({
                        "first_frame": "data:image/png;base64,AAAA",
                        "last_frame": "data:image/png;base64,BBBB",
                    })
                    .to_string(),
                ))
                .unwrap(),
        )
        .await;

        std::env::remove_var("OPENAI_BASE_URL");
        std::env::remove_var("OPENAI_API_KEY");
        handle.abort();

        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["prompt"], "A woman turns to face the camera and smiles.");
    }
}
