//! Clipper MANAGEMENT routes — port of the non-runner surface of
//! `routers/clipper.py`: job listing/rename/delete/download-all, batch status
//! polling, cookie management, R2 upload staging, and URL-source resolution.
//!
//! The clip *runner* (`POST /api/jobs/clip`, the ffmpeg pipeline) lives in
//! `routes/jobs.rs` and is out of scope here (wave-3). This file talks to the
//! runner's output only through `clab_core::repo::clipper` (jobs+assets rows).
//!
//! Response field names are kept byte-compatible with the Python router where
//! the contract is frozen (`jobs`, `job_id`, `label`, `clip_count`, `clips`,
//! `name`, `url`, `thumb_url`, etc.) — see the KEEP list in the wave-2 brief.

use std::path::{Path as FsPath, PathBuf};
use std::time::Duration;

use axum::extract::{Path, Query, State};
use axum::routing::{delete, get, patch, post};
use axum::{Json, Router};
use clab_core::repo;
use serde::Deserialize;
use serde_json::{json, Value};

use super::ApiError;
use crate::state::AppState;

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/api/clipper/jobs", get(list_jobs))
        .route("/api/clipper/jobs/{job_id}", delete(delete_job))
        .route("/api/clipper/jobs/{job_id}/rename", patch(rename_job))
        .route("/api/clipper/jobs/{job_id}/download-all", get(download_all))
        .route(
            "/api/clipper/process-batch",
            post(process_batch_start),
        )
        .route("/api/clipper/process-batch/{job_id}", get(process_batch_status))
        .route("/api/clipper/download-url", post(download_url))
        .route("/api/clipper/cookies", post(cookies_upload).delete(cookies_delete))
        .route("/api/clipper/cookies/status", get(cookies_status))
        .route("/api/clipper/r2/upload-init", post(r2_upload_init))
        .route("/api/clipper/r2/upload-complete", post(r2_upload_complete))
        // stage-streamed exists for multi-GB source uploads; disable the global
        // 100MB DefaultBodyLimit for this one route (the handler streams to disk
        // and enforces MAX_STAGE_UPLOAD_BYTES incrementally).
        .route(
            "/api/clipper/stage-streamed",
            post(stage_streamed).layer(axum::extract::DefaultBodyLimit::disable()),
        )
}

// ── shared helpers ───────────────────────────────────────────────────────────

fn data_dir() -> String {
    std::env::var("RAILWAY_VOLUME_MOUNT_PATH")
        .ok()
        .filter(|p| FsPath::new(p).exists())
        .unwrap_or_else(|| "./data".to_string())
}

fn default_project() -> String {
    "quick-test".to_string()
}

#[derive(Deserialize)]
struct ProjectQuery {
    #[serde(default = "default_project")]
    project: String,
}

/// A clip's location, byte-compatible with the Python `{name, url, thumb_url}`
/// shape. `name` is the file's basename (e.g. `clip_000.mp4`); `url`/`thumb_url`
/// are paths under the `/projects` static mount.
fn clip_json(sanitized_project: &str, job_id: &str, asset: &clab_core::model::Asset) -> Value {
    let path = asset.path.as_deref().unwrap_or_default();
    let name = FsPath::new(path)
        .file_name()
        .and_then(|f| f.to_str())
        .unwrap_or("clip.mp4")
        .to_string();
    let stem = name.strip_suffix(".mp4").unwrap_or(&name);
    let url = format!("/projects/{sanitized_project}/clips/{job_id}/{name}");
    // Mirror the Python thumbnail-name fallback chain: "<stem>_thumb.jpg" then
    // "thumb_<index>.jpg", else no thumbnail. We can't stat the filesystem for
    // the *other* variant from here without extra I/O per clip — so this checks
    // both candidate paths on disk (cheap, local files) same as Python did.
    let job_dir = PathBuf::from(&data_dir())
        .join("projects")
        .join(sanitized_project)
        .join("clips")
        .join(job_id);
    let thumb_a = job_dir.join(format!("{stem}_thumb.jpg"));
    let idx = stem.rsplit('_').next().unwrap_or(stem);
    let thumb_b = job_dir.join(format!("thumb_{idx}.jpg"));
    let thumb_url = if thumb_a.is_file() {
        Some(format!("/projects/{sanitized_project}/clips/{job_id}/{stem}_thumb.jpg"))
    } else if thumb_b.is_file() {
        Some(format!("/projects/{sanitized_project}/clips/{job_id}/thumb_{idx}.jpg"))
    } else {
        None
    };
    json!({ "name": name, "url": url, "thumb_url": thumb_url })
}

// ── GET /api/clipper/jobs ────────────────────────────────────────────────────

async fn list_jobs(
    State(st): State<AppState>,
    Query(q): Query<ProjectQuery>,
) -> Result<Json<Value>, ApiError> {
    let sanitized = crate::paths::sanitize_project_name(&q.project)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;
    let jobs = repo::clipper::list_clip_jobs(&st.db, &sanitized).await?;

    // Match Python: skip jobs with zero clips (still running / produced nothing).
    let out: Vec<Value> = jobs
        .into_iter()
        .filter(|j| !j.clips.is_empty())
        .map(|j| {
            let label = repo::clipper::job_label(&j.job);
            let clips: Vec<Value> = j
                .clips
                .iter()
                .map(|a| clip_json(&sanitized, &j.job.id, a))
                .collect();
            json!({
                "job_id": j.job.id,
                "label": label,
                "clip_count": clips.len(),
                "clips": clips,
            })
        })
        .collect();

    Ok(Json(json!({ "jobs": out })))
}

// ── PATCH /api/clipper/jobs/{job_id}/rename ─────────────────────────────────

#[derive(Deserialize)]
struct RenameBody {
    label: Option<String>,
}

async fn rename_job(
    State(st): State<AppState>,
    Path(job_id): Path<String>,
    Query(q): Query<ProjectQuery>,
    Json(body): Json<RenameBody>,
) -> Result<Json<Value>, ApiError> {
    let sanitized = crate::paths::sanitize_project_name(&q.project)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;
    let label = body.label.unwrap_or_default().trim().to_string();
    if label.is_empty() {
        return Err(ApiError::BadRequest("Label is required".into()));
    }
    let ok = repo::clipper::rename_clip_job(&st.db, &sanitized, &job_id, &label).await?;
    if !ok {
        return Err(ApiError::NotFound);
    }
    Ok(Json(json!({ "ok": true, "label": label })))
}

// ── DELETE /api/clipper/jobs/{job_id} ───────────────────────────────────────

async fn delete_job(
    State(st): State<AppState>,
    Path(job_id): Path<String>,
    Query(q): Query<ProjectQuery>,
) -> Result<Json<Value>, ApiError> {
    let sanitized = crate::paths::sanitize_project_name(&q.project)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;

    // Resolve before delete so we still have the clip paths to remove on disk.
    let _existing = repo::clipper::get_clip_job(&st.db, &sanitized, &job_id)
        .await?
        .ok_or(ApiError::NotFound)?;

    let ok = repo::clipper::delete_clip_job(&st.db, &sanitized, &job_id).await?;
    if !ok {
        return Err(ApiError::NotFound);
    }

    // Best-effort filesystem cleanup (mirrors Python's safe_rmtree on the job
    // dir) — never fail the request over a stray file.
    let job_dir = PathBuf::from(data_dir())
        .join("projects")
        .join(&sanitized)
        .join("clips")
        .join(&job_id);
    let _ = tokio::fs::remove_dir_all(&job_dir).await;

    Ok(Json(json!({ "deleted": true, "job_id": job_id })))
}

// ── GET /api/clipper/jobs/{job_id}/download-all ─────────────────────────────
//
// Python's primary path returns presigned R2 URLs (bypasses the Railway proxy
// timeout); falls back to a server-built ZIP when R2 isn't configured or a
// clip wasn't mirrored. We port both: R2 presign via the already-built
// `integrations::r2` client, and a minimal hand-rolled ZIP_STORED writer for
// the fallback (no `zip` crate is available to this crate this wave — see
// seam request in the final report).

