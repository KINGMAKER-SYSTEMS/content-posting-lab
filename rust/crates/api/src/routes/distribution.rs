//! Telegram distribution surface — /api/telegram/* parity with the Python
//! router (routers/telegram.py), rebuilt on the relational schema.
//!
//! The old layer had ~45 endpoints, half of them recovery band-aids
//! (scan-inventory, discover-topics, send-batch watermarks) that existed only
//! to repair state the brittle range-forwarding kept corrupting. With per-row
//! inventory none of that is needed — those paths are CUT per the contract.
//!
//! Hard rules enforced here (do not weaken):
//!   1. Bot token priority: env TELEGRAM_BOT_TOKEN beats the stored value.
//!   2. Topic creation is APPEND-ONLY — existing (scope, owner, page) mappings
//!      are never overwritten unless force = true is explicit (the bulk-restore
//!      endpoint is the only force caller).
//!   3. Nothing sends or forwards at startup — manual endpoints or the
//!      schedule loop only.
//!   4. Forwarding is per-inventory-row claim→confirm/release
//!      (repo::claim_pending_inventory & co). No message-id-range walking, no
//!      shared watermarks — ever again.
//!   5. A poster with chat_id = 0 is UNCONFIGURED: sends/forwards to it are
//!      refused with a clear 400.

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::{Mutex as StdMutex, OnceLock};
use std::time::Duration as StdDuration;

use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, get, post, put};
use axum::{Json, Router};
use chrono::{DateTime, Datelike, Duration, NaiveDate, NaiveDateTime, NaiveTime, TimeZone, Utc, Weekday};
use clab_core::model::Poster;
use clab_core::repo;
use clab_core::repo::telegram_cfg as tcfg;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::state::AppState;
use crate::telegram::Telegram;

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/api/telegram/status", get(status))
        .route("/api/telegram/bot-token", put(set_bot_token).delete(delete_bot_token))
        .route("/api/telegram/staging-group", get(get_staging_group).put(put_staging_group))
        .route("/api/telegram/staging-group/sync-topics", post(sync_staging_topics))
        .route("/api/telegram/staging-group/topics", put(bulk_restore_staging_topics))
        .route(
            "/api/telegram/staging-group/topics/{integration_id}",
            delete(delete_staging_topic),
        )
        .route("/api/telegram/posters", get(get_posters).post(create_poster))
        .route(
            "/api/telegram/posters/{poster_id}",
            put(update_poster).delete(delete_poster),
        )
        .route("/api/telegram/posters/{poster_id}/users", post(bind_poster_user))
        .route(
            "/api/telegram/posters/{poster_id}/users/{user_id}",
            delete(unbind_poster_user),
        )
        .route("/api/telegram/posters/{poster_id}/pages", post(assign_pages))
        .route(
            "/api/telegram/posters/{poster_id}/pages/{integration_id}",
            delete(unassign_page),
        )
        .route("/api/telegram/posters/{poster_id}/sync-topics", post(sync_poster_topics))
        .route("/api/telegram/send", post(send_content))
        .route("/api/telegram/assign-batch", post(assign_batch))
        .route("/api/telegram/forward/{integration_id}", post(forward_page))
        .route("/api/telegram/schedule", get(get_schedule).put(put_schedule))
        .route("/api/telegram/batch/run", post(trigger_batch_run))
}

// ── Errors ───────────────────────────────────────────────────────────────────
// The shared ApiError has no 503/401/409, and this surface needs the Python
// status codes (e.g. 503 "Telegram bot is not running"). Local error type,
// same {"error": msg} body shape as the rest of the app.

#[derive(Debug)]
enum TgError {
    /// 503 — the bot isn't running (no token installed).
    BotDown,
    /// 400
    Bad(String),
    /// 404
    NotFound(String),
    /// 401
    Unauthorized(String),
    /// 409
    Conflict(String),
    /// 500
    Db(sqlx::Error),
    /// 500
    Other(anyhow::Error),
}

impl From<sqlx::Error> for TgError {
    fn from(e: sqlx::Error) -> Self {
        TgError::Db(e)
    }
}
impl From<anyhow::Error> for TgError {
    fn from(e: anyhow::Error) -> Self {
        TgError::Other(e)
    }
}

impl IntoResponse for TgError {
    fn into_response(self) -> Response {
        let (status, msg) = match self {
            TgError::BotDown => (
                StatusCode::SERVICE_UNAVAILABLE,
                "Telegram bot is not running".to_string(),
            ),
            TgError::Bad(m) => (StatusCode::BAD_REQUEST, m),
            TgError::NotFound(m) => (StatusCode::NOT_FOUND, m),
            TgError::Unauthorized(m) => (StatusCode::UNAUTHORIZED, m),
            TgError::Conflict(m) => (StatusCode::CONFLICT, m),
            TgError::Db(e) => {
                tracing::error!("db error: {e}");
                (StatusCode::INTERNAL_SERVER_ERROR, "internal error".into())
            }
            TgError::Other(e) => {
                tracing::error!("error: {e:#}");
                (StatusCode::INTERNAL_SERVER_ERROR, "internal error".into())
            }
        };
        (status, Json(json!({ "error": msg }))).into_response()
    }
}

/// The bot, or 503. A handle can exist with no bot installed (token deleted at
/// runtime), so this checks both layers.
async fn require_bot(st: &AppState) -> Result<Telegram, TgError> {
    match &st.telegram {
        Some(tg) if tg.is_running().await => Ok(tg.clone()),
        _ => Err(TgError::BotDown),
    }
}

/// HARD RULE 5: a poster with chat_id = 0 is unconfigured — refuse to send or
/// forward to it with a clear 400 instead of letting Telegram error opaquely.
fn require_configured_chat(poster: &Poster) -> Result<i64, TgError> {
    if poster.chat_id == 0 {
        return Err(TgError::Bad(format!(
            "Poster '{}' has no Telegram group configured (chat_id = 0)",
            poster.name
        )));
    }
    Ok(poster.chat_id)
}

fn env_nonempty(key: &str) -> bool {
    std::env::var(key).map(|v| !v.trim().is_empty()).unwrap_or(false)
}

fn data_dir() -> String {
    std::env::var("RAILWAY_VOLUME_MOUNT_PATH")
        .ok()
        .filter(|p| std::path::Path::new(p).exists())
        .unwrap_or_else(|| "./data".to_string())
}

// ── Status & bot token ───────────────────────────────────────────────────────

async fn status(State(st): State<AppState>) -> Result<Json<Value>, TgError> {
    let stored_token = tcfg::get_setting(&st.db, "bot_token").await?;
    let bot_configured =
        env_nonempty("TELEGRAM_BOT_TOKEN") || stored_token.map(|t| !t.trim().is_empty()).unwrap_or(false);
    let bot_running = match &st.telegram {
        Some(tg) => tg.is_running().await,
        None => false,
    };
    let bot_username = tcfg::get_setting(&st.db, "bot_username").await?;

    let summary = tcfg::inventory_summary(&st.db).await?;
    let total_inventory: i64 = summary.iter().map(|s| s.total).sum();

    let staging = match tcfg::staging_group(&st.db).await? {
        Some((chat_id, name)) => {
            let topics = enriched_staging_topics(&st.db, &summary).await?;
            json!({
                "chat_id": chat_id,
                "name": name,
                "topic_count": topics.len(),
                "topics": topics,
            })
        }
        None => Value::Null,
    };

    let schedule = tcfg::get_schedule(&st.db).await?;
    let poster_count = repo::list_posters(&st.db).await?.len();

    Ok(Json(json!({
        "bot_configured": bot_configured,
        "bot_running": bot_running,
        // No MTProto sidecar in the rewrite — topic discovery is replaced by
        // stored topic rows, so this is permanently false.
        "pyrogram_available": false,
        "bot_username": bot_username,
        "sounds_bot_configured": env_nonempty("SOUNDS_BOT_TOKEN"),
        "sounds_bot_running": false,
        "staging_group": staging,
        "poster_count": poster_count,
        "total_inventory": total_inventory,
        "schedule": schedule,
        "notion_configured": env_nonempty("NOTION_API_KEY") && env_nonempty("NOTION_CAMPAIGNS_DB"),
        // Campaign Hub has a baked-in default URL (matches the Python service).
        "campaign_hub_configured": std::env::var("CAMPAIGN_HUB_URL").map(|v| !v.trim().is_empty()).unwrap_or(true),
    })))
}

