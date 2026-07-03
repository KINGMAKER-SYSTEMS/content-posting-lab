//! Job routes — the durable clip pipeline (first real vertical slice).
//!
//! POST /api/jobs/clip starts a background clip job: a long source video is cut
//! into fixed-length 9:16 shorts, each persisted as a `clip` Asset whose
//! `parent_id` points back at the source. Progress lives in SQLite (survives
//! restarts) and streams over SSE at GET /api/jobs/{id}/events. Plain polling is
//! available at GET /api/jobs/{id}.

use std::convert::Infallible;
use std::path::PathBuf;
use std::time::Duration;

use axum::extract::{Path, State};
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::routing::{get, post};
use axum::{Json, Router};
use clab_core::model::{AssetKind, JobKind};
use clab_core::repo;
use futures::stream::Stream;
use serde::Deserialize;

use super::ApiError;
use crate::state::AppState;

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/api/jobs/clip", post(start_clip))
        .route("/api/jobs/burn", post(start_burn))
        .route("/api/jobs/generate", post(start_generate))
        .route("/api/jobs/{id}", get(get_job))
        .route("/api/jobs/{id}/events", get(job_events))
}

#[derive(Deserialize)]
struct ClipRequest {
    project: String,
    /// Absolute or project-relative path to the long-form source video.
    source_path: String,
    /// Seconds per clip (e.g. 15).
    #[serde(default = "default_clip_len")]
    clip_length: f64,
    /// Optional trim window into the source; defaults to the whole video.
    #[serde(default)]
    start: f64,
    end: Option<f64>,
}

fn default_clip_len() -> f64 {
    15.0
}

async fn start_clip(
    State(st): State<AppState>,
    Json(req): Json<ClipRequest>,
) -> Result<Json<serde_json::Value>, ApiError> {
    // Validate inputs BEFORE anything touches the filesystem or ffmpeg.
    // (1) project name → safe slug (same rule as the /projects endpoint).
    let project = crate::paths::sanitize_project_name(&req.project)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;
    // (2) source_path must be a relative path confined under the data dir — a
    //     client may only clip files that live under data/, never /etc/passwd.
    let source_abs = crate::paths::confine_under_data(&data_dir(), &req.source_path)
        .map_err(|e| ApiError::BadRequest(format!("invalid source_path: {e}")))?;
    if !source_abs.is_file() {
        return Err(ApiError::BadRequest("source_path does not exist".into()));
    }
    let source_abs_str = source_abs.to_string_lossy().to_string();

    // Make sure the project exists (idempotent).
    repo::create_project(&st.db, &project).await?;

    // Record the source as an asset (so the clip lineage has a real parent).
    let source_meta = serde_json::json!({ "source_path": req.source_path }).to_string();
    let source = repo::create_asset(
        &st.db,
        &project,
        AssetKind::Source,
        None,
        Some(&source_meta),
    )
    .await?;
    // Store the confined relative path (not the raw request value).
    repo::mark_asset_ready(&st.db, &source.id, &req.source_path).await?;

    let params = serde_json::json!({
        "clip_length": req.clip_length,
        "start": req.start,
        "end": req.end,
    })
    .to_string();
    let job = repo::create_job(
        &st.db,
        &project,
        JobKind::Clip,
        Some(&source.id),
        Some(&params),
    )
    .await?;

    // Spawn the actual work; return the job id immediately (dodges any proxy
    // request timeout, mirrors the old fire-and-poll design but durable).
    let db = st.db.clone();
    let job_id = job.id.clone();
    let source_id = source.id.clone();
    tokio::spawn(async move {
        if let Err(e) = run_clip_job(
            db.clone(),
            &job_id,
            &project,
            &source_id,
            &source_abs_str,
            req.clip_length,
            req.start,
            req.end,
        )
        .await
        {
            tracing::error!("clip job {job_id} failed: {e:#}");
            let _ = repo::fail_job(&db, &job_id, &format!("{e:#}")).await;
        }
    });

    Ok(Json(serde_json::json!({ "job": job })))
}

