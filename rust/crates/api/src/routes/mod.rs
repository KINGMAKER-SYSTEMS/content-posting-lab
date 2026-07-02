//! HTTP routes. v1 = the asset spine (projects + assets) and health.
//! Media jobs (clip/burn) and Telegram distribution mount here as they land.

mod assets;
mod distribution;
mod error;
pub mod jobs;
mod projects;

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