#[derive(Deserialize)]
struct BotTokenBody {
    token: String,
}

/// Store + activate a bot token. The token is validated against Telegram
/// (get_me) before anything is stored — a bad token is rejected with 400 and
/// nothing changes, mirroring the Python start-or-rollback behavior.
///
/// NOTE (env priority, hard rule 1): the ENV token still wins at boot. This
/// endpoint hot-swaps the RUNNING bot (exactly like the Python version, where
/// PUT restarts the bot on the new token) and persists it for the next boot's
/// env-then-stored resolution.
async fn set_bot_token(
    State(st): State<AppState>,
    Json(b): Json<BotTokenBody>,
) -> Result<Json<Value>, TgError> {
    let username = match Telegram::validate_token(&b.token).await {
        Ok(u) => u,
        Err(e) => {
            tcfg::delete_setting(&st.db, "bot_token").await?;
            tcfg::delete_setting(&st.db, "bot_username").await?;
            return Err(TgError::Bad(format!("Failed to start bot: {e:#}")));
        }
    };
    tcfg::set_setting(&st.db, "bot_token", b.token.trim()).await?;
    tcfg::set_setting(&st.db, "bot_username", &username).await?;
    if let Some(tg) = &st.telegram {
        tg.install(&b.token).await;
    }
    // When no Telegram handle exists (booted with neither env nor stored
    // token under the current main.rs wiring), the stored token takes effect
    // on the next boot via Telegram::from_env_or_settings.
    Ok(Json(json!({ "ok": true, "bot_username": username })))
}

/// Stop the bot and clear the stored token. If TELEGRAM_BOT_TOKEN is set in
/// the env it will win again on the next boot (env > stored, by design).
async fn delete_bot_token(State(st): State<AppState>) -> Result<Json<Value>, TgError> {
    if let Some(tg) = &st.telegram {
        tg.shutdown().await;
    }
    tcfg::delete_setting(&st.db, "bot_token").await?;
    tcfg::delete_setting(&st.db, "bot_username").await?;
    Ok(Json(json!({ "ok": true })))
}

// ── Staging group ────────────────────────────────────────────────────────────

/// Build the {page_id: {topic_id, topic_name, inventory_*}} map the frontend
/// renders for the staging group.
async fn enriched_staging_topics(
    db: &clab_core::Db,
    summary: &[tcfg::InventorySummary],
) -> Result<serde_json::Map<String, Value>, sqlx::Error> {
    let by_page: HashMap<&str, &tcfg::InventorySummary> =
        summary.iter().map(|s| (s.page_id.as_str(), s)).collect();
    let mut out = serde_json::Map::new();
    for t in tcfg::list_topics(db, "staging", "").await? {
        let inv = by_page.get(t.page_id.as_str());
        out.insert(
            t.page_id.clone(),
            json!({
                "topic_id": t.topic_id,
                "topic_name": t.topic_name,
                "inventory_total": inv.map(|s| s.total).unwrap_or(0),
                "inventory_pending": inv.map(|s| s.pending).unwrap_or(0),
                "inventory_forwarded": inv.map(|s| s.forwarded).unwrap_or(0),
            }),
        );
    }
    Ok(out)
}

async fn get_staging_group(State(st): State<AppState>) -> Result<Json<Value>, TgError> {
    let Some((chat_id, name)) = tcfg::staging_group(&st.db).await? else {
        return Ok(Json(json!({ "staging_group": null })));
    };
    let summary = tcfg::inventory_summary(&st.db).await?;
    let topics = enriched_staging_topics(&st.db, &summary).await?;
    Ok(Json(json!({
        "staging_group": { "chat_id": chat_id, "name": name, "topics": topics }
    })))
}

#[derive(Deserialize)]
struct StagingGroupBody {
    chat_id: i64,
}

async fn put_staging_group(
    State(st): State<AppState>,
    Json(b): Json<StagingGroupBody>,
) -> Result<Json<Value>, TgError> {
    let tg = require_bot(&st).await?;
    let info = tg.validate_group(b.chat_id).await?;
    if !info.is_admin {
        return Err(TgError::Bad("Bot must be an admin in the group".into()));
    }
    if !info.is_forum {
        return Err(TgError::Bad("Group must be a forum (topics enabled)".into()));
    }
    tcfg::set_staging_group(&st.db, b.chat_id, &info.title).await?;
    Ok(Json(json!({ "ok": true, "group": info })))
}

/// Create a forum topic with the Python retry policy: 3 attempts, 5s backoff
/// on rate-limit-shaped errors.
async fn create_topic_with_retry(tg: &Telegram, chat_id: i64, name: &str) -> Result<i64, String> {
    let mut last_err = String::new();
    for attempt in 0..3 {
        match tg.create_topic(chat_id, name).await {
            Ok(tid) => return Ok(tid),
            Err(e) => {
                let msg = format!("{e:#}");
                let lower = msg.to_lowercase();
                let transient =
                    lower.contains("retry") || lower.contains("too many") || lower.contains("429");
                if attempt < 2 && transient {
                    tokio::time::sleep(StdDuration::from_secs(5)).await;
                    last_err = msg;
                    continue;
                }
                return Err(msg);
            }
        }
    }
    Err(last_err)
}

/// Create staging topics for roster pages that don't have one yet.
/// APPEND-ONLY: pages with an existing mapping are skipped — even if the
/// topic_id is stale. To remap, delete the mapping first.
async fn sync_staging_topics(State(st): State<AppState>) -> Result<Json<Value>, TgError> {
    let tg = require_bot(&st).await?;
    let Some((chat_id, _)) = tcfg::staging_group(&st.db).await? else {
        return Err(TgError::Bad("Staging group not configured".into()));
    };

    let pages = tcfg::list_pages_brief(&st.db).await?;
    let mut created = 0u32;
    let mut existing = 0u32;
    let mut failed: Vec<String> = Vec::new();
    let mut seen_names: HashSet<String> = HashSet::new();

    for page in &pages {
        // Re-read fresh each iteration to see topics just created in this loop.
        let name_key = page.name.trim().to_lowercase();
        if tcfg::get_topic(&st.db, "staging", "", &page.id).await?.is_some() {
            existing += 1;
            seen_names.insert(name_key);
            continue;
        }
        // Skip if another integration with the same display name has a topic.
        if seen_names.contains(&name_key) {
            existing += 1;
            continue;
        }
        seen_names.insert(name_key);

        let topic_name = match page.provider.as_deref().filter(|p| !p.is_empty()) {
            Some(p) => format!("{} ({p})", page.name),
            None => page.name.clone(),
        };
        match create_topic_with_retry(&tg, chat_id, &topic_name).await {
            Ok(topic_id) => {
                // Append-only write (force = false).
                tcfg::set_topic(&st.db, "staging", "", &page.id, topic_id, &topic_name, false)
                    .await?;
                created += 1;
            }
            Err(e) => {
                tracing::warn!("failed to create staging topic for {}: {e}", page.id);
                failed.push(page.name.clone());
            }
        }
        tokio::time::sleep(StdDuration::from_millis(1500)).await;
    }

    Ok(Json(json!({ "created": created, "existing": existing, "failed": failed })))
}

