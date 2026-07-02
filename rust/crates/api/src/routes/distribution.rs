//! Distribution routes — the part the owner called "a mess", rebuilt clean.
//!
//! The old layer had ~45 endpoints, half of them recovery band-aids
//! (scan-inventory, discover-topics, dedup, audit-dupes) that existed only to
//! repair state the brittle range-forwarding kept corrupting. With a real
//! schema and per-row inventory, none of that is needed. v1 surface:
//!
//!   GET  /api/pages                  list roster
//!   POST /api/pages                  upsert a page
//!   GET  /api/posters                list posters
//!   POST /api/posters                upsert a poster
//!   POST /api/posters/{id}/pages     assign a page to a poster
//!   POST /api/distribute/send        send a burned asset → its page's staging topic
//!   POST /api/distribute/forward/{page_id}   forward that page's PENDING items to its poster
//!
//! Every send is append-only and recorded as one inventory row. Forwarding
//! walks pending rows and marks each one individually — no message-id ranges,
//! no shared watermark, so nothing is ever skipped or double-sent.

use axum::extract::{Path, State};
use axum::routing::{get, post};
use axum::{Json, Router};
use clab_core::repo;
use serde::Deserialize;

use super::ApiError;
use crate::state::AppState;

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/api/pages", get(list_pages).post(upsert_page))
        .route("/api/posters", get(list_posters).post(upsert_poster))
        .route("/api/posters/{id}/pages", post(assign_page))
        .route("/api/distribute/send", post(send_asset))
        .route("/api/distribute/forward/{page_id}", post(forward_pending))
}

// ── Roster + posters ─────────────────────────────────────────────────────────

async fn list_pages(State(st): State<AppState>) -> Result<Json<serde_json::Value>, ApiError> {
    let pages = repo::list_pages(&st.db).await?;
    Ok(Json(serde_json::json!({ "pages": pages })))
}

#[derive(Deserialize)]
struct PageBody {
    id: String,
    name: String,
    project: Option<String>,
}

async fn upsert_page(
    State(st): State<AppState>,
    Json(b): Json<PageBody>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let page = repo::upsert_page(&st.db, &b.id, &b.name, b.project.as_deref()).await?;
    Ok(Json(serde_json::json!({ "page": page })))
}

async fn list_posters(State(st): State<AppState>) -> Result<Json<serde_json::Value>, ApiError> {
    let posters = repo::list_posters(&st.db).await?;
    Ok(Json(serde_json::json!({ "posters": posters })))
}

#[derive(Deserialize)]
struct PosterBody {
    id: String,
    name: String,
    chat_id: i64,
}

async fn upsert_poster(
    State(st): State<AppState>,
    Json(b): Json<PosterBody>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let poster = repo::upsert_poster(&st.db, &b.id, &b.name, b.chat_id).await?;
    Ok(Json(serde_json::json!({ "poster": poster })))
}

#[derive(Deserialize)]
struct AssignBody {
    page_id: String,
}

async fn assign_page(
    State(st): State<AppState>,
    Path(poster_id): Path<String>,
    Json(b): Json<AssignBody>,
) -> Result<Json<serde_json::Value>, ApiError> {
    repo::assign_page(&st.db, &poster_id, &b.page_id).await?;
    Ok(Json(serde_json::json!({ "ok": true })))
}

// ── Distribution actions ─────────────────────────────────────────────────────

#[derive(Deserialize)]
struct SendBody {
    /// A burned asset to distribute.
    asset_id: String,
    /// The page (and its staging topic) to send it into.
    page_id: String,
    /// Staging group chat id (the shared "Rising Tides Pages" group).
    staging_chat_id: i64,
    /// The page's topic id within the staging group.
    staging_topic_id: Option<i64>,
    caption: Option<String>,
}