#[allow(clippy::too_many_arguments)]
pub(crate) async fn run_clip_job(
    db: clab_core::Db,
    job_id: &str,
    project: &str,
    source_id: &str,
    source_path: &str,
    clip_length: f64,
    start: f64,
    end: Option<f64>,
) -> anyhow::Result<()> {
    let info = media::probe(source_path).await?;
    let trim_end = end.unwrap_or(info.duration_secs).min(info.duration_secs);
    let span = (trim_end - start).max(0.0);
    let num_clips = (span / clip_length).floor() as usize;
    if num_clips == 0 {
        anyhow::bail!(
            "source too short: {span:.1}s span < {clip_length:.1}s clip length"
        );
    }

    // Output directory: <data>/projects/<project>/clips/<job_id>/
    let data_dir = data_dir();
    let out_dir = PathBuf::from(&data_dir)
        .join("projects")
        .join(project)
        .join("clips")
        .join(job_id);
    tokio::fs::create_dir_all(&out_dir).await?;

    let mut produced = Vec::new();

    for i in 0..num_clips {
        let clip_start = start + (i as f64) * clip_length;
        let out_path = out_dir.join(format!("clip_{i:03}.mp4"));

        // Create the clip asset up front (queued), so it's visible while running.
        let meta = serde_json::json!({ "index": i, "source_id": source_id }).to_string();
        let clip_asset = repo::create_asset(
            &db,
            project,
            AssetKind::Clip,
            Some(source_id),
            Some(&meta),
        )
        .await?;
        repo::mark_asset_processing(&db, &clip_asset.id).await?;

        // Per-clip progress folds into overall job progress.
        let base = i as f64 / num_clips as f64;
        let span_frac = 1.0 / num_clips as f64;
        let db_for_cb = db.clone();
        let job_id_owned = job_id.to_string();
        let result = media::ffmpeg::clip_segment(
            std::path::Path::new(source_path),
            &out_path,
            clip_start,
            clip_length,
            &info,
            move |frac| {
                let overall = base + frac * span_frac;
                let db = db_for_cb.clone();
                let jid = job_id_owned.clone();
                // Fire-and-forget progress write; ignore errors (best-effort UI).
                tokio::spawn(async move {
                    let _ = repo::update_job_progress(&db, &jid, overall).await;
                });
            },
        )
        .await;

        match result {
            Ok(()) => {
                // Store the project-relative path (served under /projects later).
                let rel = format!("projects/{project}/clips/{job_id}/clip_{i:03}.mp4");
                repo::mark_asset_ready(&db, &clip_asset.id, &rel).await?;
                produced.push(clip_asset.id);
            }
            Err(e) => {
                repo::mark_asset_failed(&db, &clip_asset.id, &format!("{e:#}")).await?;
                // Continue with remaining clips rather than failing the whole job.
                tracing::warn!("clip {i} failed: {e:#}");
            }
        }
    }

    // If every clip failed, the job did NOT succeed — mark it failed rather
    // than reporting Done with zero results (which would feed the pipeline an
    // empty success). Per-asset error rows carry the detail.
    if produced.is_empty() {
        repo::fail_job(
            &db,
            job_id,
            "all clips failed — see per-asset errors",
        )
        .await?;
        return Ok(());
    }

    repo::finish_job(&db, job_id, &produced).await?;
    Ok(())
}

// ── Generate ─────────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct GenerateRequest {
    project: String,
    /// Public model id from the registry (e.g. "grok", "hailuo", "wan-t2v").
    model: String,
    prompt: String,
    #[serde(default = "default_aspect")]
    aspect_ratio: String,
    #[serde(default = "default_resolution")]
    resolution: String,
    #[serde(default = "default_duration")]
    duration: u32,
    /// Optional first-frame image data URI for image-to-video models.
    image_data_uri: Option<String>,
    /// Optional multi-crop of the generated source: single|dual|triptych|both.
    crop_mode: Option<String>,
}