async fn download_all(
    State(st): State<AppState>,
    Path(job_id): Path<String>,
    Query(q): Query<ProjectQuery>,
) -> Result<axum::response::Response, ApiError> {
    use axum::body::Body;
    use axum::http::header;
    use axum::response::IntoResponse;

    let sanitized = crate::paths::sanitize_project_name(&q.project)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;
    let existing = repo::clipper::get_clip_job(&st.db, &sanitized, &job_id)
        .await?
        .ok_or(ApiError::NotFound)?;
    if existing.clips.is_empty() {
        return Err(ApiError::NotFound);
    }

    if let Some(r2) = integrations::r2::R2::from_env() {
        let mut urls = Vec::new();
        for clip in &existing.clips {
            let path = clip.path.as_deref().unwrap_or_default();
            let name = FsPath::new(path)
                .file_name()
                .and_then(|f| f.to_str())
                .unwrap_or("clip.mp4")
                .to_string();
            let key = integrations::r2::clip_key(&sanitized, &job_id, &name);
            let abs = super::jobs::resolve_asset_path(path);
            let present = r2.exists(&key).await.unwrap_or(false);
            let present = if present {
                true
            } else if abs.is_file() {
                // Backfill: mirror the clip to R2 like Python does for older
                // pre-R2 jobs, so the client always gets a direct-download URL.
                r2.upload_from_path(&key, &abs, "video/mp4").await.is_ok()
            } else {
                false
            };
            if present {
                if let Ok(url) = r2
                    .presign_get(&key, integrations::r2::DOWNLOAD_TTL, Some(&name))
                    .await
                {
                    let size = tokio::fs::metadata(&abs).await.map(|m| m.len()).unwrap_or(0);
                    urls.push(json!({ "name": name, "url": url, "size": size }));
                }
            }
        }
        if !urls.is_empty() {
            return Ok(Json(json!({ "mode": "r2", "job_id": job_id, "clips": urls })).into_response());
        }
    }

    // Fallback: build an uncompressed (STORED) zip of the clips on disk.
    let mut entries = Vec::new();
    for clip in &existing.clips {
        let path = clip.path.as_deref().unwrap_or_default();
        let name = FsPath::new(path)
            .file_name()
            .and_then(|f| f.to_str())
            .unwrap_or("clip.mp4")
            .to_string();
        let abs = super::jobs::resolve_asset_path(path);
        match tokio::fs::read(&abs).await {
            Ok(bytes) => entries.push((name, bytes)),
            Err(e) => tracing::warn!("download-all: skipping unreadable clip {}: {e}", abs.display()),
        }
    }
    if entries.is_empty() {
        return Err(ApiError::NotFound);
    }
    let zip_bytes = zipstore::write_stored_zip(&entries);

    let headers = [
        (header::CONTENT_TYPE, "application/zip".to_string()),
        (
            header::CONTENT_DISPOSITION,
            format!("attachment; filename=\"{job_id}.zip\""),
        ),
    ];
    Ok((headers, Body::from(zip_bytes)).into_response())
}

/// A tiny, dependency-free ZIP writer (STORED = no compression, matching the
/// Python fallback's `zipfile.ZIP_STORED`). This crate has no `zip` crate
/// available this wave (api/Cargo.toml is owned by another workstream) — see
/// seam request in the final report to add a real `zip` dependency later.
mod zipstore {
    /// CRC-32 (ISO-HDLC / zip's checksum), computed without any external crate.
    fn crc32(data: &[u8]) -> u32 {
        let mut crc: u32 = 0xFFFF_FFFF;
        for &byte in data {
            crc ^= byte as u32;
            for _ in 0..8 {
                let mask = (crc & 1).wrapping_neg();
                crc = (crc >> 1) ^ (0xEDB8_8320 & mask);
            }
        }
        !crc
    }

    /// Build a minimal ZIP archive (local file headers + central directory,
    /// no compression) from `(name, bytes)` entries.
    pub fn write_stored_zip(entries: &[(String, Vec<u8>)]) -> Vec<u8> {
        let mut out = Vec::new();
        let mut central = Vec::new();

        for (name, data) in entries {
            let offset = out.len() as u32;
            let crc = crc32(data);
            let name_bytes = name.as_bytes();

            // Local file header.
            out.extend_from_slice(&0x0403_4b50u32.to_le_bytes());
            out.extend_from_slice(&20u16.to_le_bytes()); // version needed
            out.extend_from_slice(&0u16.to_le_bytes()); // flags
            out.extend_from_slice(&0u16.to_le_bytes()); // method = stored
            out.extend_from_slice(&0u16.to_le_bytes()); // mod time
            out.extend_from_slice(&0u16.to_le_bytes()); // mod date
            out.extend_from_slice(&crc.to_le_bytes());
            out.extend_from_slice(&(data.len() as u32).to_le_bytes()); // compressed size
            out.extend_from_slice(&(data.len() as u32).to_le_bytes()); // uncompressed size
            out.extend_from_slice(&(name_bytes.len() as u16).to_le_bytes());
            out.extend_from_slice(&0u16.to_le_bytes()); // extra field len
            out.extend_from_slice(name_bytes);
            out.extend_from_slice(data);

            // Central directory entry (needs the local header's offset, so
            // buffered separately and appended once all locals are written).
            central.extend_from_slice(&0x0201_4b50u32.to_le_bytes());
            central.extend_from_slice(&20u16.to_le_bytes()); // version made by
            central.extend_from_slice(&20u16.to_le_bytes()); // version needed
            central.extend_from_slice(&0u16.to_le_bytes()); // flags
            central.extend_from_slice(&0u16.to_le_bytes()); // method
            central.extend_from_slice(&0u16.to_le_bytes()); // mod time
            central.extend_from_slice(&0u16.to_le_bytes()); // mod date
            central.extend_from_slice(&crc.to_le_bytes());
            central.extend_from_slice(&(data.len() as u32).to_le_bytes());
            central.extend_from_slice(&(data.len() as u32).to_le_bytes());
            central.extend_from_slice(&(name_bytes.len() as u16).to_le_bytes());
            central.extend_from_slice(&0u16.to_le_bytes()); // extra len
            central.extend_from_slice(&0u16.to_le_bytes()); // comment len
            central.extend_from_slice(&0u16.to_le_bytes()); // disk number start
            central.extend_from_slice(&0u16.to_le_bytes()); // internal attrs
            central.extend_from_slice(&0u32.to_le_bytes()); // external attrs
            central.extend_from_slice(&offset.to_le_bytes());
            central.extend_from_slice(name_bytes);
        }

        let central_offset = out.len() as u32;
        let central_size = central.len() as u32;
        out.extend_from_slice(&central);

        // End of central directory record.
        out.extend_from_slice(&0x0605_4b50u32.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes()); // disk number
        out.extend_from_slice(&0u16.to_le_bytes()); // disk with central dir
        out.extend_from_slice(&(entries.len() as u16).to_le_bytes());
        out.extend_from_slice(&(entries.len() as u16).to_le_bytes());
        out.extend_from_slice(&central_size.to_le_bytes());
        out.extend_from_slice(&central_offset.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes()); // comment len

        out
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn crc32_matches_known_vector() {
            // "123456789" → 0xCBF43926 is the standard CRC-32 check value.
            assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
        }

        #[test]
        fn zip_has_valid_signatures_and_entry_count() {
            let entries = vec![
                ("a.txt".to_string(), b"hello".to_vec()),
                ("b.txt".to_string(), b"world!!".to_vec()),
            ];
            let zip = write_stored_zip(&entries);
            // Starts with a local file header signature.
            assert_eq!(&zip[0..4], &0x0403_4b50u32.to_le_bytes());
            // End-of-central-directory signature must appear somewhere near the end.
            let eocd_sig = 0x0605_4b50u32.to_le_bytes();
            assert!(zip.windows(4).any(|w| w == eocd_sig));
            // Entry count in EOCD must be findable in the buffer.
            let count_bytes = (entries.len() as u16).to_le_bytes();
            assert!(zip.windows(2).any(|w| w == count_bytes));
        }
    }
}

