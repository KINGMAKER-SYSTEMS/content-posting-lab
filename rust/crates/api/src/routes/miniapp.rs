//! Telegram Mini App API — the lightweight client posters open from Telegram.
//!
//! Poster-facing endpoints authenticate via Telegram `initData` (validated
//! against the bot token in `crate::miniapp_auth`) and are scoped to the
//! poster the calling Telegram user acts as:
//!
//!   GET  /api/miniapp/me        → poster profile + the pages they run
//!   GET  /api/miniapp/videos    → rendered videos across those pages
//!   GET  /api/miniapp/requests  → the poster's own content requests
//!   POST /api/miniapp/requests  → file a new content request
//!
//! Agent-facing endpoints (gated by X-Agent-Key when MINIAPP_AGENT_KEY is set)
//! let the external content agent read/work the request queue:
//!
//!   GET   /api/miniapp/agent/requests
//!   PATCH /api/miniapp/agent/requests/{request_id}
//!
//! Error bodies use `{"detail": ...}` — FastAPI's HTTPException shape — so the
//! Mini App frontend and the agent keep working unchanged.

use std::collections::{HashMap, HashSet};

use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, patch};
use axum::{Json, Router};
use clab_core::repo::requests::{self as repo_req, MiniappPage, MiniappPoster, RequestUpdateError};
use clab_core::Db;
use serde::Deserialize;
use serde_json::json;

use crate::miniapp_auth::{resolve_poster_from_request, AuthError};
use crate::state::AppState;

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/api/miniapp/me", get(me))
        .route("/api/miniapp/videos", get(my_videos))
        .route("/api/miniapp/requests", get(my_requests).post(create_request))
        .route("/api/miniapp/agent/requests", get(agent_list_requests))
        .route("/api/miniapp/agent/requests/{request_id}", patch(agent_update_request))
}

// ── Error type (FastAPI-shaped: {"detail": ...}) ─────────────────────────────

struct MiniErr {
    status: StatusCode,
    detail: String,
}

impl MiniErr {
    fn new(status: StatusCode, detail: impl Into<String>) -> Self {
        Self { status, detail: detail.into() }
    }
}

impl IntoResponse for MiniErr {
    fn into_response(self) -> Response {
        (self.status, Json(json!({ "detail": self.detail }))).into_response()
    }
}

impl From<AuthError> for MiniErr {
    fn from(e: AuthError) -> Self {
        Self { status: e.status, detail: e.message }
    }
}

impl From<sqlx::Error> for MiniErr {
    fn from(e: sqlx::Error) -> Self {
        tracing::error!("miniapp db error: {e}");
        Self::new(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error")
    }
}

// ── Auth helpers ─────────────────────────────────────────────────────────────

/// Resolve the authenticated poster or fail. initData is read from the
/// `X-Telegram-Init-Data` header (preferred) or `Authorization: tma <initData>`.
/// The dev bypass reads `X-Dev-Poster-Id` (honored only when MINIAPP_DEV_AUTH
/// is enabled — enforced inside miniapp_auth).
async fn require_poster(db: &Db, headers: &HeaderMap) -> Result<MiniappPoster, MiniErr> {
    let header_str = |name: &str| {
        headers
            .get(name)
            .and_then(|v| v.to_str().ok())
            .map(str::to_string)
    };
    let init_data = header_str("x-telegram-init-data").or_else(|| {
        let auth = header_str("authorization").unwrap_or_default();
        if auth.len() >= 4 && auth[..4].eq_ignore_ascii_case("tma ") {
            Some(auth[4..].trim().to_string())
        } else {
            None
        }
    });
    let dev_poster_id = header_str("x-dev-poster-id");

    Ok(resolve_poster_from_request(db, init_data.as_deref(), dev_poster_id.as_deref()).await?)
}

/// Gate agent endpoints behind MINIAPP_AGENT_KEY when it is configured.
/// Not configured → open, consistent with the rest of the app.
fn require_agent_key(headers: &HeaderMap) -> Result<(), MiniErr> {
    let expected = std::env::var("MINIAPP_AGENT_KEY").unwrap_or_default();
    let expected = expected.trim();
    if expected.is_empty() {
        return Ok(());
    }
    let provided = headers
        .get("x-agent-key")
        .and_then(|v| v.to_str().ok())
        .map(str::trim)
        .unwrap_or("");
    if provided != expected {
        return Err(MiniErr::new(
            StatusCode::UNAUTHORIZED,
            "invalid or missing agent key",
        ));
    }
    Ok(())
}

// ── Per-poster page federation (port of services/poster_content.py) ─────────

/// Normalize a name for matching: lowercase, alphanumerics only.
fn normalize(s: &str) -> String {
    s.to_lowercase().chars().filter(|c| c.is_ascii_alphanumeric()).collect()
}

/// Resolve a page's Notion `poster_name` to a registered poster — exact
/// normalized match first, then the fuzzy first-name-prefix fallback
/// ("Jake Balik" matches "Jake B."). Port of services/poster_router.py.
fn resolve_poster_for_page<'a>(
    posters: &'a [MiniappPoster],
    poster_name: Option<&str>,
) -> Option<&'a MiniappPoster> {
    let target = poster_name.unwrap_or("").trim();
    let target_norm = normalize(target);
    if target_norm.is_empty() {
        return None;
    }
    if let Some(hit) = posters.iter().find(|p| normalize(&p.name) == target_norm) {
        return Some(hit);
    }
    let target_first = target.split_whitespace().next().unwrap_or("").to_lowercase();
    if target_first.is_empty() {
        return None;
    }
    posters
        .iter()
        .find(|p| p.name.to_lowercase().starts_with(&target_first))
}