fn default_aspect() -> String {
    "9:16".into()
}
fn default_resolution() -> String {
    "768p".into()
}
fn default_duration() -> u32 {
    6
}

async fn start_generate(
    State(st): State<AppState>,
    Json(req): Json<GenerateRequest>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let project = crate::paths::sanitize_project_name(&req.project)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;
    if req.prompt.trim().is_empty() {
        return Err(ApiError::BadRequest("prompt is required".into()));
    }
    let model = crate::providers::find_model(&req.model)
        .ok_or_else(|| ApiError::BadRequest(format!("unknown model: {}", req.model)))?;
    if model.needs_image && req.image_data_uri.is_none() {
        return Err(ApiError::BadRequest(format!(
            "{} is image-to-video — an image is required",
            model.name
        )));
    }
    // Same 50MB base64 guard the burn endpoint uses (OOM protection).
    if let Some(uri) = &req.image_data_uri {
        if uri.len() > 50 * 1024 * 1024 {
            return Err(ApiError::BadRequest("image_data_uri too large".into()));
        }
    }
    // Validate crop mode up front (default single).
    let crop_mode = match req.crop_mode.as_deref() {
        None => media::CropMode::Single,
        Some(s) => media::CropMode::parse(s)
            .ok_or_else(|| ApiError::BadRequest(format!("invalid crop_mode: {s}")))?,
    };
    // Build the provider now so a missing API key fails fast (before a job row).
    let provider = crate::providers::provider_for(model)
        .ok_or_else(|| ApiError::BadRequest(format!("{} not configured (set {})", model.name, model.key_env)))?;

    repo::create_project(&st.db, &project).await?;

    let meta = serde_json::json!({
        "model": model.id,
        "prompt": req.prompt,
        "aspect_ratio": req.aspect_ratio,
        "resolution": req.resolution,
        "duration": req.duration,
    })
    .to_string();
    let asset = repo::create_asset(&st.db, &project, AssetKind::Generated, None, Some(&meta)).await?;
    let job = repo::create_job(&st.db, &project, JobKind::Generate, Some(&asset.id), Some(&meta)).await?;

    let db = st.db.clone();
    let job_id = job.id.clone();
    let asset_id = asset.id.clone();
    let model_id = model.model_id.to_string();
    let gen_params = crate::providers::GenParams {
        prompt: req.prompt,
        aspect_ratio: req.aspect_ratio,
        resolution: req.resolution,
        duration: req.duration,
        image_data_uri: req.image_data_uri,
        // Internal JSON engine path carries no model-specific knobs; the legacy
        // multipart runner (routes/video.rs) is what populates these.
        extra: Default::default(),
    };
    tokio::spawn(async move {
        if let Err(e) = run_generate_job(
            db.clone(),
            &job_id,
            &project,
            &asset_id,
            provider,
            &model_id,
            gen_params,
            crop_mode,
        )
        .await
        {
            tracing::error!("generate job {job_id} failed: {e:#}");
            let _ = repo::mark_asset_failed(&db, &asset_id, &format!("{e:#}")).await;
            let _ = repo::fail_job(&db, &job_id, &format!("{e:#}")).await;
        }
    });

    Ok(Json(serde_json::json!({ "job": job, "asset_id": asset.id })))
}

