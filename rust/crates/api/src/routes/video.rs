//! Video-generation management/metadata routes — parity port of the
//! non-generate parts of `routers/video.py` (940 lines in Python).
//!
//! The actual generate-job runner (submit → poll → download, multi-crop
//! fan-out) is wave 3's `POST /api/jobs/generate` (see `routes/jobs.rs`) and is
//! explicitly out of scope here. This module covers everything *around* it:
//!
//!   - `GET  /api/video/providers`        — configured-provider list (Python
//!     `PROVIDERS` shape: id/name/group/key_id/pricing/models)
//!   - `GET  /api/video/provider-schemas` — per-provider input-param schemas
//!     (verbatim port of Python's `PROVIDER_SCHEMAS` constant)
//!   - `GET  /api/video/jobs` + `GET /api/video/jobs/{id}` + `DELETE .../{id}`
//!     — reshapes `JobKind::Generate` rows (+ their result assets) into the
//!     old `{id, prompt, provider, count, videos: [...]}` job shape the
//!     frontend (`frontend/src/types/api.ts` `Job`/`VideoEntry`) expects
//!   - `GET  /api/video/prompts` + `DELETE /api/video/prompts` — saved prompt
//!     history (now backed by `repo::video`, see that module's doc comment for
//!     why it rides the existing `settings` KV table instead of a new one)
//!   - `POST /api/video/color-correct` + `.../color-correct/bulk` — synchronous
//!     ffmpeg color-correction using the already-ported `media::cc` matrix math
//!   - `DELETE /api/video/file` — delete a single video file, confined under
//!     the project's video directory
//!
//! Response field names are kept identical to the Python router on purpose —
//! the React `Generate.tsx` page (and its `frontend/src/types/api.ts` contract)
//! is frozen for this wave and consumes these shapes as-is.
//!
//! **Parity deviation (documented, not silently dropped):** the Python
//! `/color-correct` endpoints used a dedicated `STANDARD_ENCODE_ARGS` preset
//! (preserves source framerate, `-c:a copy`, no forced `-r`). The Rust `media`
//! crate is frozen this wave and only exposes `media::ffmpeg::burn_overlay`,
//! which bakes in the TikTok-oriented encode args (`-r 30`, AAC re-encode).
//! Reused here rather than forking ffmpeg-arg-building logic into this route
//! file. Framerate/audio-codec output can differ slightly from the old Burn
//! tab's color-correct output; flagged as a seam request for wave 3 (see final
//! report) to add a `media::ffmpeg::color_correct` primitive with the original
//! preset.

use std::path::{Path as StdPath, PathBuf};

use axum::body::Body;
use axum::extract::{Path, Query, State};
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, get, post};
use axum::{Json, Router};
use clab_core::model::{JobKind, JobStatus};
use clab_core::repo;
use clab_core::repo::video as prompts_repo;
use serde::Deserialize;
use serde_json::{json, Value};

use super::ApiError;
use crate::state::AppState;

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/api/video/providers", get(list_providers))
        .route("/api/video/provider-schemas", get(provider_schemas))
        .route("/api/video/jobs", get(list_jobs))
        .route("/api/video/jobs/{job_id}", get(get_job).delete(delete_job))
        .route("/api/video/prompts", get(list_prompts).delete(clear_prompts))
        .route("/api/video/color-correct", post(color_correct_one))
        .route("/api/video/color-correct/bulk", post(color_correct_bulk))
        .route("/api/video/file", delete(delete_file))
}

fn default_project() -> String {
    "quick-test".to_string()
}

// ── Providers ────────────────────────────────────────────────────────────────

/// `key_env` → Python's `key_id` (the env-var group name the old `API_KEYS`
/// dict indexed providers by). Only the two groups the curated Rust registry
/// actually uses.
fn key_id_for(key_env: &str) -> &'static str {
    match key_env {
        "XAI_API_KEY" => "xai",
        "REPLICATE_API_TOKEN" => "replicate",
        _ => "unknown",
    }
}

/// Static pricing blurbs for the models that exist in both the old Python
/// `PROVIDERS` dict and the new curated `MODELS` registry (see
/// `providers/__init__.py` for the source values). Cosmetic-only field —
/// missing entries fall back to an empty string rather than failing.
fn pricing_for(id: &str) -> &'static str {
    match id {
        "grok" => "~$5/10s video",
        "hailuo" => "~$0.28/video",
        "wan-t2v" => "~$0.06/sec",
        "wan-i2v" => "~$0.06/sec",
        _ => "",
    }
}

