//! Project CRUD. A project groups all assets for one campaign.

use axum::extract::State;
use axum::routing::get;
use axum::{Json, Router};
use clab_core::repo;
use serde::Deserialize;

use super::ApiError;
use crate::state::AppState;

pub fn router() -> Router<AppState> {
    Router::new().route("/api/projects", get(list).post(create))
}

async fn list(State(st): State<AppState>) -> Result<Json<serde_json::Value>, ApiError> {
    let projects = repo::list_projects(&st.db).await?;
    Ok(Json(serde_json::json!({ "projects": projects })))
}

#[derive(Deserialize)]
struct CreateBody {
    name: String,
}

async fn create(
    State(st): State<AppState>,
    Json(body): Json<CreateBody>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let name = sanitize_project_name(&body.name)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;
    let project = repo::create_project(&st.db, &name).await?;
    Ok(Json(serde_json::json!({ "project": project })))
}

/// Lowercase, spaces→hyphens, strip unsafe chars, block traversal, cap length.
/// Mirrors the old `sanitize_project_name`.
fn sanitize_project_name(name: &str) -> Option<String> {
    if name.contains("..") || name.contains('/') || name.contains('\\') {
        return None;
    }
    let s: String = name
        .to_lowercase()
        .replace(' ', "-")
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '-' || *c == '_')
        .take(100)
        .collect();
    if s.is_empty() {
        None
    } else {
        Some(s)
    }
}