async fn delete_staging_topic(
    State(st): State<AppState>,
    Path(integration_id): Path<String>,
) -> Result<Json<Value>, TgError> {
    tcfg::remove_topic(&st.db, "staging", "", &integration_id).await?;
    Ok(Json(json!({ "ok": true })))
}

#[derive(Deserialize)]
struct BulkTopicsBody {
    #[serde(default)]
    topics: HashMap<String, BulkTopicEntry>,
}

#[derive(Deserialize)]
struct BulkTopicEntry {
    topic_id: Option<i64>,
    #[serde(default)]
    topic_name: String,
}

/// Bulk-restore staging topic mappings after config loss. Does NOT call the
/// Telegram API. This is the ONE sanctioned force=true caller — an explicit
/// restore, never an implicit overwrite.
async fn bulk_restore_staging_topics(
    State(st): State<AppState>,
    Json(b): Json<BulkTopicsBody>,
) -> Result<Json<Value>, TgError> {
    let mut restored = 0u32;
    for (iid, entry) in &b.topics {
        let Some(tid) = entry.topic_id else { continue };
        // Pages may not have synced yet — placeholder row satisfies the FK.
        tcfg::ensure_page(&st.db, iid, if entry.topic_name.is_empty() { iid } else { &entry.topic_name })
            .await?;
        tcfg::set_topic(&st.db, "staging", "", iid, tid, &entry.topic_name, true).await?;
        restored += 1;
    }
    Ok(Json(json!({ "ok": true, "restored": restored })))
}

// ── Posters ──────────────────────────────────────────────────────────────────

/// Full poster JSON in the exact Python field shape.
async fn poster_json(db: &clab_core::Db, p: &Poster) -> Result<Value, sqlx::Error> {
    let page_ids = tcfg::poster_page_ids(db, &p.id).await?;
    let mut topics = serde_json::Map::new();
    for t in tcfg::list_topics(db, "poster", &p.id).await? {
        topics.insert(
            t.page_id.clone(),
            json!({ "topic_id": t.topic_id, "topic_name": t.topic_name }),
        );
    }
    let user_ids = tcfg::poster_user_ids(db, &p.id).await?;
    let username = tcfg::poster_username(db, &p.id).await?;
    Ok(json!({
        "poster_id": p.id,
        "name": p.name,
        "chat_id": p.chat_id,
        "page_ids": page_ids,
        "topics": topics,
        "sounds_topic_id": p.sounds_topic_id,
        "telegram_user_ids": user_ids,
        "telegram_username": username,
        "added_at": p.created_at,
        "updated_at": p.updated_at,
    }))
}

async fn get_posters(State(st): State<AppState>) -> Result<Json<Value>, TgError> {
    let posters = repo::list_posters(&st.db).await?;
    let mut out = Vec::with_capacity(posters.len());
    for p in &posters {
        out.push(poster_json(&st.db, p).await?);
    }
    Ok(Json(Value::Array(out)))
}

/// URL-safe poster id from a display name (matches the Python `_slugify`).
fn slugify(name: &str) -> String {
    let mut out = String::new();
    let mut prev_dash = true; // suppress a leading dash
    for c in name.to_lowercase().chars() {
        if c.is_ascii_alphanumeric() {
            out.push(c);
            prev_dash = false;
        } else if !prev_dash {
            out.push('-');
            prev_dash = true;
        }
    }
    let out = out.trim_matches('-').to_string();
    if out.is_empty() {
        "poster".to_string()
    } else {
        out
    }
}

#[derive(Deserialize)]
struct PosterCreateBody {
    name: String,
    chat_id: i64,
}

async fn create_poster(
    State(st): State<AppState>,
    Json(b): Json<PosterCreateBody>,
) -> Result<Json<Value>, TgError> {
    let tg = require_bot(&st).await?;
    let info = tg
        .validate_group(b.chat_id)
        .await
        .map_err(|e| TgError::Bad(format!("Can't reach group {}: {e:#}", b.chat_id)))?;
    if !info.is_forum {
        return Err(TgError::Bad("Group must be a forum (topics enabled)".into()));
    }
    if !info.is_admin {
        return Err(TgError::Bad("Bot must be an admin in the group".into()));
    }

    // Slug collision → append a numeric suffix (Python behavior).
    let base = slugify(&b.name);
    let mut poster_id = base.clone();
    let mut counter = 2;
    while repo::get_poster(&st.db, &poster_id).await?.is_some() {
        poster_id = format!("{base}-{counter}");
        counter += 1;
    }

    repo::upsert_poster(&st.db, &poster_id, &b.name, b.chat_id).await?;
    Ok(Json(json!({
        "ok": true,
        "poster_id": poster_id,
        "name": b.name,
        "chat_id": b.chat_id,
    })))
}

#[derive(Deserialize)]
struct PosterUpdateBody {
    name: Option<String>,
    chat_id: Option<i64>,
}

async fn update_poster(
    State(st): State<AppState>,
    Path(poster_id): Path<String>,
    Json(b): Json<PosterUpdateBody>,
) -> Result<Json<Value>, TgError> {
    let poster = repo::get_poster(&st.db, &poster_id)
        .await?
        .ok_or_else(|| TgError::NotFound("Poster not found".into()))?;

    if let Some(new_chat) = b.chat_id {
        if new_chat != poster.chat_id {
            let tg = require_bot(&st).await?;
            let info = tg.validate_group(new_chat).await?;
            if !info.is_admin || !info.is_forum {
                return Err(TgError::Bad("Group must be a forum with bot as admin".into()));
            }
        }
    }

    tcfg::update_poster(&st.db, &poster_id, b.name.as_deref(), b.chat_id).await?;
    Ok(Json(json!({ "ok": true, "poster_id": poster_id })))
}

async fn delete_poster(
    State(st): State<AppState>,
    Path(poster_id): Path<String>,
) -> Result<Json<Value>, TgError> {
    if repo::get_poster(&st.db, &poster_id).await?.is_none() {
        return Err(TgError::NotFound("Poster not found".into()));
    }
    tcfg::delete_poster(&st.db, &poster_id).await?;
    Ok(Json(json!({ "ok": true })))
}

// ── Poster ↔ user bindings (Mini App identity; external-client endpoints) ───

/// Gate poster-binding endpoints behind MINIAPP_AGENT_KEY when configured.
/// Binding a Telegram id to a poster grants that user the poster's Mini App
/// content — it must not be public.
fn require_admin_key(headers: &HeaderMap) -> Result<(), TgError> {
    let expected = std::env::var("MINIAPP_AGENT_KEY").unwrap_or_default();
    let expected = expected.trim();
    if expected.is_empty() {
        return Ok(()); // not configured → open, consistent with the app's auth posture
    }
    match headers.get("x-agent-key").and_then(|v| v.to_str().ok()) {
        Some(v) if v.trim() == expected => Ok(()),
        _ => Err(TgError::Unauthorized("invalid or missing agent key".into())),
    }
}