/// `GET /api/video/providers` — providers whose API key env var is set.
/// Shape matches Python's `list_providers`: a flat array of
/// `{id, name, group, key_id, pricing, models}`.
async fn list_providers() -> Json<Vec<Value>> {
    let out = crate::providers::available_models()
        .into_iter()
        .map(|m| {
            json!({
                "id": m.id,
                "name": m.name,
                "group": m.group,
                "key_id": key_id_for(m.key_env),
                "pricing": pricing_for(m.id),
                "models": [m.model_id],
            })
        })
        .collect();
    Json(out)
}

// ── Provider schemas ─────────────────────────────────────────────────────────

/// `GET /api/video/provider-schemas` — verbatim port of the Python
/// `PROVIDER_SCHEMAS` constant (per-provider UI input schema). Static data —
/// built fresh per request rather than as a `once_cell`/`lazy_static` since
/// it's cheap and this endpoint is polled rarely (once per project switch).
async fn provider_schemas() -> Json<Value> {
    Json(json!({
        "grok": {
            "duration": {"type": "range", "min": 1, "max": 15, "default": 10, "label": "Duration (seconds)"},
            "aspect_ratio": {
                "type": "select",
                "options": ["16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3"],
                "default": "16:9",
                "label": "Aspect Ratio"
            },
            "resolution": {"type": "select", "options": ["480p", "720p"], "default": "480p", "label": "Resolution"}
        },
        "hailuo": {
            "duration": {"type": "select", "options": [6, 10], "default": 6, "label": "Duration (seconds)", "note": "10s only at 768p"},
            "resolution": {"type": "select", "options": ["768p", "1080p"], "default": "768p", "label": "Resolution", "note": "1080p locks duration to 6s"},
            "crop_mode": {"type": "select", "options": ["none", "dual", "triptych", "both"], "default": "both", "label": "Multi-Crop", "note": "dual=2, triptych=3, both=5 crops from one 16:9"},
            "optimize_prompt": {"type": "toggle", "default": true, "label": "Prompt Optimizer"}
        },
        "wan-t2v": {
            "aspect_ratio": {"type": "select", "options": ["16:9", "9:16"], "default": "16:9", "label": "Aspect Ratio"},
            "resolution": {"type": "select", "options": ["480p", "720p"], "default": "480p", "label": "Resolution"},
            "num_frames": {"type": "range", "min": 81, "max": 121, "default": 81, "step": 4, "label": "Frames", "note": "81 = ~5s, 121 = ~7.5s at 16fps"},
            "_advanced": {
                "sample_shift": {"type": "range", "min": 1, "max": 20, "default": 12, "step": 1, "label": "Sample Shift"},
                "frames_per_second": {"type": "range", "min": 5, "max": 30, "default": 16, "step": 1, "label": "FPS"},
                "go_fast": {"type": "toggle", "default": true, "label": "Go Fast"},
                "interpolate_output": {"type": "toggle", "default": true, "label": "Interpolate to 30fps"},
                "negative_prompt": {"type": "text", "default": "", "label": "Negative Prompt", "placeholder": "motion, morphing, text, extra vehicles..."},
                "lora_weights_transformer": {"type": "text", "default": "", "label": "LoRA Weights URL", "placeholder": "https://huggingface.co/.../lora.safetensors"},
                "lora_scale_transformer": {"type": "range", "min": 0, "max": 2, "default": 1, "step": 0.1, "label": "LoRA Scale"}
            }
        },
        "wan-i2v": {
            "resolution": {"type": "select", "options": ["480p", "720p"], "default": "480p", "label": "Resolution"},
            "num_frames": {"type": "range", "min": 81, "max": 100, "default": 81, "step": 1, "label": "Frames", "note": "81 = ~5s, 100 = ~6.25s at 16fps"},
            "image_required": true,
            "_advanced": {
                "sample_steps": {"type": "range", "min": 1, "max": 50, "default": 40, "step": 1, "label": "Sample Steps"},
                "sample_shift": {"type": "range", "min": 1, "max": 20, "default": 5, "step": 1, "label": "Sample Shift"},
                "frames_per_second": {"type": "range", "min": 5, "max": 24, "default": 16, "step": 1, "label": "FPS"},
                "go_fast": {"type": "toggle", "default": false, "label": "Go Fast"},
                "negative_prompt": {"type": "text", "default": "", "label": "Negative Prompt", "placeholder": "motion, morphing, text, extra vehicles..."}
            }
        },
        "pruna-pvideo": {
            "duration": {"type": "range", "min": 1, "max": 10, "default": 6, "label": "Duration (seconds)"},
            "resolution": {"type": "select", "options": ["720p", "1080p"], "default": "720p", "label": "Resolution"},
            "crop_mode": {"type": "select", "options": ["none", "dual", "triptych", "both"], "default": "both", "label": "Multi-Crop", "note": "dual=2, triptych=3, both=5 crops from one 16:9"},
            "optimize_prompt": {"type": "toggle", "default": true, "label": "Prompt Optimizer"}
        },
        "pruna-pvideo-vertical": {
            "duration": {"type": "range", "min": 1, "max": 10, "default": 6, "label": "Duration (seconds)"},
            "resolution": {"type": "select", "options": ["720p", "1080p"], "default": "720p", "label": "Resolution"},
            "optimize_prompt": {"type": "toggle", "default": true, "label": "Prompt Optimizer"}
        },
        "wan-i2v-fast": {
            "resolution": {"type": "select", "options": ["480p", "720p"], "default": "480p", "label": "Resolution"},
            "num_frames": {"type": "range", "min": 81, "max": 121, "default": 81, "step": 4, "label": "Frames", "note": "81 = ~5s, 121 = ~7.5s at 16fps"},
            "image_required": true,
            "last_image_supported": true,
            "_advanced": {
                "sample_shift": {"type": "range", "min": 1, "max": 20, "default": 12, "step": 1, "label": "Sample Shift"},
                "frames_per_second": {"type": "range", "min": 5, "max": 30, "default": 16, "step": 1, "label": "FPS"},
                "go_fast": {"type": "toggle", "default": true, "label": "Go Fast"},
                "interpolate_output": {"type": "toggle", "default": false, "label": "Interpolate to 30fps"},
                "negative_prompt": {"type": "text", "default": "", "label": "Negative Prompt", "placeholder": "motion, morphing, text, extra vehicles..."},
                "lora_weights_transformer": {"type": "text", "default": "", "label": "LoRA Weights URL", "placeholder": "https://huggingface.co/.../lora.safetensors"},
                "lora_scale_transformer": {"type": "range", "min": 0, "max": 2, "default": 1, "step": 0.1, "label": "LoRA Scale"}
            }
        }
    }))
}