/// Send a burned asset's file into the page's staging topic, and record ONE
/// inventory row for it. Append-only.
async fn send_asset(
    State(st): State<AppState>,
    Json(b): Json<SendBody>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let tg = st
        .telegram
        .as_ref()
        .ok_or_else(|| ApiError::BadRequest("Telegram not configured".into()))?;

    let asset = repo::get_asset(&st.db, &b.asset_id)
        .await?
        .ok_or(ApiError::NotFound)?;
    let rel = asset
        .path
        .clone()
        .ok_or_else(|| ApiError::BadRequest("asset has no file".into()))?;
    let abs = super::jobs::resolve_asset_path(&rel);

    // Idempotency: serialize per page, and if this (page, asset) was already
    // sent, return the existing row instead of sending a duplicate to Telegram
    // (a double-click / retried request must not produce a second copy).
    let lock = st.page_lock(&b.page_id);
    let _guard = lock.lock().await;
    if let Some(existing) = repo::find_inventory_for_asset(&st.db, &b.page_id, &b.asset_id).await? {
        return Ok(Json(serde_json::json!({
            "sent": false,
            "already_sent": true,
            "message_id": existing.staging_msg_id,
            "inventory_id": existing.id,
        })));
    }

    let msg_id = tg
        .send_video(
            b.staging_chat_id,
            b.staging_topic_id,
            &abs,
            b.caption.as_deref(),
        )
        .await
        .map_err(ApiError::Other)?;

    // Record exactly what we placed — one row, with the real message id.
    let item = repo::add_inventory(&st.db, &b.page_id, Some(&b.asset_id), Some(msg_id)).await?;
    Ok(Json(serde_json::json!({ "sent": true, "message_id": msg_id, "inventory_id": item.id })))
}

/// Forward a page's PENDING inventory to its assigned poster's topic.
///
/// Concurrency-safe by design (fixes the double-forward findings):
///   1. a per-page async lock serializes forwards for the same page,
///   2. we ATOMICALLY claim the pending rows (sentinel in forwarded_to) before
///      sending, so a second concurrent caller claims zero rows,
///   3. each claimed row is forwarded, then CONFIRMED (claim→real poster) on
///      success, or RELEASED back to pending on failure — never silently lost,
///      never double-forwarded.
async fn forward_pending(
    State(st): State<AppState>,
    Path(page_id): Path<String>,
    Json(b): Json<ForwardBody>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let tg = st
        .telegram
        .as_ref()
        .ok_or_else(|| ApiError::BadRequest("Telegram not configured".into()))?
        .clone();

    let poster = repo::poster_for_page(&st.db, &page_id)
        .await?
        .ok_or_else(|| ApiError::BadRequest("page has no assigned poster".into()))?;

    // Serialize forwards for THIS page (a double-click or schedule+manual
    // overlap can't run two forward loops for the same page at once).
    let lock = st.page_lock(&page_id);
    let _guard = lock.lock().await;

    // Atomically claim everything currently pending for this page.
    let claimed = repo::claim_pending_inventory(&st.db, &page_id, &poster.id).await?;
    let total = claimed.len() as u32;
    let mut forwarded = 0u32;
    let mut errors = Vec::new();

    for item in &claimed {
        let Some(staging_msg_id) = item.staging_msg_id else {
            // No staging message to forward — release the claim, skip.
            repo::release_claim(&st.db, &item.id, &poster.id).await?;
            continue;
        };
        match tg
            .forward(
                poster.chat_id,
                b.poster_topic_id,
                b.staging_chat_id,
                staging_msg_id,
            )
            .await
        {
            Ok(fwd_id) => {
                // Promote claim → confirmed forwarded for THIS row.
                repo::confirm_forwarded(&st.db, &item.id, &poster.id, fwd_id).await?;
                forwarded += 1;
            }
            Err(e) => {
                // Release the claim so a retry picks it up — no silent loss.
                repo::release_claim(&st.db, &item.id, &poster.id).await?;
                errors.push(format!("{}: {e}", item.id));
            }
        }
    }

    Ok(Json(serde_json::json!({
        "forwarded": forwarded,
        "pending_remaining": total - forwarded,
        "errors": errors,
    })))
}

#[derive(Deserialize)]
struct ForwardBody {
    staging_chat_id: i64,
    poster_topic_id: Option<i64>,
}