/// The roster pages this poster runs: Master-Pages-driven (`poster_name`
/// resolves to this poster) unioned with manual `poster_pages` assignments.
async fn pages_for_poster(db: &Db, poster: &MiniappPoster) -> Result<Vec<MiniappPage>, sqlx::Error> {
    let all_pages = repo_req::list_pages_mini(db).await?;
    let posters = repo_req::list_posters_mini(db).await?;

    let mut picked: Vec<MiniappPage> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();

    // 1) Master-Pages-driven: pages whose poster_name resolves to this poster.
    for page in &all_pages {
        if let Some(resolved) = resolve_poster_for_page(&posters, page.poster_name.as_deref()) {
            if resolved.id == poster.id && seen.insert(page.id.clone()) {
                picked.push(page.clone());
            }
        }
    }

    // 2) Manual override: anything explicitly assigned on poster_pages.
    for page_id in repo_req::assigned_page_ids(db, &poster.id).await? {
        if seen.contains(&page_id) {
            continue;
        }
        if let Some(page) = all_pages.iter().find(|p| p.id == page_id) {
            seen.insert(page_id);
            picked.push(page.clone());
        }
    }

    Ok(picked)
}

fn page_summary_json(p: &MiniappPage) -> serde_json::Value {
    json!({
        "integration_id": p.id,
        "name": p.name,
        "project": p.project,
        "provider": p.provider,
        "status": p.status,
    })
}

// ── Poster-facing endpoints ──────────────────────────────────────────────────