#[derive(Deserialize)]
struct BindUserBody {
    user_id: i64,
    username: Option<String>,
}

async fn bind_poster_user(
    State(st): State<AppState>,
    Path(poster_id): Path<String>,
    headers: HeaderMap,
    Json(b): Json<BindUserBody>,
) -> Result<Json<Value>, TgError> {
    require_admin_key(&headers)?;
    if repo::get_poster(&st.db, &poster_id).await?.is_none() {
        return Err(TgError::NotFound("Poster not found".into()));
    }
    tcfg::bind_user(&st.db, &poster_id, b.user_id, b.username.as_deref()).await?;
    Ok(Json(json!({
        "ok": true,
        "poster_id": poster_id,
        "telegram_user_ids": tcfg::poster_user_ids(&st.db, &poster_id).await?,
        "telegram_username": tcfg::poster_username(&st.db, &poster_id).await?,
    })))
}

async fn unbind_poster_user(
    State(st): State<AppState>,
    Path((poster_id, user_id)): Path<(String, i64)>,
    headers: HeaderMap,
) -> Result<Json<Value>, TgError> {
    require_admin_key(&headers)?;
    if repo::get_poster(&st.db, &poster_id).await?.is_none() {
        return Err(TgError::NotFound("Poster not found".into()));
    }
    tcfg::unbind_user(&st.db, &poster_id, user_id).await?;
    Ok(Json(json!({
        "ok": true,
        "poster_id": poster_id,
        "telegram_user_ids": tcfg::poster_user_ids(&st.db, &poster_id).await?,
    })))
}

// ── Page assignment ──────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct AssignPagesBody {
    page_ids: Vec<String>,
    /// Optional {integration_id: display_name} hint for pages the roster sync
    /// hasn't landed yet (used to seed the placeholder page row).
    page_names: Option<HashMap<String, String>>,
}

/// Assign pages to a poster. Topics are NOT auto-created here (avoids
/// duplicates and Railway request timeouts) — use "Set Up Folders"
/// (POST /posters/{id}/sync-topics) after assigning.
async fn assign_pages(
    State(st): State<AppState>,
    Path(poster_id): Path<String>,
    Json(b): Json<AssignPagesBody>,
) -> Result<Json<Value>, TgError> {
    if repo::get_poster(&st.db, &poster_id).await?.is_none() {
        return Err(TgError::NotFound("Poster not found".into()));
    }

    for page_id in &b.page_ids {
        let name = b
            .page_names
            .as_ref()
            .and_then(|m| m.get(page_id))
            .cloned()
            .unwrap_or_else(|| page_id.clone());
        tcfg::ensure_page(&st.db, page_id, &name).await?;
        repo::assign_page(&st.db, &poster_id, page_id).await?;
    }

    let bot_available = match &st.telegram {
        Some(tg) => tg.is_running().await,
        None => false,
    };
    let poster_page_ids = tcfg::poster_page_ids(&st.db, &poster_id).await?;
    let poster_topics: Vec<String> = tcfg::list_topics(&st.db, "poster", &poster_id)
        .await?
        .into_iter()
        .map(|t| t.page_id)
        .collect();

    Ok(Json(json!({
        "ok": true,
        "assigned": b.page_ids.len(),
        "bot_available": bot_available,
        "poster_page_ids": poster_page_ids,
        "poster_topics": poster_topics,
    })))
}

/// Remove a page assignment; best-effort delete of the poster-group topic.
async fn unassign_page(
    State(st): State<AppState>,
    Path((poster_id, integration_id)): Path<(String, String)>,
) -> Result<Json<Value>, TgError> {
    let poster = repo::get_poster(&st.db, &poster_id)
        .await?
        .ok_or_else(|| TgError::NotFound("Poster not found".into()))?;

    let mut topic_deleted = false;
    if let Some(topic) = tcfg::get_topic(&st.db, "poster", &poster_id, &integration_id).await? {
        if poster.chat_id != 0 {
            if let Some(tg) = &st.telegram {
                if tg.is_running().await {
                    topic_deleted = tg.delete_topic(poster.chat_id, topic.topic_id).await;
                }
            }
        }
    }

    tcfg::unassign_page(&st.db, &poster_id, &integration_id).await?;
    tcfg::remove_topic(&st.db, "poster", &poster_id, &integration_id).await?;
    Ok(Json(json!({ "ok": true, "topic_deleted": topic_deleted })))
}

/// Ensure topics exist in a poster's group for all assigned pages, plus the
/// "Campaign Sounds" topic. APPEND-ONLY: existing mappings are never touched.
async fn sync_poster_topics(
    State(st): State<AppState>,
    Path(poster_id): Path<String>,
) -> Result<Json<Value>, TgError> {
    let tg = require_bot(&st).await?;
    let poster = repo::get_poster(&st.db, &poster_id)
        .await?
        .ok_or_else(|| TgError::NotFound("Poster not found".into()))?;
    let chat_id = require_configured_chat(&poster)?;

    let page_ids = tcfg::poster_page_ids(&st.db, &poster_id).await?;
    let mut created = 0u32;
    let mut existing = 0u32;
    let mut errors: Vec<String> = Vec::new();

    for page_id in &page_ids {
        // Re-read fresh each iteration (append-only check).
        if tcfg::get_topic(&st.db, "poster", &poster_id, page_id).await?.is_some() {
            existing += 1;
            continue;
        }
        let page_name = tcfg::page_name(&st.db, page_id)
            .await?
            .unwrap_or_else(|| page_id.clone());
        match create_topic_with_retry(&tg, chat_id, &page_name).await {
            Ok(topic_id) => {
                tcfg::set_topic(&st.db, "poster", &poster_id, page_id, topic_id, &page_name, false)
                    .await?;
                created += 1;
            }
            Err(e) => errors.push(format!("{page_name}: {e}")),
        }
        tokio::time::sleep(StdDuration::from_millis(1500)).await;
    }

    // Ensure a "Campaign Sounds" topic exists for sound-link forwarding.
    let mut sounds_topic_created = false;
    let fresh = repo::get_poster(&st.db, &poster_id).await?;
    if fresh.map(|p| p.sounds_topic_id.is_none()).unwrap_or(false) {
        match create_topic_with_retry(&tg, chat_id, "Campaign Sounds").await {
            Ok(tid) => {
                tcfg::set_poster_sounds_topic(&st.db, &poster_id, tid).await?;
                sounds_topic_created = true;
            }
            Err(e) => errors.push(format!("Sounds topic: {e}")),
        }
    }

    Ok(Json(json!({
        "created": created,
        "existing": existing,
        "sounds_topic_created": sounds_topic_created,
        "errors": errors,
        "total_pages": page_ids.len(),
    })))
}

// ── Content: send / assign-batch / forward ───────────────────────────────────

/// "video" vs "document", the same split the Python /send used for inventory.
fn media_type_of(path: &std::path::Path) -> &'static str {
    match path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_ascii_lowercase()
        .as_str()
    {
        "mp4" | "mov" | "avi" | "mkv" => "video",
        _ => "document",
    }
}