// ── POST /api/clipper/process-batch ─────────────────────────────────────────
//
// Ports the frontend's `handleProcessAll` start call: multiple staged sources,
// each with its own trim window, cut into fixed-length 9:16 clips. Modeled as
// ONE clip job (`kind = Clip`) whose `params` JSON carries the *plan*
// (per-source asset id / display name / trim window / planned clip count) —
// that plan is what `process_batch_status` reads back to reconstruct
// `clip`/`total`/`source` without a new repo function or in-memory map. Each
// segment produced is a normal Clip asset (parent_id = that source's asset
// id), created via the exact same `repo`/`media` calls `run_clip_job` uses —
// so a batch job's clips are indistinguishable from a single-source job's
// once done, and show up in `GET /api/clipper/jobs` the same way.

#[derive(Deserialize)]
struct BatchSource {
    path: String,
    trim_start: f64,
    trim_end: f64,
    original_name: String,
}

#[derive(Deserialize)]
struct ProcessBatchBody {
    #[serde(default = "default_project")]
    project: String,
    #[serde(default)]
    #[allow(dead_code)] // part of the contract; not needed once sources are staged
    batch_id: Option<String>,
    clip_length: f64,
    sources: Vec<BatchSource>,
}

/// One planned source's slice of the batch — persisted into the job's `params`
/// JSON so the status endpoint can reconstruct progress across polls (and
/// across a process restart, modulo the orphan-recovery sweep).
#[derive(serde::Serialize, serde::Deserialize, Clone)]
struct BatchPlanSource {
    source_asset_id: String,
    original_name: String,
    trim_start: f64,
    trim_end: f64,
    planned_clips: usize,
}

#[derive(serde::Serialize, serde::Deserialize, Clone)]
struct BatchPlan {
    clip_length: f64,
    sources: Vec<BatchPlanSource>,
}

fn clips_in_window(trim_start: f64, trim_end: f64, clip_length: f64) -> usize {
    if clip_length <= 0.0 {
        return 0;
    }
    let span = (trim_end - trim_start).max(0.0);
    (span / clip_length).floor() as usize
}

async fn process_batch_start(
    State(st): State<AppState>,
    Json(body): Json<ProcessBatchBody>,
) -> Result<Json<Value>, ApiError> {
    let sanitized = crate::paths::sanitize_project_name(&body.project)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;
    if body.sources.is_empty() {
        return Err(ApiError::BadRequest("sources[] is required".into()));
    }
    if body.clip_length <= 0.0 {
        return Err(ApiError::BadRequest("clip_length must be positive".into()));
    }

    repo::create_project(&st.db, &sanitized).await?;

    // Confine + register every source up front so a bad path fails the whole
    // request before any ffmpeg work starts (same fail-fast contract start_clip
    // uses for a single source).
    let data = data_dir();
    let mut plan_sources = Vec::with_capacity(body.sources.len());
    let mut abs_paths = Vec::with_capacity(body.sources.len());
    for src in &body.sources {
        // Uploaded sources (R2 / stage-streamed) hand back an ABSOLUTE path in
        // production; relativize it under the data dir before confining, and use
        // the relative form downstream so the stored asset path stays
        // project-relative (consistent with URL-download sources). An absolute
        // path that escapes the data dir is rejected here.
        let rel = crate::paths::relativize_under_data(&data, &src.path)
            .map_err(|e| ApiError::BadRequest(format!("invalid source path {}: {e}", src.path)))?;
        let abs = crate::paths::confine_under_data(&data, &rel)
            .map_err(|e| ApiError::BadRequest(format!("invalid source path {}: {e}", src.path)))?;
        if !abs.is_file() {
            return Err(ApiError::BadRequest(format!("source not found: {}", src.path)));
        }
        let planned = clips_in_window(src.trim_start, src.trim_end, body.clip_length);
        let meta = json!({ "source_path": rel, "original_name": src.original_name }).to_string();
        let source_asset =
            repo::create_asset(&st.db, &sanitized, clab_core::model::AssetKind::Source, None, Some(&meta))
                .await?;
        repo::mark_asset_ready(&st.db, &source_asset.id, &rel).await?;
        plan_sources.push(BatchPlanSource {
            source_asset_id: source_asset.id,
            original_name: src.original_name.clone(),
            trim_start: src.trim_start,
            trim_end: src.trim_end,
            planned_clips: planned,
        });
        abs_paths.push(abs);
    }

    let total_clips: usize = plan_sources.iter().map(|s| s.planned_clips).sum();
    let plan = BatchPlan {
        clip_length: body.clip_length,
        sources: plan_sources.clone(),
    };
    let params = serde_json::to_string(&plan).unwrap_or_else(|_| "{}".to_string());
    let job = repo::create_job(&st.db, &sanitized, clab_core::model::JobKind::Clip, None, Some(&params)).await?;

    let db = st.db.clone();
    let job_id = job.id.clone();
    let project = sanitized.clone();
    let clip_length = body.clip_length;
    tokio::spawn(async move {
        if let Err(e) =
            run_batch_clip_job(db.clone(), &job_id, &project, &plan_sources, &abs_paths, clip_length, total_clips)
                .await
        {
            tracing::error!("process-batch job {job_id} failed: {e:#}");
            let _ = repo::fail_job(&db, &job_id, &format!("{e:#}")).await;
        }
    });

    Ok(Json(json!({ "job_id": job.id, "total_clips": total_clips })))
}

/// Cut every planned segment of every source, in order. Reuses the same
/// asset/progress lifecycle `run_clip_job` (jobs.rs) uses per-clip — this is
/// the multi-source loop version, since `run_clip_job` itself only takes one
/// source and one job. A single source's ffmpeg failure does not abort the
/// rest of the batch (mirrors `run_clip_job`'s per-clip resilience).
#[allow(clippy::too_many_arguments)]
async fn run_batch_clip_job(
    db: clab_core::Db,
    job_id: &str,
    project: &str,
    sources: &[BatchPlanSource],
    abs_paths: &[PathBuf],
    clip_length: f64,
    total_clips: usize,
) -> anyhow::Result<()> {
    let data = data_dir();
    let out_dir = PathBuf::from(&data).join("projects").join(project).join("clips").join(job_id);
    tokio::fs::create_dir_all(&out_dir).await?;

    let mut produced = Vec::new();
    let mut done = 0usize;

    for (src, abs_path) in sources.iter().zip(abs_paths.iter()) {
        if src.planned_clips == 0 {
            continue;
        }
        let info = match media::probe(&abs_path.to_string_lossy()).await {
            Ok(i) => i,
            Err(e) => {
                tracing::warn!("process-batch: probe failed for {}: {e:#}", src.original_name);
                done += src.planned_clips;
                let _ = repo::update_job_progress(&db, job_id, done as f64 / total_clips.max(1) as f64).await;
                continue;
            }
        };

        for i in 0..src.planned_clips {
            let clip_start = src.trim_start + (i as f64) * clip_length;
            // Prefix by source asset id so filenames stay unique across sources.
            let out_name = format!("{}_{i:03}.mp4", &src.source_asset_id);
            let out_path = out_dir.join(&out_name);

            let meta = json!({
                "index": i,
                "source_id": src.source_asset_id,
                "source_name": src.original_name,
            })
            .to_string();
            let clip_asset = repo::create_asset(
                &db,
                project,
                clab_core::model::AssetKind::Clip,
                Some(&src.source_asset_id),
                Some(&meta),
            )
            .await?;
            repo::mark_asset_processing(&db, &clip_asset.id).await?;

            let result = media::ffmpeg::clip_segment(
                abs_path,
                &out_path,
                clip_start,
                clip_length,
                &info,
                |_frac| {}, // ponytail: per-ffmpeg-frame progress folded into the coarser per-clip counter below, not sub-clip granularity
            )
            .await;

            match result {
                Ok(()) => {
                    let rel = format!("projects/{project}/clips/{job_id}/{out_name}");
                    repo::mark_asset_ready(&db, &clip_asset.id, &rel).await?;
                    produced.push(clip_asset.id);
                }
                Err(e) => {
                    repo::mark_asset_failed(&db, &clip_asset.id, &format!("{e:#}")).await?;
                    tracing::warn!("process-batch: clip {i} of {} failed: {e:#}", src.original_name);
                }
            }
            done += 1;
            let _ = repo::update_job_progress(&db, job_id, done as f64 / total_clips.max(1) as f64).await;
        }
    }

    if produced.is_empty() && total_clips > 0 {
        repo::fail_job(&db, job_id, "all clips failed — see per-asset errors").await?;
        return Ok(());
    }

    repo::finish_job(&db, job_id, &produced).await?;
    Ok(())
}

