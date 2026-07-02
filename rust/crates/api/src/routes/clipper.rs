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
        .route("/api/clipper/process-batch/{job_id}", get(process_batch_status))
        .route("/api/clipper/download-url", post(download_url))
        .route("/api/clipper/cookies", post(cookies_upload).delete(cookies_delete))
        .route("/api/clipper/cookies/status", get(cookies_status))
        .route("/api/clipper/r2/upload-init", post(r2_upload_init))
        .route("/api/clipper/r2/upload-complete", post(r2_upload_complete))
        .route("/api/clipper/stage-streamed", post(stage_streamed))
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

// ── GET /api/clipper/process-batch/{job_id} ─────────────────────────────────
//
// The Python in-memory `_batch_jobs` map has no equivalent durable store of
// its own here — durable clip-job state IS the `jobs` table. This endpoint
// reports the same shape (`status`, `clip`/`total` progress counters, `clips`,
// `error`) by reading the `jobs` row: `clip`/`total` are derived from however
// many result assets exist so far (the wave-3 runner owns the authoritative
// planned total; until then this reports what's actually been produced).

async fn process_batch_status(
    State(st): State<AppState>,
    Path(job_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let job = repo::get_job(&st.db, &job_id).await?.ok_or(ApiError::NotFound)?;
    if job.kind != clab_core::model::JobKind::Clip {
        return Err(ApiError::NotFound);
    }
    let result_ids: Vec<String> = serde_json::from_str(&job.result_asset_ids).unwrap_or_default();
    let status = match job.status {
        clab_core::model::JobStatus::Queued => "running",
        clab_core::model::JobStatus::Processing => "running",
        clab_core::model::JobStatus::Done => "done",
        clab_core::model::JobStatus::Failed => "error",
    };
    Ok(Json(json!({
        "job_id": job.id,
        "status": status,
        "clip": result_ids.len(),
        "total": result_ids.len(),
        "progress": job.progress,
        "clips": result_ids,
        "ok_count": result_ids.len(),
        "error": job.error,
    })))
}

// ── POST /api/clipper/download-url ──────────────────────────────────────────
//
// Deliberately NOT a full download (that's the runner/upload flow's job) —
// this resolves/validates the URL via yt-dlp's `--dump-json` metadata probe
// only, so the client can show a preview before committing to a full pull.

#[derive(Deserialize)]
struct DownloadUrlBody {
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

    let meta = ytdlp_dump_json(url)
        .await
        .map_err(|e| ApiError::BadRequest(format!("could not resolve video_url: {e}")))?;

    Ok(Json(json!({
        "video_url": url,
        "title": meta.title,
        "duration": meta.duration,
        "width": meta.width,
        "height": meta.height,
        "thumbnail": meta.thumbnail,
        "ext": meta.ext,
    })))
}

struct YtdlpMeta {
    title: Option<String>,
    duration: Option<f64>,
    width: Option<u32>,
    height: Option<u32>,
    thumbnail: Option<String>,
    ext: Option<String>,
}

/// Run `yt-dlp --dump-json --no-download <url>` and parse out the fields we
/// surface. Metadata only — never triggers a video download.
async fn ytdlp_dump_json(url: &str) -> anyhow::Result<YtdlpMeta> {
    use anyhow::Context;
    use tokio::process::Command;

    let mut args: Vec<String> = vec![
        "--no-download".into(),
        "--no-warnings".into(),
        "--no-check-certificates".into(),
        "--dump-json".into(),
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
    let out = tokio::time::timeout(Duration::from_secs(30), child.wait_with_output())
        .await
        .map_err(|_| anyhow::anyhow!("yt-dlp metadata lookup timed out after 30s"))?
        .context("yt-dlp wait failed")?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        anyhow::bail!("yt-dlp failed: {}", tail(err.trim(), 300));
    }
    let stdout = String::from_utf8_lossy(&out.stdout);
    // --dump-json prints one JSON object (first line is enough for our fields).
    let first_line = stdout.lines().next().unwrap_or("");
    let v: Value = serde_json::from_str(first_line).context("yt-dlp returned non-JSON output")?;
    Ok(YtdlpMeta {
        title: v.get("title").and_then(Value::as_str).map(str::to_string),
        duration: v.get("duration").and_then(Value::as_f64),
        width: v.get("width").and_then(Value::as_u64).map(|n| n as u32),
        height: v.get("height").and_then(Value::as_u64).map(|n| n as u32),
        thumbnail: v.get("thumbnail").and_then(Value::as_str).map(str::to_string),
        ext: v.get("ext").and_then(Value::as_str).map(str::to_string),
    })
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
    body: axum::body::Bytes,
) -> Result<Json<Value>, ApiError> {
    let sanitized = crate::paths::sanitize_project_name(&q.project)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;
    let batch_id = q.batch_id.unwrap_or_else(repo::new_id);

    if body.len() as u64 > MAX_STAGE_UPLOAD_BYTES {
        return Err(ApiError::BadRequest(format!(
            "File exceeds maximum size of {}GB",
            MAX_STAGE_UPLOAD_BYTES / (1024 * 1024 * 1024)
        )));
    }
    if body.is_empty() {
        return Err(ApiError::BadRequest(format!("{}: uploaded file is empty (0 bytes)", q.filename)));
    }

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
    tokio::fs::write(&dest, &body)
        .await
        .map_err(|e| ApiError::Other(anyhow::anyhow!("write staged upload: {e}")))?;

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
}