/// Confine a request-supplied file path under the data dir (the Rust analog of
/// the Python PROJECT_ROOT check). Relative paths are joined + traversal-
/// checked; absolute paths must canonicalize inside the data dir.
fn confine_file(data_dir: &str, p: &str) -> Result<PathBuf, TgError> {
    let path = std::path::Path::new(p);
    if path.is_absolute() {
        let base = PathBuf::from(data_dir)
            .canonicalize()
            .map_err(|_| TgError::Bad("data directory unavailable".into()))?;
        return match path.canonicalize() {
            Ok(c) if c.starts_with(&base) => Ok(c),
            Ok(_) => Err(TgError::Bad("File path outside project root".into())),
            // Missing file: decide 400 vs 404 by canonicalizing the parent dir
            // (a textual prefix check would break on symlinked temp/volume
            // roots, e.g. /var → /private/var on macOS).
            Err(_) => {
                let parent_in_base = path
                    .parent()
                    .and_then(|p| p.canonicalize().ok())
                    .map(|p| p.starts_with(&base))
                    .unwrap_or(false);
                if parent_in_base {
                    Err(TgError::NotFound("File not found".into()))
                } else {
                    Err(TgError::Bad("File path outside project root".into()))
                }
            }
        };
    }
    crate::paths::confine_under_data(data_dir, p).map_err(TgError::Bad)
}

#[derive(Deserialize)]
struct SendBody {
    integration_id: String,
    file_path: String,
    caption: Option<String>,
}

/// Send a media file to the staging topic for a page and record ONE inventory
/// row for it. Append-only — this never deletes or reposts.
async fn send_content(
    State(st): State<AppState>,
    Json(b): Json<SendBody>,
) -> Result<Json<Value>, TgError> {
    let tg = require_bot(&st).await?;

    let file_path = confine_file(&data_dir(), &b.file_path)?;
    if !file_path.exists() {
        return Err(TgError::NotFound("File not found".into()));
    }

    let Some((staging_chat, _)) = tcfg::staging_group(&st.db).await? else {
        return Err(TgError::Bad("Staging group not configured".into()));
    };
    let topic = tcfg::get_topic(&st.db, "staging", "", &b.integration_id)
        .await?
        .ok_or_else(|| {
            TgError::Bad(format!("No staging topic for integration {}", b.integration_id))
        })?;

    // Serialize sends for this page (double-click safety).
    let lock = st.page_lock(&b.integration_id);
    let _guard = lock.lock().await;

    let (message_id, file_id) = tg
        .send_media(staging_chat, Some(topic.topic_id), &file_path, b.caption.as_deref())
        .await?;

    let file_name = file_path
        .file_name()
        .and_then(|f| f.to_str())
        .unwrap_or("file")
        .to_string();
    let media_type = media_type_of(&file_path);
    let meta = json!({
        "file_id": file_id,
        "file_name": file_name,
        "media_type": media_type,
        "caption": b.caption,
        "source": "api",
    });
    let item = tcfg::add_inventory_with_meta(
        &st.db,
        &b.integration_id,
        None,
        Some(message_id),
        &meta.to_string(),
    )
    .await?;

    // Same shape the Python endpoint returned (the raw inventory item).
    Ok(Json(json!({
        "id": item.id,
        "added_at": item.created_at,
        "forwarded": {},
        "message_id": message_id,
        "file_id": file_id,
        "file_name": file_name,
        "media_type": media_type,
        "caption": b.caption,
        "source": "api",
    })))
}

/// Round-robin split of `items` across `n` buckets (bucket i gets items
/// i, i+n, i+2n, …) — the Python assign-batch distribution.
fn round_robin<T: Clone>(items: &[T], n: usize) -> Vec<Vec<T>> {
    let mut out = vec![Vec::new(); n.max(1)];
    for (idx, item) in items.iter().enumerate() {
        out[idx % n.max(1)].push(item.clone());
    }
    out
}

fn active_assign_batches() -> &'static StdMutex<HashSet<String>> {
    static ACTIVE: OnceLock<StdMutex<HashSet<String>>> = OnceLock::new();
    ACTIVE.get_or_init(Default::default)
}

/// Removes the batch key from the active set when the request finishes —
/// including early `?` returns.
struct BatchGuard(String);
impl Drop for BatchGuard {
    fn drop(&mut self) {
        active_assign_batches().lock().unwrap().remove(&self.0);
    }
}

#[derive(Deserialize)]
struct AssignBatchBody {
    batch_id: String,
    project: String,
    integration_ids: Vec<String>,
}

/// Round-robin split burned videos across pages and send each page's share to
/// its staging topic. One inventory row per real message.
async fn assign_batch(
    State(st): State<AppState>,
    Json(b): Json<AssignBatchBody>,
) -> Result<Json<Value>, TgError> {
    let tg = require_bot(&st).await?;

    // Double-send guard (a second identical request while one runs → 409).
    let batch_key = format!("{}:{}", b.project, b.batch_id);
    {
        let mut active = active_assign_batches().lock().unwrap();
        if !active.insert(batch_key.clone()) {
            return Err(TgError::Conflict("This batch is already being assigned".into()));
        }
    }
    let _guard = BatchGuard(batch_key);

    let project = crate::paths::sanitize_project_name(&b.project)
        .ok_or_else(|| TgError::Bad("invalid project name".into()))?;
    if b.batch_id.contains("..") || b.batch_id.contains('/') || b.batch_id.contains('\\') {
        return Err(TgError::Bad("invalid batch_id".into()));
    }

    // Batch dirs are stored data-relative — resolve exactly like asset paths.
    let batch_dir =
        super::jobs::resolve_asset_path(&format!("projects/{project}/burned/{}", b.batch_id));
    if !batch_dir.is_dir() {
        return Err(TgError::NotFound(format!("Batch '{}' not found", b.batch_id)));
    }

    // burned_*.mp4, sorted by name (matches the Python glob).
    let mut mp4s: Vec<PathBuf> = Vec::new();
    let mut rd = tokio::fs::read_dir(&batch_dir)
        .await
        .map_err(|e| TgError::Other(e.into()))?;
    while let Some(entry) = rd.next_entry().await.map_err(|e| TgError::Other(e.into()))? {
        let name = entry.file_name().to_string_lossy().to_string();
        if name.starts_with("burned_") && name.ends_with(".mp4") {
            mp4s.push(entry.path());
        }
    }
    mp4s.sort();
    if mp4s.is_empty() {
        return Err(TgError::NotFound("No burned videos in batch".into()));
    }
    if b.integration_ids.is_empty() {
        return Err(TgError::Bad("No pages selected".into()));
    }

    let Some((staging_chat, _)) = tcfg::staging_group(&st.db).await? else {
        return Err(TgError::Bad("Staging group not configured".into()));
    };

    // Validate every page has a staging topic BEFORE sending anything.
    struct ValidPage {
        integration_id: String,
        topic_id: i64,
        page_name: String,
    }
    let mut valid_pages: Vec<ValidPage> = Vec::new();
    for iid in &b.integration_ids {
        let topic = tcfg::get_topic(&st.db, "staging", "", iid).await?.ok_or_else(|| {
            TgError::Bad(format!("Page {iid} has no staging topic. Run 'Set Up Folders' first."))
        })?;
        let page_name = tcfg::page_name(&st.db, iid).await?.unwrap_or_else(|| iid.clone());
        valid_pages.push(ValidPage {
            integration_id: iid.clone(),
            topic_id: topic.topic_id,
            page_name,
        });
    }

    let shares = round_robin(&mp4s, valid_pages.len());
    let mut results: Vec<Value> = Vec::new();

    for (page, files) in valid_pages.iter().zip(shares.iter()) {
        let mut sent = 0u32;
        let mut errors: Vec<String> = Vec::new();
        for mp4 in files {
            let fname = mp4
                .file_name()
                .and_then(|f| f.to_str())
                .unwrap_or("file")
                .to_string();
            match tg.send_media(staging_chat, Some(page.topic_id), mp4, None).await {
                Ok((message_id, file_id)) => {
                    let meta = json!({
                        "file_id": file_id,
                        "file_name": fname,
                        "media_type": "video",
                        "caption": null,
                        "source": "assign-batch",
                    });
                    tcfg::add_inventory_with_meta(
                        &st.db,
                        &page.integration_id,
                        None,
                        Some(message_id),
                        &meta.to_string(),
                    )
                    .await?;
                    sent += 1;
                    tokio::time::sleep(StdDuration::from_millis(300)).await;
                }
                Err(e) => {
                    tracing::warn!("assign-batch send fail {fname}→{}: {e:#}", page.integration_id);
                    errors.push(format!("{fname}: {e:#}"));
                }
            }
        }
        let file_names: Vec<String> = files
            .iter()
            .map(|f| f.file_name().and_then(|x| x.to_str()).unwrap_or("file").to_string())
            .collect();
        results.push(json!({
            "integration_id": page.integration_id,
            "page_name": page.page_name,
            "files": file_names,
            "sent": sent,
            "errors": errors,
        }));
    }

    Ok(Json(json!({
        "total": mp4s.len(),
        "pages_used": valid_pages.len(),
        "assignments": results,
        "batch_id": b.batch_id,
    })))
}

