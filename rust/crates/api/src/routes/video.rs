//! Video-generation routes — parity port of `routers/video.py` (940 lines in
//! Python), including the legacy runner this wave brings online:
//!
//!   - `POST /api/video/generate`         — the legacy multipart runner
//!     (prompt/provider/count/media → job_id). See "count fan-out" below.
//!   - `GET  /api/video/providers`        — configured-provider list (Python
//!     `PROVIDERS` shape: id/name/group/key_id/pricing/models)
//!   - `GET  /api/video/provider-schemas` — per-provider input-param schemas
//!     (verbatim port of Python's `PROVIDER_SCHEMAS` constant)
//!   - `GET  /api/video/jobs` + `GET /api/video/jobs/{id}` + `DELETE .../{id}`
//!     — reshapes `JobKind::Generate` row(s) into the old
//!     `{id, prompt, provider, count, videos: [...]}` job shape the frontend
//!     (`frontend/src/types/api.ts` `Job`/`VideoEntry`) expects
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
//! ## `count` fan-out (design decision)
//!
//! `Generate.tsx` submits ONE multipart request with a `count` field and then
//! polls ONE `job_id`, expecting `Job.videos` to grow to `count` entries with
//! independent per-index statuses. But `clab_core::model::Job` has exactly one
//! `result_asset_ids` list and one overall `status` — there is no schema for
//! "N sub-jobs under one parent" (confirmed against `core/src/model.rs`; adding
//! a parent/child job column would be a migration, out of scope this wave).
//!
//! This reuses the exact tagging trick `routes/recreate.rs` already uses to
//! group jobs without a schema change (see its `RECREATE_MARKER` / `params
//! LIKE` doc comment): each of the `count` requested videos becomes its own
//! real `JobKind::Generate` row, created and run through the *unchanged*
//! `run_generate_job` engine from `routes/jobs.rs`, but its `params` JSON
//! carries a `"batch_id"` (a fresh id invented here) and a `"batch_index"`.
//! The `job_id` returned to the frontend is that batch id, not any single job
//! row's id. `GET/DELETE /api/video/jobs/{id}` first try to resolve `{id}` as
//! a batch (N rows sharing that `batch_id`, ordered by `batch_index`) and fall
//! back to a plain single-job lookup (a job created directly via the older
//! `POST /api/jobs/generate` path has no batch tag and is still addressable by
//! its own row id, count=1). This gives real per-index granularity — each
//! sub-job's own `JobStatus` maps straight to a `VideoEntry.status` — instead
//! of the single synthetic "generating" placeholder this file previously
//! reported for the whole job.
//!
//! `/color-correct` uses `media::ffmpeg::color_correct` (source-framerate
//! preserving, `-c:a copy`, no forced `-r`) — the port of Python's
//! `STANDARD_ENCODE_ARGS` — NOT `burn_overlay`'s TikTok encode, so
//! color-correcting a generated video doesn't resample fps or re-encode audio.
//!
//! Model-specific fields the frontend sends per provider-schema (`num_frames`,
//! `sample_shift`, `sample_steps`, `go_fast`, `interpolate_output`,
//! `optimize_prompt`, `lora_*`, `negative_prompt`) are collected + type-coerced
//! into `GenParams.extra` and forwarded to the provider input builder (mirrors
//! the Python router's `extra` dict). `last_image` is parsed but inert — only
//! the dropped wan-i2v-fast model ever consumed it.

use std::path::{Path as StdPath, PathBuf};

use axum::body::Body;
use axum::extract::{Multipart, Path, Query, State};
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
        .route("/api/video/generate", post(generate_video))
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

// ── Legacy generate runner (POST /api/video/generate) ───────────────────────

/// Marker prefix embedded in a batch sub-job's `params` JSON — see the module
/// doc comment's "count fan-out" section. Kept as a plain substring (like
/// `recreate.rs`'s `RECREATE_MARKER`) so resolving a batch never depends on
/// SQLite's JSON1 extension being compiled into the sqlx build.
fn batch_marker(batch_id: &str) -> String {
    format!("\"batch_id\":\"{batch_id}\"")
}

/// 50MB base64 guard, shared by `media`/`last_image` — same ceiling
/// `start_generate` (routes/jobs.rs) and burn use for their data-URI inputs.
const MAX_IMAGE_B64_LEN: usize = 50 * 1024 * 1024;

/// Standard base64 encode (data URI payload) — avoids pulling the `base64`
/// crate for this one call site; mirrors the size of the hand-rolled decoder
/// `routes/jobs.rs` already carries for the opposite direction.
fn base64_encode(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let b0 = chunk[0];
        let b1 = *chunk.get(1).unwrap_or(&0);
        let b2 = *chunk.get(2).unwrap_or(&0);
        let n = ((b0 as u32) << 16) | ((b1 as u32) << 8) | (b2 as u32);
        out.push(ALPHABET[(n >> 18 & 0x3F) as usize] as char);
        out.push(ALPHABET[(n >> 12 & 0x3F) as usize] as char);
        out.push(if chunk.len() > 1 { ALPHABET[(n >> 6 & 0x3F) as usize] as char } else { '=' });
        out.push(if chunk.len() > 2 { ALPHABET[(n & 0x3F) as usize] as char } else { '=' });
    }
    out
}