#[allow(clippy::too_many_arguments)]
pub(crate) async fn run_generate_job(
    db: clab_core::Db,
    job_id: &str,
    project: &str,
    asset_id: &str,
    provider: Box<dyn crate::providers::Provider>,
    model_id: &str,
    params: crate::providers::GenParams,
    crop_mode: media::CropMode,
) -> anyhow::Result<()> {
    repo::mark_asset_processing(&db, asset_id).await?;
    repo::update_job_progress(&db, job_id, 0.02).await?;

    // Provider drives submit→poll; progress 0..0.6 maps into the job's 0..0.6.
    let db_cb = db.clone();
    let jid = job_id.to_string();
    let on_progress = move |frac: f64| {
        let db = db_cb.clone();
        let jid = jid.clone();
        tokio::spawn(async move {
            let _ = repo::update_job_progress(&db, &jid, frac).await;
        });
    };

    let client = reqwest::Client::new();
    let video_url = provider
        .generate(&client, model_id, &params, &on_progress)
        .await?;

    // Download the finished video to the project's generated/ dir.
    repo::update_job_progress(&db, job_id, 0.7).await?;
    let data = data_dir();
    let out_dir = PathBuf::from(&data)
        .join("projects")
        .join(project)
        .join("generated")
        .join(job_id);
    tokio::fs::create_dir_all(&out_dir).await?;
    let out_path = out_dir.join("video.mp4");
    download_to_file(&client, &video_url, &out_path).await?;

    let rel = format!("projects/{project}/generated/{job_id}/video.mp4");
    repo::mark_asset_ready(&db, asset_id, &rel).await?;

    // The generated video is the SOURCE asset. The result_asset_ids list is the
    // source plus any crops we fan out from it.
    let mut produced = vec![asset_id.to_string()];

    // Multi-crop the generated source into N 9:16 clip assets — the yield
    // multiplier. Each crop is a Clip asset whose parent is the generated source.
    if crop_mode != media::CropMode::Single {
        repo::update_job_progress(&db, job_id, 0.8).await?;
        let info = media::probe(out_path.to_str().unwrap_or_default()).await?;
        let crop_dir = PathBuf::from(&data)
            .join("projects")
            .join(project)
            .join("clips")
            .join(job_id);
        tokio::fs::create_dir_all(&crop_dir).await?;
        match media::multi_crop(&out_path, &crop_dir, &info, crop_mode).await {
            Ok(paths) => {
                for (i, p) in paths.iter().enumerate() {
                    let meta = serde_json::json!({
                        "crop_mode": crop_mode.as_str(),
                        "crop_index": i,
                        "source_id": asset_id,
                    })
                    .to_string();
                    let clip = repo::create_asset(
                        &db,
                        project,
                        AssetKind::Clip,
                        Some(asset_id),
                        Some(&meta),
                    )
                    .await?;
                    let fname = p.file_name().and_then(|f| f.to_str()).unwrap_or("crop.mp4");
                    let crop_rel = format!("projects/{project}/clips/{job_id}/{fname}");
                    repo::mark_asset_ready(&db, &clip.id, &crop_rel).await?;
                    produced.push(clip.id);
                }
            }
            Err(e) => {
                // The generated source is still good; surface the crop failure
                // but don't lose the generation.
                tracing::warn!("multi-crop failed for job {job_id}: {e:#}");
            }
        }
    }

    repo::finish_job(&db, job_id, &produced).await?;
    Ok(())
}

/// Max bytes we'll pull from a provider's video URL (guards against a rogue or
/// buggy CDN response filling the volume).
const MAX_VIDEO_BYTES: u64 = 2 * 1024 * 1024 * 1024; // 2 GiB

/// Validate the provider-returned video URL before fetching it. The URL is only
/// semi-trusted (it comes from the provider's JSON), so require https — except
/// a localhost http URL is allowed when a provider base-url override is set,
/// which only happens in tests. Closes the SSRF / file:// surface.
fn validate_video_url(url: &str) -> anyhow::Result<()> {
    if url.starts_with("https://") {
        return Ok(());
    }
    let test_mode = std::env::var("XAI_BASE_URL").is_ok()
        || std::env::var("REPLICATE_BASE_URL").is_ok();
    if test_mode && (url.starts_with("http://127.0.0.1") || url.starts_with("http://localhost")) {
        return Ok(());
    }
    anyhow::bail!("provider returned a non-https video URL (refusing to fetch)")
}

