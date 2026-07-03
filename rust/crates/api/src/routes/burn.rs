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
use clab_core::model::{AssetKind, JobKind};
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
        .route("/api/burn/overlay", post(burn_overlay))
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

/// Resolve a burn-overlay `videoPath` to a data-dir-relative string, mirroring
/// `routers/burn.py::burn_overlay` exactly: a path already prefixed with
/// `clips/` is project-relative as-is; anything else lives under the
/// project's `videos/` subdir. The result is handed to
/// `crate::paths::confine_under_data`, which does the actual traversal
/// checking — this function only reproduces the *prefix* rule.
fn video_rel_under_data(project: &str, video_path: &str) -> String {
    if let Some(stripped) = video_path.strip_prefix("clips/") {
        format!("projects/{project}/clips/{stripped}")
    } else {
        format!("projects/{project}/videos/{video_path}")
    }
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

// ── Overlay (legacy per-item burn runner) ───────────────────────────────────
//
// **Contract.** `POST /api/burn/overlay` is the endpoint the live Burn.tsx
// page actually calls (`submitBurnItem`, ~line 947): the frontend generates
// ONE opaque `batchId` client-side (`makeBatchId`, unrelated to any job id)
// and POSTs each (video, overlay) pair one at a time, sharing that batchId
// and carrying its own `index`. It then polls `GET
// /api/burn/batch-status/{batchId}` (below) expecting `items` to accumulate
// one entry per index across all the jobs that share that batchId.
//
// This is a genuinely different shape from `POST /api/jobs/burn` (jobs.rs,
// no frontend caller) where `batch_id == job.id` 1:1. To serve the real
// contract without touching `repo/*` or `jobs.rs`, the external `batchId` +
// `index` are stashed in `job.params` as JSON (the same "repurpose params
// once the runner has consumed it" trick `repo::burn::set_batch_label`
// already uses for the rename label), and `batch_status` below scans jobs
// for that value instead of doing a job-id primary-key lookup.
#[derive(Deserialize)]
struct BurnOverlayRequest {
    project: String,
    #[serde(rename = "batchId")]
    batch_id: String,
    index: i64,
    #[serde(rename = "videoPath")]
    video_path: String,
    #[serde(rename = "overlayPng")]
    overlay_png: Option<String>,
    #[serde(rename = "colorCorrection")]
    color_correction: Option<media::ColorCorrection>,
}

/// Strip a `data:image/png;base64,...` prefix if present — the engine
/// (`run_burn_job` / `media::ffmpeg::burn_overlay`) wants raw base64, but the
/// browser-side `captureTextOverlay` hands over a full data URI.
fn strip_data_uri_prefix(s: &str) -> &str {
    match s.find(",") {
        Some(idx) if s.starts_with("data:") => &s[idx + 1..],
        _ => s,
    }
}

/// POST /api/burn/overlay — queue one item of a client-tracked burn batch and
/// return immediately; the frontend polls `batch-status` for progress.
async fn burn_overlay(
    State(st): State<AppState>,
    Json(req): Json<BurnOverlayRequest>,
) -> Result<Json<Value>, ApiError> {
    let project = crate::paths::sanitize_project_name(&req.project)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;
    if req.batch_id.trim().is_empty() {
        return Err(ApiError::BadRequest("batchId is required".into()));
    }

    // Resolve videoPath -> confined absolute path, then the data-dir-relative
    // string `run_burn_job` (jobs.rs) expects — same prefix rule as Python's
    // `/overlay` (clips/... is project-relative as-is; anything else lives
    // under videos/).
    let data_dir = data_dir();
    let rel_under_data = video_rel_under_data(&project, &req.video_path);
    let video_abs = crate::paths::confine_under_data(&data_dir, &rel_under_data)
        .map_err(|e| ApiError::BadRequest(format!("invalid videoPath: {e}")))?;
    if !video_abs.is_file() {
        return Err(ApiError::BadRequest(format!("Video not found: {}", req.video_path)));
    }

    // Guard the overlay size like the old burn.py (50MB base64 cap → OOM
    // guard) and strip a data-URI prefix if the browser sent one whole.
    let overlay_raw = req.overlay_png.as_deref().map(strip_data_uri_prefix);
    if let Some(b64) = overlay_raw {
        if b64.len() > 50 * 1024 * 1024 {
            return Err(ApiError::BadRequest("overlay PNG too large".into()));
        }
    }

    repo::create_project(&st.db, &project).await?;

    // Find-or-create a Clip asset for this videoPath so `run_burn_job` has a
    // clip_id + path to consume. This endpoint has no prior asset-id
    // handshake (unlike /api/jobs/burn) — the frontend only ever knows a
    // disk path — so a fresh Clip asset is recorded and marked ready
    // immediately (the file already exists on disk, confirmed above).
    // ponytail: no dedup against a previously-recorded asset for the same
    // path — one Clip row per overlay submission. Simplest thing that keeps
    // correct lineage (burned.parent_id -> this clip); a dedup index would
    // need a new repo query, which is out of this file's ownership.
    let clip_meta = json!({ "video_path": req.video_path }).to_string();
    let clip = repo::create_asset(&st.db, &project, AssetKind::Clip, None, Some(&clip_meta)).await?;
    repo::mark_asset_ready(&st.db, &clip.id, &rel_under_data).await?;

    let job_meta = json!({ "batch_id": req.batch_id, "index": req.index }).to_string();
    let job = repo::create_job(&st.db, &project, JobKind::Burn, Some(&clip.id), Some(&job_meta)).await?;

    let db = st.db.clone();
    let job_id = job.id.clone();
    let clip_id = clip.id.clone();
    let overlay_owned = overlay_raw.map(|s| s.to_string());
    let cc = req.color_correction;
    tokio::spawn(async move {
        if let Err(e) =
            super::jobs::run_burn_job(db.clone(), &job_id, &project, &clip_id, &rel_under_data, overlay_owned, cc)
                .await
        {
            tracing::error!("burn overlay job {job_id} failed: {e:#}");
            let _ = repo::fail_job(&db, &job_id, &format!("{e:#}")).await;
        }
    });

    Ok(Json(json!({ "ok": true, "index": req.index, "status": "queued", "job_id": job.id })))
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
/// in-memory poller: `{batchId, items, total, done, ok, failed}`.
///
/// `batch_id` here is the frontend's client-generated, opaque `batchId`
/// (`makeBatchId` in Burn.tsx) — NOT a job id. Every `POST /api/burn/overlay`
/// item shares one `batchId` across N independent jobs (one job per index),
/// stashed in `job.params` as `{"batch_id","index"}` at submit time (see
/// `burn_overlay` above). There is no repo query to look jobs up by an
/// arbitrary params value (adding one would mean editing `repo/*`, out of
/// this file's ownership), so this scans every project's jobs and filters
/// in-process — acceptable at this data scale (SQLite, polled every 2s from
/// one page). Falls back to a direct job-id lookup (the `POST /api/jobs/burn`
/// convention, batch_id == job.id) if no params match, so that path — unused
/// by the frontend today, but exercised by tests — still resolves.
async fn batch_status(
    State(st): State<AppState>,
    Path(batch_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    use clab_core::model::JobStatus;

    let jobs = jobs_for_batch(&st.db, &batch_id).await?;

    let jobs = if jobs.is_empty() {
        // Fall back to direct job-id lookup (POST /api/jobs/burn convention).
        match repo::get_job(&st.db, &batch_id).await? {
            Some(j) if j.kind == JobKind::Burn => vec![(0i64, j)],
            _ => return Err(ApiError::NotFound),
        }
    } else {
        jobs
    };

    let mut items = serde_json::Map::new();
    let mut done_n = 0u64;
    let mut ok_n = 0u64;
    let mut failed_n = 0u64;

    for (index, job) in &jobs {
        let (status_str, ok, failed, done) = match job.status {
            JobStatus::Queued => ("queued", false, false, false),
            JobStatus::Processing => ("burning", false, false, false),
            JobStatus::Done => ("done", true, false, true),
            JobStatus::Failed => ("error", false, true, true),
        };
        if done {
            done_n += 1;
        }
        if ok {
            ok_n += 1;
        }
        if failed {
            failed_n += 1;
        }

        let mut item = json!({
            "status": status_str,
            "index": index,
            "ok": ok,
        });
        if let Some(obj) = item.as_object_mut() {
            if done && ok {
                if let Some(file) = burned_file_for_job(&st.db, job).await? {
                    obj.insert("file".into(), json!(file));
                }
            }
            if failed {
                obj.insert("error".into(), json!(job.error.clone().unwrap_or_default()));
            }
        }
        items.insert(index.to_string(), item);
    }

    Ok(Json(json!({
        "batchId": batch_id,
        "items": Value::Object(items),
        "total": jobs.len(),
        "done": done_n,
        "ok": ok_n,
        "failed": failed_n,
    })))
}

/// Find every `Burn` job across all projects whose `params.batch_id` matches
/// the frontend's opaque batch id, paired with its `params.index`. See
/// `batch_status` doc for why this is a scan rather than an indexed query.
async fn jobs_for_batch(
    db: &clab_core::Db,
    batch_id: &str,
) -> Result<Vec<(i64, clab_core::model::Job)>, ApiError> {
    let mut out = Vec::new();
    for project in repo::list_projects(db).await? {
        for job in repo::list_jobs(db, &project.name).await? {
            if job.kind != JobKind::Burn {
                continue;
            }
            let Some(params) = job.params.as_deref() else { continue };
            let Ok(v) = serde_json::from_str::<Value>(params) else { continue };
            let Some(b) = v.get("batch_id").and_then(|b| b.as_str()) else { continue };
            if b != batch_id {
                continue;
            }
            let index = v.get("index").and_then(|i| i.as_i64()).unwrap_or(0);
            out.push((index, job));
        }
    }
    out.sort_by_key(|(i, _)| *i);
    Ok(out)
}

/// Resolve a finished burn job's output asset to the path fragment the
/// frontend expects in `file`: relative to `/projects/{project}/burned/`
/// (mirrors Python's `out_file = f"{batch_id}/burned_{idx:03d}.mp4"`, which
/// `Burn.tsx` loads via `staticUrl(/projects/{project}/burned/{file})`).
async fn burned_file_for_job(db: &clab_core::Db, job: &clab_core::model::Job) -> Result<Option<String>, ApiError> {
    let ids: Vec<String> = serde_json::from_str(&job.result_asset_ids).unwrap_or_default();
    let Some(id) = ids.first() else { return Ok(None) };
    let Some(asset) = repo::get_asset(db, id).await? else { return Ok(None) };
    let Some(path) = asset.path else { return Ok(None) };
    let prefix = format!("projects/{}/burned/", job.project);
    Ok(Some(path.strip_prefix(&prefix).unwrap_or(&path).to_string()))
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

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    // These tests mutate `RAILWAY_VOLUME_MOUNT_PATH`; the crate-wide guard
    // serializes them against every other env-mutating test in the binary.
    use crate::testlock::ENV_LOCK;

    /// A self-cleaning temp dir (no `tempfile` dev-dependency on this crate —
    /// `api`'s Cargo.toml is out of this file's ownership boundary — so this
    /// hand-rolls the same "unique dir, remove on drop" behavior with
    /// `std::env::temp_dir()` + `uuid`, both already direct dependencies).
    struct TempDir(PathBuf);
    impl TempDir {
        fn path(&self) -> &std::path::Path {
            &self.0
        }
    }
    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    async fn test_state() -> (AppState, TempDir) {
        let dir = std::env::temp_dir().join(format!("burn-overlay-test-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        std::env::set_var("RAILWAY_VOLUME_MOUNT_PATH", &dir);
        let db_path = dir.join("db.sqlite");
        let db = clab_core::Db::connect(&format!("sqlite://{}", db_path.display()))
            .await
            .unwrap();
        (AppState::new(db, None), TempDir(dir))
    }

    async fn call(st: &AppState, req: Request<Body>) -> (StatusCode, Value) {
        // main.rs applies a 100MB DefaultBodyLimit app-wide; this bare
        // sub-router needs the same layer so the oversized-overlay test
        // exercises our 400 guard rather than axum's un-lifted 2MB default.
        let app = router()
            .layer(axum::extract::DefaultBodyLimit::max(100 * 1024 * 1024))
            .with_state(st.clone());
        let resp = app.oneshot(req).await.unwrap();
        let status = resp.status();
        let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let body = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).unwrap_or(Value::Null)
        };
        (status, body)
    }

    fn json_req(method: &str, uri: &str, body: Value) -> Request<Body> {
        Request::builder()
            .method(method)
            .uri(uri)
            .header("content-type", "application/json")
            .body(Body::from(body.to_string()))
            .unwrap()
    }

    /// Write a tiny real video file under `<data>/projects/<project>/videos/`
    /// so `burn_overlay`'s `is_file()` + confinement checks pass. A 1x1
    /// yuv420p mp4 — small enough to generate inline via ffmpeg (present in
    /// this dev environment; if ffmpeg is missing the test will fail loudly
    /// rather than silently pass, which is the right failure mode here).
    async fn write_test_video(dir: &std::path::Path, project: &str, name: &str) -> String {
        let video_dir = dir.join("projects").join(project).join("videos");
        tokio::fs::create_dir_all(&video_dir).await.unwrap();
        let out = video_dir.join(name);
        let status = tokio::process::Command::new("ffmpeg")
            .args([
                "-y", "-f", "lavfi", "-i", "color=c=black:s=32x32:d=1", "-pix_fmt", "yuv420p", "-c:v", "libx264",
            ])
            .arg(&out)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .await
            .unwrap();
        assert!(status.success(), "ffmpeg failed to build test fixture video");
        name.to_string()
    }

    #[tokio::test]
    async fn overlay_queues_and_batch_status_groups_by_external_batch_id() {
        let _g = ENV_LOCK.lock().await;
        let (st, dir) = test_state().await;
        let video = write_test_video(dir.path(), "proj", "clip.mp4").await;

        let batch_id = "my-batch-2026-abcd";
        for i in 0..3i64 {
            let (status, body) = call(
                &st,
                json_req(
                    "POST",
                    "/api/burn/overlay",
                    json!({
                        "project": "proj",
                        "batchId": batch_id,
                        "index": i,
                        "videoPath": video,
                        "overlayPng": null,
                        "colorCorrection": null,
                    }),
                ),
            )
            .await;
            assert_eq!(status, StatusCode::OK, "submit #{i} body={body:?}");
            assert_eq!(body["ok"], true);
            assert_eq!(body["index"], i);
        }

        // Poll immediately — the three submissions share one external
        // batchId even though each is backed by its own Job row.
        let (status, body) = call(&st, get(&format!("/api/burn/batch-status/{batch_id}"))).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["batchId"], batch_id);
        assert_eq!(body["total"], 3);
        let items = body["items"].as_object().unwrap();
        assert_eq!(items.len(), 3);
        for i in 0..3i64 {
            let item = &items[&i.to_string()];
            assert_eq!(item["index"], i);
            assert!(item["status"].is_string());
        }
    }

    #[tokio::test]
    async fn overlay_strips_data_uri_prefix_and_accepts_color_correction_only() {
        let _g = ENV_LOCK.lock().await;
        let (st, dir) = test_state().await;
        let video = write_test_video(dir.path(), "proj2", "clip.mp4").await;

        // 1x1 transparent PNG, base64-encoded, wrapped as a data URI — same
        // shape captureTextOverlay hands the endpoint.
        let png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
        let data_uri = format!("data:image/png;base64,{png_b64}");

        let (status, body) = call(
            &st,
            json_req(
                "POST",
                "/api/burn/overlay",
                json!({
                    "project": "proj2",
                    "batchId": "b2",
                    "index": 0,
                    "videoPath": video,
                    "overlayPng": data_uri,
                    "colorCorrection": {"brightness": 10, "contrast": -5},
                }),
            ),
        )
        .await;
        assert_eq!(status, StatusCode::OK, "body={body:?}");
        assert_eq!(body["ok"], true);
    }

    #[tokio::test]
    async fn overlay_rejects_missing_video() {
        let _g = ENV_LOCK.lock().await;
        let (st, dir) = test_state().await;
        tokio::fs::create_dir_all(dir.path().join("projects").join("proj3").join("videos"))
            .await
            .unwrap();

        let (status, body) = call(
            &st,
            json_req(
                "POST",
                "/api/burn/overlay",
                json!({
                    "project": "proj3",
                    "batchId": "b3",
                    "index": 0,
                    "videoPath": "does-not-exist.mp4",
                    "overlayPng": null,
                    "colorCorrection": null,
                }),
            ),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert!(body["error"].as_str().unwrap().contains("not found"));
    }

    #[tokio::test]
    async fn overlay_rejects_oversized_overlay() {
        let _g = ENV_LOCK.lock().await;
        let (st, dir) = test_state().await;
        let video = write_test_video(dir.path(), "proj4", "clip.mp4").await;

        let huge = "A".repeat(51 * 1024 * 1024);
        let (status, body) = call(
            &st,
            json_req(
                "POST",
                "/api/burn/overlay",
                json!({
                    "project": "proj4",
                    "batchId": "b4",
                    "index": 0,
                    "videoPath": video,
                    "overlayPng": huge,
                    "colorCorrection": null,
                }),
            ),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert!(body["error"].as_str().unwrap().contains("too large"));
    }

    #[tokio::test]
    async fn batch_status_unknown_id_is_404() {
        let _g = ENV_LOCK.lock().await;
        let (st, _dir) = test_state().await;
        let (status, _) = call(&st, get("/api/burn/batch-status/nonexistent-batch")).await;
        assert_eq!(status, StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn batch_status_falls_back_to_direct_job_id_lookup() {
        let _g = ENV_LOCK.lock().await;
        let (st, _dir) = test_state().await;
        repo::create_project(&st.db, "proj5").await.unwrap();
        let job = repo::create_job(&st.db, "proj5", clab_core::model::JobKind::Burn, None, None)
            .await
            .unwrap();

        // No params -> jobs_for_batch scan finds nothing -> falls back to
        // treating the path segment as a literal job id (the POST
        // /api/jobs/burn convention).
        let (status, body) = call(&st, get(&format!("/api/burn/batch-status/{}", job.id))).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["total"], 1);
        assert_eq!(body["items"]["0"]["status"], "queued");
    }

    #[test]
    fn video_rel_under_data_matches_python_prefix_rule() {
        assert_eq!(video_rel_under_data("proj", "foo.mp4"), "projects/proj/videos/foo.mp4");
        assert_eq!(
            video_rel_under_data("proj", "clips/job1/clip_000.mp4"),
            "projects/proj/clips/job1/clip_000.mp4"
        );
    }

    #[test]
    fn strip_data_uri_prefix_handles_both_shapes() {
        assert_eq!(strip_data_uri_prefix("data:image/png;base64,QUJD"), "QUJD");
        assert_eq!(strip_data_uri_prefix("QUJD"), "QUJD");
    }

    fn get(uri: &str) -> Request<Body> {
        Request::builder().uri(uri).body(Body::empty()).unwrap()
    }
}