/// One parsed multipart request. `media`/`last_image` are files; everything else
/// arrives as text. `media` becomes `GenParams.image_data_uri`; the model-specific
/// knobs land type-coerced in `extra` (forwarded to the provider builder).
/// `last_image` is read but inert — only the dropped wan-i2v-fast model used it.
#[derive(Default)]
struct GenerateForm {
    prompt: Option<String>,
    provider: Option<String>,
    project: Option<String>,
    count: Option<String>,
    duration: Option<String>,
    resolution: Option<String>,
    aspect_ratio: Option<String>,
    crop_mode: Option<String>,
    media_data_uri: Option<String>,
    /// Model-specific knobs (num_frames, sample_shift, negative_prompt, lora_*, …)
    /// collected from the multipart form and forwarded to the provider builder.
    extra: serde_json::Map<String, Value>,
}

/// The model-specific fields the frontend sends per provider-schema. Anything
/// not in this list is ignored (never 422s). `last_image` is deliberately absent
/// — only the dropped wan-i2v-fast model consumed it.
const EXTRA_PARAM_KEYS: &[&str] = &[
    "num_frames",
    "frames_per_second",
    "sample_shift",
    "sample_steps",
    "go_fast",
    "interpolate_output",
    "optimize_prompt",
    "negative_prompt",
    "lora_weights_transformer",
    "lora_scale_transformer",
    "lora_weights_transformer_2",
    "lora_scale_transformer_2",
];

/// Coerce a multipart text value to the JSON type the Replicate input expects.
/// The frontend sends everything as `String(val)`, so a numeric-looking negative
/// prompt must stay a string — hence per-key typing rather than a blind JSON parse.
fn coerce_extra(key: &str, text: &str) -> Option<Value> {
    let t = text.trim();
    if t.is_empty() {
        return None;
    }
    match key {
        "num_frames" | "frames_per_second" | "sample_shift" | "sample_steps" => {
            t.parse::<i64>().ok().map(Value::from)
        }
        "lora_scale_transformer" | "lora_scale_transformer_2" => {
            t.parse::<f64>().ok().map(Value::from)
        }
        "go_fast" | "interpolate_output" | "optimize_prompt" => match t {
            "true" | "1" | "on" | "yes" => Some(Value::Bool(true)),
            "false" | "0" | "off" | "no" => Some(Value::Bool(false)),
            _ => None,
        },
        // Everything else (negative_prompt, lora_weights_transformer*) is a string.
        _ => Some(Value::String(t.to_string())),
    }
}

async fn parse_generate_multipart(mut mp: Multipart) -> Result<GenerateForm, ApiError> {
    let mut form = GenerateForm::default();
    loop {
        let field = mp
            .next_field()
            .await
            .map_err(|e| ApiError::BadRequest(format!("invalid multipart body: {e}")))?;
        let Some(field) = field else { break };
        let name = field.name().unwrap_or("").to_string();
        match name.as_str() {
            "media" | "last_image" => {
                let content_type = field.content_type().unwrap_or("application/octet-stream").to_string();
                let bytes = field
                    .bytes()
                    .await
                    .map_err(|e| ApiError::BadRequest(format!("failed to read {name}: {e}")))?;
                if bytes.is_empty() {
                    continue;
                }
                // Guard against an oversized upload before it's base64-inflated further.
                if bytes.len() > MAX_IMAGE_B64_LEN {
                    return Err(ApiError::BadRequest(format!("{name} too large")));
                }
                let uri = format!("data:{content_type};base64,{}", base64_encode(&bytes));
                if uri.len() > MAX_IMAGE_B64_LEN {
                    return Err(ApiError::BadRequest(format!("{name} too large")));
                }
                if name == "media" {
                    form.media_data_uri = Some(uri);
                }
                // last_image is accepted but has nowhere to go yet (see doc comment).
            }
            _ => {
                let text = field
                    .text()
                    .await
                    .map_err(|e| ApiError::BadRequest(format!("failed to read field {name}: {e}")))?;
                match name.as_str() {
                    "prompt" => form.prompt = Some(text),
                    "provider" => form.provider = Some(text),
                    "project" => form.project = Some(text),
                    "count" => form.count = Some(text),
                    "duration" => form.duration = Some(text),
                    "resolution" => form.resolution = Some(text),
                    "aspect_ratio" => form.aspect_ratio = Some(text),
                    "crop_mode" => form.crop_mode = Some(text),
                    // Model-specific extras (num_frames, sample_shift, go_fast, ...)
                    // are collected + type-coerced, then forwarded to the provider
                    // input builder. Unknown fields are ignored (never 422).
                    other => {
                        if EXTRA_PARAM_KEYS.contains(&other) {
                            if let Some(v) = coerce_extra(other, &text) {
                                form.extra.insert(other.to_string(), v);
                            }
                        }
                    }
                }
            }
        }
    }
    Ok(form)
}