// ── GET /api/clipper/process-batch/{job_id} ─────────────────────────────────
//
// Reshapes the durable `jobs` row (+ its produced Clip assets) into exactly
// what `handleProcessAll`'s poll loop expects: `clip`/`total` progress
// counters, the *current* source's display name, an overall `status`, and a
// `clips[]` array the client filters down to `ok === true` entries for
// display. The per-source plan lives in `job.params` (written by
// `process_batch_start`) so this needs no extra repo surface — just the
// existing `list_assets` to pull back whichever Clip assets this batch has
// produced so far (matched by `parent_id` against the plan's source ids).

async fn process_batch_status(
    State(st): State<AppState>,
    Path(job_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let job = repo::get_job(&st.db, &job_id).await?.ok_or(ApiError::NotFound)?;
    if job.kind != clab_core::model::JobKind::Clip {
        return Err(ApiError::NotFound);
    }

    let plan: Option<BatchPlan> = job.params.as_deref().and_then(|p| serde_json::from_str(p).ok());

    let status_str = match job.status {
        clab_core::model::JobStatus::Queued => "processing",
        clab_core::model::JobStatus::Processing => "processing",
        clab_core::model::JobStatus::Done => "complete",
        clab_core::model::JobStatus::Failed => "error",
    };

    let Some(plan) = plan else {
        // A clip job started via /api/jobs/clip (not this batch endpoint) has
        // no BatchPlan — report a degenerate-but-valid shape from result ids.
        let result_ids: Vec<String> = serde_json::from_str(&job.result_asset_ids).unwrap_or_default();
        return Ok(Json(json!({
            "job_id": job.id,
            "status": status_str,
            "clip": result_ids.len(),
            "total": result_ids.len(),
            "source": "",
            "clips": [],
            "ok_count": result_ids.len(),
            "error": job.error,
        })));
    };

    let total: usize = plan.sources.iter().map(|s| s.planned_clips).sum();
    let source_ids: std::collections::HashSet<&str> =
        plan.sources.iter().map(|s| s.source_asset_id.as_str()).collect();
    let names_by_id: std::collections::HashMap<&str, &str> = plan
        .sources
        .iter()
        .map(|s| (s.source_asset_id.as_str(), s.original_name.as_str()))
        .collect();

    let mut all_clips = repo::list_assets(&st.db, &job.project, Some(clab_core::model::AssetKind::Clip))
        .await?
        .into_iter()
        .filter(|a| a.parent_id.as_deref().is_some_and(|p| source_ids.contains(p)))
        .collect::<Vec<_>>();
    // list_assets returns newest-first; the client/creation order reads better oldest-first.
    all_clips.sort_by_key(|a| a.created_at);

    let clip_count = all_clips
        .iter()
        .filter(|a| matches!(a.status, clab_core::model::AssetStatus::Ready | clab_core::model::AssetStatus::Failed))
        .count();

    // "Current" source: the first one (in plan order) whose produced-clip
    // count hasn't reached its planned count yet. Empty string once the whole
    // batch is done, matching the frontend's `job.source || ''` fallback.
    let mut current_source = String::new();
    if status_str == "processing" {
        let mut counted: std::collections::HashMap<&str, usize> = std::collections::HashMap::new();
        for a in &all_clips {
            if let Some(pid) = a.parent_id.as_deref() {
                *counted.entry(pid).or_insert(0) += 1;
            }
        }
        for s in &plan.sources {
            let done = counted.get(s.source_asset_id.as_str()).copied().unwrap_or(0);
            if done < s.planned_clips {
                current_source = s.original_name.clone();
                break;
            }
        }
    }

    let clips_json: Vec<Value> = all_clips
        .iter()
        .map(|a| {
            let ok = matches!(a.status, clab_core::model::AssetStatus::Ready);
            let source_name = a.parent_id.as_deref().and_then(|p| names_by_id.get(p)).copied();
            if ok {
                let path = a.path.as_deref().unwrap_or_default();
                let name = FsPath::new(path)
                    .file_name()
                    .and_then(|f| f.to_str())
                    .unwrap_or("clip.mp4");
                let url = format!("/projects/{}/clips/{}/{name}", job.project, job.id);
                json!({
                    "index": all_clips.iter().position(|x| x.id == a.id).unwrap_or(0),
                    "name": name,
                    "source_name": source_name,
                    "ok": true,
                    "url": url,
                    "thumb_url": Value::Null,
                })
            } else {
                json!({
                    "source_name": source_name,
                    "ok": false,
                    "error": a.error,
                })
            }
        })
        .collect();

    let ok_count = all_clips
        .iter()
        .filter(|a| matches!(a.status, clab_core::model::AssetStatus::Ready))
        .count();

    Ok(Json(json!({
        "job_id": job.id,
        "status": status_str,
        "clip": clip_count,
        "total": total,
        "source": current_source,
        "clips": clips_json,
        "ok_count": ok_count,
        "error": job.error,
    })))
}

// ── POST /api/clipper/download-url ──────────────────────────────────────────
//
// Actually downloads the source video via yt-dlp and stages it under the
// project's clips/_staging/<batch_id>/ dir (same staging convention as
// stage-streamed / r2-upload-complete), so the client can hand the returned
// `path` straight to process-batch for trimming.

#[derive(Deserialize)]
struct DownloadUrlBody {
    #[serde(default = "default_project")]
    project: String,
    video_url: String,
}

async fn download_url(Json(body): Json<DownloadUrlBody>) -> Result<Json<Value>, ApiError> {
    let url = body.video_url.trim();
    if url.is_empty() {
        return Err(ApiError::BadRequest("video_url is required".into()));
    }
    // Only http(s) — refuse file://, and anything yt-dlp might interpret as a
    // local path/search query (SSRF / local-file-read guard).
    if !(url.starts_with("http://") || url.starts_with("https://")) {
        return Err(ApiError::BadRequest("video_url must be an http(s) URL".into()));
    }
    let sanitized = crate::paths::sanitize_project_name(&body.project)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;

    let batch_id = repo::new_id();
    let staging_dir = PathBuf::from(data_dir())
        .join("projects")
        .join(&sanitized)
        .join("clips")
        .join("_staging")
        .join(&batch_id);
    tokio::fs::create_dir_all(&staging_dir)
        .await
        .map_err(|e| ApiError::Other(anyhow::anyhow!("create staging dir: {e}")))?;

    let dest = staging_dir.join("src_000.mp4");
    ytdlp_download(url, &dest)
        .await
        .map_err(|e| ApiError::BadRequest(format!("could not download video_url: {e}")))?;

    let (duration, width, height) = probe_basic(&dest)
        .await
        .map_err(|e| ApiError::BadRequest(format!("downloaded file is not a readable video: {e}")))?;
    if duration <= 0.0 {
        let _ = tokio::fs::remove_file(&dest).await;
        return Err(ApiError::BadRequest(
            "downloaded video has no readable duration — likely corrupt or not a video".into(),
        ));
    }

    // Best-effort thumbnail; a failure here shouldn't fail the whole download.
    let thumb_path = staging_dir.join("src_000_thumb.jpg");
    let thumb_url = match ffmpeg_thumbnail(&dest, &thumb_path).await {
        Ok(()) => Some(format!(
            "/projects/{sanitized}/clips/_staging/{batch_id}/src_000_thumb.jpg"
        )),
        Err(e) => {
            tracing::warn!("download-url: thumbnail generation failed: {e}");
            None
        }
    };

    let original_name = url
        .rsplit('/')
        .next()
        .filter(|s| !s.is_empty())
        .unwrap_or("video.mp4")
        .to_string();

    Ok(Json(json!({
        "batch_id": batch_id,
        "files": [{
            "index": 0,
            "original_name": original_name,
            "path": format!("projects/{sanitized}/clips/_staging/{batch_id}/src_000.mp4"),
            "url": format!("/projects/{sanitized}/clips/_staging/{batch_id}/src_000.mp4"),
            "thumb_url": thumb_url,
            "duration": duration,
            "width": width,
            "height": height,
        }],
    })))
}

/// Run `yt-dlp -o <dest> <url>` to actually pull the video down (as opposed to
/// `ytdlp_dump_json`'s metadata-only probe). Re-muxes to mp4 so downstream
/// ffprobe/ffmpeg always see a consistent container.
async fn ytdlp_download(url: &str, dest: &FsPath) -> anyhow::Result<()> {
    use anyhow::Context;
    use tokio::process::Command;

    let mut args: Vec<String> = vec![
        "--no-warnings".into(),
        "--no-check-certificates".into(),
        "--no-playlist".into(),
        "--merge-output-format".into(),
        "mp4".into(),
        "-o".into(),
        dest.to_string_lossy().into_owned(),
    ];
    if let Some(cookies) = cookies_path() {
        args.push("--cookies".into());
        args.push(cookies.to_string_lossy().into_owned());
    }
    args.push(url.to_string());

    let child = Command::new("yt-dlp")
        .args(&args)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .context("yt-dlp not found on PATH")?;
    // Downloads can legitimately take a while — much longer than the metadata
    // probe's 30s budget, but still bounded so a wedged process can't hang a
    // request forever.
    let out = tokio::time::timeout(Duration::from_secs(600), child.wait_with_output())
        .await
        .map_err(|_| anyhow::anyhow!("yt-dlp download timed out after 600s"))?
        .context("yt-dlp wait failed")?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        anyhow::bail!("yt-dlp failed: {}", tail(err.trim(), 300));
    }
    if !dest.is_file() {
        anyhow::bail!("yt-dlp reported success but produced no output file");
    }
    Ok(())
}