/// Stream a remote video URL to a local file. Bounded (no whole-file
/// buffering), size-capped, and the partial file is removed on failure.
pub(crate) async fn download_to_file(
    client: &reqwest::Client,
    url: &str,
    dest: &std::path::Path,
) -> anyhow::Result<()> {
    validate_video_url(url)?;
    match download_inner(client, url, dest).await {
        Ok(()) => Ok(()),
        Err(e) => {
            // Don't leave a truncated video.mp4 behind on a mid-stream error.
            let _ = tokio::fs::remove_file(dest).await;
            Err(e)
        }
    }
}

async fn download_inner(
    client: &reqwest::Client,
    url: &str,
    dest: &std::path::Path,
) -> anyhow::Result<()> {
    use anyhow::Context;
    use futures::StreamExt;
    use tokio::io::AsyncWriteExt;

    let resp = client
        .get(url)
        .timeout(Duration::from_secs(300))
        .send()
        .await
        .context("download request failed")?;
    if !resp.status().is_success() {
        anyhow::bail!("download failed: HTTP {}", resp.status());
    }
    // Fast path: reject oversized via Content-Length when present.
    if let Some(len) = resp.content_length() {
        if len > MAX_VIDEO_BYTES {
            anyhow::bail!("remote video too large: {len} bytes (limit {MAX_VIDEO_BYTES})");
        }
    }
    let mut file = tokio::fs::File::create(dest).await.context("create output file")?;
    let mut stream = resp.bytes_stream();
    let mut written: u64 = 0;
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.context("download stream error")?;
        written += chunk.len() as u64;
        if written > MAX_VIDEO_BYTES {
            anyhow::bail!("download exceeded size limit ({MAX_VIDEO_BYTES} bytes)");
        }
        file.write_all(&chunk).await.context("write output file")?;
    }
    file.flush().await.ok();
    Ok(())
}

// ── Burn ─────────────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct BurnRequest {
    /// The clip asset to burn onto. Burn consumes a stable asset id — never a
    /// folder path — so the selection can't be silently widened.
    asset_id: String,
    /// Base64-encoded transparent PNG overlay rendered by the browser (the same
    /// contract as the old app: text/typography is rasterized client-side and
    /// composited via the ffmpeg overlay filter). Optional → color-correct only.
    overlay_png_b64: Option<String>,
    /// Optional color-correction sliders.
    #[serde(default)]
    color_correction: Option<media::ColorCorrection>,
}

async fn start_burn(
    State(st): State<AppState>,
    Json(req): Json<BurnRequest>,
) -> Result<Json<serde_json::Value>, ApiError> {
    // Resolve the source clip asset (must exist and be ready).
    let clip = repo::get_asset(&st.db, &req.asset_id)
        .await?
        .ok_or(ApiError::NotFound)?;
    let clip_path = clip
        .path
        .clone()
        .ok_or_else(|| ApiError::BadRequest("source asset has no file yet".into()))?;

    // Guard the overlay size like the old burn.py (50MB base64 cap → OOM guard).
    if let Some(b64) = &req.overlay_png_b64 {
        if b64.len() > 50 * 1024 * 1024 {
            return Err(ApiError::BadRequest("overlay PNG too large".into()));
        }
    }

    let project = clip.project.clone();
    let job = repo::create_job(&st.db, &project, JobKind::Burn, Some(&clip.id), None).await?;

    let db = st.db.clone();
    let job_id = job.id.clone();
    let clip_id = clip.id.clone();
    tokio::spawn(async move {
        if let Err(e) = run_burn_job(
            db.clone(),
            &job_id,
            &project,
            &clip_id,
            &clip_path,
            req.overlay_png_b64,
            req.color_correction,
        )
        .await
        {
            tracing::error!("burn job {job_id} failed: {e:#}");
            let _ = repo::fail_job(&db, &job_id, &format!("{e:#}")).await;
        }
    });

    Ok(Json(serde_json::json!({ "job": job })))
}

