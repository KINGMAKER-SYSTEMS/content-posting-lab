//! Stub — implemented by the wave-2 SPA+proxy workstream.
//! `attach` adds: ABN reverse-proxy (/api/agenticnews/*, /api/pipeline/*),
//! the /fonts static mount, and the React SPA (frontend/dist + index fallback).

use axum::Router;
use crate::state::AppState;

/// Wrap the API router with static-serving + reverse-proxy layers.
/// Returns the app unchanged until implemented (valid no-op stub).
pub fn attach(app: Router<AppState>, _data_dir: &str) -> Router<AppState> {
    app
}