/// Forward a page's PENDING inventory to its assigned poster's topic.
///
/// HARD RULE 4 — per-row claim→confirm/release, concurrency-safe:
///   1. a per-page async lock serializes forwards for the same page,
///   2. rows are ATOMICALLY claimed before sending (a second concurrent caller
///      claims zero rows),
///   3. each claimed row is CONFIRMED on success or RELEASED back to pending on
///      failure — never silently lost, never double-forwarded.
async fn forward_page(
    State(st): State<AppState>,
    Path(integration_id): Path<String>,
) -> Result<Json<Value>, TgError> {
    let tg = require_bot(&st).await?;

    let poster = repo::poster_for_page(&st.db, &integration_id)
        .await?
        .ok_or_else(|| TgError::Bad(format!("No poster assigned for page {integration_id}")))?;
    let poster_chat = require_configured_chat(&poster)?;

    let poster_topic = tcfg::get_topic(&st.db, "poster", &poster.id, &integration_id)
        .await?
        .ok_or_else(|| {
            TgError::Bad("Poster has no folder for this page — run Set Up Folders first".into())
        })?;
    let Some((staging_chat, _)) = tcfg::staging_group(&st.db).await? else {
        return Err(TgError::Bad("Staging group not configured".into()));
    };
    if tcfg::get_topic(&st.db, "staging", "", &integration_id).await?.is_none() {
        return Err(TgError::Bad("No staging folder for this page".into()));
    }

    let lock = st.page_lock(&integration_id);
    let _guard = lock.lock().await;

    let (forwarded, skipped) = forward_claimed_rows(
        &st.db,
        &tg,
        &integration_id,
        &poster.id,
        poster_chat,
        Some(poster_topic.topic_id),
        staging_chat,
    )
    .await?;

    Ok(Json(json!({
        "forwarded": forwarded,
        "skipped": skipped,
        "poster_id": poster.id,
    })))
}

/// The shared claim→forward→confirm/release loop (manual forward + daily batch).
async fn forward_claimed_rows(
    db: &clab_core::Db,
    tg: &Telegram,
    page_id: &str,
    poster_id: &str,
    poster_chat: i64,
    poster_topic: Option<i64>,
    staging_chat: i64,
) -> Result<(u32, u32), TgError> {
    let claimed = repo::claim_pending_inventory(db, page_id, poster_id).await?;
    let mut forwarded = 0u32;
    let mut skipped = 0u32;
    for item in &claimed {
        let Some(msg_id) = item.staging_msg_id else {
            // Nothing to forward for this row — release the claim, skip.
            repo::release_claim(db, &item.id, poster_id).await?;
            skipped += 1;
            continue;
        };
        match tg.forward(poster_chat, poster_topic, staging_chat, msg_id).await {
            Ok(fwd_id) => {
                repo::confirm_forwarded(db, &item.id, poster_id, fwd_id).await?;
                forwarded += 1;
            }
            Err(e) => {
                // Release so a retry picks it up — no silent loss.
                repo::release_claim(db, &item.id, poster_id).await?;
                skipped += 1;
                tracing::warn!("forward failed for inventory {}: {e:#}", item.id);
            }
        }
    }
    Ok((forwarded, skipped))
}

// ── Schedule ─────────────────────────────────────────────────────────────────

async fn get_schedule(State(st): State<AppState>) -> Result<Json<Value>, TgError> {
    let s = tcfg::get_schedule(&st.db).await?;
    Ok(Json(serde_json::to_value(s).unwrap_or(Value::Null)))
}

#[derive(Deserialize)]
struct ScheduleBody {
    enabled: Option<bool>,
    forward_time: Option<String>,
    timezone: Option<String>,
}

async fn put_schedule(
    State(st): State<AppState>,
    Json(b): Json<ScheduleBody>,
) -> Result<Json<Value>, TgError> {
    let s = tcfg::update_schedule(
        &st.db,
        b.enabled,
        b.forward_time.as_deref(),
        b.timezone.as_deref(),
    )
    .await?;
    Ok(Json(json!({ "ok": true, "schedule": s })))
}

// ── Daily batch ──────────────────────────────────────────────────────────────

async fn trigger_batch_run(State(st): State<AppState>) -> Result<Json<Value>, TgError> {
    require_bot(&st).await?;
    let result = run_daily_batch(&st).await?;
    Ok(Json(result))
}