pub(crate) async fn run_burn_job(
    db: clab_core::Db,
    job_id: &str,
    project: &str,
    clip_id: &str,
    clip_rel_path: &str,
    overlay_png_b64: Option<String>,
    cc: Option<media::ColorCorrection>,
) -> anyhow::Result<()> {
    let data_dir = data_dir();
    // Resolve the clip's real path. Stored paths are project-relative
    // ("projects/.../clip_000.mp4"); the source video in tests may be absolute.
    let clip_abs = resolve_path(&data_dir, clip_rel_path);
    let info = media::probe(clip_abs.to_str().unwrap_or_default()).await?;

    // The burned output asset (parent = the clip → lineage clip→burned).
    let burned = repo::create_asset(
        &db,
        project,
        AssetKind::Burned,
        Some(clip_id),
        None,
    )
    .await?;
    repo::mark_asset_processing(&db, &burned.id).await?;
    repo::update_job_progress(&db, job_id, 0.05).await?;

    // Output dir: <data>/projects/<project>/burned/<job_id>/
    let out_dir = PathBuf::from(&data_dir)
        .join("projects")
        .join(project)
        .join("burned")
        .join(job_id);
    tokio::fs::create_dir_all(&out_dir).await?;
    let out_path = out_dir.join("burned_000.mp4");

    // Decode the overlay PNG (if any) to a temp file for ffmpeg's 2nd input.
    let overlay_tmp = match &overlay_png_b64 {
        Some(b64) => {
            let bytes = base64_decode(b64)
                .map_err(|_| anyhow::anyhow!("overlay_png_b64 is not valid base64"))?;
            let tmp = out_dir.join("_overlay.png");
            tokio::fs::write(&tmp, &bytes).await?;
            Some(tmp)
        }
        None => None,
    };

    let cc_filter = cc
        .as_ref()
        .map(|c| media::build_cc_filter(Some(c), None))
        .filter(|f| f != "null");

    let db_cb = db.clone();
    let jid = job_id.to_string();
    let result = media::ffmpeg::burn_overlay(
        &clip_abs,
        overlay_tmp.as_deref(),
        cc_filter.as_deref(),
        &out_path,
        info.duration_secs,
        move |frac| {
            let db = db_cb.clone();
            let jid = jid.clone();
            // Map ffmpeg 0..1 into 0.05..1.0 (we reserved a little for setup).
            let overall = 0.05 + frac * 0.95;
            tokio::spawn(async move {
                let _ = repo::update_job_progress(&db, &jid, overall).await;
            });
        },
    )
    .await;

    // Clean up the temp overlay regardless of outcome.
    if let Some(tmp) = overlay_tmp {
        let _ = tokio::fs::remove_file(tmp).await;
    }

    match result {
        Ok(()) => {
            let rel = format!("projects/{project}/burned/{job_id}/burned_000.mp4");
            repo::mark_asset_ready(&db, &burned.id, &rel).await?;
            repo::finish_job(&db, job_id, &[burned.id]).await?;
            Ok(())
        }
        Err(e) => {
            repo::mark_asset_failed(&db, &burned.id, &format!("{e:#}")).await?;
            Err(e)
        }
    }
}

async fn get_job(
    State(st): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let job = repo::get_job(&st.db, &id).await?.ok_or(ApiError::NotFound)?;
    Ok(Json(serde_json::json!({ "job": job })))
}

// ── path + base64 helpers ────────────────────────────────────────────────────

fn data_dir() -> String {
    std::env::var("RAILWAY_VOLUME_MOUNT_PATH")
        .ok()
        .filter(|p| std::path::Path::new(p).exists())
        .unwrap_or_else(|| "./data".to_string())
}