/// A single-frame ffmpeg thumbnail (2s in, or the first frame for very short
/// clips) — cheaper than a full yt-dlp cover fetch and works for any source.
async fn ffmpeg_thumbnail(src: &FsPath, dest: &FsPath) -> anyhow::Result<()> {
    use anyhow::Context;
    use tokio::process::Command;

    let out = Command::new("ffmpeg")
        .args(["-y", "-ss", "2", "-i"])
        .arg(src)
        .args(["-frames:v", "1", "-q:v", "4"])
        .arg(dest)
        .output()
        .await
        .context("failed to spawn ffmpeg for thumbnail")?;
    if !out.status.success() || !dest.is_file() {
        // Retry at t=0 — a 2s seek can fail on very short clips.
        let out0 = Command::new("ffmpeg")
            .args(["-y", "-i"])
            .arg(src)
            .args(["-frames:v", "1", "-q:v", "4"])
            .arg(dest)
            .output()
            .await
            .context("failed to spawn ffmpeg for thumbnail (retry)")?;
        if !out0.status.success() || !dest.is_file() {
            anyhow::bail!(
                "ffmpeg thumbnail failed: {}",
                String::from_utf8_lossy(&out.stderr).trim()
            );
        }
    }
    Ok(())
}

/// Last `n` characters of a string (char-boundary safe) — same convention
/// `media::ytdlp` uses for truncating stderr in error messages.
fn tail(s: &str, n: usize) -> &str {
    let count = s.chars().count();
    if count <= n {
        return s;
    }
    let skip = count - n;
    let (idx, _) = s.char_indices().nth(skip).unwrap_or((0, ' '));
    &s[idx..]
}

// ── yt-dlp cookies management ───────────────────────────────────────────────
//
// Same volume convention as the Python router: <RAILWAY_VOLUME_MOUNT_PATH or
// data_dir>/cookies.txt. This is a plain file, not a DB row, matching exactly
// how yt-dlp itself is invoked (both here and by the captions workstream's
// `media::ytdlp`, which resolves the same volume path independently).

fn cookies_path() -> Option<PathBuf> {
    // Same precedence chain as `media::ytdlp::cookies_path` (env override,
    // then the volume file, then cwd) — duplicated intentionally rather than
    // reaching into that crate's internals, since `media` is owned by another
    // workstream this wave.
    if let Ok(explicit) = std::env::var("YTDLP_COOKIES_FILE") {
        let p = PathBuf::from(explicit);
        if p.exists() {
            return Some(p);
        }
    }
    let volume = cookies_volume_path();
    if volume.exists() {
        return Some(volume);
    }
    let cwd = PathBuf::from("cookies.txt");
    if cwd.exists() {
        return Some(cwd);
    }
    None
}

fn cookies_volume_path() -> PathBuf {
    PathBuf::from(data_dir()).join("cookies.txt")
}

async fn cookies_status() -> Json<Value> {
    let volume_path = cookies_volume_path();
    let path = cookies_path();
    let source = path.as_ref().map(|p| {
        if std::env::var("YTDLP_COOKIES_FILE").ok().as_deref() == Some(p.to_string_lossy().as_ref()) {
            "env:YTDLP_COOKIES_FILE"
        } else if *p == volume_path {
            "volume"
        } else {
            "cwd"
        }
    });
    let size_bytes = match &path {
        Some(p) => tokio::fs::metadata(p).await.map(|m| m.len()).unwrap_or(0),
        None => 0,
    };
    let volume_writable = volume_path.parent().map(|p| p.exists()).unwrap_or(false);
    Json(json!({
        "configured": path.is_some(),
        "source": source,
        "path": path.map(|p| p.to_string_lossy().into_owned()),
        "size_bytes": size_bytes,
        "volume_path": volume_path.to_string_lossy(),
        "volume_writable": volume_writable,
    }))
}

/// Upload a Netscape-format cookies.txt to the volume (multipart, field `file`
/// — matches the Python `UploadFile = File(...)` contract).
///
/// Hand-rolled multipart parsing rather than `axum::extract::Multipart`: that
/// extractor is gated behind axum's `multipart` cargo feature, which isn't
/// enabled on this workspace's `axum` dependency (api/Cargo.toml is owned by
/// another workstream this wave — see seam request in the final report). A
/// single-field, non-chunked parse of a <2MB body is simple enough to do by
/// hand rather than block on that.
async fn cookies_upload(
    headers: axum::http::HeaderMap,
    body: axum::body::Bytes,
) -> Result<Json<Value>, ApiError> {
    let content_type = headers
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or_default();
    let boundary = multipart_boundary(content_type)
        .ok_or_else(|| ApiError::BadRequest("expected multipart/form-data with a boundary".into()))?;
    let raw = extract_multipart_field(&body, &boundary, "file")
        .ok_or_else(|| ApiError::BadRequest("Empty file".into()))?;
    if raw.is_empty() {
        return Err(ApiError::BadRequest("Empty file".into()));
    }
    const MAX_COOKIES_BYTES: usize = 2 * 1024 * 1024;
    if raw.len() > MAX_COOKIES_BYTES {
        return Err(ApiError::BadRequest("File too large (max 2MB)".into()));
    }
    let text = String::from_utf8(raw.clone())
        .map_err(|_| ApiError::BadRequest("File must be UTF-8 text (Netscape cookies.txt)".into()))?;
    if !text.contains('\t') && !text.trim_start().starts_with('#') {
        return Err(ApiError::BadRequest(
            "Does not look like a Netscape cookies.txt file".into(),
        ));
    }

    let dest = cookies_volume_path();
    if let Some(parent) = dest.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .map_err(|e| ApiError::Other(anyhow::anyhow!("create volume dir: {e}")))?;
    }
    let tmp = dest.with_extension("txt.tmp");
    tokio::fs::write(&tmp, &raw)
        .await
        .map_err(|e| ApiError::Other(anyhow::anyhow!("write cookies temp file: {e}")))?;
    tokio::fs::rename(&tmp, &dest)
        .await
        .map_err(|e| ApiError::Other(anyhow::anyhow!("finalize cookies file: {e}")))?;

    Ok(Json(json!({ "ok": true, "path": dest.to_string_lossy(), "size_bytes": raw.len() })))
}

