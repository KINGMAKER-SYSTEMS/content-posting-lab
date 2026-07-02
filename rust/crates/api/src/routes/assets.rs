//! Asset routes — the durable spine of the generate→clip→burn flow.
//!
//! The whole point: the frontend addresses media by **stable asset id**, never
//! by guessing a folder. `GET /api/assets?project=X&kind=clip` lists exactly the
//! clips; a burn job is later started with an explicit list of asset ids. This
//! is what makes the flow intuitive and reload-safe — the opposite of the old
//! one-shot Zustand handoff that silently widened a 3-of-12 selection to all 12.

use axum::extract::{Path, Query, State};
use axum::routing::get;
use axum::{Json, Router};
use clab_core::model::AssetKind;
use clab_core::repo;
use serde::Deserialize;

use super::ApiError;
use crate::state::AppState;

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/api/assets", get(list))
        .route("/api/assets/{id}", get(get_one))
}

#[derive(Deserialize)]
struct ListQuery {
    project: String,
    /// Optional kind filter: generated | clip | burned | source.
    kind: Option<String>,
}

async fn list(
    State(st): State<AppState>,
    Query(q): Query<ListQuery>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let kind = match q.kind.as_deref() {
        None => None,
        Some(k) => Some(parse_kind(k)?),
    };
    let assets = repo::list_assets(&st.db, &q.project, kind).await?;
    Ok(Json(serde_json::json!({ "assets": assets })))
}

async fn get_one(
    State(st): State<AppState>,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let asset = repo::get_asset(&st.db, &id)
        .await?
        .ok_or(ApiError::NotFound)?;
    Ok(Json(serde_json::json!({ "asset": asset })))
}

fn parse_kind(s: &str) -> Result<AssetKind, ApiError> {
    match s {
        "generated" => Ok(AssetKind::Generated),
        "clip" => Ok(AssetKind::Clip),
        "burned" => Ok(AssetKind::Burned),
        "source" => Ok(AssetKind::Source),
        other => Err(ApiError::BadRequest(format!("unknown kind: {other}"))),
    }
}