/// Execute the daily batch: forward pending inventory to every configured
/// poster and post a per-poster summary (with today's sounds). Uses the same
/// claim→confirm path as the manual endpoint, so scheduled and manual sends
/// can't diverge — and can't double-forward each other.
async fn run_daily_batch(st: &AppState) -> Result<Value, TgError> {
    let tg = require_bot(st).await?;

    let Some((staging_chat, _)) = tcfg::staging_group(&st.db).await? else {
        return Ok(json!({ "posters_notified": 0, "videos_forwarded": 0, "sounds_sent": 0 }));
    };

    let posters = repo::list_posters(&st.db).await?;
    let sounds = tcfg::active_sounds(&st.db).await?;
    let schedule = tcfg::get_schedule(&st.db).await?;

    let mut total_forwarded = 0u32;
    let mut posters_notified = 0u32;
    let now = Utc::now();
    let local_now = now + Duration::minutes(zone_offset_minutes(&schedule.timezone, now) as i64);
    let today = local_now.format("%B %d, %Y").to_string();

    for poster in &posters {
        // chat_id = 0 → unconfigured: the batch silently skips (mirrors the
        // Python falsy-chat_id skip); the MANUAL endpoints 400 instead.
        if poster.chat_id == 0 {
            continue;
        }

        let page_ids = tcfg::poster_page_ids(&st.db, &poster.id).await?;
        let mut summary_lines: Vec<String> = Vec::new();
        let mut page_count = 0u32;
        let mut poster_total = 0u32;

        for page_id in &page_ids {
            if tcfg::get_topic(&st.db, "staging", "", page_id).await?.is_none() {
                continue;
            }
            let Some(poster_topic) =
                tcfg::get_topic(&st.db, "poster", &poster.id, page_id).await?
            else {
                continue;
            };
            let page_name = tcfg::page_name(&st.db, page_id)
                .await?
                .unwrap_or_else(|| page_id.clone());

            let lock = st.page_lock(page_id);
            let _guard = lock.lock().await;
            match forward_claimed_rows(
                &st.db,
                &tg,
                page_id,
                &poster.id,
                poster.chat_id,
                Some(poster_topic.topic_id),
                staging_chat,
            )
            .await
            {
                Ok((forwarded, _skipped)) if forwarded > 0 => {
                    summary_lines.push(format!("\u{1F4F1} {page_name}: {forwarded} new video(s)"));
                    poster_total += forwarded;
                    page_count += 1;
                }
                Ok(_) => {}
                Err(e) => tracing::error!("batch forward error page={page_id}: {e:?}"),
            }
        }

        total_forwarded += poster_total;

        if poster_total == 0 && sounds.is_empty() {
            continue;
        }

        // Build the summary message (same layout as the Python batch).
        let mut parts: Vec<String> = vec![format!("\u{1F4CB} Daily Content Drop \u{2014} {today}")];
        if !summary_lines.is_empty() {
            parts.push(String::new());
            parts.extend(summary_lines);
            parts.push(String::new());
            parts.push(format!("Total: {poster_total} videos across {page_count} pages"));
        }
        if !sounds.is_empty() {
            parts.push(String::new());
            parts.push("\u{1F3B5} Today's Sounds:".to_string());
            for s in &sounds {
                parts.push(format!("\u{2022} {}: {}", s.label, s.url));
            }
        }
        match tg.send_text(poster.chat_id, None, &parts.join("\n")).await {
            Ok(_) => posters_notified += 1,
            Err(e) => tracing::error!("summary send error poster={}: {e:#}", poster.id),
        }
    }

    tcfg::set_last_run(&st.db, &Utc::now().to_rfc3339()).await?;

    Ok(json!({
        "posters_notified": posters_notified,
        "videos_forwarded": total_forwarded,
        "sounds_sent": sounds.len(),
        // Per-page sound-assignment sends belong to the sounds workstream;
        // reported as 0 until that path is wired in.
        "sound_assignments_sent": 0,
    }))
}

// ── Schedule loop ────────────────────────────────────────────────────────────
// NOT wired into main.rs here — the lead wires `spawn_schedule_loop(state)` at
// integration (shared-seam file). HARD RULE 3: this loop never sends at
// startup; its first action is always a sleep until the configured time.

/// Background task that runs the daily batch at schedule.forward_time in
/// schedule.timezone, when enabled. Call once from main after AppState is
/// built: `distribution::spawn_schedule_loop(state.clone());`
#[allow(dead_code)] // awaiting the main.rs seam wiring at integration
pub fn spawn_schedule_loop(state: AppState) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        loop {
            let sched = match tcfg::get_schedule(&state.db).await {
                Ok(s) => s,
                Err(e) => {
                    tracing::error!("schedule read error: {e}");
                    tokio::time::sleep(StdDuration::from_secs(60)).await;
                    continue;
                }
            };
            if !sched.enabled {
                tokio::time::sleep(StdDuration::from_secs(60)).await;
                continue;
            }
            let wait = seconds_until_next_run(&sched.forward_time, &sched.timezone, Utc::now());
            tokio::time::sleep(wait).await;

            // Re-check: the schedule may have been disabled during the sleep.
            match tcfg::get_schedule(&state.db).await {
                Ok(s) if s.enabled => {
                    if let Err(e) = run_daily_batch(&state).await {
                        tracing::error!("scheduled batch failed: {e:?}");
                        // Back off so a persistent failure (e.g. bot down)
                        // doesn't spin.
                        tokio::time::sleep(StdDuration::from_secs(60)).await;
                    }
                }
                Ok(_) => {}
                Err(e) => tracing::error!("schedule re-read error: {e}"),
            }
        }
    })
}

/// Parse "HH:MM"; falls back to 09:00 on any malformed input (the Python
/// scheduler would error+retry; a sane default is strictly better).
fn parse_hhmm(s: &str) -> Option<(u32, u32)> {
    let (h, m) = s.split_once(':')?;
    let h: u32 = h.trim().parse().ok()?;
    let m: u32 = m.trim().parse().ok()?;
    if h > 23 || m > 59 {
        return None;
    }
    Some((h, m))
}

/// UTC offset in minutes for the small set of IANA zones the schedule uses
/// (no chrono-tz in the workspace; US DST rules are computed directly).
/// Unknown zones fall back to America/New_York, the schedule default.
fn zone_offset_minutes(tz: &str, utc: DateTime<Utc>) -> i32 {
    let (std_off, dst_off) = match tz {
        "UTC" | "Etc/UTC" => (0, 0),
        "America/Chicago" => (-360, -300),
        "America/Denver" => (-420, -360),
        "America/Phoenix" => (-420, -420), // no DST
        "America/Los_Angeles" => (-480, -420),
        _ => (-300, -240), // America/New_York (default)
    };
    if std_off == dst_off {
        return std_off;
    }
    let local_std = (utc + Duration::minutes(std_off as i64)).naive_utc();
    if us_dst_active(local_std) {
        dst_off
    } else {
        std_off
    }
}

/// US DST: from the second Sunday of March 02:00 (standard time) to the first
/// Sunday of November 01:00 (standard time == 02:00 daylight).
fn us_dst_active(local_std: NaiveDateTime) -> bool {
    let y = local_std.year();
    let start = nth_weekday(y, 3, Weekday::Sun, 2).and_hms_opt(2, 0, 0).unwrap();
    let end = nth_weekday(y, 11, Weekday::Sun, 1).and_hms_opt(1, 0, 0).unwrap();
    local_std >= start && local_std < end
}

/// The n-th given weekday of a month (n is 1-based).
fn nth_weekday(year: i32, month: u32, wd: Weekday, n: u32) -> NaiveDate {
    let mut d = NaiveDate::from_ymd_opt(year, month, 1).expect("valid month start");
    let mut count = 0;
    loop {
        if d.weekday() == wd {
            count += 1;
            if count == n {
                return d;
            }
        }
        d = d.succ_opt().expect("date overflow");
    }
}