// ── Jobs (read/list/delete over JobKind::Generate) ──────────────────────────

#[derive(Deserialize)]
struct ProjectQuery {
    #[serde(default = "default_project")]
    project: String,
}

/// Reshape a durable `Job` (+ its result assets) into the old
/// `{id, prompt, provider, count, project, created_at, videos: [...]}` shape.
/// `videos` is synthesized from `result_asset_ids` — the new pipeline doesn't
/// track a queued/generating/polling/downloading progression per clip the way
/// the in-memory Python dict did (that granularity lives inside the generate
/// job runner itself, wave 3's concern); a finished job's assets are always
/// reported "done", an in-progress job reports a single synthetic "generating"
/// placeholder so the frontend's `isTerminal` polling loop has something to
/// poll, and a failed job reports "error" with the job's error message.
async fn job_to_video_json(db: &clab_core::Db, job: &clab_core::model::Job) -> Value {
    let params: Option<Value> = job.params.as_deref().and_then(|s| serde_json::from_str(s).ok());
    let prompt = params
        .as_ref()
        .and_then(|v| v.get("prompt").and_then(|p| p.as_str()))
        .unwrap_or_default()
        .to_string();
    let provider = params
        .as_ref()
        .and_then(|v| v.get("model").and_then(|p| p.as_str()))
        .unwrap_or_default()
        .to_string();

    let result_ids: Vec<String> = serde_json::from_str(&job.result_asset_ids).unwrap_or_default();

    let videos: Vec<Value> = match job.status {
        JobStatus::Done => {
            let mut out = Vec::with_capacity(result_ids.len());
            for (i, asset_id) in result_ids.iter().enumerate() {
                let asset = repo::get_asset(db, asset_id).await.ok().flatten();
                let file = asset.as_ref().and_then(|a| a.path.clone());
                let mut entry = json!({
                    "index": i,
                    "status": "done",
                });
                if let Some(f) = &file {
                    entry["file"] = json!(f);
                    entry["url"] = json!(format!("/{f}"));
                }
                out.push(entry);
            }
            if out.is_empty() {
                vec![json!({"index": 0, "status": "error", "error": "no output produced"})]
            } else {
                out
            }
        }
        JobStatus::Failed => {
            vec![json!({
                "index": 0,
                "status": "error",
                "error": job.error.clone().unwrap_or_else(|| "generation failed".to_string()),
            })]
        }
        JobStatus::Queued | JobStatus::Processing => {
            vec![json!({"index": 0, "status": "generating"})]
        }
    };

    json!({
        "id": job.id,
        "prompt": prompt,
        "provider": provider,
        "count": videos.len().max(1),
        "project": job.project,
        "created_at": job.created_at.to_rfc3339(),
        "videos": videos,
    })
}