/// Poster profile plus the pages they run (from Master Pages).
async fn me(
    State(st): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<serde_json::Value>, MiniErr> {
    let poster = require_poster(&st.db, &headers).await?;
    let pages = pages_for_poster(&st.db, &poster).await?;
    Ok(Json(json!({
        "poster_id": poster.id,
        "name": poster.name,
        "page_count": pages.len(),
        "pages": pages.iter().map(page_summary_json).collect::<Vec<_>>(),
    })))
}

#[derive(Deserialize)]
struct VideosQuery {
    page_id: Option<String>,
}

/// Rendered videos aggregated across the poster's pages.
///
/// Rust rewrite: sourced from the `assets` table (kind generated/clip/burned,
/// status ready) via each page's `project` — not a filesystem walk. Field
/// names match the Python response; see the parity notes in the wave report.
async fn my_videos(
    State(st): State<AppState>,
    Query(q): Query<VideosQuery>,
    headers: HeaderMap,
) -> Result<Json<serde_json::Value>, MiniErr> {
    let poster = require_poster(&st.db, &headers).await?;
    let mut pages = pages_for_poster(&st.db, &poster).await?;
    if let Some(pid) = &q.page_id {
        pages.retain(|p| &p.id == pid);
    }

    // Scan each distinct project once, then fan results back out to pages.
    let mut project_cache: HashMap<String, Vec<clab_core::model::Asset>> = HashMap::new();
    let mut page_summaries = Vec::new();
    let mut videos = Vec::new();

    for page in &pages {
        let mut count = 0usize;
        if let Some(project) = &page.project {
            if !project_cache.contains_key(project) {
                let assets = repo_req::ready_videos_for_project(&st.db, project).await?;
                project_cache.insert(project.clone(), assets);
            }
            for asset in &project_cache[project] {
                let Some(path) = &asset.path else { continue };
                // Stored paths are data-dir-relative ("projects/<p>/..."); the
                // client-facing path is relative to the project dir, like the
                // old scanner's output, and the URL resolves via /projects.
                let prefix = format!("projects/{project}/");
                let rel = path.strip_prefix(&prefix).unwrap_or(path);
                let name = rel.rsplit('/').next().unwrap_or(rel);
                let folder = match rel.rsplit_once('/') {
                    Some((dir, _)) => dir,
                    None => "",
                };
                videos.push(json!({
                    "page_id": page.id,
                    "page_name": page.name,
                    "project": project,
                    "path": rel,
                    "name": name,
                    "folder": folder,
                    "created": asset.created_at.timestamp(),
                    "url": format!("/{path}"),
                }));
                count += 1;
            }
        }
        let mut summary = page_summary_json(page);
        summary["video_count"] = json!(count);
        summary["scan_error"] = json!(false);
        page_summaries.push(summary);
    }

    videos.sort_by_key(|v| std::cmp::Reverse(v["created"].as_i64().unwrap_or(0)));

    Ok(Json(json!({
        "poster_id": poster.id,
        "pages": page_summaries,
        "videos": videos,
        "scan_errors": [],
    })))
}

#[derive(Deserialize)]
struct StatusQuery {
    status: Option<String>,
}

/// List the calling poster's content requests.
async fn my_requests(
    State(st): State<AppState>,
    Query(q): Query<StatusQuery>,
    headers: HeaderMap,
) -> Result<Json<serde_json::Value>, MiniErr> {
    let poster = require_poster(&st.db, &headers).await?;
    let requests =
        repo_req::list_requests(&st.db, Some(&poster.id), q.status.as_deref()).await?;
    Ok(Json(json!({ "requests": requests })))
}

#[derive(Deserialize)]
struct ContentRequestBody {
    text: String,
    #[serde(default)]
    page_id: Option<String>,
    #[serde(default)]
    quantity: Option<i64>,
}

/// File a content request for the calling poster.
async fn create_request(
    State(st): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<ContentRequestBody>,
) -> Result<Response, MiniErr> {
    let poster = require_poster(&st.db, &headers).await?;
    if body.text.trim().is_empty() {
        return Err(MiniErr::new(StatusCode::BAD_REQUEST, "text is required"));
    }

    // Resolve the page name from the poster's own pages (same lookup scope as
    // the Python router — an arbitrary page_id outside their pages stores None).
    let mut page_name: Option<String> = None;
    if let Some(pid) = &body.page_id {
        page_name = pages_for_poster(&st.db, &poster)
            .await?
            .into_iter()
            .find(|p| &p.id == pid)
            .map(|p| p.name);
    }

    let entry = repo_req::add_request(
        &st.db,
        repo_req::NewRequest {
            poster_id: &poster.id,
            poster_name: Some(&poster.name),
            text: &body.text,
            page_id: body.page_id.as_deref(),
            page_name: page_name.as_deref(),
            quantity: body.quantity,
            source: "miniapp",
        },
    )
    .await?;
    Ok((StatusCode::CREATED, Json(entry)).into_response())
}

// ── Agent-facing endpoints ───────────────────────────────────────────────────

#[derive(Deserialize)]
struct AgentListQuery {
    status: Option<String>,
    poster_id: Option<String>,
}

/// The external agent polls this for requests to work.
async fn agent_list_requests(
    State(st): State<AppState>,
    Query(q): Query<AgentListQuery>,
    headers: HeaderMap,
) -> Result<Json<serde_json::Value>, MiniErr> {
    require_agent_key(&headers)?;
    // Default "open" when the param is absent; an explicit empty string means "all".
    let status = q.status.unwrap_or_else(|| "open".to_string());
    let status_filter = (!status.is_empty()).then_some(status);
    let requests =
        repo_req::list_requests(&st.db, q.poster_id.as_deref(), status_filter.as_deref()).await?;
    Ok(Json(json!({ "requests": requests })))
}

#[derive(Deserialize)]
struct AgentUpdateBody {
    #[serde(default)]
    status: Option<String>,
    #[serde(default)]
    agent_note: Option<String>,
}

/// The agent marks a request in_progress / fulfilled / cancelled.
async fn agent_update_request(
    State(st): State<AppState>,
    Path(request_id): Path<String>,
    headers: HeaderMap,
    Json(body): Json<AgentUpdateBody>,
) -> Result<Json<serde_json::Value>, MiniErr> {
    require_agent_key(&headers)?;
    let updated = repo_req::update_request(
        &st.db,
        &request_id,
        body.status.as_deref(),
        body.agent_note.as_deref(),
    )
    .await
    .map_err(|e| match e {
        RequestUpdateError::InvalidStatus(_) => {
            MiniErr::new(StatusCode::BAD_REQUEST, e.to_string())
        }
        RequestUpdateError::Db(db) => db.into(),
    })?;
    match updated {
        Some(entry) => Ok(Json(serde_json::to_value(entry).unwrap_or_default())),
        None => Err(MiniErr::new(StatusCode::NOT_FOUND, "request not found")),
    }
}