/// Pull the `boundary=` parameter out of a `multipart/form-data; boundary=...`
/// Content-Type header value.
fn multipart_boundary(content_type: &str) -> Option<String> {
    if !content_type.starts_with("multipart/form-data") {
        return None;
    }
    content_type.split(';').find_map(|part| {
        let part = part.trim();
        part.strip_prefix("boundary=").map(|b| b.trim_matches('"').to_string())
    })
}

/// Extract one named field's raw bytes from a (small, non-chunked) multipart
/// body. Scans for `--{boundary}` delimited parts, matches
/// `Content-Disposition: form-data; name="{field}"`, and returns everything
/// between that part's header/body blank-line separator and the next
/// boundary marker (trimming the trailing CRLF the spec requires there).
/// Good enough for a single small file field — not a general MIME parser.
fn extract_multipart_field(body: &[u8], boundary: &str, field: &str) -> Option<Vec<u8>> {
    let delim = format!("--{boundary}").into_bytes();
    let mut pos = 0usize;
    while let Some(start) = find_bytes(&body[pos..], &delim) {
        let part_start = pos + start + delim.len();
        // Boundary immediately followed by "--" marks the terminal delimiter.
        if body[part_start..].starts_with(b"--") {
            break;
        }
        // Header/body split is the first blank line (CRLFCRLF, tolerate LFLF).
        let header_end = find_bytes(&body[part_start..], b"\r\n\r\n")
            .map(|i| (part_start + i, 4))
            .or_else(|| find_bytes(&body[part_start..], b"\n\n").map(|i| (part_start + i, 2)));
        let Some((header_end, sep_len)) = header_end else {
            break;
        };
        let headers = String::from_utf8_lossy(&body[part_start..header_end]);
        let is_target = headers
            .to_ascii_lowercase()
            .contains(&format!("name=\"{field}\""));
        let body_start = header_end + sep_len;
        let next = find_bytes(&body[body_start..], &delim).map(|i| body_start + i);
        let body_end = next.unwrap_or(body.len());
        // Strip the trailing CRLF that precedes the next boundary marker.
        let mut end = body_end;
        if end >= 2 && &body[end - 2..end] == b"\r\n" {
            end -= 2;
        } else if end >= 1 && body[end - 1] == b'\n' {
            end -= 1;
        }
        if is_target {
            return Some(body[body_start..end].to_vec());
        }
        match next {
            Some(n) => pos = n,
            None => break,
        }
    }
    None
}

fn find_bytes(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|w| w == needle)
}

async fn cookies_delete() -> Result<Json<Value>, ApiError> {
    let dest = cookies_volume_path();
    let existed = dest.exists();
    if existed {
        tokio::fs::remove_file(&dest)
            .await
            .map_err(|e| ApiError::Other(anyhow::anyhow!("delete cookies file: {e}")))?;
    }
    Ok(Json(json!({ "ok": true, "deleted": existed })))
}

// ── R2 upload handshake ──────────────────────────────────────────────────────

#[derive(Deserialize)]
struct R2UploadInitFile {
    filename: Option<String>,
    content_type: Option<String>,
}

#[derive(Deserialize)]
struct R2UploadInitBody {
    #[serde(default = "default_project")]
    project: String,
    files: Vec<R2UploadInitFile>,
}

async fn r2_upload_init(Json(body): Json<R2UploadInitBody>) -> Result<Json<Value>, ApiError> {
    let r2 = integrations::r2::R2::from_env()
        .ok_or_else(|| ApiError::Other(anyhow::anyhow!("R2 storage is not configured on this deployment")))?;
    if body.files.is_empty() {
        return Err(ApiError::BadRequest("files[] is required".into()));
    }
    if body.files.len() > 20 {
        return Err(ApiError::BadRequest("max 20 files per batch".into()));
    }
    let sanitized = crate::paths::sanitize_project_name(&body.project)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;
    let batch_id = repo::new_id();
    let expires_at = chrono::Utc::now().timestamp() + integrations::r2::UPLOAD_TTL.as_secs() as i64;

    let mut items = Vec::with_capacity(body.files.len());
    for (i, f) in body.files.iter().enumerate() {
        let filename = f.filename.clone().unwrap_or_else(|| format!("video_{i}.mp4"));
        let content_type = f.content_type.clone().unwrap_or_else(|| "video/mp4".to_string());
        let key = integrations::r2::upload_key(&sanitized, &batch_id, i, &filename);
        let put_url = r2
            .presign_put(&key, &content_type, integrations::r2::UPLOAD_TTL)
            .await
            .map_err(ApiError::Other)?;
        items.push(json!({
            "index": i,
            "filename": filename,
            "key": key,
            "put_url": put_url,
        }));
    }

    Ok(Json(json!({ "batch_id": batch_id, "items": items, "expires_at": expires_at })))
}

#[derive(Deserialize)]
struct R2UploadCompleteItem {
    #[serde(default)]
    index: usize,
    filename: Option<String>,
    key: Option<String>,
}

#[derive(Deserialize)]
struct R2UploadCompleteBody {
    #[serde(default = "default_project")]
    project: String,
    batch_id: String,
    items: Vec<R2UploadCompleteItem>,
    expires_at: Option<i64>,
}

async fn r2_upload_complete(Json(body): Json<R2UploadCompleteBody>) -> Result<Json<Value>, ApiError> {
    let r2 = integrations::r2::R2::from_env()
        .ok_or_else(|| ApiError::Other(anyhow::anyhow!("R2 storage is not configured on this deployment")))?;
    if body.batch_id.trim().is_empty() {
        return Err(ApiError::BadRequest("batch_id is required".into()));
    }
    if body.items.is_empty() {
        return Err(ApiError::BadRequest("items[] is required".into()));
    }
    if let Some(deadline) = body.expires_at {
        if deadline <= chrono::Utc::now().timestamp() {
            return Err(ApiError::BadRequest(
                "upload session expired — re-initialize the upload (call /r2/upload-init again)".into(),
            ));
        }
    }

    let sanitized = crate::paths::sanitize_project_name(&body.project)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;
    let staging_dir = PathBuf::from(data_dir())
        .join("projects")
        .join(&sanitized)
        .join("clips")
        .join(format!("_staging_{}", body.batch_id));
    tokio::fs::create_dir_all(&staging_dir)
        .await
        .map_err(|e| ApiError::Other(anyhow::anyhow!("create staging dir: {e}")))?;

    let mut files_out = Vec::new();
    let mut errors = Vec::new();

    for item in &body.items {
        let filename = item.filename.clone().unwrap_or_else(|| format!("video_{}.mp4", item.index));
        let Some(key) = item.key.as_deref().filter(|k| !k.is_empty()) else {
            errors.push(json!({ "filename": filename, "error": "missing R2 key" }));
            continue;
        };

        let safe_name = format!("src_{:03}.mp4", item.index);
        let dest = staging_dir.join(&safe_name);

        let written = match r2.download_to_path(key, &dest).await {
            Ok(n) => n,
            Err(e) => {
                errors.push(json!({ "filename": filename, "error": format!("R2 fetch failed: {e}") }));
                continue;
            }
        };
        if written == 0 {
            let _ = tokio::fs::remove_file(&dest).await;
            errors.push(json!({ "filename": filename, "error": "uploaded file is empty" }));
            continue;
        }

        // NB: non-mp4 transcode + rotation-aware probing is the pipeline's job
        // (media crate, owned elsewhere this wave). We still report a
        // best-effort ffprobe read here so the response shape is complete; a
        // probe failure surfaces as a per-item error, same as Python's
        // `_get_video_info` failure path.
        match probe_basic(&dest).await {
            Ok((duration, width, height)) if duration > 0.0 => {
                files_out.push(json!({
                    "index": item.index,
                    "original_name": filename,
                    "path": dest.to_string_lossy(),
                    "url": format!("/projects/{sanitized}/clips/_staging_{}/{safe_name}", body.batch_id),
                    "thumb_url": "",
                    "duration": duration,
                    "width": width,
                    "height": height,
                    "r2_key": key,
                }));
            }
            Ok(_) => {
                let _ = tokio::fs::remove_file(&dest).await;
                errors.push(json!({ "filename": filename, "error": "zero-duration video" }));
            }
            Err(e) => {
                let _ = tokio::fs::remove_file(&dest).await;
                errors.push(json!({ "filename": filename, "error": format!("probe failed: {e}") }));
            }
        }
    }

    if files_out.is_empty() && !errors.is_empty() {
        return Err(ApiError::BadRequest(format!(
            "All {} files failed: {}",
            errors.len(),
            Value::Array(errors)
        )));
    }

    Ok(Json(json!({ "batch_id": body.batch_id, "files": files_out, "errors": errors })))
}