/// `GET /api/video/jobs?project=...` — list generate jobs for a project.
async fn list_jobs(
    State(st): State<AppState>,
    Query(q): Query<ProjectQuery>,
) -> Result<Json<Vec<Value>>, ApiError> {
    let jobs = repo::list_jobs(&st.db, &q.project).await?;
    let mut out = Vec::with_capacity(jobs.len());
    for job in jobs.iter().filter(|j| j.kind == JobKind::Generate) {
        out.push(job_to_video_json(&st.db, job).await);
    }
    Ok(Json(out))
}

/// `GET /api/video/jobs/{job_id}` — fetch a single generate job by id.
async fn get_job(State(st): State<AppState>, Path(job_id): Path<String>) -> Result<Json<Value>, ApiError> {
    let job = repo::get_job(&st.db, &job_id).await?.ok_or(ApiError::NotFound)?;
    if job.kind != JobKind::Generate {
        return Err(ApiError::NotFound);
    }
    Ok(Json(job_to_video_json(&st.db, &job).await))
}

/// `DELETE /api/video/jobs/{job_id}` — delete a generate job's DB row and the
/// video files its result assets point to (confined under the project's video
/// dir, same containment rule as `DELETE /api/video/file`). The row delete
/// itself goes through `repo::video::delete_job_row` (this module's own repo
/// file) rather than the shared job-lifecycle functions in `repo::mod`, which
/// only support create/update/finish/fail — not delete.
async fn delete_job(
    State(st): State<AppState>,
    Path(job_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let job = repo::get_job(&st.db, &job_id).await?.ok_or(ApiError::NotFound)?;
    if job.kind != JobKind::Generate {
        return Err(ApiError::NotFound);
    }

    let result_ids: Vec<String> = serde_json::from_str(&job.result_asset_ids).unwrap_or_default();
    let video_dir = project_video_dir(&job.project);
    let mut files_removed = 0u32;
    for asset_id in &result_ids {
        if let Some(asset) = repo::get_asset(&st.db, asset_id).await? {
            if let Some(rel) = &asset.path {
                if let Some(abs) = confine_in_dir(&video_dir, rel) {
                    if tokio::fs::remove_file(&abs).await.is_ok() {
                        files_removed += 1;
                    }
                }
            }
        }
    }

    prompts_repo::delete_job_row(&st.db, &job_id).await?;

    Ok(Json(json!({
        "deleted": true,
        "job_id": job_id,
        "files_removed": files_removed,
    })))
}

// ── Prompt history ───────────────────────────────────────────────────────────

/// `GET /api/video/prompts?project=...` — saved prompt history, newest first.
async fn list_prompts(
    State(st): State<AppState>,
    Query(q): Query<ProjectQuery>,
) -> Result<Json<Vec<prompts_repo::PromptEntry>>, ApiError> {
    Ok(Json(prompts_repo::list_prompts(&st.db, &q.project).await?))
}

/// `DELETE /api/video/prompts?project=...` — clear a project's prompt history.
async fn clear_prompts(
    State(st): State<AppState>,
    Query(q): Query<ProjectQuery>,
) -> Result<Json<Value>, ApiError> {
    prompts_repo::clear_prompts(&st.db, &q.project).await?;
    Ok(Json(json!({ "ok": true })))
}

// ── File delete ──────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct FileDeleteQuery {
    #[serde(default = "default_project")]
    project: String,
    #[serde(default)]
    path: String,
}

/// `DELETE /api/video/file?project=...&path=...` — delete a single video file,
/// confined under the project's video directory (same containment rule as the
/// Python router's `delete_video_file`).
async fn delete_file(Query(q): Query<FileDeleteQuery>) -> Result<Json<Value>, ApiError> {
    if q.path.is_empty() {
        return Err(ApiError::BadRequest("path is required".into()));
    }
    let video_dir = project_video_dir(&q.project);
    let target = confine_in_dir(&video_dir, &q.path)
        .ok_or_else(|| ApiError::BadRequest("Invalid path".into()))?;
    if !target.exists() {
        return Err(ApiError::NotFound);
    }
    tokio::fs::remove_file(&target)
        .await
        .map_err(|e| ApiError::Other(anyhow::anyhow!("failed to delete file: {e}")))?;
    Ok(Json(json!({ "deleted": true, "path": q.path })))
}

// ── Color correction ─────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct ColorCorrectOneRequest {
    #[serde(default)]
    project: String,
    #[serde(default)]
    path: String,
    #[serde(default)]
    color_correction: Option<media::ColorCorrection>,
}