/// How long to sleep until the next `forward_time` in `tz` (strictly future;
/// a target equal to now rolls to tomorrow — same as the Python scheduler).
fn seconds_until_next_run(forward_time: &str, tz: &str, now_utc: DateTime<Utc>) -> StdDuration {
    let (h, m) = parse_hhmm(forward_time).unwrap_or((9, 0));
    let off_now = zone_offset_minutes(tz, now_utc);
    let local_now = (now_utc + Duration::minutes(off_now as i64)).naive_utc();

    let target_time = NaiveTime::from_hms_opt(h, m, 0).expect("validated hh:mm");
    let mut target_local = local_now.date().and_time(target_time);
    if target_local <= local_now {
        target_local = local_now
            .date()
            .succ_opt()
            .expect("date overflow")
            .and_time(target_time);
    }

    // Convert local target → UTC using the offset AT the target date (so a DST
    // boundary between now and the target lands on the right instant).
    let approx_utc = Utc.from_utc_datetime(&target_local) - Duration::minutes(off_now as i64);
    let off_target = zone_offset_minutes(tz, approx_utc);
    let target_utc = Utc.from_utc_datetime(&target_local) - Duration::minutes(off_target as i64);

    (target_utc - now_utc)
        .to_std()
        // A DST edge can technically produce a non-positive wait; retry soon.
        .unwrap_or(StdDuration::from_secs(60))
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use clab_core::model::Poster;

    fn utc(y: i32, mo: u32, d: u32, h: u32, mi: u32) -> DateTime<Utc> {
        Utc.with_ymd_and_hms(y, mo, d, h, mi, 0).unwrap()
    }

    // HARD RULE 5: chat_id = 0 posters are refused with a clear 400.
    #[test]
    fn chat_id_zero_is_refused() {
        let poster = Poster {
            id: "p".into(),
            name: "Seffra".into(),
            chat_id: 0,
            sounds_topic_id: None,
            created_at: Utc::now(),
            updated_at: Utc::now(),
        };
        let err = require_configured_chat(&poster).unwrap_err();
        match err {
            TgError::Bad(msg) => {
                assert!(msg.contains("Seffra"), "error must name the poster: {msg}");
                assert!(msg.contains("chat_id = 0"), "error must explain why: {msg}");
            }
            other => panic!("expected Bad(400), got {other:?}"),
        }
        let poster_ok = Poster { chat_id: -100123, ..poster };
        assert_eq!(require_configured_chat(&poster_ok).unwrap(), -100123);
    }

    #[test]
    fn slugify_matches_python() {
        assert_eq!(slugify("Johnny Balik"), "johnny-balik");
        assert_eq!(slugify("  Eric  Cromartie!! "), "eric-cromartie");
        assert_eq!(slugify("Seño ra"), "se-o-ra");
        assert_eq!(slugify("!!!"), "poster");
        assert_eq!(slugify(""), "poster");
    }

    #[test]
    fn round_robin_splits_evenly() {
        let items: Vec<i32> = (0..7).collect();
        let split = round_robin(&items, 3);
        assert_eq!(split.len(), 3);
        assert_eq!(split[0], vec![0, 3, 6]);
        assert_eq!(split[1], vec![1, 4]);
        assert_eq!(split[2], vec![2, 5]);
        // Degenerate cases don't panic.
        assert_eq!(round_robin(&items, 1).len(), 1);
        assert_eq!(round_robin::<i32>(&[], 3).iter().map(Vec::len).sum::<usize>(), 0);
    }

    #[test]
    fn media_type_by_extension() {
        assert_eq!(media_type_of(std::path::Path::new("a/burned_000.mp4")), "video");
        assert_eq!(media_type_of(std::path::Path::new("clip.MOV")), "video");
        assert_eq!(media_type_of(std::path::Path::new("caption.txt")), "document");
        assert_eq!(media_type_of(std::path::Path::new("noext")), "document");
    }

    #[test]
    fn parse_hhmm_bounds() {
        assert_eq!(parse_hhmm("09:00"), Some((9, 0)));
        assert_eq!(parse_hhmm("23:59"), Some((23, 59)));
        assert_eq!(parse_hhmm("24:00"), None);
        assert_eq!(parse_hhmm("9"), None);
        assert_eq!(parse_hhmm("garbage"), None);
    }

    #[test]
    fn dst_boundaries_2026() {
        // 2026: DST starts Mar 8, ends Nov 1.
        assert_eq!(nth_weekday(2026, 3, Weekday::Sun, 2), NaiveDate::from_ymd_opt(2026, 3, 8).unwrap());
        assert_eq!(nth_weekday(2026, 11, Weekday::Sun, 1), NaiveDate::from_ymd_opt(2026, 11, 1).unwrap());

        let d = |mo, day, h, mi| {
            NaiveDate::from_ymd_opt(2026, mo, day).unwrap().and_hms_opt(h, mi, 0).unwrap()
        };
        assert!(!us_dst_active(d(3, 8, 1, 59)), "just before spring-forward");
        assert!(us_dst_active(d(3, 8, 2, 0)), "at spring-forward");
        assert!(us_dst_active(d(7, 2, 12, 0)), "midsummer");
        assert!(us_dst_active(d(11, 1, 0, 59)), "just before fall-back");
        assert!(!us_dst_active(d(11, 1, 1, 0)), "at fall-back (std time)");
        assert!(!us_dst_active(d(1, 15, 12, 0)), "midwinter");
    }

    #[test]
    fn zone_offsets() {
        let winter = utc(2026, 1, 15, 12, 0);
        let summer = utc(2026, 7, 15, 12, 0);
        assert_eq!(zone_offset_minutes("UTC", winter), 0);
        assert_eq!(zone_offset_minutes("America/New_York", winter), -300);
        assert_eq!(zone_offset_minutes("America/New_York", summer), -240);
        assert_eq!(zone_offset_minutes("America/Chicago", summer), -300);
        assert_eq!(zone_offset_minutes("America/Phoenix", winter), -420);
        assert_eq!(zone_offset_minutes("America/Phoenix", summer), -420, "Phoenix has no DST");
        // Unknown zone → New York default.
        assert_eq!(zone_offset_minutes("Mars/Olympus_Mons", winter), -300);
    }

    #[test]
    fn next_run_timing() {
        // Winter: 09:00 NY = 14:00 UTC. Now 13:00 UTC → wait exactly 1h.
        let wait = seconds_until_next_run("09:00", "America/New_York", utc(2026, 1, 15, 13, 0));
        assert_eq!(wait.as_secs(), 3600);

        // Summer: 09:00 NY = 13:00 UTC. Now 13:00 UTC (== target) → tomorrow.
        let wait = seconds_until_next_run("09:00", "America/New_York", utc(2026, 7, 15, 13, 0));
        assert_eq!(wait.as_secs(), 24 * 3600);

        // Already past today → tomorrow's slot.
        let wait = seconds_until_next_run("09:00", "America/New_York", utc(2026, 1, 15, 15, 0));
        assert_eq!(wait.as_secs(), 23 * 3600);

        // UTC zone, plain.
        let wait = seconds_until_next_run("09:00", "UTC", utc(2026, 1, 15, 8, 0));
        assert_eq!(wait.as_secs(), 3600);

        // Malformed time falls back to 09:00.
        let wait = seconds_until_next_run("bogus", "UTC", utc(2026, 1, 15, 8, 0));
        assert_eq!(wait.as_secs(), 3600);

        // Never zero/negative.
        let wait = seconds_until_next_run("09:00", "UTC", utc(2026, 1, 15, 9, 0));
        assert!(wait.as_secs() > 0);
    }

    #[test]
    fn confine_file_rules() {
        // Relative traversal is rejected; clean relative is allowed (delegates
        // to paths::confine_under_data, which has its own tests).
        assert!(matches!(confine_file("/nonexistent-data", "../etc/passwd"), Err(TgError::Bad(_))));
        assert!(confine_file("/nonexistent-data", "projects/x/a.mp4").is_ok());

        // Absolute path outside the data dir → 400 (use a base that exists so
        // canonicalize succeeds).
        let tmp = std::env::temp_dir();
        let base = tmp.to_string_lossy().to_string();
        match confine_file(&base, "/etc/passwd") {
            Err(TgError::Bad(m)) => assert!(m.contains("outside")),
            other => panic!("expected Bad, got {:?}", other.map(|_| ())),
        }
        // Absolute path inside the data dir but missing → 404.
        let missing = tmp.join("clab-definitely-missing-file.mp4");
        match confine_file(&base, &missing.to_string_lossy()) {
            Err(TgError::NotFound(_)) => {}
            other => panic!("expected NotFound, got {:?}", other.map(|_| ())),
        }
    }
}