// ── stage-streamed ───────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct StageStreamedQuery {
    #[serde(default = "default_project")]
    project: String,
    batch_id: Option<String>,
    #[serde(default = "default_filename")]
    filename: String,
    #[serde(default)]
    index: usize,
}

fn default_filename() -> String {
    "video.mp4".to_string()
}

/// Same order-of-magnitude cap as the Python route's streamed-upload guard.
const MAX_STAGE_UPLOAD_BYTES: u64 = 5 * 1024 * 1024 * 1024; // 5 GiB

async fn stage_streamed(
    Query(q): Query<StageStreamedQuery>,
    body: axum::body::Body,
) -> Result<Json<Value>, ApiError> {
    use futures::StreamExt;
    use tokio::io::AsyncWriteExt;

    let sanitized = crate::paths::sanitize_project_name(&q.project)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;
    let batch_id = q.batch_id.unwrap_or_else(repo::new_id);

    let staging_dir = PathBuf::from(data_dir())
        .join("projects")
        .join(&sanitized)
        .join("clips")
        .join(format!("_staging_{batch_id}"));
    tokio::fs::create_dir_all(&staging_dir)
        .await
        .map_err(|e| ApiError::Other(anyhow::anyhow!("create staging dir: {e}")))?;

    let safe_name = format!("src_{:03}.mp4", q.index);
    let dest = staging_dir.join(&safe_name);

    // Stream the request body straight to disk, enforcing the size cap as we go —
    // never buffer the (multi-GB) upload in memory. main.rs's global 100MB
    // DefaultBodyLimit is DISABLED for this route (see router()) so large uploads
    // reach us; the running-total check below is the real bound.
    let mut file = tokio::fs::File::create(&dest)
        .await
        .map_err(|e| ApiError::Other(anyhow::anyhow!("create staged file: {e}")))?;
    let mut stream = body.into_data_stream();
    let mut total: u64 = 0;
    while let Some(chunk) = stream.next().await {
        let chunk = chunk
            .map_err(|e| ApiError::BadRequest(format!("{}: upload stream error: {e}", q.filename)))?;
        total += chunk.len() as u64;
        if total > MAX_STAGE_UPLOAD_BYTES {
            drop(file);
            let _ = tokio::fs::remove_file(&dest).await;
            return Err(ApiError::BadRequest(format!(
                "File exceeds maximum size of {}GB",
                MAX_STAGE_UPLOAD_BYTES / (1024 * 1024 * 1024)
            )));
        }
        file.write_all(&chunk)
            .await
            .map_err(|e| ApiError::Other(anyhow::anyhow!("write staged upload: {e}")))?;
    }
    file.flush()
        .await
        .map_err(|e| ApiError::Other(anyhow::anyhow!("flush staged upload: {e}")))?;
    drop(file);

    if total == 0 {
        let _ = tokio::fs::remove_file(&dest).await;
        return Err(ApiError::BadRequest(format!("{}: uploaded file is empty (0 bytes)", q.filename)));
    }

    let (duration, width, height) = probe_basic(&dest)
        .await
        .map_err(|e| ApiError::BadRequest(format!("{}: probe failed: {e}", q.filename)))?;
    if duration <= 0.0 {
        let _ = tokio::fs::remove_file(&dest).await;
        return Err(ApiError::BadRequest(format!(
            "{}: could not determine video duration",
            q.filename
        )));
    }

    Ok(Json(json!({
        "batch_id": batch_id,
        "file": {
            "index": q.index,
            "original_name": q.filename,
            "path": dest.to_string_lossy(),
            "url": format!("/projects/{sanitized}/clips/_staging_{batch_id}/{safe_name}"),
            "thumb_url": "",
            "duration": duration,
            "width": width,
            "height": height,
        },
    })))
}