/// Project-relative paths live under <data>; absolute paths pass through.
fn resolve_path(data_dir: &str, p: &str) -> PathBuf {
    let pb = PathBuf::from(p);
    if pb.is_absolute() {
        pb
    } else {
        PathBuf::from(data_dir).join(p)
    }
}

/// Resolve a stored asset path to an absolute path (shared with distribution).
pub fn resolve_asset_path(rel: &str) -> PathBuf {
    resolve_path(&data_dir(), rel)
}

/// Minimal standard-base64 decoder (avoids pulling the base64 crate for one use).
fn base64_decode(s: &str) -> Result<Vec<u8>, ()> {
    fn val(c: u8) -> Result<u8, ()> {
        match c {
            b'A'..=b'Z' => Ok(c - b'A'),
            b'a'..=b'z' => Ok(c - b'a' + 26),
            b'0'..=b'9' => Ok(c - b'0' + 52),
            b'+' => Ok(62),
            b'/' => Ok(63),
            _ => Err(()),
        }
    }
    let clean: Vec<u8> = s.bytes().filter(|b| !b.is_ascii_whitespace()).collect();
    let mut out = Vec::with_capacity(clean.len() / 4 * 3);
    for chunk in clean.chunks(4) {
        let pad = chunk.iter().filter(|&&b| b == b'=').count();
        let mut buf = [0u8; 4];
        for (i, &b) in chunk.iter().enumerate() {
            buf[i] = if b == b'=' { 0 } else { val(b)? };
        }
        let n = (buf[0] as u32) << 18
            | (buf[1] as u32) << 12
            | (buf[2] as u32) << 6
            | (buf[3] as u32);
        out.push((n >> 16) as u8);
        if pad < 2 {
            out.push((n >> 8) as u8);
        }
        if pad < 1 {
            out.push(n as u8);
        }
    }
    Ok(out)
}

/// SSE progress stream. Polls the job row and emits an event whenever progress
/// or status changes; closes when the job reaches a terminal state.
async fn job_events(
    State(st): State<AppState>,
    Path(id): Path<String>,
) -> Sse<impl Stream<Item = Result<Event, Infallible>>> {
    let stream = async_stream_job(st, id);
    // Keep-alive is OFF by default in axum — set it or proxies drop idle SSE.
    Sse::new(stream).keep_alive(KeepAlive::new().interval(Duration::from_secs(15)))
}

// A hand-rolled stream (avoids pulling async-stream just for this): tick every
// 500ms, emit on change, stop at terminal status.
fn async_stream_job(
    st: AppState,
    id: String,
) -> impl Stream<Item = Result<Event, Infallible>> {
    use clab_core::model::JobStatus;
    use futures::stream::unfold;

    struct S {
        st: AppState,
        id: String,
        last: Option<(f64, JobStatus)>,
        done: bool,
    }

    unfold(
        S { st, id, last: None, done: false },
        |mut s| async move {
            if s.done {
                return None;
            }
            loop {
                tokio::time::sleep(Duration::from_millis(500)).await;
                let job = match repo::get_job(&s.st.db, &s.id).await {
                    Ok(Some(j)) => j,
                    _ => {
                        s.done = true;
                        let ev = Event::default().event("error").data("job not found");
                        return Some((Ok(ev), s));
                    }
                };
                let key = (job.progress, job.status);
                let terminal = matches!(job.status, JobStatus::Done | JobStatus::Failed);
                if s.last.as_ref() != Some(&key) || terminal {
                    s.last = Some(key);
                    let payload = serde_json::json!({
                        "status": job.status,
                        "progress": job.progress,
                        "result_asset_ids": job.result_asset_ids,
                        "error": job.error,
                    });
                    let ev = Event::default()
                        .event("progress")
                        .data(payload.to_string());
                    if terminal {
                        s.done = true;
                    }
                    return Some((Ok(ev), s));
                }
            }
        },
    )
}