fn cc_is_default(cc: &Option<media::ColorCorrection>) -> bool {
    // build_cc_filter returns "null" for both None and an all-zero struct —
    // reuse that logic instead of re-deriving Default-equality here.
    media::build_cc_filter(cc.as_ref(), None) == "null"
}

/// `POST /api/video/color-correct` — return a color-corrected copy of a single
/// video. Zero-CC fast path streams the original bytes unchanged (no re-encode).
async fn color_correct_one(Json(req): Json<ColorCorrectOneRequest>) -> Result<Response, ApiError> {
    let src = resolve_safe_video_path(&req.project, &req.path)?;
    let stem = src
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("video")
        .to_string();
    let out_name = format!("{stem}_cc.mp4");

    if cc_is_default(&req.color_correction) {
        let bytes = tokio::fs::read(&src)
            .await
            .map_err(|e| ApiError::Other(anyhow::anyhow!("failed to read source: {e}")))?;
        return Ok(video_response(bytes, &out_name));
    }

    let bytes = run_cc_encode(&src, &req.color_correction)
        .await
        .map_err(|e| ApiError::Other(anyhow::anyhow!("ffmpeg failed: {e:#}")))?;
    Ok(video_response(bytes, &out_name))
}

/// Run the color-correct ffmpeg encode for one file and return its bytes,
/// cleaning up the temp output regardless of outcome.
async fn run_cc_encode(src: &StdPath, cc: &Option<media::ColorCorrection>) -> anyhow::Result<Vec<u8>> {
    let info = media::probe(src.to_str().ok_or_else(|| anyhow::anyhow!("non-utf8 path"))?).await?;
    let cc_filter = media::build_cc_filter(cc.as_ref(), None);
    let cc_filter = if cc_filter == "null" { None } else { Some(cc_filter.as_str()) };
    let tmp = tempfile_path("mp4");
    let result = media::ffmpeg::burn_overlay(src, None, cc_filter, &tmp, info.duration_secs, |_| {}).await;
    let bytes = match result {
        Ok(()) => tokio::fs::read(&tmp).await,
        Err(e) => {
            let _ = tokio::fs::remove_file(&tmp).await;
            return Err(e);
        }
    };
    let _ = tokio::fs::remove_file(&tmp).await;
    Ok(bytes?)
}

#[derive(Deserialize)]
struct CcBulkItem {
    #[serde(default)]
    path: String,
    #[serde(default)]
    color_correction: Option<media::ColorCorrection>,
}

#[derive(Deserialize)]
struct CcBulkRequest {
    #[serde(default)]
    project: String,
    #[serde(default)]
    items: Vec<CcBulkItem>,
}

/// Hard ceiling on a single bulk color-correct request (matches the Python
/// router's `_CC_BULK_MAX_ITEMS`).
const CC_BULK_MAX_ITEMS: usize = 50;

/// `POST /api/video/color-correct/bulk` — stream a ZIP of color-corrected
/// videos. Items with no/default CC are copied raw; per-item ffmpeg failures
/// are recorded in `_errors.txt` inside the ZIP rather than aborting the batch.
async fn color_correct_bulk(Json(req): Json<CcBulkRequest>) -> Result<Response, ApiError> {
    if req.project.is_empty() {
        return Err(ApiError::BadRequest("project is required".into()));
    }
    if req.items.is_empty() {
        return Err(ApiError::BadRequest("items is required".into()));
    }
    if req.items.len() > CC_BULK_MAX_ITEMS {
        return Err(ApiError::BadRequest(format!(
            "Too many items (max {CC_BULK_MAX_ITEMS})"
        )));
    }

    // Resolve + validate every path up front — one bad path fails the whole
    // request instead of silently skipping (matches the Python router).
    let mut resolved: Vec<(PathBuf, Option<media::ColorCorrection>)> = Vec::with_capacity(req.items.len());
    for item in &req.items {
        let src = resolve_safe_video_path(&req.project, &item.path)?;
        resolved.push((src, item.color_correction.clone()));
    }

    let mut used_names: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut errors: Vec<String> = Vec::new();
    let mut zip_entries: Vec<(String, Vec<u8>)> = Vec::new();
    let mut ok_count = 0usize;

    for (src, cc) in &resolved {
        let stem = src.file_stem().and_then(|s| s.to_str()).unwrap_or("video");
        let has_cc = !cc_is_default(cc);
        let arcname = pick_zip_name(&mut used_names, stem, has_cc);
        let src_name = src.file_name().and_then(|s| s.to_str()).unwrap_or("video").to_string();

        if !has_cc {
            match tokio::fs::read(src).await {
                Ok(bytes) => {
                    zip_entries.push((arcname, bytes));
                    ok_count += 1;
                }
                Err(e) => errors.push(format!("{src_name}: failed to read source: {e}")),
            }
            continue;
        }

        match run_cc_encode(src, cc).await {
            Ok(bytes) => {
                zip_entries.push((arcname, bytes));
                ok_count += 1;
            }
            Err(e) => errors.push(format!("{src_name}: {e:#}")),
        }
    }

    if ok_count == 0 {
        let msg = if errors.is_empty() {
            "all encodes failed".to_string()
        } else {
            errors.join("; ")
        };
        return Err(ApiError::BadRequest(msg));
    }

    let zip_bytes = build_zip(&zip_entries, &errors)
        .map_err(|e| ApiError::Other(anyhow::anyhow!("failed to build zip: {e}")))?;

    let today = chrono::Utc::now().format("%Y-%m-%d");
    let zip_name = format!("videolab_cc_{today}_{ok_count}videos.zip");
    Ok(zip_response(zip_bytes, &zip_name))
}