/// Minimal ffprobe read (duration + dimensions), local to this file so
/// clipper.rs doesn't need write access to the `media` crate (owned by the
/// captions workstream this wave). Intentionally simpler than
/// `media::probe::probe` (no rotation handling) — good enough for staging
/// metadata; the wave-3 runner does the authoritative probe before clipping.
async fn probe_basic(path: &FsPath) -> anyhow::Result<(f64, u32, u32)> {
    use anyhow::Context;
    use tokio::process::Command;

    let out = Command::new("ffprobe")
        .args([
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=0",
        ])
        .arg(path)
        .output()
        .await
        .context("failed to spawn ffprobe")?;
    if !out.status.success() {
        anyhow::bail!("ffprobe failed: {}", String::from_utf8_lossy(&out.stderr).trim());
    }
    let text = String::from_utf8_lossy(&out.stdout);
    let mut width = 0u32;
    let mut height = 0u32;
    let mut duration = 0.0f64;
    for line in text.lines() {
        if let Some((k, v)) = line.split_once('=') {
            match k.trim() {
                "width" => width = v.trim().parse().unwrap_or(0),
                "height" => height = v.trim().parse().unwrap_or(0),
                "duration" => duration = v.trim().parse().unwrap_or(0.0),
                _ => {}
            }
        }
    }
    Ok((duration, width, height))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clips_in_window_floors_the_span() {
        assert_eq!(clips_in_window(0.0, 33.0, 10.0), 3);
        assert_eq!(clips_in_window(5.0, 12.0, 10.0), 0);
        assert_eq!(clips_in_window(0.0, 10.0, 10.0), 1);
        // degenerate / inverted windows never panic or go negative.
        assert_eq!(clips_in_window(10.0, 5.0, 10.0), 0);
        assert_eq!(clips_in_window(0.0, 100.0, 0.0), 0);
    }

    #[test]
    fn clip_json_shape_matches_python() {
        let asset = clab_core::model::Asset {
            id: "a1".into(),
            project: "p".into(),
            kind: clab_core::model::AssetKind::Clip,
            status: clab_core::model::AssetStatus::Ready,
            path: Some("projects/p/clips/job1/clip_000.mp4".into()),
            parent_id: None,
            meta: None,
            error: None,
            created_at: chrono::Utc::now(),
            updated_at: chrono::Utc::now(),
        };
        let v = clip_json("p", "job1", &asset);
        assert_eq!(v["name"], "clip_000.mp4");
        assert_eq!(v["url"], "/projects/p/clips/job1/clip_000.mp4");
        // no thumbnail on disk in this test env → null, not an error
        assert!(v["thumb_url"].is_null());
    }

    #[test]
    fn default_project_is_quick_test() {
        assert_eq!(default_project(), "quick-test");
    }

    #[test]
    fn tail_is_char_boundary_safe() {
        assert_eq!(tail("hello", 300), "hello");
        assert_eq!(tail("hello", 3), "llo");
    }

    #[test]
    fn multipart_boundary_parses_content_type() {
        assert_eq!(
            multipart_boundary("multipart/form-data; boundary=X-Y-Z"),
            Some("X-Y-Z".to_string())
        );
        assert_eq!(
            multipart_boundary("multipart/form-data; boundary=\"quoted\""),
            Some("quoted".to_string())
        );
        assert_eq!(multipart_boundary("application/json"), None);
    }

    #[test]
    fn extract_multipart_field_reads_file_part() {
        let boundary = "BOUNDARY123";
        let body = format!(
            "--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"cookies.txt\"\r\nContent-Type: text/plain\r\n\r\n# Netscape HTTP Cookie File\nfoo\tbar\r\n--{b}--\r\n",
            b = boundary
        );
        let extracted = extract_multipart_field(body.as_bytes(), boundary, "file").unwrap();
        assert_eq!(
            String::from_utf8(extracted).unwrap(),
            "# Netscape HTTP Cookie File\nfoo\tbar"
        );
    }

    #[test]
    fn extract_multipart_field_ignores_other_fields() {
        let boundary = "B";
        let body = format!(
            "--{b}\r\nContent-Disposition: form-data; name=\"other\"\r\n\r\nnope\r\n--{b}\r\nContent-Disposition: form-data; name=\"file\"\r\n\r\nyes-this-one\r\n--{b}--\r\n",
            b = boundary
        );
        let extracted = extract_multipart_field(body.as_bytes(), boundary, "file").unwrap();
        assert_eq!(String::from_utf8(extracted).unwrap(), "yes-this-one");
    }

    #[test]
    fn extract_multipart_field_missing_returns_none() {
        let boundary = "B";
        let body = format!("--{b}\r\nContent-Disposition: form-data; name=\"other\"\r\n\r\nnope\r\n--{b}--\r\n", b = boundary);
        assert!(extract_multipart_field(body.as_bytes(), boundary, "file").is_none());
    }

    // ── process-batch end-to-end (real ffmpeg/ffprobe) ──────────────────────

    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use tower::ServiceExt;

    // These tests mutate the process-global RAILWAY_VOLUME_MOUNT_PATH env var;
    // the crate-wide guard serializes them against every other env-mutating test
    // (per-file locks don't serialize across files in one test binary).
    use crate::testlock::ENV_LOCK;

    /// A throwaway data dir under the OS tempdir, cleaned up on drop. No
    /// `tempfile` crate dependency in this crate — same pattern routes/projects.rs
    /// uses for its throwaway sqlite file.
    struct TempDataDir(PathBuf);
    impl Drop for TempDataDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    async fn test_state_with_data_dir() -> (AppState, TempDataDir) {
        let dir = std::env::temp_dir().join(format!("clipper-test-data-{}", uuid::Uuid::new_v4()));
        tokio::fs::create_dir_all(&dir).await.unwrap();
        // Same env-var convention `data_dir()` reads everywhere in this file —
        // point it at a throwaway dir for the duration of this test.
        std::env::set_var("RAILWAY_VOLUME_MOUNT_PATH", &dir);

        let db_path = std::env::temp_dir().join(format!("clipper-test-{}.db", uuid::Uuid::new_v4()));
        let db = clab_core::Db::connect(&format!("sqlite://{}", db_path.display()))
            .await
            .unwrap();
        (AppState::new(db, None), TempDataDir(dir))
    }

    /// Generate a tiny (2s, 320x568 ~9:16) source video with real ffmpeg so the
    /// batch pipeline's probe/clip_segment calls run against a genuine file
    /// rather than a mock.
    async fn make_test_source(dir: &FsPath, name: &str, secs: u32) -> PathBuf {
        let path = dir.join(name);
        tokio::fs::create_dir_all(dir).await.unwrap();
        let out = tokio::process::Command::new("ffmpeg")
            .args(["-y", "-f", "lavfi", "-i"])
            .arg(format!("color=c=blue:s=320x568:d={secs}"))
            .args(["-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", &secs.to_string()])
            .arg(&path)
            .output()
            .await
            .expect("failed to spawn ffmpeg for test fixture");
        assert!(out.status.success(), "ffmpeg fixture failed: {}", String::from_utf8_lossy(&out.stderr));
        path
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

    fn json_req(method: &str, uri: &str, body: Value) -> Request<Body> {
        Request::builder()
            .method(method)
            .uri(uri)
            .header("content-type", "application/json")
            .body(Body::from(body.to_string()))
            .unwrap()
    }

    fn get_req(uri: &str) -> Request<Body> {
        Request::builder().uri(uri).body(Body::empty()).unwrap()
    }

    #[tokio::test]
    async fn process_batch_cuts_clips_and_status_reshapes_to_contract() {
        let _guard = ENV_LOCK.lock().await;
        let (st, data_dir) = test_state_with_data_dir().await;

        // Stage a 6s source directly under the project's clips dir (as if
        // download-url / stage-streamed had already run) — 6s at 2s/clip = 3 clips.
        let project = "batchtest";
        let staging_rel = format!("projects/{project}/clips/_staging/b1");
        let staging_abs = data_dir.0.join(&staging_rel);
        make_test_source(&staging_abs, "src_000.mp4", 6).await;
        let source_rel = format!("{staging_rel}/src_000.mp4");

        let (status, start) = call(
            &st,
            json_req(
                "POST",
                "/api/clipper/process-batch",
                json!({
                    "project": project,
                    "batch_id": "b1",
                    "clip_length": 2.0,
                    "sources": [{
                        "path": source_rel,
                        "trim_start": 0.0,
                        "trim_end": 6.0,
                        "original_name": "my_video.mp4",
                    }],
                }),
            ),
        )
        .await;
        assert_eq!(status, StatusCode::OK, "start response: {start}");
        assert_eq!(start["total_clips"], 3);
        let job_id = start["job_id"].as_str().unwrap().to_string();

        // Poll until complete (bounded so a real bug fails the test instead of hanging).
        let mut last = Value::Null;
        for _ in 0..100 {
            let (status, poll) = call(&st, get_req(&format!("/api/clipper/process-batch/{job_id}"))).await;
            assert_eq!(status, StatusCode::OK);
            last = poll.clone();
            if poll["status"] == "complete" || poll["status"] == "error" {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        }

        assert_eq!(last["status"], "complete", "job never completed: {last}");
        assert_eq!(last["total"], 3);
        assert_eq!(last["clip"], 3);
        assert_eq!(last["ok_count"], 3);
        assert_eq!(last["source"], "", "source clears once the batch is done");

        let clips = last["clips"].as_array().unwrap();
        assert_eq!(clips.len(), 3);
        for c in clips {
            assert_eq!(c["ok"], true);
            assert!(c["thumb_url"].is_null() || c["thumb_url"].as_str().is_some());
            let url = c["url"].as_str().expect("ok clip must carry a url");
            assert!(url.starts_with(&format!("/projects/{project}/clips/{job_id}/")));
        }

        std::env::remove_var("RAILWAY_VOLUME_MOUNT_PATH");
    }

    #[tokio::test]
    async fn process_batch_rejects_path_escaping_data_dir() {
        let _guard = ENV_LOCK.lock().await;
        let (st, _data_dir) = test_state_with_data_dir().await;

        let (status, body) = call(
            &st,
            json_req(
                "POST",
                "/api/clipper/process-batch",
                json!({
                    "project": "p",
                    "batch_id": "b1",
                    "clip_length": 2.0,
                    "sources": [{
                        "path": "../../etc/passwd",
                        "trim_start": 0.0,
                        "trim_end": 6.0,
                        "original_name": "evil.mp4",
                    }],
                }),
            ),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "body: {body}");

        std::env::remove_var("RAILWAY_VOLUME_MOUNT_PATH");
    }
}
