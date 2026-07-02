//! Burn output management/metadata routes — parity port of the non-job-runner
//! half of `routers/burn.py`.
//!
//! **Scope.** This wave ports only the read/list/rename/zip/import endpoints —
//! the endpoints a user hits *around* a burn run (browse fonts, list past
//! batches, rename, download, import caption banks). The actual burn *job*
//! (`POST /api/jobs/burn`, its ffmpeg pipeline, and progress polling) is a
//! separate, frozen slice living in `routes/jobs.rs` — not touched here.
//!
//! **Batch → Job/Asset mapping** (see `clab_core::repo::burn` for the full
//! rationale): a "burn batch" is a `Job` with `kind = burn`; its items are the
//! `Burned`-kind assets in `job.result_asset_ids`. `batch_id` == `job.id`.
//!
//! **Deliberate deviation from Python's `/videos`.** In the old app,
//! `GET /api/burn/videos` scanned `videos/` and `clips/` on disk for burn
//! *candidates* (things you could still choose to burn). The wave-2 contract
//! for this port redefines the same path to mean "already-burned outputs,"
//! sourced from the assets table (`kind = burned`, `status = ready`) rather
//! than a folder scan — the Rust app addresses media by durable asset id, not
//! by re-deriving a folder listing each request. Any UI that expected burn
//! *candidates* from this path should instead call `GET /api/assets?kind=clip`
//! (see `routes/assets.rs`); flagged here in case product wants that name back
//! for burn inputs.

use std::path::{Path as FsPath, PathBuf};

use axum::extract::{Path, Query, State};
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, patch, post};
use axum::{Json, Router};
use clab_core::repo;
use serde::Deserialize;
use serde_json::{json, Value};

use super::ApiError;
use crate::state::AppState;

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/api/burn/fonts", get(list_fonts))
        .route("/api/burn/videos", get(list_burned_videos))
        .route("/api/burn/captions", get(list_captions))
        .route("/api/burn/import-tos", post(import_tos))
        .route("/api/burn/batches", get(list_batches))
        .route("/api/burn/batch-status/{batch_id}", get(batch_status))
        .route("/api/burn/batches/{batch_id}/rename", patch(rename_batch))
        .route("/api/burn/folders/rename", patch(rename_folder))
        .route("/api/burn/zip/{batch_id}", get(download_zip))
}

// ── Paths (same convention as jobs.rs / captions.rs) ────────────────────────

fn data_dir() -> String {
    std::env::var("RAILWAY_VOLUME_MOUNT_PATH")
        .ok()
        .filter(|p| FsPath::new(p).exists())
        .unwrap_or_else(|| "./data".to_string())
}