fn pick_zip_name(used: &mut std::collections::HashSet<String>, stem: &str, has_cc: bool) -> String {
    let suffix = if has_cc { "_cc" } else { "" };
    let base = format!("{stem}{suffix}.mp4");
    if used.insert(base.clone()) {
        return base;
    }
    let mut n = 2;
    loop {
        let name = format!("{stem}{suffix}_{n}.mp4");
        if used.insert(name.clone()) {
            return name;
        }
        n += 1;
    }
}

/// Build a ZIP (stored, no compression — matches the Python router's
/// `ZIP_STORED`) from in-memory entries plus an optional `_errors.txt`. The
/// workspace has no `zip` crate dependency and this file can't edit
/// `api/Cargo.toml`, so this hand-rolls just enough of the format (local file
/// headers + central directory, STORED/no-compression only) to produce a
/// valid archive any unzip tool can read.
fn build_zip(entries: &[(String, Vec<u8>)], errors: &[String]) -> std::io::Result<Vec<u8>> {
    let mut buf = Vec::new();
    {
        let cursor = std::io::Cursor::new(&mut buf);
        let mut zip = zip_writer::ZipWriter::new(cursor);
        for (name, bytes) in entries {
            zip.start_file(name)?;
            zip.write_all(bytes)?;
        }
        if !errors.is_empty() {
            zip.start_file("_errors.txt")?;
            let mut body = errors.join("\n");
            body.push('\n');
            zip.write_all(body.as_bytes())?;
        }
        zip.finish()?;
    }
    Ok(buf)
}

mod zip_writer {
    use std::io::{self, Write};

    pub struct ZipWriter<W: Write + io::Seek> {
        w: W,
        entries: Vec<Entry>,
        current: Option<String>,
    }

    struct Entry {
        name: String,
        crc: u32,
        size: u32,
        offset: u32,
    }

    impl<W: Write + io::Seek> ZipWriter<W> {
        pub fn new(w: W) -> Self {
            Self { w, entries: Vec::new(), current: None }
        }

        pub fn start_file(&mut self, name: &str) -> io::Result<()> {
            self.current = Some(name.to_string());
            Ok(())
        }

        pub fn write_all(&mut self, data: &[u8]) -> io::Result<()> {
            let name = self
                .current
                .take()
                .ok_or_else(|| io::Error::other("no active entry"))?;
            let offset = self.w.stream_position()? as u32;
            let crc = crc32(data);
            write_local_header(&mut self.w, &name, crc, data.len() as u32)?;
            self.w.write_all(data)?;
            self.entries.push(Entry {
                name,
                crc,
                size: data.len() as u32,
                offset,
            });
            Ok(())
        }

        pub fn finish(mut self) -> io::Result<()> {
            let cd_start = self.w.stream_position()? as u32;
            for e in &self.entries {
                write_central_header(&mut self.w, e)?;
            }
            let cd_end = self.w.stream_position()? as u32;
            write_end_of_central_dir(&mut self.w, self.entries.len() as u16, cd_end - cd_start, cd_start)?;
            Ok(())
        }
    }

    fn write_local_header<W: Write>(w: &mut W, name: &str, crc: u32, size: u32) -> io::Result<()> {
        w.write_all(&0x04034b50u32.to_le_bytes())?;
        w.write_all(&20u16.to_le_bytes())?; // version needed
        w.write_all(&0u16.to_le_bytes())?; // flags
        w.write_all(&0u16.to_le_bytes())?; // method = stored
        w.write_all(&0u16.to_le_bytes())?; // mod time
        w.write_all(&0u16.to_le_bytes())?; // mod date
        w.write_all(&crc.to_le_bytes())?;
        w.write_all(&size.to_le_bytes())?; // compressed size
        w.write_all(&size.to_le_bytes())?; // uncompressed size
        w.write_all(&(name.len() as u16).to_le_bytes())?;
        w.write_all(&0u16.to_le_bytes())?; // extra len
        w.write_all(name.as_bytes())?;
        Ok(())
    }