/// `POST /api/video/generate` — the legacy runner. Multipart form → `count`
/// distinct generate jobs (see module doc's "count fan-out"), returns
/// `{"job_id": "<batch_id>"}` immediately; the frontend polls
/// `GET /api/video/jobs/{job_id}`.
async fn generate_video(State(st): State<AppState>, multipart: Multipart) -> Result<Json<Value>, ApiError> {
    let form = parse_generate_multipart(multipart).await?;

    let prompt = form.prompt.unwrap_or_default();
    if prompt.trim().is_empty() {
        return Err(ApiError::BadRequest("prompt is required".into()));
    }
    let provider_id = form.provider.unwrap_or_default();
    let model = crate::providers::find_model(&provider_id)
        .ok_or_else(|| ApiError::BadRequest(format!("unknown model: {provider_id}")))?;
    if model.needs_image && form.media_data_uri.is_none() {
        return Err(ApiError::BadRequest(format!(
            "{} is image-to-video — an image is required",
            model.name
        )));
    }
    let project_input = form.project.filter(|p| !p.is_empty()).unwrap_or_else(default_project);
    let project = crate::paths::sanitize_project_name(&project_input)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;
    let count: u32 = form
        .count
        .as_deref()
        .and_then(|s| s.parse().ok())
        .unwrap_or(1)
        .clamp(1, 20);
    let duration: u32 = form.duration.as_deref().and_then(|s| s.parse().ok()).unwrap_or(10);
    let resolution = form.resolution.unwrap_or_else(|| "720p".to_string());
    let aspect_ratio = form.aspect_ratio.unwrap_or_else(|| "9:16".to_string());
    let crop_mode = match form.crop_mode.as_deref() {
        None => media::CropMode::Single,
        Some(s) => media::CropMode::parse(s)
            .ok_or_else(|| ApiError::BadRequest(format!("invalid crop_mode: {s}")))?,
    };

    // Build the provider now so a missing API key fails fast (before any job rows).
    crate::providers::provider_for(model)
        .ok_or_else(|| ApiError::BadRequest(format!("{} not configured (set {})", model.name, model.key_env)))?;

    repo::create_project(&st.db, &project).await?;

    let batch_id = uuid::Uuid::new_v4().to_string();

    // Save one prompt-history entry for the whole batch (matches the old
    // Python behavior: one history row per submit, not one per video).
    let history = prompts_repo::PromptEntry {
        prompt: prompt.clone(),
        provider: provider_id.clone(),
        count,
        duration,
        aspect_ratio: aspect_ratio.clone(),
        resolution: resolution.clone(),
        negative_prompt: None,
        has_media: form.media_data_uri.is_some(),
        job_id: batch_id.clone(),
        timestamp: chrono::Utc::now().to_rfc3339(),
    };
    let _ = prompts_repo::save_prompt(&st.db, &project, history).await;

    for index in 0..count {
        // A fresh provider instance per sub-job (the trait object above was
        // only for the up-front key check) — `provider_for` is cheap (reads
        // the API key from env), so this rebuild is not worth threading a
        // `Box<dyn Provider>` through `count` spawned tasks.
        let provider = crate::providers::provider_for(model).ok_or_else(|| {
            ApiError::BadRequest(format!("{} not configured (set {})", model.name, model.key_env))
        })?;

        let meta = json!({
            "model": model.id,
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "duration": duration,
            "batch_id": batch_id,
            "batch_index": index,
            "batch_count": count,
            "crop_mode": crop_mode.as_str(),
        })
        .to_string();
        let asset = repo::create_asset(&st.db, &project, clab_core::model::AssetKind::Generated, None, Some(&meta)).await?;
        let job = repo::create_job(&st.db, &project, JobKind::Generate, Some(&asset.id), Some(&meta)).await?;

        let db = st.db.clone();
        let job_id = job.id.clone();
        let asset_id = asset.id.clone();
        let model_id = model.model_id.to_string();
        let gen_params = crate::providers::GenParams {
            prompt: prompt.clone(),
            aspect_ratio: aspect_ratio.clone(),
            resolution: resolution.clone(),
            duration,
            image_data_uri: form.media_data_uri.clone(),
            extra: form.extra.clone(),
        };
        let project_owned = project.clone();
        let batch_id_for_log = batch_id.clone();
        tokio::spawn(async move {
            if let Err(e) = crate::routes::jobs::run_generate_job(
                db.clone(),
                &job_id,
                &project_owned,
                &asset_id,
                provider,
                &model_id,
                gen_params,
                crop_mode,
            )
            .await
            {
                tracing::error!("generate job {job_id} (batch {batch_id_for_log}) failed: {e:#}");
                let _ = repo::mark_asset_failed(&db, &asset_id, &format!("{e:#}")).await;
                let _ = repo::fail_job(&db, &job_id, &format!("{e:#}")).await;
            }
        });
    }

    Ok(Json(json!({ "job_id": batch_id })))
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

/// Reshape ONE sub-job's result assets into its `VideoEntry` (per the frontend
/// `VideoEntry` type: `index/status/file/url/error/crops`). Asset ordering
/// from `run_generate_job` is `[source, crop_0, crop_1, ...]` — the first
/// result asset is the video itself; any further ones are the multi-crop
/// fan-out and land in `crops` (matching `CropEntry {file, url}`).
async fn sub_job_video_entry(db: &clab_core::Db, index: usize, job: &clab_core::model::Job) -> Value {
    match job.status {
        JobStatus::Done => {
            let result_ids: Vec<String> = serde_json::from_str(&job.result_asset_ids).unwrap_or_default();
            let Some(source_id) = result_ids.first() else {
                return json!({"index": index, "status": "error", "error": "no output produced"});
            };
            let source = repo::get_asset(db, source_id).await.ok().flatten();
            let file = source.as_ref().and_then(|a| a.path.clone());
            let mut entry = json!({"index": index, "status": "done"});
            if let Some(f) = &file {
                entry["file"] = json!(f);
                entry["url"] = json!(format!("/{f}"));
            }
            if result_ids.len() > 1 {
                let mut crops = Vec::with_capacity(result_ids.len() - 1);
                for crop_id in &result_ids[1..] {
                    if let Some(asset) = repo::get_asset(db, crop_id).await.ok().flatten() {
                        if let Some(p) = asset.path {
                            crops.push(json!({"file": p, "url": format!("/{p}")}));
                        }
                    }
                }
                if !crops.is_empty() {
                    entry["crops"] = json!(crops);
                }
            }
            entry
        }
        JobStatus::Failed => json!({
            "index": index,
            "status": "error",
            "error": job.error.clone().unwrap_or_else(|| "generation failed".to_string()),
        }),
        // Queued/Processing both map to the frontend's "generating" status —
        // `VideoEntry` also has `polling`/`downloading` for finer old-Python
        // granularity, but the durable job row only tracks one progress float
        // (see `run_generate_job`), not a named phase. "generating" is enough
        // for `isTerminal` (which only branches on done/failed/error).
        JobStatus::Queued | JobStatus::Processing => json!({"index": index, "status": "generating"}),
    }
}

/// Extract `"batch_id"` from a sub-job's `params` JSON, if present.
fn batch_id_of(job: &clab_core::model::Job) -> Option<String> {
    job.params
        .as_deref()
        .and_then(|s| serde_json::from_str::<Value>(s).ok())
        .and_then(|v| v.get("batch_id").and_then(|b| b.as_str()).map(str::to_string))
}

/// Fetch every job row sharing `batch_id`, ordered by `batch_index`. Uses the
/// same `params LIKE` substring match `recreate.rs` established for grouping
/// jobs without a schema change (see module doc's "count fan-out").
async fn fetch_batch_jobs(
    db: &clab_core::Db,
    project: &str,
    batch_id: &str,
) -> Result<Vec<clab_core::model::Job>, sqlx::Error> {
    let mut jobs: Vec<clab_core::model::Job> = sqlx::query_as(
        "SELECT * FROM jobs WHERE project = ? AND kind = 'generate' AND params LIKE ? ORDER BY created_at ASC",
    )
    .bind(project)
    .bind(format!("%{}%", batch_marker(batch_id)))
    .fetch_all(db.reader())
    .await?;
    jobs.sort_by_key(|j| {
        j.params
            .as_deref()
            .and_then(|s| serde_json::from_str::<Value>(s).ok())
            .and_then(|v| v.get("batch_index").and_then(|i| i.as_u64()))
            .unwrap_or(0)
    });
    Ok(jobs)
}

/// Reshape a batch (one or more sub-jobs sharing a `batch_id`) into the old
/// `{id, prompt, provider, count, project, crop_mode, created_at, videos}`
/// shape the frontend `Job` type expects. `jobs` must be non-empty and already
/// ordered by `batch_index`.
async fn batch_to_job_json(db: &clab_core::Db, batch_id: &str, jobs: &[clab_core::model::Job]) -> Value {
    let first = &jobs[0];
    let params: Option<Value> = first.params.as_deref().and_then(|s| serde_json::from_str(s).ok());
    let prompt = params.as_ref().and_then(|v| v.get("prompt").and_then(|p| p.as_str())).unwrap_or_default();
    // `params["model"]` is written as the public registry id (`model.id`,
    // e.g. "grok") by both this file and `routes/jobs.rs`'s `start_generate` —
    // pass it straight through as `Job.provider`. The registry lookup is a
    // defensive fallback only (in case a row's `model` ever holds the
    // provider-specific `model_id` instead, e.g. "minimax/hailuo-2.3").
    let model_field = params.as_ref().and_then(|v| v.get("model").and_then(|p| p.as_str())).unwrap_or_default();
    let provider = crate::providers::MODELS
        .iter()
        .find(|m| m.id == model_field || m.model_id == model_field)
        .map(|m| m.id)
        .unwrap_or(model_field);
    // Frontend `Job.crop_mode` is `CropMode | null` where `CropMode` excludes
    // "single" (no-crop) — only surface it when a real multi-crop mode was
    // requested, same as the Python UI never sending `crop_mode: "none"`.
    let crop_mode = params.as_ref().and_then(|v| v.get("crop_mode").and_then(|c| c.as_str()));
    let crop_mode = crop_mode.filter(|m| *m != "single" && *m != "none");

    let mut videos = Vec::with_capacity(jobs.len());
    for (i, job) in jobs.iter().enumerate() {
        videos.push(sub_job_video_entry(db, i, job).await);
    }

    let created_at = jobs.iter().map(|j| j.created_at).min().unwrap_or(first.created_at);

    json!({
        "id": batch_id,
        "prompt": prompt,
        "provider": provider,
        "count": videos.len(),
        "crop_mode": crop_mode,
        "project": first.project,
        "created_at": created_at.to_rfc3339(),
        "videos": videos,
    })
}

/// `GET /api/video/jobs?project=...` — list generate jobs for a project, one
/// entry per batch (a legacy single job with no batch tag is its own
/// one-video batch).
async fn list_jobs(
    State(st): State<AppState>,
    Query(q): Query<ProjectQuery>,
) -> Result<Json<Vec<Value>>, ApiError> {
    let jobs = repo::list_jobs(&st.db, &q.project).await?;
    // Group by batch_id (falling back to the job's own id for un-batched
    // generate jobs), preserving first-seen order (list_jobs is newest-first).
    let mut order: Vec<String> = Vec::new();
    let mut groups: std::collections::HashMap<String, Vec<clab_core::model::Job>> = std::collections::HashMap::new();
    for job in jobs.into_iter().filter(|j| j.kind == JobKind::Generate) {
        let key = batch_id_of(&job).unwrap_or_else(|| job.id.clone());
        if !groups.contains_key(&key) {
            order.push(key.clone());
        }
        groups.entry(key).or_default().push(job);
    }

    let mut out = Vec::with_capacity(order.len());
    for key in order {
        let mut jobs = groups.remove(&key).unwrap_or_default();
        jobs.sort_by_key(|j| {
            j.params
                .as_deref()
                .and_then(|s| serde_json::from_str::<Value>(s).ok())
                .and_then(|v| v.get("batch_index").and_then(|i| i.as_u64()))
                .unwrap_or(0)
        });
        out.push(batch_to_job_json(&st.db, &key, &jobs).await);
    }
    Ok(Json(out))
}

/// `GET /api/video/jobs/{job_id}` — fetch a single generate job (or batch) by
/// id. Tries the batch-id path first (the id this file hands back from
/// `POST /api/video/generate`); falls back to a plain single-job lookup for
/// ids that came from the older `POST /api/jobs/generate` path.
async fn get_job(State(st): State<AppState>, Path(job_id): Path<String>) -> Result<Json<Value>, ApiError> {
    // Batch lookup needs a project to scope the LIKE query; resolve it via
    // any one job carrying this batch_id, tried across all projects the
    // simplest possible way: check the job itself first if job_id happens to
    // BE a row id (covers the single-job fallback and lets us read its
    // project), else fall back to a cross-project batch scan.
    if let Some(job) = repo::get_job(&st.db, &job_id).await? {
        if job.kind != JobKind::Generate {
            return Err(ApiError::NotFound);
        }
        // This row might itself be one sub-job of a batch (its own id ≠ the
        // batch id in that case) — resolve the full batch via its project.
        let jobs = if let Some(batch_id) = batch_id_of(&job) {
            fetch_batch_jobs(&st.db, &job.project, &batch_id).await?
        } else {
            vec![job.clone()]
        };
        let batch_id = batch_id_of(&job).unwrap_or_else(|| job.id.clone());
        return Ok(Json(batch_to_job_json(&st.db, &batch_id, &jobs).await));
    }

    // job_id wasn't a row id — try it as a batch_id across every project.
    let jobs: Vec<clab_core::model::Job> = sqlx::query_as(
        "SELECT * FROM jobs WHERE kind = 'generate' AND params LIKE ? ORDER BY created_at ASC",
    )
    .bind(format!("%{}%", batch_marker(&job_id)))
    .fetch_all(st.db.reader())
    .await
    .map_err(ApiError::from)?;
    if jobs.is_empty() {
        return Err(ApiError::NotFound);
    }
    let mut jobs = jobs;
    jobs.sort_by_key(|j| {
        j.params
            .as_deref()
            .and_then(|s| serde_json::from_str::<Value>(s).ok())
            .and_then(|v| v.get("batch_index").and_then(|i| i.as_u64()))
            .unwrap_or(0)
    });
    Ok(Json(batch_to_job_json(&st.db, &job_id, &jobs).await))
}

/// `DELETE /api/video/jobs/{job_id}` — delete a generate job (or every
/// sub-job in a batch) and the video files their result assets point to
/// (confined under the project's video dir, same containment rule as
/// `DELETE /api/video/file`). Row deletes go through
/// `repo::video::delete_job_row` (this module's own repo file) rather than the
/// shared job-lifecycle functions in `repo::mod`, which only support
/// create/update/finish/fail — not delete.
async fn delete_job(
    State(st): State<AppState>,
    Path(job_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    // Resolve the same way get_job does: row id first, else batch_id.
    let jobs: Vec<clab_core::model::Job> = if let Some(job) = repo::get_job(&st.db, &job_id).await? {
        if job.kind != JobKind::Generate {
            return Err(ApiError::NotFound);
        }
        match batch_id_of(&job) {
            Some(batch_id) => fetch_batch_jobs(&st.db, &job.project, &batch_id).await?,
            None => vec![job],
        }
    } else {
        sqlx::query_as::<_, clab_core::model::Job>(
            "SELECT * FROM jobs WHERE kind = 'generate' AND params LIKE ?",
        )
        .bind(format!("%{}%", batch_marker(&job_id)))
        .fetch_all(st.db.reader())
        .await
        .map_err(ApiError::from)?
    };
    if jobs.is_empty() {
        return Err(ApiError::NotFound);
    }

    let mut files_removed = 0u32;
    for job in &jobs {
        let result_ids: Vec<String> = serde_json::from_str(&job.result_asset_ids).unwrap_or_default();
        for asset_id in &result_ids {
            if let Some(asset) = repo::get_asset(&st.db, asset_id).await? {
                if let Some(rel) = &asset.path {
                    if let Some(abs) = confine_project_video(&job.project, rel) {
                        if tokio::fs::remove_file(&abs).await.is_ok() {
                            files_removed += 1;
                        }
                    }
                }
            }
        }
        prompts_repo::delete_job_row(&st.db, &job.id).await?;
    }

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
    let target = confine_project_video(&q.project, &q.path)
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
    let cc_filter = media::build_cc_filter(cc.as_ref(), None); // "null" = ffmpeg no-op
    let tmp = tempfile_path("mp4");
    // Source-preserving encode (keeps fps, copies audio) — NOT burn_overlay's
    // TikTok encode, which would silently resample fps + re-encode audio to AAC.
    let result = media::ffmpeg::color_correct(src, &cc_filter, &tmp, info.duration_secs, |_| {}).await;
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

/// Confine a frontend-supplied video `path` and require it to live under THIS
/// project. Stored video paths are DATA-DIR-relative and land in several
/// subdirs — `projects/<p>/generated/<job>/video.mp4` (generate),
/// `projects/<p>/clips/<job>/...` (crops), `projects/<p>/burned/...` (burns),
/// `projects/<p>/videos/...` — NOT just `videos/`. The previous helper joined the
/// path under `<project>/videos` only, which double-prefixed `projects/<p>/` and
/// rejected/skipped every real generated or clip path (per-tile Delete 400'd,
/// delete_job orphaned files, color-correct 400'd on generated videos).
fn confine_project_video(project: &str, path: &str) -> Option<PathBuf> {
    confine_project_video_in(&data_dir(), project, path)
}

/// Pure core of `confine_project_video` (data dir injected, so it's unit-testable
/// without touching process env).
fn confine_project_video_in(data: &str, project: &str, path: &str) -> Option<PathBuf> {
    let project = crate::paths::sanitize_project_name(project)?;
    // Accept an absolute-under-data path (uploaded sources) or a relative one.
    let rel = crate::paths::relativize_under_data(data, path).ok()?;
    // Scope to this project's dir — checked on the normalized RELATIVE form so
    // it's symlink-immune (comparing canonicalized abs paths against a
    // non-canonical project root would spuriously fail on symlinked data dirs).
    let norm_rel = normalize_lexically(StdPath::new(&rel));
    if !norm_rel.starts_with(PathBuf::from("projects").join(&project)) {
        return None;
    }
    // confine_under_data does the traversal/absolute-escape rejection + returns abs.
    crate::paths::confine_under_data(data, &rel).ok()
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
    let target = confine_project_video(project, path)
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
    fn confine_project_video_accepts_all_subdirs() {
        // The bug this fixes: generated/clip/burned paths (not just videos/) must resolve.
        for rel in [
            "projects/p/generated/job1/video.mp4",
            "projects/p/clips/job1/clip_000.mp4",
            "projects/p/burned/job1/burned_000.mp4",
            "projects/p/videos/clip_000.mp4",
        ] {
            let r = confine_project_video_in("/data", "p", rel)
                .unwrap_or_else(|| panic!("should accept {rel}"));
            assert_eq!(r, PathBuf::from(format!("/data/{rel}")));
        }
    }

    #[test]
    fn confine_project_video_rejects_cross_project_and_traversal() {
        // Can't delete another project's files even with a valid-looking path.
        assert!(confine_project_video_in("/data", "p", "projects/other/videos/x.mp4").is_none());
        // Traversal + absolute escape still blocked.
        assert!(confine_project_video_in("/data", "p", "projects/p/../../etc/passwd").is_none());
        assert!(confine_project_video_in("/data", "p", "/etc/passwd").is_none());
    }

    #[test]
    fn coerce_extra_types_each_key_correctly() {
        assert_eq!(coerce_extra("num_frames", "121"), Some(Value::from(121)));
        assert_eq!(coerce_extra("lora_scale_transformer", "0.8"), Some(Value::from(0.8)));
        assert_eq!(coerce_extra("go_fast", "true"), Some(Value::Bool(true)));
        assert_eq!(coerce_extra("interpolate_output", "false"), Some(Value::Bool(false)));
        // The critical case: a numeric-looking text field must NOT become a number.
        assert_eq!(coerce_extra("negative_prompt", "123"), Some(Value::from("123")));
        assert_eq!(coerce_extra("negative_prompt", "no morphing"), Some(Value::from("no morphing")));
        // Empty → None (omitted, matches Python's "skip if empty").
        assert_eq!(coerce_extra("negative_prompt", "  "), None);
        // Garbage in a numeric field → None (omitted, builder uses its default).
        assert_eq!(coerce_extra("num_frames", "abc"), None);
    }

    #[test]
    fn confine_project_video_accepts_absolute_under_data() {
        // Uploaded-source case: absolute path under the data dir is relativized.
        let r = confine_project_video_in("/app/data", "p", "/app/data/projects/p/generated/j/v.mp4");
        assert_eq!(r, Some(PathBuf::from("/app/data/projects/p/generated/j/v.mp4")));
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

    #[test]
    fn base64_encode_known_vectors() {
        // Standard RFC 4648 test vectors.
        assert_eq!(base64_encode(b""), "");
        assert_eq!(base64_encode(b"f"), "Zg==");
        assert_eq!(base64_encode(b"fo"), "Zm8=");
        assert_eq!(base64_encode(b"foo"), "Zm9v");
        assert_eq!(base64_encode(b"foobar"), "Zm9vYmFy");
    }

    #[test]
    fn batch_marker_is_substring_findable() {
        let m = batch_marker("abc-123");
        assert_eq!(m, "\"batch_id\":\"abc-123\"");
        let params = format!("{{\"model\":\"grok\",{m},\"batch_index\":0}}");
        assert!(params.contains(&batch_marker("abc-123")));
        assert!(!params.contains(&batch_marker("other-batch")));
    }

    #[test]
    fn batch_id_of_reads_params() {
        let job = clab_core::model::Job {
            id: "j1".into(),
            project: "p".into(),
            kind: JobKind::Generate,
            status: JobStatus::Done,
            progress: 1.0,
            source_asset_id: None,
            params: Some(r#"{"model":"grok","batch_id":"batch-1","batch_index":0}"#.to_string()),
            result_asset_ids: "[]".to_string(),
            error: None,
            created_at: chrono::Utc::now(),
            updated_at: chrono::Utc::now(),
        };
        assert_eq!(batch_id_of(&job), Some("batch-1".to_string()));

        let untagged = clab_core::model::Job {
            params: Some(r#"{"model":"grok"}"#.to_string()),
            ..job
        };
        assert_eq!(batch_id_of(&untagged), None);
    }

    // ── router-level tests (batch aggregation over real job rows) ──────────

    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use tower::ServiceExt;

    async fn test_state() -> AppState {
        let path = std::env::temp_dir().join(format!("video-test-{}.db", uuid::Uuid::new_v4()));
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
            serde_json::from_slice(&bytes).unwrap_or(Value::Null)
        };
        (status, body)
    }

    fn get_req(uri: &str) -> Request<Body> {
        Request::builder().uri(uri).body(Body::empty()).unwrap()
    }

    /// Insert a generate job row directly (bypassing `repo::create_job`,
    /// which doesn't know about the batch marker) so tests can simulate the
    /// `count` fan-out without spinning up a real provider.
    #[allow(clippy::too_many_arguments)]
    async fn insert_generate_job(
        st: &AppState,
        id: &str,
        project: &str,
        status: &str,
        result_asset_ids: &str,
        error: Option<&str>,
        params: &str,
    ) {
        sqlx::query(
            "INSERT INTO jobs (id, project, kind, status, progress, source_asset_id, params, result_asset_ids, error, created_at, updated_at)
             VALUES (?, ?, 'generate', ?, 1.0, NULL, ?, ?, ?, ?, ?)",
        )
        .bind(id)
        .bind(project)
        .bind(status)
        .bind(params)
        .bind(result_asset_ids)
        .bind(error)
        .bind(chrono::Utc::now())
        .bind(chrono::Utc::now())
        .execute(st.db.writer())
        .await
        .unwrap();
    }

    async fn insert_ready_asset(st: &AppState, id: &str, project: &str, path: &str) {
        sqlx::query(
            "INSERT INTO assets (id, project, kind, status, path, parent_id, meta, error, created_at, updated_at)
             VALUES (?, ?, 'generated', 'ready', ?, NULL, NULL, NULL, ?, ?)",
        )
        .bind(id)
        .bind(project)
        .bind(path)
        .bind(chrono::Utc::now())
        .bind(chrono::Utc::now())
        .execute(st.db.writer())
        .await
        .unwrap();
    }

    #[tokio::test]
    async fn get_job_aggregates_batch_into_one_job_with_per_index_videos() {
        let st = test_state().await;
        repo::create_project(&st.db, "p").await.unwrap();
        insert_ready_asset(&st, "a0", "p", "projects/p/generated/j0/video.mp4").await;
        insert_ready_asset(&st, "a1", "p", "projects/p/generated/j1/video.mp4").await;

        let params0 = r#"{"model":"grok","prompt":"a cat","batch_id":"batch-1","batch_index":0,"batch_count":2}"#;
        let params1 = r#"{"model":"grok","prompt":"a cat","batch_id":"batch-1","batch_index":1,"batch_count":2}"#;
        insert_generate_job(&st, "j0", "p", "done", r#"["a0"]"#, None, params0).await;
        insert_generate_job(&st, "j1", "p", "processing", "[]", None, params1).await;

        // Fetch by the batch id (what POST /api/video/generate hands back).
        let (status, body) = call(&st, get_req("/api/video/jobs/batch-1")).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["id"], "batch-1");
        assert_eq!(body["prompt"], "a cat");
        assert_eq!(body["provider"], "grok");
        assert_eq!(body["count"], 2);
        let videos = body["videos"].as_array().unwrap();
        assert_eq!(videos.len(), 2);
        assert_eq!(videos[0]["index"], 0);
        assert_eq!(videos[0]["status"], "done");
        assert_eq!(videos[0]["file"], "projects/p/generated/j0/video.mp4");
        assert_eq!(videos[0]["url"], "/projects/p/generated/j0/video.mp4");
        assert_eq!(videos[1]["index"], 1);
        assert_eq!(videos[1]["status"], "generating");

        // Also fetchable via any one sub-job's own row id (resolves to the
        // same aggregated batch).
        let (status2, body2) = call(&st, get_req("/api/video/jobs/j0")).await;
        assert_eq!(status2, StatusCode::OK);
        assert_eq!(body2["id"], "batch-1");
        assert_eq!(body2["videos"].as_array().unwrap().len(), 2);
    }

    #[tokio::test]
    async fn get_job_reports_error_status_for_failed_sub_job() {
        let st = test_state().await;
        repo::create_project(&st.db, "p").await.unwrap();
        let params = r#"{"model":"grok","prompt":"x","batch_id":"batch-e","batch_index":0,"batch_count":1}"#;
        insert_generate_job(&st, "jf", "p", "failed", "[]", Some("provider exploded"), params).await;

        let (status, body) = call(&st, get_req("/api/video/jobs/batch-e")).await;
        assert_eq!(status, StatusCode::OK);
        let videos = body["videos"].as_array().unwrap();
        assert_eq!(videos.len(), 1);
        assert_eq!(videos[0]["status"], "error");
        assert_eq!(videos[0]["error"], "provider exploded");
    }

    #[tokio::test]
    async fn get_job_surfaces_multi_crop_fanout_as_crops() {
        let st = test_state().await;
        repo::create_project(&st.db, "p").await.unwrap();
        insert_ready_asset(&st, "src", "p", "projects/p/generated/j0/video.mp4").await;
        insert_ready_asset(&st, "crop0", "p", "projects/p/clips/j0/crop_0.mp4").await;
        insert_ready_asset(&st, "crop1", "p", "projects/p/clips/j0/crop_1.mp4").await;
        let params = r#"{"model":"hailuo","prompt":"x","batch_id":"batch-c","batch_index":0,"batch_count":1}"#;
        insert_generate_job(&st, "j0", "p", "done", r#"["src","crop0","crop1"]"#, None, params).await;

        let (status, body) = call(&st, get_req("/api/video/jobs/batch-c")).await;
        assert_eq!(status, StatusCode::OK);
        let videos = body["videos"].as_array().unwrap();
        assert_eq!(videos.len(), 1);
        assert_eq!(videos[0]["file"], "projects/p/generated/j0/video.mp4");
        let crops = videos[0]["crops"].as_array().unwrap();
        assert_eq!(crops.len(), 2);
        assert_eq!(crops[0]["file"], "projects/p/clips/j0/crop_0.mp4");
        assert_eq!(crops[0]["url"], "/projects/p/clips/j0/crop_0.mp4");
    }

    #[tokio::test]
    async fn get_job_404_for_unknown_id() {
        let st = test_state().await;
        let (status, _) = call(&st, get_req("/api/video/jobs/nope")).await;
        assert_eq!(status, StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn list_jobs_groups_by_batch_and_scopes_to_project() {
        let st = test_state().await;
        repo::create_project(&st.db, "p").await.unwrap();
        repo::create_project(&st.db, "other").await.unwrap();
        let p0 = r#"{"model":"grok","prompt":"a","batch_id":"b1","batch_index":0,"batch_count":2}"#;
        let p1 = r#"{"model":"grok","prompt":"a","batch_id":"b1","batch_index":1,"batch_count":2}"#;
        insert_generate_job(&st, "j0", "p", "done", "[]", None, p0).await;
        insert_generate_job(&st, "j1", "p", "done", "[]", None, p1).await;
        // A legacy single (un-batched) job in the same project.
        insert_generate_job(&st, "solo", "p", "done", "[]", None, r#"{"model":"grok","prompt":"b"}"#).await;
        // A job in a different project must not leak in.
        insert_generate_job(&st, "elsewhere", "other", "done", "[]", None, r#"{"model":"grok","prompt":"c"}"#).await;

        let (status, body) = call(&st, get_req("/api/video/jobs?project=p")).await;
        assert_eq!(status, StatusCode::OK);
        let jobs = body.as_array().unwrap();
        assert_eq!(jobs.len(), 2, "batch b1 collapses to one entry, plus the solo job");
        let ids: Vec<&str> = jobs.iter().map(|j| j["id"].as_str().unwrap()).collect();
        assert!(ids.contains(&"b1"));
        assert!(ids.contains(&"solo"));
        let batch_entry = jobs.iter().find(|j| j["id"] == "b1").unwrap();
        assert_eq!(batch_entry["count"], 2);
    }

    #[tokio::test]
    async fn delete_job_removes_every_sub_job_in_a_batch() {
        let st = test_state().await;
        repo::create_project(&st.db, "p").await.unwrap();
        let p0 = r#"{"model":"grok","prompt":"a","batch_id":"b-del","batch_index":0,"batch_count":2}"#;
        let p1 = r#"{"model":"grok","prompt":"a","batch_id":"b-del","batch_index":1,"batch_count":2}"#;
        insert_generate_job(&st, "d0", "p", "done", "[]", None, p0).await;
        insert_generate_job(&st, "d1", "p", "done", "[]", None, p1).await;

        let (status, body) = call(
            &st,
            Request::builder()
                .method("DELETE")
                .uri("/api/video/jobs/b-del")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["deleted"], true);

        assert!(repo::get_job(&st.db, "d0").await.unwrap().is_none());
        assert!(repo::get_job(&st.db, "d1").await.unwrap().is_none());

        // Second delete of the same (now-gone) batch id 404s.
        let (status2, _) = call(
            &st,
            Request::builder()
                .method("DELETE")
                .uri("/api/video/jobs/b-del")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(status2, StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn generate_video_rejects_missing_prompt() {
        let st = test_state().await;
        let body_bytes = b"--X\r\nContent-Disposition: form-data; name=\"provider\"\r\n\r\ngrok\r\n--X--\r\n".to_vec();
        let req = Request::builder()
            .method("POST")
            .uri("/api/video/generate")
            .header("content-type", "multipart/form-data; boundary=X")
            .body(Body::from(body_bytes))
            .unwrap();
        let (status, body) = call(&st, req).await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert!(body["error"].as_str().unwrap().contains("prompt"));
    }

    #[tokio::test]
    async fn generate_video_rejects_unknown_provider() {
        let st = test_state().await;
        let body_bytes = b"--X\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\nhello\r\n--X\r\nContent-Disposition: form-data; name=\"provider\"\r\n\r\nnope-not-real\r\n--X--\r\n".to_vec();
        let req = Request::builder()
            .method("POST")
            .uri("/api/video/generate")
            .header("content-type", "multipart/form-data; boundary=X")
            .body(Body::from(body_bytes))
            .unwrap();
        let (status, body) = call(&st, req).await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert!(body["error"].as_str().unwrap().contains("unknown model"));
    }

    #[tokio::test]
    async fn generate_video_rejects_image_to_video_without_media() {
        let st = test_state().await;
        // wan-i2v needs_image=true — omit `media` entirely.
        let body_bytes = b"--X\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\nhello\r\n--X\r\nContent-Disposition: form-data; name=\"provider\"\r\n\r\nwan-i2v\r\n--X--\r\n".to_vec();
        let req = Request::builder()
            .method("POST")
            .uri("/api/video/generate")
            .header("content-type", "multipart/form-data; boundary=X")
            .body(Body::from(body_bytes))
            .unwrap();
        let (status, body) = call(&st, req).await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert!(body["error"].as_str().unwrap().contains("image-to-video"));
    }
}