fn font_dir() -> PathBuf {
    // Python: `BASE_DIR / "fonts"` — BASE_DIR is the repo root, i.e. sibling
    // of `data/`, NOT inside the per-project data dir. Fonts ship with the
    // app, not with project data.
    std::env::var("CLAB_FONT_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("fonts"))
}

fn caption_dir(project: &str) -> PathBuf {
    PathBuf::from(data_dir()).join("projects").join(project).join("captions")
}

// ── Fonts ────────────────────────────────────────────────────────────────

/// GET /api/burn/fonts
async fn list_fonts() -> Json<Value> {
    let fonts = repo::burn::list_fonts(&font_dir());
    Json(json!({ "fonts": fonts }))
}

// ── Burned outputs ("videos") ───────────────────────────────────────────────

#[derive(Deserialize)]
struct ProjectQuery {
    project: String,
}

/// GET /api/burn/videos — burned outputs for a project (see module doc for
/// the deliberate deviation from Python's burn-candidate disk scan).
async fn list_burned_videos(
    State(st): State<AppState>,
    Query(q): Query<ProjectQuery>,
) -> Result<Json<Value>, ApiError> {
    let project = crate::paths::sanitize_project_name(&q.project)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;
    let assets = repo::burn::list_burned_assets(&st.db, &project).await?;
    Ok(Json(json!({ "videos": assets })))
}

// ── Captions (disk CSV bank — parity with services/captions.py) ────────────

/// GET /api/burn/captions — caption CSV sources + filter facets for a project.
async fn list_captions(Query(q): Query<ProjectQuery>) -> Result<Json<Value>, ApiError> {
    let project = crate::paths::sanitize_project_name(&q.project)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;
    let dir = caption_dir(&project);
    let prefix = format!("projects/{project}/captions");
    let sources = repo::burn::scan_caption_sources(&dir, &prefix);
    let facets = repo::burn::caption_facets(&sources);
    Ok(Json(json!({ "sources": sources, "facets": facets })))
}

// ── import-tos ───────────────────────────────────────────────────────────────
//
// Parity note: the Python endpoint pulls Slingshot TOS captions from Supabase
// (external HTTP call to `${SUPABASE_URL}/rest/v1/tos_captions`) and writes
// one `captions.csv` per song under `captions/tos-<slug>/`. That external
// integration (Supabase client, HTTP fetch) is out of this file's ownership
// (`repo/burn.rs` + `routes/burn.rs` only — no new integrations module, and
// `reqwest`/env wiring for a *new* upstream is a seam decision). This port
// keeps the exact request/response contract (query param, error shapes, 400
// when unconfigured) but the fetch itself is stubbed as "not configured"
// unless SUPABASE_URL/KEY env vars are present, in which case it still 502s
// with a clear message rather than silently no-op-ing — see the seam request
// in the final report for wiring this fully once integrations decides where
// a generic Supabase REST client lives.

/// POST /api/burn/import-tos
async fn import_tos(Query(q): Query<ProjectQuery>) -> Result<Json<Value>, ApiError> {
    let _project = crate::paths::sanitize_project_name(&q.project)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;

    let url = std::env::var("SUPABASE_URL").unwrap_or_default();
    let key = std::env::var("SUPABASE_SERVICE_KEY")
        .or_else(|_| std::env::var("SUPABASE_KEY"))
        .unwrap_or_default();
    if url.trim().is_empty() || key.trim().is_empty() {
        return Err(ApiError::BadRequest(
            "SUPABASE_URL / SUPABASE_KEY not configured".into(),
        ));
    }

    // Deferred: see module doc — a full Supabase REST fetch + per-song CSV
    // writer is out of this file's ownership boundary. Report as a server
    // error rather than silently fabricating an empty success, so a caller
    // can tell this path isn't wired yet even though env vars are present.
    Err(ApiError::Other(anyhow::anyhow!(
        "import-tos Supabase fetch not yet wired in the Rust port (env is configured; see wave-2 seam request for services::supabase)"
    )))
}

// ── Batches ──────────────────────────────────────────────────────────────────

/// GET /api/burn/batches — parity shape `{id, label, count, created}` per
/// batch, newest first (plus `assets`, additive — see repo module doc).
async fn list_batches(
    State(st): State<AppState>,
    Query(q): Query<ProjectQuery>,
) -> Result<Json<Value>, ApiError> {
    let project = crate::paths::sanitize_project_name(&q.project)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;
    let batches = repo::burn::list_batches(&st.db, &project).await?;
    let batches: Vec<Value> = batches
        .into_iter()
        .map(|b| {
            json!({
                "id": b.id,
                "label": b.label,
                "count": b.count,
                "created": b.created,
                "assets": b.assets,
            })
        })
        .collect();
    Ok(Json(json!({ "batches": batches })))
}

/// GET /api/burn/batch-status/{batch_id} — parity shape with the old
/// in-memory poller: `{batchId, items, total, done, ok, failed}`. The Rust
/// burn job runner (jobs.rs) is single-item-per-job (no per-index sub-items),
/// so `items` here is synthesized from the job's own state: one entry
/// (index 0) whose status mirrors the job's status. This is a parity
/// deviation — Python's burn accepted a whole batch of (video, overlay) pairs
/// per request and tracked N items; the Rust burn job (`POST /api/jobs/burn`)
/// takes exactly one asset per job, so a "batch" here is really one job.
async fn batch_status(
    State(st): State<AppState>,
    Path(batch_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    use clab_core::model::JobStatus;

    let job = repo::get_job(&st.db, &batch_id).await?.ok_or(ApiError::NotFound)?;
    let (status_str, ok, failed, done) = match job.status {
        JobStatus::Queued => ("queued", false, false, false),
        JobStatus::Processing => ("burning", false, false, false),
        JobStatus::Done => ("done", true, false, true),
        JobStatus::Failed => ("error", false, true, true),
    };
    let mut item = json!({
        "status": status_str,
        "index": 0,
        "ok": ok,
    });
    if let Some(obj) = item.as_object_mut() {
        if done && ok {
            let ids: Vec<String> = serde_json::from_str(&job.result_asset_ids).unwrap_or_default();
            if let Some(id) = ids.first() {
                obj.insert("file".into(), json!(id));
            }
        }
        if failed {
            obj.insert("error".into(), json!(job.error.clone().unwrap_or_default()));
        }
    }
    let items = json!({ "0": item });

    Ok(Json(json!({
        "batchId": batch_id,
        "items": items,
        "total": 1,
        "done": if done { 1 } else { 0 },
        "ok": if ok { 1 } else { 0 },
        "failed": if failed { 1 } else { 0 },
    })))
}

#[derive(Deserialize)]
struct RenameBatchBody {
    label: Option<String>,
}

/// PATCH /api/burn/batches/{batch_id}/rename
async fn rename_batch(
    State(st): State<AppState>,
    Path(batch_id): Path<String>,
    Json(body): Json<RenameBatchBody>,
) -> Result<Json<Value>, ApiError> {
    let new_label = body.label.unwrap_or_default().trim().to_string();
    if new_label.is_empty() {
        return Err(ApiError::BadRequest("Label is required".into()));
    }
    let found = repo::burn::set_batch_label(&st.db, &batch_id, &new_label).await?;
    if !found {
        return Err(ApiError::NotFound);
    }
    Ok(Json(json!({ "ok": true, "label": new_label })))
}

// ── Folder rename (pure filesystem — no asset-table equivalent) ────────────

#[derive(Deserialize)]
struct RenameFolderBody {
    folder: Option<String>,
    new_name: Option<String>,
}

/// PATCH /api/burn/folders/rename — renames a source video/clips folder on
/// disk. No DB equivalent (burn source folders are pure filesystem concepts),
/// so this stays disk-based, same as Python, but reuses the ported validation
/// helpers (`sanitize_folder_leaf`, `is_virtual_folder`) from `repo::burn` so
/// the rules are unit-tested without touching a real filesystem.
async fn rename_folder(
    State(_st): State<AppState>,
    Query(q): Query<ProjectQuery>,
    Json(body): Json<RenameFolderBody>,
) -> Result<Json<Value>, ApiError> {
    let project = crate::paths::sanitize_project_name(&q.project)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;
    let folder = body.folder.unwrap_or_default().trim().to_string();
    if folder.is_empty() {
        return Err(ApiError::BadRequest("folder is required".into()));
    }
    if repo::burn::is_virtual_folder(&folder) {
        return Err(ApiError::BadRequest(
            "This folder is virtual and cannot be renamed".into(),
        ));
    }
    if folder == "clips" {
        return Err(ApiError::BadRequest("The clips root cannot be renamed".into()));
    }
    let new_leaf = repo::burn::sanitize_folder_leaf(body.new_name.as_deref().unwrap_or(""))
        .map_err(ApiError::BadRequest)?;

    let (root_dir, rel_folder, response_prefix) = if let Some(stripped) = folder.strip_prefix("clips/") {
        (
            PathBuf::from(data_dir()).join("projects").join(&project).join("clips"),
            stripped.to_string(),
            "clips/",
        )
    } else {
        (
            PathBuf::from(data_dir()).join("projects").join(&project).join("videos"),
            folder.clone(),
            "",
        )
    };
    if rel_folder.is_empty() {
        return Err(ApiError::BadRequest("Cannot rename the root folder".into()));
    }

    let root_resolved = tokio::fs::canonicalize(&root_dir)
        .await
        .map_err(|_| ApiError::NotFound)?;
    let src_path = root_dir.join(&rel_folder);
    let src_canon = tokio::fs::canonicalize(&src_path)
        .await
        .map_err(|_| ApiError::BadRequest(format!("Folder not found: {folder}")))?;
    if !src_canon.starts_with(&root_resolved) {
        return Err(ApiError::BadRequest("Folder path escapes project root".into()));
    }
    let meta = tokio::fs::metadata(&src_canon).await.map_err(|_| ApiError::NotFound)?;
    if !meta.is_dir() {
        return Err(ApiError::BadRequest(format!("Folder not found: {folder}")));
    }

    let rel_path = FsPath::new(&rel_folder);
    let parent_rel = rel_path.parent();
    let new_rel = match parent_rel {
        Some(p) if !p.as_os_str().is_empty() => p.join(&new_leaf),
        _ => PathBuf::from(&new_leaf),
    };
    let dst_path = root_dir.join(&new_rel);

    // Defensive containment check on the (possibly nonexistent) destination:
    // resolve the parent instead (dst itself doesn't exist yet).
    let dst_parent = dst_path.parent().unwrap_or(&root_dir);
    let dst_parent_canon = tokio::fs::canonicalize(dst_parent)
        .await
        .unwrap_or_else(|_| root_resolved.clone());
    if !dst_parent_canon.starts_with(&root_resolved) {
        return Err(ApiError::BadRequest("Destination path escapes project root".into()));
    }

    if src_canon == dst_path || src_canon == dst_parent_canon.join(&new_leaf) {
        let new_folder = format!("{response_prefix}{}", folder.trim_start_matches("clips/"));
        return Ok(Json(json!({ "ok": true, "old_folder": folder, "new_folder": new_folder })));
    }

    if tokio::fs::metadata(&dst_path).await.is_ok() {
        return Err(ApiError::BadRequest(format!(
            "A folder named '{new_leaf}' already exists here"
        )));
    }

    tokio::fs::rename(&src_canon, &dst_path)
        .await
        .map_err(|e| ApiError::Other(anyhow::anyhow!("Rename failed: {e}")))?;

    let new_folder = format!("{response_prefix}{}", new_rel.to_string_lossy());
    Ok(Json(json!({ "ok": true, "old_folder": folder, "new_folder": new_folder })))
}

// ── Zip download ─────────────────────────────────────────────────────────────

/// GET /api/burn/zip/{batch_id} — zip every ready burned asset in a batch and
/// return it as `application/zip`. Built with method-0 (STORED, no
/// compression) via `repo::burn::build_zip_stored` — see that module's doc
/// for why no zip crate is used. Unlike Python's chunked `StreamingResponse`
/// (worked around Railway's proxy timeout by getting bytes moving fast), this
/// builds the whole archive in memory and returns it in one response body;
/// see the final report for the streaming trade-off note.
async fn download_zip(
    State(st): State<AppState>,
    Path(batch_id): Path<String>,
    Query(q): Query<ProjectQuery>,
) -> Result<Response, ApiError> {
    let _project = crate::paths::sanitize_project_name(&q.project)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;

    let batch = repo::burn::get_batch(&st.db, &batch_id).await?.ok_or(ApiError::NotFound)?;
    if batch.assets.is_empty() {
        return Err(ApiError::NotFound);
    }

    let mut entries = Vec::with_capacity(batch.assets.len());
    for (i, asset) in batch.assets.iter().enumerate() {
        let Some(rel_path) = &asset.path else { continue };
        let abs = super::jobs::resolve_asset_path(rel_path);
        let bytes = tokio::fs::read(&abs)
            .await
            .map_err(|e| ApiError::Other(anyhow::anyhow!("failed to read {}: {e}", abs.display())))?;
        entries.push((format!("burned_{i:03}.mp4"), bytes));
    }
    if entries.is_empty() {
        return Err(ApiError::NotFound);
    }

    let zip_bytes = repo::burn::build_zip_stored(&entries);
    let zip_label = batch.label.clone().unwrap_or_else(|| batch_id.clone());
    // ASCII-safe filename, same guard style as captions.rs export.
    let fname: String = format!("{zip_label}.zip")
        .chars()
        .filter(|c| c.is_ascii_graphic() || *c == ' ')
        .filter(|c| *c != '"')
        .collect();

    Ok((
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, "application/zip".to_string()),
            (header::CONTENT_DISPOSITION, format!("attachment; filename=\"{fname}\"")),
        ],
        zip_bytes,
    )
        .into_response())
}