    fn write_central_header<W: Write>(w: &mut W, e: &Entry) -> io::Result<()> {
        w.write_all(&0x02014b50u32.to_le_bytes())?;
        w.write_all(&20u16.to_le_bytes())?; // version made by
        w.write_all(&20u16.to_le_bytes())?; // version needed
        w.write_all(&0u16.to_le_bytes())?; // flags
        w.write_all(&0u16.to_le_bytes())?; // method
        w.write_all(&0u16.to_le_bytes())?; // mod time
        w.write_all(&0u16.to_le_bytes())?; // mod date
        w.write_all(&e.crc.to_le_bytes())?;
        w.write_all(&e.size.to_le_bytes())?;
        w.write_all(&e.size.to_le_bytes())?;
        w.write_all(&(e.name.len() as u16).to_le_bytes())?;
        w.write_all(&0u16.to_le_bytes())?; // extra len
        w.write_all(&0u16.to_le_bytes())?; // comment len
        w.write_all(&0u16.to_le_bytes())?; // disk number
        w.write_all(&0u16.to_le_bytes())?; // internal attrs
        w.write_all(&0u32.to_le_bytes())?; // external attrs
        w.write_all(&e.offset.to_le_bytes())?;
        w.write_all(e.name.as_bytes())?;
        Ok(())
    }

    fn write_end_of_central_dir<W: Write>(w: &mut W, count: u16, cd_size: u32, cd_offset: u32) -> io::Result<()> {
        w.write_all(&0x06054b50u32.to_le_bytes())?;
        w.write_all(&0u16.to_le_bytes())?; // disk number
        w.write_all(&0u16.to_le_bytes())?; // disk with cd
        w.write_all(&count.to_le_bytes())?;
        w.write_all(&count.to_le_bytes())?;
        w.write_all(&cd_size.to_le_bytes())?;
        w.write_all(&cd_offset.to_le_bytes())?;
        w.write_all(&0u16.to_le_bytes())?; // comment len
        Ok(())
    }

    /// Standard CRC-32 (ISO-HDLC / zip polynomial 0xEDB88320), computed
    /// byte-by-byte — no external crate needed for this size of payload.
    fn crc32(data: &[u8]) -> u32 {
        let mut crc = 0xFFFF_FFFFu32;
        for &byte in data {
            crc ^= byte as u32;
            for _ in 0..8 {
                let mask = (crc & 1).wrapping_neg();
                crc = (crc >> 1) ^ (0xEDB8_8320 & mask);
            }
        }
        !crc
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn crc32_matches_known_vector() {
            // CRC-32 of the ASCII string "123456789" is the standard check
            // value 0xCBF43926 (used to validate CRC implementations).
            assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
        }
    }
}

// ── shared helpers ───────────────────────────────────────────────────────────

fn data_dir() -> String {
    std::env::var("RAILWAY_VOLUME_MOUNT_PATH")
        .ok()
        .filter(|p| StdPath::new(p).exists())
        .unwrap_or_else(|| "./data".to_string())
}

fn project_video_dir(project: &str) -> PathBuf {
    PathBuf::from(data_dir()).join("projects").join(project).join("videos")
}

/// Confine `rel` (a caller-supplied relative path) under `dir`, rejecting any
/// path that escapes it. Returns `None` on traversal — same containment intent
/// as `crate::paths::confine_under_data`, scoped to an arbitrary base dir (the
/// project's video dir here) instead of the whole data dir.
fn confine_in_dir(dir: &StdPath, rel: &str) -> Option<PathBuf> {
    let target = normalize_lexically(&dir.join(rel));
    let dir_norm = normalize_lexically(dir);
    if target.starts_with(&dir_norm) {
        Some(target)
    } else {
        None
    }
}

