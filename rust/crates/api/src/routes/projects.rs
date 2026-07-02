//! Project CRUD. A project groups all assets for one campaign.
//!
//! Parity target (frozen contract — see `rust/contract/CUTLIST.md` KEEP list
//! and `rust/contract/openapi.json`): `GET,POST /api/projects/` and
//! `GET,DELETE /api/projects/{name}`. The old Python router also exposed
//! `/import-legacy`, `/videos/recent`, `/{name}/stats`, and `/{name}/videos` —
//! none of those are in the KEEP list (no frontend caller, no external
//! allowlisted caller), so they are intentionally not ported here.
//!
//! Response shape mirrors the old filesystem-scan `_project_info()` dict
//! (`name`, `path`, `video_count`, `caption_count`, `burned_count`,
//! `last_activity`) so the existing frontend (`frontend/src/types/api.ts`
//! `Project`) needs no changes — but the numbers now come from the `assets` /
//! `captions` tables instead of walking directories. See
//! `clab_core::repo::projects_ext` for the mapping.

use axum::extract::{Path, State};
use axum::routing::get;
use axum::{Json, Router};
use clab_core::repo;
use clab_core::repo::projects_ext;
use serde::Deserialize;

use super::ApiError;
use crate::paths::sanitize_project_name;
use crate::state::AppState;

pub fn router() -> Router<AppState> {
    Router::new()
        // Trailing-slash variants (what the frozen contract/frontend call).
        .route("/api/projects/", get(list).post(create))
        .route("/api/projects/{name}", get(get_one).delete(delete))
}

#[derive(Deserialize)]
struct CreateBody {
    name: String,
}

async fn list(State(st): State<AppState>) -> Result<Json<serde_json::Value>, ApiError> {
    let names = repo::list_projects(&st.db).await?;
    let mut projects = Vec::with_capacity(names.len());
    for p in names {
        if let Some(detail) = projects_ext::get_project_detail(&st.db, &p.name).await? {
            projects.push(detail);
        }
    }
    Ok(Json(serde_json::json!({ "projects": projects })))
}

async fn create(
    State(st): State<AppState>,
    Json(body): Json<CreateBody>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let name = sanitize_project_name(&body.name)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;
    repo::create_project(&st.db, &name).await?;
    let detail = projects_ext::get_project_detail(&st.db, &name)
        .await?
        .ok_or(ApiError::NotFound)?;
    Ok(Json(serde_json::json!({ "project": detail })))
}

async fn get_one(
    State(st): State<AppState>,
    Path(name): Path<String>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let sanitized = sanitize_project_name(&name)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;
    let detail = projects_ext::get_project_detail(&st.db, &sanitized)
        .await?
        .ok_or(ApiError::NotFound)?;
    Ok(Json(serde_json::json!({ "project": detail })))
}

async fn delete(
    State(st): State<AppState>,
    Path(name): Path<String>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let sanitized = sanitize_project_name(&name)
        .ok_or_else(|| ApiError::BadRequest("invalid project name".into()))?;
    let deleted = projects_ext::delete_project(&st.db, &sanitized).await?;
    if !deleted {
        return Err(ApiError::NotFound);
    }
    Ok(Json(serde_json::json!({ "deleted": true, "name": sanitized })))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use serde_json::Value;
    use tower::ServiceExt;

    async fn test_state() -> AppState {
        // `Db::connect_temp` is cfg(test)-private to clab_core (same
        // constraint documented in routes/email.rs's test helper) — open a
        // throwaway file DB in the OS tempdir instead.
        let path = std::env::temp_dir().join(format!("projects-test-{}.db", uuid::Uuid::new_v4()));
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
    async fn create_then_list_then_get() {
        let st = test_state().await;

        let (status, created) = call(
            &st,
            json_req("POST", "/api/projects/", serde_json::json!({"name": "My Project"})),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(created["project"]["name"], "my-project");
        assert_eq!(created["project"]["video_count"], 0);
        assert_eq!(created["project"]["caption_count"], 0);
        assert_eq!(created["project"]["burned_count"], 0);

        let (status, listed) = call(&st, get_req("/api/projects/")).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(listed["projects"].as_array().unwrap().len(), 1);

        let (status, got) = call(&st, get_req("/api/projects/my-project")).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(got["project"]["name"], "my-project");
    }

    #[tokio::test]
    async fn get_missing_is_404() {
        let st = test_state().await;
        let (status, _) = call(&st, get_req("/api/projects/nope")).await;
        assert_eq!(status, StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn create_rejects_invalid_name() {
        let st = test_state().await;
        let (status, _) = call(
            &st,
            json_req("POST", "/api/projects/", serde_json::json!({"name": "../etc"})),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn delete_cascades_and_then_404s() {
        let st = test_state().await;
        repo::create_project(&st.db, "gone-soon").await.unwrap();
        clab_core::repo::create_asset(
            &st.db,
            "gone-soon",
            clab_core::model::AssetKind::Generated,
            None,
            None,
        )
        .await
        .unwrap();

        let (status, _) = call(
            &st,
            Request::builder()
                .method("DELETE")
                .uri("/api/projects/gone-soon")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::OK);

        // Second delete → 404 (already gone).
        let (status, _) = call(
            &st,
            Request::builder()
                .method("DELETE")
                .uri("/api/projects/gone-soon")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::NOT_FOUND);

        // The asset cascade-deleted with it.
        let assets = repo::list_assets(&st.db, "gone-soon", None).await.unwrap();
        assert!(assets.is_empty());
    }
}
