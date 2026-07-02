//! HTTP routes.
//!
//! Wave-1 file ownership (one workstream per module; do not cross-edit):
//! - `captions`     — captions workstream (also owns media::ytdlp)
//! - `sounds`, `roster` — sounds/roster workstream (also owns repo::{sounds,roster}, integrations::{notion,hub})
//! - `distribution` — telegram workstream (also owns crate::telegram, repo::telegram_cfg)
//! - `email`        — integrations workstream (also owns integrations::{r2,email,drive})
//! - `miniapp`      — miniapp workstream (also owns crate::miniapp_auth, repo::requests)
//!
//! This file, error.rs, state.rs, and main.rs are integration seams — lead-owned.

mod assets;
mod captions;
pub mod distribution;
mod email;
mod error;
pub mod jobs;
mod miniapp;
mod projects;
mod roster;
mod sounds;

use axum::routing::get;
use axum::Router;

use crate::state::AppState;

pub use error::ApiError;

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/api/health", get(health))
        .route("/api/providers", get(list_providers))
        .merge(projects::router())
        .merge(assets::router())
        .merge(jobs::router())
        .merge(distribution::router())
        .merge(captions::router())
        .merge(sounds::router())
        .merge(roster::router())
        .merge(email::router())
        .merge(miniapp::router())
}

/// List the generation models whose API key is configured (usable now).
async fn list_providers() -> axum::Json<serde_json::Value> {
    let models = crate::providers::available_models();
    axum::Json(serde_json::json!({ "models": models }))
}

async fn health() -> axum::Json<serde_json::Value> {
    // Report whether the media binaries are on PATH (the old /api/health did this).
    let ffmpeg = which("ffmpeg").await;
    let ytdlp = which("yt-dlp").await;
    axum::Json(serde_json::json!({
        "status": if ffmpeg && ytdlp { "ok" } else { "degraded" },
        "ffmpeg": ffmpeg,
        "ytdlp": ytdlp,
    }))
}

async fn which(bin: &str) -> bool {
    tokio::process::Command::new(bin)
        .arg("-version")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .await
        .map(|s| s.success())
        .unwrap_or(false)
}