/// Lexical (non-IO) path normalization — resolves `.`/`..` components without
/// touching the filesystem, so containment can be checked even for paths that
/// don't exist yet. An absolute `rel` joined onto `dir` replaces `dir`
/// entirely (`std::path::Path::join` semantics), which this still catches
/// because the replaced (absolute) path won't start with the normalized `dir`.
fn normalize_lexically(p: &StdPath) -> PathBuf {
    let mut out = PathBuf::new();
    for comp in p.components() {
        match comp {
            std::path::Component::ParentDir => {
                out.pop();
            }
            std::path::Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// Resolve+validate a user-supplied video path against the project's video
/// dir. 400 on missing project/path or traversal, 404 if the file is missing —
/// mirrors the Python router's `_resolve_safe_video_path`.
fn resolve_safe_video_path(project: &str, path: &str) -> Result<PathBuf, ApiError> {
    if project.is_empty() {
        return Err(ApiError::BadRequest("project is required".into()));
    }
    if path.is_empty() {
        return Err(ApiError::BadRequest("path is required".into()));
    }
    let video_dir = project_video_dir(project);
    let target = confine_in_dir(&video_dir, path)
        .ok_or_else(|| ApiError::BadRequest("Invalid path".into()))?;
    if !target.is_file() {
        return Err(ApiError::NotFound);
    }
    Ok(target)
}

fn tempfile_path(ext: &str) -> PathBuf {
    let mut p = std::env::temp_dir();
    p.push(format!("clab-cc-{}.{ext}", uuid::Uuid::new_v4().simple()));
    p
}

fn video_response(bytes: Vec<u8>, filename: &str) -> Response {
    let len = bytes.len();
    (
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, "video/mp4".to_string()),
            (
                header::CONTENT_DISPOSITION,
                format!("attachment; filename=\"{filename}\""),
            ),
            (header::CONTENT_LENGTH, len.to_string()),
        ],
        Body::from(bytes),
    )
        .into_response()
}

fn zip_response(bytes: Vec<u8>, filename: &str) -> Response {
    let len = bytes.len();
    (
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, "application/zip".to_string()),
            (
                header::CONTENT_DISPOSITION,
                format!("attachment; filename=\"{filename}\""),
            ),
            (header::CONTENT_LENGTH, len.to_string()),
        ],
        Body::from(bytes),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn key_id_mapping() {
        assert_eq!(key_id_for("XAI_API_KEY"), "xai");
        assert_eq!(key_id_for("REPLICATE_API_TOKEN"), "replicate");
        assert_eq!(key_id_for("SOMETHING_ELSE"), "unknown");
    }

    #[test]
    fn confine_allows_clean_relative() {
        let dir = StdPath::new("/data/projects/p/videos");
        let r = confine_in_dir(dir, "clip_000.mp4").unwrap();
        assert_eq!(r, PathBuf::from("/data/projects/p/videos/clip_000.mp4"));
    }

    #[test]
    fn confine_rejects_traversal() {
        let dir = StdPath::new("/data/projects/p/videos");
        assert!(confine_in_dir(dir, "../../etc/passwd").is_none());
        assert!(confine_in_dir(dir, "../../../secrets").is_none());
    }

    #[test]
    fn confine_rejects_absolute_escape() {
        let dir = StdPath::new("/data/projects/p/videos");
        assert!(confine_in_dir(dir, "/etc/passwd").is_none());
    }

    #[test]
    fn pick_zip_name_dedupes_collisions() {
        let mut used = std::collections::HashSet::new();
        assert_eq!(pick_zip_name(&mut used, "clip", false), "clip.mp4");
        assert_eq!(pick_zip_name(&mut used, "clip", false), "clip_2.mp4");
        assert_eq!(pick_zip_name(&mut used, "clip", true), "clip_cc.mp4");
        assert_eq!(pick_zip_name(&mut used, "clip", true), "clip_cc_2.mp4");
    }

    #[test]
    fn zip_roundtrips_with_stdlib_reader() {
        let entries = vec![
            ("a.txt".to_string(), b"hello".to_vec()),
            ("b.txt".to_string(), b"world!!".to_vec()),
        ];
        let bytes = build_zip(&entries, &[]).unwrap();
        assert_eq!(&bytes[0..4], &0x04034b50u32.to_le_bytes());
        let eocd = 0x06054b50u32.to_le_bytes();
        assert!(bytes.windows(4).any(|w| w == eocd));
    }

    #[test]
    fn zip_with_errors_includes_errors_txt() {
        let bytes = build_zip(&[], &["boom: ffmpeg failed".to_string()]).unwrap();
        let haystack = String::from_utf8_lossy(&bytes);
        assert!(haystack.contains("_errors.txt"));
        assert!(haystack.contains("boom: ffmpeg failed"));
    }

    #[test]
    fn cc_default_detection() {
        assert!(cc_is_default(&None));
        assert!(cc_is_default(&Some(media::ColorCorrection::default())));
        let cc = media::ColorCorrection {
            brightness: 20.0,
            ..Default::default()
        };
        assert!(!cc_is_default(&Some(cc)));
    }
}
